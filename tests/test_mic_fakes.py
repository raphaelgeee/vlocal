"""Doublures du lot micro : sounddevice, CoreAudio et process worker simulés.

Aucun micro réel n'est ouvert, aucun process n'est lancé : le VRAI MicWorker
(micworker.py) tourne dans un thread, relié au VRAI RemoteMic par de vrais
pipes (os.pipe), exactement comme le process --mic-worker l'est par stdin et
stdout. Ce module ne contient pas de test ; il est importé par les test_mic_*.
"""
import os
import subprocess
import sys
import threading
import time

import numpy as np

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

import micworker  # noqa: E402


class FakePortAudioError(Exception):
    pass


class FakeStream:
    """Flux d'entrée simulé. `feed()` joue le rôle du thread CoreAudio ; en
    mode automatique, un thread livre des blocs au rythme réel."""

    def __init__(self, sd, name, rate, blocksize, callback):
        self.sd, self.name, self.rate = sd, name, rate
        self.blocksize, self.callback = blocksize, callback
        self.started = self.closed = False
        self.max_cb_s = 0.0
        self.cb_calls = 0
        self._halt = threading.Event()

    def start(self):
        self.started = True
        if self.sd.auto_feed:
            threading.Thread(target=self._auto, daemon=True).start()

    def stop(self):
        self._halt.set()

    def close(self):
        self._halt.set()
        self.closed = True

    def call(self, block, status=0):
        t0 = time.perf_counter()
        self.callback(block, len(block) // 4, None, status)
        self.max_cb_s = max(self.max_cb_s, time.perf_counter() - t0)
        self.cb_calls += 1

    def feed(self, samples, statuses=()):
        """Livre `samples` (float32) par blocs ; `statuses` = indices des blocs
        livrés avec un drapeau de dépassement."""
        samples = np.asarray(samples, dtype=np.float32)
        for k, i in enumerate(range(0, len(samples), self.blocksize)):
            self.call(samples[i:i + self.blocksize].tobytes(), 1 if k in statuses else 0)

    def _auto(self):
        block = np.full(self.blocksize, self.sd.level, dtype=np.float32).tobytes()
        period = self.blocksize / float(self.rate)
        nxt = time.monotonic()
        while not self._halt.is_set():
            self.call(block)
            nxt += period
            self._halt.wait(max(0.0, nxt - time.monotonic()))


class FakeSD:
    """sounddevice simulé. `world` = périphériques réellement présents ;
    `devices` = la vue figée de PortAudio, rafraîchie par _initialize()."""

    PortAudioError = FakePortAudioError

    def __init__(self, world, default, refuse_rates=(), open_delay=None,
                 auto_feed=False, level=0.05):
        self.world = [dict(d) for d in world]
        self.world_default = default
        self.refuse_rates = set(refuse_rates)
        self.open_delay = dict(open_delay or {})   # nom -> secondes
        self.auto_feed = auto_feed
        self.level = level
        self.inits = 0
        self.streams = []
        self._snapshot()

    def _snapshot(self):
        self.devices = [dict(d) for d in self.world]
        self.default = self.world_default

    def plug(self, world, default):
        """Le monde change (AirPods connectés...) ; PortAudio ne le voit pas."""
        self.world = [dict(d) for d in world]
        self.world_default = default

    def _terminate(self):
        pass

    def _initialize(self):
        self.inits += 1
        self._snapshot()

    def query_devices(self, device=None, kind=None):
        if kind == "input":
            for d in self.devices:
                if d["name"] == self.default:
                    return dict(d)
            raise FakePortAudioError("aucun périphérique d'entrée par défaut")
        if device is not None:
            return dict(self.devices[device])
        return [dict(d) for d in self.devices]

    def RawInputStream(self, samplerate, channels, dtype, blocksize, callback, device=None):
        name = self.devices[device]["name"] if device is not None else self.default
        if name not in [d["name"] for d in self.world]:
            raise FakePortAudioError(f"périphérique disparu : {name}")
        if samplerate in self.refuse_rates:
            raise FakePortAudioError("Invalid sample rate [PaErrorCode -9997]")
        delay = self.open_delay.get(name, 0.0)
        if delay:
            time.sleep(delay)
        s = FakeStream(self, name, samplerate, blocksize, callback)
        self.streams.append(s)
        return s


def device(name, rate=48000, inputs=1):
    return {"name": name, "max_input_channels": inputs, "default_samplerate": float(rate)}


class FakeCoreAudio:
    """CoreAudio simulé : défaut courant, transports, écouteur de changements."""

    def __init__(self, sd, transports=None, listen=False):
        self.sd = sd
        self.transports = dict(transports or {})
        self.listen = listen
        self.callbacks = []

    def default_input(self):
        name = self.sd.world_default
        return {"uid": name, "name": name, "transport": self.transports.get(name)}

    def transport_of(self, name):
        return self.transports.get(name)

    def on_change(self, callback):
        if not self.listen:
            return False
        self.callbacks.append(callback)
        return True

    def change_default(self, world, default):
        self.sd.plug(world, default)
        for cb in self.callbacks:
            cb()


class FakeProc:
    """Process --mic-worker simulé : le vrai MicWorker dans un thread, relié
    par de vrais pipes. `ready_delay` simule un spawn lent (ou muet)."""

    def __init__(self, sd, ca=None, ready_delay=0.0, queue_max_s=None):
        r_in, w_in = os.pipe()
        r_out, self._w_out = os.pipe()
        self.stdin = os.fdopen(w_in, "wb")
        self.stdout = os.fdopen(r_out, "rb")
        self._in = os.fdopen(r_in, "r", encoding="utf-8")
        self.sd, self.ca, self.queue_max_s = sd, ca, queue_max_s
        self.ready_delay = ready_delay
        self.returncode = None
        self.worker = None
        self._killed = threading.Event()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def _run(self):
        try:
            if self._killed.wait(self.ready_delay):
                return
            self.worker = micworker.MicWorker(self.sd, self._w_out, self.ca,
                                              queue_max_s=self.queue_max_s)
            self.worker.run(self._in)
        finally:
            os.close(self._w_out)
            self._in.close()
            if self.returncode is None:
                self.returncode = 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self._killed.set()
        try:
            self.stdin.close()
        except OSError:
            pass

    def wait(self, timeout=None):
        if self._killed.is_set():
            return self.returncode     # SIGKILL : récolté sur-le-champ
        return self.join(timeout)

    def join(self, timeout=None):
        """Attend la fin réelle du thread (nettoyage des tests)."""
        self._thr.join(timeout)
        if self._thr.is_alive():
            raise subprocess.TimeoutExpired("mic-worker simulé", timeout)
        return self.returncode


class ProcFactory:
    """Fabrique de FakeProc pour RemoteMic(spawn=...). `delays` = délais de
    ready des spawns successifs (le dernier vaut pour la suite)."""

    def __init__(self, sd, ca=None, delays=(0.0,), queue_max_s=None):
        self.sd, self.ca, self.delays, self.queue_max_s = sd, ca, list(delays), queue_max_s
        self.procs = []

    def __call__(self):
        d = self.delays[min(len(self.procs), len(self.delays) - 1)]
        p = FakeProc(self.sd, self.ca, ready_delay=d, queue_max_s=self.queue_max_s)
        self.procs.append(p)
        return p

    def alive(self):
        return [p for p in self.procs if p.poll() is None]

    def shutdown(self):
        for p in self.procs:
            p.kill()
        for p in self.procs:
            try:
                p.join(5.0)
            except subprocess.TimeoutExpired:
                pass


def bare_engine(remote_mic):
    """VlocalEngine sans modèle (le constructeur charge Whisper) : seuls les
    attributs du chemin de capture sont posés, comme __init__ les pose."""
    import engine
    eng = engine.VlocalEngine.__new__(engine.VlocalEngine)
    eng.sample_rate = engine.SAMPLE_RATE
    eng._lock = threading.Lock()
    eng._frames = []
    eng._recording = False
    eng._rec_via_worker = False
    eng._remote_mic = remote_mic
    eng._cap_token = None
    eng._xruns = 0
    eng.last_capture_stats = None
    eng.on_mic_connecting = None
    eng._stream = None
    eng._last_audio_t = 0.0
    eng.prewarm = lambda want_small=True: None   # aucun modèle dans ces tests
    return eng
