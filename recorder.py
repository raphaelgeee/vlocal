#!/usr/bin/env python3
"""
Vlocal — Enregistrement audio long pour le mode RÉUNION (robuste, v15).

Architecture producteur/consommateur (corrige les crashs v12-v14) :

  - Le CALLBACK audio (thread temps-réel PortAudio) ne fait QUE :
      1. copier le bloc audio dans une file thread-safe,
      2. calculer un RMS léger pour la wave UI.
    Il ne touche JAMAIS au disque. Écrire le WAV dans le callback (ancien
    code) bloquait le thread temps-réel → xruns → PortAudio abandonnait le
    flux "après quelques minutes", et écrire dans un fichier fermé au stop
    faisait planter tout le process.

  - Un THREAD D'ÉCRITURE dédié draine la file et écrit le WAV sur disque,
    avec flush périodique et surveillance de l'espace disque.

Garanties :
  - stop() est idempotent et ne lève jamais (best-effort, draine puis ferme).
  - Aucune exception du callback ne remonte (capturée), donc pas de crash.
  - Surveillance disque : si l'espace tombe sous le seuil, on arrête proprement
    l'enregistrement et on signale via `disk_full`.
  - Durée calculée sur les frames réellement écrites (fiable).

100% local. Aucune donnée ne quitte la machine.
"""

import os
import queue
import shutil
import threading
import time
import wave
from pathlib import Path

import numpy as np

REC_DIR = Path(os.path.expanduser(
    "~/Library/Application Support/Vlocal/recordings"
))
SAMPLE_RATE = 16000
CHANNELS = 1
MIN_FREE_MB = 500          # refus de démarrer sous ce seuil
STOP_FREE_MB = 200         # arrêt auto si on descend sous ce seuil en cours
BLOCK_SEC = 0.05           # 50 ms par bloc de capture
# File bornée : ~10 s de marge. Si le disque est trop lent et que ça déborde,
# on DROP des blocs (compté) plutôt que de bloquer le thread temps-réel.
_QUEUE_MAX_BLOCKS = int(10 / BLOCK_SEC)


def _free_mb(path: Path) -> int:
    try:
        target = path if path.exists() else path.parent
        st = shutil.disk_usage(str(target))
        return int(st.free / (1024 * 1024))
    except Exception:
        return 0


def _close_stream_bounded(stream, timeout: float = 3.0) -> None:
    """v1.0.9 — ferme un flux audio (stop+close) SANS jamais bloquer l'appelant >
    timeout. Un changement de périphérique peut FIGER close() ; sans borne, le
    verrou du recorder resterait tenu à vie -> réunion impossible à relancer.
    On ferme sur un thread daemon et on abandonne si ça dépasse le délai."""
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
    threading.Thread(target=_close, name="rec-audio-close", daemon=True).start()
    if not done.wait(timeout):
        try:
            print(f"[recorder] fermeture du flux figée (>{timeout:.0f}s) -> abandonnée (auto-heal).")
        except Exception:
            pass


class MeetingRecorder:
    """Enregistre l'audio long dans un WAV, de façon robuste (jamais bloquant,
    jamais crashant). Une instance = un enregistrement (ne pas réutiliser après
    stop ; en créer une nouvelle)."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, device=None):
        self.sample_rate = sample_rate
        # v2 — périphérique d'entrée (None = défaut système). Permet de choisir
        # un device de loopback (BlackHole/Aggregate, Stereo Mix) pour capter le
        # son d'une visio (Meet/Teams) en plus / à la place du micro.
        self.device = device
        self._stream = None
        self._path = None
        self._lock = threading.RLock()
        self._recording = False
        self._t0 = 0.0
        self._last_rms = 0.0

        # File + thread d'écriture
        self._q: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX_BLOCKS)
        self._writer = None
        self._writer_stop = threading.Event()
        self._frames_written = 0          # frames PCM écrites (pour la durée)
        self._dropped_blocks = 0          # blocs perdus si file pleine
        self._disk_full = False           # arrêt forcé manque de place
        self._error = None                # message d'erreur éventuel

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

    def elapsed(self) -> float:
        """Durée écoulée (fiable même si la wave n'a pas démarré)."""
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
    def _writer_loop(self, wav):
        """Thread d'écriture : draine la file vers le WAV, flush périodique,
        surveille l'espace disque. Ne lève jamais (best-effort)."""
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
                        # 2 octets / frame (int16 mono)
                        self._frames_written += len(block) // 2
                    except Exception as e:
                        self._error = f"écriture disque : {e}"
                        break
                # Flush périodique (limite la perte en cas de coupure)
                now = time.perf_counter()
                if now - last_flush > 1.0:
                    try:
                        # wave.Wave_write n'expose pas flush direct ; on s'appuie
                        # sur l'OS. On force via le file object sous-jacent.
                        wav._file.flush()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    last_flush = now
                # Surveillance disque toutes les ~5 s
                if now - last_disk_check > 5.0:
                    if _free_mb(REC_DIR) < STOP_FREE_MB:
                        self._disk_full = True
                        self._error = "espace disque critique"
                        break
                    last_disk_check = now
                # Condition de sortie : stop demandé ET file vidée
                if self._writer_stop.is_set() and self._q.empty():
                    break
        finally:
            # Draine ce qui reste puis ferme le WAV proprement.
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
    def start(self) -> str:
        """Démarre l'enregistrement. Retourne le path WAV.
        Lève RuntimeError si déjà en cours OU espace disque insuffisant."""
        import sounddevice as sd
        with self._lock:
            if self._recording:
                raise RuntimeError("Enregistrement déjà en cours.")
            if not self.disk_ok():
                raise RuntimeError(
                    f"Espace disque insuffisant (moins de {MIN_FREE_MB} Mo "
                    "libres). Libère de l'espace avant de démarrer une réunion.")
            REC_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d_%H%M%S")
            self._path = REC_DIR / f"reunion_{stamp}.wav"

            try:
                wav = wave.open(str(self._path), "wb")
                wav.setnchannels(CHANNELS)
                wav.setsampwidth(2)            # 16 bits
                wav.setframerate(self.sample_rate)
            except Exception as e:
                raise RuntimeError(f"Impossible de créer le fichier audio : {e}")

            # À partir d'ici, si quoi que ce soit échoue AVANT que le thread
            # d'écriture ne prenne possession de `wav` (et donc le ferme dans son
            # finally), on ferme le handle nous-mêmes -> pas de fuite de fichier.
            try:
                # Réinitialise l'état
                self._writer_stop.clear()
                self._frames_written = 0
                self._dropped_blocks = 0
                self._disk_full = False
                self._error = None
                # Vide une éventuelle file résiduelle
                try:
                    while True:
                        self._q.get_nowait()
                except queue.Empty:
                    pass

                # Thread d'écriture AVANT le flux (prêt à drainer)
                self._writer = threading.Thread(
                    target=self._writer_loop, args=(wav,), daemon=True)
                self._writer.start()
            except Exception:
                try:
                    wav.close()
                except Exception:
                    pass
                raise

            def _callback(indata, frames, time_info, status):
                # Thread temps-réel : RAPIDE, ne lève jamais, pas d'I/O disque.
                try:
                    if status:
                        # xrun / overflow : on ne fait que le noter implicitement
                        pass
                    pcm = np.clip(indata, -1.0, 1.0)
                    # RMS léger pour la wave
                    try:
                        self._last_rms = float(np.sqrt(np.mean(pcm * pcm)))
                    except Exception:
                        pass
                    pcm_int16 = (pcm * 32767.0).astype(np.int16)
                    data = pcm_int16.tobytes()
                    try:
                        self._q.put_nowait(data)
                    except queue.Full:
                        # File pleine (disque trop lent) : on DROP ce bloc
                        # plutôt que de bloquer le thread audio.
                        self._dropped_blocks += 1
                except Exception:
                    # Aucune exception ne doit remonter du callback temps-réel.
                    pass

            try:
                # v1.0.1 — GARDE-FOU MICRO : un device configuré SANS entrée
                # (moniteur/sortie choisi par erreur, ex. audio_device=0, ou un
                # loopback débranché) fait lever PaErrorCode -9998 « Invalid number
                # of channels ». On valide max_input_channels et on retombe sur le
                # micro PAR DÉFAUT (comme la dictée) plutôt qu'échouer.
                dev_to_use = self.device
                try:
                    if dev_to_use is not None:
                        _info = sd.query_devices(dev_to_use)
                        if int(_info.get("max_input_channels", 0)) < 1:
                            print(f"[recorder] device {dev_to_use!r} sans entrée "
                                  "(max_input_channels=0) -> repli micro défaut.")
                            dev_to_use = None
                except Exception:
                    dev_to_use = None     # device introuvable -> défaut système
                try:
                    self._stream = sd.InputStream(
                        samplerate=self.sample_rate,
                        channels=CHANNELS,
                        dtype="float32",
                        callback=_callback,
                        blocksize=int(self.sample_rate * BLOCK_SEC),
                        device=dev_to_use,   # None = défaut ; sinon device validé
                    )
                    self._stream.start()
                except Exception:
                    # Dernier filet : si on tentait un device explicite, retente UNE
                    # fois sur le défaut système (couvre device occupé/disparu).
                    if dev_to_use is not None:
                        self._stream = sd.InputStream(
                            samplerate=self.sample_rate, channels=CHANNELS,
                            dtype="float32", callback=_callback,
                            blocksize=int(self.sample_rate * BLOCK_SEC), device=None)
                        self._stream.start()
                    else:
                        raise
            except Exception as e:
                # Nettoyage si le flux ne démarre pas (close AVANT d'oublier la
                # référence, sinon le handle PortAudio fuit).
                self._writer_stop.set()
                try:
                    if self._writer:
                        self._writer.join(timeout=2.0)
                except Exception:
                    pass
                try:
                    if self._stream is not None:
                        self._stream.close()
                except Exception:
                    pass
                self._stream = None
                raise RuntimeError(f"Micro indisponible : {e}")

            self._recording = True
            self._t0 = time.perf_counter()
            return str(self._path)

    # ------------------------------------------------------------------ #
    def stop(self) -> tuple:
        """Stop idempotent. Retourne (path, durée_secondes). Ne lève jamais."""
        with self._lock:
            if not self._recording:
                # Idempotent : si déjà stoppé, renvoie ce qu'on a.
                return (str(self._path) if self._path else "", self.elapsed())
            self._recording = False
            # v1.0.9 — détache le flux sous verrou ; sa fermeture (potentiellement
            # FIGÉE sur un changement de périphérique) se fera HORS verrou, bornée,
            # pour ne JAMAIS retenir _lock à vie (sinon réunion non redémarrable).
            s = self._stream
            self._stream = None
            # Demande au writer de finir de drainer puis de fermer le WAV
            self._writer_stop.set()

        # 1. Ferme le flux audio HORS verrou, de façon bornée (anti-figeage device).
        _close_stream_bounded(s, 3.0)

        # join HORS du lock (le writer peut encore drainer)
        try:
            if self._writer is not None:
                self._writer.join(timeout=15.0)
        except Exception:
            pass
        self._writer = None

        # Blocs perdus (file pleine) : lu ICI, après l'arrêt du flux et le join
        # du writer, pour que le compteur soit final (le callback incrémente
        # tant que le stream tourne). On AJOUTE au message d'erreur existant
        # sans l'écraser (ne masque ni « espace disque critique » ni
        # « écriture disque : … ») ; app.py le remonte via rec.error.
        if self._dropped_blocks:
            msg = f"{self._dropped_blocks} blocs audio perdus (file pleine)"
            self._error = f"{self._error} ; {msg}" if self._error else msg
            try:
                print(f"[recorder] {msg}")
            except Exception:
                pass

        dur = self.elapsed()
        path = str(self._path) if self._path else ""
        return (path, dur)
