"""API du tableau de bord : fichiers audio déposés ou importés.

Méthodes appelées par le JavaScript (window.pywebview.api.*) : transcrire un
fichier déposé sur la fenêtre (dictée), ou l'importer comme réunion (pipeline
complet avec locuteurs). Formats et taille maximale contrôlés avant tout
décodage.
"""
import json
import threading

import applog
import appstate
from dictation import copy_to_clipboard
from lifecycle import _begin_activity, _end_activity
from meetings import _meeting_lock, _meeting_state, _reunion_transcribe_pipeline
from reminder_flow import _post_process_by_mode
from settings import _current_mode, _load_settings
from texts import _te
from ui_bridge import _api_safe, _ui

log = applog.get("api_files")


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

    def transcribe_dropped_audio(self, filename, base64_data):
        """v12 — Réception drag & drop fiable : on accepte le contenu en
        base64 envoyé depuis JS (FileReader.readAsDataURL), on écrit le
        fichier dans un tmp, puis on appelle transcribe_file.

        Cette voie est plus fiable que f.path qui n'est pas exposé par
        WKWebView de pywebview sur macOS.
        """
        import base64 as _b64
        import os
        import tempfile
        if not base64_data:
            return {"ok": False, "error": "Fichier vide."}
        name = os.path.basename(filename or "audio.bin")
        ext = os.path.splitext(name)[1].lower()
        if ext not in self.SUPPORTED_AUDIO_EXT:
            return {"ok": False,
                    "error": f"Format non supporté : {ext}. Accepté : "
                             + self._EXT_LABEL}
        try:
            raw = _b64.b64decode(base64_data, validate=False)
        except Exception as e:
            log.debug("_b64.b64decode : échec ignoré", exc_info=True)
            return {"ok": False, "error": f"Décodage échoué : {e}"}
        if len(raw) > self.MAX_AUDIO_BYTES:
            return {"ok": False, "error": self._size_limit_msg(len(raw))}
        # Écriture tmp
        tmp_dir = tempfile.mkdtemp(prefix="vlocal_drop_")
        tmp_path = os.path.join(tmp_dir, name)
        try:
            with open(tmp_path, "wb") as f:
                f.write(raw)
        except Exception as e:
            log.debug("open : échec ignoré", exc_info=True)
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)   # pas de fuite si KO
            return {"ok": False, "error": f"Écriture tmp échouée : {e}"}
        # _cleanup_dir : le dossier tmp est supprimé par le thread de
        # transcription À LA FIN (sinon course : le caller le supprimerait avant
        # que le thread daemon n'ait lu le fichier).
        return self.transcribe_file(tmp_path, _cleanup_dir=tmp_dir)

    def transcribe_file(self, file_path, _cleanup_dir=None):
        """v12 — Transcrit un fichier audio déposé (drag & drop).

        Copie le texte brut au presse-papier, puis auto-détection de rappel si
        le mode actif est DICTÉE. Retourne {ok, text, mode, error}. Aucun emoji.
        """
        import os
        if appstate.controller is None:
            return {"ok": False,
                    "error": "Le moteur de transcription se prépare encore, "
                             "réessayez dans un instant."}
        if not file_path:
            return {"ok": False, "error": "Aucun fichier."}
        path = os.path.expanduser(str(file_path))
        if not os.path.exists(path):
            return {"ok": False, "error": f"Fichier introuvable : {path}"}
        ext = os.path.splitext(path)[1].lower()
        if ext not in self.SUPPORTED_AUDIO_EXT:
            return {"ok": False,
                    "error": f"Format non supporté : {ext}. Accepté : "
                             + self._EXT_LABEL}
        try:
            size = os.path.getsize(path)
        except Exception:
            log.debug("path.getsize : échec ignoré", exc_info=True)
            size = 0
        if size > self.MAX_AUDIO_BYTES:
            return {"ok": False, "error": self._size_limit_msg(size)}

        _ui("showToast(%s)" % json.dumps(
            _te("Transcription du fichier en cours...")))

        def _run():
            import time as _t
            t0 = _t.perf_counter()
            try:
                # Drapeau « occupé » : rend la transcription de fichier VISIBLE
                # de l'idle-unloader (sinon il pouvait se bloquer sur le verrou
                # d'inférence puis décharger turbo à l'instant où elle finit).
                _meeting_state["file_busy"] = True
                _act = _begin_activity("Vlocal file transcription")  # v21
                try:
                    # v20 — VOIE RAPIDE batched si RAM dispo et pas en mode
                    # silencieux (mesuré -33 % sur fichier long, qualité
                    # équivalente) ; repli transparent sur le monobloc.
                    text = ""
                    try:
                        if not _load_settings().get("cpu_economy", False):
                            import psutil as _ps
                            _avail_gb = _ps.virtual_memory().available / 2 ** 30
                            _bs = 4 if _avail_gb >= 6.0 else (2 if _avail_gb >= 2.5 else 0)
                            if _bs:
                                det = appstate.controller.engine.transcribe_file_batched(
                                    path, mode="dictee", batch_size=_bs,
                                    on_progress=lambda p: _ui("setFileProgress(%.2f)" % p))
                                text = det.get("text", "")
                    except Exception:
                        log.debug("get : échec ignoré", exc_info=True)
                        text = ""
                    if not text:
                        # mode="dictee" : voie dictée par fichier (kwargs identiques
                        # à l'ancien défaut "reunion" — word_timestamps est forcé à
                        # False dans engine.transcribe_file).
                        text = appstate.controller.engine.transcribe_file(
                            path,
                            on_progress=lambda p: _ui(
                                "setFileProgress(%.2f)" % p
                            ),
                            mode="dictee",
                        )
                except Exception as e:
                    log.debug("get : échec ignoré", exc_info=True)
                    _ui("showToast(%s)" % json.dumps(
                        _te(f"Erreur de transcription : {e}")))
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
                    log.debug("copy_to_clipboard : échec ignoré", exc_info=True)
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
                    import shutil
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
        import os
        import time as _t
        if appstate.controller is None:
            return {"ok": False, "error": "Le moteur de transcription se prépare encore."}
        if appstate.store is None:
            return {"ok": False, "error": "Stockage indisponible."}
        name = os.path.basename(filename or "audio")
        ext = os.path.splitext(name)[1].lower()
        if ext not in self.SUPPORTED_AUDIO_EXT:
            return {"ok": False,
                    "error": f"Format non supporté : {ext}. Accepté : "
                             + self._EXT_LABEL}
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
                raise ValueError(self._size_limit_msg(len(raw)))
            import shutil
            import tempfile
            import wave
            import numpy as np
            import av
            from recorder import REC_DIR
            tmpdir = tempfile.mkdtemp(prefix="vlocal_imp_")
            srcp = os.path.join(tmpdir, name)
            with open(srcp, "wb") as f:
                f.write(raw)
            # Conversion -> WAV 16 kHz mono, écrit dans le dossier des réunions
            # (devient le WAV persistant de la réunion importée).
            os.makedirs(str(REC_DIR), exist_ok=True)
            wav_path = os.path.join(str(REC_DIR),
                                    "import_" + _t.strftime("%Y-%m-%d_%H%M%S") + ".wav")
            container = av.open(srcp)
            # try/finally : un fichier corrompu/tronqué fait lever le décodage
            # AVANT close() -> sans le finally, descripteur + contextes ffmpeg
            # fuyaient jusqu'au GC (l'erreur reste gérée par l'except global).
            # v20 (RAM) — décodage en STREAMING : chaque chunk resamplé est
            # écrit AU FIL DE L'EAU dans le WAV. Avant : np.concatenate de TOUT
            # le fichier (~230 Mo int16 pour 2 h) AVANT l'écriture -> pic RAM
            # inutile sur 8 Go. Mêmes octets écrits (byte-identique).
            n_frames = 0
            try:
                res = av.AudioResampler(format="s16", layout="mono", rate=16000)
                with wave.open(wav_path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16000)
                    for fr in container.decode(audio=0):
                        for rf in res.resample(fr):
                            pcm = rf.to_ndarray().reshape(-1).astype(np.int16)
                            w.writeframes(pcm.tobytes())
                            n_frames += len(pcm)
            finally:
                container.close()
            if not n_frames:
                try:
                    os.remove(wav_path)   # pas de WAV orphelin de 44 octets
                except Exception:
                    log.debug("os.remove : échec ignoré", exc_info=True)
                raise ValueError("Aucune piste audio exploitable dans le fichier.")
            dur = n_frames / 16000.0
            if dur < 0.6:
                try:
                    os.remove(wav_path)
                except Exception:
                    log.debug("os.remove : échec ignoré", exc_info=True)
                raise ValueError("Audio trop court.")
            titre = "Import · " + (os.path.splitext(name)[0][:40] or "audio")
            mid = appstate.store.add_meeting(titre, wav_path, "transcribing")
            # Import : le pipeline transcrit le WAV de façon INCRÉMENTALE (fenêtres
            # + pacing anti-chauffe) et alimente le diariseur en ligne.
            _meeting_state["live"] = None
            _meeting_state["diar"] = None
            _meeting_state["import"] = True
            _meeting_state["id"] = mid
            # v29 — robustesse : le nombre choisi à l'import FORCE le clustering
            # (consommé une fois par le pipeline partagé, l. ~1336). >=1 force ;
            # 0/None laisse le comptage auto.
            if num_speakers is not None and int(num_speakers) >= 1:
                _meeting_state["n_spk_forced"] = int(num_speakers)
            threading.Thread(
                target=_reunion_transcribe_pipeline,
                args=(mid, wav_path, dur), daemon=True).start()
            return {"ok": True, "id": mid}
        except Exception as e:
            _meeting_state["pipeline_busy"] = False
            log.warning(f"[import] réunion KO : {e}")
            return {"ok": False, "error": f"Import en réunion échoué : {e}"}
        finally:
            if tmpdir:
                try:
                    import shutil
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:
                    log.debug("import shutil : échec ignoré", exc_info=True)
