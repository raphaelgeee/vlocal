"""Resilience GPU (v1.1.0) : timeout proportionnel et suspension temporaire."""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mlx_engine  # noqa: E402
from engine import dictation_gpu_timeout  # noqa: E402


class DictationTimeoutTests(unittest.TestCase):
    def test_short_dictation_keeps_historical_floor(self):
        self.assertEqual(dictation_gpu_timeout(0), 22.0)
        self.assertAlmostEqual(dictation_gpu_timeout(10), 27.0)

    def test_long_dictation_gets_proportional_budget(self):
        # cas reel du 19/08 : reliquat de 371 s transcrit en ~17 s sur GPU,
        # coupe a 22 s et declare gel Metal
        self.assertGreater(dictation_gpu_timeout(371), 100.0)
        self.assertLess(dictation_gpu_timeout(371), 400.0)

    def test_monotonic_and_robust_to_bad_input(self):
        self.assertLessEqual(dictation_gpu_timeout(30), dictation_gpu_timeout(60))
        self.assertEqual(dictation_gpu_timeout(-5), 22.0)


class GpuStrikeTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(mlx_engine, k) for k in
                       ("_available", "_gpu_suspended_until", "_gpu_strikes", "_diag")}
        self._errors = sys.modules.get("errors")
        self.events = []
        fake = types.ModuleType("errors")
        fake.E = types.SimpleNamespace(GPU_FALLBACK_CPU="gpu_fallback_cpu")
        fake.log = lambda code, **ctx: self.events.append((code, ctx))
        sys.modules["errors"] = fake
        mlx_engine._diag = lambda msg: None
        mlx_engine._available = True
        mlx_engine._gpu_suspended_until = 0.0
        mlx_engine._gpu_strikes = []

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(mlx_engine, k, v)
        if self._errors is not None:
            sys.modules["errors"] = self._errors
        else:
            sys.modules.pop("errors", None)

    def test_one_strike_suspends_then_recovers(self):
        mlx_engine._gpu_strike("inférence MLX figée (>22s)")
        self.assertTrue(mlx_engine._available, "un seul gel ne condamne pas la session")
        self.assertFalse(mlx_engine.available(), "GPU suspendu pendant le cooldown")
        self.assertEqual(self.events[0][0], "gpu_fallback_cpu")
        mlx_engine._gpu_suspended_until = time.time() - 1
        self.assertTrue(mlx_engine.available(), "GPU de retour apres le cooldown")
        self.assertEqual(mlx_engine._gpu_suspended_until, 0.0)

    def test_three_strikes_in_window_force_cpu(self):
        for _ in range(mlx_engine._GPU_MAX_STRIKES):
            mlx_engine._gpu_strike("gel")
        self.assertFalse(mlx_engine._available)
        self.assertFalse(mlx_engine.available())

    def test_old_strikes_expire(self):
        old = time.time() - mlx_engine._GPU_STRIKE_WINDOW_S - 1
        mlx_engine._gpu_strikes = [old, old]
        mlx_engine._gpu_strike("gel")
        self.assertTrue(mlx_engine._available)
        self.assertEqual(len(mlx_engine._gpu_strikes), 1)

    def test_unavailable_gpu_stays_unavailable(self):
        mlx_engine._available = False
        self.assertFalse(mlx_engine.available())


if __name__ == "__main__":
    unittest.main()
