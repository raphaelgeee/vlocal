"""
Vlocal — v1.0.22 « MICRO BÉTON » : capture micro de la DICTÉE dans un
PROCESS JETABLE.

POURQUOI (terrain, diagnostics 24/07 -> 10/08 sur 1.0.21) : 28 échecs micro
au démarrage, chaque retry in-process échoue aussi, ZÉRO mic_recovered.
Cause structurelle : quand une fermeture/ouverture de flux fige (device tenu,
bascule Bluetooth...), l'auto-heal ABANDONNE le thread figé — mais ce zombie
reste coincé DANS PortAudio. À partir de là, Pa_Terminate/Pa_Initialize opère
sur un sous-système corrompu DANS LE PROCESS : tout échoue jusqu'à la mort du
process. C'est pourquoi relancer l'app répare toujours. Les 3 tentatives
in-process (1.0.9, 1.0.11, 1.0.21) ne pouvaient pas gagner.

PRINCIPE : faire automatiquement, en ~1 s et invisiblement, ce que
l'utilisateur faisait à la main en relançant l'app. Le binaire Vlocal est
re-exécuté avec --mic-worker (même mécanisme que --emb-worker, shim en tête
d'app.py avant tout import lourd) : un process ultra-léger (sounddevice seul,
PAS de numpy ni de modèle) qui ouvre le micro SUR ORDRE et streame le PCM au
parent par pipe. Worker figé ou mort -> kill -9 + respawn : l'état CoreAudio
corrompu meurt avec le process, la récupération est GARANTIE et BORNÉE.

GARANTIES PRODUIT :
  - Chemin de transcription STRICTEMENT inchangé : les frames arrivent dans
    engine._frames comme avant, toujours en float32 mono 16 kHz.
  - Micro FERMÉ au repos (pastille orange uniquement pendant la dictée) : le
    worker chaud ne tient AUCUN flux entre deux dictées.
  - Audio jamais perdu : le PCM est streamé en continu vers le parent, et le
    « stopped » ne part qu'APRÈS le dernier chunk (même file, même ordre).
  - Pas d'orphelin : le worker sort tout seul sur EOF stdin (mort du parent).
  - Repli : si le worker ne peut pas se lancer (env cassé), engine repasse
    sur le chemin in-process historique ; VLOCAL_MIC_PROC=0 désactive tout.

v1.5 — LOT MICRO (« ça se coupe sous tension ») :
  - Périphériques : le worker suit le micro par défaut de CoreAudio (écouteur
    de propriété + comparaison d'UID à chaque ouverture) et réinitialise
    PortAudio hors du chemin de l'appui ; un micro choisi est résolu PAR NOM
    dans le worker, jamais par un index énuméré dans le parent.
  - Bluetooth : le worker annonce le transport AVANT d'ouvrir ; le parent
    étend alors l'attente (bascule HFP de 1 à 3 s) et l'app affiche « connexion ».
  - Pipe découplé : le callback temps réel ne fait qu'un dépôt non bloquant
    dans une file bornée ; un thread d'écriture la vide vers le pipe ; les
    dépassements (status du callback + file pleine) remontent au parent.
  - Fréquence : un périphérique qui refuse 16 kHz est ouvert à sa fréquence
    native et rééchantillonné dans le worker (numpy chargé à ce moment-là
    seulement).
  - Verrou : un appui n'attend jamais une réparation de fond (voir RemoteMic).

PROTOCOLE (volontairement minuscule) :
  parent -> worker : une commande JSON par ligne sur stdin
      {"cmd":"start","device":<nom|null>,"prefer":"builtin"|null,"sr":16000}
      {"cmd":"stop"} {"cmd":"ping"} {"cmd":"devices"} {"cmd":"quit"}
  worker -> parent : messages cadrés sur stdout : 1 octet type + longueur
      uint32 big-endian + payload
      b"A" = chunk audio brut (float32 mono, à la fréquence demandée)
      b"J" = événement JSON utf-8
      Événements : {"ev":"ready"} {"ev":"pong"} {"ev":"error","err":"..."}
      {"ev":"opening","device":nom,"transport":"bluetooth"|...|null}
      {"ev":"started","device":nom,"open_s":s,"sr_native":hz|null,"fallback":bool}
      {"ev":"stopped","xruns":n,"dropped":n}
      {"ev":"devices","names":[...]} {"ev":"refreshed","reason":"..."}
      {"ev":"warn","msg":"..."}
"""

import collections
import contextlib
import ctypes
import json
import os
import struct
import subprocess
import sys
import threading
import time


# ---------------------------------------------------------------------------
# QoS des threads (parent et worker)
# ---------------------------------------------------------------------------

QOS_USER_INTERACTIVE = 0x21
QOS_USER_INITIATED = 0x19
_QOS_WARNED = set()


def set_thread_qos(qos_class: int) -> bool:
    """Place le thread APPELANT dans la classe QoS donnée (macOS). Sur Apple
    Silicon, la QoS décide P-core ou E-core : un thread démoté peut rester
    confiné aux E-cores ~1 GHz et prendre du retard. No-op hors macOS ; un
    refus (thread hors système QoS) est journalisé une fois par classe."""
    try:
        err = ctypes.CDLL(None).pthread_set_qos_class_self_np(int(qos_class), 0)
    except (AttributeError, OSError):
        return False   # pas de libpthread Apple : rien à faire
    if err and qos_class not in _QOS_WARNED:
        _QOS_WARNED.add(qos_class)
        print(f"[qos] pthread_set_qos_class_self_np({qos_class:#x}) a renvoyé {err} "
              "(thread hors système QoS : placement P-core non garanti).")
    return not err


# ---------------------------------------------------------------------------
# CoreAudio (ctypes, lecture seule) : périphérique d'entrée par défaut, nom,
# transport, et écoute de ses changements. Aucun flux n'est ouvert ici.
# ---------------------------------------------------------------------------

def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode("ascii"))[0]


_K_SYSTEM_OBJECT = 1
_K_DEFAULT_INPUT = _fourcc("dIn ")
_K_DEVICES = _fourcc("dev#")
_K_SCOPE_GLOBAL = _fourcc("glob")
_K_DEVICE_UID = _fourcc("uid ")
_K_NAME = _fourcc("lnam")
_K_TRANSPORT = _fourcc("tran")
_K_CF_UTF8 = 0x08000100

# Transports CoreAudio (kAudioDeviceTransportType*) ramenés à un mot stable.
_TRANSPORTS = {
    _fourcc("bltn"): "builtin", _fourcc("usb "): "usb", _fourcc("blue"): "bluetooth",
    _fourcc("blea"): "bluetooth", _fourcc("virt"): "virtual", _fourcc("grup"): "aggregate",
    _fourcc("airp"): "airplay", _fourcc("hdmi"): "hdmi", _fourcc("thun"): "thunderbolt",
}


class _PropAddr(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


_LISTENER_PROC = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.POINTER(_PropAddr), ctypes.c_void_p)


class CoreAudio:
    """Accès minimal à l'objet système CoreAudio. Construit seulement sur
    macOS (OSError sinon). Les lectures renvoient None en cas d'échec plutôt
    que de lever : elles servent à décider, jamais à capturer."""

    def __init__(self):
        self._ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self._cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        get = self._ca.AudioObjectGetPropertyData
        get.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PropAddr), ctypes.c_uint32,
                        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        get.restype = ctypes.c_int32
        size = self._ca.AudioObjectGetPropertyDataSize
        size.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PropAddr), ctypes.c_uint32,
                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        size.restype = ctypes.c_int32
        add = self._ca.AudioObjectAddPropertyListener
        add.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PropAddr), _LISTENER_PROC,
                        ctypes.c_void_p]
        add.restype = ctypes.c_int32
        self._cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                ctypes.c_long, ctypes.c_uint32]
        self._cf.CFStringGetCString.restype = ctypes.c_bool
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._listeners = []   # garde les trampolines ctypes en vie

    def _get(self, obj, selector, ctype):
        addr = _PropAddr(selector, _K_SCOPE_GLOBAL, 0)
        val = ctype()
        sz = ctypes.c_uint32(ctypes.sizeof(val))
        err = self._ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None,
                                                  ctypes.byref(sz), ctypes.byref(val))
        return None if err else val.value

    def _get_str(self, obj, selector):
        ref = self._get(obj, selector, ctypes.c_void_p)
        if not ref:
            return None
        try:
            buf = ctypes.create_string_buffer(1024)
            if self._cf.CFStringGetCString(ref, buf, len(buf), _K_CF_UTF8):
                return buf.value.decode("utf-8", "replace")
            return None
        finally:
            self._cf.CFRelease(ref)

    def _device_ids(self):
        addr = _PropAddr(_K_DEVICES, _K_SCOPE_GLOBAL, 0)
        sz = ctypes.c_uint32(0)
        if self._ca.AudioObjectGetPropertyDataSize(_K_SYSTEM_OBJECT, ctypes.byref(addr),
                                                   0, None, ctypes.byref(sz)):
            return []
        arr = (ctypes.c_uint32 * (sz.value // ctypes.sizeof(ctypes.c_uint32)))()
        if self._ca.AudioObjectGetPropertyData(_K_SYSTEM_OBJECT, ctypes.byref(addr), 0,
                                               None, ctypes.byref(sz), arr):
            return []
        return list(arr)

    def _describe(self, dev_id):
        tr = self._get(dev_id, _K_TRANSPORT, ctypes.c_uint32)
        return {"uid": self._get_str(dev_id, _K_DEVICE_UID),
                "name": self._get_str(dev_id, _K_NAME),
                "transport": _TRANSPORTS.get(tr, "other") if tr is not None else None}

    def default_input(self):
        """{"uid","name","transport"} du micro par défaut, ou None."""
        dev = self._get(_K_SYSTEM_OBJECT, _K_DEFAULT_INPUT, ctypes.c_uint32)
        return self._describe(dev) if dev else None

    def transport_of(self, name):
        """Transport du périphérique nommé `name` (PortAudio lit ses noms dans
        CoreAudio : ils sont identiques), ou None."""
        for dev_id in self._device_ids():
            d = self._describe(dev_id)
            if d["name"] == name:
                return d["transport"]
        return None

    def on_change(self, callback) -> bool:
        """Appelle `callback()` (thread CoreAudio : il doit rendre la main tout
        de suite) quand le micro par défaut ou la liste des périphériques
        change. True si l'écoute est en place."""
        def _proc(obj, n, addrs, data):  # noqa: ANN001
            callback()
            return 0
        tramp = _LISTENER_PROC(_proc)
        self._listeners.append(tramp)
        ok = True
        for sel in (_K_DEFAULT_INPUT, _K_DEVICES):
            addr = _PropAddr(sel, _K_SCOPE_GLOBAL, 0)
            if self._ca.AudioObjectAddPropertyListener(_K_SYSTEM_OBJECT, ctypes.byref(addr),
                                                       tramp, None):
                ok = False
        return ok


def open_coreaudio():
    """CoreAudio si disponible (macOS), sinon None."""
    if sys.platform != "darwin":
        return None
    try:
        return CoreAudio()
    except OSError as e:
        print(f"[micworker] CoreAudio indisponible : {e}")
        return None


# ---------------------------------------------------------------------------
# Rééchantillonnage en flux (numpy, chargé à la demande dans le worker)
# ---------------------------------------------------------------------------

class StreamResampler:
    """Rééchantillonne un flux float32 mono par morceaux, sans couture : filtre
    passe-bas à fenêtre (anti-repliement, seulement en descente) puis
    interpolation linéaire à pas fractionnaire, l'état passant d'un morceau au
    suivant. Pour la voix vers 16 kHz, 63 coefficients coupant à 90 % de
    Nyquist (7,2 kHz) suffisent : plus fin n'apporte rien à Whisper."""

    TAPS = 63

    def __init__(self, src_rate: int, dst_rate: int):
        import numpy as np
        self._np = np
        self.src_rate, self.dst_rate = int(src_rate), int(dst_rate)
        self._step = self.src_rate / self.dst_rate
        self._t = 0.0                                  # position de la prochaine sortie
        self._prev = np.zeros(0, dtype=np.float32)     # dernier échantillon filtré
        self._h = None
        if self.src_rate > self.dst_rate:
            fc = 0.9 * 0.5 * self.dst_rate / self.src_rate   # cycles par échantillon
            n = np.arange(self.TAPS) - (self.TAPS - 1) / 2.0
            h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(self.TAPS)
            self._h = (h / h.sum()).astype(np.float32)
            self._hist = np.zeros(self.TAPS - 1, dtype=np.float32)

    def process(self, x):
        np = self._np
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if self._h is not None:
            buf = np.concatenate((self._hist, x))
            self._hist = buf[-(self.TAPS - 1):]
            x = np.convolve(buf, self._h, mode="valid").astype(np.float32)
        f = np.concatenate((self._prev, x))
        last = len(f) - 1
        if last < 0 or self._t > last:
            self._t -= max(0, last)
            self._prev = f[-1:]
            return np.zeros(0, dtype=np.float32)
        n = int((last - self._t) // self._step) + 1
        pos = self._t + self._step * np.arange(n)
        i = pos.astype(np.int64)
        frac = (pos - i).astype(np.float32)
        out = f[i] * (1.0 - frac) + f[np.minimum(i + 1, last)] * frac
        self._t = float(pos[-1] + self._step - last)
        self._prev = f[-1:]
        return out.astype(np.float32)

    def process_bytes(self, data: bytes) -> bytes:
        return self.process(self._np.frombuffer(data, dtype=self._np.float32)).tobytes()


# ---------------------------------------------------------------------------
# CÔTÉ WORKER (process enfant : --mic-worker)
# ---------------------------------------------------------------------------

def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _frame(typ: bytes, payload: bytes) -> bytes:
    return typ + struct.pack(">I", len(payload)) + payload


class _Outbox:
    """File de sortie du worker : le callback CoreAudio y DÉPOSE sans jamais
    attendre (deque, aucun verrou au-delà du GIL), un thread d'écriture la
    vide vers le pipe. Avant, le callback écrivait lui-même dans le pipe
    (~64 Ko ≈ 1 s d'audio) : un parent affamé le bloquait, CoreAudio perdait
    des blocs et le parent voyait un faux flux mort.
    Audio et événements partagent la file : l'ordre est garanti (le « stopped »
    arrive après le dernier chunk)."""

    def __init__(self, out_fd: int, max_items: int):
        self._fd = out_fd
        self._max = int(max_items)
        self._q = collections.deque()
        self._wake = threading.Event()
        self.dead = False      # pipe cassé : parent mort, plus rien ne sort
        self._thr = threading.Thread(target=self._run, name="mic-out", daemon=True)
        self._thr.start()

    def audio(self, data: bytes, transform=None) -> bool:
        """Dépôt non bloquant d'un chunk. False si la file est pleine (chunk
        perdu, compté par l'appelant)."""
        if len(self._q) >= self._max:
            return False
        self._q.append((b"A", data, transform))
        self._wake.set()
        return True

    def event(self, **ev) -> None:
        self._q.append((b"J", json.dumps(ev).encode("utf-8"), None))
        self._wake.set()

    def close(self, timeout: float = 1.0) -> None:
        self._q.append(None)
        self._wake.set()
        self._thr.join(timeout)

    def _run(self):
        set_thread_qos(QOS_USER_INTERACTIVE)
        while True:
            try:
                item = self._q.popleft()
            except IndexError:
                self._wake.clear()
                if not self._q:
                    self._wake.wait()
                continue
            if item is None:
                return
            typ, payload, transform = item
            if self.dead:
                continue
            if transform is not None:
                payload = transform(payload)
            try:
                _write_all(self._fd, _frame(typ, payload))
            except OSError:
                # Parent mort : le worker sortira sur l'EOF de stdin. On cesse
                # d'écrire mais on continue de vider la file (aucun blocage).
                self.dead = True


def _is_builtin_name(name) -> bool:
    low = str(name or "").lower()
    return "macbook" in low or "built-in" in low or "intégr" in low


def _input_indices(devices):
    return [i for i, d in enumerate(devices) if int(d.get("max_input_channels", 0)) > 0]


def _pick_builtin(devices):
    """Index du micro intégré (ou premier micro valide). None si aucun."""
    inputs = _input_indices(devices)
    for i in inputs:
        if _is_builtin_name(devices[i].get("name")):
            return i
    return inputs[0] if inputs else None


def input_names_builtin_first(devices) -> list:
    """Noms des périphériques d'entrée, micro intégré en tête."""
    names = [str(devices[i].get("name", "")) for i in _input_indices(devices)]
    return sorted(names, key=lambda n: 0 if _is_builtin_name(n) else 1)


class MicWorker:
    """Le worker micro : commandes sur une entrée ligne à ligne, sortie cadrée
    via _Outbox. `sd` = module sounddevice (simulable), `coreaudio` =
    CoreAudio ou None (hors macOS, tests)."""

    BLOCK_S = 0.05          # 50 ms : sans lui ~1000 écritures pipe/s pour rien (mesuré)
    # 10 s d'audio en attente (640 Ko) avant de perdre un bloc : un parent
    # affamé plus longtemps que ça a un problème qu'aucune file ne réglera.
    QUEUE_MAX_S = 10.0
    REFRESH_DEBOUNCE_S = 0.3   # une bascule AirPods émet plusieurs notifications

    def __init__(self, sd, out_fd: int, coreaudio=None, queue_max_s: float = None):
        self.sd = sd
        self.ca = coreaudio
        qmax = int((queue_max_s or self.QUEUE_MAX_S) / self.BLOCK_S)
        self.out = _Outbox(out_fd, max(1, qmax))
        self._pa_lock = threading.Lock()   # toute opération PortAudio
        self._stream = None
        self._xruns = 0
        self._dropped = 0
        self._dirty = threading.Event()    # le défaut a changé depuis la dernière init
        self._closing = False
        self._pa_uid = self._default_uid()
        if self.ca is not None and self.ca.on_change(self._dirty.set):
            threading.Thread(target=self._refresh_loop, name="mic-refresh",
                             daemon=True).start()

    # -- PortAudio ---------------------------------------------------------

    def _default_uid(self):
        if self.ca is None:
            return None
        d = self.ca.default_input()
        return d["uid"] if d else None

    def _reinit_locked(self, reason: str) -> None:
        """Réinitialise PortAudio : il fige la liste des périphériques et le
        défaut à son initialisation. Flux fermé obligatoire (pa_lock tenu)."""
        self._dirty.clear()
        self.sd._terminate()
        self.sd._initialize()
        self._pa_uid = self._default_uid()
        self.out.event(ev="refreshed", reason=reason)

    def _sync_locked(self) -> None:
        """Filet de sécurité à l'ouverture : si l'écouteur a manqué un
        changement de défaut (ou n'existe pas), la comparaison d'UID le voit."""
        uid = self._default_uid()
        if self._dirty.is_set() or (uid and self._pa_uid and uid != self._pa_uid):
            self._reinit_locked("default_changed")

    def _refresh_loop(self):
        """Réinitialise PortAudio dès que le défaut change, AU REPOS : l'appui
        suivant trouve un PortAudio à jour sans payer la réinit."""
        while True:
            self._dirty.wait()
            if self._closing:
                return
            time.sleep(self.REFRESH_DEBOUNCE_S)
            if self._closing:
                return
            # Verrou pris (ouverture en cours, qui se synchronise elle-même) ou
            # dictée en cours : on repassera après.
            if not self._pa_lock.acquire(blocking=False):
                continue
            try:
                if self._stream is None and self._dirty.is_set():
                    self._reinit_locked("device_change")
            except Exception as e:
                self.out.event(ev="warn", msg=f"réinit PortAudio : {e}"[:300])
            finally:
                self._pa_lock.release()

    def _resolve_locked(self, name, prefer):
        """(index ou None, nom effectif, repli). Le nom est cherché dans la liste
        COURANTE de ce process : exact, puis partiel. Absent -> défaut."""
        devices = list(self.sd.query_devices())
        inputs = _input_indices(devices)
        idx, fallback = None, False
        if name:
            low = name.lower()
            found = ([i for i in inputs if devices[i].get("name") == name]
                     or [i for i in inputs if low in str(devices[i].get("name", "")).lower()])
            if found:
                idx = found[0]
            else:
                fallback = True
        elif prefer == "builtin":
            idx = _pick_builtin(devices)
        if idx is not None:
            return idx, devices[idx].get("name"), fallback
        try:
            return None, self.sd.query_devices(kind="input").get("name"), fallback
        except self.sd.PortAudioError:
            return None, None, fallback   # aucun défaut : l'ouverture dira pourquoi

    def _native_rate(self, idx) -> int:
        info = (self.sd.query_devices(idx) if idx is not None
                else self.sd.query_devices(kind="input"))
        return int(round(float(info.get("default_samplerate") or 0)))

    def _open_locked(self, idx, sr: int):
        """(flux, fréquence native ou None). Ouvre à `sr` ; si le périphérique
        refuse, à sa fréquence native avec rééchantillonnage vers `sr`
        (agrégats, micros virtuels Teams/Zoom)."""
        try:
            return self._make_stream(idx, sr, None), None
        except Exception as first:
            try:
                native = self._native_rate(idx)
            except Exception as exc:
                raise first from exc
            if not native or native == sr:
                raise first
            rs = StreamResampler(native, sr)
            try:
                return self._make_stream(idx, native, rs.process_bytes), native
            except Exception as exc:
                raise first from exc

    def _make_stream(self, idx, rate: int, transform):
        out = self.out

        def _cb(indata, frames, t, status):  # noqa: ANN001
            # Temps réel : un dépôt, deux compteurs, rien qui puisse attendre.
            if status:
                self._xruns += 1
            if not out.audio(bytes(indata), transform):
                self._dropped += 1

        kw = dict(samplerate=rate, channels=1, dtype="float32",
                  blocksize=int(rate * self.BLOCK_S), callback=_cb)
        if idx is not None:
            kw["device"] = idx
        s = self.sd.RawInputStream(**kw)
        try:
            s.start()
        except Exception:
            s.close()
            raise
        return s

    def _close_locked(self):
        s, self._stream = self._stream, None
        if s is None:
            return
        for step in (s.stop, s.close):
            try:
                step()
            except Exception as e:
                self.out.event(ev="warn", msg=f"fermeture du flux : {e}"[:300])

    # -- commandes ---------------------------------------------------------

    def _start(self, cmd):
        sr = int(cmd.get("sr") or 16000)
        with self._pa_lock:
            self._close_locked()   # jamais deux flux (re-start après stop raté)
            try:
                self._sync_locked()
                idx, name, fallback = self._resolve_locked(cmd.get("device"),
                                                           cmd.get("prefer"))
                transport = self.ca.transport_of(name) if (self.ca and name) else None
                self.out.event(ev="opening", device=name, transport=transport)
                self._xruns = self._dropped = 0
                t0 = time.monotonic()
                self._stream, native = self._open_locked(idx, sr)
                self.out.event(ev="started", device=name, fallback=fallback,
                               sr_native=native, open_s=round(time.monotonic() - t0, 3))
            except Exception as e:
                self._close_locked()
                self.out.event(ev="error", err=str(e)[:300])

    def _stop(self):
        with self._pa_lock:
            self._close_locked()
            # Après stop(), plus aucun callback : les compteurs sont définitifs.
            self.out.event(ev="stopped", xruns=self._xruns, dropped=self._dropped)

    def _devices(self):
        with self._pa_lock:
            try:
                self._sync_locked()
                self.out.event(ev="devices",
                               names=input_names_builtin_first(self.sd.query_devices()))
            except Exception as e:
                self.out.event(ev="error", err=str(e)[:300])

    def run(self, lines) -> int:
        self.out.event(ev="ready")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except ValueError:
                self.out.event(ev="warn", msg="commande illisible")
                continue
            c = cmd.get("cmd")
            if c == "ping":
                self.out.event(ev="pong")
            elif c == "start":
                self._start(cmd)
            elif c == "stop":
                self._stop()
            elif c == "devices":
                self._devices()
            elif c == "quit":
                break
        with self._pa_lock:
            self._close_locked()
        self._closing = True
        self._dirty.set()
        self.out.close()
        return 0


def worker_main(argv) -> int:
    """Point d'entrée du process --mic-worker. Ne charge que sounddevice
    (numpy seulement pour rééchantillonner) : spawn rapide, ~30 Mo. Le worker
    ne meurt que sur quit/EOF, ou tué par le parent, ce qui est un mode de
    sortie NORMAL ici."""
    try:
        import sounddevice as sd
    except Exception as e:
        msg = json.dumps({"ev": "error", "err": f"import sounddevice: {e}"})
        _write_all(1, _frame(b"J", msg.encode("utf-8")))
        return 1
    return MicWorker(sd, 1, open_coreaudio()).run(sys.stdin)


# ---------------------------------------------------------------------------
# CÔTÉ PARENT (classe cliente utilisée par engine.py)
# ---------------------------------------------------------------------------

class WorkerUnavailable(RuntimeError):
    """Le worker ne peut pas être lancé (env cassé) -> repli in-process."""


class MicStartError(RuntimeError):
    """Le worker tourne mais l'ouverture du micro a échoué/figé."""


def _helper_exe():
    """v1.0.25 — lance le worker depuis `Contents/Helpers/` et non
    `Contents/MacOS/` : macOS enregistre comme APPLICATION tout binaire lancé
    depuis MacOS/, d'où une icône Dock par worker. Depuis Helpers/, le process
    démarre directement en « prohibited » (jamais d'icône). Volontairement
    dupliqué depuis diarizer.py : ce chemin est celui de la DICTÉE, il ne doit
    pas déclencher l'import de diarizer (numpy + sherpa). Repli sur
    sys.executable si le lien manque."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    macos = os.path.dirname(sys.executable)            # .../Contents/MacOS
    cand = os.path.join(os.path.dirname(macos), "Helpers", "VlocalWorker")
    return cand if os.path.exists(cand) else sys.executable


def _worker_cmd():
    """Commande de spawn : binaire gelé re-exécuté (shim app.py), ou ce
    fichier en dev. Même patron éprouvé que diarizer._worker_cmd."""
    if getattr(sys, "frozen", False):
        return [_helper_exe(), "--mic-worker"]
    return [sys.executable, os.path.abspath(__file__), "--mic-worker"]


def _popen_worker():
    return subprocess.Popen(
        _worker_cmd(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,   # pas tué par un Ctrl-C du parent
    )


class RemoteMic:
    """Client du worker micro. Maintient un worker CHAUD (spawné à l'avance,
    micro fermé au repos) et le remplace par kill+respawn BORNÉ dès qu'il
    fige : la récupération ne dépend plus jamais de l'état CoreAudio du
    process principal. Thread-safe.

    v1.5 — L'APPUI GAGNE. Avant, une réparation de fond (respawn préventif,
    réparation après échec) tenait le verrou pendant tout un spawn (jusqu'à
    5 s) et l'appui attendait derrière : dictée perdue. Désormais :
      - le verrou n'est tenu que pour des échanges courts ; l'attente du
        « ready » d'un spawn se fait HORS verrou ;
      - un appui se déclare (`press()`) ; toute attente d'un AUTRE thread lui
        cède la place à la tranche suivante (50 ms) sans rien tuer, et
        l'appui REJOINT le spawn en vol (au-delà de SPAWN_JOIN_MAX, le spawn
        est jugé muet, tué et relancé) ;
      - l'acquisition du verrou par l'appui est bornée par son budget ;
      - rien de fond ne relance un worker pendant une capture.
    Jamais deux workers (tout lancement tue le précédent sous verrou), jamais
    zéro durablement (l'appui relance au besoin)."""

    # v1.0.22 — BUDGET « JAMAIS BLOQUÉ > 2 s » : bornes calibrées sur le réel
    # (open sain ~0,1 s, stop ~0,05 s, respawn ~0,15 s dev / ~1 s gelé).
    SPAWN_TIMEOUT = 6.0    # 1er "ready" (spawn à froid du binaire gelé ; préchauffé au boot)
    START_TIMEOUT = 2.0    # open micro : au-delà = figé -> kill (le respawn+retry suit)
    # v1.5 — Bluetooth : la bascule A2DP -> HFP du casque prend 1 à 3 s. 4 s
    # laisse passer une bascule lente sans qu'un open réellement figé retienne
    # l'appui indéfiniment.
    START_TIMEOUT_SLOW = 4.0
    # Une ouverture passée au-delà de ce seuil classe le périphérique « lent »
    # (repli quand CoreAudio ne dit pas le transport).
    SLOW_OPEN_S = 1.2
    # Au-delà, l'overlay passe en « connexion du micro » ; en dessous l'attente
    # est imperceptible et un changement d'état ferait clignoter la pilule.
    CONNECTING_UI_S = 0.5
    SPAWN_JOIN_MAX = 1.5   # un spawn gelé sain répond en ~1 s ; au-delà il est muet
    STOP_TIMEOUT = 1.5     # stop : au-delà = kill (les frames sont déjà chez le parent)
    PING_TIMEOUT = 0.8     # détection d'un worker vivant mais figé (healthcheck)
    QUERY_TIMEOUT = 1.0    # liste des périphériques
    _SLICE = 0.05

    def __init__(self, sample_rate: int = 16000, spawn=None):
        self.sample_rate = int(sample_rate)
        self._spawn_fn = spawn or _popen_worker
        self._lock = threading.RLock()
        self._press_lock = threading.Lock()   # tenue de l'identité de l'appui
        self._proc = None
        self._ready = threading.Event()
        self._spawn_t = 0.0
        self._events = {}          # ev -> Event
        self._payloads = {}        # ev -> dernier événement reçu
        self._last_error = None
        self._on_chunk = None      # callable(bytes) pendant une dictée
        self._capturing = False
        self._press_owner = None   # thread de l'appui en cours
        self._press_depth = 0
        self._slow_devices = {}    # nom -> dernière durée d'ouverture (s)
        self.last_open = None      # dernier "started"
        self.last_stats = None     # {"xruns","dropped"} de la dernière capture

    # -- appui -------------------------------------------------------------

    @contextlib.contextmanager
    def press(self):
        """Déclare un appui de l'utilisateur pour la durée du bloc : les
        attentes des autres threads lui cèdent la place."""
        me = threading.get_ident()
        with self._press_lock:
            if self._press_owner not in (None, me):
                # Deux appuis simultanés n'arrivent pas (le contrôleur les
                # sérialise) ; si cela arrivait, le second passe sans priorité.
                mine = False
            else:
                mine = True
                self._press_owner = me
                self._press_depth += 1
        if not mine:
            yield
            return
        try:
            yield
        finally:
            with self._press_lock:
                self._press_depth -= 1
                if self._press_depth == 0:
                    self._press_owner = None

    def _must_yield(self) -> bool:
        owner = self._press_owner
        return owner is not None and owner != threading.get_ident()

    # -- cycle de vie ------------------------------------------------------

    def _alive_locked(self) -> bool:
        return (self._proc is not None and self._proc.poll() is None
                and self._ready.is_set())

    def _launch_locked(self):
        """Lance un worker neuf (le précédent est tué) SANS attendre son ready."""
        self._kill_locked()
        ready = threading.Event()
        self._ready = ready
        self._events = {}
        self._payloads = {}
        self._last_error = None
        try:
            proc = self._spawn_fn()
        except Exception as e:
            raise WorkerUnavailable(f"spawn micro-worker: {e}") from e
        self._proc = proc
        self._spawn_t = time.monotonic()
        threading.Thread(target=self._reader, args=(proc, ready),
                         name="micworker-read", daemon=True).start()
        return proc, ready

    def _wait_ready(self, ready, timeout: float) -> str:
        """"ok" | "timeout" | "yield" (un appui d'un autre thread a la main)."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if ready.wait(min(self._SLICE, max(0.0, deadline - time.monotonic()))):
                return "ok"
            if self._must_yield():
                return "yield"
            if time.monotonic() >= deadline:
                return "timeout"

    @contextlib.contextmanager
    def _locked_until(self, t_end: float):
        """Verrou pris au plus tard à `t_end` (horloge monotone), sinon
        WorkerUnavailable : l'appui ne reste jamais derrière un verrou."""
        if not self._acquire(t_end - time.monotonic()):
            raise WorkerUnavailable("verrou micro occupé au-delà du budget")
        try:
            yield
        finally:
            self._lock.release()

    def _drop_if_current(self, proc) -> None:
        with self._lock:
            if self._proc is proc:
                self._kill_locked()

    def ensure(self, timeout: float = None) -> None:
        """Garantit un worker vivant et prêt. Lève WorkerUnavailable sinon.
        Un spawn déjà en vol (réparation de fond) est REJOINT plutôt que
        relancé ; s'il ne répond pas dans SPAWN_JOIN_MAX il est jugé muet."""
        t_end = time.monotonic() + (timeout or self.SPAWN_TIMEOUT)
        with self._locked_until(t_end):
            if self._alive_locked():
                return
            inflight = self._proc is not None and self._proc.poll() is None
            if inflight:
                proc, ready = self._proc, self._ready
                age = time.monotonic() - self._spawn_t
            else:
                proc, ready = self._launch_locked()
                age = 0.0
        wait = t_end - time.monotonic()
        if inflight:
            wait = min(wait, max(0.0, self.SPAWN_JOIN_MAX - age))
        r = self._wait_ready(ready, wait)
        if r == "ok":
            return
        if r == "yield":
            raise WorkerUnavailable("spawn cédé à un appui")
        with self._locked_until(t_end):
            if self._alive_locked():
                return
            if self._proc is proc:
                self._kill_locked()
            if not inflight or t_end - time.monotonic() <= self._SLICE:
                raise WorkerUnavailable("micro-worker muet")
            print("[micworker] spawn en vol muet -> relancé.")
            proc, ready = self._launch_locked()
        if self._wait_ready(ready, t_end - time.monotonic()) == "ok":
            return
        self._drop_if_current(proc)
        raise WorkerUnavailable("micro-worker muet")

    def respawn(self, timeout: float = None, wait: bool = True) -> bool:
        """Kill + spawn borné. True si le worker neuf est prêt (ou lancé, si
        `wait=False`). C'est LE geste de récupération garanti (équivalent
        invisible du « relance l'app »). Ne s'exécute ni pendant une capture
        ni pendant l'appui d'un autre thread."""
        with self._lock:
            if self._must_yield() or self._capturing:
                return False
            try:
                proc, ready = self._launch_locked()
            except WorkerUnavailable as e:
                print(f"[micworker] {e}")
                return False
        if not wait:
            return True
        r = self._wait_ready(ready, timeout or self.SPAWN_TIMEOUT)
        if r == "timeout":
            self._drop_if_current(proc)
        # "yield" : le spawn reste en vol, l'appui le rejoint (ensure).
        return r == "ok"

    def _reader(self, proc, ready) -> None:
        """Thread lecteur : démultiplexe chunks audio et événements. Sort sur
        EOF (mort du worker) et marque le worker indisponible. QoS
        user-interactive : s'il est affamé pendant que le modèle se charge, la
        file du worker grossit puis déborde (trous dans l'audio)."""
        set_thread_qos(QOS_USER_INTERACTIVE)
        f = proc.stdout
        try:
            while True:
                head = f.read(5)
                if not head or len(head) < 5:
                    break
                typ, ln = head[:1], struct.unpack(">I", head[1:])[0]
                payload = f.read(ln) if ln else b""
                if payload is None or len(payload) < ln:
                    break
                if self._proc is not proc:
                    continue   # génération périmée : on draine sans rien livrer
                if typ == b"A":
                    self._deliver(payload)
                elif typ == b"J":
                    try:
                        ev = json.loads(payload.decode("utf-8"))
                    except ValueError:
                        print("[micworker] événement illisible ignoré.")
                        continue
                    self._dispatch(ev, ready)
        except (OSError, ValueError) as e:
            print(f"[micworker] lecture du worker interrompue : {e}")
        finally:
            f.close()
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self._ready = threading.Event()

    def _deliver(self, payload: bytes) -> None:
        cb = self._on_chunk
        if cb is None:
            return
        try:
            cb(payload)
        except Exception as e:   # un chunk mal reçu ne doit pas tuer le lecteur
            print(f"[micworker] chunk audio non livré : {e}")

    def _dispatch(self, ev: dict, ready) -> None:
        name = ev.get("ev")
        if name == "ready":
            ready.set()
            return
        if name == "error":
            self._last_error = ev.get("err") or "?"
        elif name == "refreshed":
            print(f"[micworker] PortAudio réinitialisé ({ev.get('reason')}).")
        elif name == "warn":
            print(f"[micworker] {ev.get('msg')}")
        self._payloads[name] = ev
        e = self._events.get(name)
        if e:
            e.set()

    def _kill_locked(self) -> None:
        p, self._proc = self._proc, None
        self._ready = threading.Event()
        self._on_chunk = None
        self._capturing = False
        if p is None:
            return
        p.kill()       # SIGKILL : l'état CoreAudio corrompu meurt ici
        try:
            p.stdin.close()
        except OSError:
            pass   # pipe déjà rompu : le worker est mort, c'est le but
        try:
            p.wait(timeout=0.5)   # pas de zombie
        except subprocess.TimeoutExpired:
            print("[micworker] worker tué mais pas encore récolté.")

    def kill(self) -> None:
        with self._lock:
            self._kill_locked()

    def alive(self) -> bool:
        with self._lock:
            return self._alive_locked()

    # -- commandes ---------------------------------------------------------

    def _send(self, obj: dict) -> None:
        p = self._proc
        if p is None or p.poll() is not None:
            raise WorkerUnavailable("micro-worker mort")
        try:
            p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            p.stdin.flush()
        except (OSError, ValueError) as e:
            raise WorkerUnavailable(f"pipe micro-worker: {e}") from e

    def _arm(self, *names) -> None:
        """Arme les événements AVANT d'envoyer la commande (sinon une réponse
        ultra-rapide du worker arriverait avant l'attente et serait perdue)."""
        for n in names:
            self._events.setdefault(n, threading.Event()).clear()
            self._payloads.pop(n, None)

    def _await(self, name: str, timeout: float, yieldable: bool = False) -> str:
        """"ok" | "error" | "timeout" | "yield" pour l'événement `name`. Seules
        les attentes de fond sont `yieldable` : un stop ou une ouverture ne
        cèdent jamais (ils tueraient le worker d'une dictée en cours)."""
        ok_evt = self._events.setdefault(name, threading.Event())
        err_evt = self._events.setdefault("error", threading.Event())
        deadline = time.monotonic() + timeout
        while True:
            if ok_evt.wait(self._SLICE):
                return "ok"
            if err_evt.is_set():
                return "error"
            if yieldable and self._must_yield():
                return "yield"
            if time.monotonic() >= deadline:
                return "timeout"

    def _acquire(self, timeout: float) -> bool:
        return self._lock.acquire(timeout=max(0.0, timeout))

    def healthcheck(self) -> bool:
        """Worker vivant ET réactif ? (un worker figé répond au poll() mais
        pas au ping : c'est lui qu'on veut détecter AVANT la dictée). Un
        appui qui survient compte comme « sain » : ce n'est plus notre affaire."""
        with self._lock:
            if not self._alive_locked():
                return False
            self._arm("pong", "error")
            try:
                self._send({"cmd": "ping"})
            except WorkerUnavailable:
                return False
            return self._await("pong", self.PING_TIMEOUT, yieldable=True) in ("ok", "yield")

    def heal(self) -> None:
        """Santé au repos : ping ; mort ou figé -> respawn préventif. Ne gêne
        jamais un appui (cède) ni une capture (n'agit pas)."""
        if self._press_owner is not None or self._capturing:
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._capturing or self.healthcheck():
                return
        finally:
            self._lock.release()
        print("[micworker] worker mort/figé détecté au repos -> respawn préventif.")
        self.respawn()

    def list_devices(self, timeout: float = None) -> list:
        """Noms des micros vus PAR LE WORKER (PortAudio à jour), intégré en
        tête. Lève WorkerUnavailable si le worker ne répond pas."""
        t_end = time.monotonic() + (timeout or self.QUERY_TIMEOUT)
        self.ensure(t_end - time.monotonic())
        with self._locked_until(t_end):
            self._arm("devices", "error")
            self._send({"cmd": "devices"})
            if self._await("devices", max(self._SLICE, t_end - time.monotonic())) != "ok":
                raise WorkerUnavailable("liste des micros sans réponse")
            return list(self._payloads.get("devices", {}).get("names") or [])

    def _is_slow(self, opening: dict) -> bool:
        if opening.get("transport") == "bluetooth":
            return True
        return self._slow_devices.get(opening.get("device"), 0.0) >= self.SLOW_OPEN_S

    def start(self, on_chunk, device=None, prefer=None, budget=None,
              on_connecting=None) -> None:
        """Ouvre le micro dans le worker. `device` = NOM du micro (résolu dans
        le worker ; absent -> défaut). `on_chunk(bytes)` reçoit le PCM float32
        16 kHz en continu. `on_connecting()` est appelé (une fois au plus)
        quand l'ouverture s'annonce lente. Lève MicStartError (échec device)
        ou WorkerUnavailable (worker mort/figé -> l'appelant respawne).

        CONTRAT ANTI-DEADLOCK : l'appelant ne doit PAS tenir le verrou que
        `on_chunk` prend (les premiers chunks peuvent arriver AVANT
        l'événement "started" : le thread lecteur les livre en série)."""
        # BUDGET DUR : `budget` (secondes) borne spawn, verrou ET ouverture ;
        # seule une ouverture annoncée lente (Bluetooth) l'étend, jusqu'à
        # START_TIMEOUT_SLOW après l'envoi de la commande.
        t0 = time.monotonic()

        def _left(default):
            if budget is None:
                return default
            return max(self._SLICE, min(default, budget - (time.monotonic() - t0)))

        with self.press():
            self.ensure(_left(self.SPAWN_TIMEOUT))
            with self._locked_until(time.monotonic() + _left(self.START_TIMEOUT)):
                self._open_locked(on_chunk, device, prefer, _left(self.START_TIMEOUT),
                                  on_connecting)

    def _open_locked(self, on_chunk, device, prefer, wait_s, on_connecting):
        self._on_chunk = on_chunk
        self._last_error = None
        self._capturing = True
        self._arm("started", "error", "opening")
        try:
            self._send({"cmd": "start", "device": device, "prefer": prefer,
                        "sr": self.sample_rate})
        except WorkerUnavailable:
            self._on_chunk = None
            self._capturing = False
            raise
        t_send = time.monotonic()
        deadline = t_send + wait_s
        opening_seen = connecting = False
        while True:
            r = self._await("started", self._SLICE)
            if r in ("ok", "error"):
                break
            now = time.monotonic()
            if not opening_seen and self._events["opening"].is_set():
                opening_seen = True
                if self._is_slow(self._payloads.get("opening") or {}):
                    deadline = max(deadline, t_send + self.START_TIMEOUT_SLOW)
                    connecting = connecting or self._notify(on_connecting)
            if not connecting and now - t_send >= self.CONNECTING_UI_S:
                connecting = self._notify(on_connecting)
            if now >= deadline:
                break
        if r == "ok":
            self._note_open(self._payloads.get("started") or {})
            return
        self._on_chunk = None
        self._capturing = False
        if r == "error":
            raise MicStartError(self._last_error)
        # Ni started ni error dans la borne = worker FIGÉ (open CoreAudio
        # gelé). On le tue : l'appelant respawne et réessaie, borné et
        # toujours efficace, l'état corrompu meurt avec le process.
        self._kill_locked()
        raise WorkerUnavailable(
            f"ouverture micro figée (> {deadline - t_send:.1f}s) -> worker tué")

    def _note_open(self, info: dict) -> None:
        self.last_open = info
        if info.get("device"):
            self._slow_devices[info["device"]] = float(info.get("open_s") or 0.0)
        if info.get("fallback"):
            print(f"[micworker] micro choisi absent -> défaut ({info.get('device')}).")
        if info.get("sr_native"):
            print(f"[micworker] ouverture à {info['sr_native']} Hz, rééchantillonnée.")

    @staticmethod
    def _notify(cb) -> bool:
        if cb is not None:
            try:
                cb()
            except Exception as e:   # un souci d'affichage n'arrête pas l'ouverture
                print(f"[micworker] affichage « connexion » en échec : {e}")
        return True

    def stop(self) -> None:
        """Ferme le micro côté worker et attend le « stopped », qui suit le
        dernier chunk : la fin de la phrase n'est plus coupée. Un stop figé ->
        worker tué : les frames reçues restent chez le parent, et le prochain
        ensure() repartira sur un process neuf."""
        with self._lock:
            self.last_stats = None
            try:
                if not self._alive_locked():
                    return
                self._arm("stopped", "error")
                try:
                    self._send({"cmd": "stop"})
                except WorkerUnavailable:
                    return
                if self._await("stopped", self.STOP_TIMEOUT) == "ok":
                    ev = self._payloads.get("stopped") or {}
                    self.last_stats = {"xruns": int(ev.get("xruns") or 0),
                                       "dropped": int(ev.get("dropped") or 0)}
                else:
                    self._kill_locked()
            finally:
                self._on_chunk = None
                self._capturing = False


if __name__ == "__main__":
    sys.exit(worker_main(sys.argv))
