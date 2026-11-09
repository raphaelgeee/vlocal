"""Lot micro : le VRAI DictationController de app.py, sur le VRAI moteur de
capture (engine.py) et le VRAI client du worker (micworker.RemoteMic), avec
sounddevice, CoreAudio et le process worker simulés. Aucun micro ouvert.

Bout en bout : changement de défaut entre deux appuis (critère 1),
Bluetooth lent et état « connexion » (2), appui pendant une réparation de
fond (3), parent affamé et un seul mic_overflow (4), assertion anti-App Nap
avant l'ouverture et préchargement différé (E), réglage de micro appliqué à
la dictée.
"""
import sys
import logging
import threading
import time
import types
import unittest
from unittest import mock

import numpy as np

from test_incidents import _JournalCapture, _charge_depuis_app
from test_mic_fakes import FakeCoreAudio, FakeSD, ProcFactory, bare_engine, device

import errors
import micworker

E = errors.E
MAC = "Micro MacBook Pro"
AIRPODS = "AirPods Pro de Raphaël"


class _Overlay:
    def __init__(self, journal):
        self.j = journal

    def info(self, titre, sous_titre):
        self.j.append(("info", titre))

    def recording(self):
        self.j.append(("recording",))

    def level(self, rms):
        pass


class _Base(unittest.TestCase):

    def monte(self, sd, ca=None, delays=(0.0,), reglages=None, queue_max_s=None):
        self.journal = []
        self.reglages = {"dictation_speed": "rapide"}
        self.reglages.update(reglages or {})
        ns = {"threading": threading, "time": time, "VlocalEngine": object, "_EV": errors,
              "_mic_fail_count": [0], "_mic_grabber_name": lambda: None,
              "_toast": lambda m, kind="info": self.journal.append(("toast", m)),
              "_ui": lambda code: None, "overlay": _Overlay(self.journal),
              "_te": lambda t: t,
              "_begin_activity": lambda nom: self.journal.append(("activity",)) or "jeton",
              "_end_activity": lambda tok: self.journal.append(("end_activity", tok)),
              "_load_settings": lambda: self.reglages, "_DictationLiveTail": None}
        _charge_depuis_app({"_time_left", "_DictationStop", "DictationController",
                            "_LOOPBACK_KW"}, ns)
        self.fab = ProcFactory(sd, ca, delays, queue_max_s)
        self.rm = micworker.RemoteMic(16000, spawn=self.fab)
        self.eng = bare_engine(self.rm)
        self.addCleanup(self.fab.shutdown)
        c = ns["DictationController"](self.eng)
        self.addCleanup(c._stop_evt.set)
        return c

    def appui(self, c):
        t0 = time.monotonic()
        ok = c.start()
        return ok, time.monotonic() - t0

    def relache(self, c):
        """Ce que stop_and_finalize fait de la capture (sans transcription)."""
        c._active = False
        c._stop_evt.set()
        self.eng.stop_recording()
        c._after_capture(c._stop_evt)
        c._release_activity()


class TestChangementDeDefaut(_Base):
    """Critère 1 : AirPods connectés entre deux appuis -> le 2e les ouvre."""

    def test_deuxieme_appui_ouvre_le_nouveau_defaut_sans_incident(self):
        sd = FakeSD([device(MAC)], MAC)
        ca = FakeCoreAudio(sd, transports={MAC: "builtin", AIRPODS: "bluetooth"})
        c = self.monte(sd, ca)
        self.eng.prewarm_mic_worker()
        with _JournalCapture() as j:
            ok1, _ = self.appui(c)
            self.relache(c)
            ca.change_default([device(MAC), device(AIRPODS)], AIRPODS)
            ok2, _ = self.appui(c)
            self.relache(c)
        self.assertTrue(ok1 and ok2)
        self.assertEqual([s.name for s in sd.streams], [MAC, AIRPODS])
        self.assertEqual(j.evts, [], "0 incident")
        self.assertEqual(len(self.fab.procs), 1, "sans respawn : PortAudio réinitialisé dans le worker")

    def test_micro_choisi_dans_les_reglages_sert_aussi_a_la_dictee(self):
        usb = "Yeti USB"
        sd = FakeSD([device(MAC), device(usb)], MAC)
        c = self.monte(sd, reglages={"audio_device": 0, "audio_device_name": usb})
        ok, _ = self.appui(c)
        self.relache(c)
        self.assertTrue(ok)
        self.assertEqual(sd.streams[-1].name, usb)

    def test_micro_choisi_absent_repli_sur_le_defaut(self):
        sd = FakeSD([device(MAC)], MAC)
        c = self.monte(sd, reglages={"audio_device_name": "Yeti USB"})
        with _JournalCapture() as j:
            ok, _ = self.appui(c)
            self.relache(c)
        self.assertTrue(ok)
        self.assertEqual((sd.streams[-1].name, j.evts), (MAC, []))

    def test_peripherique_de_capture_systeme_ignore_pour_la_dictee(self):
        sd = FakeSD([device(MAC), device("BlackHole 2ch")], MAC)
        c = self.monte(sd, reglages={"audio_device_name": "BlackHole 2ch"})
        self.appui(c)
        self.relache(c)
        self.assertEqual(sd.streams[-1].name, MAC)


class TestBluetoothLent(_Base):
    """Critère 2 : ouverture de 3 s en Bluetooth -> dictée réussie, « connexion »."""

    def test_dictee_reussie_et_connexion_affichee(self):
        sd = FakeSD([device(AIRPODS)], AIRPODS, open_delay={AIRPODS: 3.0}, auto_feed=True)
        ca = FakeCoreAudio(sd, transports={AIRPODS: "bluetooth"})
        c = self.monte(sd, ca)
        self.eng.prewarm_mic_worker()
        with _JournalCapture() as j:
            ok, dt = self.appui(c)
            self.assertTrue(ok)
            self.assertGreaterEqual(dt, 3.0)
            # Pas de faux flux mort : l'horloge ne part qu'au premier échantillon.
            self.assertLess(self.eng.seconds_since_audio(), 1.0)
            time.sleep(0.3)
            self.relache(c)
        self.assertEqual(self.journal[:3],
                         [("activity",), ("info", "Connexion du micro"), ("recording",)])
        self.assertEqual(j.evts, [])

    def test_flux_mort_compte_a_partir_du_premier_echantillon(self):
        sd = FakeSD([device(MAC)], MAC)                  # aucun bloc livré
        c = self.monte(sd)
        self.appui(c)
        time.sleep(0.2)
        self.assertEqual(self.eng.seconds_since_audio(), 0.0)
        sd.streams[-1].feed(np.zeros(800, np.float32))
        deadline = time.monotonic() + 2.0
        while self.eng.recorded_samples() < 800 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        self.assertGreater(self.eng.seconds_since_audio(), 0.1)
        self.relache(c)


class TestAppuiPendantReparation(_Base):
    """Critère 3 : réparation de fond de 5 s, l'appui démarre dans son budget."""

    def test_appui_dans_le_budget_un_seul_worker(self):
        sd = FakeSD([device(MAC)], MAC)
        c = self.monte(sd, delays=(0.0, 5.0, 0.0))
        self.eng.prewarm_mic_worker()
        c._repair_mic_in_background()            # hard_mic_reset(timeout=5.0) en fond
        time.sleep(0.3)
        with _JournalCapture() as j:
            ok, dt = self.appui(c)
            self.relache(c)
        self.assertTrue(ok)
        self.assertLessEqual(dt, 2.0)
        self.assertEqual(j.evts, [])
        time.sleep(0.2)                          # la réparation a cédé, rien ne la relance
        self.assertEqual(len(self.fab.alive()), 1, "un seul worker vivant")
        self.assertTrue(self.rm.alive())


class TestParentAffame(_Base):
    """Critère 4 : lecteur du parent bloqué 2 s -> un seul mic_overflow."""

    def test_un_seul_incident_par_dictee(self):
        sd = FakeSD([device(MAC)], MAC, auto_feed=True)
        c = self.monte(sd, queue_max_s=0.3)
        with _JournalCapture() as j:
            ok, _ = self.appui(c)
            self.assertTrue(ok)
            with self.eng._lock:                 # le lecteur du parent est bloqué
                time.sleep(2.0)
            time.sleep(0.2)
            self.relache(c)
            # Dictée suivante saine : aucun incident de plus.
            sd.auto_feed = False
            self.appui(c)
            self.relache(c)
        st = sd.streams[0]
        self.assertLess(st.max_cb_s, 0.005, "le callback n'a jamais attendu")
        overflow = [e for e in j.evts if e[0] == E.MIC_OVERFLOW]
        self.assertEqual(len(overflow), 1)
        self.assertGreater(overflow[0][2]["dropped"], 0)
        self.assertEqual(len(j.evts), 1)


class TestChargeAuDemarrage(_Base):
    """E : anti-App Nap AVANT l'ouverture, préchargement après CAPTURE_SETTLE_S."""

    def test_ordre_et_delai(self):
        sd = FakeSD([device(MAC)], MAC)
        c = self.monte(sd)
        ouvertures = []
        vrai_start = self.eng.start_recording

        def _start(**kw):
            ouvertures.append(len(self.journal))
            return vrai_start(**kw)
        self.eng.start_recording = _start
        prechauffes = []
        self.eng.prewarm = lambda want_small=True: prechauffes.append(time.monotonic())
        t0 = time.monotonic()
        self.appui(c)
        self.assertEqual(self.journal[ouvertures[0] - 1], ("activity",))
        time.sleep(0.1)
        self.assertEqual(prechauffes, [], "pas de chargement dans les premières ms")
        time.sleep(c.CAPTURE_SETTLE_S + 0.2)
        self.assertEqual(len(prechauffes), 1)
        self.assertGreaterEqual(prechauffes[0] - t0, c.CAPTURE_SETTLE_S)
        self.relache(c)

    def test_appui_rate_rend_l_assertion(self):
        sd = FakeSD([device(MAC)], MAC)
        c = self.monte(sd)
        self.eng.start_recording = lambda **kw: False      # capture précédente coincée
        c._mic_fail_count = [0]
        ok, _ = self.appui(c)
        self.assertFalse(ok)
        self.assertIn(("end_activity", "jeton"), self.journal)
        self.assertIsNone(c._activity)


class TestReveilEtFluxMort(_Base):
    """Veille et flux mort : le worker est renouvelé AU REPOS, pas à l'appui."""

    def test_reveil_renouvelle_le_worker_au_repos(self):
        sd = FakeSD([device(MAC)], MAC)
        c = self.monte(sd)
        self.eng.prewarm_mic_worker()
        recu = {}

        class _Centre:
            def addObserverForName_object_queue_usingBlock_(self, nom, obj, file, bloc):
                recu["nom"], recu["bloc"] = nom, bloc
                return "jeton"

        appkit = types.ModuleType("AppKit")
        appkit.NSWorkspaceDidWakeNotification = "NSWorkspaceDidWakeNotification"
        appkit.NSWorkspace = types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(
            notificationCenter=lambda: _Centre()))
        ns = {"threading": threading, "log": logging.getLogger("vlocal.lifecycle")}
        _charge_depuis_app({"WAKE_SETTLE_S", "_wake_observers", "_watch_system_wake",
                            "_renew_mic_after_wake"}, ns)
        ns["WAKE_SETTLE_S"] = 0.05
        with mock.patch.dict(sys.modules, {"AppKit": appkit}):
            ns["_watch_system_wake"](self.eng)
        self.assertEqual((recu["nom"], ns["_wake_observers"]),
                         ("NSWorkspaceDidWakeNotification", ["jeton"]))
        recu["bloc"](None)                       # macOS sort de veille
        deadline = time.monotonic() + 3.0
        while len(self.fab.procs) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.fab.procs), 2)
        ok, _ = self.appui(c)
        self.relache(c)
        self.assertTrue(ok)
        self.assertEqual(len(self.fab.alive()), 1)

    def test_reveil_pendant_une_dictee_ne_la_coupe_pas(self):
        sd = FakeSD([device(MAC)], MAC)
        c = self.monte(sd)
        self.appui(c)
        self.eng.mic_after_wake()
        self.assertEqual(len(self.fab.procs), 1)
        self.relache(c)

    def test_flux_mort_reparation_lancee_a_la_fin_de_la_dictee(self):
        sd = FakeSD([device(MAC)], MAC)
        c = self.monte(sd)
        self.appui(c)
        c._stop_evt.stream_dead = True           # détecté par le worker de niveau
        c._mic_lost = True
        self.relache(c)
        deadline = time.monotonic() + 3.0
        while not (len(self.fab.procs) == 2 and self.rm.alive()) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.fab.procs), 2)
        self.assertFalse(c._mic_lost, "l'appui suivant ne repaie pas la réparation")
        ok, dt = self.appui(c)
        self.relache(c)
        self.assertTrue(ok)
        self.assertEqual(len(self.fab.procs), 2)

    def test_flux_mort_non_repare_l_appui_ne_preleve_pas_de_delai_fixe(self):
        sd = FakeSD([device(MAC)], MAC)
        c = self.monte(sd)
        self.eng.prewarm_mic_worker()
        c._mic_lost = True                       # dictée annulée avant réparation
        ok, dt = self.appui(c)
        self.relache(c)
        self.assertTrue(ok)
        self.assertLess(dt, 0.5)
        self.assertEqual(len(self.fab.procs), 2, "worker neuf lancé puis rejoint")
        self.assertEqual(len(self.fab.alive()), 1)


class TestBilanDeCapture(unittest.TestCase):
    def test_sans_perte_aucun_incident(self):
        ns = {"threading": threading, "_EV": errors, "VlocalEngine": object}
        _charge_depuis_app({"_DictationStop", "DictationController"}, ns)
        c = ns["DictationController"].__new__(ns["DictationController"])
        c.engine = types.SimpleNamespace(last_capture_stats={"xruns": 0, "dropped": 0})
        with _JournalCapture() as j:
            c._after_capture(ns["_DictationStop"]())
        self.assertEqual(j.evts, [])

    def test_mic_overflow_transmis_sans_contexte(self):
        import telemetry
        code = telemetry.incident_code({"code": E.MIC_OVERFLOW, "level": "warning",
                                        "ctx": {"xruns": 3, "dropped": 12}})
        self.assertEqual(code, "mic_overflow")


if __name__ == "__main__":
    unittest.main()
