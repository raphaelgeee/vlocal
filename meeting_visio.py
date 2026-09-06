#!/usr/bin/env python3
"""
Vlocal — Capture VISIO pour le mode Réunion (brique ISOLÉE, additive).

Objectif : permettre une réunion en VISIO (Meet/Teams/Zoom) sans driver tiers,
en captant l'audio système (les autres participants) via un helper natif
ScreenCaptureKit, puis en le MIXANT avec le micro (toi) dans UNE seule piste
16 kHz mono — exactement le format que le pipeline réunion existant attend.

Principe « ne rien casser » :
  - Présentiel = chemin actuel inchangé (MeetingRecorder seul).
  - Visio      = MeetingRecorder (micro) + ce module (système), puis mixage au
                 stop -> le pipeline existant transcrit/diarise la piste mixée
                 comme une réunion normale. Aucune réécriture du pipeline.

Ce module ne touche à RIEN d'autre. Il est importé paresseusement par le chemin
visio de reunion_start/stop uniquement.

100% local. L'audio système est capté en numérique (avant les écouteurs) -> pas
d'écho, marche avec un casque.
"""

import os
import signal
import subprocess
import sys
import time
import wave

import numpy as np

try:
    import soundfile as _sf          # lecture WAV float (système 48k stéréo)
except Exception:                    # pragma: no cover
    _sf = None
try:
    from scipy.signal import resample_poly as _resample_poly
except Exception:                    # pragma: no cover
    _resample_poly = None

SYS_SR = 48000      # le helper écrit du 48 kHz stéréo float (format ScreenCaptureKit)
OUT_SR = 16000      # format cible (== MeetingRecorder : 16 kHz mono, 16-bit)


# --------------------------------------------------------------------------- #
# Localisation du helper natif `syscapture` (embarqué dans Vlocal.app)
# --------------------------------------------------------------------------- #
def helper_path() -> str | None:
    """Trouve le binaire `syscapture`. Embarqué dans le bundle signé
    (Contents/Helpers/syscapture) ; repli dev pour les tests hors bundle."""
    candidates = []
    # 1) App gelée (PyInstaller) : .../Vlocal.app/Contents/MacOS/<exe>
    exe_dir = os.path.dirname(sys.executable)
    candidates.append(os.path.join(exe_dir, "..", "Helpers", "syscapture"))
    candidates.append(os.path.join(exe_dir, "syscapture"))
    # 2) Ressource PyInstaller
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "syscapture"))
    # 3) Dev : à côté de ce fichier, ou dans le bac à sable Soundbox
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "syscapture"))
    candidates.append(os.path.expanduser("~/Desktop/vlocal-soundbox/syscapture"))
    for c in candidates:
        c = os.path.normpath(c)
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


def available() -> bool:
    """Visio possible si le helper est présent (ScreenCaptureKit = macOS 13+)."""
    return helper_path() is not None


# --------------------------------------------------------------------------- #
# Capture système : lance le helper, l'arrête proprement (SIGTERM)
# --------------------------------------------------------------------------- #
class SystemCapture:
    """Lance `syscapture` en sous-process écrivant un WAV système, jusqu'au stop.
    Le helper hérite de l'autorisation « Enregistrement de l'écran » de Vlocal
    (process responsable = l'app). Best-effort : ne lève jamais."""

    def __init__(self, out_wav: str):
        self.out_wav = out_wav
        self._proc = None
        self._started = False

    def start(self) -> bool:
        hp = helper_path()
        if not hp:
            print("[visio] helper syscapture introuvable — capture système ignorée.")
            return False
        try:
            self._proc = subprocess.Popen(
                [hp, self.out_wav],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._started = True
            return True
        except Exception as e:
            print(f"[visio] lancement syscapture KO : {e}")
            self._proc = None
            return False

    def stop(self, timeout: float = 6.0) -> str | None:
        """Arrête le helper (SIGTERM -> flush WAV) sans jamais bloquer > timeout."""
        p = self._proc
        self._proc = None
        if p is None:
            return None
        try:
            p.send_signal(signal.SIGTERM)
        except Exception:
            pass
        try:
            p.wait(timeout=timeout)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        return self.out_wav if os.path.exists(self.out_wav) else None


# --------------------------------------------------------------------------- #
# Mixage : micro (16k mono) + système (48k stéréo float) -> 16k mono 16-bit
# --------------------------------------------------------------------------- #
def _load_mono_16k(path: str) -> np.ndarray:
    """Charge un WAV quelconque en mono float32 @16 kHz. Vide si illisible."""
    if not path or not os.path.exists(path):
        return np.zeros(0, np.float32)
    try:
        if _sf is not None:
            data, sr = _sf.read(path, dtype="float32", always_2d=True)
            mono = data.mean(axis=1)
        else:                                  # repli : PCM16 via wave
            with wave.open(path, "rb") as wf:
                sr = wf.getframerate(); ch = wf.getnchannels()
                raw = wf.readframes(wf.getnframes())
            mono = (np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0)
            if ch > 1:
                mono = mono.reshape(-1, ch).mean(axis=1)
        if sr != OUT_SR and len(mono) and _resample_poly is not None:
            from math import gcd
            g = gcd(int(sr), OUT_SR)
            mono = _resample_poly(mono, OUT_SR // g, int(sr) // g).astype(np.float32)
        return np.ascontiguousarray(mono, np.float32)
    except Exception as e:
        print(f"[visio] lecture {os.path.basename(path)} KO : {e}")
        return np.zeros(0, np.float32)


def mix_to_wav(mic_wav: str, sys_wav: str, out_wav: str) -> str | None:
    """Mixe micro + système en UNE piste 16 kHz mono 16-bit (format
    MeetingRecorder), prête pour le pipeline réunion existant. Tolérant : si une
    piste manque, écrit l'autre. Renvoie le chemin écrit ou None."""
    mic = _load_mono_16k(mic_wav)
    sysa = _load_mono_16k(sys_wav)
    if len(mic) == 0 and len(sysa) == 0:
        return None
    n = max(len(mic), len(sysa))
    if len(mic) < n:
        mic = np.concatenate([mic, np.zeros(n - len(mic), np.float32)])
    if len(sysa) < n:
        sysa = np.concatenate([sysa, np.zeros(n - len(sysa), np.float32)])
    # Somme avec marge (-3 dB chacun) + limiteur doux pour éviter l'écrêtage.
    mixed = mic * 0.71 + sysa * 0.71
    peak = float(np.max(np.abs(mixed))) if n else 0.0
    if peak > 0.99:
        mixed *= 0.99 / peak
    pcm16 = np.clip(mixed * 32767.0, -32768, 32767).astype("<i2")
    try:
        with wave.open(out_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(OUT_SR)
            wf.writeframes(pcm16.tobytes())
        return out_wav
    except Exception as e:
        print(f"[visio] écriture mix KO : {e}")
        return None


if __name__ == "__main__":   # auto-test rapide (sans modèles) : python meeting_visio.py
    print("helper:", helper_path())
    print("available:", available())
