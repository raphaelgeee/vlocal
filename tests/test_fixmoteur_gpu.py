"""Correctifs moteur 1.5 : file GPU (travail abandonné sauté, délai compté depuis
le démarrage effectif, jamais deux threads MLX), blocs d'import de 60 s,
attentes bornées du moteur CPU sur le chemin de la dictée, politique mémoire
(hystérésis sous avertissement, GPU inutilisable déchargé).
Inférences simulées : aucun modèle chargé, aucune écriture hors du dossier de test."""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import memory_policy as mp  # noqa: E402
import mlx_engine  # noqa: E402


class _Probe:
    """Travaux simulés sur le worker : compte les exécutions simultanées."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.ran = []

    def job(self, name, dur):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(dur)
        with self.lock:
            self.running -= 1
        self.ran.append(name)
        return name


class GpuQueueTests(unittest.TestCase):
    def setUp(self):
        self.strikes = []
        p = mock.patch.object(mlx_engine, "_gpu_strike",
                              lambda reason, dictation=True: self.strikes.append(reason))
        p.start()
        self.addCleanup(p.stop)
        self.probe = _Probe()

    def _bg(self, name, dur):
        t = threading.Thread(target=lambda: mlx_engine._on_worker(
            self.probe.job, name, dur, _timeout=5))
        t.start()
        return t

    def test_dictee_abandonnee_jamais_executee(self):
        t = self._bg("import", 0.8)
        time.sleep(0.05)
        with self.assertRaises(TimeoutError):
            mlx_engine._on_worker(self.probe.job, "dictee", 0.01, _timeout=0.3,
                                  _prio=mlx_engine.PRIO_DICTATION)
        t.join()
        time.sleep(0.2)
        self.assertEqual(self.probe.ran, ["import"])
        self.assertEqual(self.strikes, [], "un embouteillage n'accuse pas le GPU")

    def test_delai_compte_depuis_le_demarrage(self):
        # 0,4 s d'attente de tour + 0,3 s de calcul > 0,5 s au total, mais le
        # calcul lui-même reste sous son délai : ni échec ni gel déclaré.
        t = self._bg("fond", 0.4)
        time.sleep(0.05)
        r = mlx_engine._on_worker(self.probe.job, "dictee", 0.3, _timeout=0.5,
                                  _prio=mlx_engine.PRIO_DICTATION)
        t.join()
        self.assertEqual(r, "dictee")
        self.assertEqual(self.strikes, [])

    def test_vrai_gel_toujours_detecte(self):
        with self.assertRaises(TimeoutError):
            mlx_engine._on_worker(self.probe.job, "gel", 0.6, _timeout=0.2,
                                  _prio=mlx_engine.PRIO_DICTATION)
        self.assertEqual(len(self.strikes), 1)
        time.sleep(0.6)
        self.assertEqual(mlx_engine._on_worker(self.probe.job, "apres", 0.0, _timeout=2),
                         "apres", "un worker neuf reprend après un gel")

    def test_renew_worker_jamais_deux_threads_mlx(self):
        ths = [self._bg(n, 0.4) for n in ("fond1", "fond2")]
        time.sleep(0.1)
        mlx_engine.renew_worker()
        mlx_engine._on_worker(self.probe.job, "dictee-retry", 0.2, _timeout=5,
                              _prio=mlx_engine.PRIO_DICTATION)
        for t in ths:
            t.join()
        self.assertEqual(self.probe.peak, 1)
        self.assertEqual(sorted(self.probe.ran), ["dictee-retry", "fond1", "fond2"])
        time.sleep(0.1)
        vivants = [t for t in threading.enumerate() if t.name == "mlx-gpu"]
        self.assertEqual(len(vivants), 1, "aucun worker orphelin")


class ImportChunkTests(unittest.TestCase):
    def test_blocs_d_import_de_60_s_au_plus(self):
        eng = object.__new__(engine.VlocalEngine)
        sr = engine.SAMPLE_RATE
        audio = (np.random.RandomState(0).randn(sr * 300) * 0.05).astype(np.float32)
        tailles = []

        def fake_detailed(a, offset, mode):
            tailles.append(len(a))
            return {"segments": [{"text": " mot", "start": offset, "end": offset + 1,
                                  "words": [{"word": " mot", "prob": 0.9}]}]}

        eng._mlx_detailed = fake_detailed
        with mock.patch.object(engine.mlx_engine, "available", lambda: True):
            r = eng.transcribe_import_mlx(audio, sr=sr, on_progress=None)
        self.assertEqual(set(r), {"text", "segments", "avg_conf"})
        self.assertLessEqual(max(tailles), 60 * sr)
        self.assertEqual(sum(tailles), len(audio), "aucun échantillon perdu ni doublé")
        self.assertGreaterEqual(len(tailles), 5)


def _fake_engine(tmp):
    e = object.__new__(engine.VlocalEngine)
    d = tempfile.mkdtemp(dir=tmp)
    open(os.path.join(d, "model.bin"), "w").close()
    e.model_dir, e._cpu_threads, e.model = d, 1, None
    e._infer_lock = threading.Lock()
    e._fast_model_lock = threading.Lock()
    e._load_lock = threading.Lock()
    e._outcome_tls = threading.local()
    e._outcome_lock = threading.Lock()
    e._last_producer = ""
    e.last_use = 0
    e._fast_last_use = 0
    e._batched = None
    e._fast_model = None
    e.DICTATION_LOCK_WAIT_S = 0.5
    return e


class _FakeWhisper:
    def __init__(self, *a, **k):
        pass

    def transcribe(self, audio, **kw):
        return iter(()), mock.Mock(duration_after_vad=0)


class InferLockTests(unittest.TestCase):
    """L'attente du moteur CPU est bornée partout sur le chemin de la dictée,
    et le modèle se charge sans le verrou d'inférence."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for p in (mock.patch.object(engine.mlx_engine, "available", lambda: False),
                  mock.patch("faster_whisper.WhisperModel", _FakeWhisper)):
            p.start()
            self.addCleanup(p.stop)
        self.audio = (np.random.RandomState(1).randn(16000 * 3) * 0.1).astype(np.float32)

    def _run(self, fn):
        box = {}

        def go():
            t0 = time.monotonic()
            try:
                fn()
                box["r"] = "rendu"
            except Exception as ex:
                box["r"] = type(ex).__name__
            box["s"] = time.monotonic() - t0

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(5)
        return box.get("r", "BLOQUÉ"), box.get("s")

    def test_fidele_turbo_decharge(self):
        e = _fake_engine(self.tmp)
        e._infer_lock.acquire()
        r, s = self._run(lambda: e.transcribe(self.audio))
        self.assertEqual(r, "TimeoutError")
        self.assertLess(s, 2.0)
        self.assertIsNotNone(e.model, "chargé hors du verrou d'inférence")

    def test_auto_cascade_small(self):
        e = _fake_engine(self.tmp)
        e._fast_model = _FakeWhisper()
        e._infer_lock.acquire()
        with mock.patch.object(e, "_ensure_fast_model", lambda: e._fast_model):
            r, s = self._run(lambda: e.transcribe_adaptive(self.audio))
        self.assertEqual(r, "TimeoutError")
        self.assertLess(s, 2.0, "une seule attente bornée, pas deux")


class MemoryPolicyTests(unittest.TestCase):
    def test_hysteresis_sous_avertissement(self):
        # Mac 8 Go sous avertissement persistant, une dictée toutes les 90 s.
        loads, gpu, idle = 0, True, 0.0
        for t in range(0, 900, 5):
            if t % 90 == 0:
                if not gpu:
                    loads += 1
                    gpu = True
                idle = 0.0
            a = mp.plan(mp.WARN, busy=False, gpu_loaded=gpu, gpu_idle_s=idle,
                        cpu_loaded=False, cpu_idle_s=0, cpu_is_fallback=True)
            if "gpu" in a:
                gpu = False
            idle += 5
        self.assertLessEqual(loads, 2)

    def test_gpu_inactif_5_min_part_sous_avertissement(self):
        self.assertEqual(mp.plan(mp.WARN, busy=False, gpu_loaded=True, gpu_idle_s=301,
                                 cpu_loaded=False, cpu_idle_s=0, cpu_is_fallback=True),
                         ("gpu",))

    def test_gpu_inutilisable_decharge(self):
        self.assertEqual(mp.plan(mp.NORMAL, busy=False, gpu_loaded=True, gpu_idle_s=3600,
                                 cpu_loaded=True, cpu_idle_s=3600, cpu_is_fallback=False),
                         ("gpu",))
        self.assertEqual(mp.plan(mp.NORMAL, busy=True, gpu_loaded=True, gpu_idle_s=3600,
                                 cpu_loaded=True, cpu_idle_s=3600, cpu_is_fallback=False),
                         (), "jamais pendant un travail")

    def test_critique_inchange(self):
        self.assertEqual(mp.plan(mp.CRITICAL, busy=False, gpu_loaded=True, gpu_idle_s=1,
                                 cpu_loaded=True, cpu_idle_s=1, cpu_is_fallback=True),
                         ("cpu", "gpu"))


if __name__ == "__main__":
    unittest.main()
