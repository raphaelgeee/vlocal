#!/usr/bin/env python3
"""
Vlocal : live-tail de dictée.

Pendant une dictée longue, les fenêtres déjà parlées sont transcrites PENDANT
que l'utilisateur parle, coupées uniquement à de vrais silences (mêmes seuils
que le moteur). Au relâchement, seul le reliquat reste à transcrire : la
latence de finalisation devient quasi constante au lieu de proportionnelle à
la durée dictée.

Qualité : validé sur corpus, textes normalisés strictement identiques au
single-pass (coupe aux silences + condition_on_previous_text=False, le
décodage est indépendant par fenêtre). Sans silence franc, on ne coupe pas.

Historique
  v21      : première version (fenêtres de 30 s).
  v22.5    : armement dès 29 s d'audio (zone morte 29-40 s corrigée).
  v1.0.22  : snapshot atomique (textes, position), join 3 s après fermeture du micro.
  v1.1.0   : la recherche de silence n'est plus bornée à 16 s après la cible.
             Elle s'étend à tout l'audio disponible et est incrémentale (chaque
             échantillon n'est analysé qu'une fois). Avant, une phrase sans
             pause de 0,4 s entre 24 et 40 s de fenêtre bloquait le live-tail
             pour tout le reste de la dictée : un reliquat de plusieurs minutes
             dépassait alors le timeout GPU, déclaré à tort « gel Metal ».
"""
import threading

from engine import SAMPLE_RATE, SILENCE_RMS


class DictationLiveTail:
    """Transcription anticipée des fenêtres complètes d'une dictée en cours.

    Le thread interne scrute l'audio capturé ; dès qu'une fenêtre d'au moins
    TARGET_S se termine sur un vrai silence (>= 0,4 s sous SILENCE_RMS), il la
    transcrit avec le moteur (mêmes kwargs que le single-pass) et publie un
    snapshot atomique (textes, position). L'appelant récupère ce snapshot via
    stop_and_collect() puis ne transcrit que le reliquat.
    """

    TARGET_S = 24.0     # taille de fenêtre visée (coupe au premier vrai silence après)
    ARM_S = 29.0        # ne jamais s'armer avant (29 s = branche turbo certaine)
    STEP_S = 0.1        # pas d'analyse RMS
    NEED_STEPS = 4      # 4 x 100 ms = 0,4 s de silence continu (comme le VAD du moteur)

    def __init__(self, engine, autostart: bool = True):
        self.eng = engine
        # Snapshot (textes, position) publié en une seule affectation par le
        # thread : stop_and_collect le lit en une fois (pas d'entrelacement
        # possible entre la liste et la position).
        self._snap = ([], 0)
        self.texts = []
        self.pos = 0            # échantillons déjà transcrits
        self._scanned = 0       # échantillons déjà analysés sans silence trouvé
        self._stop = threading.Event()
        self._thr = None
        if autostart:
            self._thr = threading.Thread(target=self._loop, daemon=True,
                                         name="dictation-livetail")
            self._thr.start()

    def _find_cut(self, n_avail: int, sr: int):
        """Cherche un vrai silence dans [pos + TARGET_S, n_avail).

        Renvoie l'indice de coupe (milieu du premier creux de NEED_STEPS pas
        consécutifs sous SILENCE_RMS) ou None. La recherche est incrémentale :
        on repart de _scanned (avec un recouvrement de NEED_STEPS - 1 pas pour
        ne pas rater un creux à cheval sur deux passages), donc le coût par
        appel reste borné par l'audio nouvellement capturé.
        """
        import numpy as np
        step = int(self.STEP_S * sr)
        need = self.NEED_STEPS
        lo = self.pos + int(self.TARGET_S * sr)
        start = max(lo, self._scanned - (need - 1) * step)
        if n_avail - start < need * step:
            return None
        seg = self.eng.snapshot_audio(start, n_avail)
        n_frames = len(seg) // step
        if n_frames < need:
            return None
        frames = seg[:n_frames * step].reshape(n_frames, step)
        quiet = np.sqrt(np.mean(frames * frames, axis=1)) < SILENCE_RMS
        run = 0
        for i, q in enumerate(quiet):
            if not q:
                run = 0
                continue
            run += 1
            if run >= need:
                first = i - (need - 1)
                return start + first * step + (need * step) // 2
        self._scanned = start + n_frames * step
        return None

    def _loop(self):
        sr = SAMPLE_RATE
        while not self._stop.is_set():
            self._stop.wait(0.5)
            if self._stop.is_set():
                return
            try:
                n = self.eng.recorded_samples()
                # Armement : ARM_S d'audio non transcrit, plus 1 s de marge
                # pour ne jamais analyser la toute fin (encore en cours).
                if n - self.pos < int(self.ARM_S * sr) + sr:
                    continue
                cut = self._find_cut(n - sr, sr)
                if cut is None:
                    continue            # pas de silence franc : on patiente (qualité)
                window = self.eng.snapshot_audio(self.pos, cut)
                if self._stop.is_set() or len(window) == 0:
                    return
                txt = self.eng.transcribe(window)   # mêmes kwargs que le single-pass
                self.texts.append(txt)
                self.pos = cut
                self._scanned = 0
                self._snap = (list(self.texts), cut)
                print(f"[live-tail] fenêtre {len(self.texts)} transcrite pendant la "
                      f"dictée ({len(window) / sr:.0f}s).")
            except Exception as e:
                print(f"[live-tail] arrêt (sans gravité, repli single-pass) : {e}")
                self.texts = []
                self.pos = 0
                self._snap = ([], 0)
                return

    def stop_and_collect(self):
        """Arrête la boucle et renvoie (textes, position du reliquat), ([], 0) si rien.

        L'appelant a déjà fermé le micro : ce join de 3 s ne retient plus la
        capture. Abandonner une fenêtre en vol est sans perte, `pos` n'ayant
        pas avancé, l'audio correspondant est retranscrit dans le reliquat.
        """
        self._stop.set()
        if self._thr is not None:
            self._thr.join(timeout=3.0)
        texts, pos = self._snap
        return list(texts), pos

    def abort(self):
        self._stop.set()
