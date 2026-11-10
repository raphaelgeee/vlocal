"""Lot micro : le worker (process --mic-worker) sans micro réel.

Couvre : file de sortie découplée et dépassements (critère 4), suivi du
micro par défaut et résolution par nom (critère 1), repli à la fréquence
native avec rééchantillonnage (critère 6).
"""
import json
import os
import queue
import struct
import threading
import time
import unittest

import numpy as np

from test_mic_fakes import FakeCoreAudio, FakeSD, device

import micworker


class WorkerHarness:
    """Le vrai MicWorker dans un thread ; sa sortie cadrée est lue (ou pas,
    pour simuler un parent affamé) depuis un vrai pipe."""

    def __init__(self, sd, ca=None, queue_max_s=None, read=True):
        self.r, self.w = os.pipe()
        self.lines = queue.Queue()
        self.events = queue.Queue()
        self.audio = []
        self.worker = micworker.MicWorker(sd, self.w, ca, queue_max_s=queue_max_s)
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()
        self._rd = None
        if read:
            self.start_reading()

    def _run(self):
        try:
            self.worker.run(iter(self.lines.get, None))
        finally:
            os.close(self.w)

    def start_reading(self):
        self._rd = threading.Thread(target=self._read, daemon=True)
        self._rd.start()

    def _read(self):
        f = os.fdopen(self.r, "rb")
        while True:
            head = f.read(5)
            if len(head) < 5:
                break
            typ, ln = head[:1], struct.unpack(">I", head[1:])[0]
            payload = f.read(ln)
            if typ == b"A":
                self.audio.append(np.frombuffer(payload, dtype=np.float32))
            else:
                self.events.put(json.loads(payload))
        f.close()

    def send(self, **cmd):
        self.lines.put(json.dumps(cmd))

    def wait_event(self, name, timeout=3.0):
        deadline = time.monotonic() + timeout
        seen = []
        while time.monotonic() < deadline:
            try:
                ev = self.events.get(timeout=0.05)
            except queue.Empty:
                continue
            seen.append(ev)
            if ev.get("ev") == name:
                return ev, seen
        raise AssertionError(f"événement {name!r} absent ; reçus : {seen}")

    def close(self):
        self.lines.put(None)
        self._thr.join(3.0)
        if self._rd is not None:
            self._rd.join(3.0)


def _wait_stream(sd, n=1, timeout=3.0):
    deadline = time.monotonic() + timeout
    while len(sd.streams) < n:
        if time.monotonic() > deadline:
            raise AssertionError("flux jamais ouvert")
        time.sleep(0.01)
    return sd.streams[n - 1]


class TestFileDeSortieDecouplee(unittest.TestCase):
    """Critère 4 : parent bloqué 2 s -> callback jamais bloquant, pertes comptées."""

    def test_parent_bloque_deux_secondes(self):
        sd = FakeSD([device("Micro MacBook Pro")], "Micro MacBook Pro")
        h = WorkerHarness(sd, queue_max_s=0.5, read=False)   # personne ne lit
        h.send(cmd="start", sr=16000)
        st = _wait_stream(sd)
        bloc = np.full(st.blocksize, 0.05, dtype=np.float32).tobytes()
        n_blocs = 44                     # 2,2 s au rythme réel
        for k in range(n_blocs):
            st.call(bloc, 1 if k in (3, 30) else 0)
            time.sleep(0.05)
        self.assertLess(st.max_cb_s, 0.005, "le callback temps réel a attendu")
        h.start_reading()                # le parent se remet à lire
        h.send(cmd="stop")
        stopped, _ = h.wait_event("stopped")
        self.assertEqual(stopped["xruns"], 2)
        self.assertGreater(stopped["dropped"], 0)
        recus = len(h.audio)
        self.assertEqual(recus + stopped["dropped"], n_blocs, "chaque bloc est livré ou compté")
        h.close()

    def test_parent_sain_aucune_perte_et_stopped_apres_le_dernier_bloc(self):
        sd = FakeSD([device("Micro MacBook Pro")], "Micro MacBook Pro")
        h = WorkerHarness(sd)
        h.send(cmd="start", sr=16000)
        h.wait_event("started")
        st = sd.streams[0]
        st.feed(np.full(16000, 0.1, dtype=np.float32))
        h.send(cmd="stop")
        stopped, _ = h.wait_event("stopped")
        # L'événement arrive APRÈS le dernier chunk : tout est déjà là.
        self.assertEqual(sum(len(a) for a in h.audio), 16000)
        self.assertEqual((stopped["xruns"], stopped["dropped"]), (0, 0))
        h.close()


class TestPeripheriques(unittest.TestCase):
    """Critère 1 : le micro par défaut suivi, les noms résolus dans le worker."""

    MAC = "Micro MacBook Pro"
    AIRPODS = "AirPods Pro de Raphaël"

    def test_nouveau_defaut_vu_a_l_ouverture_sans_ecouteur(self):
        sd = FakeSD([device(self.MAC)], self.MAC)
        ca = FakeCoreAudio(sd)                       # pas d'écouteur : filet UID seul
        h = WorkerHarness(sd, ca)
        h.send(cmd="start", sr=16000)
        h.wait_event("started")
        h.send(cmd="stop")
        h.wait_event("stopped")
        # AirPods connectés entre deux appuis : PortAudio ne les connaît pas.
        ca.change_default([device(self.MAC), device(self.AIRPODS)], self.AIRPODS)
        h.send(cmd="start", sr=16000)
        started, seen = h.wait_event("started")
        self.assertEqual(sd.streams[-1].name, self.AIRPODS)
        self.assertEqual(started["device"], self.AIRPODS)
        self.assertIn({"ev": "refreshed", "reason": "default_changed"}, seen)
        self.assertFalse([e for e in seen if e.get("ev") == "error"])
        h.close()

    def test_ecouteur_rafraichit_au_repos_avant_l_appui(self):
        sd = FakeSD([device(self.MAC)], self.MAC)
        ca = FakeCoreAudio(sd, listen=True)
        h = WorkerHarness(sd, ca)
        ca.change_default([device(self.MAC), device(self.AIRPODS)], self.AIRPODS)
        ev, _ = h.wait_event("refreshed")        # sans aucun appui
        self.assertEqual(ev["reason"], "device_change")
        self.assertEqual(sd.inits, 1)
        h.send(cmd="start", sr=16000)
        h.wait_event("started")
        self.assertEqual(sd.streams[-1].name, self.AIRPODS)
        self.assertEqual(sd.inits, 1, "l'appui n'a pas payé de seconde réinit")
        h.close()

    def test_micro_choisi_resolu_par_nom_dans_le_worker(self):
        usb = "Yeti USB"
        sd = FakeSD([device(self.MAC), device("Haut-parleurs", inputs=0), device(usb)], self.MAC)
        h = WorkerHarness(sd)
        h.send(cmd="start", device=usb, sr=16000)
        started, _ = h.wait_event("started")
        self.assertEqual((sd.streams[-1].name, started["fallback"]), (usb, False))
        h.send(cmd="stop")
        h.wait_event("stopped")
        h.send(cmd="start", device="Micro débranché", sr=16000)
        started, _ = h.wait_event("started")
        self.assertEqual((sd.streams[-1].name, started["fallback"]), (self.MAC, True))
        h.close()

    def test_liste_des_micros_integre_en_tete(self):
        sd = FakeSD([device("Yeti USB"), device("Haut-parleurs", inputs=0),
                     device(self.MAC)], "Yeti USB")
        h = WorkerHarness(sd)
        h.send(cmd="devices")
        ev, _ = h.wait_event("devices")
        self.assertEqual(ev["names"], [self.MAC, "Yeti USB"])
        h.close()


class TestFrequenceNative(unittest.TestCase):
    """Critère 6 : 16 kHz refusé -> 48 kHz rééchantillonné vers 16 kHz."""

    def test_ouverture_48k_et_reechantillonnage(self):
        teams = "Microsoft Teams Audio"
        sd = FakeSD([device(teams, rate=48000)], teams, refuse_rates={16000})
        h = WorkerHarness(sd)
        h.send(cmd="start", sr=16000)
        started, _ = h.wait_event("started")
        st = sd.streams[0]
        self.assertEqual((st.rate, started["sr_native"]), (48000, 48000))
        self.assertEqual(st.blocksize, 2400)
        t = np.arange(48000) / 48000.0
        st.feed((0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32))
        h.send(cmd="stop")
        h.wait_event("stopped")
        out = np.concatenate(h.audio)
        self.assertEqual(len(out), 16000)                   # 1 s à 16 kHz
        spectre = np.abs(np.fft.rfft(out[1000:]))           # hors amorce du filtre
        freqs = np.fft.rfftfreq(len(out) - 1000, 1 / 16000.0)
        self.assertAlmostEqual(freqs[int(np.argmax(spectre))], 440.0, delta=2.0)
        self.assertAlmostEqual(float(np.max(np.abs(out[1000:]))), 0.3, delta=0.02)
        h.close()

    def test_refus_sans_frequence_alternative_rend_l_erreur_d_origine(self):
        sd = FakeSD([device("Micro", rate=16000)], "Micro", refuse_rates={16000})
        h = WorkerHarness(sd)
        h.send(cmd="start", sr=16000)
        ev, _ = h.wait_event("error")
        self.assertIn("Invalid sample rate", ev["err"])
        h.close()

    def test_reechantillonnage_par_morceaux_sans_couture(self):
        rs_un = micworker.StreamResampler(44100, 16000)
        rs_morceaux = micworker.StreamResampler(44100, 16000)
        x = np.random.default_rng(0).standard_normal(44100).astype(np.float32) * 0.1
        tout = rs_un.process(x)
        par_morceaux = np.concatenate([rs_morceaux.process(x[i:i + 1003])
                                       for i in range(0, len(x), 1003)])
        self.assertEqual(len(tout), len(par_morceaux))
        self.assertTrue(np.allclose(tout, par_morceaux, atol=1e-5))
        self.assertLessEqual(abs(len(tout) - 16000), 1)


if __name__ == "__main__":
    unittest.main()


class WorkerStdoutProtocolTests(unittest.TestCase):
    """fd 1 du worker ne porte que des trames, même quand QoS et CoreAudio échouent."""
    SCRIPT = r"""
import ctypes, sys, types
sys.path.insert(0, sys.argv[1])
import micworker

class _Pthread:
    def pthread_set_qos_class_self_np(self, *a):
        return 22                       # refus de la QoS : avertissement attendu

def _no_coreaudio():
    raise OSError("CoreAudio absent")

ctypes.CDLL = lambda *a, **k: _Pthread()
micworker.CoreAudio = _no_coreaudio
micworker.sys.platform = "darwin"
sys.modules["sounddevice"] = types.ModuleType("sounddevice")
micworker.MicWorker._default_uid = lambda self: None
micworker.set_thread_qos(micworker.QOS_USER_INTERACTIVE)
sys.exit(micworker.worker_main([]))
"""

    def test_only_frames_on_stdout(self):
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-c", self.SCRIPT, root], input=b"",
                           capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace"))
        out, events = r.stdout, []
        while out:
            typ, n = out[:1], struct.unpack(">I", out[1:5])[0]
            self.assertIn(typ, (b"J", b"A"), out[:80])
            if typ == b"J":
                events.append(json.loads(out[5:5 + n])["ev"])
            out = out[5 + n:]
        self.assertIn("ready", events)
        err = r.stderr.decode(errors="replace")
        self.assertIn("CoreAudio indisponible", err)
        self.assertIn("[qos]", err)


class DeviceNameAtTests(unittest.TestCase):
    def test_index_maps_to_coreaudio_device_order(self):
        ca = micworker.CoreAudio.__new__(micworker.CoreAudio)
        ca._device_ids = lambda: [40, 41]
        ca._get_str = lambda dev, sel: {40: "Haut-parleurs", 41: "Micro USB"}[dev]
        self.assertEqual(ca.device_name_at(1), "Micro USB")
        self.assertIsNone(ca.device_name_at(2))
        self.assertIsNone(ca.device_name_at(-1))
