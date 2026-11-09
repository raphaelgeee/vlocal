"""Politique mémoire (v1.6) sous pression SIMULÉE : ordre de déchargement (CPU
puis GPU), aucun déchargement sans pression, rien pendant un travail en cours,
rechargement masqué au prochain appui, préchargement du repli CPU soumis à la
mémoire disponible. Aucun modèle chargé, aucune écriture sur disque."""
import os
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import memory_policy as mp  # noqa: E402
import mlx_engine  # noqa: E402
from test_incidents import _charge_depuis_app  # noqa: E402

IDLE_LONG = 3600.0
RECENT = 2.0


def _plan(level, **kw):
    base = dict(busy=False, gpu_loaded=True, gpu_idle_s=IDLE_LONG,
                cpu_loaded=False, cpu_idle_s=IDLE_LONG, cpu_is_fallback=True)
    base.update(kw)
    return mp.plan(level, **base)


class PressureSignalTests(unittest.TestCase):
    def test_niveaux_du_noyau(self):
        for raw, attendu in ((1, mp.NORMAL), (2, mp.WARN), (4, mp.CRITICAL),
                             (3, None), (None, None)):
            with mock.patch.object(mp, "_sysctl_int", return_value=raw):
                self.assertEqual(mp.kernel_level(), attendu, raw)

    def test_garde_fou_memoire_disponible(self):
        self.assertEqual(mp.combine(None, None), mp.NORMAL)
        self.assertEqual(mp.combine(mp.NORMAL, 6.0), mp.NORMAL)
        self.assertEqual(mp.combine(mp.NORMAL, 0.8), mp.WARN)
        self.assertEqual(mp.combine(mp.NORMAL, 0.3), mp.CRITICAL)
        self.assertEqual(mp.combine(mp.CRITICAL, 6.0), mp.CRITICAL, "le plus sévère gagne")
        self.assertEqual(mp.combine(mp.WARN, None), mp.WARN)

    def test_sysctl_reel_lisible_sur_mac(self):
        if sys.platform != "darwin":
            self.skipTest("macOS seulement")
        self.assertIn(mp.kernel_level(), (mp.NORMAL, mp.WARN, mp.CRITICAL))
        self.assertIsNone(mp._sysctl_int("vlocal.inexistant"))


class PlanTests(unittest.TestCase):
    def test_sans_pression_rien_n_est_decharge(self):
        # GPU inactif depuis une heure, CPU moteur principal (Intel) idem.
        self.assertEqual(_plan(mp.NORMAL), ())
        self.assertEqual(_plan(mp.NORMAL, cpu_loaded=True, cpu_is_fallback=False), ())

    def test_sans_pression_le_repli_cpu_inutile_part(self):
        self.assertEqual(_plan(mp.NORMAL, cpu_loaded=True, cpu_idle_s=RECENT), ())
        self.assertEqual(_plan(mp.NORMAL, cpu_loaded=True,
                               cpu_idle_s=mp.CPU_FALLBACK_IDLE_S), ("cpu",))

    def test_jamais_pendant_un_travail(self):
        for level in (mp.NORMAL, mp.WARN, mp.CRITICAL):
            self.assertEqual(_plan(level, busy=True, cpu_loaded=True), (), level)

    def test_avertissement_cpu_d_abord_puis_gpu(self):
        etat = dict(cpu_loaded=True)
        self.assertEqual(_plan(mp.WARN, **etat), ("cpu",), "un seul modèle par passage")
        etat["cpu_loaded"] = False                  # passage suivant, pression persistante
        self.assertEqual(_plan(mp.WARN, **etat), ("gpu",))
        etat["gpu_loaded"] = False
        self.assertEqual(_plan(mp.WARN, **etat), ())

    def test_avertissement_respecte_le_delai_de_grace(self):
        self.assertEqual(_plan(mp.WARN, cpu_loaded=True, cpu_idle_s=RECENT), ())
        self.assertEqual(_plan(mp.WARN, gpu_idle_s=RECENT), ())

    def test_critique_tout_part_dans_l_ordre(self):
        self.assertEqual(_plan(mp.CRITICAL, cpu_loaded=True, cpu_idle_s=RECENT,
                               gpu_idle_s=RECENT), ("cpu", "gpu"))
        self.assertEqual(_plan(mp.CRITICAL, gpu_loaded=False), ())


class CpuPreloadTests(unittest.TestCase):
    def test_seuil(self):
        self.assertTrue(mp.cpu_preload_allowed(mp.NORMAL, 5.0))
        self.assertFalse(mp.cpu_preload_allowed(mp.NORMAL, mp.CPU_PRELOAD_MIN_AVAIL_GB - 0.1))
        self.assertFalse(mp.cpu_preload_allowed(mp.WARN, 12.0))

    def _after_failure(self, allowed):
        stub = types.SimpleNamespace(model=None, _cpu_preload_lock=threading.Lock(),
                                     _cpu_preloading=False, load_model=mock.Mock(),
                                     last_use=0.0)
        with mock.patch.object(engine.mlx_engine, "status", return_value=("suspended", "gel")), \
                mock.patch.object(engine.memory_policy, "cpu_preload_allowed",
                                  return_value=allowed):
            engine.VlocalEngine._after_gpu_failure(stub)
            for t in threading.enumerate():
                if t.name == "cpu-fallback-preload":
                    t.join(2.0)
        return stub.load_model.call_count

    def test_prechargement_si_la_memoire_le_permet(self):
        self.assertEqual(self._after_failure(True), 1)

    def test_aucun_prechargement_sur_machine_serree(self):
        self.assertEqual(self._after_failure(False), 0)


class _Eng:
    def __init__(self, cpu=False):
        self.model = object() if cpu else None
        self._fast_model = None
        self.last_use = 0.0
        self._fast_last_use = 0.0
        self.unloads = []

    def unload_model(self):
        self.unloads.append("cpu")
        self.model = None


class AppTickTests(unittest.TestCase):
    """Le VRAI _memory_tick de app.py, sur un moteur et un GPU simulés."""

    def setUp(self):
        self.gpu = {"loaded": True, "unloads": 0}
        self.eng = _Eng(cpu=True)
        self.recording = False
        ctrl = types.SimpleNamespace(engine=self.eng,
                                     recording_active=lambda: self.recording)
        self.ns = _charge_depuis_app(["_apply_memory_plan", "_memory_tick"],
                                     {"_controller": ctrl, "_meeting_state": {}})
        gpu = self.gpu

        def unload():
            gpu["unloads"] += 1
            gpu["loaded"] = False
        for p in (mock.patch.object(mlx_engine, "loaded", lambda: gpu["loaded"]),
                  mock.patch.object(mlx_engine, "idle_seconds", lambda: IDLE_LONG),
                  mock.patch.object(mlx_engine, "status", lambda: ("ok", "")),
                  mock.patch.object(mlx_engine, "unload", unload)):
            p.start()
            self.addCleanup(p.stop)

    def _tick(self, level):
        with mock.patch.object(mp, "pressure_level", return_value=level):
            self.ns["_memory_tick"]()

    def test_sans_pression_modeles_gardes(self):
        self.eng.last_use = __import__("time").time()     # repli CPU utilisé à l'instant
        for _ in range(5):
            self._tick(mp.NORMAL)
        self.assertEqual((self.eng.unloads, self.gpu["unloads"]), ([], 0))

    def test_pression_persistante_cpu_puis_gpu(self):
        self._tick(mp.WARN)
        self.assertEqual((self.eng.unloads, self.gpu["unloads"]), (["cpu"], 0))
        self._tick(mp.WARN)
        self.assertEqual((self.eng.unloads, self.gpu["unloads"]), (["cpu"], 1))
        self._tick(mp.WARN)
        self.assertEqual(self.gpu["unloads"], 1, "rien à décharger de plus")

    def test_rien_pendant_une_dictee(self):
        self.recording = True
        self._tick(mp.CRITICAL)
        self.assertEqual((self.eng.unloads, self.gpu["unloads"]), ([], 0))

    def test_echec_de_dechargement_journalise_pas_avale(self):
        with mock.patch.object(mlx_engine, "unload", side_effect=TimeoutError("GPU occupé")), \
                mock.patch("builtins.print") as out:
            self.ns["_apply_memory_plan"](self.eng, ("gpu",))
        self.assertIn("KO", " ".join(str(c) for c in out.call_args_list))


class ReloadOnNextPressTests(unittest.TestCase):
    """Après un déchargement sous pression, le début de la parole suivante
    recharge le modèle GPU (prewarm) : le rechargement est masqué par la parole."""

    def test_prewarm_recharge_le_gpu(self):
        stub = types.SimpleNamespace()
        with mock.patch.object(engine.mlx_engine, "available", return_value=True), \
                mock.patch.object(engine.mlx_engine, "warmup") as warm, \
                mock.patch.object(engine, "_qos_user_initiated"):
            engine.VlocalEngine.prewarm(stub)
        warm.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
