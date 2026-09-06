#!/usr/bin/env python3
"""
Vlocal — VisioRecorder : enregistrement RÉUNION en mode VISIO (additif, isolé).

Capte SIMULTANÉMENT le micro (toi) + l'audio système (les autres d'un Meet/Teams/
Zoom, via le helper natif `syscapture` ScreenCaptureKit) et les MIXE EN TEMPS RÉEL
dans UN SEUL WAV 16 kHz / mono / 16-bit qui grandit — STRICTEMENT le même format
et le même contrat public que MeetingRecorder. Résultat : LiveMeetingTranscriber,
le diariseur CAM++ live et tout le pipeline réunion tournent À L'IDENTIQUE
(reunion_start choisit juste ce recorder à la place de MeetingRecorder).

Principe « ne rien casser » :
  - N'importe de recorder.py QUE des CONSTANTES publiques (zéro symbole privé).
  - RECOPIE _writer_loop + _close_stream_bounded (découplage total : un refactor
    futur de recorder.py ne peut pas casser le visio).
  - N'importe JAMAIS meeting_visio (qui importe soundfile au niveau module et
    n'est pas dans le bundle).
  - MICRO = HORLOGE MAÎTRE : le callback micro 50 ms cadence l'écriture ; la durée
    et la timeline restent fiables même si le helper meurt.
  - DÉGRADATION GRACIEUSE : toute panne système (helper absent/refusé/mort) ->
    on continue en MICRO SEUL (= un présentiel), l'enregistrement réussit toujours.
  - stop() idempotent, ne lève jamais, TUE le helper (SIGTERM->wait<=3s->kill),
    joint tous les threads (aucune écriture après le retour).

100% local. Aucune donnée ne quitte la machine.
"""

import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

# UNIQUEMENT des constantes publiques (pas de logique privée importée).
from recorder import (REC_DIR, SAMPLE_RATE, CHANNELS, BLOCK_SEC,
                      MIN_FREE_MB, STOP_FREE_MB)

_QUEUE_MAX_BLOCKS = int(10 / BLOCK_SEC)          # ~10 s de marge (comme recorder)
_SYS_RING_MAX = int(1.5 * SAMPLE_RATE)           # ring système borné ~1,5 s (anti-dérive)
_MIX_GAIN = 0.71                                  # -3 dB par source (somme sans écrêtage)


def _free_mb(path: Path) -> int:
    try:
        target = path if path.exists() else path.parent
        return int(shutil.disk_usage(str(target)).free / (1024 * 1024))
    except Exception:
        return 0


def _close_stream_bounded(stream, timeout: float = 3.0) -> None:
    """RECOPIE de recorder._close_stream_bounded : ferme un flux sans bloquer >
    timeout (un changement de device peut figer close())."""
    if stream is None:
        return
    done = threading.Event()

    def _close():
        try:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        finally:
            done.set()
    threading.Thread(target=_close, name="visio-audio-close", daemon=True).start()
    if not done.wait(timeout):
        try:
            print(f"[visio] fermeture micro figée (>{timeout:.0f}s) -> abandonnée (auto-heal).")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Localisation du helper natif syscapture (réimplémentée ici, n'importe PAS
# meeting_visio -> pas de dépendance soundfile sur le chemin live).
# --------------------------------------------------------------------------- #
def _helper_path():
    cands = []
    exe_dir = os.path.dirname(sys.executable)
    cands.append(os.path.join(exe_dir, "..", "Helpers", "syscapture"))   # bundle gelé
    cands.append(os.path.join(exe_dir, "syscapture"))
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, "syscapture"))
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, "syscapture"))                        # dev (repo vlocal)
    cands.append(os.path.expanduser("~/Desktop/vlocal-soundbox/syscapture"))  # dev (sandbox)
    for c in cands:
        c = os.path.normpath(c)
        try:
            if os.path.exists(c) and os.access(c, os.X_OK):
                return c
        except Exception:
            pass
    return None


def is_helper_available() -> bool:
    return _helper_path() is not None


# --------------------------------------------------------------------------- #
class VisioRecorder:
    """Recorder visio. Contrat public IDENTIQUE à MeetingRecorder + extras
    additifs (system_status) ignorés par les appelants présentiel."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, device=None):
        self.sample_rate = sample_rate
        self.device = device
        self._stream = None
        self._path = None
        self._lock = threading.RLock()
        self._recording = False
        self._t0 = 0.0
        self._last_rms = 0.0

        # File d'écriture (mix) + writer (recopie du contrat recorder)
        self._q: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX_BLOCKS)
        self._writer = None
        self._writer_stop = threading.Event()
        self._frames_written = 0          # frames MICRO écrites = horloge maître
        self._dropped_blocks = 0
        self._disk_full = False
        self._error = None

        # Capture système (helper en sous-process + ring borné)
        self._proc = None
        self._sysreader_t = None
        self._stderr_t = None
        self._sys_stop = threading.Event()
        self._sys_buf = np.zeros(0, dtype=np.float32)
        self._sys_lock = threading.Lock()
        self._sys_sr = SAMPLE_RATE        # mis à jour par le handshake FMT
        self._sys_frames = 0
        self._sys_dropped = 0
        self._system_status = "starting"  # starting|ok|denied|lost|never
        self._dom = []                    # v1.0.12 — (mic_rms, sys_rms)/bloc -> sidecar .dom (diarisation par source)

    # ------------------------------------------------------------------ #
    # Contrat public IDENTIQUE à MeetingRecorder
    # ------------------------------------------------------------------ #
    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def disk_full(self) -> bool:
        return self._disk_full

    @property
    def error(self) -> str:
        return self._error or ""

    @property
    def system_status(self) -> str:           # ADDITIF (toast live), ignoré ailleurs
        return self._system_status

    def elapsed(self) -> float:
        if self._frames_written:
            return self._frames_written / float(self.sample_rate)
        if self._recording and self._t0:
            return max(0.0, time.perf_counter() - self._t0)
        return 0.0

    def rms_recent(self) -> float:
        return self._last_rms

    def disk_ok(self) -> bool:
        try:
            REC_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            return False
        return _free_mb(REC_DIR) >= MIN_FREE_MB

    # ------------------------------------------------------------------ #
    # Writer : RECOPIE de MeetingRecorder._writer_loop (mêmes garanties)
    # ------------------------------------------------------------------ #
    def _writer_loop(self, wav):
        last_flush = time.perf_counter()
        last_disk_check = last_flush
        try:
            while True:
                try:
                    block = self._q.get(timeout=0.2)
                except queue.Empty:
                    block = None
                if block is not None:
                    try:
                        wav.writeframes(block)
                        self._frames_written += len(block) // 2     # int16 mono
                    except Exception as e:
                        self._error = f"écriture disque : {e}"
                        break
                now = time.perf_counter()
                if now - last_flush > 1.0:
                    try:
                        wav._file.flush()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    last_flush = now
                if now - last_disk_check > 5.0:
                    if _free_mb(REC_DIR) < STOP_FREE_MB:
                        self._disk_full = True
                        self._error = "espace disque critique"
                        break
                    last_disk_check = now
                if self._writer_stop.is_set() and self._q.empty():
                    break
        finally:
            try:
                while True:
                    try:
                        block = self._q.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        wav.writeframes(block)
                        self._frames_written += len(block) // 2
                    except Exception:
                        break
            finally:
                try:
                    wav.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # Capture système (helper) — best-effort, ne fait jamais échouer la réunion
    # ------------------------------------------------------------------ #
    def _start_system(self):
        hp = _helper_path()
        if not hp:
            self._system_status = "never"
            print("[visio] helper syscapture introuvable -> micro seul.")
            return
        try:
            self._proc = subprocess.Popen(
                [hp, "-"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as e:
            self._system_status = "never"
            self._proc = None
            print(f"[visio] lancement helper KO -> micro seul : {e}")
            return
        self._stderr_t = threading.Thread(target=self._stderr_drain, args=(self._proc,),
                                          name="visio-sys-stderr", daemon=True)
        self._stderr_t.start()
        self._sysreader_t = threading.Thread(target=self._sysreader, args=(self._proc,),
                                             name="visio-sys-read", daemon=True)
        self._sysreader_t.start()

    def _stderr_drain(self, proc):
        try:
            for raw in iter(proc.stderr.readline, b""):
                s = raw.decode("utf-8", "replace").strip()
                if not s:
                    continue
                if s.startswith("FMT"):
                    try:
                        self._sys_sr = int(s.split()[1])
                    except Exception:
                        pass
                elif s == "READY":
                    if self._system_status in ("starting",):
                        self._system_status = "ok"
                elif s.startswith("START_FAIL"):
                    self._system_status = "denied"
                    self._note_error("audio système indisponible (permission écran ?)")
                elif s.startswith("ERR"):
                    print(f"[visio] helper: {s}")
        except Exception:
            pass

    def _sysreader(self, proc):
        """Lit le PCM mono float32 du helper -> ring borné. Resample SEULEMENT si
        le handshake FMT annonce un SR != 16k (cas rare). Thread minimal."""
        leftover = b""
        fd = proc.stdout.fileno()
        got_any = False
        while not self._sys_stop.is_set():
            try:
                chunk = os.read(fd, 65536)
            except Exception:
                break
            if not chunk:
                break                          # EOF = helper terminé
            data = leftover + chunk
            rem = len(data) % 4
            if rem:
                leftover = data[len(data) - rem:]
                data = data[:len(data) - rem]
            else:
                leftover = b""
            if not data:
                continue
            samples = np.frombuffer(data, dtype="<f4")
            if self._sys_sr and self._sys_sr != SAMPLE_RATE:
                try:
                    from scipy.signal import resample_poly
                    from math import gcd
                    g = gcd(int(self._sys_sr), SAMPLE_RATE)
                    samples = resample_poly(samples, SAMPLE_RATE // g,
                                            int(self._sys_sr) // g).astype(np.float32)
                except Exception:
                    pass
            with self._sys_lock:
                if self._sys_buf.size:
                    self._sys_buf = np.concatenate([self._sys_buf, samples])
                else:
                    self._sys_buf = samples.astype(np.float32, copy=True)
                if self._sys_buf.size > _SYS_RING_MAX:        # borne : drop le plus vieux
                    over = self._sys_buf.size - _SYS_RING_MAX
                    self._sys_buf = self._sys_buf[over:]
                    self._sys_dropped += over
            self._sys_frames += int(samples.size)
            got_any = True
        # EOF : si on capturait et que ce n'est pas un stop volontaire -> perdu
        if not self._sys_stop.is_set() and self._system_status in ("ok", "starting"):
            self._system_status = "lost" if got_any else (
                self._system_status if self._system_status == "denied" else "never")
            if got_any:
                self._note_error("flux audio système interrompu")

    def _note_error(self, msg):
        self._error = f"{self._error} ; {msg}" if self._error else msg

    def _pull_system(self, n: int) -> np.ndarray:
        """Récupère n échantillons système (zero-pad si en retard). Non bloquant."""
        with self._sys_lock:
            buf = self._sys_buf
            if buf.size >= n:
                out = buf[:n]
                self._sys_buf = buf[n:]
                return out
            # en retard : ce qu'on a + zéros
            if buf.size:
                out = np.concatenate([buf, np.zeros(n - buf.size, np.float32)])
            else:
                out = np.zeros(n, np.float32)
            self._sys_buf = np.zeros(0, dtype=np.float32)
            return out

    def _kill_helper(self):
        p = self._proc
        self._proc = None
        if p is None:
            return
        try:
            p.send_signal(signal.SIGTERM)
        except Exception:
            pass
        try:
            p.wait(timeout=3.0)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        for s in (p.stdout, p.stderr):
            try:
                if s:
                    s.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    def start(self) -> str:
        """Démarre l'enregistrement visio. Retourne le path WAV.
        Lève RuntimeError UNIQUEMENT si déjà en cours ou disque insuffisant
        (mêmes conditions que MeetingRecorder) — JAMAIS pour une panne système."""
        import sounddevice as sd
        with self._lock:
            if self._recording:
                raise RuntimeError("Enregistrement déjà en cours.")
            if not self.disk_ok():
                raise RuntimeError(
                    f"Espace disque insuffisant (moins de {MIN_FREE_MB} Mo libres). "
                    "Libère de l'espace avant de démarrer une réunion.")
            REC_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d_%H%M%S")
            self._path = REC_DIR / f"reunion_{stamp}.wav"
            try:
                wav = wave.open(str(self._path), "wb")
                wav.setnchannels(CHANNELS)
                wav.setsampwidth(2)
                wav.setframerate(self.sample_rate)
            except Exception as e:
                raise RuntimeError(f"Impossible de créer le fichier audio : {e}")

            try:
                self._writer_stop.clear()
                self._sys_stop.clear()
                self._frames_written = 0
                self._dropped_blocks = 0
                self._disk_full = False
                self._error = None
                self._sys_buf = np.zeros(0, dtype=np.float32)
                self._sys_frames = 0
                self._sys_dropped = 0
                self._system_status = "starting"
                self._dom = []
                try:
                    while True:
                        self._q.get_nowait()
                except queue.Empty:
                    pass
                self._writer = threading.Thread(
                    target=self._writer_loop, args=(wav,), daemon=True)
                self._writer.start()
            except Exception:
                try:
                    wav.close()
                except Exception:
                    pass
                raise

            # Système AVANT le micro : le ring se remplit pendant l'ouverture micro.
            # Best-effort : aucune exception ne fait échouer la réunion.
            try:
                self._start_system()
            except Exception as e:
                self._system_status = "never"
                print(f"[visio] capture système non démarrée -> micro seul : {e}")

            def _callback(indata, frames, time_info, status):
                # Thread temps-réel : mix micro + système, RMS, push int16. Jamais lever.
                try:
                    mic = indata[:, 0] if getattr(indata, "ndim", 1) > 1 else indata
                    mic = np.clip(mic, -1.0, 1.0).astype(np.float32, copy=False)
                    n = mic.shape[0]
                    sysb = self._pull_system(n)
                    mixed = mic * _MIX_GAIN + sysb * _MIX_GAIN
                    np.clip(mixed, -1.0, 1.0, out=mixed)
                    try:
                        self._last_rms = float(np.sqrt(np.mean(mixed * mixed)))
                        # v1.0.12 — timeline de dominance (diarisation par source au stop)
                        self._dom.append((float(np.sqrt(np.mean(mic * mic))),
                                          float(np.sqrt(np.mean(sysb * sysb)))))
                    except Exception:
                        pass
                    data = (mixed * 32767.0).astype(np.int16).tobytes()
                    try:
                        self._q.put_nowait(data)
                    except queue.Full:
                        self._dropped_blocks += 1
                except Exception:
                    pass

            try:
                dev_to_use = self.device
                try:
                    if dev_to_use is not None:
                        _info = sd.query_devices(dev_to_use)
                        if int(_info.get("max_input_channels", 0)) < 1:
                            print(f"[visio] device {dev_to_use!r} sans entrée -> micro défaut.")
                            dev_to_use = None
                except Exception:
                    dev_to_use = None
                try:
                    self._stream = sd.InputStream(
                        samplerate=self.sample_rate, channels=CHANNELS, dtype="float32",
                        callback=_callback, blocksize=int(self.sample_rate * BLOCK_SEC),
                        device=dev_to_use)
                    self._stream.start()
                except Exception:
                    if dev_to_use is not None:
                        self._stream = sd.InputStream(
                            samplerate=self.sample_rate, channels=CHANNELS, dtype="float32",
                            callback=_callback, blocksize=int(self.sample_rate * BLOCK_SEC),
                            device=None)
                        self._stream.start()
                    else:
                        raise
            except Exception as e:
                # Micro KO : on rend l'état proprement (comme MeetingRecorder) + on
                # coupe le système (sinon helper orphelin).
                self._writer_stop.set()
                self._sys_stop.set()
                self._kill_helper()
                try:
                    if self._writer:
                        self._writer.join(timeout=2.0)
                except Exception:
                    pass
                self._stream = None
                raise RuntimeError(f"Micro indisponible : {e}")

            self._recording = True
            self._t0 = time.perf_counter()
            return str(self._path)

    # ------------------------------------------------------------------ #
    def stop(self) -> tuple:
        """Stop idempotent, ne lève jamais. Tue le helper. Joint les threads
        AVANT de rendre (aucune écriture du WAV après le retour)."""
        with self._lock:
            if not self._recording:
                return (str(self._path) if self._path else "", self.elapsed())
            self._recording = False
            s = self._stream
            self._stream = None
            self._writer_stop.set()
            self._sys_stop.set()

        # 1. Micro (horloge) fermé HORS verrou, borné (anti-figeage device).
        _close_stream_bounded(s, 3.0)
        # 2. Helper tué (SIGTERM->wait<=3s->kill) — y compris chemin quit.
        self._kill_helper()
        # 3. Joindre les threads système (le kill a fermé les pipes -> EOF).
        for t in (self._sysreader_t, self._stderr_t):
            try:
                if t is not None:
                    t.join(timeout=3.0)
            except Exception:
                pass
        self._sysreader_t = None
        self._stderr_t = None
        # 4. Joindre le writer (peut encore drainer la file).
        try:
            if self._writer is not None:
                self._writer.join(timeout=15.0)
        except Exception:
            pass
        self._writer = None

        # Incidents (après les joins -> compteurs finaux), ajoutés sans écraser.
        if self._dropped_blocks:
            self._note_error(f"{self._dropped_blocks} blocs audio perdus (file pleine)")
        if self._sys_dropped:
            self._note_error(f"{self._sys_dropped} échantillons système écrêtés (dérive)")

        # v1.0.12 — sidecar de dominance (marqueur visio + diarisation par source au pipeline)
        try:
            if self._dom and self._path:
                import numpy as _np
                _np.array(self._dom, dtype="<f4").tofile(str(self._path) + ".dom")
        except Exception:
            pass

        dur = self.elapsed()
        path = str(self._path) if self._path else ""
        return (path, dur)


if __name__ == "__main__":   # test manuel : python visio_recorder.py [duree]
    print("helper:", _helper_path(), "| dispo:", is_helper_available())
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    r = VisioRecorder()
    p = r.start()
    print("REC ->", p)
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(0.5)
        print(f"  t={r.elapsed():.1f}s rms={r.rms_recent():.3f} sys={r.system_status}")
    path, dur = r.stop()
    print(f"STOP -> {path}  dur={dur:.1f}s  err={r.error!r}  sys={r.system_status}")
