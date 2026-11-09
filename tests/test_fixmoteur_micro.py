"""Correctifs micro côté moteur (1.5) : flux muet réparé, zéros numériques ->
micro intégré, refus du périphérique -> micro intégré sans respawn, reset
pendant une ouverture lente, renouvellement différé du worker, aucune
énumération PortAudio au démarrage. Worker et sounddevice simulés : aucun
micro ouvert."""
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_mic_fakes import FakeSD, ProcFactory, bare_engine, device  # noqa: E402

import engine  # noqa: E402
import micworker  # noqa: E402

MAC = "Micro MacBook Pro"
USB = "Yeti USB"
TEAMS = "Microsoft Teams Audio"


class _Base(unittest.TestCase):
    def monte(self, sd, delays=(0.0,)):
        self.sd = sd
        self.fab = ProcFactory(sd, None, delays)
        self.rm = micworker.RemoteMic(16000, spawn=self.fab)
        self.eng = bare_engine(self.rm)
        self.addCleanup(self.fab.shutdown)
        self.rm.ensure()
        return self.eng

    def attendre(self, cond, s=3.0):
        t_end = time.monotonic() + s
        while not cond() and time.monotonic() < t_end:
            time.sleep(0.02)
        return cond()


class FluxMuetTests(_Base):
    def test_started_sans_aucun_bloc_rouvert_sur_un_worker_neuf(self):
        eng = self.monte(FakeSD([device(MAC)], MAC))          # aucun bloc livré
        self.assertTrue(eng.start_recording(deadline=time.monotonic() + 2.0))
        self.assertTrue(self.attendre(lambda: len(self.sd.streams) >= 2),
                        "flux rouvert après FIRST_BLOCK_S")
        self.assertEqual(len(self.fab.procs), 2, "worker neuf")
        eng.stop_recording()
        self.assertFalse(self.rm._capturing)

    def test_flux_vivant_jamais_rouvert(self):
        eng = self.monte(FakeSD([device(MAC)], MAC, auto_feed=True))
        eng.start_recording(deadline=time.monotonic() + 2.0)
        time.sleep(eng.FIRST_BLOCK_S + 0.4)
        eng.stop_recording()
        self.assertEqual((len(self.sd.streams), len(self.fab.procs)), (1, 1))

    def test_zeros_numeriques_bascule_sur_le_micro_integre(self):
        eng = self.monte(FakeSD([device(MAC), device(TEAMS)], TEAMS,
                                auto_feed=True, level=0.0))
        eng.start_recording(deadline=time.monotonic() + 2.0)
        self.assertTrue(self.attendre(lambda: len(self.sd.streams) >= 2))
        self.assertEqual(self.sd.streams[-1].name, MAC)
        time.sleep(eng.FIRST_BLOCK_S + 0.3)
        self.assertEqual(len(self.sd.streams), 2, "pas de boucle sur l'intégré")
        eng.stop_recording()


class RefusTests(_Base):
    def test_peripherique_refuse_integre_immediat_sans_respawn(self):
        sd = FakeSD([device(MAC), device(USB)], USB, auto_feed=True)
        eng = self.monte(sd)
        sd.world = [device(MAC)]               # le défaut refuse de s'ouvrir
        t0 = time.monotonic()
        ok = eng.start_recording(deadline=t0 + 2.0)
        dt = time.monotonic() - t0
        self.assertTrue(ok)
        self.assertLess(dt, 0.2)
        self.assertEqual(sd.streams[-1].name, MAC)
        self.assertEqual(len(self.fab.procs), 1, "worker sain conservé")
        eng.stop_recording()

    def test_refus_total_signale_worker_sain(self):
        sd = FakeSD([device(USB)], USB)
        eng = self.monte(sd)
        sd.world = []
        with self.assertRaises(engine.MicRefused) as cm:
            eng.start_recording(deadline=time.monotonic() + 2.0)
        self.assertTrue(cm.exception.worker_healthy)
        self.assertFalse(eng._recording)


class ResetPendantOuvertureTests(_Base):
    def test_capture_orpheline_refermee(self):
        sd = FakeSD([device(MAC)], MAC, open_delay={MAC: 0.8}, auto_feed=True)
        eng = self.monte(sd)
        box = {}
        th = threading.Thread(target=lambda: box.setdefault(
            "ok", eng.start_recording(deadline=time.monotonic() + 3.0)))
        th.start()
        time.sleep(0.3)
        eng.reset_audio()                       # ce que fait force_reset
        th.join(5)
        self.assertFalse(box["ok"])
        self.assertFalse(self.rm._capturing, "worker sorti de capture")
        self.assertFalse(eng._rec_via_worker)

    def test_stop_arrete_un_worker_reste_en_capture(self):
        sd = FakeSD([device(MAC)], MAC, auto_feed=True)
        eng = self.monte(sd)
        eng.start_recording(deadline=time.monotonic() + 2.0)
        with eng._lock:
            eng._recording = False              # état moteur déjà relâché
        eng.stop_recording()
        self.assertFalse(self.rm._capturing)


class RenouvellementTests(_Base):
    def test_au_repos_tout_de_suite(self):
        self.monte(FakeSD([device(MAC)], MAC))
        self.eng.request_mic_renewal()
        self.assertTrue(self.attendre(lambda: len(self.fab.procs) == 2))
        self.assertTrue(self.attendre(lambda: not self.eng._mic_renewal_due))

    def test_pendant_une_capture_differe_au_prochain_repos(self):
        eng = self.monte(FakeSD([device(MAC)], MAC, auto_feed=True))
        eng.start_recording(deadline=time.monotonic() + 2.0)
        eng.request_mic_renewal()
        time.sleep(0.3)
        self.assertEqual(len(self.fab.procs), 1, "la capture n'est pas tuée")
        eng.stop_recording()
        eng.mic_health_heal()                   # passage du superviseur, au repos
        self.assertTrue(self.attendre(lambda: len(self.fab.procs) == 2))
        self.assertFalse(eng._mic_renewal_due)


class DemarrageTests(unittest.TestCase):
    def test_aucune_enumeration_portaudio_au_demarrage(self):
        sd = types.SimpleNamespace(query_devices=mock.Mock(), default=types.SimpleNamespace())
        env = {k: v for k, v in os.environ.items() if k != "VLOCAL_AUDIO_DEVICE"}
        with mock.patch.dict(sys.modules, {"sounddevice": sd}), \
                mock.patch.dict(os.environ, env, clear=True):
            engine.VlocalEngine._log_input_device(object.__new__(engine.VlocalEngine))
        sd.query_devices.assert_not_called()


if __name__ == "__main__":
    unittest.main()
