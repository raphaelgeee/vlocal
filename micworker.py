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
    engine._frames comme avant (qualité/vitesse/RAM intactes).
  - Micro FERMÉ au repos (pastille orange uniquement pendant la dictée) : le
    worker chaud ne tient AUCUN flux entre deux dictées.
  - Audio jamais perdu : le PCM est streamé en continu vers le parent ; un
    worker tué au stop n'emporte rien.
  - Pas d'orphelin : le worker sort tout seul sur EOF stdin (mort du parent).
  - Repli : si le worker ne peut pas se lancer (env cassé), engine repasse
    sur le chemin in-process historique ; VLOCAL_MIC_PROC=0 désactive tout.

PROTOCOLE (volontairement minuscule) :
  parent -> worker : une commande JSON par ligne sur stdin
      {"cmd":"start","device":<int|null>,"prefer":"builtin"|null,"sr":16000}
      {"cmd":"stop"} {"cmd":"ping"} {"cmd":"quit"}
  worker -> parent : messages cadrés sur stdout : 1 octet type + longueur
      uint32 big-endian + payload
      b"A" = chunk audio brut (float32 mono)   b"J" = événement JSON utf-8
      Événements : {"ev":"ready"} {"ev":"started","device":...}
      {"ev":"stopped"} {"ev":"pong"} {"ev":"error","err":"..."}
"""

import json
import os
import struct
import subprocess
import sys
import threading
import time


# ---------------------------------------------------------------------------
# CÔTÉ WORKER (process enfant : --mic-worker)
# ---------------------------------------------------------------------------

def _w_send(lock, typ: bytes, payload: bytes) -> None:
    """Écrit un message cadré sur stdout (fd 1, sans buffering Python)."""
    with lock:
        os.write(1, typ + struct.pack(">I", len(payload)) + payload)


def _w_event(lock, **ev) -> None:
    _w_send(lock, b"J", json.dumps(ev).encode("utf-8"))


def _w_pick_builtin():
    """Index du micro intégré (ou premier micro valide). None si aucun."""
    import sounddevice as sd
    pick = None
    for i, d in enumerate(sd.query_devices()):
        try:
            if int(d.get("max_input_channels", 0)) < 1:
                continue
            if pick is None:
                pick = i
            nm = str(d.get("name", "")).lower()
            if "macbook" in nm or "built-in" in nm or "intégr" in nm:
                return i
        except Exception:
            continue
    return pick


def worker_main(argv) -> int:
    """Boucle du worker micro. Ne charge QUE sounddevice (RawInputStream ->
    pas de numpy) : spawn rapide, ~30 Mo. Toute erreur d'une commande est
    renvoyée en événement "error" ; le worker ne meurt que sur quit/EOF —
    ou tué par le parent, ce qui est un mode de sortie NORMAL ici."""
    out_lock = threading.Lock()
    stream = {"s": None}

    try:
        import sounddevice as sd
    except Exception as e:
        _w_event(out_lock, ev="error", err=f"import sounddevice: {e}")
        return 1

    def _close_current():
        s = stream["s"]
        stream["s"] = None
        if s is not None:
            try:
                s.stop()
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass

    _w_event(out_lock, ev="ready")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except Exception:
            continue
        c = cmd.get("cmd")

        if c == "ping":
            _w_event(out_lock, ev="pong")

        elif c == "start":
            _close_current()   # jamais deux flux (re-start après stop raté)
            dev = cmd.get("device")
            if dev is None and cmd.get("prefer") == "builtin":
                try:
                    dev = _w_pick_builtin()
                except Exception:
                    dev = None
            sr = int(cmd.get("sr") or 16000)

            def _cb(indata, frames, t, status):  # noqa: ANN001
                # Chunk brut float32 -> parent. Un write qui échoue (parent
                # mort) fait mourir le worker : comportement voulu.
                _w_send(out_lock, b"A", bytes(indata))

            try:
                # blocksize 50 ms : sans lui, PortAudio livre des paquets de
                # ~4 ms -> 1000 écritures pipe/s pour rien (mesuré). 50 ms
                # reste plus fin que la wave UI (10 Hz) : réactivité intacte.
                kw = dict(samplerate=sr, channels=1, dtype="float32",
                          blocksize=int(sr * 0.05), callback=_cb)
                if dev is not None:
                    kw["device"] = dev
                s = sd.RawInputStream(**kw)
                s.start()
                stream["s"] = s
                _w_event(out_lock, ev="started", device=dev)
            except Exception as e:
                _close_current()
                _w_event(out_lock, ev="error", err=str(e)[:300])

        elif c == "stop":
            _close_current()
            _w_event(out_lock, ev="stopped")

        elif c == "quit":
            break

    _close_current()
    return 0


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
    try:
        macos = os.path.dirname(sys.executable)            # .../Contents/MacOS
        cand = os.path.join(os.path.dirname(macos), "Helpers", "VlocalWorker")
        if os.path.exists(cand):
            return cand
    except Exception:
        pass
    return sys.executable


def _worker_cmd():
    """Commande de spawn : binaire gelé re-exécuté (shim app.py), ou ce
    fichier en dev. Même patron éprouvé que diarizer._worker_cmd."""
    if getattr(sys, "frozen", False):
        return [_helper_exe(), "--mic-worker"]
    return [sys.executable, os.path.abspath(__file__), "--mic-worker"]


class RemoteMic:
    """Client du worker micro. Maintient un worker CHAUD (spawné à l'avance,
    micro fermé au repos), le PING avant chaque dictée, et le remplace par
    kill+respawn BORNÉ dès qu'il fige — la récupération ne dépend plus jamais
    de l'état CoreAudio du process principal. Thread-safe."""

    # v1.0.22 — BUDGET « JAMAIS BLOQUÉ > 2 s » : bornes calibrées sur le réel
    # (open sain ~0,1 s, stop ~0,05 s, respawn ~0,15 s dev / ~1 s gelé). Les
    # anciennes bornes 4 s / 3 s dataient de l'open in-process (elles laissaient
    # UN SEUL figeage consommer tout le budget). Un open > 2 s = worker déclaré
    # figé -> kill+respawn+retry (plus rapide ET plus sûr que d'attendre).
    SPAWN_TIMEOUT = 6.0    # 1er "ready" (spawn à froid du binaire gelé ; préchauffé au boot)
    START_TIMEOUT = 2.0    # open micro : au-delà = figé -> kill (le respawn+retry suit)
    STOP_TIMEOUT = 1.5     # stop : au-delà = kill (les frames sont déjà chez le parent)
    PING_TIMEOUT = 0.8     # détection d'un worker vivant mais figé (healthcheck)

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = int(sample_rate)
        self._lock = threading.RLock()
        self._proc = None
        self._ready = threading.Event()
        self._events = {}          # ev -> Event (started/stopped/pong)
        self._last_error = None
        self._on_chunk = None      # callable(bytes) pendant une dictée

    # -- cycle de vie ------------------------------------------------------

    def ensure(self, timeout: float = None) -> None:
        """Garantit un worker vivant et prêt. Lève WorkerUnavailable sinon.
        `timeout` : borne du spawn (permet à l'appelant d'imposer son budget)."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None \
                    and self._ready.is_set():
                return
            self._spawn(timeout or self.SPAWN_TIMEOUT)

    def _spawn(self, timeout: float) -> None:
        self._kill_locked()
        self._ready = threading.Event()
        self._events = {}
        self._last_error = None
        try:
            self._proc = subprocess.Popen(
                _worker_cmd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,   # pas tué par un Ctrl-C du parent
            )
        except Exception as e:
            self._proc = None
            raise WorkerUnavailable(f"spawn micro-worker: {e}")
        threading.Thread(target=self._reader, args=(self._proc,),
                         name="micworker-read", daemon=True).start()
        if not self._ready.wait(timeout):
            self._kill_locked()
            raise WorkerUnavailable(f"micro-worker muet (> {timeout:.0f}s)")

    def _reader(self, proc) -> None:
        """Thread lecteur : démultiplexe chunks audio et événements. Sort sur
        EOF (mort du worker) et marque le worker indisponible."""
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
                if typ == b"A":
                    cb = self._on_chunk
                    if cb is not None:
                        try:
                            cb(payload)
                        except Exception:
                            pass
                elif typ == b"J":
                    try:
                        ev = json.loads(payload.decode("utf-8"))
                    except Exception:
                        continue
                    name = ev.get("ev")
                    if name == "ready":
                        self._ready.set()
                    elif name == "error":
                        self._last_error = ev.get("err") or "?"
                        e = self._events.get("error")
                        if e:
                            e.set()
                    else:
                        e = self._events.get(name)
                        if e:
                            e.set()
        except Exception:
            pass
        finally:
            # Worker mort : seule la génération COURANTE est invalidée (un
            # respawn a pu remplacer proc entre-temps).
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self._ready = threading.Event()

    def _kill_locked(self) -> None:
        p, self._proc = self._proc, None
        self._ready = threading.Event()
        self._on_chunk = None
        if p is not None:
            try:
                p.kill()       # SIGKILL : l'état CoreAudio corrompu meurt ici
            except Exception:
                pass
            try:
                p.stdin.close()
            except Exception:
                pass

    def kill(self) -> None:
        with self._lock:
            self._kill_locked()

    def respawn(self, timeout: float = None) -> bool:
        """Kill + spawn synchrone borné. True si le worker neuf est prêt.
        C'est LE geste de récupération garanti (équivalent invisible du
        « relance l'app »)."""
        with self._lock:
            try:
                self._spawn(timeout or self.SPAWN_TIMEOUT)
                return True
            except WorkerUnavailable:
                return False

    def alive(self) -> bool:
        with self._lock:
            return (self._proc is not None and self._proc.poll() is None
                    and self._ready.is_set())

    # -- commandes ---------------------------------------------------------

    def _send(self, obj: dict) -> None:
        p = self._proc
        if p is None or p.poll() is not None:
            raise WorkerUnavailable("micro-worker mort")
        try:
            p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            p.stdin.flush()
        except Exception as e:
            raise WorkerUnavailable(f"pipe micro-worker: {e}")

    def _arm(self, *names) -> None:
        """Arme les événements AVANT d'envoyer la commande (sinon une réponse
        ultra-rapide du worker arriverait avant l'attente et serait perdue)."""
        for n in names:
            self._events.setdefault(n, threading.Event()).clear()

    def _await(self, name: str, timeout: float) -> bool:
        """Attend l'événement `name` (armé via _arm) OU une erreur."""
        ok_evt = self._events.setdefault(name, threading.Event())
        err_evt = self._events.setdefault("error", threading.Event())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ok_evt.wait(0.05):
                return True
            if err_evt.is_set():
                return False
        return False

    def healthcheck(self) -> bool:
        """Worker vivant ET réactif ? (un worker figé répond au poll() mais
        pas au ping — c'est lui qu'on veut détecter AVANT la dictée)."""
        with self._lock:
            if not self.alive():
                return False
            self._arm("pong", "error")
            try:
                self._send({"cmd": "ping"})
            except WorkerUnavailable:
                return False
            return self._await("pong", self.PING_TIMEOUT)

    def start(self, on_chunk, device=None, prefer=None, budget=None) -> None:
        """Ouvre le micro dans le worker. `on_chunk(bytes)` reçoit le PCM
        float32 en continu. Lève MicStartError (échec device) ou
        WorkerUnavailable (worker mort/figé -> l'appelant respawne).

        CONTRAT ANTI-DEADLOCK : l'appelant ne doit PAS tenir le verrou que
        `on_chunk` prend (les premiers chunks peuvent arriver AVANT
        l'événement "started" — le thread lecteur les livre en série)."""
        # v1.0.22 — BUDGET DUR : `budget` (secondes restantes autorisées) borne
        # spawn ET ouverture. L'utilisateur ne doit jamais attendre plus que le
        # budget que lui accorde l'appelant, même si tout échoue.
        _t_start = time.monotonic()

        def _left(default):
            if budget is None:
                return default
            return max(0.05, min(default, budget - (time.monotonic() - _t_start)))

        with self._lock:
            self.ensure(_left(self.SPAWN_TIMEOUT))
            self._on_chunk = on_chunk
            self._last_error = None
            self._arm("started", "error")
            self._send({"cmd": "start",
                        "device": device,
                        "prefer": prefer,
                        "sr": self.sample_rate})
            if self._await("started", _left(self.START_TIMEOUT)):
                return
            self._on_chunk = None
            if self._last_error is not None:
                raise MicStartError(self._last_error)
            # Ni started ni error dans la borne = worker FIGÉ (open CoreAudio
            # gelé). On le tue : l'appelant respawne et réessaie — borné,
            # toujours efficace, l'état corrompu meurt avec le process.
            self._kill_locked()
            raise WorkerUnavailable(
                f"ouverture micro figée (> {self.START_TIMEOUT:.0f}s) "
                "-> worker tué")

    def stop(self) -> None:
        """Ferme le micro côté worker. Un stop figé -> worker tué : les
        frames sont DÉJÀ chez le parent, on ne perd rien, et le prochain
        ensure() repartira sur un process neuf."""
        with self._lock:
            self._on_chunk = None
            if not self.alive():
                return
            self._arm("stopped", "error")
            try:
                self._send({"cmd": "stop"})
            except WorkerUnavailable:
                return
            if not self._await("stopped", self.STOP_TIMEOUT):
                self._kill_locked()


if __name__ == "__main__":
    sys.exit(worker_main(sys.argv))
