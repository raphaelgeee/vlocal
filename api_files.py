"""API du tableau de bord : fichiers audio déposés ou importés.

Méthodes appelées par le JavaScript (window.pywebview.api.*) : transcrire un
fichier déposé sur la fenêtre (dictée), ou l'importer comme réunion (pipeline
complet avec locuteurs). Formats et taille maximale contrôlés avant tout
décodage.
"""
import json
import os
import shutil
import tempfile
import threading

import numpy as np

import applog
import appstate
import mlx_engine
from dictation import copy_to_clipboard
from engine import SAMPLE_RATE
from lifecycle import _begin_activity, _end_activity
from meetings import (
    IMPORT_GPU_MAX_MIN,
    _meeting_lock,
    _meeting_state,
    _reunion_transcribe_pipeline,
)
from reminder_flow import _post_process_by_mode
from settings import _current_mode, _load_settings
from texts import _te
from ui_bridge import _api_safe, _msg, _ui

log = applog.get("api_files")

# Repli CPU d'un fichier déposé : lot de 2 au plus (le lot de 4 coûtait
# +1,4 Go), et seulement avec assez de RAM libre ; sinon monobloc.
CPU_FALLBACK_BATCH = 2
CPU_FALLBACK_MIN_FREE_GB = 2.5


def _decode_to_wav16k(src_path: str, wav_path: str) -> int:
    """Décode un fichier audio quelconque en WAV 16 kHz mono int16, en
    STREAMING (chaque bloc resamplé est écrit au fil de l'eau : RAM bornée).
    Renvoie le nombre d'échantillons écrits. Lève si le décodage échoue."""
    import wave

    import av
    container = av.open(src_path)
    # try/finally : un fichier corrompu fait lever le décodage AVANT close() ;
    # sans lui, descripteur et contextes ffmpeg fuyaient jusqu'au GC.
    n_frames = 0
    try:
        res = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            for fr in container.decode(audio=0):
                for rf in res.resample(fr):
                    pcm = rf.to_ndarray().reshape(-1).astype(np.int16)
                    w.writeframes(pcm.tobytes())
                    n_frames += len(pcm)
    finally:
        container.close()
    return n_frames


def _transcribe_file_gpu(engine, path, on_progress):
    """Voie GPU d'un fichier déposé : même chemin que l'import de réunion
    (engine.transcribe_import_mlx, sans le verrou d'inférence CPU : la dictée
    reste possible pendant ce temps). Renvoie le texte, ou None quand le GPU
    est indisponible, l'audio hors enveloppe, ou la passe GPU en échec."""
    if not mlx_engine.available():
        return None
    tmp_dir = tempfile.mkdtemp(prefix="vlocal_gpu_")
    try:
        wav = os.path.join(tmp_dir, "audio16k.wav")
        n = _decode_to_wav16k(path, wav)
        if not n:
            return None
        if n / float(SAMPLE_RATE * 60) > IMPORT_GPU_MAX_MIN:
            log.info("[fichier] audio au-delà de l'enveloppe GPU : repli CPU.")
            return None
        # memmap int16 : ~0 RAM audio côté app, conversion par bloc côté moteur.
        audio = np.memmap(wav, dtype="<i2", mode="r", offset=44)
        try:
            det = engine.transcribe_import_mlx(audio, sr=SAMPLE_RATE,
                                               on_progress=on_progress)
        finally:
            del audio
        return det.get("text", "")
    except Exception:
        log.warning("[fichier] passe GPU en échec : repli CPU.", exc_info=True)
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _transcribe_file_cpu(engine, path, on_progress):
    """Repli CPU : lot de CPU_FALLBACK_BATCH si la RAM le permet (et hors mode
    économie), sinon transcription monobloc."""
    batch = 0
    try:
        if not _load_settings().get("cpu_economy", False):
            import psutil
            if psutil.virtual_memory().available / 2 ** 30 >= CPU_FALLBACK_MIN_FREE_GB:
                batch = CPU_FALLBACK_BATCH
    except Exception:
        log.warning("[fichier] mesure de la RAM impossible : monobloc.", exc_info=True)
    if batch:
        try:
            det = engine.transcribe_file_batched(path, mode="dictee", batch_size=batch,
                                                 on_progress=on_progress)
            text = det.get("text", "")
            if text:
                return text
        except Exception:
            log.warning("[fichier] transcription par lots en échec : monobloc.",
                        exc_info=True)
    # mode="dictee" : word_timestamps forcé à False dans engine.transcribe_file.
    return engine.transcribe_file(path, on_progress=on_progress, mode="dictee")


class _ImportRefused(Exception):
    """Refus d'import expliqué à l'utilisateur (message fixe, déjà traduit)."""

    def __init__(self, user_message):
        super().__init__(user_message)
        self.user_message = user_message


class FilesApi:
    """Fichiers audio : transcription d'un dépôt, import comme réunion."""

    # ------------------------ v12 : drag & drop fichiers --------------------
    SUPPORTED_AUDIO_EXT = {
        ".mp3", ".mp4", ".m4a", ".wav", ".flac", ".ogg",
        ".opus", ".aac", ".webm", ".mov",
    }

    MAX_AUDIO_BYTES = 200 * 1024 * 1024  # 200 Mo

    # Point de vérité UNIQUE des messages d'erreur (formats acceptés) : dérivé
    # de la constante ci-dessus, jamais écrit en dur dans les messages.
    _EXT_LABEL = " ".join(sorted(SUPPORTED_AUDIO_EXT))

    def _size_limit_msg(self, observed_bytes) -> str:
        """Message unique « fichier trop gros », dérivé de MAX_AUDIO_BYTES (i18n)."""
        _mb = int(observed_bytes) // (1024 * 1024)
        _max = self.MAX_AUDIO_BYTES // (1024 * 1024)
        if appstate.ui_lang == "en":
            return f"File too large ({_mb} MB). Maximum {_max} MB."
        return f"Fichier trop gros ({_mb} Mo). Maximum {_max} Mo."

    def _format_msg(self, ext) -> str:
        """Message unique « format non supporté » (i18n)."""
        return _msg(f"Format non supporté : {ext}. Accepté : {self._EXT_LABEL}",
                    f"Unsupported format: {ext}. Accepted: {self._EXT_LABEL}")

    def transcribe_dropped_audio(self, filename, base64_data):
        """v12 — Réception drag & drop fiable : on accepte le contenu en
        base64 envoyé depuis JS (FileReader.readAsDataURL), on écrit le
        fichier dans un tmp, puis on appelle transcribe_file.

        Cette voie est plus fiable que f.path qui n'est pas exposé par
        WKWebView de pywebview sur macOS.
        """
        import base64 as _b64
        if not base64_data:
            return {"ok": False, "error": _msg("Fichier vide.", "Empty file.")}
        name = os.path.basename(filename or "audio.bin")
        ext = os.path.splitext(name)[1].lower()
        if ext not in self.SUPPORTED_AUDIO_EXT:
            return {"ok": False, "error": self._format_msg(ext)}
        # Taille estimée AVANT décodage : base64 = 4 caractères pour 3 octets.
        if len(base64_data) * 3 // 4 > self.MAX_AUDIO_BYTES:
            return {"ok": False, "error": self._size_limit_msg(len(base64_data) * 3 // 4)}
        try:
            raw = _b64.b64decode(base64_data, validate=False)
        except Exception:
            log.warning("[fichier] décodage base64 en échec", exc_info=True)
            return {"ok": False, "error": _msg(
                "Ce fichier n'a pas pu être lu. Réessayez.",
                "This file could not be read. Try again.")}
        if len(raw) > self.MAX_AUDIO_BYTES:
            return {"ok": False, "error": self._size_limit_msg(len(raw))}
        tmp_dir = tempfile.mkdtemp(prefix="vlocal_drop_")
        tmp_path = os.path.join(tmp_dir, name)
        try:
            with open(tmp_path, "wb") as f:
                f.write(raw)
        except Exception:
            log.warning("[fichier] écriture temporaire en échec", exc_info=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)   # pas de fuite si KO
            return {"ok": False, "error": _msg(
                "Espace disque insuffisant pour lire ce fichier.",
                "Not enough disk space to read this file.")}
        # _cleanup_dir : le dossier tmp est supprimé par le thread de
        # transcription À LA FIN (sinon course : le caller le supprimerait avant
        # que le thread daemon n'ait lu le fichier).
        return self.transcribe_file(tmp_path, _cleanup_dir=tmp_dir)

    def transcribe_file(self, file_path, _cleanup_dir=None):
        """v12 — Transcrit un fichier audio déposé (drag & drop).

        Copie le texte brut au presse-papier, puis auto-détection de rappel si
        le mode actif est DICTÉE. Retourne {ok, text, mode, error}. Aucun emoji.
        """
        if appstate.controller is None:
            return {"ok": False,
                    "error": "Le moteur de transcription se prépare encore, "
                             "réessayez dans un instant."}
        if not file_path:
            return {"ok": False, "error": "Aucun fichier."}
        path = os.path.expanduser(str(file_path))
        if not os.path.exists(path):
            return {"ok": False, "error": _msg("Fichier introuvable.", "File not found.")}
        ext = os.path.splitext(path)[1].lower()
        if ext not in self.SUPPORTED_AUDIO_EXT:
            return {"ok": False, "error": self._format_msg(ext)}
        try:
            size = os.path.getsize(path)
        except OSError:
            log.warning("[fichier] taille illisible", exc_info=True)
            size = 0
        if size > self.MAX_AUDIO_BYTES:
            return {"ok": False, "error": self._size_limit_msg(size)}

        _ui("showToast(%s)" % json.dumps(
            _te("Transcription du fichier en cours...")))
        engine = appstate.controller.engine

        def _progress(p):
            _ui("setFileProgress(%.2f)" % p)

        def _run():
            import time as _t
            t0 = _t.perf_counter()
            # Drapeau « occupé » : rend la transcription de fichier VISIBLE
            # de l'idle-unloader (sinon il pouvait se bloquer sur le verrou
            # d'inférence puis décharger turbo à l'instant où elle finit).
            _meeting_state["file_busy"] = True
            _act = _begin_activity("Vlocal file transcription")  # v21
            try:
                try:
                    # GPU d'abord (chemin de l'import réunion) ; CPU en repli.
                    text = _transcribe_file_gpu(engine, path, _progress)
                    if text is None:
                        text = _transcribe_file_cpu(engine, path, _progress)
                except Exception:
                    log.warning("[fichier] transcription en échec", exc_info=True)
                    _ui("showToast(%s)" % json.dumps(_msg(
                        "La transcription du fichier a échoué. Réessayez.",
                        "File transcription failed. Try again.")))
                    return
                dt = _t.perf_counter() - t0
                if not text:
                    _ui("showToast(%s)" % json.dumps(
                        _te("Aucun son détectable dans le fichier.")))
                    return
                # Copie brut au presse-papier puis appel reveal côté UI
                try:
                    copy_to_clipboard(text)
                except Exception:
                    log.warning("[fichier] copie presse-papier en échec", exc_info=True)
                _ui("showFileResult(%s, %.1f)" % (json.dumps(text), dt))
                # Auto-détection de rappel (sans effet si ce n'est pas un rappel).
                threading.Thread(target=_post_process_by_mode,
                                 args=(text, _current_mode()), daemon=True).start()
            finally:
                _meeting_state["file_busy"] = False
                _end_activity(_act)  # v21
                # Supprime le dossier temporaire (audio déposé) sur TOUS les
                # chemins de sortie -> aucune fuite disque.
                if _cleanup_dir:
                    shutil.rmtree(_cleanup_dir, ignore_errors=True)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    @_api_safe(default=lambda: {"ok": False, "error": "Import indisponible."})
    def import_meeting_audio(self, filename, base64_data, num_speakers=None):
        """Importe un fichier audio et le traite comme une RÉUNION : transcription
        + diarisation WhoTalks + frise temporelle (au lieu de la dictée). Permet
        de diariser un enregistrement/podcast existant, sans le rejouer au micro.

        num_speakers (v29, optionnel) : nombre de participants choisi au sélecteur
        d'import. FORCE le clustering à ce nombre — court-circuite le comptage
        automatique, dont le stress a montré qu'il déraille sous bruit/visio
        (K=3-4 ou K=1) là où K forcé tient (>=0,93 pire cas, ~0,98 réaliste).
        0/None = comptage auto (inchangé). Renvoie {ok, id}."""
        import base64 as _b64
        import time as _t
        if appstate.controller is None:
            return {"ok": False, "error": "Le moteur de transcription se prépare encore."}
        if appstate.store is None:
            return {"ok": False, "error": "Stockage indisponible."}
        name = os.path.basename(filename or "audio")
        ext = os.path.splitext(name)[1].lower()
        if ext not in self.SUPPORTED_AUDIO_EXT:
            return {"ok": False, "error": self._format_msg(ext)}
        # Taille estimée AVANT décodage : on ne matérialise pas 270 Mo de base64
        # décodé pour le refuser ensuite.
        if len(base64_data or "") * 3 // 4 > self.MAX_AUDIO_BYTES:
            return {"ok": False,
                    "error": self._size_limit_msg(len(base64_data) * 3 // 4)}
        with _meeting_lock:
            if _meeting_state.get("recording") or _meeting_state.get("pipeline_busy"):
                return {"ok": False,
                        "error": "Une réunion est déjà en cours de traitement."}
            _meeting_state["pipeline_busy"] = True   # bloque un 2e import/réunion
        tmpdir = None
        try:
            raw = _b64.b64decode(base64_data, validate=False)
            if len(raw) > self.MAX_AUDIO_BYTES:
                # raise (PAS de return) : c'est l'except plus bas qui remet
                # pipeline_busy à False — un return court-circuiterait ce reset.
                raise _ImportRefused(self._size_limit_msg(len(raw)))
            from recorder import REC_DIR
            tmpdir = tempfile.mkdtemp(prefix="vlocal_imp_")
            srcp = os.path.join(tmpdir, name)
            with open(srcp, "wb") as f:
                f.write(raw)
            del raw
            # Conversion -> WAV 16 kHz mono, écrit dans le dossier des réunions
            # (devient le WAV persistant de la réunion importée).
            os.makedirs(str(REC_DIR), exist_ok=True)
            wav_path = os.path.join(str(REC_DIR),
                                    "import_" + _t.strftime("%Y-%m-%d_%H%M%S") + ".wav")
            n_frames = _decode_to_wav16k(srcp, wav_path)
            dur = n_frames / float(SAMPLE_RATE)
            if dur < 0.6:
                try:
                    os.remove(wav_path)   # pas de WAV orphelin de 44 octets
                except OSError:
                    log.warning("[import] WAV vide non supprimé", exc_info=True)
                raise _ImportRefused(_msg(
                    "Aucune piste audio exploitable dans le fichier.",
                    "No usable audio track in this file.") if not n_frames
                    else _msg("Audio trop court.", "Audio too short."))
            titre = "Import · " + (os.path.splitext(name)[0][:40] or "audio")
            mid = appstate.store.add_meeting(titre, wav_path, "transcribing")
            # Import : le pipeline transcrit le WAV de façon INCRÉMENTALE (fenêtres
            # + pacing anti-chauffe) et alimente le diariseur en ligne.
            _meeting_state["live"] = None
            _meeting_state["diar"] = None
            _meeting_state["import"] = True
            _meeting_state["id"] = mid
            # v29 — robustesse : le nombre choisi à l'import FORCE le clustering
            # (consommé une fois par le pipeline partagé). >=1 force ;
            # 0/None laisse le comptage auto.
            if num_speakers is not None and int(num_speakers) >= 1:
                _meeting_state["n_spk_forced"] = int(num_speakers)
            threading.Thread(
                target=_reunion_transcribe_pipeline,
                args=(mid, wav_path, dur), daemon=True).start()
            return {"ok": True, "id": mid}
        except _ImportRefused as e:
            _meeting_state["pipeline_busy"] = False
            return {"ok": False, "error": e.user_message}
        except Exception:
            _meeting_state["pipeline_busy"] = False
            log.warning("[import] réunion KO", exc_info=True)
            return {"ok": False, "error": _msg(
                "Ce fichier n'a pas pu être importé. Vérifiez qu'il s'agit bien "
                "d'un enregistrement audio lisible.",
                "This file could not be imported. Check that it is a readable "
                "audio recording.")}
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
