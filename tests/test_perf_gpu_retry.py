"""Le CPU en dernier recours (v1.6) : une erreur GPU rapide est retentée une
fois sur un worker MLX neuf avant le repli CPU ; un gel (TimeoutError) garde
la logique de suspension. GPU simulé, aucun modèle chargé."""
import os
import sys
import threading
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import mlx_engine  # noqa: E402

AUDIO = np.full(16000 * 5, 0.1, dtype=np.float32)


class DictationRetryTests(unittest.TestCase):
    def setUp(self):
        self.results = []
        self.calls = []
        self.available = True

        def fake_transcribe(audio, **kw):
            self.calls.append(kw)
            r = self.results.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r

        self.renew = mock.Mock()
        for p in (mock.patch.object(engine.mlx_engine, "transcribe", fake_transcribe),
                  mock.patch.object(engine.mlx_engine, "available", lambda: self.available),
                  mock.patch.object(engine.mlx_engine, "renew_worker", self.renew),
                  mock.patch.object(engine, "_load_glossary_hotwords", lambda: "")):
            p.start()
            self.addCleanup(p.stop)
        self.stub = types.SimpleNamespace(_note=lambda *a: None, last_model=None)

    def _run(self):
        return engine.VlocalEngine._mlx_text(self.stub, AUDIO)

    def test_erreur_rapide_retentee_sur_le_gpu(self):
        self.results = [RuntimeError("[METAL] Command buffer execution failed"),
                        {"text": " Bonjour."}]
        self.assertEqual(self._run(), "Bonjour.")
        self.assertEqual(len(self.calls), 2)
        self.renew.assert_called_once_with()
        budget = engine.dictation_gpu_timeout(5.0)
        self.assertLessEqual(self.calls[1]["_timeout"], budget, "même budget total")

    def test_gel_jamais_retente(self):
        self.results = [TimeoutError("transcription MLX figée")]
        with self.assertRaises(TimeoutError):
            self._run()
        self.assertEqual(len(self.calls), 1)
        self.renew.assert_not_called()

    def test_un_seul_second_essai(self):
        self.results = [RuntimeError("a"), RuntimeError("b")]
        with self.assertRaises(RuntimeError):
            self._run()
        self.assertEqual(len(self.calls), 2)

    def test_erreur_lente_va_au_cpu(self):
        self.results = [RuntimeError("lent")]
        t = iter([0.0, engine.GPU_RETRY_WITHIN_S + 1.0])
        with mock.patch.object(engine._time, "monotonic", lambda: next(t)):
            with self.assertRaises(RuntimeError):
                self._run()
        self.assertEqual(len(self.calls), 1)

    def test_gpu_suspendu_entre_temps_va_au_cpu(self):
        self.results = [RuntimeError("x")]
        self.available = False
        with self.assertRaises(RuntimeError):
            self._run()
        self.assertEqual(len(self.calls), 1)


class RenewWorkerTests(unittest.TestCase):
    """Vrai worker MLX (aucun appel Metal : fonctions Python pures)."""

    def test_worker_neuf_et_ancien_arrete(self):
        first = mlx_engine._on_worker(threading.current_thread)
        self.assertEqual(first.name, "mlx-gpu")
        mlx_engine.renew_worker()
        second = mlx_engine._on_worker(threading.current_thread)
        self.assertIsNot(second, first)
        first.join(2.0)
        self.assertFalse(first.is_alive(), "aucun thread orphelin")
        self.assertTrue(second.is_alive())

    def test_travail_deja_en_file_termine_avant_l_arret(self):
        gate = threading.Event()
        box = {}
        t = threading.Thread(target=lambda: box.setdefault(
            "r", mlx_engine._on_worker(lambda: gate.wait(2.0) and "fini")))
        t.start()
        while mlx_engine._mlx_worker is None:
            pass
        old = mlx_engine._mlx_worker
        mlx_engine.renew_worker()
        gate.set()
        t.join(3.0)
        self.assertEqual(box.get("r"), "fini")
        old.join(2.0)
        self.assertFalse(old.is_alive())


if __name__ == "__main__":
    unittest.main()
