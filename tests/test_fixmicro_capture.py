"""Correctifs capture micro 1.5.0 (audit micro #1, #5, #6, #7, #10, #13).

Vrai MicWorker contre le vrai RemoteMic par de vrais pipes, sounddevice et
CoreAudio simulés (test_mic_fakes) : aucun micro réel n'est ouvert, aucun
process n'est lancé. Chaque test échoue sur le code d'avant ces correctifs.
"""
import json
import os
import struct
import time
import unittest

import numpy as np

from test_mic_fakes import FakeCoreAudio, FakeSD, ProcFactory, device

import micworker

MAC = "Micro MacBook Pro"
AIRPODS = "AirPods Pro de Raphaël"
SR = 16000


def _secondes(chunks) -> float:
    return sum(len(b) for b in chunks) / 4 / SR


class _Base(unittest.TestCase):
    def _rm(self, sd, ca=None, delays=(0.0,)):
        self.fab = ProcFactory(sd, ca, delays)
        self.addCleanup(self.fab.shutdown)
        rm = micworker.RemoteMic(SR, spawn=self.fab)
        rm.ensure()
        return rm

    @staticmethod
    def _attendre(cond, delai=3.0):
        fin = time.monotonic() + delai
        while not cond() and time.monotonic() < fin:
            time.sleep(0.02)
        return cond()


class TestPeripheriquePerdu(_Base):
    """Micro #1 (bloquant) : périphérique perdu en pleine dictée."""

    def test_rouvert_dans_la_meme_capture_sans_couper_le_parent(self):
        sd = FakeSD([device(MAC), device(AIRPODS)], AIRPODS, auto_feed=True)
        rm = self._rm(sd, FakeCoreAudio(sd))
        recu = []
        rm.start(recu.append, budget=2.0)
        time.sleep(1.0)
        # Les AirPods partent sur l'iPhone : plus aucun rappel, ils
        # disparaissent de la liste, le défaut redevient le micro intégré.
        sd.streams[-1].stop()
        sd.plug([device(MAC)], MAC)
        time.sleep(2.0)
        rm.stop()
        parle = 3.0
        rendu = _secondes(recu)
        trou = (getattr(rm, "last_reroute", None) or {}).get("gap_s")
        print(f"\n[sonde deadmid] rendu {rendu:.2f} s sur {parle:.1f} s parlées, trou {trou} s")
        self.assertGreater(rendu, parle - 0.8)
        self.assertIsNotNone(rm.last_reroute, "aucune réouverture")
        self.assertEqual(rm.last_reroute["device"], MAC)
        self.assertEqual(rm.last_reroute["reason"], "stalled")
        self.assertLess(trou, 0.8)
        self.assertGreater(rm.rerouted_at, 0.0, "contrat engine : horodatage de la bascule")
        self.assertIsNotNone(rm.last_stats, "stop propre, worker non tué")
        self.assertTrue(rm.alive())
        self.assertTrue(all(s.closed for s in sd.streams), "aucun flux laissé ouvert")

    def test_defaut_change_en_dictee_suit_le_nouveau_defaut(self):
        sd = FakeSD([device(MAC)], MAC, auto_feed=True)
        ca = FakeCoreAudio(sd, listen=True)
        rm = self._rm(sd, ca)
        recu = []
        rm.start(recu.append, budget=2.0)
        time.sleep(0.4)
        ca.change_default([device(MAC), device(AIRPODS)], AIRPODS)
        self.assertTrue(self._attendre(lambda: rm.last_reroute is not None))
        self.assertEqual(rm.last_reroute, {"device": AIRPODS, "reason": "default_changed",
                                           "gap_s": rm.last_reroute["gap_s"]})
        n = len(recu)
        time.sleep(0.4)
        self.assertGreater(len(recu), n, "les blocs continuent après la bascule")
        rm.stop()
        self.assertEqual(sd.streams[-1].name, AIRPODS)

    def test_micro_choisi_ne_suit_pas_le_defaut(self):
        sd = FakeSD([device(MAC), device(AIRPODS)], MAC, auto_feed=True)
        ca = FakeCoreAudio(sd, listen=True)
        rm = self._rm(sd, ca)
        rm.start(lambda b: None, device=MAC, budget=2.0)
        time.sleep(0.2)
        ca.change_default([device(MAC), device(AIRPODS)], AIRPODS)
        time.sleep(0.8)
        rm.stop()
        self.assertIsNone(getattr(rm, "last_reroute", None))   # garde de non-régression
        self.assertEqual(len(sd.streams), 1)

    def test_rien_n_est_rouvert_apres_le_stop(self):
        sd = FakeSD([device(MAC)], MAC, auto_feed=True)
        rm = self._rm(sd, FakeCoreAudio(sd))
        rm.start(lambda b: None, budget=2.0)
        time.sleep(0.3)
        rm.stop()
        time.sleep(1.0)
        self.assertEqual(len(sd.streams), 1)
        self.assertTrue(sd.streams[0].closed)


class _CAVolume(FakeCoreAudio):
    def __init__(self, sd, volume):
        super().__init__(sd)
        self.volume = volume

    def input_volume(self):
        if isinstance(self.volume, Exception):
            raise self.volume
        return self.volume


class TestVolumeEntree(_Base):
    """Contrat engine : volume d'entrée lu par le worker (« micro à 0 % »)."""

    def test_volume_lu_par_le_worker(self):
        sd = FakeSD([device(MAC)], MAC)
        self.assertEqual(self._rm(sd, _CAVolume(sd, 0.0)).input_volume(), 0.0)

    def test_volume_illisible_rend_none(self):
        sd = FakeSD([device(MAC)], MAC)
        self.assertIsNone(self._rm(sd, _CAVolume(sd, None)).input_volume())
        self.assertIsNone(self._rm(sd, _CAVolume(sd, OSError("HAL"))).input_volume())
        self.assertIsNone(self._rm(sd, None).input_volume())


class TestOuvertureLente(_Base):
    """Micro #5 : un périphérique non Bluetooth de 2,5 s est appris lent même
    sur l'échec, et l'appui suivant l'ouvre."""

    def test_lenteur_apprise_sur_echec(self):
        lent = "Agrégat Continuity"
        sd = FakeSD([device(lent)], lent, open_delay={lent: 2.5})
        rm = self._rm(sd, FakeCoreAudio(sd, transports={lent: "aggregate"}))
        with self.assertRaises(micworker.WorkerUnavailable):
            rm.start(lambda b: None, budget=2.0)
        self.assertGreaterEqual(rm._slow_devices.get(lent, 0.0), rm.SLOW_OPEN_S)
        rm.ensure()
        t0 = time.monotonic()
        rm.start(lambda b: None, budget=2.0)
        self.assertLess(time.monotonic() - t0, rm.START_TIMEOUT_SLOW)
        rm.stop()


class TestPingPortAudioFige(_Base):
    """Micro #6 : réinitialisation de fond figée (_pa_lock tenu) -> le
    healthcheck doit dire « malade » pour que heal() respawne au repos."""

    def test_ping_sans_reponse_si_portaudio_tenu(self):
        sd = FakeSD([device(MAC)], MAC)
        rm = self._rm(sd)
        self.assertTrue(rm.healthcheck())
        verrou = self.fab.procs[0].worker._pa_lock
        verrou.acquire()
        try:
            self.assertFalse(rm.healthcheck())
        finally:
            verrou.release()
        self.assertTrue(rm.healthcheck())


class TestStopParentAffame(_Base):
    """Micro #7 : parent affamé -> le stop attend tant que les blocs arrivent
    (sonde backlog : 80 blocs sur 80)."""

    def test_tous_les_blocs_livres(self):
        sd = FakeSD([device(MAC)], MAC, auto_feed=True)
        rm = self._rm(sd, FakeCoreAudio(sd))
        recu = []

        def lent(b):
            recu.append(b)
            time.sleep(0.12)          # lecteur 2,4 fois plus lent que le temps réel

        rm.start(lent, budget=2.0)
        time.sleep(4.0)
        t0 = time.monotonic()
        rm.stop()
        duree = time.monotonic() - t0
        produits = sd.streams[-1].cb_calls
        print(f"\n[sonde backlog] {len(recu)} blocs livrés sur {produits}, stop en {duree:.2f} s")
        self.assertEqual(len(recu), produits)
        self.assertGreaterEqual(produits, 78)
        self.assertLessEqual(duree, rm.STOP_MAX + 0.5)
        self.assertIsNotNone(rm.last_stats)
        self.assertTrue(rm.alive(), "worker sain : pas tué")


class TestReechantillonneur(unittest.TestCase):
    """Micro #10 et #13."""

    def _lire(self, fd, attente=1.0):
        os.set_blocking(fd, False)
        data, fin = b"", time.monotonic() + attente
        while time.monotonic() < fin:
            try:
                data += os.read(fd, 65536)
            except BlockingIOError:
                time.sleep(0.02)
        trames, i = [], 0
        while i + 5 <= len(data):
            typ, n = data[i:i + 1], struct.unpack(">I", data[i + 1:i + 5])[0]
            trames.append((typ, data[i + 5:i + 5 + n]))
            i += 5 + n
        return trames

    def test_exception_de_transform_signalee_et_thread_vivant(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        ob = micworker._Outbox(w, 100)

        def casse(_b):
            raise ValueError("boum")

        ob.audio(b"\0" * 8, casse)
        ob.audio(b"\0" * 8, casse)       # même flux : signalé une seule fois
        ob.event(ev="started")
        trames = self._lire(r, 0.5)
        self.assertTrue(ob._thr.is_alive())
        evs = [json.loads(p)["ev"] for t, p in trames if t == b"J"]
        self.assertEqual(evs, ["error", "started"])
        ob.close()
        os.close(w)

    def test_erreur_de_flux_ne_fait_pas_echouer_l_ouverture(self):
        """Le premier bloc casse le rééchantillonneur : l'ouverture réussit
        quand même et le flux ne reste pas ouvert après le stop."""
        sd = FakeSD([device(MAC)], MAC, refuse_rates=(16000,), auto_feed=True)
        fab = ProcFactory(sd, None)
        self.addCleanup(fab.shutdown)
        rm = micworker.RemoteMic(SR, spawn=fab)
        rm.ensure()
        casse = micworker.StreamResampler.process_bytes
        try:
            micworker.StreamResampler.process_bytes = lambda self, d: 1 / 0
            rm.start(lambda b: None, budget=2.0)
            time.sleep(0.3)
            self.assertEqual(rm.list_devices(), [MAC])
            rm.stop()
        finally:
            micworker.StreamResampler.process_bytes = casse
        self.assertEqual(rm.last_open["sr_native"], 48000)
        self.assertIn("rééchantillonnage", rm._payloads["stream_error"]["err"])
        self.assertTrue(all(s.closed for s in sd.streams))

    def test_bloc_vide_ne_produit_rien(self):
        x = np.sin(np.arange(4800, dtype=np.float32) / 10.0)
        a, b = micworker.StreamResampler(48000, 16000), micworker.StreamResampler(48000, 16000)
        sortie_a = np.concatenate([a.process(x[:2400]), a.process(x[2400:])])
        sortie_b = np.concatenate([b.process(x[:2400]), b.process(np.zeros(0, np.float32)),
                                   b.process(x[2400:])])
        self.assertEqual(len(b.process(np.zeros(0, np.float32))), 0)
        np.testing.assert_array_equal(sortie_a, sortie_b)


if __name__ == "__main__":
    unittest.main()
