"""Correctifs dictée 1.5 : un dernier mot faible après une pause n'est jamais
perdu au relâchement ; le compteur des finalisations en vol ne dérive plus
après force_reset. Audio synthétique, moteur simulé, aucun modèle chargé."""
import os
import sys
import threading
import time
import types
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import errors  # noqa: E402
from livetail import DictationLiveTail  # noqa: E402
from test_incidents import _charge_depuis_app  # noqa: E402

SR = engine.SAMPLE_RATE


def _noise(seconds, rms, seed):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * rms).astype(np.float32)


class _WordEngine:
    """Moteur simulé qui « transcrit ce qu'on lui donne » : il retrouve le
    morceau reçu dans la capture et rend les mots qui s'y trouvent."""

    def __init__(self, parts):
        chunks, self.words, pos = [], [], 0
        for i, (word, seconds, rms) in enumerate(parts):
            a = _noise(seconds, rms, i)
            if word:
                self.words.append((pos, pos + len(a), word))
            chunks.append(a)
            pos += len(a)
        self.audio = np.concatenate(chunks)
        self.n = 0

    def recorded_samples(self):
        return self.n

    def snapshot_audio(self, a, b):
        return self.audio[a:min(b, self.n)].copy()

    def transcribe_chunk(self, x, cancel=None, context=None):
        return self.text_of(x)

    def text_of(self, x):
        if len(x) == 0:
            return ""
        hits = np.flatnonzero(self.audio == x[0])
        off = next(int(h) for h in hits
                   if np.array_equal(self.audio[h:h + len(x)], x))
        return " ".join(w for a, b, w in self.words if a < off + len(x) and b > off)


class DernierMotFaibleTests(unittest.TestCase):
    def _dicte(self, parts):
        eng = _WordEngine(parts)
        lt = DictationLiveTail(eng, autostart=False)
        lt._speculation_allowed = lambda: True
        step = int(lt.STEP_S * SR)
        while eng.n < len(eng.audio):
            eng.n = min(len(eng.audio), eng.n + step)
            with lt._lock:
                lt._feed_until(eng.n)
                lt._tick(True)
                job = lt._job
            if job is not None:
                job.done.wait(1.0)
        texts, pos = lt.stop_and_collect(eng.audio)
        if not texts:
            pos = 0
        reste = eng.audio[pos:]
        # Finalisation : le reliquat passe par la porte du moteur, puis le moteur.
        if len(reste) and not engine._is_silent(reste):
            texts = texts + [eng.text_of(reste)]
        return " ".join(t for t in texts if t)

    def test_deux_mots_a_moins_45_dbfs_apres_une_pause(self):
        faible = 10 ** (-45 / 20)        # -45 dBFS RMS
        texte = self._dicte([("phrase normale", 6.0, 0.1), (None, 1.0, 0.0025),
                             ("mot1", 0.35, faible), (None, 0.15, 0.0025),
                             ("mot2", 0.35, faible), (None, 0.3, 0.0025)])
        self.assertEqual(texte, "phrase normale mot1 mot2")

    def test_pause_finale_sans_voix_reste_finale(self):
        eng_parts = [("phrase normale", 6.0, 0.1), (None, 1.5, 0.0025)]
        self.assertEqual(self._dicte(eng_parts), "phrase normale")


class FinInflightTests(unittest.TestCase):
    def test_compteur_ne_derive_pas_apres_force_reset(self):
        release = {}
        ns = {"threading": threading, "time": time, "VlocalEngine": object, "_EV": errors,
              "log": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                           debug=lambda *a, **k: None),
              "_finalize_budget_s": lambda d: 60.0, "_end_activity": lambda tok: None,
              "_ui": lambda c: None, "overlay": None, "_te": lambda t: t,
              "_toast": lambda *a, **k: None}
        _charge_depuis_app({"_DictationStop", "_FinalizeWatch", "DictationController"}, ns)
        eng = types.SimpleNamespace(on_mic_connecting=None, reset_audio=lambda: True)
        c = ns["DictationController"](eng)

        def fake_finalize(self_, notify_ui, evt, worker, tail, ticket, epoch, watch):
            ev = threading.Event()
            release[ticket] = ev
            ev.wait(5)
            return ""

        c._do_finalize = types.MethodType(fake_finalize, c)

        def dictee():
            c._active, c._tail, c._t0 = True, None, time.time()
            t = threading.Thread(target=c.stop_and_finalize, daemon=True)
            t.start()
            return t

        a = dictee()
        while 0 not in release:
            time.sleep(0.01)
        c.force_reset("test")
        b = dictee()
        while 1 not in release:
            time.sleep(0.01)
        self.assertEqual(c._fin_inflight, 1)
        release[0].set()
        a.join(2)
        self.assertEqual(c._fin_inflight, 1, "la dictée B est toujours en vol")
        self.assertTrue(c._busy)
        release[1].set()
        b.join(2)
        self.assertEqual((c._fin_inflight, c._busy), (0, False))


if __name__ == "__main__":
    unittest.main()
