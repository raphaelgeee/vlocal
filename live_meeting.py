#!/usr/bin/env python3
"""
Vlocal v16 — Transcription INCRÉMENTALE d'une réunion pendant l'enregistrement.

Problème résolu : passer une réunion d'1 h dans Whisper APRÈS l'arrêt = ~45 min
d'attente sur CPU. Ici on transcrit en continu, par fenêtres coupées sur les
silences, PENDANT que ça enregistre. À l'arrêt, il ne reste qu'une courte
fenêtre finale -> résultat quasi immédiat même sur une longue réunion.

Principe :
  - Le MeetingRecorder écrit un WAV mono 16 kHz 16 bits en continu.
  - On lit le PCM brut du fichier en croissance (après l'en-tête de 44 octets),
    sans dépendre de l'en-tête (nframes inconnu tant que non finalisé).
  - Dès qu'on a >= WINDOW_TARGET_S d'audio non transcrit, on cherche un point de
    coupe sur un SILENCE (pour ne pas couper en plein mot), on transcrit cette
    fenêtre (horodatages décalés) et on avance le curseur.
  - À l'arrêt : on transcrit la fenêtre finale restante.

Robuste : toute erreur est capturée ; en dernier recours, app.py peut toujours
retomber sur une transcription complète du fichier (transcribe_detailed).
"""

import os
import re
import threading

import numpy as np

# Point de vérité UNIQUE des seuils de fenêtrage : définis dans engine.py
# (chemin import) et partagés ici (chemin live), pour qu'un recalibrage ne
# désynchronise jamais silencieusement les deux chemins. Pas de cycle :
# engine n'importe pas live_meeting.
from engine import (SAMPLE_RATE, WINDOW_TARGET_S, WINDOW_MAX_S, SILENCE_RMS,
                    SILENCE_SEARCH_S)

WAV_HEADER_BYTES = 44          # en-tête WAV PCM standard
BYTES_PER_FRAME = 2            # int16 mono

# v17 — Overlap (chevauchement) entre fenêtres consécutives. AVANT v17 le
# curseur avançait EXACTEMENT de `cut` : aucun chevauchement, donc un mot coupé
# à une coupure FRANCHE (fenêtre pleine de 40 s ou arrêt) pouvait être perdu ou
# tronqué (Cas 1/3/4). On ré-inclut OVERLAP_S secondes au début de la fenêtre
# suivante (contexte sonore + le mot coupé est ré-entendu en entier). La
# répétition de texte ainsi créée est nettoyée par la dédup de jointure.
# NB : 1 s (et non 8 s) — 8 s re-transcrirait un tiers de chaque fenêtre pour un
# coût CPU élevé sans bénéfice. La coupure de fin (arrêt) n'a PAS d'overlap, donc
# la latence à l'arrêt est inchangée.
OVERLAP_S = 1.0

# v17 — ÉTAPE 1 — Métatexte halluciné par Whisper. La cause racine #1 est
# l'écho de notre propre initial_prompt glossaire (« Transcription en français.
# Termes propres au contexte : … ») recraché sur les fenêtres quasi-vides, plus
# les génériques de sous-titres. On les retire de CHAQUE fenêtre avant fusion.
METATEXT_HALLUCINATIONS = [
    r"transcription en fran[çc]ais\s*\.?",
    r"termes? propres? au contexte\b[^.]*\.?",   # capture aussi l'écho des termes
    r"le mot de l['’]?uk\s*\.?",
    r"sous[- ]titres?\s+r[ée]alis[ée]s?\s+par[^.]*\.?",
    r"sous[- ]titres?\s*:[^.]*\.?",
    r"merci d['’]avoir regard[ée][^.]*\.?",
    r"merci d['’]avoir suivi[^.]*\.?",
    r"sous[- ]titrage[^.]*\.?",
]
_METATEXT_RE = [re.compile(p, re.IGNORECASE) for p in METATEXT_HALLUCINATIONS]


def _norm_word(w: str) -> str:
    """Mot normalisé pour comparaison (minuscule, sans ponctuation/accents nus)."""
    return re.sub(r"[^\w]", "", (w or "").lower())


def _ends_punct(w: str) -> bool:
    return (w or "").rstrip()[-1:] in ".!?…"


def strip_metatext(text: str):
    """ÉTAPE 1 — Retire les motifs de métatexte halluciné. Renvoie
    (texte_nettoyé, [fragments_retirés]) pour pouvoir loguer chaque suppression."""
    if not text:
        return text or "", []
    removed = []
    out = text
    for rx in _METATEXT_RE:
        def _cap(m):
            frag = m.group(0).strip()
            if frag:
                removed.append(frag)
            return " "
        out = rx.sub(_cap, out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out, removed


def _overlap_ngram(prev_words, new_words, lo=3, hi=8) -> int:
    """ÉTAPE 2 — Plus long n-gram (lo..hi mots) qui est à la fois SUFFIXE de
    prev_words et PRÉFIXE de new_words. Renvoie sa longueur (0 si aucun)."""
    if not prev_words or not new_words:
        return 0
    maxk = min(hi, len(prev_words), len(new_words))
    for k in range(maxk, lo - 1, -1):
        if [_norm_word(x) for x in prev_words[-k:]] == \
           [_norm_word(x) for x in new_words[:k]]:
            return k
    return 0


def _heal_and_dedup(prev_words, new_words):
    """ÉTAPES 2+3 — À la jointure de deux fenêtres :
      1. SOIN DE TRONCATURE : si prev se termine par un fragment court (<=3) qui
         est le DÉBUT du 1er mot de new (ex. « en » -> « entre »), on retire le
         fragment tronqué (new fournit le mot complet).
      2. DÉDUP n-gram : si un n-gram (3..8 mots) est répété à la frontière, on
         le coupe du début de new.
    Renvoie (prev_words ajusté, new_words ajusté)."""
    if prev_words and new_words:
        a = _norm_word(prev_words[-1])
        b = _norm_word(new_words[0])
        if a and b and a != b and len(a) <= 3 and b.startswith(a):
            prev_words = prev_words[:-1]
    k = _overlap_ngram(prev_words, new_words, 3, 8)
    if k:
        return prev_words, new_words[k:]
    # Cas tenace : un fragment terminal COURT de prev (troncature résiduelle,
    # ex. « ...de 3 », « ...constamment en ») casse l'alignement du n-gram. On
    # retente la dédup en ignorant ce fragment ; s'il y a recouvrement, on JETTE
    # le fragment tronqué (la fenêtre suivante, ré-entendue via l'overlap, fournit
    # la version complète). C'est ce qui répare proprement les Cas 1 et 4.
    if len(prev_words) >= 4 and len(_norm_word(prev_words[-1])) <= 3 \
            and not _ends_punct(prev_words[-1]):
        k2 = _overlap_ngram(prev_words[:-1], new_words, 3, 8)
        if k2:
            return prev_words[:-1], new_words[k2:]
    return prev_words, new_words


def merge_window_texts(texts):
    """Fusionne les textes de fenêtres/segments consécutifs en éliminant
    métatexte (étape 1), troncatures + duplications de jointure (étapes 2/3).
    Renvoie (texte_fusionné, [métatexte_retiré]). Marque par « … » une fin
    visiblement tronquée (fragment court final, ou dernière fenêtre = pur
    métatexte) — honnêteté : l'utilisateur voit la coupure plutôt qu'un texte
    halluciné ou silencieusement perdu."""
    out, removed, last_meta = [], [], False
    for raw in texts:
        clean, rem = strip_metatext(raw)
        if rem:
            removed += rem
        clean = clean.strip()
        if not clean:
            if rem:
                last_meta = True           # cette fenêtre était du pur métatexte
            continue
        last_meta = False
        nw = clean.split()
        if out:
            out, nw = _heal_and_dedup(out, nw)
        if not nw:
            continue
        out.extend(nw)
    text = " ".join(out)
    if out and not _ends_punct(out[-1]) and (last_meta or len(_norm_word(out[-1])) <= 2):
        text = text + "…"
    return text, removed


class LiveMeetingTranscriber:
    """Transcrit un WAV en croissance, par fenêtres, en arrière-plan."""

    def __init__(self, engine, wav_path: str, sample_rate: int = SAMPLE_RATE,
                 diarizer=None, window_target_s: float = WINDOW_TARGET_S,
                 window_max_s: float = WINDOW_MAX_S):
        self.engine = engine
        self.wav_path = wav_path
        self.sample_rate = sample_rate
        # v1.0.12 — fenêtres paramétrables (défaut = constantes engine -> présentiel
        # STRICTEMENT inchangé). La visio passe des fenêtres plus courtes -> moins
        # de scratch MLX par inférence -> pic RAM ↓, SANS changer de modèle/qualité.
        self._wt = float(window_target_s)
        self._wm = float(window_max_s)
        # WhoTalks v1.1 — diariseur alimenté EN LIGNE : à chaque fenêtre on lui
        # passe le PCM pour extraire les empreintes PENDANT la réunion. À l'arrêt
        # il ne reste que le clustering (~0,1 s) -> zéro ralentissement.
        self.diarizer = diarizer
        self._pcm_cursor = 0                   # curseur en octets PCM purs
        self._segments = []                     # segments cumulés (avec offset)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._failed = False

    # ------------------------------------------------------------------ #
    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def failed(self) -> bool:
        return self._failed

    def _file_pcm_size(self) -> int:
        try:
            return max(0, os.path.getsize(self.wav_path) - WAV_HEADER_BYTES)
        except Exception:
            return 0

    def _read_pcm(self, start_byte: int, end_byte: int) -> np.ndarray:
        """Lit le PCM int16 entre deux offsets et renvoie du float32 [-1,1]."""
        try:
            with open(self.wav_path, "rb") as f:
                f.seek(WAV_HEADER_BYTES + start_byte)
                raw = f.read(end_byte - start_byte)
            if not raw:
                return np.zeros(0, dtype=np.float32)
            # tronque à un nombre pair d'octets (frames entières)
            if len(raw) % BYTES_PER_FRAME:
                raw = raw[:-(len(raw) % BYTES_PER_FRAME)]
            pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            return pcm
        except Exception:
            return np.zeros(0, dtype=np.float32)

    def _find_cut(self, audio: np.ndarray):
        """Cherche un point de coupe sur un SILENCE dans la dernière partie de la
        fenêtre, pour ne JAMAIS couper un mot en deux. Renvoie l'index (en
        échantillons) où couper, ou None si aucun silence trouvé (on attendra
        alors plus d'audio plutôt que de tronquer un mot)."""
        n = len(audio)
        frame = int(0.1 * self.sample_rate)           # fenêtres de 100 ms
        # Garde DÉFENSIVE inatteignable aujourd'hui : _find_cut n'est appelé
        # qu'après le test avail_s >= WINDOW_TARGET_S, donc n >= 25 s et
        # n - 8 s >= 17 s > 12,5 s — le plancher du max() ne gagne jamais. On
        # le conserve comme filet : si un futur appelant passait un audio court,
        # search_from resterait positif au lieu de balayer jusqu'à ~0.
        search_from = max(int(self._wt * self.sample_rate * 0.5),
                          n - int(SILENCE_SEARCH_S * self.sample_rate))
        i = n - frame
        while i > search_from:
            seg = audio[i:i + frame]
            if seg.size and float(np.sqrt(np.mean(seg * seg))) < SILENCE_RMS:
                return i + frame      # couper juste après le silence
            i -= frame
        return None

    # ------------------------------------------------------------------ #
    def _process_window(self, force_tail: bool = False):
        """Traite une fenêtre si assez d'audio est dispo (ou tout le reste si
        force_tail)."""
        pcm_size = self._file_pcm_size()
        # Curseur en octets PCM purs : octets de données déjà consommés (aucune
        # conversion ±WAV_HEADER_BYTES, seul _read_pcm gère l'en-tête au seek).
        cur_pcm = self._pcm_cursor
        avail = pcm_size - cur_pcm
        avail_s = avail / (self.sample_rate * BYTES_PER_FRAME)
        if not force_tail and avail_s < self._wt:
            return False
        if avail <= 0:
            return False
        # Lit la fenêtre disponible, TOUJOURS bornée à WINDOW_MAX_S : même en
        # finalisation d'une réunion de 2 h où la transcription aurait pris du
        # retard, on ne charge jamais plus de 40 s d'audio en RAM. La boucle de
        # finalize() rappelle cette fonction jusqu'à épuisement.
        max_bytes = int(self._wm * self.sample_rate * BYTES_PER_FRAME)
        win_bytes = min(avail, max_bytes)
        audio = self._read_pcm(cur_pcm, cur_pcm + win_bytes)
        if audio.size == 0:
            return False
        # Coupe : sur tail final OU fenêtre pleine (WINDOW_MAX_S) -> coupe
        # franche acceptée. Sinon on EXIGE un silence : si on n'en trouve pas,
        # on n'avance pas (return False) et on attendra plus d'audio -> aucun
        # mot tronqué en fin de fenêtre (corrige « pipe », « comt »...).
        window_full = win_bytes >= max_bytes
        if force_tail or window_full:
            cut = len(audio)
        else:
            cut = self._find_cut(audio)
            if cut is None:
                return False          # pas de silence : on patiente
            cut = min(cut, len(audio))
        window = audio[:cut]
        offset_s = cur_pcm / (self.sample_rate * BYTES_PER_FRAME)
        try:
            res = self.engine.transcribe_window(window, time_offset=offset_s,
                                                mode="reunion")
            with self._lock:
                self._segments.extend(res.get("segments", []))
        except Exception as e:
            print(f"[live] fenêtre KO : {e}")
            self._failed = True
        # WhoTalks v1.1 — empreintes vocales EN LIGNE sur la même fenêtre (le
        # dédoublonnage d'overlap est interne). Jamais bloquant.
        # v27 — PLAFONNÉ à 40 min d'audio : l'extraction tuile est IN-PROCESS et
        # son arène ONNX fuit (~3 Go mesurés à 34 min) ; au-delà, la diarisation
        # GUIDÉE en sous-process (voie primaire au stop) couvre de toute façon —
        # les tuiles ne sont qu'un repli, partiel passé 40 min plutôt que de
        # mettre un M1 8 Go à genoux pendant l'enregistrement.
        if self.diarizer is not None and offset_s < 2400.0:
            try:
                self.diarizer.feed_window(window, self.sample_rate, offset_s)
            except Exception as e:
                print(f"[whotalks] feed live KO (ignoré) : {e}")
        # Avance le curseur (en octets). v17 — OVERLAP : on recule de OVERLAP_S
        # pour que la fenêtre suivante ré-inclue la fin de celle-ci (contexte
        # sonore + mot coupé ré-entendu en entier). PAS d'overlap sur la coupure
        # finale (force_tail) ni quand la fenêtre est pleine sans silence
        # (window_full) — on ne veut pas reculer sur un point déjà « franc », et
        # ça garde la latence à l'arrêt inchangée. Garde-fou : on n'applique
        # l'overlap que si cut est assez grand pour TOUJOURS progresser.
        advance = cut
        if not force_tail and not window_full:
            ov = int(OVERLAP_S * self.sample_rate)
            if cut > 3 * ov:
                advance = cut - ov
        self._pcm_cursor += advance * BYTES_PER_FRAME
        return True

    def _loop(self):
        while not self._stop.is_set():
            # v20 — après un échec avéré, le résultat live sera JETÉ par le
            # pipeline (repli transcribe_detailed) : continuer à transcrire
            # gaspillerait des heures de CPU turbo sur une longue réunion.
            if self._failed:
                print("[live] échec marqué : boucle arrêtée (le pipeline "
                      "retombera sur la transcription complète).")
                return
            try:
                worked = self._process_window(force_tail=False)
            except Exception as e:
                print(f"[live] boucle KO : {e}")
                self._failed = True
                worked = False
            # rythme : si on a travaillé, on enchaîne ; sinon on patiente
            self._stop.wait(0.5 if worked else 2.0)

    # ------------------------------------------------------------------ #
    def finalize(self) -> dict:
        """Arrête la boucle et transcrit la fenêtre finale restante.
        Renvoie {text, segments, avg_conf}. Trie les segments par horodatage."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        # Traite tout le reliquat (en plusieurs passes si > WINDOW_MAX_S)
        # v29.2 — bornes ANTI-BOUCLE : garde-temps (jamais > 20 s ici) ET arrêt si
        # le curseur n'avance plus (sinon _process_window pouvait répéter la même
        # fenêtre jusqu'à guard=1000 -> finalisation qui « tourne dans le vide »).
        import time as _t
        _fin_t0 = _t.time()
        guard = 0
        while guard < 1000:
            guard += 1
            if _t.time() - _fin_t0 > 20.0:
                print("[live] finalisation : garde-temps 20 s atteint -> stop.")
                break
            cur_pcm = self._pcm_cursor
            avail = self._file_pcm_size() - cur_pcm
            if avail <= self.sample_rate * BYTES_PER_FRAME * 0.3:   # < 0.3 s
                break
            if not self._process_window(force_tail=True):
                break
            if self._pcm_cursor <= cur_pcm:       # aucun progrès -> on arrête
                print("[live] finalisation : curseur bloqué -> stop (anti-boucle).")
                break
        with self._lock:
            segs = sorted(self._segments, key=lambda s: s.get("start", 0.0))
        from engine import strip_hallucinations, assemble_segments
        # v17 — On écarte d'abord les segments qui ne sont QUE du métatexte (pour
        # la confiance ET la liste de segments affichée), puis on fusionne les
        # textes avec dédup de jointure + soin de troncature (merge_window_texts).
        clean_segs = [s for s in segs if strip_metatext(s.get("text", ""))[0].strip()]
        merged, removed = merge_window_texts([s.get("text", "") for s in clean_segs])
        for frag in removed:
            print(f"[live] métatexte retiré : {frag!r}")
        text = strip_hallucinations(assemble_segments([merged]))
        probs = [w["prob"] for s in clean_segs for w in s.get("words", [])]
        avg = round(sum(probs) / len(probs), 3) if probs else 1.0
        return {"text": text, "segments": clean_segs, "avg_conf": avg}
