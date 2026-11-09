"""Disponibilité GPU (v1.4.4) : plus de verdict négatif permanent sur une cause
passagère, bascule réservée aux gels de dictée, status() sans effet de bord,
budget de finalisation avec réserve CPU, issue de la dernière dictée.

Hermétique : aucune vraie sonde Metal, aucun vrai modèle, aucune écriture dans
les journaux de l'utilisateur (_diag et errors.log sont remplacés)."""
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import mlx_engine  # noqa: E402

_STATE = ("_available", "_model_ref", "_final_state", "_state_reason", "_retry_at",
          "_retry_n", "_gpu_suspended_until", "_gpu_suspend_reason", "_gpu_strikes",
          "_kernels_warm", "_last_use", "_probe_running", "_probe_thread", "_probe_gen")


def _settle(timeout=2.0):
    """Attend la fin des sondes lancées en fond par available()."""
    for t in threading.enumerate():
        if t.name.startswith("mlx-probe"):
            t.join(timeout)


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class MlxStateCase(unittest.TestCase):
    """Isole l'état module de mlx_engine : horloge simulée, sonde comptée,
    modules mlx factices, journaux neutralisés."""

    def setUp(self):
        saved = {k: getattr(mlx_engine, k) for k in _STATE}
        saved["_gpu_strikes"] = list(saved["_gpu_strikes"])
        self.addCleanup(lambda: [setattr(mlx_engine, k, v) for k, v in saved.items()])
        mlx_engine._available = None
        mlx_engine._model_ref = None
        mlx_engine._final_state = ""
        mlx_engine._state_reason = ""
        mlx_engine._retry_at = 0.0
        mlx_engine._retry_n = 0
        mlx_engine._gpu_suspended_until = 0.0
        mlx_engine._gpu_suspend_reason = ""
        mlx_engine._gpu_strikes = []
        mlx_engine._probe_running = False
        mlx_engine._probe_thread = None
        mlx_engine._probe_gen += 1          # ignore le verdict tardif d'un test précédent
        self.addCleanup(_settle)

        self.clock = _Clock()
        self.events = []
        fake_errors = types.ModuleType("errors")
        fake_errors.E = types.SimpleNamespace(GPU_FALLBACK_CPU="gpu_fallback_cpu")
        fake_errors.log = lambda code, **ctx: self.events.append(code)
        self.smoke_calls = 0
        self.smoke_error = None

        def smoke():
            self.smoke_calls += 1
            if self.smoke_error is not None:
                raise self.smoke_error

        fake_mlx = types.ModuleType("mlx")
        fake_core = types.ModuleType("mlx.core")
        fake_mlx.core = fake_core
        self.fake_whisper = types.ModuleType("mlx_whisper")
        env = {k: v for k, v in os.environ.items() if k != "VLOCAL_NO_MLX"}
        for p in (mock.patch.object(mlx_engine, "_now", self.clock),
                  mock.patch.object(mlx_engine, "_diag", lambda msg: None),
                  mock.patch.object(mlx_engine, "_metal_smoke", smoke),
                  mock.patch.object(sys, "platform", "darwin"),
                  mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.dict(sys.modules, {"errors": fake_errors, "mlx": fake_mlx,
                                                "mlx.core": fake_core,
                                                "mlx_whisper": self.fake_whisper})):
            p.start()
            self.addCleanup(p.stop)

    def retry(self):
        """Essai dû : available() renvoie False sans attendre et sonde en fond ;
        le verdict vaut pour l'appel suivant."""
        self.assertFalse(mlx_engine.available(), "jamais de sonde bloquante")
        _settle()
        return mlx_engine.available()


class ModelAppearsTests(MlxStateCase):
    """Critère 1 : modèle téléchargé après le 1er lancement."""

    def setUp(self):
        super().setUp()
        self.base = tempfile.mkdtemp()
        for p in (mock.patch.object(sys, "frozen", True, create=True),
                  mock.patch.object(mlx_engine, "_base_dir", lambda: self.base)):
            p.start()
            self.addCleanup(p.stop)

    def _install_model(self):
        d = os.path.join(self.base, "models", "whisper-large-v3-turbo-mlx-q8")
        os.makedirs(d)
        open(os.path.join(d, "config.json"), "w").close()
        return d

    def test_absent_then_reset_after_download(self):
        self.assertFalse(mlx_engine.available())
        self.assertEqual(mlx_engine.status()[0], "retry")
        self.assertEqual(self.smoke_calls, 0, "pas de sonde Metal sans modèle")
        d = self._install_model()
        mlx_engine.reset_availability("modèle GPU téléchargé")
        self.assertTrue(mlx_engine.available())
        self.assertEqual(mlx_engine.model_ref(), d)
        self.assertEqual(mlx_engine.status(), ("ok", ""))

    def test_absent_then_found_by_retry_without_reset(self):
        self.assertFalse(mlx_engine.available())
        self._install_model()
        self.clock.t += mlx_engine._RETRY_DELAYS_S[0] + 1
        self.assertTrue(self.retry(), "le repli HF n'est plus mémorisé")

    def test_reset_keeps_a_pointed_existing_model(self):
        d = self._install_model()
        mlx_engine._model_ref = d
        mlx_engine.reset_availability("test")
        self.assertEqual(mlx_engine._model_ref, d)
        mlx_engine._model_ref = os.path.join(self.base, "disparu")
        mlx_engine.reset_availability("test")
        self.assertIsNone(mlx_engine._model_ref)


class TransientProbeTests(MlxStateCase):
    """Critère 2 : sonde en échec passager, délai croissant, coût nul entre deux."""

    def test_transient_failure_retried_after_delay(self):
        self.smoke_error = RuntimeError("Failed to load the default metallib: "
                                        "Interrupted system call")
        self.assertFalse(mlx_engine.available())
        self.assertEqual(self.smoke_calls, 1)
        for _ in range(5):
            self.clock.t += 10
            self.assertFalse(mlx_engine.available())
        self.assertEqual(self.smoke_calls, 1, "aucune sonde pendant le délai")
        self.smoke_error = None
        self.clock.t += mlx_engine._RETRY_DELAYS_S[0]
        self.assertTrue(self.retry())
        self.assertEqual(self.smoke_calls, 2)
        self.assertEqual(mlx_engine._retry_n, 0, "succès : l'échelle repart du bas")

    def test_delays_grow_then_cap(self):
        self.smoke_error = RuntimeError("metal KO")
        seen = []
        mlx_engine.available()
        for _ in range(6):
            if mlx_engine._retry_at <= self.clock.t:
                self.retry()
            seen.append(round(mlx_engine._retry_at - self.clock.t))
            self.clock.t = mlx_engine._retry_at
        self.assertEqual(seen, [60, 300, 900, 1800, 1800, 1800])

    def test_definitive_causes_never_reprobe(self):
        with mock.patch.dict(os.environ, {"VLOCAL_NO_MLX": "1"}):
            self.assertFalse(mlx_engine.available())
            self.clock.t += 10 * 3600
            self.assertFalse(mlx_engine.available())
            self.assertEqual(mlx_engine.status()[0], "disabled")
        self.assertEqual(self.smoke_calls, 0)

    def test_missing_mlx_package_is_unsupported(self):
        with mock.patch.dict(sys.modules, {"mlx_whisper": None}):
            self.assertFalse(mlx_engine.available())
        self.assertEqual(mlx_engine.status()[0], "unsupported")
        self.clock.t += 10 * 3600
        self.assertFalse(mlx_engine.available())
        self.assertEqual(self.smoke_calls, 0)


class ProbeOffCallerThreadTests(MlxStateCase):
    """Vérificateur (bloquant) : la sonde ne fige jamais le thread qui dicte, et
    une seule sonde part même sous appels concurrents."""

    def setUp(self):
        super().setUp()
        mlx_engine._set_unavailable("sonde Metal KO", final_state="")
        self.clock.t = mlx_engine._retry_at + 1          # nouvel essai dû

    def test_hanging_probe_does_not_block_available(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hang():
            self.smoke_calls += 1
            release.wait(10)

        caller = threading.Thread(target=mlx_engine.available, name="dictee")
        with mock.patch.object(mlx_engine, "_metal_smoke", hang), \
                mock.patch.object(mlx_engine, "_PROBE_TIMEOUT_S", 0.2):
            caller.start()
            caller.join(0.05)
            self.assertFalse(caller.is_alive(), "available() a attendu la sonde")
            for t in threading.enumerate():
                if t.name == "mlx-probe-retry":
                    t.join(2.0)
            self.assertEqual(mlx_engine.status()[0], "retry")
            self.assertIn("figée", mlx_engine._state_reason)
            self.assertEqual(mlx_engine._retry_n, 2, "délai dépassé = échec, palier suivant")
            # Essai suivant dû alors que le 1er kernel est toujours coincé :
            # échec immédiat, aucun second kernel lancé.
            self.clock.t = mlx_engine._retry_at + 1
            self.assertFalse(mlx_engine.available())
            for t in threading.enumerate():
                if t.name == "mlx-probe-retry":
                    t.join(2.0)
            self.assertEqual(self.smoke_calls, 1)
            self.assertEqual(mlx_engine._retry_n, 3)
        release.set()
        caller.join(1.0)

    def test_concurrent_callers_launch_a_single_probe(self):
        import time as _t

        def slow_ok():
            self.smoke_calls += 1
            _t.sleep(0.1)

        barrier = threading.Barrier(10)

        def call():
            barrier.wait()
            mlx_engine.available()

        with mock.patch.object(mlx_engine, "_metal_smoke", slow_ok):
            threads = [threading.Thread(target=call) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5.0)
            _settle()
        self.assertEqual(self.smoke_calls, 1)
        self.assertEqual(mlx_engine._retry_n, 0)
        self.assertTrue(mlx_engine.available())

    def test_first_probe_waits_for_verdict_but_is_bounded(self):
        mlx_engine._available = None
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(mlx_engine, "_metal_smoke", lambda: release.wait(10)), \
                mock.patch.object(mlx_engine, "_FIRST_PROBE_TIMEOUT_S", 0.2):
            self.assertFalse(mlx_engine.available())
        self.assertEqual(mlx_engine.status()[0], "retry")


class ForceCpuAndStrikeTests(MlxStateCase):
    """Critères 3 et 4 : bascule temporaire, seuls les gels de dictée comptent."""

    def setUp(self):
        super().setUp()
        self.assertTrue(mlx_engine.available())
        self.smoke_calls = 0

    def test_force_cpu_is_retried(self):
        mlx_engine._force_cpu("préchauffe Metal KO (RuntimeError)")
        self.assertFalse(mlx_engine.available())
        self.assertEqual(self.events, ["gpu_fallback_cpu"])
        self.clock.t += mlx_engine._RETRY_DELAYS_S[0] - 1
        self.assertFalse(mlx_engine.available())
        self.assertEqual(self.smoke_calls, 0)
        self.clock.t += 2
        self.assertTrue(self.retry(), "GPU retenté, pas « jamais »")

    def test_background_freezes_only_suspend(self):
        for _ in range(mlx_engine._GPU_MAX_STRIKES):
            mlx_engine._gpu_strike("inférence MLX figée (>60s)", dictation=False)
            self.clock.t += 60
        self.assertTrue(mlx_engine._available, "pas de bascule sur gels hors dictée")
        self.assertEqual(mlx_engine._gpu_strikes, [])
        self.assertEqual(mlx_engine.status()[0], "suspended")
        self.assertFalse(mlx_engine.available())
        self.clock.t += mlx_engine._GPU_COOLDOWN_S
        self.assertTrue(mlx_engine.available())

    def test_dictation_freezes_switch_temporarily(self):
        for _ in range(mlx_engine._GPU_MAX_STRIKES):
            mlx_engine._gpu_strike("inférence MLX figée (>22s)")
            self.clock.t += 60
        self.assertFalse(mlx_engine._available)
        self.assertEqual(mlx_engine.status()[0], "retry")
        self.clock.t += mlx_engine._FORCE_CPU_RETRY_S - 200
        self.assertFalse(mlx_engine.available())
        self.clock.t += 200
        self.assertTrue(self.retry())

    def test_worker_freeze_flags_dictation_by_priority(self):
        calls = []
        release = threading.Event()
        with mock.patch.object(mlx_engine, "_gpu_strike",
                               lambda reason, dictation=True: calls.append(dictation)):
            for prio in (mlx_engine.PRIO_BACKGROUND, mlx_engine.PRIO_DICTATION):
                with self.assertRaises(TimeoutError):
                    mlx_engine._on_worker(release.wait, 5, _timeout=0.5, _prio=prio)
        release.set()
        self.assertEqual(calls, [False, True])


class WarmupMarkersTests(MlxStateCase):
    """Critère 5 : seules les vraies erreurs Metal font basculer."""

    def setUp(self):
        super().setUp()
        self.assertTrue(mlx_engine.available())
        mlx_engine._kernels_warm = False
        # v1.6 : la préchauffe charge le modèle (encodeur accéléré) avant
        # l'inférence à blanc ; les modules mlx factices n'ont pas de modèle.
        for p in (mock.patch.object(mlx_engine, "loaded", lambda: False),
                  mock.patch.object(mlx_engine, "_ensure_model", lambda: None)):
            p.start()
            self.addCleanup(p.stop)

    def _warmup_raising(self, exc):
        def boom(*a, **k):
            raise exc
        self.fake_whisper.transcribe = boom
        mlx_engine._warmup_impl()

    def test_disk_full_does_not_switch(self):
        self._warmup_raising(OSError(28, "No space left on device"))
        self.assertTrue(mlx_engine._available)
        self.assertEqual(self.events, [])

    def test_metallib_failure_switches(self):
        self._warmup_raising(RuntimeError("Failed to load the default metallib"))
        self.assertFalse(mlx_engine._available)
        self.assertEqual(mlx_engine.status()[0], "retry")


class StatusSideEffectTests(MlxStateCase):
    """Critère 6 : status() ne sonde, ne charge et ne modifie rien."""

    def _snapshot(self):
        return {k: getattr(mlx_engine, k) for k in _STATE}

    def test_status_never_probes(self):
        probe = mock.Mock(side_effect=AssertionError("status a sondé"))
        with mock.patch.object(mlx_engine, "_probe", probe):
            self.assertEqual(mlx_engine.status()[0], "unknown")
        self.smoke_error = RuntimeError("metal KO")
        mlx_engine.available()
        self.clock.t += 10 * 3600            # délai écoulé : available() re-sonderait
        before = self._snapshot()
        with mock.patch.object(mlx_engine, "_probe", probe):
            state, reason = mlx_engine.status()
        self.assertEqual(state, "retry")
        self.assertLessEqual(len(reason), 80)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.smoke_calls, 1)

    def test_expired_suspension_is_not_cleared_by_status(self):
        mlx_engine.available()
        mlx_engine._gpu_strike("x" * 200, dictation=False)
        state, reason = mlx_engine.status()
        self.assertEqual(state, "suspended")
        self.assertLessEqual(len(reason), 80)
        self.clock.t += mlx_engine._GPU_COOLDOWN_S + 1
        before = self._snapshot()
        self.assertEqual(mlx_engine.status(), ("ok", ""))
        self.assertEqual(self._snapshot(), before)


class FinalizeBudgetTests(unittest.TestCase):
    """Critère 7 : budget calculé sur le chemin réel (GPU ou non, CPU chargé ou non)."""

    def budget(self, gpu_state, cpu_loaded, audio_s=5.0):
        with mock.patch.object(engine.mlx_engine, "status", lambda: (gpu_state, "")):
            return engine.finalize_budget_s(audio_s, cpu_loaded=cpu_loaded)

    def test_table(self):
        gpu = engine.dictation_gpu_timeout(5)
        cpu = engine.CPU_FALLBACK_BASE_S + engine.CPU_FALLBACK_PER_AUDIO_S * 5
        load = engine.CPU_FALLBACK_LOAD_S
        table = {("ok", True): gpu + cpu,
                 ("ok", False): gpu + cpu + load,
                 ("unknown", False): gpu + cpu + load,
                 ("suspended", True): cpu,
                 ("retry", False): cpu + load,
                 ("unsupported", True): cpu,          # Intel : pas de 22 s de GPU
                 ("disabled", False): cpu + load}
        for (state, loaded), expected in table.items():
            with self.subTest(state=state, cpu_loaded=loaded):
                self.assertAlmostEqual(self.budget(state, loaded), expected)

    def test_gpu_fallback_keeps_real_cpu_reserve(self):
        reserve = self.budget("ok", False, 10) - engine.dictation_gpu_timeout(10)
        self.assertGreater(reserve, 3.0, "l'ancien chien de garde ne laissait que 3 s")
        self.assertLess(self.budget("ok", True), 30.0, "5 s d'audio, CPU déjà prêt")

    def test_monotonic_and_robust(self):
        self.assertLess(self.budget("ok", True, 10), self.budget("ok", True, 300))
        self.assertEqual(self.budget("ok", True, -5), self.budget("ok", True, 0))

    def test_reads_live_engine_model_state(self):
        fake = types.SimpleNamespace(model=None)
        with mock.patch.object(engine, "_ENGINE_REF", lambda: fake):
            self.assertFalse(engine._cpu_model_resident())
            fake.model = object()
            self.assertTrue(engine._cpu_model_resident())


class _Seg:
    def __init__(self, text):
        self.text = text


class _FakeWhisper:
    """WhisperModel factice : `behaviour` décide de la sortie de transcribe."""
    behaviour = "ok"

    def __init__(self, *a, **k):
        pass

    def transcribe(self, audio, **kwargs):
        if _FakeWhisper.behaviour == "raise":
            raise RuntimeError("CTranslate2 KO")
        if _FakeWhisper.behaviour == "vad":
            return iter([]), types.SimpleNamespace(duration_after_vad=0.0)
        return iter([_Seg(" Bonjour à tous.")]), types.SimpleNamespace(duration_after_vad=1.0)


class DictationOutcomeTests(unittest.TestCase):
    """Critère 8 : last_backend / last_empty_reason, moteur simulé."""

    LOUD = np.full(16000, 0.1, dtype=np.float32)
    SILENT = np.zeros(16000, dtype=np.float32)

    def setUp(self):
        self.logged = []
        model_dir = tempfile.mkdtemp()
        open(os.path.join(model_dir, "model.bin"), "w").close()
        self.gpu = {"available": False, "status": ("unsupported", "test"), "calls": 0}

        def mlx_transcribe(*a, **k):
            self.gpu["calls"] += 1
            if isinstance(self.gpu.get("result"), BaseException):
                raise self.gpu["result"]
            return self.gpu["result"]

        _FakeWhisper.behaviour = "ok"
        for p in (mock.patch("faster_whisper.WhisperModel", _FakeWhisper),
                  mock.patch.object(engine.VlocalEngine, "_log_input_device", lambda s: None),
                  mock.patch.object(engine, "_load_glossary_hotwords", lambda: ""),
                  mock.patch.object(engine.errors, "log",
                                    lambda code, **ctx: self.logged.append(code)),
                  mock.patch.object(engine.mlx_engine, "available",
                                    lambda: self.gpu["available"]),
                  mock.patch.object(engine.mlx_engine, "status", lambda: self.gpu["status"]),
                  mock.patch.object(engine.mlx_engine, "transcribe", mlx_transcribe)):
            p.start()
            self.addCleanup(p.stop)
        self.eng = engine.VlocalEngine(model_dir=model_dir)

    def _dictate(self, audio, **kw):
        return self.eng.stop_and_transcribe(audio=audio, **kw)

    def test_cpu_success(self):
        self.assertEqual(self._dictate(self.LOUD), "Bonjour à tous.")
        self.assertEqual(self.eng.last_backend(), "cpu")
        self.assertIsNone(self.eng.last_empty_reason())

    def test_engine_exception_is_reported_not_silence(self):
        _FakeWhisper.behaviour = "raise"
        self.assertEqual(self._dictate(self.LOUD), "", "contrat visible inchangé")
        self.assertEqual(self.eng.last_empty_reason(), "fail")
        self.assertEqual(self.eng.last_backend(), "")
        self.assertEqual(self.logged, [], "journalisé une seule fois, par l'app")
        _FakeWhisper.behaviour = "ok"
        self._dictate(self.LOUD)
        self.assertIsNone(self.eng.last_empty_reason(), "remis à zéro à chaque dictée")

    def test_gate_and_vad(self):
        self.assertEqual(self._dictate(self.SILENT), "")
        self.assertEqual(self.eng.last_empty_reason(), "gate")
        self.assertEqual(self.eng.last_backend(), "")
        _FakeWhisper.behaviour = "vad"
        self.assertEqual(self._dictate(self.LOUD), "")
        self.assertEqual(self.eng.last_empty_reason(), "vad")

    def test_gpu_success(self):
        self.gpu.update(available=True, status=("ok", ""), result={"text": " Salut."})
        self.assertEqual(self._dictate(self.LOUD, adaptive=True), "Salut.")
        self.assertEqual(self.eng.last_backend(), "mlx")
        self.assertIsNone(self.eng.last_empty_reason())

    def test_gpu_failure_cascade_stays_on_cpu(self):
        self.gpu.update(available=True, status=("ok", ""),
                        result=TimeoutError("GPU occupé (>27s)"))
        self.assertEqual(self._dictate(self.LOUD, adaptive=True), "Bonjour à tous.")
        self.assertEqual(self.gpu["calls"], 1, "la cascade ne retente pas le GPU")
        self.assertEqual(self.eng.last_backend(), "cpu")

    def test_gpu_freeze_preloads_cpu_model(self):
        self.gpu.update(available=True, status=("suspended", "gel"),
                        result=TimeoutError("transcription MLX figée"))
        self.eng.model = None               # GPU actif : turbo CPU jamais chargé
        # v1.6 : préchargement soumis à la mémoire disponible ; ici, on teste
        # le déclenchement, pas la mémoire de la machine qui lance les tests.
        with mock.patch.object(engine.memory_policy, "cpu_preload_allowed",
                               return_value=True), \
                mock.patch.object(self.eng, "load_model") as load:
            self.eng.transcribe_window(self.LOUD)
            for t in threading.enumerate():
                if t.name == "cpu-fallback-preload":
                    t.join(2.0)
        self.assertGreaterEqual(load.call_count, 1)

    def test_cascade_turbo_failure_rescued_by_small_is_not_a_failure(self):
        base = tempfile.mkdtemp()
        for name in ("turbo", engine.SMALL_DIR_NAME):
            os.makedirs(os.path.join(base, name))
            open(os.path.join(base, name, "model.bin"), "w").close()
        eng = engine.VlocalEngine(model_dir=os.path.join(base, "turbo"))

        class _Word:
            probability = 0.2                  # confiance basse : escalade turbo

        class _SmallSeg(_Seg):
            words = [_Word()]

        class _Small:
            def transcribe(self, audio, **kw):
                return iter([_SmallSeg(" Bonjour.")]), None

        class _Turbo:
            def transcribe(self, audio, **kw):
                raise RuntimeError("turbo KO")

        eng._fast_model = _Small()
        eng.model = _Turbo()
        self.assertEqual(eng.stop_and_transcribe(audio=self.LOUD, adaptive=True), "Bonjour.")
        self.assertIsNone(eng.last_empty_reason())
        self.assertEqual(eng.last_backend(), "cpu")
        self.assertEqual(self.logged, [], "aucun faux échec journalisé")

    def test_timeout_exception_publishes_fail(self):
        with mock.patch.object(self.eng, "transcribe",
                               side_effect=TimeoutError("moteur occupé")):
            with self.assertRaises(TimeoutError):
                self._dictate(self.LOUD)
        self.assertEqual(self.eng.last_empty_reason(), "fail")


if __name__ == "__main__":
    unittest.main()
