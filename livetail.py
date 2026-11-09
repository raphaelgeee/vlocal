#!/usr/bin/env python3
"""
Vlocal : découpeur de dictée. Le live-tail et l'anticipation ne font qu'un (v1.7).

Pendant que la touche est tenue, l'audio capturé est lu par fenêtres de 30 ms
(comme la porte de silence du moteur) et classé voix / calme (SILENCE_RMS).
Dès qu'un calme dure LAUNCH_PAUSE_S, tout ce qui a été dit depuis la dernière
coupe part en transcription de FOND : priorité basse sur le worker GPU,
interruptible à tout instant (mlx_engine.Cancel). La coupe tombe dans le
calme, LAUNCH_PAUSE_S / 2 après la fin du dernier mot : jamais dans un mot.

Ce calcul sert de deux façons :
  - texte FINAL si la touche est relâchée sans que la voix revienne : il est
    déjà prêt (ou le sera à la fin du calcul en cours), rien d'autre à faire ;
  - morceau VALIDÉ si la voix revient après une vraie pause (COMMIT_PAUSE_S)
    et que le morceau contient au moins MIN_COMMIT_SPEECH_S de parole. Sinon
    le calcul est jeté (interrompu s'il tourne encore : il ne coûte plus rien)
    et le morceau sera repris, plus long, à la pause suivante. Whisper
    ponctue mal quelques mots isolés : les « 4 mots après la pause » restent
    rattachés au morceau suivant ou à la fin.
Au relâchement, seule la fin non validée reste à transcrire. v1.5 — « rien
n'a été dit depuis » se juge avec la porte de silence du moteur (_is_silent),
plus sensible que SILENCE_RMS : un dernier mot faible (sous -38 dBFS) après
une pause n'est jamais jeté, il est transcrit avec la fin.

Une dictée finale n'attend jamais un calcul spéculatif : la mise en file d'une
dictée interrompt le travail de fond en cours (mlx_engine), et le relâchement
interrompt tout calcul qui ne rend pas le texte final.

Garde-fous :
  - aucune transcription avant SPEC_MIN_SPEECH_S de parole (dictée < 2 s :
    rien), et aucun morceau sans voix (anti-hallucination) ;
  - un seul calcul à la fois ;
  - le travail SPÉCULATIF (qui peut être jeté) n'a lieu que sur GPU, hors
    pression mémoire. Sinon, seul reste le découpage historique en fenêtres
    longues (TARGET_S, ARM_S), dont le résultat n'est jamais jeté : sur CPU,
    une transcription coûte plusieurs secondes, seules les longues fenêtres
    la remboursent ;
  - un échec GPU arrête l'anticipation pour la dictée : la fin est
    transcrite à la finalisation (aucun repli CPU de plus).

Qualité : morceaux coupés dans le calme, mêmes réglages que la dictée, et
chaque morceau reçoit en contexte la fin du texte déjà validé
(engine.dictation_context) : Whisper poursuit la phrase, la langue et le
style. Réglages mesurés sur un corpus de dictées avec pauses
(docs/ARCHITECTURE.md).

Historique
  v21      : live-tail, fenêtres de 30 s.
  v1.1.0   : recherche de silence non bornée et incrémentale.
  v1.7     : découpeur unique (pauses, spéculatif interruptible, relâchement).
"""
import threading

import numpy as np

import memory_policy
import mlx_engine
from engine import (DICTATION_CONTEXT_CHARS, SAMPLE_RATE, SILENCE_RMS, _is_silent,
                    dictation_context)


class _VoiceTrack:
    """Voix / calme par fenêtres de WIN_S de l'audio capturé.

    Chaque échantillon n'est lu qu'une fois (feed incrémental). Requêtes en
    indices de fenêtres : parole cumulée, reprise de voix, pause."""

    WIN_S = 0.03

    def __init__(self, sr: int = SAMPLE_RATE):
        self.win = int(self.WIN_S * sr)
        self._voiced = np.zeros(4096, dtype=bool)
        self._cum = np.zeros(4097, dtype=np.int64)   # _cum[i] = voix dans [0, i)
        self.n = 0              # fenêtres analysées
        self._rest = np.zeros(0, dtype=np.float32)

    @property
    def samples(self) -> int:
        """Échantillons lus (fenêtres complètes et reste en attente)."""
        return self.n * self.win + len(self._rest)

    def feed(self, audio) -> None:
        a = np.asarray(audio, dtype=np.float32).reshape(-1)
        if len(self._rest):
            a = np.concatenate([self._rest, a])
        k = len(a) // self.win
        self._rest = a[k * self.win:].copy()
        if k == 0:
            return
        frames = a[:k * self.win].reshape(k, self.win)
        voiced = np.sqrt(np.einsum("ij,ij->i", frames, frames) / self.win) >= SILENCE_RMS
        need = self.n + k
        if need > len(self._voiced):
            cap = max(need, 2 * len(self._voiced))
            self._voiced = np.resize(self._voiced, cap)
            self._cum = np.resize(self._cum, cap + 1)
        self._voiced[self.n:need] = voiced
        self._cum[self.n + 1:need + 1] = self._cum[self.n] + np.cumsum(voiced)
        self.n = need

    def speech_s(self, a: int, b: int) -> float:
        """Secondes de voix dans les fenêtres [a, b)."""
        b = min(b, self.n)
        return float(self._cum[b] - self._cum[a]) * self.WIN_S if b > a else 0.0

    def voice_start(self, a: int, min_w: int):
        """Début de la première suite d'au moins min_w fenêtres voisées à partir
        de a (un clic ou un souffle isolé ne compte pas), sinon None."""
        v = self._voiced[a:self.n]
        if len(v) < min_w:
            return None
        run = np.convolve(v.astype(np.int8), np.ones(min_w, dtype=np.int8), "valid")
        hits = np.flatnonzero(run == min_w)
        return a + int(hits[0]) if len(hits) else None

    def pause_from(self, a: int, min_w: int):
        """Première pause (suite de fenêtres calmes) d'au moins min_w fenêtres
        commençant en a ou après. Renvoie (début, fin) ; fin vaut None si la
        pause dure encore. None si aucune."""
        quiet = ~self._voiced[a:self.n]
        if len(quiet) < min_w:
            return None
        edges = np.diff(np.concatenate(([0], quiet.astype(np.int8), [0])))
        starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
        for s, e in zip(starts, ends):
            if e - s >= min_w:
                return a + int(s), (a + int(e) if e < len(quiet) else None)
        return None


class _Job:
    """Un calcul de fond : le morceau [start, cut), en fenêtres."""

    __slots__ = ("start", "cut", "pause_start", "spec", "cancel", "done",
                 "text", "error")

    def __init__(self, start, cut, pause_start, spec):
        self.start, self.cut, self.pause_start, self.spec = start, cut, pause_start, spec
        self.cancel = mlx_engine.Cancel()
        self.done = threading.Event()
        self.text = None        # texte rendu (str)
        self.error = None       # exception (Cancelled comprise)

    def ok(self) -> bool:
        return self.done.is_set() and self.error is None and self.text is not None


class DictationLiveTail:
    """Découpeur d'une dictée en cours (un objet par appui).

    Le thread interne lit l'audio capturé (eng.recorded_samples /
    eng.snapshot_audio), lance au plus un calcul de fond à la fois
    (eng.transcribe_chunk) et valide les morceaux. L'appelant récupère par
    stop_and_collect(audio) les textes validés et la position du reliquat.
    """

    STEP_S = 0.1                # pas de lecture de l'audio capturé
    LAUNCH_PAUSE_S = 0.4        # calme qui lance un calcul (coupe à sa moitié)
    COMMIT_PAUSE_S = 0.7        # vraie pause qui peut fermer un morceau
    MIN_COMMIT_SPEECH_S = 4.0   # parole minimale d'un morceau validé
    SPEC_MIN_SPEECH_S = 2.0     # parole minimale avant tout calcul
    VOICE_MIN_S = 0.15          # reprise de voix : 150 ms voisées d'affilée
    TARGET_S = 24.0             # morceau long : la pause de lancement suffit
    ARM_S = 29.0                # sans spéculatif : jamais avant (historique)
    JOIN_S = 3.0                # attente maximale d'un calcul utile au relâchement
    CONTEXT_CHARS = DICTATION_CONTEXT_CHARS   # contexte : fin du texte validé

    def __init__(self, engine, autostart: bool = True):
        self.eng = engine
        self._track = _VoiceTrack(SAMPLE_RATE)
        self._win = self._track.win
        self._lock = threading.Lock()
        self._texts = []
        self._pos = 0               # fenêtre de début de la partie non validée
        self._search = 0            # pause candidate suivante : à partir d'ici
        self._job = None
        self._closed = False
        self._broken = False        # échec : plus d'anticipation pour cette dictée
        self.stats = {"lances": 0, "valides": 0, "jetes": 0, "final": 0}
        self._stop = threading.Event()
        self._thr = None
        if autostart:
            self._thr = threading.Thread(target=self._loop, daemon=True,
                                         name="dictation-livetail")
            self._thr.start()

    # ------------------------------------------------------------------ #
    # Politique
    @staticmethod
    def _w(seconds: float) -> int:
        return max(1, int(round(seconds / _VoiceTrack.WIN_S)))

    def _speculation_allowed(self) -> bool:
        """Travail qui peut être jeté : GPU disponible et mémoire à l'aise."""
        try:
            return (mlx_engine.available()
                    and memory_policy.pressure_level() == memory_policy.NORMAL)
        except Exception as e:
            print(f"[découpeur] état GPU/mémoire illisible ({e}) : pas de spéculatif.")
            return False

    def _committable(self, job, resume: int) -> bool:
        """La voix revient en `resume` : le morceau de `job` devient-il définitif ?
        Un morceau long (TARGET_S) se ferme à la pause de lancement, comme le
        live-tail historique ; sinon il faut une vraie pause et assez de parole."""
        long_chunk = (job.cut - job.start) * _VoiceTrack.WIN_S >= self.TARGET_S
        need = self._w(self.LAUNCH_PAUSE_S if long_chunk else self.COMMIT_PAUSE_S)
        return (resume - job.pause_start >= need
                and self._track.speech_s(job.start, job.pause_start)
                >= self.MIN_COMMIT_SPEECH_S)

    def _next_job(self, speculative: bool):
        """Morceau à lancer maintenant, ou None. Une pause en cours lance un
        calcul spéculatif ; une pause déjà finie (voix revenue entre deux
        lectures) n'en lance un que s'il sera validé."""
        t = self._track
        lw = self._w(self.LAUNCH_PAUSE_S)
        while True:
            p = t.pause_from(self._search, lw)
            if p is None:
                return None
            ps, pe = p
            job = _Job(self._pos, ps + lw // 2, ps, spec=speculative)
            if speculative:
                ok = t.speech_s(self._pos, ps) >= self.SPEC_MIN_SPEECH_S
                if ok and pe is not None:
                    resume = t.voice_start(pe, self._w(self.VOICE_MIN_S))
                    ok = resume is not None and self._committable(job, resume)
            else:
                # sans spéculatif : fenêtres longues seulement, toujours validées
                ok = ((ps - self._pos) * _VoiceTrack.WIN_S >= self.TARGET_S
                      and (t.n - self._pos) * _VoiceTrack.WIN_S >= self.ARM_S + 1.0)
            if ok:
                return job
            if pe is None:
                return None         # pause en cours, pas encore utile : on attend
            self._search = pe

    def context(self):
        """Fin du texte validé, passée en contexte au morceau suivant."""
        return dictation_context(self._texts, self.CONTEXT_CHARS)

    # ------------------------------------------------------------------ #
    # Actions (verrou tenu)
    def _launch(self, job) -> None:
        audio = self.eng.snapshot_audio(job.start * self._win, job.cut * self._win)
        context = self.context()
        self._job = job
        self.stats["lances"] += 1

        def run():
            try:
                job.text = self.eng.transcribe_chunk(audio, cancel=job.cancel,
                                                     context=context)
            except BaseException as e:      # Cancelled ou échec GPU : sans gravité
                job.error = e
            finally:
                job.done.set()
        threading.Thread(target=run, daemon=True, name="dictation-chunk").start()

    def _commit(self, job) -> None:
        self._texts.append(job.text)
        self._pos = self._search = job.cut
        self._job = None
        self.stats["valides"] += 1

    def _drop(self, job, search_from: int) -> None:
        job.cancel.set()
        self._job = None
        self._search = max(self._search, search_from)
        self.stats["jetes"] += 1

    def _tick(self, speculative: bool) -> None:
        """Un pas de décision du thread du découpeur (verrou tenu).
        `speculative` : _speculation_allowed(), lu hors verrou."""
        job = self._job
        if job is not None:
            if job.done.is_set() and job.error is not None:
                self._job = None
                if isinstance(job.error, mlx_engine.Cancelled):
                    return      # interrompu par une dictée : la pause sera reprise
                self._broken = True
                print(f"[découpeur] anticipation arrêtée pour cette dictée "
                      f"(sans gravité) : {type(job.error).__name__}: {job.error}")
                return
            resume = self._track.voice_start(job.cut, self._w(self.VOICE_MIN_S))
            if resume is None:
                return          # toujours en pause : le calcul peut devenir final
            if not self._committable(job, resume):
                self._drop(job, resume)
            elif job.done.is_set():
                if job.text:
                    self._commit(job)
                else:
                    self._drop(job, resume)     # rien d'audible : repris plus tard
            return
        if not self._broken:
            nxt = self._next_job(speculative)
            if nxt is not None:
                self._launch(nxt)

    def _feed_until(self, n: int, audio=None) -> None:
        have = self._track.samples
        if n > have:
            self._track.feed(audio[have:n] if audio is not None
                             else self.eng.snapshot_audio(have, n))

    def _loop(self):
        while not self._stop.wait(self.STEP_S):
            try:
                n = self.eng.recorded_samples()
                # Hors verrou : la sonde GPU peut attendre (1re de la session),
                # le relâchement ne doit jamais attendre derrière elle.
                spec = self._job is None and self._speculation_allowed()
                with self._lock:
                    if self._closed:
                        return
                    self._feed_until(n)
                    self._tick(spec)
            except Exception as e:
                print(f"[découpeur] arrêt (sans gravité, finalisation complète) : {e}")
                with self._lock:
                    self._broken = True
                    if self._job is not None:
                        self._job.cancel.set()
                return

    # ------------------------------------------------------------------ #
    def stop_and_collect(self, audio=None):
        """Relâchement : arrête le découpeur, renvoie (textes, position).

        `audio` : PCM complet de la dictée (micro fermé). La position est en
        échantillons ; elle vaut len(audio) quand aucune voix n'a suivi le
        dernier morceau (rien à transcrire). ([], 0) si rien d'anticipé.
        Un calcul en vol n'est attendu (au plus JOIN_S) que s'il rend le texte
        final ou s'il ne peut pas être interrompu ; sinon il est interrompu.
        Après cet appel, aucun résultat tardif ne modifie plus rien : la
        livraison reste tranchée par le seul claim() de la finalisation."""
        self._stop.set()
        if self._thr is not None:
            self._thr.join(timeout=1.0)
        vmin = self._w(self.VOICE_MIN_S)
        with self._lock:
            self._closed = True
            if audio is not None:
                if self._track.samples > len(audio):
                    return [], 0    # audio incohérent : finalisation complète
                self._feed_until(len(audio), audio)
            job, wait, final = self._job, None, False
            if job is not None and not (job.done.is_set() and job.error is not None):
                resume = self._track.voice_start(job.cut, vmin)
                if resume is None:
                    # Calme selon SILENCE_RMS : il rend le texte final, sauf si
                    # un mot faible suit (alors morceau validé, fin transcrite).
                    wait, final = job, not self._audible_after(audio, job.cut)
                elif not self._committable(job, resume):
                    self._drop(job, resume)
                elif job.ok() and job.text:
                    self._commit(job)
                elif job.spec:
                    self._drop(job, resume)         # interrompre coûte moins qu'attendre
                else:
                    wait = job                      # fenêtre longue, non interruptible
            texts, pos = list(self._texts), self._pos
            n_total = self._track.n
        if wait is not None:
            if not wait.done.wait(self.JOIN_S):
                wait.cancel.set()
            elif wait.ok() and (final or wait.text):
                texts.append(wait.text)
                if final:
                    self.stats["final"] += 1
                    return texts, (len(audio) if audio is not None
                                   else wait.cut * self._win)
                self.stats["valides"] += 1
                pos = wait.cut
            # sinon calcul perdu : la fin depuis `pos` sera transcrite
        if not texts:
            return [], 0
        if (audio is not None and self._track.voice_start(pos, vmin) is None
                and not self._audible_after(audio, pos)):
            return texts, len(audio)    # rien n'a été dit depuis le dernier morceau
        return texts, min(pos * self._win, n_total * self._win)

    def _audible_after(self, audio, w: int) -> bool:
        """Le PCM après la fenêtre `w` passe-t-il la porte de silence du moteur ?
        Sans PCM (appelant sans audio), on s'en tient à SILENCE_RMS."""
        return audio is not None and not _is_silent(audio[w * self._win:])

    def abort(self):
        """Dictée annulée ou réinitialisée : tout s'arrête, rien n'est gardé."""
        self._stop.set()
        with self._lock:
            self._closed = True
            if self._job is not None:
                self._job.cancel.set()
