#!/usr/bin/env python3
"""
Vlocal v18 — Diarisation locale « qui parle quand » (sherpa-onnx, 100% local).

Choix d'architecture (tranché avec l'utilisateur) : backend LÉGER sherpa-onnx
plutôt que pyannote.audio. Pourquoi :
  - zéro torch (pyannote tirait ~1,2 Go de torch/lightning/speechbrain),
  - zéro modèle « gated » (pyannote exige un token HuggingFace + CGU),
  - modèles ONNX ~28 Mo utiles (embeddings locuteur CAM++ 3D-Speaker 192-d,
    27 Mo + segmentation pyannote-3.0 exportée int8, 1,5 Mo), bundle .app propre,
  - onnxruntime est DÉJÀ une dépendance (faster-whisper l'utilise).

Tout est optionnel et à repli silencieux : si sherpa-onnx ou les modèles sont
absents, `available()` renvoie False et le pipeline réunion retombe sur
l'affichage continu sans locuteurs (aucun crash).

Ce module contient :
  - Diarizer : adaptateur sherpa-onnx (load / unload / diarize),
  - des FONCTIONS PURES de fusion timestamps Whisper <-> locuteurs, de
    renommage et de correction manuelle — testables sans aucun modèle.
"""

import os
import threading

_BASE = os.path.dirname(os.path.abspath(__file__))
_SEG_DIR = os.path.join(_BASE, "models", "diarization",
                        "sherpa-onnx-pyannote-segmentation-3-0")
# v18.5 — on préfère le modèle de segmentation int8 (plus rapide) s'il est là.
_SEG_INT8 = os.path.join(_SEG_DIR, "model.int8.onnx")
SEG_MODEL = _SEG_INT8 if os.path.exists(_SEG_INT8) else os.path.join(_SEG_DIR, "model.onnx")
# Override R&D (env) : tester un autre modèle d'empreinte sans toucher au code
# (hérité par les workers sous-process). Ex. VLOCAL_EMB_MODEL=models/_diar_backup/...
EMB_MODEL = (os.environ.get("VLOCAL_EMB_MODEL")
             or os.path.join(_BASE, "models", "diarization", "embedding.onnx"))

# v18.6 — Embeddings = 3D-Speaker CAM++ (multilingue, robuste canal/bruit) au
# lieu de NeMo TitaNet-small (anglophone, cause racine de la sur-segmentation sur
# voix FR/téléphonique). Seuil AUTO recalibré pour CAM++ (les 0,70 compensaient
# un embedding cassé). Plus HAUT = fusionne plus = MOINS de locuteurs.
CLUSTER_THRESHOLD = 0.60


# NOTE (leçon mesurée, v18.6) : un prétraitement pré-emphase + normalisation RMS
# avant CAM++ a été testé et DÉGRADE la séparation des voix (CAM++ normalise déjà
# en interne) — ne pas réintroduire. L'audio brut alimente directement l'extracteur.

# Constantes PARTAGÉES live (finalize_stream) / offline (_whotalks, diarize) :
# un réglage = UN seul endroit, plus de divergence silencieuse entre copies.
TILE_WIN_S = 2.0           # fenêtre d'empreinte CAM++ (s)
TILE_HOP_S = 1.0           # pas entre fenêtres (s)
DISSOLVE_MIN_SHARE = 0.02  # _dissolve_minor : part de temps mini d'une voix
DISSOLVE_MIN_DUR = 3.0     # _dissolve_minor : durée cumulée mini (s)
POST_MIN_DUR = 1.0         # postprocess_segments : durée mini d'un segment (s)
POST_GAP = 0.5             # postprocess_segments : trou max fusionné (s)
# v22 — Fusion ANTI « LOCUTEUR INVENTÉ » (mode auto seulement) : deux clusters
# dont les centroïdes CAM++ dépassent ce cosinus sont LA MÊME personne scindée
# par le clustering. Calibré sur corpus : un vrai locuteur scindé donne
# cos 0,66-0,84 entre ses moitiés ; deux personnes distinctes restent < 0,57
# (et < 0,4 typiquement). 0,65 sépare nettement les deux distributions.
MERGE_CENTROID_COS = 0.65

# v25 — Surcouche "Turn-Aware Consensus" (robuste à l'intonation). DIAR_TURN_LAMBDA :
# coût d'un CHANGEMENT de locuteur dans l'assignation Viterbi (plus haut = plus
# "collant" -> un "oui" intonné isolé n'invente plus de voix). Désactivable d'un
# flag pour prouver la non-régression différentielle (réfs inchangées). Calibré corpus.
DIAR_TURN_LAMBDA = 0.40
# OFF par défaut : sur les réfs à voix STABLES (sans bruit d'intonation), le
# Viterbi ne fait que casser de vrais tours (0,039 -> 0,05). On ne l'active+calibre
# qu'avec un enregistrement RÉEL à intonation variée (le bruit qu'il doit filtrer).
DIAR_USE_VITERBI = False

# v26 — FUSION PAR ÉVIDENCE DE LIENS (anti « voix inventée par l'intonation »,
# mode AUTO). Les sous-régions d'un MÊME segment Whisper sont du même locuteur
# par construction : si deux clusters sont massivement reliés par ces paires
# (taux >= LINK_MIN_RATE des adjacences, >= LINK_MIN_COUNT liens), c'est UNE voix
# scindée (intonation) -> fusion. Entre deux VRAIS locuteurs le taux mesuré est
# ~3-6 % (réfs) : la passe ne les touche jamais. Gloutonne et RE-ÉVALUÉE après
# chaque fusion (pas de pont transitif). Banc : 2 voix dont 1 « en vrille »
# (pitch ±3 st sur moitié des régions) : K 3-4 -> 2, justesse 0,72 -> 0,95-0,98 ;
# réfs propres bit-à-bit inchangées.
DIAR_LINK_MERGE = True
LINK_MIN_COUNT = 3
LINK_MIN_RATE = 0.30
# v29 — seuil de bascule en attribution TOUT-AU-MOT (lent mais robuste aux
# chevauchements denses). Relevé 0.045 -> 0.06 : l'AGC resserre les marges, des
# entretiens NETS franchissaient 4,5 % sans gain de justesse (mesuré). 0.06 garde
# la bascule pour les vrais chevauchements sans pénaliser la vitesse des nets.
WORD_ALLWORD_TRIP = 0.06


def _merge_close_centroids(E, lab, threshold=MERGE_CENTROID_COS, times=None):
    """v22 — Fusionne itérativement les clusters dont les centroïdes sont plus
    proches que `threshold` en cosinus : un vrai locuteur SCINDÉ en deux par le
    clustering (le « locuteur inventé ») a des moitiés quasi colinéaires ; deux
    personnes distinctes jamais (corpus : 0,66-0,84 vs < 0,57). Mode AUTO
    uniquement — un nombre de locuteurs FORCÉ par l'utilisateur est respecté.

    v1.0.26 — SEUIL ADAPTATIF À LA DURÉE. Terrain : une réunion de 30 s où UNE
    SEULE personne parlait a été découpée en 3 « locuteurs », avec des
    similarités de 0,603 / 0,643 / 0,711 — donc SOUS le seuil fixe de 0,65 pour
    deux paires sur trois. La raison est physique : une empreinte vocale a
    besoin de durée pour être fiable (EER ~27 % à 0,5 s, ~13 % à 1 s, ~5 % à
    2 s), et sur des fragments de 2 à 6 s la similarité entre deux extraits
    d'une MÊME voix s'effondre. Appliquer à ces fragments le seuil calibré sur
    des minutes de parole revient à inventer des locuteurs.
    On assouplit donc le seuil quand les deux clusters comparés sont COURTS.
    Repère mesuré sur les données réelles : même personne 0,603-0,711 (clusters
    de 2-6 s) contre personnes différentes 0,317 (clusters de 20-40 min) —
    les deux familles restent largement séparées.
    `times` : [(t0,t1)...] aligné sur E, pour connaître la durée de chaque
    cluster. Absent -> comportement d'origine, seuil fixe."""
    import numpy as np
    lab = np.asarray(lab).copy()
    dur = None
    if times is not None:
        try:
            T = np.asarray(times, dtype=float)
            dur = np.maximum(0.0, T[:, 1] - T[:, 0])
        except Exception:
            dur = None

    def _thr(a, b):
        """Seuil pour la paire (a,b) : d'autant plus tolérant que le PLUS COURT
        des deux clusters est bref (empreinte peu fiable)."""
        if dur is None:
            return threshold
        s = min(float(dur[lab == a].sum()), float(dur[lab == b].sum()))
        if s < 8.0:
            return 0.46          # fragments : l'empreinte ne vaut presque rien
        if s < 20.0:
            return 0.52
        if s < 60.0:
            return 0.58
        return threshold         # >= 1 min de parole : exigence d'origine

    while True:
        ids = sorted(set(int(x) for x in lab))
        if len(ids) < 2:
            return lab
        cents = {}
        for i in ids:
            c = E[lab == i].mean(axis=0)
            cents[i] = c / (np.linalg.norm(c) + 1e-9)
        best, pair, best_thr = -1.0, None, threshold
        for ai in range(len(ids)):
            for bi in range(ai + 1, len(ids)):
                a, b = ids[ai], ids[bi]
                cos = float(np.dot(cents[a], cents[b]))
                # on retient la paire la plus « fusionnable » par rapport à SON
                # propre seuil, pas la plus proche dans l'absolu
                if (cos - _thr(a, b)) > (best - best_thr):
                    best, pair, best_thr = cos, (a, b), _thr(a, b)
        threshold_eff = best_thr
        if best < threshold_eff or pair is None:
            remap = {v: k for k, v in enumerate(ids)}
            return np.array([remap[int(x)] for x in lab])
        a, b = pair
        print(f"[whotalks] fusion de 2 clusters quasi identiques "
              f"(cos={best:.2f} >= {threshold_eff:.2f}) — même personne scindée.")
        lab[lab == b] = a


def _downmix(a, ch):
    """Down-mix par DÉCIMATION (canaux entrelacés : on garde le canal 1, les
    canaux 2..n sont JETÉS — une voix latéralisée sur source stéréo est perdue).
    Phase 2 (D5) : passer à la MOYENNE des canaux changerait les empreintes ->
    à mesurer au DER avant bascule. No-op si mono (même vue, pas de copie)."""
    return a[::ch] if ch > 1 else a


# v20 — Threads ONNX ALIGNÉS sur le plafond mesuré de Whisper (au-delà de 4 :
# zéro gain, +chauffe, contention avec Slack/Chrome — mesure v19). app.py pousse
# son cpu_threads effectif (qui intègre cpu_economy et VLOCAL_THREADS) via
# set_num_threads() ; repli autonome si jamais appelé. Le nombre de threads ne
# change PAS les embeddings/le clustering (byte-identique), seulement la
# vitesse et la chaleur.
_NUM_THREADS = None


def set_num_threads(n):
    global _NUM_THREADS
    try:
        _NUM_THREADS = max(1, int(n))
    except Exception:
        pass


def _threads():
    if _NUM_THREADS:
        return _NUM_THREADS
    return max(2, min(4, (os.cpu_count() or 4) - 2))


# ===================== WhoTalks v1 — moteur de diarisation =================== #
# Validé empiriquement sur audio téléphonique FR réel (27 min, 2 voix) :
#   - extraction d'empreintes CAM++ par fenêtre 2 s = ~10 ms/fenêtre (ULTRA léger,
#     115x temps réel) ; pas de segmentation lourde -> 15 s pour 27 min (vs 100 s).
#   - AUTO-DÉTECTION du nombre de voix par SILHOUETTE (balayage K=2..max) :
#     donne K=2 sur cet audio là où FastClustering auto donnait 54.
#   - réconciliation finale (silhouette + k-means) = 0,08 s -> zéro ralentissement.
# v22 — DÉTERMINISME : l'ancien RNG partagé au niveau module (_WT_RNG) faisait
# DÉRIVER l'état à chaque appel -> deux diarisations du MÊME audio dans la même
# session ne donnaient pas le même résultat (« Ré-identifier » loterie, nombre
# de locuteurs instable). Chaque appel crée désormais son RNG à graine fixe :
# même audio => même clustering, toujours.


def _kmeans_sph(X, K, iters=25):
    """k-means SPHÉRIQUE (cosine) — X est L2-normalisé donc le produit scalaire
    EST la similarité cosine. Init k-means++ pondérée par la distance.
    DÉTERMINISTE : RNG frais par appel (graine fixe)."""
    import numpy as np
    if K <= 1 or len(X) < K:
        # Cas dégénéré : le second retour (X[:1]) est purement INDICATIF — c'est
        # le premier point brut, PAS une moyenne L2-normalisée, et si len(X) < K
        # avec K >= 2 un seul « centroïde » est renvoyé au lieu de K. Tous les
        # appelants l'ignorent (lab, _ = ...) et recalculent les centroïdes via
        # _centroids_array/_centroids_by_label.
        return np.zeros(len(X), dtype=int), X[:1]
    _rng = np.random.RandomState(K)

    def _one_run():
        idx = [_rng.randint(len(X))]
        for _ in range(K - 1):
            d = 1 - (X @ X[idx].T).max(1)
            d = np.clip(d, 0, None)
            s = d.sum()
            idx.append(_rng.choice(len(X), p=(d / s) if s > 0 else None))
        C = X[idx].copy()
        prev_lab = None
        for _ in range(iters):
            lab = (X @ C.T).argmax(1)
            # EARLY-EXIT à convergence : si la partition n'a pas bougé, C (déjà
            # recalculé d'elle) est au point fixe -> les itérations restantes
            # reproduiraient le même état à l'identique. Aucun RNG n'est
            # consommé dans cette boucle -> sortie bit-à-bit identique.
            if prev_lab is not None and (lab == prev_lab).all():
                break
            prev_lab = lab
            for k in range(K):
                m = X[lab == k]
                if len(m):
                    C[k] = m.mean(0)
                    C[k] /= np.linalg.norm(C[k]) + 1e-9
        lab = (X @ C.T).argmax(1)
        # cohésion = somme des similarités au centroïde assigné (à maximiser)
        coh = float((X * C[lab]).sum())
        return lab, C, coh
    # MULTI-RESTART : k-means++ tombe parfois dans un mauvais minimum local
    # (ex. déséquilibre 70/30 -> il scinde le locuteur dominant au lieu de
    # séparer les 2 voix). On garde le meilleur de plusieurs essais.
    best = None
    for _ in range(8):
        lab, C, coh = _one_run()
        if best is None or coh > best[2]:
            best = (lab, C, coh)
    return best[0], best[1]


def _silhouette(X, lab):
    """Score silhouette (distance cosine), échantillonné pour la vitesse.
    DÉTERMINISTE : échantillon à graine fixe par appel."""
    import numpy as np
    K = int(lab.max()) + 1
    if K < 2:
        return -1.0
    samp = np.random.RandomState(0).choice(len(X), min(400, len(X)), replace=False)
    D = 1 - X[samp] @ X.T
    out = []
    for ii, i in enumerate(samp):
        o = lab[i]
        a = D[ii][lab == o]
        a = a[a > 0]
        a = a.mean() if len(a) else 0.0
        others = [D[ii][lab == k].mean() for k in range(K) if k != o]
        b = min(others) if others else 0.0
        out.append((b - a) / (max(a, b) + 1e-9))
    return float(np.mean(out)) if out else -1.0


# silhouette en-dessous de laquelle on conclut « une seule voix » (mono-locuteur)
WT_SIL_MIN = 0.12


def _eigengap_k(E, kmax):
    """v1.1.0 — Nombre de voix par ÉCART SPECTRAL (idée NME-SC) : laplacien
    normalisé du graphe des p plus proches voisins (cosinus), K = plus grand
    saut entre valeurs propres consécutives. Renvoie (K, netteté) ou None si
    l'estimation n'est pas fiable (trop peu de régions, graphe non connexe).

    Pourquoi : la silhouette hésitait entre 2 et 4 voix sur une réunion réelle
    d'une heure à quatre personnes (0,296 contre 0,289) et tranchait pour 2 ;
    résultat 70 % de mots bien attribués au lieu de 88 %. L'écart spectral
    trouve 4 sur cette réunion, 2 sur cinq réunions à deux voix, 1 sur des
    sous-ensembles à une seule voix (où la silhouette répondait 2 à 4).
    On essaie plusieurs densités de voisinage et on garde la plus nette."""
    import numpy as np
    n = len(E)
    if n < 8:
        return None
    if n > 2500:                       # coût du spectre : on sous-échantillonne
        E = E[np.linspace(0, n - 1, 2500).astype(int)]
        n = len(E)
    X = np.asarray(E, dtype=np.float64)
    X = X / np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-9)
    S = X @ X.T
    kmax = max(1, min(kmax, n - 2))
    best = None
    for p_frac in (0.1, 0.2, 0.3):
        p = min(n - 1, max(5, int(p_frac * n)))
        A = np.zeros_like(S)
        idx = np.argsort(-S, axis=1)[:, :p]
        rows = np.arange(n)[:, None]
        A[rows, idx] = S[rows, idx]
        A = np.maximum(A, A.T)
        np.fill_diagonal(A, 0.0)
        d = A.sum(axis=1)
        dm = 1.0 / np.sqrt(np.maximum(d, 1e-9))
        L = np.eye(n) - (A * dm[:, None]) * dm[None, :]
        w = np.sort(np.linalg.eigvalsh(L))[:kmax + 1]
        if len(w) < 3 or w[1] < 1e-6:
            continue                   # graphe non connexe : sauts artificiels
        gaps = np.diff(w)
        k = int(np.argmax(gaps[:kmax])) + 1
        sharp = float(gaps[k - 1] / max(float(gaps[:kmax].sum()), 1e-9))
        if best is None or sharp > best[1]:
            best = (k, sharp)
    return best


def estimate_speakers(E, kmax=8, speech_s=None):
    """AUTO-DÉTECTION du nombre de voix. Renvoie (K, labels, sil).

    v1.1.0 — écart spectral d'abord (_eigengap_k), silhouette en repli quand il
    n'est pas fiable. La silhouette du regroupement retenu sert de garde-fou
    (WT_SIL_MIN) : trop faible -> une seule voix.

    v1.0.26 — PLAFOND PHYSIQUE SUR LES AUDIOS COURTS. Une empreinte vocale n'est
    exploitable qu'au-delà d'une certaine durée de parole (EER ~27 % à 0,5 s,
    ~13 % à 1 s, ~5 % à 2 s) : conclure « 3 personnes » à partir de 13 secondes
    de parole revient à décrire du bruit. On exige donc ~15 s de parole par
    locuteur supposé : les estimateurs peuvent proposer moins, jamais plus.
    `speech_s` absent -> pas de plafond."""
    import numpy as np
    kmax = min(kmax, len(E) - 1)
    if speech_s is not None:
        try:
            cap = max(1, int(float(speech_s) // 15))
            if cap < kmax:
                kmax = cap
        except Exception:
            pass
    if kmax < 2:                      # pas assez de matière : une seule voix
        return 1, np.zeros(len(E), dtype=int), -1.0
    one = (1, np.zeros(len(E), dtype=int), -1.0)
    eig = _eigengap_k(E, kmax)
    if eig is not None:
        K = eig[0]
        if K < 2:
            return one
        lab, _ = _kmeans_sph(E, K)
        s = _silhouette(E, lab)
        if s < WT_SIL_MIN:
            return 1, np.zeros(len(E), dtype=int), s
        return K, lab, s
    best = one
    for K in range(2, kmax + 1):
        lab, _ = _kmeans_sph(E, K)
        s = _silhouette(E, lab)
        if s > best[2]:
            best = (K, lab, s)
    if best[2] < WT_SIL_MIN:   # silhouette trop faible -> 1 seule voix
        return 1, np.zeros(len(E), dtype=int), best[2]
    return best


def _labels_to_segments(times, lab, grid=0.5, merge_gap=0.75):
    """Empreintes fenêtrées (qui se chevauchent) -> frise temporelle : vote
    majoritaire par case de `grid` s, puis fusion des cases contiguës de même
    voix. Robuste (lisse les fenêtres isolées)."""
    import numpy as np
    if not len(times):
        return []
    end = float(times[-1][1])
    nb = int(end / grid) + 1
    votes = [dict() for _ in range(nb)]
    for i, (t1, t2) in enumerate(times):
        for g in range(int(t1 / grid), min(nb, int(t2 / grid) + 1)):
            votes[g][lab[i]] = votes[g].get(lab[i], 0) + 1
    segs = []
    cur = None
    for g, v in enumerate(votes):
        if not v:
            continue
        l = max(v, key=v.get)
        t = g * grid
        if cur and cur["speaker"] == l and t - cur["end"] <= merge_gap:
            cur["end"] = t + grid
        else:
            if cur:
                segs.append(cur)
            cur = {"start": t, "end": t + grid, "speaker": l}
    if cur:
        segs.append(cur)
    return [{"start": s["start"], "end": s["end"],
             "speaker": "SPEAKER_%02d" % int(s["speaker"])} for s in segs]


def _centroids_by_label(E, lab, times):
    """Centroïde L2 (empreinte moyenne) + durée cumulée par voix. Le centroïde
    est l'« ID vocal » stable d'un interlocuteur, comparable entre réunions."""
    import numpy as np
    lab = np.asarray(lab)
    times = np.asarray(times, dtype=float)
    out = {}
    for k in sorted(set(int(x) for x in lab)):
        m = lab == k
        c = E[m].mean(0)
        c = c / (np.linalg.norm(c) + 1e-9)
        secs = float((times[m, 1] - times[m, 0]).sum()) if len(times) else 0.0
        out["SPEAKER_%02d" % k] = {"centroid": [float(x) for x in c],
                                   "seconds": secs}
    return out


def _centroids_array(E, lab):
    """Centroïdes L2 (K x dim) indexés par label 0..K-1 (pour l'attribution
    par mot : on assigne chaque mot au centroïde le plus proche)."""
    import numpy as np
    lab = np.asarray(lab)
    K = int(lab.max()) + 1 if len(lab) else 0
    C = []
    for k in range(K):
        m = E[lab == k]
        c = m.mean(0) if len(m) else E.mean(0)
        C.append(c / (np.linalg.norm(c) + 1e-9))
    return np.array(C) if C else None


def _robust_centroids(E, lab):
    """v25 — Centroïdes ROBUSTES par cluster (médoïde : l'embedding au cosinus
    médian le plus élevé vis-à-vis de ses pairs). Une intonation extrême est un
    OUTLIER (faible cos médian) qui ne déplace donc plus l'identité de la voix —
    contrairement à la moyenne L2 que tire un embedding aberrant. K x dim L2."""
    import numpy as np
    E = np.asarray(E, dtype=np.float32)
    lab = np.asarray(lab)
    K = int(lab.max()) + 1 if len(lab) else 0
    C = []
    for k in range(K):
        Ek = E[lab == k]
        if len(Ek) == 0:
            c = E.mean(0)
        elif len(Ek) <= 2:
            c = Ek.mean(0)
        else:
            med = np.median(Ek @ Ek.T, axis=1)   # cos médian aux pairs (E normalisé)
            c = Ek[int(med.argmax())]             # médoïde
        C.append(c / (np.linalg.norm(c) + 1e-9))
    return np.array(C, dtype=np.float32) if C else None


def _viterbi_assign(E, C, lam=DIAR_TURN_LAMBDA):
    """v25 — COEUR de la surcouche. Assignation GLOBALE des régions (ordonnées dans
    le temps) aux centroïdes C, en pénalisant chaque CHANGEMENT de locuteur d'un
    coût `lam`. Un changement ISOLÉ (une seule région aberrante, ex. un "oui" à
    intonation bizarre) coûte plus cher que l'écart acoustique gagné -> le décodeur
    GARDE le locuteur courant au lieu d'inventer/basculer. Déterministe, O(N*K),
    pur NumPy. E : N x dim (L2-normalisé) ; C : K x dim (L2). Renvoie labels 0..K-1."""
    import numpy as np
    E = np.asarray(E, dtype=np.float32)
    C = np.asarray(C, dtype=np.float32)
    N, K = len(E), len(C)
    if N == 0 or K <= 1:
        return np.zeros(N, dtype=int)
    emis = -(E @ C.T)                          # N x K : coût (plus bas = mieux)
    V = np.empty((N, K), dtype=np.float32)
    back = np.zeros((N, K), dtype=np.int32)
    V[0] = emis[0]
    for i in range(1, N):
        prev = V[i - 1]
        jmin = int(prev.argmin()); pmin = float(prev[jmin])
        for s in range(K):
            stay = float(prev[s])              # venir de s (aucun changement)
            switch = pmin + lam                # venir du meilleur autre (+ pénalité)
            if stay <= switch:
                V[i, s] = stay + emis[i, s]; back[i, s] = s
            else:
                V[i, s] = switch + emis[i, s]; back[i, s] = jmin
    lab = np.zeros(N, dtype=int)
    lab[N - 1] = int(V[N - 1].argmin())
    for i in range(N - 1, 0, -1):
        lab[i - 1] = int(back[i, lab[i]])
    return lab


def _link_merge_greedy(lab, times, seg_ids,
                       min_links=LINK_MIN_COUNT, min_rate=LINK_MIN_RATE):
    """v26 — Fusionne les clusters massivement reliés par des paires de régions
    CONSÉCUTIVES D'UN MÊME SEGMENT Whisper (même locuteur par construction).
    GLOUTON + RE-ÉVALUÉ : on fusionne LA meilleure paire, puis on recompte au
    niveau des composantes — une fois une voix réunifiée, son taux de liens vers
    l'autre locuteur retombe sous le seuil et la fusion s'arrête seule (pas
    d'effondrement par pont transitif). Pur, déterministe, O(K²·N)."""
    import numpy as np
    lab = np.asarray(lab).copy()
    seg_ids = np.asarray(seg_ids)
    order = np.argsort(np.asarray(times, dtype=float)[:, 0])
    while True:
        if int(lab.max()) + 1 <= 1:
            break
        lo, so = lab[order], seg_ids[order]
        links, adj = {}, {}
        for i in range(len(lo) - 1):
            a, b = sorted((int(lo[i]), int(lo[i + 1])))
            if a == b:
                continue
            adj[(a, b)] = adj.get((a, b), 0) + 1
            if so[i] == so[i + 1]:
                links[(a, b)] = links.get((a, b), 0) + 1
        best, best_rate = None, 0.0
        for (a, b), n in adj.items():
            l = links.get((a, b), 0)
            r = l / n
            if l >= min_links and r >= min_rate and r > best_rate:
                best, best_rate = (a, b), r
        if best is None:
            break
        lab = np.where(lab == best[1], best[0], lab)
        uniq = {l: i for i, l in enumerate(sorted(set(lab.tolist())))}
        lab = np.array([uniq[int(l)] for l in lab])
    return lab


def _dissolve_minor(lab, times, min_share=0.02, min_dur=3.0):
    """Dissout les voix de FAIBLE MASSE (parasites : bruit, brèves interjections
    mal séparées) : toute voix totalisant < min_share du temps ET < min_dur s est
    supprimée, ses fenêtres réassignées à la voix MAJEURE la plus proche dans le
    temps. C'est ce qui ramène K au vrai nombre (ex. 2 dominantes au lieu de 2+2
    parasites). Pur, robuste."""
    import numpy as np
    lab = np.asarray(lab).copy()
    times = np.asarray(times, dtype=float)
    if len(lab) == 0:
        return lab
    durs = {}
    for i, l in enumerate(lab):
        durs[int(l)] = durs.get(int(l), 0.0) + (times[i, 1] - times[i, 0])
    total = sum(durs.values()) or 1.0
    major = {l for l, d in durs.items() if d >= min_dur or d / total >= min_share}
    if not major:                       # garde au moins la plus grosse
        major = {max(durs, key=durs.get)}
    if len(major) == len(durs):
        return lab
    centers = times.mean(1)
    major_idx = [i for i in range(len(lab)) if int(lab[i]) in major]
    if not major_idx:
        return lab
    mc = centers[major_idx]
    for i in range(len(lab)):
        if int(lab[i]) in major:
            continue
        j = int(np.argmin(np.abs(mc - centers[i])))     # voisin majeur le + proche
        lab[i] = lab[major_idx[j]]
    # ré-indexe les labels en 0..K-1 contigus
    uniq = {l: k for k, l in enumerate(sorted(set(int(x) for x in lab)))}
    return np.array([uniq[int(x)] for x in lab])


def postprocess_segments(segs, min_dur=1.0, gap=0.5):
    """v18.6 — Anti-scintillement APRÈS clustering (cause des « 100 locuteurs ») :
      1. tout segment < min_dur est réassigné au VOISIN dominant (pas un nouveau
         locuteur pour 0,3 s),
      2. filtre médian glissant (fenêtre 3) sur la séquence de labels (lisse les
         sauts isolés),
      3. fusion des segments adjacents d'un même locuteur séparés de < gap.
    Fonction PURE (testable) : ne modifie pas les segments d'entrée, retourne de
    nouveaux dicts. Entrée/sortie : [{start,end,speaker}] triés."""
    if not segs:
        return segs
    # copie des DICTS (pas seulement de la liste) : les étapes 1-2 mutent
    # `speaker` -> jamais sur les segments de l'appelant.
    s = [dict(x) for x in sorted(segs, key=lambda d: d["start"])]
    # 1. réassignation des segments trop courts au voisin le plus long
    for i, seg in enumerate(s):
        if seg["end"] - seg["start"] >= min_dur:
            continue
        left = s[i - 1] if i > 0 else None
        right = s[i + 1] if i + 1 < len(s) else None
        cand = max([n for n in (left, right) if n],
                   key=lambda n: n["end"] - n["start"], default=None)
        if cand:
            seg["speaker"] = cand["speaker"]
    # 2. filtre médian (fenêtre 3) sur les labels pour tuer les sauts isolés
    labels = [seg["speaker"] for seg in s]
    for i in range(1, len(labels) - 1):
        a, b, c = labels[i - 1], labels[i], labels[i + 1]
        if a == c and b != a:
            s[i]["speaker"] = a
    # 3. fusion des segments adjacents même locuteur (gap court)
    merged = [dict(s[0])]
    for seg in s[1:]:
        last = merged[-1]
        if seg["speaker"] == last["speaker"] and seg["start"] - last["end"] <= gap:
            last["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    return merged


def available() -> bool:
    """True si sherpa-onnx est importable ET les deux modèles ONNX présents."""
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return False
    return os.path.exists(SEG_MODEL) and os.path.exists(EMB_MODEL)


class Diarizer:
    """Adaptateur sherpa-onnx. Léger : ~28 Mo de modèles utiles (CAM++ 27 Mo +
    segmentation int8 1,5 Mo), faible RAM. Conçu pour le mode RELAIS (charger
    après Whisper), mais assez léger pour coexister."""

    def __init__(self, seg_model: str = SEG_MODEL, emb_model: str = EMB_MODEL):
        self.seg_model = seg_model
        self.emb_model = emb_model
        self._sd = None
        self._num = None          # nb de clusters du _sd courant (repli offline)
        self._ext = None          # WhoTalks : extracteur d'empreintes CAM++
        self._lock = threading.Lock()
        # WhoTalks v1.1 — accumulateur d'empreintes EN LIGNE (rempli pendant
        # l'enregistrement par feed_window) : on étale le coût lourd, et à
        # l'arrêt il ne reste que le clustering (~0,1 s).
        self._st_embs = []        # empreintes (192-d L2)
        self._st_times = []       # (t0, t1) horodatées (offset global)
        self._st_last_t = 0.0     # anti-doublon overlap entre fenêtres

    # -- WhoTalks v1.1 : flux incrémental ----------------------------------- #
    def reset_stream(self):
        self._st_embs = []
        self._st_times = []
        self._st_last_t = 0.0

    def feed_window(self, pcm, sr, offset_s,
                    win_s=TILE_WIN_S, hop_s=TILE_HOP_S, gate=None):
        """Extrait les empreintes d'UNE fenêtre live (PCM float32) et les
        accumule, horodatées avec offset_s. Appelé PENDANT l'enregistrement
        (≈1 appel / 25 s d'audio). Coût ~10 ms/tuile -> imperceptible. Robuste :
        n'échoue jamais (try interne), saute les tuiles silencieuses, dédoublonne
        le chevauchement avec la fenêtre précédente.

        v22.4 — GATE RELATIF (aligné sur l'offline) : l'ancien gate FIXE 0,02
        laissait passer, sur de l'audio faible/réverbérant (téléphone, pièce qui
        résonne), des tuiles de « bouillie » acoustique sans parole nette ; ces
        tuiles tiraient TOUS les centroïdes vers une direction commune (cos
        inter-locuteurs 0,80 mesuré sur réunion réelle vs 0,44 en gating
        relatif) -> fusion abusive 2->1. Gate = 0,5 x RMS de la fenêtre reçue
        (≈ le 0,5 x RMS global de l'offline pour des fenêtres denses en parole),
        avec PLANCHER absolu pour les fenêtres pathologiquement silencieuses."""
        try:
            import numpy as np
            ext = self._ensure_extractor()
            if ext is None or pcm is None or len(pcm) == 0:
                return 0
            if gate is None:
                win_rms = float(np.sqrt(np.mean(
                    np.asarray(pcm, dtype=np.float32) ** 2)))
                gate = max(0.008, 0.5 * win_rms)
            win = int(win_s * sr)
            hop = int(hop_s * sr)
            added = 0
            for s in range(0, max(0, len(pcm) - win + 1), hop):
                t0 = offset_s + s / sr
                if t0 < self._st_last_t - 1e-3:      # déjà couvert (overlap)
                    continue
                seg = pcm[s:s + win]
                if float(np.sqrt(np.mean(seg * seg))) < gate:
                    continue
                st = ext.create_stream()
                st.accept_waveform(sr, seg)
                st.input_finished()
                e = np.array(ext.compute(st), dtype=np.float32)
                e /= np.linalg.norm(e) + 1e-9
                self._st_embs.append(e)
                self._st_times.append((t0, offset_s + (s + win) / sr))
                self._st_last_t = offset_s + (s + win) / sr
                added += 1
            return added
        except Exception as e:
            print(f"[whotalks] feed_window KO (ignoré) : {e}")
            return 0

    def finalize_stream(self, num_speakers=0, max_speakers=8):
        """À l'arrêt : clusterise les empreintes accumulées (déjà extraites
        pendant la réunion) -> frise. Coût ~0,1 s -> AUCUN ralentissement.
        Renvoie [{start,end,speaker}] ou [] si pas assez de données."""
        # Purge de l'état _last_* du run PRÉCÉDENT (cf. diarize) — AVANT le try :
        # de simples affectations ne lèvent pas, et la purge tient même en échec.
        self._last_voices = {}
        self._last_windows = None
        self._last_C = None
        self._last_E = None
        try:
            import numpy as np
            if len(self._st_embs) < 3:
                return []
            E = np.array(self._st_embs)
            T = np.array(self._st_times)
            lab, K, sil, forced = self._cluster_and_remember(E, T, num_speakers,
                                                             max_speakers)
            if not forced:
                print(f"[whotalks] flux : {K} voix (silhouette {sil:.2f}, "
                      f"{len(E)} empreintes).")
            segs = _labels_to_segments(T, lab)
            return postprocess_segments(segs, min_dur=POST_MIN_DUR, gap=POST_GAP)
        except Exception as e:
            print(f"[whotalks] finalize_stream KO : {e}")
            return []

    def _cluster_and_remember(self, E, T, num_speakers, max_speakers,
                              seg_ids=None):
        """Clustering COMMUN à finalize_stream et _whotalks (une seule vérité,
        plus de divergence live/offline silencieuse) : nombre de voix forcé
        (k-means sphérique) ou auto-détecté (silhouette), dissolution des voix
        parasites en mode auto, puis mémorisation _last_* pour l'attribution au
        mot et l'enrôlement voiceid. N'imprime RIEN (chaque appelant garde son
        propre message). Renvoie (lab, K, sil, forced) — sil=None si forcé.

        seg_ids (optionnel, v26) : id de segment Whisper par région — permet la
        fusion par évidence de liens en mode auto (anti voix-inventée-par-
        l'intonation). Seul diarize_from_segments le fournit ; le chemin live
        (tuiles, sans transcription alignée) reste inchangé."""
        if num_speakers and int(num_speakers) > 0:
            K = min(int(num_speakers), len(E))
            lab, _ = _kmeans_sph(E, K)
            forced = True
            sil = None
            # v25 — Surcouche Turn-Aware : K est connu, il ne reste que l'ATTRIBUTION.
            # Centroïdes robustes (médoïde) + Viterbi (pénalise les changements
            # isolés) + lissage des flips d'1 région -> un "oui" intonné isolé hérite
            # du locuteur courant au lieu d'inventer/basculer. K reste fixé (forcé).
            if DIAR_USE_VITERBI and K > 1 and len(E) > 2:
                Cr = _robust_centroids(E, lab)
                if Cr is not None:
                    lab = _viterbi_assign(E, Cr, lam=DIAR_TURN_LAMBDA)
                    lab = _smooth_single_flips(list(lab))
        else:
            # v1.0.26 — on transmet la durée de parole réelle : le nombre de
            # locuteurs ne peut pas dépasser ce que l'audio permet d'établir.
            _sp_s = None
            try:
                _T = np.asarray(T, dtype=float)
                _sp_s = float(np.maximum(0.0, _T[:, 1] - _T[:, 0]).sum())
            except Exception:
                _sp_s = None
            K, lab, sil = estimate_speakers(E, max_speakers, speech_s=_sp_s)
            forced = False
            # v22 — ANTI « LOCUTEUR INVENTÉ » : fusionne les clusters dont les
            # centroïdes sont quasi colinéaires (même personne scindée par le
            # clustering). Validé corpus : répare 3->2 sur les trois cas de
            # scission simulée, ne touche JAMAIS 3 vraies voix (cos < 0,57).
            lab = _merge_close_centroids(E, lab, times=T)
            # v26 — fusion par évidence de liens même-segment (cf. constantes) :
            # répare le sur-comptage dû à l'intonation (2 pers. -> 3-5 « voix »).
            # Ne touche JAMAIS deux vrais locuteurs (taux mesuré ~3-6 % << 30 %).
            if DIAR_LINK_MERGE and seg_ids is not None and len(seg_ids) == len(lab):
                lab = _link_merge_greedy(lab, T, seg_ids)
            K = int(lab.max()) + 1 if len(lab) else K
        # _dissolve_minor (suppression des voix parasites de faible masse)
        # SEULEMENT en mode auto : si l'utilisateur a FORCÉ le nombre de voix,
        # on le respecte (sinon un 2e locuteur discret serait fusionné -> on
        # retomberait à 1 voix et la parole serait « confondue »).
        if not forced:
            lab = _dissolve_minor(lab, T, min_share=DISSOLVE_MIN_SHARE,
                                  min_dur=DISSOLVE_MIN_DUR)
            # K reflète le résultat FINAL (dissolve peut encore retirer des voix ;
            # sinon le log « K voix » du chemin guidé surcompte).
            K = len(set(int(l) for l in lab)) if len(lab) else K
        # WhoTalks v1.2 — centroïde + durée cumulée par voix (pour
        # l'enrôlement / la reconnaissance inter-réunions).
        self._last_voices = _centroids_by_label(E, lab, T)
        # Labels de fenêtre FINS conservés : l'attribution au mot s'appuie
        # dessus, pas sur les segments grossis (frise).
        self._last_windows = (T, lab)
        self._last_C = _centroids_array(E, lab)   # centroïdes (assign par mot)
        self._last_E = E                          # empreintes fenêtre (marge)
        return lab, K, sil, forced

    # ---- v24 — diarisation GUIDÉE PAR LA TRANSCRIPTION (post-hoc) -------------
    # Au lieu de tuiles fixes 2 s/1 s (qui chevauchent les changements de tour ->
    # embedding MIXTE -> confusion de locuteur), on extrait UN embedding par
    # RÉGION DE PAROLE alignée sur les mots de la transcription. Mesuré sur 3
    # réunions réelles vs Gemini : DER 0,033 (vs 0,349 en tuiles), confusion
    # 0,03, K correct, 0 invention. Réutilise _cluster_and_remember (gardes
    # anti-invention) + assign_words (via _last_windows). À appeler À L'ARRÊT
    # avec l'audio complet + les segments détaillés (word-timestamps).
    @staticmethod
    def _speech_regions(segments, maxlen):
        """Régions de parole : sous-découpe les longs segments aux frontières de
        MOTS (<= maxlen) pour ne jamais embarquer à cheval sur un changement de
        tour ; garde les courts tels quels."""
        reg = []
        for s in segments:
            words = s.get("words") or []
            if not words:
                reg.append((float(s["start"]), float(s["end"]))); continue
            cur0 = float(words[0]["start"])
            for w in words:
                we = float(w["end"])
                if we - cur0 >= maxlen:
                    reg.append((cur0, we)); cur0 = we
            if float(words[-1]["end"]) - cur0 > 0.05:
                reg.append((cur0, float(words[-1]["end"])))
        return reg

    @staticmethod
    def _agc(seg, target=0.1, floor=0.0005):
        """v29 — ÉGALISATION DE GAIN (AGC) d'un span avant l'empreinte CAM++.
        Ramène le RMS à `target` : l'empreinte reflète alors le TIMBRE (identité
        vocale) et non le NIVEAU du micro. Cause racine corrigée : un locuteur
        plus faible/lointain (vu 20 dB d'écart) était soit JETÉ par le gate, soit
        encodé sur un signal trop faible -> fusion 2->1 ou diarisation vide. Le
        gain ne change pas l'identité vocale, il la révèle à volume égal.
        Renvoie None pour le VRAI silence uniquement (< floor)."""
        import numpy as np
        seg = np.asarray(seg, dtype=np.float32)
        rms = float(np.sqrt(np.mean(seg * seg))) if seg.size else 0.0
        if rms < floor:
            return None
        return np.clip(seg * (target / rms), -1.0, 1.0).astype(np.float32)

    def _emb_span(self, ext, audio, sr, t0, t1):
        """UNE empreinte CAM++ pour la région de parole [t0,t1] (CAM++ gère la
        longueur variable -> 1 seule inférence par région, vs ~5 en fenêtrant :
        ~4-5x MOINS d'inférences -> l'arène mémoire d'ONNX Runtime reste petite
        (réunion 34 min : ~6 Go -> ~1 Go). `audio` = float32 normalisé OU memmap
        int16 (lecture par tranche, RAM minimale). Région trop courte/silencieuse
        (sous le gate) -> None (héritée du voisin à la frise)."""
        import numpy as np
        seg = np.asarray(audio[int(t0 * sr):int(t1 * sr)])
        if seg.dtype != np.float32:
            seg = seg.astype(np.float32) / 32768.0   # memmap int16 -> float32 normalisé
        if len(seg) < int(0.4 * sr):
            return None
        seg = self._agc(seg)                         # v29 : égalise le volume
        if seg is None:                              # (vrai silence uniquement)
            return None
        # RAM : entrée de FORME FIXE et COURTE (3 s). sherpa/ONNX Runtime fuit
        # ~6-12 Mo par inférence (proportionnel à la longueur d'entrée) ; en fixant
        # une fenêtre courte on RÉDUIT le pic (~5 Go -> ~3 Go sur 34 min). Le fix
        # COMPLET (RAM bornée à plat) = isoler l'extraction dans un sous-process tué
        # par lots (TODO industrialisation). DER 0,039 (vs 0,033 variable-longueur).
        # Remplissage par VRAIE parole (le zéro-padding faussait l'embedding) :
        # centre si région longue, TUILAGE (répétition) si courte.
        L = int(3.0 * sr)
        if len(seg) >= L:
            off = (len(seg) - L) // 2
            seg = seg[off:off + L]
        else:
            reps = (L + len(seg) - 1) // len(seg)
            seg = np.tile(seg, reps)[:L]
        seg = np.ascontiguousarray(seg, dtype=np.float32)
        st = ext.create_stream()
        st.accept_waveform(sr, seg)
        st.input_finished()
        e = np.array(ext.compute(st), dtype=np.float32)
        del st
        n = np.linalg.norm(e)
        return e / (n + 1e-9) if n > 0 else None

    @staticmethod
    def _regions_with_ids(segments, maxlen=6.0):
        """v26 — régions de parole + ID du segment Whisper de chacune (les
        sous-régions d'un même segment = même locuteur par construction :
        alimente la fusion par évidence de liens en mode auto)."""
        regions, seg_ids = [], []
        for si, seg in enumerate(segments):
            for (t0, t1) in Diarizer._speech_regions([seg], maxlen):
                regions.append((t0, t1)); seg_ids.append(si)
        return regions, seg_ids

    def _frise_from_embeddings(self, E, T, seg_ids, num_speakers, max_speakers,
                               dur):
        """Queue COMMUNE de la diarisation guidée (in-process et sous-process) :
        clustering + mémorisation _last_*, puis frise comblée/fusionnée.
        E : N x 192 float32 L2 ; T : N x 2 ; seg_ids : N. [] si < 3 régions."""
        import numpy as np
        if len(E) < 3:
            return []
        E = np.asarray(E, dtype=np.float32); T = np.asarray(T, dtype=float)
        lab, K, sil, forced = self._cluster_and_remember(
            E, T, num_speakers, max_speakers,
            seg_ids=np.asarray(seg_ids, dtype=int))
        if not forced:
            print(f"[whotalks] guidé-transcription : {K} voix "
                  f"(silhouette {sil:.2f}, {len(E)} régions).")
        segs = sorted(
            [{"speaker": "SPEAKER_%02d" % int(lab[j]),
              "start": float(T[j][0]), "end": float(T[j][1])}
             for j in range(len(E))], key=lambda x: x["start"])
        if segs:
            segs[0]["start"] = 0.0
            segs[-1]["end"] = dur
            for j in range(len(segs) - 1):       # comble les trous (mi-chemin)
                g0, g1 = segs[j]["end"], segs[j + 1]["start"]
                if g1 > g0:
                    mid = (g0 + g1) / 2.0
                    segs[j]["end"] = mid; segs[j + 1]["start"] = mid
            merged = [dict(segs[0])]              # fusionne voisins même voix
            for s in segs[1:]:
                if s["speaker"] == merged[-1]["speaker"]:
                    merged[-1]["end"] = s["end"]
                else:
                    merged.append(dict(s))
            segs = merged
        return segs

    def diarize_from_segments(self, audio, sr, segments, maxlen=6.0,
                              num_speakers=0, max_speakers=8):
        """Diarisation guidée par la transcription. audio = float32 mono 16k ;
        segments = sortie détaillée (avec words). Renvoie la frise
        [{speaker,start,end}] (comblée) et peuple _last_windows pour assign_words.
        Renvoie [] si pas assez de régions (l'appelant retombe sur l'ancien chemin)."""
        import numpy as np
        self._last_voices = {}; self._last_windows = None
        self._last_C = None; self._last_E = None
        try:
            ext = self._ensure_extractor()
            if ext is None:
                return []
            regions, seg_ids = self._regions_with_ids(segments, maxlen)
            E, T, SI = [], [], []
            for (t0, t1), si in zip(regions, seg_ids):
                e = self._emb_span(ext, audio, sr, t0, t1)
                if e is not None:
                    E.append(e); T.append((t0, t1)); SI.append(si)
            return self._frise_from_embeddings(E, T, SI, num_speakers,
                                               max_speakers,
                                               len(audio) / float(sr))
        except Exception as e:
            print(f"[whotalks] diarize_from_segments KO : {e}")
            return []

    def diarize_from_segments_subproc(self, wav_path, sr, segments, maxlen=6.0,
                                      num_speakers=0, max_speakers=8,
                                      on_progress=None):
        """v27 — Diarisation guidée avec extraction CAM++ en SOUS-PROCESS par lots
        (RAM du process principal PLATE : l'arène ONNX qui fuit ~6-12 Mo/inférence
        meurt avec chaque worker). Même qualité que diarize_from_segments
        (embeddings byte-identiques : même _emb_span, même audio). Repli in-process
        automatique si le spawn échoue. Pour l'app : c'est CE chemin qui tient la
        cible 8 Go sur les réunions longues."""
        self._last_voices = {}; self._last_windows = None
        self._last_C = None; self._last_E = None
        try:
            regions, seg_ids = self._regions_with_ids(segments, maxlen)
            if len(regions) < 3:
                return []
            embs = _extract_spans_subproc(wav_path, sr, regions,
                                          on_progress=on_progress)
            if embs is None:                      # spawn KO -> repli in-process
                # PLAFONNÉ à l'enveloppe v26 VALIDÉE (~3 Go à 34 min) : l'arène
                # ONNX in-process fuit ~6-12 Mo/inférence, donc SANS plafond une
                # réunion de 4 h consommerait >10 Go dans le process principal
                # (mort sur M1 8 Go). Au-delà : [] -> l'appelant retombe sur les
                # tuiles historiques, comme avant v27.
                if len(regions) > 400:
                    print("[whotalks] sous-process KO + réunion longue "
                          f"({len(regions)} régions > 400) -> repli tuiles.")
                    return []
                import numpy as np
                off = _wav_data_offset(wav_path)
                au = np.memmap(wav_path, dtype="<i2", mode="r", offset=off)
                return self.diarize_from_segments(au, sr, segments, maxlen,
                                                  num_speakers, max_speakers)
            # COUVERTURE : si des lots ont échoué APRÈS le premier (worker tué,
            # timeout), leurs régions sont None et la frise étendrait en silence
            # le dernier locuteur connu sur le trou. Au-delà de 30 % de pertes,
            # la frise serait mensongère -> replis.
            n_ok = sum(1 for e in embs if e is not None)
            if n_ok < 0.7 * len(regions):
                print(f"[whotalks] extraction partielle ({n_ok}/{len(regions)} "
                      "régions) -> repli tuiles.")
                return []
            E, T, SI = [], [], []
            for e, (t0, t1), si in zip(embs, regions, seg_ids):
                if e is not None:
                    E.append(e); T.append((t0, t1)); SI.append(si)
            import wave as _w
            with _w.open(wav_path, "rb") as wf:
                dur = wf.getnframes() / float(wf.getframerate())
            return self._frise_from_embeddings(E, T, SI, num_speakers,
                                               max_speakers, dur)
        except Exception as e:
            print(f"[whotalks] diarize_from_segments_subproc KO : {e}")
            return []

    def last_voices(self):
        """WhoTalks v1.2 — {speaker_id: {"centroid":[192 floats], "seconds":float}}
        de la dernière diarisation (vide si aucune)."""
        return getattr(self, "_last_voices", {}) or {}

    def assign_words(self, words):
        """Attribue chaque MOT au locuteur via les labels de FENÊTRE fins de la
        dernière diarisation (précision ~98 % mesurée), au lieu des segments
        grossis. Renvoie des blocs [{speaker,start,end,text}] regroupés, ou None
        si pas de labels de fenêtre (ex. repli offline) -> l'appelant retombe sur
        merge_transcript_with_speakers."""
        import numpy as np
        tw = getattr(self, "_last_windows", None)
        if not tw or not words:
            return None
        T, lab = tw
        T = np.asarray(T, dtype=float)
        lab = np.asarray(lab)
        if len(T) == 0:
            return None
        ww = [w for w in words if (w.get("word") or "").strip()]
        if not ww:
            return None
        centers = (T[:, 0] + T[:, 1]) / 2.0
        spks = []
        for w in ww:
            ws = float(w.get("start", 0.0)); we = float(w.get("end", ws))
            mask = (T[:, 0] < we) & (T[:, 1] > ws)        # fenêtres couvrant le mot
            cov = lab[mask]
            if len(cov):
                vals, cnts = np.unique(cov, return_counts=True)
                spks.append(int(vals[cnts.argmax()]))     # vote majoritaire
            else:
                spks.append(int(lab[int(np.argmin(np.abs(centers - (ws + we) / 2.0)))]))
        # lissage des flips d'un seul mot (erreurs de frontière isolées), puis
        # groupage en blocs, texte par concaténation brute (style Whisper) —
        # helpers partagés avec assign_words_precise et merge_transcript.
        sm = _smooth_single_flips(spks)
        # v1.0.23 — recalage des frontières sur les coupures naturelles : ne
        # change pas QUI parle, seulement OÙ le tour est coupé (56 % des blocs
        # finissaient au milieu d'une phrase). VLOCAL_DIAR_SNAP=0 pour désactiver.
        if os.environ.get("VLOCAL_DIAR_SNAP", "1") != "0":
            sm = snap_labels_to_speech(ww, sm)
        return _words_to_blocks(ww, ["SPEAKER_%02d" % int(s) for s in sm])

    def assign_units(self, words, pause=0.25, min_share=0.60):
        """v1.0.23 — ATTRIBUTION PAR UNITÉ DE PAROLE (remplace l'attribution au MOT).

        POURQUOI. `assign_words_precise` décidait mot par mot, sur une empreinte
        calculée sur 0,4 s d'audio minimum. La littérature est sans ambiguïté sur
        ce point : à fenêtre d'empreinte égale, le DER passe de 8,4 % (1,5 s) à
        9,1 % (1,0 s) puis 17,9 % (0,5 s) — Park et al., ICASSP 2021, CALLHOME ;
        et l'EER de vérification du locuteur passe de 5,4 % (2 s) à 12,8 % (1 s)
        puis 26,8 % (0,5 s). À 0,4 s on est SOUS le plancher publié : l'empreinte
        n'identifie plus personne, et l'argmax devient un tirage. C'est ce qui
        produisait 15,4 changements de locuteur par minute et 56 % de tours coupés
        en pleine phrase.

        CE QU'ON FAIT À LA PLACE. La frise `_last_windows` contient DÉJÀ un label
        par RÉGION DE PAROLE alignée sur les mots (médiane ~3,4 s d'audio par
        empreinte, cf. `_frise_from_embeddings`) — c'est-à-dire exactement la
        durée que la littérature demande. On ne recalcule donc RIEN : on groupe
        les mots en unités de parole (coupées aux vraies pauses), et chaque unité
        reçoit UN locuteur, par vote pondéré par le recouvrement temporel.
        Zéro inférence supplémentaire, zéro RAM en plus, et c'est plus rapide.

        GARDE DE CONTINUITÉ. Un changement de locuteur sans respiration est
        physiquement improbable (mesuré : 65 % des changements produits par
        l'ancien étage se faisaient sans aucune pause). Une unité collée à la
        précédente (< `pause`) dont le vote n'est pas franc (< `min_share`)
        hérite donc du locuteur précédent au lieu d'inventer un tour.

        Renvoie des blocs [{speaker,start,end,text,conf}] ou None si la frise
        n'est pas disponible (l'appelant retombe alors sur l'ancien chemin)."""
        import numpy as np
        tw = getattr(self, "_last_windows", None)
        if not tw or not words:
            return None
        T, lab = tw
        T = np.asarray(T, dtype=float)
        lab = np.asarray(lab)
        if len(T) == 0 or len(lab) == 0:
            return None
        ww = [w for w in words if (w.get("word") or "").strip()]
        if not ww:
            return None

        # 1) Grouper les mots en UNITÉS DE PAROLE : on coupe sur une vraie pause.
        units, cur = [], [ww[0]]
        for prev, w in zip(ww, ww[1:]):
            gap = float(w.get("start", 0.0)) - float(prev.get("end", 0.0))
            if gap >= pause:
                units.append(cur); cur = [w]
            else:
                cur.append(w)
        units.append(cur)

        # 2) Un vote par unité, pondéré par le RECOUVREMENT TEMPOREL réel avec les
        #    régions de la frise (une région qui couvre 3 s de l'unité pèse plus
        #    qu'une région qui l'effleure sur 100 ms).
        K = int(lab.max()) + 1 if len(lab) else 1
        votes = []
        for u in units:
            u0 = float(u[0].get("start", 0.0))
            u1 = max(float(x.get("end", u0)) for x in u)
            ov = np.minimum(T[:, 1], u1) - np.maximum(T[:, 0], u0)
            ov = np.clip(ov, 0.0, None)
            w = np.zeros(K, dtype=float)
            if ov.sum() > 0:
                for j in np.nonzero(ov)[0]:
                    w[int(lab[j])] += ov[j]
            else:   # unité sans recouvrement (silence de la frise) : région la plus proche
                c = (T[:, 0] + T[:, 1]) / 2.0
                w[int(lab[int(np.argmin(np.abs(c - 0.5 * (u0 + u1))))])] = 1.0
            tot = w.sum()
            votes.append((int(w.argmax()), float(w.max() / tot) if tot > 0 else 0.0,
                          u0, u1))

        # 3) Garde de continuité : pas de changement de locuteur sans respiration,
        #    sauf si le vote est franc (vrai enchaînement ou chevauchement net).
        spk_of_unit = []
        for i, (best, share, u0, u1) in enumerate(votes):
            if i > 0:
                prev_spk = spk_of_unit[-1]
                gap = u0 - votes[i - 1][3]
                if best != prev_spk and gap < pause and share < min_share:
                    best = prev_spk
            spk_of_unit.append(best)

        # 4) Étiquette par mot (l'unité entière porte le même locuteur) + score
        #    de fiabilité d'attribution, pondéré par la durée de l'unité.
        labels, confs, num, den = [], [], 0.0, 0.0
        for u, s, (_b, share, u0, u1) in zip(units, spk_of_unit, votes):
            labels.extend(["SPEAKER_%02d" % int(s)] * len(u))
            confs.extend([share] * len(u))
            d = max(0.0, u1 - u0)
            num += share * d; den += d
        self._last_attr_conf = round(num / den, 4) if den > 0 else 0.0
        self._last_units = len(units)
        print(f"[whotalks] attribution par unité : {len(units)} unités "
              f"({len(ww)} mots), fiabilité {100 * self._last_attr_conf:.0f} %.")
        blocks = _words_to_blocks(ww, labels)
        return blocks

    def assign_words_precise(self, words, wav_path, pad=0.2, margin=0.18,
                             embed_batch_fn=None):
        """Attribution au mot CIBLÉE : on part des labels-fenêtre (gratuits, ~98 %
        sur les zones nettes) et on ne calcule une empreinte SERRÉE par mot QUE
        dans les zones INCERTAINES — là où le coarse hésite :
          - fenêtre à faible marge (top1-top2 des cosinus < `margin`) = transition
            ou chevauchement potentiel ;
          - mot couvert par une fenêtre bordant un changement de label (transition).
        Ailleurs (un seul locuteur franc), on garde le label-fenêtre. Capte les
        interruptions courtes que les fenêtres noyaient (94->98 % sur conv. avec
        coupures) en n'embarquant au mot que ~20-30 % des mots -> coût divisé par
        ~3-5 vs le tout-au-mot, à qualité égale. Renvoie des blocs, ou None.

        Repli : si les empreintes-fenêtre manquent (offline), on retombe sur le
        tout-au-mot (chaque mot embarqué).

        embed_batch_fn (v27, optionnel) : lots-de-spans -> embeddings L2 (mode
        'raw'), typiquement make_subproc_embedder(wav, sr) — les empreintes-mot
        sont alors extraites en SOUS-PROCESS (l'arène ONNX à tailles variables
        meurt avec le worker) et le WAV n'est PAS chargé en RAM ici."""
        import numpy as np
        import wave
        C = getattr(self, "_last_C", None)
        win = getattr(self, "_last_windows", None)
        E = getattr(self, "_last_E", None)
        ext = None if embed_batch_fn is not None else self._ensure_extractor()
        if C is None or not words or not wav_path:
            return None
        if embed_batch_fn is None and ext is None:
            return None
        au = None
        try:
            with wave.open(wav_path, "rb") as wf:
                sr = wf.getframerate(); ch = wf.getnchannels()
                n_frames = wf.getnframes()
                if embed_batch_fn is None:
                    au = np.frombuffer(wf.readframes(n_frames),
                                       dtype=np.int16).astype(np.float32) / 32768.0
            if au is not None:
                au = _downmix(au, ch)
        except Exception as e:
            print(f"[whotalks] assign_words_precise lecture KO ({e}).")
            return None
        total_dur = n_frames / float(sr)
        ww = [w for w in words if (w.get("word") or "").strip()]
        if not ww:
            return None

        # --- Pré-calcul : labels-fenêtre + fenêtres incertaines -------------- #
        Tarr = None; wlab = None; uncertain_win = None
        if win is not None and E is not None and len(E) == len(win[1]):
            T, lab = win
            Tarr = np.asarray(T, dtype=np.float32)
            lab = np.asarray(lab)
            sims = E @ C.T                                  # N x K cosinus
            part = np.sort(sims, axis=1)
            mgn = (part[:, -1] - part[:, -2]) if part.shape[1] >= 2 else \
                np.full(len(part), 1.0, dtype=np.float32)
            low = mgn < margin
            edge = np.zeros(len(lab), dtype=bool)
            if len(lab) > 1:
                chg = lab[1:] != lab[:-1]
                edge[1:] |= chg; edge[:-1] |= chg            # fenêtres bordant un saut
            uncertain_win = low | edge
            # Chevauchement DENSE (beaucoup de fenêtres à faible marge) : les
            # interjections courtes tombent dans des fenêtres pourtant « franches »
            # que ce filtre ne voit pas. Au-delà du seuil, on bascule en tout-au-mot
            # pour garder >=98 %. Sinon on reste ciblé/rapide.
            # v29 : 0.045 -> WORD_ALLWORD_TRIP (0.06). L'AGC a légèrement resserré la
            # distribution des marges (volume égalisé), faisant passer des entretiens
            # NETS de ~4,4 % à ~5,0 % -> ils basculaient à tort en tout-au-mot (lent)
            # sans gain de justesse (mesuré : corpus identique en ciblé). 0.06 garde
            # la bascule pour les VRAIS chevauchements (>6 %) sans pénaliser les nets.
            if low.mean() > WORD_ALLWORD_TRIP:
                uncertain_win = np.ones(len(lab), dtype=bool)
                print("[whotalks] chevauchement dense -> attribution tout-au-mot.")
            wlab = lab

        def _win_idx(t):
            if Tarr is None:
                return -1
            j = int(np.searchsorted(Tarr[:, 0], t, side="right") - 1)
            return max(0, min(len(Tarr) - 1, j))

        minlen = int(0.4 * sr)

        def _span_sec(ws, we):
            """Span paddé/clampé en SECONDES (même géométrie que l'_embed
            historique, partagée in-process / sous-process)."""
            s0 = max(0.0, ws - pad)
            s1 = min(total_dur, we + pad)
            if (s1 - s0) * sr < minlen:
                s1 = min(total_dur, s0 + minlen / float(sr))
            return s0, s1

        def _embed_inproc(s0, s1):
            seg = au[int(s0 * sr):int(s1 * sr)]
            if len(seg) < int(0.1 * sr):
                return None
            seg = self._agc(seg)               # v29 : AGC, même espace que la frise
            if seg is None:
                return None
            st = ext.create_stream(); st.accept_waveform(sr, seg); st.input_finished()
            e = np.array(ext.compute(st), dtype=np.float32)
            e /= np.linalg.norm(e) + 1e-9
            return e

        # PASSE 1 : qui a besoin d'une empreinte serrée ? (logique inchangée)
        spks, pending = [], []   # pending = (position dans spks, s0, s1, j)
        for w in ww:
            ws = float(w.get("start", 0.0)); we = float(w.get("end", ws))
            j = _win_idx(0.5 * (ws + we))
            need = (uncertain_win is None) or (j < 0) or bool(uncertain_win[j])
            if need:
                s0, s1 = _span_sec(ws, we)
                pending.append((len(spks), s0, s1, j))
                spks.append(None)              # résolu en passe 2
            else:
                spks.append(int(wlab[j]))
        # PASSE 2 : extraction (lot sous-process, ou in-process à l'identique)
        n_embed = 0
        if pending:
            if embed_batch_fn is not None:
                embs = embed_batch_fn([(s0, s1) for _, s0, s1, _ in pending])
            else:
                embs = [_embed_inproc(s0, s1) for _, s0, s1, _ in pending]
            if embs is None:                   # mécanisme sous-process KO
                embs = [None] * len(pending)
            for (pos, _s0, _s1, j), e in zip(pending, embs):
                if e is not None:
                    spks[pos] = int((C @ np.asarray(e, dtype=np.float32)).argmax())
                    n_embed += 1
                else:
                    spks[pos] = int(wlab[j]) if (wlab is not None and j >= 0) \
                        else next((s for s in reversed(spks[:pos])
                                   if s is not None), 0)
        if uncertain_win is not None:
            print(f"[whotalks] attribution ciblée : {n_embed}/{len(ww)} mots "
                  f"embarqués ({100 * n_embed // max(1, len(ww))} %).")
        # lissage des flips d'un seul mot, puis groupage en blocs (helpers
        # partagés avec assign_words et merge_transcript_with_speakers)
        sm = _smooth_single_flips(spks)
        # v1.0.23 — RECALAGE DES FRONTIÈRES sur les coupures naturelles. C'EST
        # LE CHEMIN DE PRODUCTION (assign_words n'est que le repli) : mesuré
        # 92,3 % -> 96,9 % de justesse et 60 % -> 32 % de blocs coupés au milieu
        # d'une phrase. VLOCAL_DIAR_SNAP=0 pour revenir au comportement d'avant.
        if os.environ.get("VLOCAL_DIAR_SNAP", "1") != "0":
            sm = snap_labels_to_speech(ww, sm)
        return _words_to_blocks(ww, ["SPEAKER_%02d" % int(s) for s in sm])

    @property
    def stream_count(self):
        return len(self._st_embs)

    # -- gestion mémoire (relais) ------------------------------------------- #
    def load(self) -> bool:
        """Pré-charge l'extracteur d'empreintes WhoTalks. False si indispo."""
        return self._ensure_extractor() is not None

    def unload(self) -> None:
        """Libère les moteurs ONNX et la RAM associée."""
        with self._lock:
            self._sd = None
            self._num = None
            self._ext = None
        self.reset_stream()
        import gc
        gc.collect()

    def _ensure_extractor(self):
        """WhoTalks — charge l'extracteur d'empreintes CAM++ (léger). C'est le
        SEUL modèle nécessaire au moteur WhoTalks (pas de segmentation lourde)."""
        if self._ext is not None:
            return self._ext
        if not available():
            return None
        with self._lock:
            if self._ext is None:
                import sherpa_onnx as so
                nt = _threads()
                self._ext = so.SpeakerEmbeddingExtractor(
                    so.SpeakerEmbeddingExtractorConfig(
                        model=self.emb_model, num_threads=nt))
        return self._ext

    def _ensure(self, num_clusters: int, threshold: float = CLUSTER_THRESHOLD):
        """(Re)construit OfflineSpeakerDiarization pour le nb de clusters voulu.
        sherpa fixe num_clusters à la config -> on rebuild si ça change."""
        if not available():
            return None
        with self._lock:
            if self._sd is not None and self._num == num_clusters:
                return self._sd
            import sherpa_onnx as so
            nt = _threads()   # même plafond mesuré que Whisper (v20)
            cfg = so.OfflineSpeakerDiarizationConfig(
                segmentation=so.OfflineSpeakerSegmentationModelConfig(
                    pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(
                        model=self.seg_model),
                    num_threads=nt),
                embedding=so.SpeakerEmbeddingExtractorConfig(
                    model=self.emb_model, num_threads=nt),
                clustering=so.FastClusteringConfig(
                    num_clusters=num_clusters, threshold=threshold),
                min_duration_on=0.3, min_duration_off=0.8,
            )
            sd = so.OfflineSpeakerDiarization(cfg)
            self._sd = sd
            self._num = num_clusters
            return sd

    # -- diarisation -------------------------------------------------------- #
    def _run(self, sd, samples, on_progress):
        """Lance sd.process avec un callback de progression. Le callback fait
        revenir périodiquement dans Python -> le GIL est relâché -> l'UI ne gèle
        PAS (fini le beachball sur les longues réunions)."""
        cb = None
        if on_progress:
            def cb(done, total):
                try:
                    on_progress(min(1.0, float(done) / max(1, total)))
                except Exception:
                    pass
                return 0
        try:
            return sd.process(samples, callback=cb).sort_by_start_time()
        except TypeError:
            # binding sans callback -> appel simple
            return sd.process(samples).sort_by_start_time()

    def diarize(self, wav_path: str, num_speakers: int = 0,
                max_speakers: int = 8, on_progress=None) -> list:
        """WhoTalks v1 — moteur principal (empreintes CAM++ + silhouette + frise),
        validé sur audio réel : auto-détecte le nombre de voix (K=2 là où l'ancien
        donnait 54) et ~7x plus rapide. Repli silencieux sur l'ancien moteur
        OfflineSpeakerDiarization si WhoTalks échoue. Jamais d'exception."""
        # Purge de l'état _last_* du run PRÉCÉDENT : sur instance réutilisée, un
        # repli offline (qui ne pose pas ces champs) ne doit jamais servir à
        # last_voices()/assign_words* les centroïdes/fenêtres d'un AUTRE audio.
        self._last_voices = {}
        self._last_windows = None
        self._last_C = None
        self._last_E = None
        try:
            segs = self._whotalks(wav_path, num_speakers, max_speakers, on_progress)
            if segs:
                return postprocess_segments(segs, min_dur=POST_MIN_DUR, gap=POST_GAP)
            print("[whotalks] résultat vide -> repli moteur offline.")
        except Exception as e:
            print(f"[whotalks] échec ({e}) -> repli moteur offline.")
        try:
            return self._diarize_offline(wav_path, num_speakers, max_speakers,
                                         on_progress)
        except Exception as e:
            print(f"[diar] échec total (repli sans locuteurs) : {e}")
            return []

    def _whotalks(self, wav_path, num_speakers, max_speakers, on_progress):
        """Moteur WhoTalks : fenêtres 2 s gatées sur l'énergie -> empreintes CAM++
        L2-normalisées -> auto-détection du nombre de voix (silhouette) ou nombre
        forcé -> k-means sphérique -> frise temporelle. Léger et rapide."""
        import numpy as np
        import wave
        ext = self._ensure_extractor()
        if ext is None:
            return []
        with wave.open(wav_path, "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            raw = w.readframes(w.getnframes())
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        a = _downmix(a, ch)
        win = int(TILE_WIN_S * sr)
        hop = int(TILE_HOP_S * sr)
        rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
        gate = 0.5 * rms
        embs, times = [], []
        total = max(1, (len(a) - win) // hop)
        for j, s in enumerate(range(0, max(0, len(a) - win), hop)):
            seg = a[s:s + win]
            if float(np.sqrt(np.mean(seg * seg))) < gate:
                continue
            st = ext.create_stream()
            st.accept_waveform(sr, seg)
            st.input_finished()
            e = np.array(ext.compute(st), dtype=np.float32)
            e /= np.linalg.norm(e) + 1e-9
            embs.append(e)
            times.append((s / sr, (s + win) / sr))
            if on_progress and (j % 40 == 0):
                try:
                    on_progress(min(0.95, j / total))
                except Exception:
                    pass
        if len(embs) < 3:
            return []
        E = np.array(embs)
        T = np.array(times)
        lab, K, sil, forced = self._cluster_and_remember(E, T, num_speakers,
                                                         max_speakers)
        if not forced:
            print(f"[whotalks] {K} voix auto-détectées (silhouette {sil:.2f}).")
        if on_progress:
            try:
                on_progress(1.0)
            except Exception:
                pass
        return _labels_to_segments(T, lab)

    def _diarize_offline(self, wav_path: str, num_speakers: int = 0,
                         max_speakers: int = 6, on_progress=None) -> list:
        """Ancien moteur (OfflineSpeakerDiarization) — REPLI uniquement."""
        try:
            import numpy as np
            import wave
            with wave.open(wav_path, "rb") as w:
                sr = w.getframerate()
                ch = w.getnchannels()
                raw = w.readframes(w.getnframes())
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            samples = _downmix(samples, ch)
            forced = num_speakers if num_speakers and num_speakers > 0 else -1
            sd = self._ensure(num_clusters=forced)
            if sd is None:
                return []
            if sr != sd.sample_rate:
                samples = _resample(samples, sr, sd.sample_rate)
            # NB : aucun prétraitement (pré-emphase/normalisation) — mesuré NUISIBLE
            # (dégradait la séparation : forcé-2 -> 1). sherpa/CAM++ normalisent en
            # interne ; l'audio brut est passé tel quel (cf. note en tête de module).
            result = self._run(sd, samples, on_progress)
            segs = [{"start": float(s.start), "end": float(s.end),
                     "speaker": "SPEAKER_%02d" % int(s.speaker)} for s in result]
            n = len({s["speaker"] for s in segs})
            # Garde-fou : auto sur-segmenté -> on borne à max_speakers (1 relance).
            if forced == -1 and max_speakers and n > max_speakers:
                print(f"[diar] auto={n} locuteurs (> {max_speakers}) -> relance "
                      f"bornée à {max_speakers}.")
                # Libère l'ANCIEN pipeline AVANT d'en construire un second :
                # sinon 2x segmentation + 2x CAM++ coexistent en RAM pendant la
                # relance, dans le chemin de repli censé être le plus frugal.
                # NB : self._lock n'est PAS réentrant -> on le referme avant
                # d'appeler _ensure() (qui le reprend).
                with self._lock:
                    self._sd = None
                    self._num = None
                del sd
                sd2 = self._ensure(num_clusters=max_speakers)
                if sd2 is not None:
                    result = self._run(sd2, samples, on_progress)
                    segs = [{"start": float(s.start), "end": float(s.end),
                             "speaker": "SPEAKER_%02d" % int(s.speaker)}
                            for s in result]
            # v18.6 — anti-scintillement (réassignation courts + médian + fusion).
            segs = postprocess_segments(segs, min_dur=POST_MIN_DUR, gap=POST_GAP)
            return segs
        except Exception as e:
            print(f"[diar] échec diarisation (repli sans locuteurs) : {e}")
            return []


def _resample(x, sr_in: int, sr_out: int):
    """Rééchantillonnage linéaire léger (sans scipy). Suffisant pour la
    diarisation (on ne juge pas la qualité audio, juste les frontières)."""
    import numpy as np
    if sr_in == sr_out or x.size == 0:
        return x
    n_out = int(round(len(x) * sr_out / sr_in))
    xp = np.linspace(0.0, 1.0, num=len(x), endpoint=False)
    fp = x
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, xp, fp).astype(np.float32)


# ===================== FONCTIONS PURES (testables sans modèle) ============= #

def find_speaker_at(t: float, speaker_segments: list) -> str:
    """Locuteur actif à l'instant t. 'SPEAKER_UNKNOWN' si aucun segment ne couvre
    t : on prend alors le segment le PLUS PROCHE (robustesse aux micro-trous de
    la diarisation), sinon UNKNOWN si la liste est vide."""
    if not speaker_segments:
        return "SPEAKER_UNKNOWN"
    for seg in speaker_segments:
        if seg["start"] <= t <= seg["end"]:
            return seg["speaker"]
    # Aucun segment ne contient t (trou) -> plus proche par distance temporelle.
    # (Le cas « t couvert » est impossible ici : la 1re boucle aurait retourné.)
    best, best_d = None, None
    for seg in speaker_segments:
        d = min(abs(t - seg["start"]), abs(t - seg["end"]))
        if best_d is None or d < best_d:
            best, best_d = seg["speaker"], d
    return best or "SPEAKER_UNKNOWN"


def words_from_segments(segments: list) -> list:
    """Aplati les segments Whisper en MOTS horodatés [{word(brut), start, end}].
    Si un segment n'a pas de timestamps mot (anciennes données), on découpe son
    texte et on répartit les instants linéairement -> attribution au mot possible
    quand même."""
    out = []
    for s in segments or []:
        ws = s.get("words") or []
        usable = [w for w in ws if isinstance(w, dict) and "start" in w and (w.get("word") or "").strip()]
        if usable:
            for w in usable:
                out.append({"word": w.get("word") or "",
                            "start": float(w.get("start", s.get("start", 0.0))),
                            "end": float(w.get("end", s.get("end", 0.0)))})
        else:
            # repli : découpe le texte du segment, instants interpolés
            toks = (s.get("text") or "").split()
            if not toks:
                continue
            s0 = float(s.get("start", 0.0)); s1 = float(s.get("end", s0))
            step = (s1 - s0) / len(toks)   # len(toks) >= 1 garanti par le continue
            for i, tok in enumerate(toks):
                out.append({"word": " " + tok, "start": s0 + i * step,
                            "end": s0 + (i + 1) * step})
    return out


def _smooth_single_flips(spks: list) -> list:
    """LISSAGE des flips d'un seul mot : un mot isolé attribué à une voix alors
    que ses deux voisins sont l'AUTRE voix est presque toujours une erreur de
    frontière (ex. « ! Et ») -> on le recolle au voisin. Conserve les vrais
    tours (≥ 2 mots). Pur ; les labels peuvent être des ints (assign_words*)
    ou des chaînes (merge_transcript_with_speakers), comparés par simple ==."""
    sm = spks[:]
    for i in range(1, len(spks) - 1):
        if spks[i - 1] == spks[i + 1] and spks[i] != spks[i - 1]:
            sm[i] = spks[i - 1]
    return sm


def snap_labels_to_speech(words: list, labels: list, max_shift: int = 2,
                          pause_min: float = 0.20) -> list:
    """v1.0.23 — RECALE LES CHANGEMENTS DE LOCUTEUR SUR LES COUPURES NATURELLES.

    Constat sur une réunion réelle : 56 % des blocs se terminaient AU MILIEU
    d'une phrase (« Vous voulez inviter d'autre genre à voir » / « ça ? Si. »).
    L'attribution elle-même est bonne — c'est la POSITION de la frontière qui
    est fausse de un ou deux mots, et c'est ce que l'utilisateur lit.

    On ne touche donc PAS à qui parle : on déplace seulement la frontière vers
    la coupure naturelle la plus proche (fin de phrase, ou vraie respiration),
    dans une fenêtre de `max_shift` mots. Au-delà, on ne bouge pas : mieux vaut
    une frontière mal placée qu'une phrase attribuée au mauvais locuteur.

    `words` : [{word,start,end}] ; `labels` : un label par mot. Renvoie les
    labels recalés (même longueur).

    MESURÉ sur deux fenêtres annotées indépendantes d'une réunion réelle de
    4 personnes (référence établie par le contenu, hors diarisation) :
      sans recalage      92,3 % / 92,3 %  -> 92,3 %   (60 % de blocs coupés
                                                       au milieu d'une phrase)
      max_shift = 2      97,1 % / 96,5 %  -> 96,9 %   (32 %)
      max_shift = 3      96,7 % / 88,4 %  -> 93,8 %   INSTABLE
    D'où `max_shift = 2` : au-delà, la frontière peut partir trop loin et
    emporter des mots au mauvais locuteur. Ne pas augmenter sans re-mesurer."""
    if not words or not labels or len(words) != len(labels):
        return labels
    out = list(labels)
    n = len(out)

    def _is_break(i: int) -> float:
        """Qualité d'une coupure AVANT le mot i : plus c'est haut, mieux c'est."""
        if i <= 0 or i >= n:
            return -1.0
        prev = (words[i - 1].get("word") or "").strip()
        gap = float(words[i].get("start", 0.0)) - float(words[i - 1].get("end", 0.0))
        score = 0.0
        if prev.endswith((".", "!", "?", "…")):
            score += 2.0                     # fin de phrase : la meilleure coupure
        elif prev.endswith((",", ";", ":")):
            score += 0.5
        if gap >= pause_min:
            score += 1.0 + min(gap, 1.0)     # respiration réelle
        return score

    i = 1
    while i < n:
        if out[i] == out[i - 1]:
            i += 1
            continue
        # frontière en i : cherche la meilleure coupure naturelle à portée
        here = _is_break(i)
        best_j, best_s = i, here
        for j in range(max(1, i - max_shift), min(n, i + max_shift + 1)):
            s = _is_break(j)
            # à qualité égale on préfère ne pas bouger (|j-i| départage)
            if s > best_s + 1e-9 or (abs(s - best_s) < 1e-9 and abs(j - i) < abs(best_j - i)):
                best_j, best_s = j, s
        if best_j != i and best_s > here:
            new = out[i]
            if best_j > i:        # la frontière recule : les mots i..best_j-1
                for k in range(i, best_j):      # repassent au locuteur précédent
                    out[k] = out[i - 1]
            else:                 # la frontière avance : best_j..i-1 au nouveau
                for k in range(best_j, i):
                    out[k] = new
            i = max(best_j, i) + 1
        else:
            i += 1
    return out


def _polish_block_boundaries(blocks: list) -> list:
    """v29.5 — nettoie les ARTEFACTS de frontière au mot (purement cosmétique sur
    le TEXTE, n'affecte pas le clustering ni l'attribution réelle) :
      1. ponctuation terminale en TÊTE d'un bloc (« ? Oui bien sûr ») : elle clôt
         la phrase du locuteur PRÉCÉDENT -> rapatriée en fin du bloc d'avant ;
      2. fragment d'UNE lettre capitale en FIN, suivi d'un bloc démarrant par une
         apostrophe (« …situations. C » / « 'est une bonne nouvelle » = « C'est »
         coupé à la jointure) -> le « C » recollé au bloc suivant.
    Corrige les glissements d'un caractère vus en frontière de tour, sans risque."""
    import re as _re
    if not blocks or len(blocks) < 2:
        return blocks
    b = [dict(x) for x in blocks]
    for i in range(1, len(b)):                       # 1) ponctuation en tête
        cur = b[i]["text"].lstrip()
        m = _re.match(r"^([?!.;:…]+)", cur)
        if m and b[i - 1]["text"].strip():
            b[i - 1]["text"] = b[i - 1]["text"].rstrip() + m.group(1)
            b[i]["text"] = cur[m.end():].lstrip()
    for i in range(len(b) - 1):                      # 2) « C » + « 'est »
        t = b[i]["text"].rstrip()
        m = _re.search(r"(^|\s)([A-ZÀ-Ý])$", t)
        nxt = b[i + 1]["text"].lstrip()
        if m and nxt[:1] == "'":
            b[i]["text"] = t[:m.start()].rstrip()
            b[i + 1]["text"] = m.group(2) + nxt
    return [x for x in b if (x.get("text") or "").strip()]


def _words_to_blocks(ww: list, spk_strs: list) -> list:
    """GROUPAGE mots -> blocs [{speaker,start,end,text}] (mots consécutifs d'un
    même locuteur regroupés). Style Whisper (les mots portent leur espace de
    tête : « Bonjour », « c », « 'est ») -> concaténation BRUTE qui reproduit
    le texte exact. Sinon (mots « nus », ex. tests) -> jointure par espace.
    `spk_strs` : un label par mot de `ww`, DÉJÀ formaté en chaîne (le chemin
    merge transporte 'SPEAKER_UNKNOWN' : pas de conversion int ici)."""
    whisper_style = any((w.get("word") or "")[:1] == " " for w in ww[:80])
    blocks = []
    cur = None
    for w, spk in zip(ww, spk_strs):
        raw = w.get("word") or ""
        piece = raw if whisper_style else raw.strip()
        if cur is None or cur["speaker"] != spk:
            if cur is not None:
                cur["text"] = cur["text"].strip()
                blocks.append(cur)
            cur = {"speaker": spk, "start": float(w.get("start", 0.0)),
                   "end": float(w.get("end", 0.0)), "text": piece}
        else:
            cur["end"] = float(w.get("end", 0.0))
            cur["text"] += piece if whisper_style else (" " + piece)
    if cur is not None:
        cur["text"] = cur["text"].strip()
        blocks.append(cur)
    return _polish_block_boundaries(blocks)   # v29.5 — nettoyage frontières


def merge_transcript_with_speakers(whisper_words: list,
                                   speaker_segments: list) -> list:
    """ÉTAPE 3 — Fusionne les mots horodatés Whisper avec les segments locuteurs.

    whisper_words   : [{"word","start","end"}, ...] (mots BRUTS Whisper, l'espace
                      de tête est conservé pour reproduire le texte exact)
    speaker_segments: [{"start","end","speaker"}, ...]
    Renvoie des BLOCS : [{"speaker","start","end","text"}, ...] où les mots
    consécutifs d'un même locuteur sont regroupés. Le texte est reconstruit par
    CONCATÉNATION BRUTE (pas de strip+join) pour préserver « c'est », « l'idée »…"""
    ww = [w for w in whisper_words if (w.get("word") or "").strip()]
    # Locuteur de chaque mot (le lissage anti-flip et le groupage en blocs sont
    # factorisés dans _smooth_single_flips/_words_to_blocks, partagés avec
    # assign_words et assign_words_precise).
    spks = []
    for w in ww:
        mid = (float(w.get("start", 0.0)) + float(w.get("end", 0.0))) / 2.0
        spks.append(find_speaker_at(mid, speaker_segments))
    sm = _smooth_single_flips(spks)
    # v1.0.25 — RECALAGE ICI AUSSI. Ce chemin est le dernier repli, et c'est
    # précisément celui qu'une ré-identification empruntait quand le worker
    # d'empreintes échouait : le transcript revenait alors sans recalage
    # (59 % de blocs coupés en pleine phrase, contre 37 % par la voie normale)
    # et l'utilisateur ne voyait aucune amélioration. Le résultat est désormais
    # correct QUEL QUE SOIT le chemin emprunté.
    if os.environ.get("VLOCAL_DIAR_SNAP", "1") != "0":
        sm = snap_labels_to_speech(ww, sm)
    return _words_to_blocks(ww, sm)


def apply_speaker_names(blocks: list, names: dict) -> list:
    """ÉTAPE 6 — Applique une map {'SPEAKER_00': 'Marie'} à l'affichage. Ne touche
    PAS au champ `speaker` (identifiant stable) ; ajoute `label` pour l'UI.
    NON UTILISÉE en prod (les noms voyagent dans speaker_blocks_json et l'UI
    applique la map `names` côté JS) — conservée comme pur utilitaire testé
    (tests/test_diarization_v18.py)."""
    out = []
    for b in blocks:
        nb = dict(b)
        nb["label"] = (names or {}).get(b["speaker"], "")
        out.append(nb)
    return out


def reassign_portion(blocks: list, block_index: int, word_start: int,
                     word_end: int, new_speaker: str) -> list:
    """ÉTAPE 7 — Réassigne une PORTION (mots [word_start:word_end] du bloc
    block_index) à new_speaker. La portion devient un nouveau bloc ; le reste du
    bloc d'origine est conservé. Puis on FUSIONNE les blocs adjacents de même
    locuteur. Pure manipulation de liste (pas de re-diarisation)."""
    if block_index < 0 or block_index >= len(blocks):
        return blocks
    b = blocks[block_index]
    words = b["text"].split()
    word_start = max(0, word_start)
    word_end = min(len(words), word_end)
    if word_start >= word_end:
        return blocks
    before = words[:word_start]
    middle = words[word_start:word_end]
    after = words[word_end:]
    # Découpe le temps PROPORTIONNELLEMENT au nombre de mots (approx. de timing
    # uniforme) pour que les 3 morceaux ne se chevauchent pas sur la frise.
    b0 = float(b.get("start", 0.0))
    b1 = float(b.get("end", b0))
    n = max(1, len(words))
    dur = b1 - b0
    t_bef = b0 + (word_start / n) * dur
    t_mid = b0 + (word_end / n) * dur
    pieces = []
    if before:
        pieces.append({"speaker": b["speaker"], "text": " ".join(before),
                       "start": b0, "end": t_bef})
    pieces.append({"speaker": new_speaker, "text": " ".join(middle),
                   "start": t_bef, "end": t_mid})
    if after:
        pieces.append({"speaker": b["speaker"], "text": " ".join(after),
                       "start": t_mid, "end": b1})
    result = blocks[:block_index] + pieces + blocks[block_index + 1:]
    return _merge_adjacent_same_speaker(result)


def _merge_adjacent_same_speaker(blocks: list) -> list:
    """Fusionne les blocs adjacents partageant le même locuteur."""
    out = []
    for b in blocks:
        if out and out[-1]["speaker"] == b["speaker"]:
            out[-1]["text"] = (out[-1]["text"] + " " + b["text"]).strip()
            out[-1]["end"] = b.get("end", out[-1].get("end"))
        else:
            out.append(dict(b))
    return out


# ============================================================================
# v27 — EXTRACTION CAM++ EN SOUS-PROCESS (RAM principale plate)
# L'arène mémoire d'ONNX Runtime « grow & keep » fuit ~6-12 Mo/inférence et
# n'est PAS récupérable in-process (mesuré ; del/gc/recréation inopérants).
# On extrait donc les embeddings PAR LOTS dans un process jetable : l'OS
# récupère tout à sa mort, le process principal ne garde que les vecteurs
# (192 floats/région). Worker = CE fichier en dev (python diarizer.py
# --emb-worker job out) ; en app gelée, le binaire re-exécuté avec
# --emb-worker (shim tout en haut d'app.py, avant les imports lourds).
# ============================================================================

# Tailles de lot CALIBRÉES sur le RSS mesuré des workers (base ~180 Mo +
# ~6,3 Mo/inférence en mode région, ~1,9 Mo en mode raw) pour tenir le contrat
# RAM MACHINE : optimum MESURÉ (blocs 450 s + lots 150/300 testés = PIRES en
# vitesse ET en RAM, non-monotone) : région 80 -> worker ~680 Mo ; raw 200 ->
# worker ~505 Mo ; pic machine ~1,5-1,6 Go, marge confortable sous le contrat
# nuit-2 (2,25 Go).
_SUBPROC_BATCH = 80
_SUBPROC_BATCH_RAW = 200


def _wav_data_offset(wav_path: str) -> int:
    """Offset réel du chunk 'data' (les entêtes WAV ne font pas toujours 44 o)."""
    with open(wav_path, "rb") as f:
        head = f.read(4096)
    i = head.find(b"data")
    if i < 0:
        raise ValueError("chunk 'data' introuvable")
    return i + 8


def _helper_exe():
    """v1.0.25 — CHEMIN DE LANCEMENT DES SOUS-PROCESS, SANS ICÔNE DANS LE DOCK.

    macOS enregistre comme APPLICATION tout binaire lancé depuis
    `Contents/MacOS/` d'un bundle .app : chaque worker obtenait donc sa propre
    icône. Corriger la politique d'activation depuis Python arrive trop tard —
    l'icône a déjà clignoté le temps que l'interpréteur démarre (mesuré :
    REGULAR puis accessory). Lancé depuis `Contents/Helpers/`, le même binaire
    n'est jamais enregistré : il démarre directement en « prohibited ».
    Le bundle expose donc un lien dur `Contents/Helpers/VlocalWorker` (créé au
    build, même inode donc même signature) et un lien `_internal` vers
    `../Frameworks` pour que PyInstaller retrouve ses bibliothèques.
    Repli sur sys.executable si le lien manque (build antérieur)."""
    import sys, os
    if not getattr(sys, "frozen", False):
        return sys.executable
    exe = sys.executable
    try:
        macos = os.path.dirname(exe)                       # .../Contents/MacOS
        cand = os.path.join(os.path.dirname(macos), "Helpers", "VlocalWorker")
        if os.path.exists(cand):
            return cand
    except Exception:
        pass
    return exe


def _worker_cmd(job_path: str, out_path: str):
    """Commande du worker : binaire gelé re-exécuté, ou ce fichier en dev."""
    import sys
    if getattr(sys, "frozen", False):
        return [_helper_exe(), "--emb-worker", job_path, out_path]
    return [sys.executable, os.path.abspath(__file__),
            "--emb-worker", job_path, out_path]


def _extract_spans_subproc(wav_path, sr, spans, batch=_SUBPROC_BATCH,
                           mode="region", on_progress=None):
    """Extrait les embeddings CAM++ des spans [(t0,t1)...] par lots en
    sous-process. Renvoie une liste alignée (embedding L2 ou None par span),
    ou None si le MÉCANISME échoue (spawn impossible) -> l'appelant replie
    in-process. Un lot qui échoue individuellement = embeddings None (les
    régions sont héritées du voisin à la frise, comme un span silencieux).

    mode='region' : normalisation _emb_span (fenêtre fixe 3 s) — pour la frise.
    mode='raw'    : span BRUT longueur variable — pour l'attribution au MOT
                    (byte-identique à l'_embed historique ; l'arène à tailles
                    variables grossit dans le worker JETABLE, pas dans l'app ;
                    lots plus petits)."""
    import json as _json
    import subprocess as _sp
    import sys
    import tempfile

    import numpy as np
    if mode == "raw":
        batch = _SUBPROC_BATCH_RAW
    out_all = [None] * len(spans)
    spawned_ok = False
    for b0 in range(0, len(spans), batch):
        # v1.0.24 — avancement (ré-identification : l'utilisateur attend
        # plusieurs minutes sans le moindre signe de vie). Best-effort strict :
        # un callback qui lève ne doit jamais interrompre l'extraction.
        if on_progress is not None:
            try:
                on_progress(b0, len(spans))
            except Exception:
                pass
        lot = [(float(a), float(bb)) for (a, bb) in spans[b0:b0 + batch]]
        jf = of = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json",
                                             delete=False) as f:
                _json.dump({"wav": os.path.abspath(wav_path), "sr": int(sr),
                            "spans": lot, "mode": mode}, f)
                jf = f.name
            of = jf + ".npz"
            r = _sp.run(_worker_cmd(jf, of), stdout=_sp.DEVNULL,
                        stderr=_sp.PIPE, timeout=600)
            if r.returncode != 0 or not os.path.exists(of):
                err = (r.stderr or b"")[-300:].decode("utf-8", "replace").strip()
                if not spawned_ok:
                    print(f"[whotalks] worker KO au 1er lot (rc={r.returncode}"
                          f"{' : ' + err if err else ''}) -> repli.")
                    return None          # mécanisme KO dès le 1er lot -> repli
                print(f"[whotalks] worker lot {b0 // batch} KO (rc={r.returncode}"
                      f"{' : ' + err if err else ''}) -> régions héritées.")
                continue
            z = np.load(of)
            for k, i in zip(range(len(z["idx"])), z["idx"]):
                out_all[b0 + int(i)] = z["E"][k]
            spawned_ok = True
        except Exception as e:
            if not spawned_ok:
                print(f"[whotalks] extraction sous-process indisponible ({e}).")
                return None
            print(f"[whotalks] worker lot {b0 // batch} KO ({e}) -> héritées.")
        finally:
            for p in (jf, of):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
    return out_all


def make_subproc_embedder(wav_path, sr, mode="raw"):
    """Renvoie une fonction lots-de-spans -> embeddings (pour l'attribution au
    mot : assign_words_precise(embed_batch_fn=...))."""
    def _fn(spans):
        return _extract_spans_subproc(wav_path, sr, spans, mode=mode)
    return _fn


def emb_worker_main(argv):
    """Point d'entrée du WORKER (--emb-worker job.json out.npz) : extrait les
    embeddings du lot et sort. mode 'region' = Diarizer._emb_span (byte-identique
    à l'in-process) ; mode 'raw' = span brut longueur variable (attribution au
    mot, byte-identique à l'_embed historique). Code de sortie 0 si OK."""
    import json as _json

    import numpy as np
    try:
        # argv EXPLICITE (les 2 arguments APRÈS --emb-worker) : avec
        # argv_emulation du spec, le bootloader gelé peut APPENDRE des chemins
        # d'Apple Events à sys.argv -> argv[-2:] serait faux.
        i = argv.index("--emb-worker")
        job_path, out_path = argv[i + 1], argv[i + 2]
        job = _json.load(open(job_path, encoding="utf-8"))
        wav, sr, spans = job["wav"], int(job["sr"]), job["spans"]
        mode = job.get("mode", "region")
        # Gardes format (l'app n'écrit que du PCM-16 mono ; un WAV inattendu
        # serait lu comme du bruit -> mieux vaut échouer PROPREMENT : rc=2 ->
        # le driver replie).
        import wave as _wave
        with _wave.open(wav, "rb") as _wf:
            if _wf.getsampwidth() != 2 or _wf.getnchannels() != 1:
                print("[emb-worker] WAV non PCM-16 mono", file=__import__("sys").stderr)
                return 2
            _nf = _wf.getnframes()
        au = np.memmap(wav, dtype="<i2", mode="r",
                       offset=_wav_data_offset(wav), shape=(_nf,))
        d = Diarizer()
        ext = d._ensure_extractor()
        if ext is None:
            return 2
        E, idx = [], []
        for i, (t0, t1) in enumerate(spans):
            if mode == "raw":
                s0 = max(0, int(t0 * sr)); s1 = min(len(au), int(t1 * sr))
                seg = np.asarray(au[s0:s1]).astype(np.float32) / 32768.0
                if len(seg) < int(0.1 * sr):
                    continue
                seg = Diarizer._agc(seg)        # v29 : même AGC que les régions
                if seg is None:                 # (sinon espaces d'empreintes
                    continue                    #  incompatibles -> attribution KO)
                st = ext.create_stream()
                st.accept_waveform(sr, seg)
                st.input_finished()
                e = np.array(ext.compute(st), dtype=np.float32)
                n = np.linalg.norm(e)
                e = e / (n + 1e-9) if n > 0 else None
            else:
                e = d._emb_span(ext, au, sr, t0, t1)
            if e is not None:
                E.append(e); idx.append(i)
        np.savez(out_path, E=np.array(E, dtype=np.float32),
                 idx=np.array(idx, dtype=np.int32))
        return 0
    except Exception as e:
        sys_stderr = __import__("sys").stderr
        print(f"[emb-worker] KO : {e}", file=sys_stderr)
        return 1


if __name__ == "__main__":
    import sys as _sys
    if "--emb-worker" in _sys.argv:
        _sys.exit(emb_worker_main(_sys.argv))
