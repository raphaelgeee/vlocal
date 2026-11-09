"""Lot micro : le client RemoteMic contre le vrai worker simulé (pipes réels).

Couvre : budget d'ouverture étendu au Bluetooth et état « connexion »
(critère 2), l'appui qui gagne sur une réparation de fond sans laisser deux
workers ni zéro (critère 3), la fin de phrase gardée jusqu'au « stopped ».
"""
import threading
import time
import unittest

import numpy as np

from test_mic_fakes import FakeCoreAudio, FakeSD, ProcFactory, device

import micworker

MAC = "Micro MacBook Pro"
AIRPODS = "AirPods Pro de Raphaël"


class _Base(unittest.TestCase):
    def _rm(self, sd, ca=None, delays=(0.0,), queue_max_s=None):
        self.fab = ProcFactory(sd, ca, delays, queue_max_s)
        self.addCleanup(self.fab.shutdown)
        return micworker.RemoteMic(16000, spawn=self.fab)


class TestBluetooth(_Base):
    """Critère 2 : une bascule HFP de 3 s passe, l'overlay dit « connexion »."""

    def test_ouverture_bluetooth_de_trois_secondes(self):
        sd = FakeSD([device(MAC), device(AIRPODS)], AIRPODS, open_delay={AIRPODS: 3.0})
        ca = FakeCoreAudio(sd, transports={AIRPODS: "bluetooth", MAC: "builtin"})
        rm = self._rm(sd, ca)
        rm.ensure()
        connexion = []
        t0 = time.monotonic()
        rm.start(lambda b: None, budget=2.0, on_connecting=lambda: connexion.append(1))
        dt = time.monotonic() - t0
        self.assertGreaterEqual(dt, 3.0)
        self.assertLess(dt, rm.START_TIMEOUT_SLOW)
        self.assertEqual(connexion, [1], "état « connexion » affiché une fois")
        self.assertEqual(rm.last_open["device"], AIRPODS)
        rm.stop()

    def test_meme_lenteur_sans_bluetooth_reste_un_figeage(self):
        lent = "Interface USB"
        sd = FakeSD([device(lent)], lent, open_delay={lent: 3.0})
        rm = self._rm(sd, FakeCoreAudio(sd, transports={lent: "usb"}))
        rm.ensure()
        connexion = []
        t0 = time.monotonic()
        with self.assertRaises(micworker.WorkerUnavailable):
            rm.start(lambda b: None, budget=2.0, on_connecting=lambda: connexion.append(1))
        self.assertLess(time.monotonic() - t0, 2.3)
        self.assertEqual(connexion, [1], "au-delà de 0,5 s, la pilule le dit aussi")
        self.assertFalse(rm.alive(), "worker figé tué")

    def test_transport_inconnu_l_historique_suffit(self):
        lent = "Casque inconnu"
        sd = FakeSD([device(lent)], lent, open_delay={lent: 1.4})
        rm = self._rm(sd)                                  # pas de CoreAudio
        rm.ensure()
        rm.start(lambda b: None, budget=2.0)               # 1,4 s : passe, et marque
        rm.stop()
        sd.open_delay[lent] = 3.0
        rm.start(lambda b: None, budget=2.0)               # 3 s : passe grâce à l'historique
        rm.stop()


class TestAppuiPrioritaire(_Base):
    """Critère 3 : appui pendant une réparation de fond de 5 s."""

    def _reparation_puis_appui(self, delais):
        sd = FakeSD([device(MAC)], MAC)
        rm = self._rm(sd, delays=delais)
        rm.ensure()                                        # worker chaud du démarrage
        fond = {}
        th = threading.Thread(target=lambda: fond.setdefault("ok", rm.respawn(timeout=5.0)))
        th.start()
        time.sleep(0.3)                                    # la réparation est en vol
        t0 = time.monotonic()
        with rm.press():
            rm.start(lambda b: None, budget=2.0)
        dt = time.monotonic() - t0
        th.join(1.0)
        return rm, dt, fond, th

    def test_reparation_muette_l_appui_relance_et_gagne(self):
        rm, dt, fond, th = self._reparation_puis_appui((0.0, 5.0, 0.0))
        self.assertLessEqual(dt, 2.0)
        self.assertFalse(th.is_alive(), "la réparation a cédé sans attendre ses 5 s")
        self.assertFalse(fond["ok"])
        self.assertEqual(len(self.fab.alive()), 1, "un seul worker vivant")
        self.assertTrue(rm.alive())
        rm.stop()

    def test_reparation_presque_prete_l_appui_la_rejoint(self):
        rm, dt, fond, th = self._reparation_puis_appui((0.0, 0.6, 0.0))
        self.assertLessEqual(dt, 1.0)
        self.assertEqual(len(self.fab.procs), 2, "aucun spawn de plus : rejoint")
        self.assertEqual(len(self.fab.alive()), 1)
        rm.stop()

    def test_aucune_reparation_de_fond_pendant_une_capture(self):
        sd = FakeSD([device(MAC)], MAC)
        rm = self._rm(sd)
        rm.start(lambda b: None, budget=2.0)
        self.assertFalse(rm.respawn(timeout=1.0))
        rm.heal()
        self.assertEqual(len(self.fab.procs), 1, "la capture n'a pas été tuée")
        rm.stop()
        self.assertTrue(rm.respawn(timeout=2.0))
        self.assertEqual(len(self.fab.alive()), 1)

    def test_verrou_pris_au_dela_du_budget(self):
        sd = FakeSD([device(MAC)], MAC)
        rm = self._rm(sd)
        rm.ensure()
        pris, lache = threading.Event(), threading.Event()

        def _tient():
            with rm._lock:
                pris.set()
                lache.wait(5.0)
        threading.Thread(target=_tient, daemon=True).start()
        pris.wait()
        t0 = time.monotonic()
        with self.assertRaises(micworker.WorkerUnavailable):
            rm.start(lambda b: None, budget=0.5)
        self.assertLess(time.monotonic() - t0, 0.8)
        lache.set()


class TestFinDePhrase(_Base):
    def test_les_derniers_blocs_arrivent_avant_le_retour_du_stop(self):
        sd = FakeSD([device(MAC)], MAC)
        rm = self._rm(sd)
        recus = []
        rm.start(recus.append, budget=2.0)
        st = sd.streams[-1]
        st.feed(np.full(8000, 0.2, dtype=np.float32))
        rm.stop()
        self.assertEqual(sum(len(b) for b in recus) // 4, 8000)
        self.assertEqual(rm.last_stats, {"xruns": 0, "dropped": 0})

    def test_liste_des_micros_vue_du_worker(self):
        sd = FakeSD([device(MAC)], MAC)
        rm = self._rm(sd, FakeCoreAudio(sd))
        rm.ensure()
        sd.plug([device(MAC), device(AIRPODS)], AIRPODS)   # PortAudio du worker périmé
        rm._spawn_fn.ca.change_default(sd.world, AIRPODS)
        self.assertEqual(rm.list_devices(), [MAC, AIRPODS])


if __name__ == "__main__":
    unittest.main()
