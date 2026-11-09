#!/usr/bin/env python3
"""
Vlocal — Application desktop (pywebview + moteur Whisper)

Modèle produit (v2) :
  - Aperçu QUASI-LIVE dans la fenêtre pendant qu'on parle
  - Transcription FINALE propre (espaces + ponctuation) au relâchement
  - INSERTION AU CURSEUR par défaut (réglage insert_at_cursor, activé) dans
    l'app active, avec REPLI presse-papier garanti si l'Accessibilité n'est pas
    accordée (le texte reste copié, collable avec Cmd+V) + bouton copier
  - L'insertion auto exige la permission Accessibilité (macOS)

Déclencheurs :
  - Bouton « Dicter » / barre Espace (fenêtre au premier plan)
  - Raccourci GLOBAL Ctrl+Espace (push-to-talk, même hors focus)

Le modèle Whisper est chargé PARESSEUSEMENT au 1er usage puis DÉCHARGÉ sous
pression mémoire (memory_policy.py) pour tenir sur 8 Go ; rechargé tout seul au
prochain usage, pendant la parole. Voir _start_idle_unloader.

Lancement :   ./venv/bin/python app.py
Auto-test  :   ./venv/bin/python app.py --selftest   (sans fenêtre ni micro)
"""

import sys

def _hide_worker_from_dock():
    """v1.0.24 — UN SOUS-PROCESS NE DOIT JAMAIS APPARAÎTRE DANS LE DOCK.

    Un binaire relancé depuis un bundle .app est traité par macOS comme une
    application : il obtient sa propre icône. La ré-identification des locuteurs
    relance le worker d'empreintes par lots (une douzaine de fois sur une
    réunion d'une heure) -> l'utilisateur voyait une icône apparaître et
    disparaître en boucle à côté de celle de Vlocal. Inoffensif, mais alarmant.

    TransformProcessType(kProcessTransformToUIElementApplication) fait passer le
    process en « accessoire » : aucune icône, aucun basculement de fenêtre. On
    passe par ctypes plutôt que par AppKit pour ne rien charger de lourd dans un
    worker dont le temps de démarrage compte. Best-effort strict : si l'appel
    échoue, le worker fonctionne exactement comme avant."""
    try:
        import ctypes
        _lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework"
            "/ApplicationServices")

        class _PSN(ctypes.Structure):
            _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]
        _lib.TransformProcessType(ctypes.byref(_PSN(0, 2)), 4)  # kCurrentProcess, UIElement
    except Exception:
        pass


# v27 — WORKER D'EXTRACTION (sous-process de diarisation) : en app GELÉE, le
# binaire est re-exécuté avec --emb-worker pour extraire un lot d'empreintes
# CAM++ puis SORTIR (l'arène ONNX qui fuit meurt avec lui). Ce shim DOIT
# précéder tout import lourd (engine/webview/overlay) : le worker n'a besoin
# que de diarizer (numpy + sherpa, chargés paresseusement).
if "--emb-worker" in sys.argv:
    _hide_worker_from_dock()
    import diarizer as _dz_worker
    sys.exit(_dz_worker.emb_worker_main(sys.argv))

# v1.0.22 — WORKER MICRO (--mic-worker) : capture de dictée dans un process
# JETABLE (micworker.py). Même patron que --emb-worker : shim AVANT tout import
# lourd — le worker ne charge QUE sounddevice (spawn rapide, ~30 Mo, zéro modèle).
if "--mic-worker" in sys.argv:
    _hide_worker_from_dock()
    import micworker as _mic_worker
    sys.exit(_mic_worker.worker_main(sys.argv))

import json
import os
import re
import threading
import time

# v3.2.9 — TOUR DE CONTRÔLE : journal d'événements structuré (best-effort, jamais
# bloquant). Importé EN GARDE : si le module est absent, la télémétrie se désactive
# et l'app continue normalement (une erreur de log ne doit jamais casser l'app).
try:
    import errors as _EV
except Exception:
    _EV = None

# Verrou HORS-LIGNE HuggingFace — posé AVANT tout import applicatif (engine
# importe faster-whisper, qui embarque huggingface_hub) : sur tout chemin de
# code imprévu, le mode hors-ligne lève immédiatement au lieu d'émettre une
# requête réseau. Le chemin nominal ne télécharge jamais (modèles embarqués)
# -> aucun changement observable. download_whisper.py (outil dev qui a BESOIN
# du réseau) n'est pas affecté : l'env n'est posé que dans app.py.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import appstate
import lifecycle
import processor
import notifier
import overlay
import permissions
# v1.3.2 — au niveau module, et nulle part ailleurs. En 1.2.0, start_global_hotkey()
# utilisait hotkey_mac.FN_KEYCODE AVANT sa ligne `import hotkey_mac` : Python en
# faisait une variable locale non encore définie, UnboundLocalError à chaque
# lancement. tests/test_imports_order.py interdit désormais ce motif.
import obsidian         # v1.0.13 — connecteur Obsidian local (capture des dictées dans un coffre, ADDITIF)
import telemetry        # v1.1.0 — télémétrie minimale déclarée (installs, usage par jour)
from supabase_config import edge_url, auth_headers  # v1.1.0 — backend centralisé
import updater          # v1.0.5 — mise à jour in-app (téléchargement + swap atomique)
from storage import Store
from hotkeys import _rearm_global_hotkey, start_global_hotkey
from engine_runtime import (
    _engine_ready,
    _finish_model_download,
    _load_engine_async,
    _point_mlx_to_cache,
    _post_startup_model_checks,
    _refresh_whisper_bias,
    _resolve_model_dirs,
    _telemetry_profile,
    selftest,
)
from meetings import (
    GUIDED_MAX_MIN,
    MAX_AUTO_SPEAKERS,
    _attribute_blocks,
    _meeting_lock,
    _meeting_state,
    _obsidian_meeting_sync,
    _recognize_voices,
    _reunion_transcribe_pipeline,
)
from dictation import _LOOPBACK_KW, copy_to_clipboard
from reminder_flow import (
    _cancel_reminder_timer,
    _post_process_by_mode,
    _rearm_pending_reminders,
    _schedule_reminder,
)
from lifecycle import (
    _acquire_single_instance,
    _arm_freeze_watchdog,
    _auto_diag_enabled,
    _begin_activity,
    _end_activity,
    _install_lifecycle_delegate_class,
    _quit_app,
    _register_quit_hook,
    _reveal_app_in_finder,
    _start_auto_diag,
)
from ui_bridge import (
    _api_safe,
    _cancel_overlay_hide,
    _menubar_labels,
    _open_settings_modal_robust,
    _refresh_lists_ui,
    _refresh_menubar,
    _schedule_overlay_hide,
    _toast,
    _ui,
)
from settings import (
    VISIO_MODE_ENABLED,
    _APP_SUPPORT,
    _SHOW_REQUEST,
    _current_mode,
    _diarization_enabled,
    _load_glossary_text,
    _load_settings,
    _onboarding_done,
    _save_glossary_text,
    _save_settings,
    _set_mode,
    _setup_file_logging,
    _voice_memory_enabled,
)
from texts import _te

# v16 — Base de ressources « frozen-aware » : en dev c'est le dossier du
# script ; une fois empaqueté par PyInstaller, les ressources (HTML, assets,
# modèle Whisper) sont extraites dans sys._MEIPASS. Tout chemin de ressource
# DOIT passer par BASE_DIR pour fonctionner aussi bien en dev qu'en .app.
if getattr(sys, "frozen", False):
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "vlocal-interface.html")
# v3.3 — APP THIN : les 3 modèles Whisper vivent HORS bundle (cache App Support,
# téléchargés de R2 au 1er lancement -> app ~150 Mo, MAJ = code seul). FILET DE
# SÉCURITÉ : model_store.models_dir() rend le CACHE s'il est complet, SINON le
# bundle. Tant que les modèles restent bundlés, comportement STRICTEMENT identique.
# MODEL_DIR/SMALL_MODEL_DIR sont RE-RÉSOLUS après un téléchargement.
import model_store


_resolve_model_dirs()

_update_busy = False       # v1.0.5 — une préparation de MAJ est en cours (anti-double)
_update_staged = None      # v1.0.5 — chemin de la réplique vérifiée prête à installer
_app_delegate = None       # v29.8 — notre délégué d'app NSApp (terminate -> vrai quit)
_DRAG_VIEW_CLS = None       # v3.2.4 — classe NSView "bande de déplacement" (enregistrée 1 fois)
# Géométrie de la fenêtre principale — point de vérité unique. NB : la CARTE
# CSS (.panel) fait 374 px, soit 6 px de moins que la fenêtre native.
WIN_W = 1180            # v3.1 — dashboard plein écran (était 380, carte flottante)
WIN_H = 780             # v3.1 — (était 720)
PANEL_W = 840            # (hérité ; set_meeting_panel neutralisé en v3.1)
APP_VERSION = "1.4.3"   # 19 septembre 2026 : code de la 1.3.5 (retour arrière). Synchrone avec le fichier VERSION (build) ;
                        # affiché dans l'onglet « Mises à jour ». Historique : CHANGELOG.md.
appstate.app_version = APP_VERSION


# --------------------------------------------------------------------------- #
# API exposée à l'interface (window.pywebview.api.*)
# --------------------------------------------------------------------------- #
class Api:
    @_api_safe(default=False)
    def start_dictation(self):
        if appstate.controller is None:
            # v2 — moteur encore en préparation (démarrage différé).
            _toast("Le moteur de transcription finit de se préparer, un "
                   "instant…", kind="info")
            return False
        # v3.2.8 — micro EXPLICITEMENT refusé : message clair + on ne lance pas une
        # dictée silencieuse (le bouton Dicter n'affiche pas l'overlay -> échec muet
        # sinon). Cas "indéterminé" (status 0) : on laisse le prompt micro NATIF.
        try:
            if permissions.mic_status() == 2:
                if _EV: _EV.log(_EV.E.MIC_DENIED)
                _toast("Micro refusé. Autorisez Vlocal dans Réglages système "
                       "> Confidentialité > Microphone.", kind="error")
                return False
        except Exception:
            pass
        return bool(appstate.controller.start())

    def engine_ready(self):
        """v2 — True si le moteur Whisper est chargé et prêt."""
        return appstate.controller is not None and _engine_ready.is_set()

    @_api_safe(default=False)
    def copy_text(self, text):
        """v2 — Copie fiable via le système (pbcopy/clipboard), au lieu de
        navigator.clipboard qui échoue parfois silencieusement dans WKWebView."""
        return copy_to_clipboard(text or "")

    @_api_safe(default="")
    def stop_dictation(self):
        # Renvoie le texte BRUT final à l'UI (qui le révèle). La copie presse-papier
        # est déjà faite dans le contrôleur. Aucun post-traitement LLM ici (garde-fou).
        # _api_safe garantit qu'un échec rend "" plutôt qu'une promesse rejetée
        # (sinon l'UI resterait bloquée sur "Transcription en cours").
        if appstate.controller is None:
            return ""
        return appstate.controller.stop_and_finalize()

    @_api_safe(default=False)
    def cancel_dictation(self):
        """v2 — Annule la dictée en cours sans transcrire (touche Échap / bouton
        Annuler). Jette l'audio, instantané. True si une dictée a été annulée."""
        if appstate.controller is None:
            return False
        cancelled = appstate.controller.cancel()
        if cancelled:
            _ui("dictationCancelled()")
            _cancel_overlay_hide()
            overlay.reset()
        return cancelled

    def get_mode(self):
        """v11 — Retourne le mode actif (DICTEE/REUNION)."""
        return _current_mode()

    def set_mode(self, mode):
        """v11 — Change le mode actif. Persisté entre sessions. v3.3 — ne persiste
        PAS REUNION si le mode Réunion est désactivé (sinon réouverture en état
        incohérent : UI réunion mais bouton Enregistrer qui refuse)."""
        if (mode or "").upper() == "REUNION" and not appstate.reunion_enabled:
            _toast("Le mode Réunion n'est pas activé. Activez-le dans "
                   "Réglages.", kind="info")
            new = _set_mode("DICTEE")
        else:
            new = _set_mode(mode)
        _refresh_menubar()
        return new

    def is_reunion_enabled(self):
        return appstate.reunion_enabled

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
                    pass
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
                    pass
                raise ValueError("Aucune piste audio exploitable dans le fichier.")
            dur = n_frames / 16000.0
            if dur < 0.6:
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
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
            print(f"[import] réunion KO : {e}")
            return {"ok": False, "error": f"Import en réunion échoué : {e}"}
        finally:
            if tmpdir:
                try:
                    import shutil
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:
                    pass

    # ------------------------ v12 : Mode RÉUNION -----------------------------
    @_api_safe(default=lambda: {"ok": False, "error": "Erreur interne (réunion)."})
    def reunion_start(self, num_speakers=None, mode="presentiel"):
        """v12 — Démarre un enregistrement long en mode Réunion.

        num_speakers (v26, optionnel) : choix du sélecteur « Combien de
        participants ? » pour CETTE réunion (2-6, 0=auto, None=réglage global).
        Stocké dans _meeting_state["n_spk_forced"], consommé une seule fois par
        la pipeline au moment de la diarisation.

        v13.1 — Refuse explicitement de démarrer si :
          (a) une réunion est déjà en enregistrement (`recording=True`)
          (b) la pipeline réunion précédente est encore en cours
              (`pipeline_busy=True`). Sinon les états s'entrechoquaient :
              l'ID dans `_meeting_state` était écrasé par le 2e start
              avant que la 1ʳᵉ pipeline ait fini d'écrire en SQLite, et
              l'UI mélangeait les statuts à l'écran.
        """
        if appstate.controller is None:
            _toast("Le moteur de transcription finit de se préparer, un "
                   "instant…", kind="info")
            return {"ok": False, "error": "Moteur en préparation."}
        if not appstate.reunion_enabled:
            _toast("Le mode Réunion n'est pas activé. Activez-le dans "
                   "Réglages.", kind="info")
            return {"ok": False, "error": "Mode Réunion désactivé."}
        # v29.2 — MUTUELLE EXCLUSION overlay/réunion : on force le masquage DUR de
        # l'overlay de dictée (alpha 0) en entrant en réunion. Sinon un overlay
        # resté visible (dictée figée, transition interrompue) chevauchait l'UI
        # réunion (« deux panneaux superposés »).
        try:
            overlay.hide()
            _cancel_overlay_hide()
        except Exception:
            pass
        # v16.1 — Section critique atomique : check ET prise d'état sous verrou.
        with _meeting_lock:
            if _meeting_state["recording"]:
                return {"ok": False, "error": "Enregistrement déjà en cours."}
            if _meeting_state.get("pipeline_busy"):
                _toast("La réunion précédente est encore en cours de "
                       "traitement. Patientez qu'elle apparaisse comme prête "
                       "dans la liste.", kind="info")
                return {"ok": False,
                        "error": "Pipeline réunion précédente encore en cours."}
            # On marque l'intention TOUT DE SUITE pour bloquer un 2e appel
            # concurrent, avant même d'ouvrir le micro.
            _meeting_state["recording"] = True
            # v1.0.12 — mode de CETTE réunion (présentiel par défaut, visio si demandé).
            _meeting_state["mode"] = mode if mode in ("presentiel", "visio") else "presentiel"
            # v26 — choix per-réunion du sélecteur (None = pas de choix ->
            # purge toute valeur périmée d'une réunion précédente).
            try:
                _meeting_state["n_spk_forced"] = (
                    int(num_speakers) if num_speakers is not None else None)
            except (TypeError, ValueError):
                _meeting_state["n_spk_forced"] = None
            if _meeting_state["n_spk_forced"] is None:
                _meeting_state.pop("n_spk_forced", None)
            try:
                # v13.1 — instance neuve à chaque start (pas de handle wav
                # résiduel d'une session qui aurait planté).
                # v2 — périphérique d'entrée choisi (loopback pour visio, sinon
                # micro par défaut).
                dev = _load_settings().get("audio_device")
                # v1.0.12 — VISIO : recorder qui mixe micro + audio système (helper
                # ScreenCaptureKit) dans le MÊME WAV 16k/mono -> tout l'aval (live,
                # diarisation, pipeline) tourne à l'identique. Repli présentiel
                # automatique si la visio n'est pas disponible (helper absent).
                if _meeting_state.get("mode") == "visio" and self.is_visio_available():
                    from visio_recorder import VisioRecorder
                    rec = VisioRecorder(device=dev)
                else:
                    from recorder import MeetingRecorder
                    rec = MeetingRecorder(device=dev)
                _meeting_state["recorder"] = rec
                wav_path = rec.start()
                import time as _t
                titre = "Réunion " + _t.strftime("%d/%m/%Y %H:%M")
                mid = appstate.store.add_meeting(titre, wav_path, "recording") if appstate.store else None
                _meeting_state["id"] = mid
                # v16 — Transcription INCRÉMENTALE pendant l'enregistrement :
                # au stop, il ne restera qu'une courte fenêtre à traiter (au
                # lieu de passer 1 h de WAV dans Whisper d'un coup).
                _meeting_state["live"] = None
                _meeting_state["diar"] = None
                try:
                    from live_meeting import LiveMeetingTranscriber
                    # WhoTalks v1.1 — diariseur EN LIGNE : on extrait les
                    # empreintes vocales PENDANT la réunion (coût étalé), pour
                    # qu'au stop il ne reste que le clustering (~0,1 s). Chargé
                    # seulement si la diarisation est activée.
                    _diar = None
                    # v1.0.12 — VISIO : PAS de diariseur live CAM++ (la diarisation
                    # se fait par SOURCE micro/système au stop -> on évite de charger
                    # CAM++ + sa fuite d'arène ONNX = RAM ↓, sous le plafond 2,10 Go).
                    if _diarization_enabled() and _meeting_state.get("mode") != "visio":
                        try:
                            import diarizer as _dz
                            if _dz.available():
                                _diar = _dz.Diarizer()
                                _diar.reset_stream()
                        except Exception as e:
                            print(f"[whotalks] diariseur live indispo ({e}).")
                            _diar = None
                    # v1.0.12 — VISIO : fenêtres de transcription plus courtes
                    # (pic RAM MLX ↓ sous 2,10 Go, sans toucher modèle/qualité).
                    # Présentiel : aucun kwarg -> fenêtres engine par défaut (inchangé).
                    _vk = ({"window_target_s": 12.0, "window_max_s": 18.0}
                           if _meeting_state.get("mode") == "visio" else {})
                    lt = LiveMeetingTranscriber(appstate.controller.engine, wav_path,
                                                diarizer=_diar, **_vk)
                    lt.start()
                    _meeting_state["live"] = lt
                    _meeting_state["diar"] = _diar
                except Exception as e:
                    print(f"[live] démarrage incrémental KO ({e}) — "
                          "repli sur transcription complète au stop.")
                # v21 — anti-App Nap pendant l'ENREGISTREMENT : le micro ouvert
                # ne protège PAS d'App Nap (le critère Apple est « playing
                # audio », pas la capture) ; la transcription live tourne
                # fenêtre cachée. Relâchée dans la pipeline (finally) ou sur
                # les chemins d'erreur de reunion_stop.
                _meeting_state["activity"] = _begin_activity("Vlocal meeting recording")
                # v25 — WAVEFORM RÉUNION : sans alimentation de niveau, la wave de
                # l'UI reste FIGÉE à plat pendant une réunion (la boucle
                # setAudioLevel ne tourne qu'en dictée). On pousse ici le RMS du
                # recorder à ~10 Hz, MÊME canal setAudioLevel que la dictée. Thread
                # daemon borné par le drapeau recording (baissé par reunion_stop)
                # -> s'arrête tout seul au stop, aucun thread zombie.
                def _reunion_level_loop(_rec):
                    import time as _lt
                    _last_sys = None
                    while (_meeting_state.get("recording")
                           and getattr(_rec, "recording", False)):
                        try:
                            _ui("setAudioLevel(%.4f)" % _rec.rms_recent())
                            # v1.0.12 — message live si la capture système (visio)
                            # lâche. MeetingRecorder n'a pas system_status -> None
                            # -> ignoré (présentiel strictement inchangé).
                            _ss = getattr(_rec, "system_status", None)
                            if _ss is not None and _ss != _last_sys:
                                _last_sys = _ss
                                if _ss == "denied":
                                    _toast("Audio système non capté : autorisez "
                                           "« Enregistrement de l'écran » pour Vlocal. "
                                           "Réunion en micro seul.", kind="error")
                                elif _ss == "lost":
                                    _toast("Le son système s'est interrompu ; la réunion "
                                           "continue en micro seul.", kind="info")
                                elif _ss == "never":
                                    _toast("Capture du son système indisponible : réunion "
                                           "en micro seul.", kind="info")
                        except Exception:
                            break
                        _lt.sleep(0.1)
                threading.Thread(target=_reunion_level_loop, args=(rec,),
                                 name="vlocal-reunion-level", daemon=True).start()
                return {"ok": True, "wav_path": wav_path, "id": mid}
            except Exception as e:
                # Échec micro/disque : on REND l'état (sinon bloqué en
                # "recording" sans flux). Message clair. n_spk_forced purgé
                # (sinon un IMPORT ultérieur hériterait d'un forçage périmé).
                _meeting_state["recording"] = False
                _meeting_state["recorder"] = None
                _meeting_state["id"] = None
                _meeting_state.pop("n_spk_forced", None)
                _meeting_state.pop("mode", None)
                print(f"[reunion] démarrage impossible : {e}")
                _toast("Impossible de démarrer l'enregistrement : "
                       f"{e}. Vérifiez le micro et l'espace disque.",
                       kind="error")
                return {"ok": False, "error": str(e)}

    @_api_safe(default=lambda: {"ok": False, "error": "Erreur interne (réunion)."})
    def reunion_stop(self):
        """v12 — Stop l'enregistrement et lance la transcription en arrière-plan.

        v16.1 — Atomique + idempotent : un double-clic sur « Arrêter » ne
        déclenche qu'UN seul stop / une seule pipeline."""
        with _meeting_lock:
            rec = _meeting_state.get("recorder")
            if not rec or not rec.recording or not _meeting_state["recording"]:
                return {"ok": False, "error": "Aucun enregistrement actif."}
            # On baisse le drapeau TOUT DE SUITE : un 2e appel concurrent verra
            # recording=False et sortira proprement.
            _meeting_state["recording"] = False
            try:
                wav_path, duree = rec.stop()
            except Exception as e:
                print(f"[reunion] arrêt : {e}")
                _end_activity(_meeting_state.pop("activity", None))  # v21
                return {"ok": False, "error": str(e)}
        # v15 — Remonte un éventuel incident d'enregistrement (disque plein…)
        if getattr(rec, "disk_full", False):
            _ui("if(typeof showToast==='function') showToast(%s);" % json.dumps(
                _te("Espace disque insuffisant : l'enregistrement a été tronqué. "
                    "La partie déjà captée va être transcrite.")))
        elif rec.error:
            print(f"[reunion] incident enregistrement : {rec.error}")
            _toast("Un incident d'écriture a interrompu l'enregistrement ; "
                   "la partie déjà captée va être transcrite.", kind="error")
        # v15 — Audio trop court / silencieux : pas la peine de lancer Whisper.
        if not wav_path or not os.path.exists(wav_path) or duree < 0.6:
            # v16 — arrête proprement le transcripteur incrémental éventuel.
            lt = _meeting_state.get("live")
            _meeting_state["live"] = None
            dz = _meeting_state.get("diar")
            _meeting_state["diar"] = None
            if lt is not None:
                try:
                    lt.finalize()
                except Exception:
                    pass
            # Libère le diariseur live (CAM++) APRÈS lt.finalize() — qui peut
            # encore appeler feed_window et rechargerait l'extracteur si on
            # l'avait déchargé avant. Sans cet unload, l'instance restait
            # référencée (RAM) jusqu'au prochain reunion_start.
            if dz is not None:
                try:
                    dz.unload()
                except Exception:
                    pass
            mid0 = _meeting_state.get("id")
            if appstate.store and mid0:
                try:
                    appstate.store.update_meeting(mid0, duree_audio_s=duree,
                                          status="error")
                except Exception:
                    pass
            _meeting_state.pop("n_spk_forced", None)   # forçage périmé purgé
            _meeting_state.pop("mode", None)
            _toast("Enregistrement trop court, rien à transcrire.", kind="info")
            _refresh_lists_ui()
            _end_activity(_meeting_state.pop("activity", None))  # v21
            return {"ok": False, "error": "Enregistrement trop court."}
        mid = _meeting_state.get("id")
        if appstate.store and mid:
            try:
                appstate.store.update_meeting(
                    mid, duree_audio_s=duree, status="transcribing"
                )
            except Exception:
                pass
        # v16.1 — pipeline_busy posé MAINTENANT (avant de lancer le thread) pour
        # qu'aucun reunion_start ne se faufile dans l'intervalle.
        _meeting_state["pipeline_busy"] = True
        # Lance la pipeline asynchrone : transcription incrémentale
        # (live.finalize(), repli transcribe_detailed) -> diarisation -> SQLite.
        try:
            threading.Thread(
                target=_reunion_transcribe_pipeline,
                args=(mid, wav_path, duree),
                daemon=True,
            ).start()
        except Exception as _err:
            # v3.2.8 — lancement du thread KO (épuisement ressources) : NE JAMAIS
            # laisser pipeline_busy coincé à True, sinon les Réunions sont bloquées
            # à vie ("réunion précédente en cours") jusqu'au redémarrage. On nettoie.
            _meeting_state["pipeline_busy"] = False
            print(f"[reunion] lancement pipeline KO : {_err}")
            if appstate.store and mid:
                try:
                    appstate.store.update_meeting(mid, status="error")
                except Exception:
                    pass
            _toast("Le traitement de la réunion n'a pas pu démarrer. Réessayez.",
                   kind="error")
            _end_activity(_meeting_state.pop("activity", None))
            return {"ok": False, "error": "Le traitement n'a pas pu démarrer."}
        return {"ok": True, "id": mid, "duree": duree}

    # ------------------------ v2 : périphériques audio ----------------------
    @_api_safe(default=list)
    def list_input_devices(self):
        """v2 — Périphériques d'ENTRÉE pour la réunion. Détecte ceux capables de
        capter le son système (loopback) pour transcrire une visio (Meet/Teams)."""
        try:
            import sounddevice as sd
        except Exception:
            return []
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                name = d.get("name", f"Périphérique {i}")
                is_lb = any(k in name.lower() for k in _LOOPBACK_KW)
                out.append({"index": i, "name": name, "loopback": is_lb})
        return out

    @_api_safe(default=False)
    def set_audio_device(self, index):
        """Mémorise le périphérique d'entrée choisi (None/-1 = défaut système)."""
        try:
            idx = int(index)
        except Exception:
            idx = -1
        # v1.5 — le NOM accompagne l'index : la dictée le fait résoudre par le
        # worker micro, dont l'énumération (à jour) ne suit pas celle d'ici.
        name = None
        if idx >= 0:
            import sounddevice as sd
            name = sd.query_devices(idx).get("name")
        _save_settings({"audio_device": (None if idx < 0 else idx),
                        "audio_device_name": name})
        return True

    @_api_safe(default=False)
    def is_visio_available(self):
        """v1.0.12 — La réunion VISIO est-elle possible ? (helper ScreenCaptureKit
        présent + mode non désactivé). N'importe PAS meeting_visio (donc aucune
        dépendance soundfile sur ce chemin). Utilisé par l'UI (toggle) et par
        reunion_start (repli présentiel automatique)."""
        if not VISIO_MODE_ENABLED:
            return False
        try:
            from visio_recorder import is_helper_available
            return bool(is_helper_available())
        except Exception:
            return False

    @_api_safe(default=list)
    def list_meetings(self):
        return appstate.store.list_meetings(limit=10) if appstate.store else []

    def get_meeting(self, mid):
        """v12e — Retourne le détail complet d'une réunion (transcription
        brute + propre) pour la vue détaillée avec toggle.
        v20 — accès SQL ciblé : O(1) au lieu de charger 50 réunions, et les
        réunions au-delà des 50 dernières redeviennent consultables."""
        if appstate.store is None:
            return None
        try:
            return appstate.store.get_meeting(int(mid))
        except Exception:
            return None

    @_api_safe(default=False)
    def delete_meeting(self, mid):
        if appstate.store:
            appstate.store.delete_meeting(int(mid), delete_wav=True)
            _refresh_lists_ui()
        return True

    @_api_safe(default=False)
    def retry_meeting(self, mid, num_speakers=None):
        """v29.4 — RÉESSAYER une réunion en ÉCHEC : re-traite (transcription +
        diarisation) depuis le WAV sauvegardé, sans ré-enregistrer. Réutilise le
        chemin IMPORT sur le WAV existant et MET À JOUR la réunion en place.
        Récupère notamment les réunions ratées par un ancien build. {ok, id}."""
        import os as _os
        import wave as _wave
        if appstate.controller is None or appstate.store is None:
            return {"ok": False, "error": "Le moteur se prépare encore, un instant."}
        m = appstate.store.get_meeting(mid)
        if not m:
            return {"ok": False, "error": "Réunion introuvable."}
        wav = m["wav_path"] if "wav_path" in m.keys() else None
        if not wav or not _os.path.exists(wav):
            return {"ok": False,
                    "error": "L'audio de cette réunion n'est plus disponible, "
                             "réessai impossible."}
        with _meeting_lock:
            if (_meeting_state.get("recording")
                    or _meeting_state.get("pipeline_busy")):
                return {"ok": False,
                        "error": "Un traitement est déjà en cours, réessayez dans "
                                 "un instant."}
            _meeting_state["pipeline_busy"] = True
        try:
            with _wave.open(wav, "rb") as wf:
                dur = wf.getnframes() / float(wf.getframerate())
            appstate.store.update_meeting(mid, status="transcribing")
            _meeting_state["live"] = None
            _meeting_state["diar"] = None
            _meeting_state["import"] = True          # re-traitement depuis le WAV
            _meeting_state["id"] = mid
            if (num_speakers is not None and str(num_speakers).isdigit()
                    and int(num_speakers) >= 1):
                _meeting_state["n_spk_forced"] = int(num_speakers)
            threading.Thread(target=_reunion_transcribe_pipeline,
                             args=(mid, wav, dur), daemon=True).start()
            return {"ok": True, "id": mid}
        except Exception as e:
            _meeting_state["pipeline_busy"] = False
            print(f"[reunion] retry KO : {e}")
            return {"ok": False, "error": f"Réessai impossible : {e}"}

    def rediarize_meeting(self, mid, num_speakers=0):
        """v18.5 — Relance la diarisation d'une réunion DÉJÀ transcrite, sans
        re-enregistrer : on réutilise l'audio (wav_path) + les segments Whisper
        (confidence_json). Permet de corriger une sur-segmentation en fixant le
        nombre de locuteurs. Renvoie le nb de locuteurs, ou False si impossible."""
        m = self.get_meeting(mid)
        if not m or appstate.store is None:
            return False
        wav = m.get("wav_path")
        if not wav or not os.path.exists(wav):
            _toast("L'audio de cette réunion n'est plus disponible : impossible "
                   "de relancer l'identification des locuteurs.", kind="error")
            return False
        try:
            import diarizer
            if not diarizer.available():
                return False
            segs = []
            try:
                segs = (json.loads(m.get("confidence_json") or "{}")
                        or {}).get("segments", [])
            except Exception:
                segs = []
            try:
                n = int(num_speakers) if str(num_speakers).isdigit() else 0
            except Exception:
                n = 0
            if not n:
                _sp = _load_settings().get("meeting_speakers", "auto")
                n = int(_sp) if str(_sp).isdigit() else 0
            # relais RAM (cf. pipeline) : décharge turbo le temps de re-diariser
            # (rechargé au finally SEULEMENT s'il était chargé — sur MLX, None).
            _rl = False
            try:
                if appstate.controller is not None:
                    _rl = getattr(appstate.controller.engine, "model", None) is not None
                    appstate.controller.engine.unload_model()
            except Exception:
                pass
            dia = diarizer.Diarizer()
            try:
                # v26 — même VOIE PRIMAIRE que la pipeline : guidée-transcription
                # (avec le même plafond RAM), repli tuiles. C'est l'action de
                # RATTRAPAGE de l'utilisateur : elle doit avoir la meilleure
                # qualité disponible, pas l'ancienne (tuiles, DER ~0,35).
                spk = None
                try:
                    _dur = float(m.get("duree_audio_s") or 0)
                except Exception:
                    _dur = 0.0
                if segs and 0 < _dur <= GUIDED_MAX_MIN * 60:
                    try:
                        import wave as _wv
                        with _wv.open(wav, "rb") as _wf:
                            _sr16 = _wf.getframerate()
                        # v27 — extraction en sous-process (RAM plate)
                        # v1.0.24 — AVANCEMENT VISIBLE : sur une réunion d'une
                        # heure, cette étape dure plusieurs minutes ; l'utilisateur
                        # n'avait qu'un toast figé « Ré-identification… » et ne
                        # savait pas si l'app travaillait ou était plantée.
                        def _prog(done, total):
                            pct = int(100 * done / max(1, total))
                            _ui("if(typeof setRediarProgress==='function')"
                                "setRediarProgress(%d);" % pct)
                        spk = dia.diarize_from_segments_subproc(
                            wav, _sr16, segs, num_speakers=n,
                            max_speakers=MAX_AUTO_SPEAKERS, on_progress=_prog)
                        _ui("if(typeof setRediarProgress==='function')"
                            "setRediarProgress(100);")
                    except Exception as e:
                        print(f"[reunion] re-diarisation guidée KO ({e}) -> tuiles.")
                        if _EV: _EV.log(_EV.E.DIAR_FALLBACK, where="rediar_exception",
                                        err=str(e)[:120])
                        spk = None
                if not spk:
                    # v1.0.25 — CE REPLI N'EST PLUS SILENCIEUX. La voie guidée
                    # peut rendre une liste VIDE sans lever (worker d'empreintes
                    # KO) : on basculait alors sur l'ancien chemin par tuiles,
                    # nettement moins bon, sans que rien ne l'indique — d'où des
                    # ré-identifications qui « ne changeaient rien ». On le
                    # journalise et on le dit à l'utilisateur.
                    print("[reunion] voie guidée indisponible -> repli tuiles "
                          "(qualité dégradée).")
                    if _EV: _EV.log(_EV.E.DIAR_FALLBACK, where="rediar_empty")
                    _toast("L'analyse fine des voix n'a pas pu démarrer : le "
                           "résultat est moins précis que d'habitude. Relancez "
                           "la ré-identification, et si cela persiste, "
                           "redémarrez Vlocal.", kind="error")
                    spk = dia.diarize(wav, num_speakers=n,
                                      max_speakers=MAX_AUTO_SPEAKERS)
                voices_map = dia.last_voices()        # v1.2 — centroïdes
                if not spk:
                    return False
                units = diarizer.words_from_segments(segs)
                blocks = _attribute_blocks(dia, units, wav, spk)  # attribution au mot ≥98 %
                gl = appstate.store.glossary_pairs() if appstate.store else []
                if gl:
                    for b in blocks:
                        b["text"] = processor.apply_glossary(b["text"], gl)
                # v1.2 — reconnaissance des voix connues (idem pipeline,
                # silencieuse ici : pas de log).
                auto_names = _recognize_voices(voices_map)
                appstate.store.update_meeting(int(mid), speaker_blocks_json=json.dumps(
                    {"blocks": blocks, "names": auto_names, "voices": voices_map},
                    ensure_ascii=False))
                _obsidian_meeting_sync(mid)
                return len({b["speaker"] for b in blocks})
            finally:
                # Déchargement CAM++ + rechargement turbo GARANTIS, y compris
                # sur le retour anticipé (spk vide) et sur exception — et
                # APRÈS l'attribution au mot (assign_words_precise réutilise
                # l'extracteur : le décharger avant le rechargeait aussitôt).
                try:
                    dia.unload()
                except Exception:
                    pass
                if _rl:
                    try:
                        appstate.controller.engine.load_model()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[reunion] re-diarisation KO : {e}")
            return False

    # ------------------------ v18.4 : Historique + Stockage -------------------
    @_api_safe(default=[])
    def get_history(self, limit=200):
        """v18.4 — Timeline unifiée (réunions, dictées, notes, rappels) triée du
        plus récent au plus ancien. Chaque entrée : {type,id,titre,apercu,ts}."""
        if appstate.store is None:
            return []
        items = []
        try:
            for m in appstate.store.list_meetings(limit=limit):
                txt = (m.get("transcription_structuree")
                       or m.get("transcription_brute") or "")
                items.append({"type": "reunion", "id": m.get("id"),
                              "titre": m.get("titre") or "Réunion",
                              "apercu": (txt or "")[:140],
                              "ts": m.get("created_at") or 0})
        except Exception:
            pass
        try:
            for d in appstate.store.list_dictations(limit=limit):
                _c = d.get("content") or ""
                items.append({"type": "dictee", "id": d.get("id"),
                              "titre": "Dictée",
                              "apercu": _c[:140],
                              "full": _c,        # v29.6 — copie directe au clic
                              "ts": d.get("created_at") or 0})
        except Exception:
            pass
        try:
            for t in appstate.store.list_tasks(limit=limit):
                items.append({"type": "rappel", "id": t.get("id"),
                              "titre": t.get("titre") or "Rappel",
                              "apercu": (t.get("echeance") or ""),
                              "ts": t.get("created_at") or 0})
        except Exception:
            pass
        items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        return items[:limit]

    @_api_safe(default=False)
    def set_history_scope(self, scope):
        """v18.4 — Politique de sauvegarde : 'all' (dictées + réunions),
        'meetings' (réunions seulement) ou 'none'. Indiqué clairement à l'UI."""
        scope = scope if scope in ("all", "meetings", "none") else "all"
        _save_settings({"history_scope": scope})
        return True

    @_api_safe(default=0)
    def clear_history(self, scope):
        """v18.4 — Purge l'historique. scope : 'dictees', 'reunions' ou 'tout'.
        Renvoie le nombre d'éléments supprimés. Libère le stockage."""
        if appstate.store is None:
            return 0
        n = 0
        if scope in ("dictees", "tout"):
            n += appstate.store.clear_dictations()
        if scope in ("reunions", "tout"):
            n += appstate.store.clear_meetings(delete_wav=True)
        _refresh_lists_ui()
        return n

    @_api_safe(default={})
    def get_storage_info(self):
        """v18.4 — Espace occupé localement, présenté clairement à l'utilisateur :
        base de données + fichiers audio des réunions (les plus lourds)."""
        info = {"db_mo": 0.0, "audio_mo": 0.0, "total_mo": 0.0,
                "n_reunions": 0, "n_dictees": 0, "audio_files": 0,
                "stats_since": int(_load_settings().get("stats_since", 0) or 0)}
        try:
            if appstate.store is not None and os.path.exists(appstate.store.path):
                info["db_mo"] = round(os.path.getsize(appstate.store.path) / 1e6, 2)
        except Exception:
            pass
        try:
            if appstate.store is not None:
                # v20 — comptages/chemins LÉGERS : plus aucune transcription ni
                # JSON par mot matérialisés juste pour compter (RAM transitoire).
                info["n_reunions"] = appstate.store.count_meetings()
                info["n_dictees"] = appstate.store.count_dictations()
                # v18.5 — on somme les VRAIS fichiers audio référencés par les
                # réunions (wav_path), où qu'ils soient, plutôt qu'un dossier fixe.
                tot = 0
                n = 0
                for _mid, wp in appstate.store.list_meeting_wavs():
                    try:
                        if wp and os.path.exists(wp):
                            tot += os.path.getsize(wp)
                            n += 1
                    except Exception:
                        pass
                info["audio_mo"] = round(tot / 1e6, 2)
                info["audio_files"] = n
        except Exception:
            pass
        info["total_mo"] = round(info["db_mo"] + info["audio_mo"], 2)
        return info

    # ------------------------ v18.2 : panneau réunion à droite ----------------
    @_api_safe(default=False)
    def set_meeting_panel(self, is_open):
        """v3.1 — En dashboard plein écran, le détail réunion est une VUE INTERNE
        du dashboard (panneau dans la fenêtre), plus un redimensionnement natif.
        No-op de sécurité : ne touche plus à la taille de la fenêtre."""
        return True

    # ------------------------ v18 : diarisation (locuteurs) ------------------
    def _load_speaker_payload(self, mid):
        """Charge {blocks, names} d'une réunion depuis speaker_blocks_json."""
        m = self.get_meeting(mid)
        if not m or not m.get("speaker_blocks_json"):
            return None
        try:
            data = json.loads(m["speaker_blocks_json"])
            data.setdefault("blocks", [])
            data.setdefault("names", {})
            return data
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # v20 (G1) — EXPORT FICHIER d'une réunion. Manque produit identifié par
    # l'audit : toute la matière (blocs locuteurs + horodatages) existait en
    # base sans aucune sortie fichier (seul le presse-papier existait).
    # UN dialogue natif d'enregistrement ; le FORMAT suit l'extension choisie :
    #   .txt = compte rendu lisible (Locuteur : texte)
    #   .md  = idem en Markdown (titres + gras)
    #   .srt = sous-titres horodatés (1 cue par bloc locuteur ; nécessite la
    #          diarisation — sinon repli : un seul cue plein texte si durée connue)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _srt_ts(t):
        t = max(0.0, float(t or 0.0))
        ms_total = int(round(t * 1000.0))
        h, rem = divmod(ms_total // 1000, 3600)
        mn, s = divmod(rem, 60)
        return f"{h:02d}:{mn:02d}:{s:02d},{ms_total % 1000:03d}"

    def _export_content(self, m, data, fmt):
        titre = (m.get("titre") or "Réunion").strip()
        created = m.get("created_at") or ""
        label = lambda spk: ((data or {}).get("names", {}) or {}).get(spk) or \
            ("Voix " + str(int(spk.split("_")[-1]) + 1 if "_" in str(spk) else spk))
        blocks = (data or {}).get("blocks") or []
        if fmt == "srt":
            if not blocks:
                return None
            out = []
            for i, b in enumerate(blocks, 1):
                out.append(f"{i}\n{self._srt_ts(b.get('start'))} --> "
                           f"{self._srt_ts(b.get('end'))}\n"
                           f"{label(b.get('speaker'))} : {b.get('text', '').strip()}\n")
            return "\n".join(out)
        texte = (m.get("transcription_structuree")
                 or m.get("transcription_brute") or "")
        if fmt == "md":
            head = f"# {titre}\n\n_{created}_\n\n"
            if blocks:
                return head + "\n\n".join(
                    f"**{label(b.get('speaker'))}** — {b.get('text', '').strip()}"
                    for b in blocks) + "\n"
            return head + texte + "\n"
        # txt (défaut)
        head = f"{titre}\n{created}\n{'-' * 40}\n\n"
        if blocks:
            return head + "\n\n".join(
                f"{label(b.get('speaker'))} : {b.get('text', '').strip()}"
                for b in blocks) + "\n"
        return head + texte + "\n"

    @_api_safe(default=dict)
    def export_meeting(self, mid):
        """Exporte la réunion vers un fichier choisi par l'utilisateur.
        Le format (.txt / .md / .srt) suit l'extension du nom choisi."""
        m = self.get_meeting(mid)
        if not m:
            return {"ok": False, "error": "Réunion introuvable."}
        data = self._load_speaker_payload(mid)
        safe = re.sub(r"[^\w\s.-]", "", (m.get("titre") or "reunion"),
                      flags=re.UNICODE).strip()[:60] or "reunion"
        try:
            import webview as _wv
            dest = appstate.window.create_file_dialog(
                _wv.SAVE_DIALOG,
                directory=os.path.expanduser("~/Documents"),
                save_filename=safe + ".txt")
        except Exception as e:
            return {"ok": False, "error": f"Dialogue d'enregistrement indisponible : {e}"}
        if not dest:
            return {"ok": False, "cancelled": True}
        path = dest if isinstance(dest, str) else (dest[0] if dest else None)
        if not path:
            return {"ok": False, "cancelled": True}
        fmt = os.path.splitext(path)[1].lower().lstrip(".") or "txt"
        if fmt not in ("txt", "md", "srt"):
            fmt = "txt"
            path += ".txt"
        content = self._export_content(m, data, fmt)
        if content is None:
            return {"ok": False,
                    "error": "Export SRT impossible : cette réunion n'a pas de "
                             "blocs locuteurs (diarisation absente)."}
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return {"ok": False, "error": f"Écriture impossible : {e}"}
        return {"ok": True, "path": path, "format": fmt}

    @_api_safe(default=False)
    def rename_speaker(self, mid, speaker_id, name):
        """ÉTAPE 6 — Renomme un locuteur pour cette réunion ET, WhoTalks v1.2,
        ENRÔLE sa voix dans la banque des voix connues (si on l'a entendu assez
        longtemps) : aux réunions suivantes, le nom apparaîtra tout seul. name
        vide -> retire le renommage (la voix connue, elle, reste)."""
        data = self._load_speaker_payload(mid)
        if data is None or appstate.store is None:
            return False
        name = (name or "").strip()
        if name:
            data["names"][speaker_id] = name
        else:
            data["names"].pop(speaker_id, None)
        appstate.store.update_meeting(int(mid), speaker_blocks_json=json.dumps(
            data, ensure_ascii=False))
        _obsidian_meeting_sync(mid)
        # Enrôlement / apprentissage de la voix (centroïde stocké avec la réunion).
        # v29.9 — gaté par le toggle « Mémoriser les voix » (Réglages > Voix
        # connues) : si OFF, on nomme le locuteur dans CETTE réunion mais on
        # n'enregistre AUCUNE empreinte vocale persistante.
        if name and _voice_memory_enabled():
            try:
                import voiceid
                info = (data.get("voices") or {}).get(speaker_id)
                if info and info.get("centroid"):
                    vid = voiceid.enroll_or_update(
                        _APP_SUPPORT, name, info["centroid"],
                        float(info.get("seconds", 0.0)))
                    if vid:
                        print(f"[whotalks] voix « {name} » apprise ({vid}, "
                              f"{round(float(info.get('seconds',0)))}s).")
            except Exception as e:
                print(f"[whotalks] enrôlement ignoré ({e}).")
        return True

    # ---------------- WhoTalks v1.2 : banque de voix connues -----------------
    @_api_safe(default=[])
    def list_known_voices(self):
        """Réglages > Voix connues : [{id,name,seconds,meetings}]."""
        import voiceid
        return voiceid.public_list(_APP_SUPPORT)

    @_api_safe(default=False)
    def rename_known_voice(self, voice_id, name):
        import voiceid
        return voiceid.rename(_APP_SUPPORT, voice_id, name)

    @_api_safe(default=False)
    def delete_known_voice(self, voice_id):
        import voiceid
        return voiceid.delete(_APP_SUPPORT, voice_id)

    @_api_safe(default=False)
    def update_meeting_text(self, mid, text):
        """v18.1 — Édition inline de la transcription continue (sans locuteurs).
        Persiste le texte corrigé par l'utilisateur."""
        if appstate.store is None:
            return False
        appstate.store.update_meeting(int(mid), transcription_structuree=(text or ""))
        return True

    @_api_safe(default=False)
    def update_speaker_block_text(self, mid, block_index, text):
        """v18.1 — Édition inline du texte d'un bloc locuteur. Persiste dans
        speaker_blocks_json."""
        data = self._load_speaker_payload(mid)
        if data is None or appstate.store is None:
            return False
        try:
            data["blocks"][int(block_index)]["text"] = (text or "").strip()
        except Exception:
            return False
        appstate.store.update_meeting(int(mid), speaker_blocks_json=json.dumps(
            data, ensure_ascii=False))
        _obsidian_meeting_sync(mid)
        return True

    # ------------------------ v13 : Glossaire personnel ----------------------
    # Suggestions proposées à la 1ʳᵉ ouverture (l'utilisateur coche celles
    # qu'il garde, rien n'est imposé). Inspiré du brief v13.
    GLOSSARY_SUGGESTIONS = [
        {"incorrect": "Captive",       "correct": "Captiv",     "notes": "Plateforme produit"},
        {"incorrect": "Néolife",       "correct": "Neolife",    "notes": "Client récurrent"},
        {"incorrect": "Oréégami",      "correct": "Oreegami",   "notes": "Client bootcamp marketing"},
        {"incorrect": "DoWino",        "correct": "DOWiNO",     "notes": "Casse exacte"},
        {"incorrect": "Made in IA",    "correct": "Made in AI", "notes": "Nom société (AI en anglais)"},
        {"incorrect": "Padcrabé",      "correct": "Putcrabey",  "notes": "Nom propre — interlocuteur"},
        {"incorrect": "Anthropique",   "correct": "Anthropic",  "notes": "Société Claude (anglais)"},
        {"incorrect": "SuperBase",     "correct": "Supabase",   "notes": "BaaS"},
        {"incorrect": "à Pifi",        "correct": "Apify",      "notes": "Scraping API"},
        {"incorrect": "Full on Reach", "correct": "FullEnrich", "notes": "Enrichissement B2B"},
        {"incorrect": "NOLIF",         "correct": "Neolife",    "notes": "Erreur Whisper observée"},
        {"incorrect": "Hopco",         "correct": "OPCO",       "notes": "Acronyme formation pro"},
        {"incorrect": "BGOM 3",        "correct": "BGE-M3",     "notes": "Modèle embedding"},
        {"incorrect": "Qdrand",        "correct": "Qdrant",     "notes": "Vector DB"},
        {"incorrect": "H Saint",       "correct": "H100",       "notes": "GPU Nvidia"},
        # v2 — Stack IA/dev courant : biaise le moteur dès le départ.
        {"incorrect": "Fulenrich",     "correct": "FullEnrich", "notes": "Enrichissement B2B"},
        {"incorrect": "Haïku",         "correct": "Haiku",      "notes": "Modèle Claude Haiku"},
        {"incorrect": "Anthropique",   "correct": "Anthropic",  "notes": "Société Claude"},
        {"incorrect": "Moku",          "correct": "mockup",     "notes": "Maquette UI"},
        {"incorrect": "Linked In",     "correct": "LinkedIn",   "notes": "Réseau pro"},
        {"incorrect": "prongs",        "correct": "prompts",    "notes": "Prompts LLM"},
        {"incorrect": "I C P",         "correct": "ICP",        "notes": "Ideal Customer Profile"},
        {"incorrect": "Webhook",       "correct": "webhook",    "notes": "Intégration"},
    ]

    @_api_safe(default=list)
    def list_glossary(self):
        return appstate.store.list_glossary() if appstate.store else []

    @_api_safe(default=list)
    def list_glossary_suggestions(self):
        """Suggestions non encore présentes dans la base (case insensitive)."""
        if appstate.store is None:
            return list(self.GLOSSARY_SUGGESTIONS)
        existing = {
            (e.get("incorrect") or "").strip().lower()
            for e in appstate.store.list_glossary()
        }
        return [
            s for s in self.GLOSSARY_SUGGESTIONS
            if s["incorrect"].strip().lower() not in existing
        ]

    @_api_safe(default=None)
    def add_glossary_entry(self, incorrect, correct, notes=""):
        if appstate.store is None:
            return None
        new_id = appstate.store.add_glossary_entry(incorrect, correct, notes or "")
        _refresh_whisper_bias()   # v15 — réinjecte le biais lexical (hotwords)
        return new_id

    @_api_safe(default=False)
    def delete_glossary_entry(self, entry_id):
        if appstate.store is None:
            return False
        appstate.store.delete_glossary_entry(int(entry_id))
        _refresh_whisper_bias()
        return True

    # ------------------------ Vlocal 2 : raccourcis vocaux -------------------
    @_api_safe(default=list)
    def list_snippets(self):
        return appstate.store.list_snippets() if appstate.store else []

    @_api_safe(default=None)
    def add_snippet(self, trigger, expansion):
        if appstate.store is None:
            return None
        return appstate.store.add_snippet(trigger, expansion)

    @_api_safe(default=False)
    def delete_snippet(self, snippet_id):
        if appstate.store is None:
            return False
        appstate.store.delete_snippet(int(snippet_id))
        return True

    @_api_safe(default=0)
    def import_glossary_suggestions(self, entries):
        """v13 — Import en lot des suggestions cochées par l'utilisateur.
        entries : liste de dicts {incorrect, correct, notes?}."""
        if appstate.store is None or not entries:
            return 0
        n = 0
        for e in entries:
            try:
                if appstate.store.add_glossary_entry(
                    e.get("incorrect", ""), e.get("correct", ""), e.get("notes", "")
                ):
                    n += 1
            except Exception:
                pass
        _refresh_whisper_bias()
        return n

    @_api_safe(default=False)
    def set_lang(self, lang):
        """v3.3 — fixe la langue de l'UI backend (fr/en) : messages côté Python
        (toasts, overlay, payloads d'erreur) + overlay de dictée. Poussé par le
        dashboard au démarrage et à chaque changement (il garde sa propre langue
        en localStorage ; ici on aligne le backend)."""
        appstate.ui_lang = "en" if str(lang).lower().startswith("en") else "fr"
        try:
            overlay.set_lang(appstate.ui_lang)
        except Exception:
            pass
        try:
            _refresh_menubar()   # relabel le menu NSStatusItem (Dicter/Ouvrir/Quitter…)
        except Exception:
            pass
        return True

    @_api_safe(default=False)
    def models_need_download(self):
        """v3.3 — True si les modèles Whisper doivent être téléchargés (app thin, 1er run)."""
        return bool(model_store.needs_download())

    @_api_safe(default=lambda: {"ok": False, "error": "Téléchargement des modèles échoué. Vérifiez votre connexion et réessayez."})
    def download_models(self):
        """v3.3 — Télécharge les modèles depuis R2 (bloquant ; progression poussée à
        l'UI via setModelProgress 0..1), puis lance le moteur. {ok:True} ou {ok:False,error}.
        Idempotent + reprise + 3 réessais par fichier (cf. model_store.download_models)."""
        global _dl_in_flight
        if _dl_in_flight:
            return {"ok": False, "error": "Téléchargement déjà en cours."}
        try:   # v3.3 — espace disque suffisant pour ~2,8 Go (sinon faux diagnostic 'connexion')
            import shutil as _sh
            if _sh.disk_usage(os.path.expanduser("~")).free < 3_500_000_000:
                return {"ok": False, "error": "Espace disque insuffisant : libérez environ 3 Go puis réessayez."}
        except Exception:
            pass
        def _prog(done, total, label):
            try:
                _ui("if(typeof setModelProgress==='function') setModelProgress(%.4f);"
                    % (float(done) / total if total else 0.0))
            except Exception:
                pass
        _dl_in_flight = True
        try:
            model_store.download_models(progress=_prog)   # lève si échec définitif -> _api_safe renvoie le default
            if not _finish_model_download():
                # S1 — DL « réussi » mais moteur introuvable : NE PAS débloquer en silence,
                # remonter une erreur -> l'écran préparation montre Réessayer.
                return {"ok": False, "error": "Modèles téléchargés mais moteur introuvable. Réessayez."}
            return {"ok": True}
        finally:
            _dl_in_flight = False

    @_api_safe(default=lambda: {})
    def get_diagnostics(self):
        """v3.3 — TOUR DE CONTRÔLE : état complet pour le diagnostic (à copier/envoyer
        au support). 100% local : AUCUN texte transcrit (events.jsonl exclut déjà
        'text'), aucune donnée personnelle, rien n'est envoyé automatiquement."""
        import platform as _pf
        d = {"version": appstate.app_version, "lang": appstate.ui_lang}
        try:
            d["engine_ready"] = bool(self.engine_ready())
        except Exception:
            d["engine_ready"] = False
        try:
            import mlx_engine as _mlx
            d["gpu_mlx"] = bool(_mlx.available())
        except Exception:
            d["gpu_mlx"] = False
        try:
            d["models_need_download"] = bool(model_store.needs_download())
            d["models_dir"] = model_store.models_dir()
        except Exception:
            pass
        try:
            d["macos"] = _pf.mac_ver()[0]
            d["arch"] = _pf.machine()
        except Exception:
            pass
        try:
            import psutil as _ps
            d["ram_gb"] = round(_ps.virtual_memory().total / 2 ** 30, 1)
        except Exception:
            pass
        try:
            if _EV:
                d["errors_summary"] = _EV.summary()
                d["recent_events"] = _EV.recent(40)
        except Exception:
            pass
        # v1.0.11 — flag dev : pilote l'affichage du détail technique côté UI (les
        # utilisateurs ne voient que le formulaire d'envoi). N'affecte PAS l'envoi.
        d["is_dev"] = os.environ.get("VLOCAL_DEV") == "1"
        try:
            d["auto_diag"] = _auto_diag_enabled()   # v1.0.11 — état effectif du toggle
        except Exception:
            d["auto_diag"] = False
        return d

    @_api_safe(default=lambda: {"ok": False, "reason": "error"})
    def send_support_diagnostic(self, note="", email="", source="app-diag"):
        """v1.0.10 — Envoi d'un diagnostic au support DIRECTEMENT depuis l'app.
        Garantie : AUCUNE donnée personnelle. Pas d'audio, pas de texte dicté
        (events.jsonl exclut déjà 'text'), pas de chemin de fichier (le
        models_dir, qui contient le nom d'utilisateur macOS, est EXCLU ici),
        pas de hostname. On n'envoie que : version, OS/arch, RAM, état
        moteur/GPU, et des compteurs/codes d'incidents anonymes. L'email est
        FACULTATIF (uniquement si l'utilisateur veut une réponse). Le tout part
        dans la table feedback via l'edge submit-feedback (-> admin + notif
        fondateur). Réutilise le canal feedback existant, rien de neuf côté DB."""
        import urllib.request as _u, platform as _pf
        note = (note or "").strip()
        email = (email or "").strip()[:200]
        if len(note) < 3:
            return {"ok": False, "reason": "empty"}
        if len(note) > 3000:
            note = note[:3000]
        # Diagnostic SANITISÉ (zéro chemin, zéro texte, zéro nom d'utilisateur).
        try:
            d = self.get_diagnostics()
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        lines = [
            "Vlocal " + str(d.get("version") or appstate.app_version),
            "System : " + str(d.get("arch") or "?") + " / macOS "
                + str(d.get("macos") or "?") + " / " + str(d.get("ram_gb") or "?") + " GB",
            "Engine : " + ("ready" if d.get("engine_ready") else "loading")
                + " / GPU : " + ("MLX" if d.get("gpu_mlx") else "CPU"),
            "Models : " + ("download pending" if d.get("models_need_download") else "present"),
            "Lang : " + str(d.get("lang") or "?"),
        ]
        try:
            lines.append("Errors : " + json.dumps(d.get("errors_summary") or {}))
        except Exception:
            pass
        ev = d.get("recent_events") or []
        if isinstance(ev, list) and ev:
            lines.append("Recent events :")
            for e in ev[-15:]:
                try:
                    lines.append("  " + str(e.get("ts") or "") + "  " + str(e.get("code") or ""))
                except Exception:
                    pass
        diag = "\n".join(lines)
        message = note + "\n\n— Diagnostic (aucune donnée personnelle) —\n" + diag
        if len(message) > 3900:
            message = message[:3900]
        try:
            os_ver = _pf.mac_ver()[0] or ""
        except Exception:
            os_ver = ""
        payload = {"category": "bug", "message": message,
                   "source": (source or "app-diag")[:20],
                   "app_version": appstate.app_version, "os_version": os_ver}
        if email:
            payload["email"] = email
        url = edge_url("submit-feedback")
        try:
            body = json.dumps(payload).encode("utf-8")
            req = _u.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            for _hk, _hv in auth_headers().items():
                req.add_header(_hk, _hv)
            with _u.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "reason": "network"}
        return {"ok": bool(data.get("ok")), "reason": data.get("reason") or ""}

    # ------------------------ v12c : Réglages MODAL HTML ---------------------
    def open_settings(self):
        """v12c — Ouvre le modal Réglages dans la fenêtre principale.
        Plus de 2e fenêtre webview (pywebview ne supporte pas la création
        après webview.start() de façon fiable). Le modal est dans le HTML
        principal et on l'affiche via JS.

        v13.2 — Refonte robuste après bug terrain : ni le clic menu bar ni
        Cmd+, n'ouvraient le panneau. Cause probable : evaluate_js sur une
        fenêtre cachée pouvait être un no-op silencieux, et l'activation
        Cocoa n'était pas dispatchée sur le main thread depuis tous les
        chemins. On dispatche explicitement la phase Cocoa sur le main
        thread, puis on retente l'injection JS sur 3 frames espacées de
        ~120 ms le temps que la fenêtre soit visible et le DOM prêt.
        """
        _open_settings_modal_robust()
        return True

    def load_glossary(self):
        return _load_glossary_text()

    def save_glossary(self, text):
        return _save_glossary_text(text or "")

    def load_settings(self):
        s = _load_settings()
        # Valeurs par défaut visibles côté UI
        # v20 — défaut ALIGNÉ sur le produit : ctrl_cmd (chord sans bip système,
        # affiché par l'UI et fallback de la table HOTKEYS). L'ancien défaut
        # ctrl_space divergeait des deux.
        s.setdefault("hotkey",
                     os.environ.get("VLOCAL_HOTKEY", "ctrl_cmd"))
        s.setdefault("reunion_enabled", appstate.reunion_enabled)
        s.setdefault("insert_at_cursor", True)         # Vlocal 2
        s.setdefault("dictation_speed", "auto")        # auto | fidele | rapide
        s.setdefault("audio_device", None)             # None = micro par défaut
        s.setdefault("diarization_enabled", True)      # v18 : qui parle quand
        s.setdefault("history_scope", "all")           # v18.4 : all|meetings|none
        s.setdefault("meeting_speakers", "auto")        # WhoTalks : auto fiable
        s.setdefault("obsidian_enabled", False)         # v1.0.13 : capture des dictées dans Obsidian
        s.setdefault("obsidian_vault", None)            # chemin du coffre connecté
        s.setdefault("telemetry_enabled", None)         # v1.1.0 : None = pas encore choisi
        s.setdefault("first_name", "")
        s.setdefault("last_name", "")
        s.setdefault("email", "")                       # v1.2.0 : contact (partagé si accepté)
        return s

    def save_settings(self, patch):
        new = _save_settings(patch or {})
        # v1.3.3 — touche ou mode de déclenchement modifiés : prise d'effet immédiate.
        if any(k in (patch or {}) for k in ("hotkey", "hotkey_mode")):
            threading.Thread(target=_rearm_global_hotkey, name="hotkey-rearm",
                             daemon=True).start()
        # v1.3.4 — Apparence : la bulle de dictée suit le tableau de bord.
        if "theme" in (patch or {}):
            try:
                overlay.set_theme(new.get("theme", "dark"))
            except Exception as _th_err:
                print(f"[overlay] thème non appliqué : {_th_err}")
        # v1.1.0 — partage activé depuis les Réglages : premier envoi tout de suite.
        if (patch or {}).get("telemetry_enabled") is True and appstate.telemetry is not None:
            threading.Thread(target=appstate.telemetry.sync_now, name="telemetry-now",
                             daemon=True).start()
        # Application en chaud quand possible
        try:
            if "reunion_enabled" in patch:
                appstate.reunion_enabled = bool(patch["reunion_enabled"])
                # Synchroniser l'UI principale si ouverte
                _ui("if(typeof _applyReunionFlag==='function')"
                    "_applyReunionFlag(%s);" %
                    ("true" if appstate.reunion_enabled else "false"))
        except Exception:
            pass
        return new

    # ── v1.0.13 — Connexion Obsidian (capture locale des dictées, ADDITIF) ───
    @_api_safe(default=lambda: {"enabled": False, "path": None, "valid": False})
    def obsidian_state(self):
        """État de la connexion Obsidian (lu depuis settings.json)."""
        s = _load_settings()
        path = s.get("obsidian_vault")
        return {"enabled": bool(s.get("obsidian_enabled")),
                "path": path,
                "valid": bool(path) and os.path.isdir(path)}

    @_api_safe(default=lambda: {"ok": False, "error": "Création du coffre impossible."})
    def create_obsidian_vault(self):
        """v1.1.0 — Crée le coffre « Vlocal, ma voix » dans Documents (ou le
        complète s'il existe), l'inscrit dans Obsidian et le connecte. Tout ce qui
        est dit dans Vlocal y est ensuite rangé : dictées, réunions, rappels."""
        r = obsidian.create_voice_vault()
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "Création du coffre impossible."}
        _save_settings({"obsidian_vault": r["path"], "obsidian_enabled": True})
        return {"ok": True, "path": r["path"], "created": bool(r.get("created"))}

    @_api_safe(default=lambda: {"ok": False, "error": "Connexion à Obsidian impossible."})
    def connect_obsidian(self):
        """Détecte AUTOMATIQUEMENT le coffre Obsidian (lit la config d'Obsidian,
        donc indépendant de la version installée) et s'y branche. 100% local. Si
        aucun coffre n'est détecté, l'utilisateur le choisit (pick_obsidian_vault)."""
        try:
            import obsidian as _obs
        except Exception:
            return {"ok": False, "error": "Module Obsidian indisponible."}
        path = _obs.detect_vault()
        if not path:
            return {"ok": False,
                    "error": "Aucun coffre Obsidian détecté. Choisissez-le manuellement."}
        _save_settings({"obsidian_vault": path, "obsidian_enabled": True})
        try:
            _obs.seed_routing(path)
        except Exception:
            pass
        return {"ok": True, "path": path, "auto": True}

    @_api_safe(default=lambda: {"ok": False, "error": "Connexion à Obsidian impossible."})
    def pick_obsidian_vault(self):
        """Choix MANUEL du coffre via le dialogue de dossier natif (repli si la
        détection auto n'a rien trouvé, ou pour changer de coffre)."""
        try:
            import webview as _wv
            dest = appstate.window.create_file_dialog(
                _wv.FOLDER_DIALOG, directory=os.path.expanduser("~"))
        except Exception:
            return {"ok": False, "error": "Connexion à Obsidian impossible."}
        if not dest:
            return {"ok": False, "cancelled": True}
        path = dest if isinstance(dest, str) else (dest[0] if dest else None)
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            import obsidian as _obs
            if not _obs.is_vault(path):
                return {"ok": False, "error": "Ce dossier n'est pas un coffre Obsidian."}
            _save_settings({"obsidian_vault": path, "obsidian_enabled": True})
            _obs.seed_routing(path)
        except Exception:
            return {"ok": False, "error": "Connexion à Obsidian impossible."}
        return {"ok": True, "path": path}

    # ── v1.0 — Activation par clé de licence ────────────────────────────────
    @_api_safe(default=lambda: {"since_day": None, "total": {}, "month": {}, "series": []})
    def usage_stats(self):
        """v1.2.0 — Chiffres EXACTS de l'accueil : lus dans les compteurs
        (usage_days), pas recalculés sur les entrées d'historique affichées.
        L'ancien calcul ne voyait que les 400 dernières et rien du tout si
        l'historique était désactivé : sur une base réelle il affichait 50 min
        là où l'utilisateur avait gagné 4 h 05 dans le mois."""
        return appstate.store.usage_stats() if appstate.store is not None else {
            "since_day": None, "total": {}, "month": {}, "series": []}

    # ------------------- v1.1.0 : identité et télémétrie déclarée -------------
    def telemetry_state(self):
        """Ce que l'utilisateur a choisi de partager, et les compteurs locaux.
        `enabled` vaut None tant qu'aucun choix n'a été fait (l'UI le demande)."""
        try:
            s = _load_settings()
            totals = appstate.store.usage_totals() if appstate.store is not None else {}
            return {"enabled": s.get("telemetry_enabled"),
                    "first_name": s.get("first_name") or "",
                    "last_name": s.get("last_name") or "",
                    "email": s.get("email") or "",
                    "install_id": s.get("install_id") or "",
                    "last_sync": float(s.get("telemetry_last_sync") or 0),
                    "totals": totals}
        except Exception:
            return {"enabled": None, "first_name": "", "last_name": "",
                    "email": "", "install_id": "", "last_sync": 0, "totals": {}}

    def save_identity(self, first_name="", last_name="", email="", enabled=True):
        """Enregistre prénom, nom, e-mail et le choix de partage. Si le partage
        est activé, un envoi part tout de suite (thread, jamais bloquant).
        L'e-mail est validé ici comme côté serveur : une valeur mal formée est
        refusée plutôt que stockée."""
        email = (email or "").strip().lower()[:200]
        if enabled and email and not telemetry.valid_email(email):
            return {"ok": False, "error": _te("Cette adresse e-mail n'est pas valide.")}
        patch = {"first_name": (first_name or "").strip()[:80],
                 "last_name": (last_name or "").strip()[:80],
                 "email": email,
                 "telemetry_enabled": bool(enabled)}
        try:
            _save_settings(patch)
        except Exception:
            return {"ok": False}
        if patch["telemetry_enabled"] and appstate.telemetry is not None:
            threading.Thread(target=appstate.telemetry.sync_now, name="telemetry-now",
                             daemon=True).start()
        return {"ok": True}

    def check_updates(self):
        """v1.0.3 — Y a-t-il une version plus récente ? + éventuel message d'annonce
        (broadcast admin) via get-latest-version. Fail-open total : hors-ligne ->
        offline=True (on n'affirme PAS « à jour »), aucune donnée utilisateur envoyée
        (simple GET). C'est ce qui rend la base installée « rattrapable »."""
        import urllib.request as _u
        cur = appstate.app_version
        out = {"current": cur, "latest": cur, "update": False, "offline": False,
               "url": "https://www.vlocal.org/telecharger", "notes": "", "message": "",
               "length": 0}
        url = edge_url("get-latest-version")
        try:
            req = _u.Request(url)
            for _hk, _hv in auth_headers().items():
                req.add_header(_hk, _hv)
            with _u.urlopen(req, timeout=6) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            latest = (d.get("version") or cur).strip()
            out["latest"] = latest
            out["notes"] = (d.get("notes") or "").strip()
            out["message"] = (d.get("message") or d.get("admin_message") or "").strip()
            if d.get("url"):
                out["url"] = d.get("url")
            try:
                out["length"] = int(d.get("length") or 0)
            except Exception:
                out["length"] = 0
            try:
                pa = [int(x) for x in latest.split(".")]
                pb = [int(x) for x in cur.split(".")]
                out["update"] = pa > pb
            except Exception:
                out["update"] = (latest != cur)
        except Exception:
            out["offline"] = True
        return out

    def update_prepare(self):
        """v1.0.5 — MAJ IN-APP, étape 1/2 : télécharge le DMG de la dernière
        version, VÉRIFIE qu'il est signé Developer ID + notarisé Apple, puis en
        prépare une réplique exacte hors du DMG. Tourne en THREAD (la promesse JS
        revient tout de suite) et pousse l'état vers l'UI :
          setUpdateProgress(frac,label) / updateStage(name) /
          updateReady(version) / updateError(msg).
        Rien n'est installé ici : c'est update_apply() qui déclenche le swap."""
        global _update_busy, _update_staged
        if _update_busy:
            return {"ok": False, "busy": True}
        info = self.check_updates()
        if info.get("offline"):
            _ui("if(typeof updateError==='function') updateError(%s);"
                % json.dumps(_te("Connexion impossible. Réessayez plus tard.")))
            return {"ok": False, "offline": True}
        if not info.get("update"):
            return {"ok": False, "uptodate": True}
        url = info.get("url") or ""
        ver = info.get("latest") or ""
        length = info.get("length") or 0
        if not url.startswith("https://"):
            _ui("if(typeof updateError==='function') updateError(%s);"
                % json.dumps(_te("Lien de mise à jour invalide.")))
            return {"ok": False}
        _update_busy = True

        def _work():
            global _update_busy, _update_staged
            try:
                def _prog(frac, label):
                    _ui("if(typeof setUpdateProgress==='function') setUpdateProgress(%.4f,%s);"
                        % (float(frac), json.dumps(label)))

                def _stage(name):
                    _ui("if(typeof updateStage==='function') updateStage(%s);"
                        % json.dumps(name))

                _stage("download")
                dmg = updater.download(url, length, ver, on_progress=_prog)
                staged = updater.verify_and_stage(dmg, on_stage=_stage)
                _update_staged = staged
                _ui("if(typeof updateReady==='function') updateReady(%s);"
                    % json.dumps(ver))
            except updater.UpdateError as e:
                _ui("if(typeof updateError==='function') updateError(%s);"
                    % json.dumps(str(e)))
            except Exception as e:  # noqa: BLE001
                print(f"[update] prepare KO : {e}")
                _ui("if(typeof updateError==='function') updateError(%s);"
                    % json.dumps(_te("La mise à jour a échoué. Réessayez.")))
            finally:
                _update_busy = False

        threading.Thread(target=_work, daemon=True).start()
        return {"ok": True, "started": True, "version": ver}

    def update_apply(self):
        """v1.0.5 — MAJ IN-APP, étape 2/2 : lance l'installateur détaché (qui
        attend la mort de l'app, remplace le bundle en 2 renames atomiques —
        JAMAIS deux versions — puis ROUVRE Vlocal à jour), affiche « Vlocal va
        se fermer » et termine ce process. Les modèles ne sont pas touchés."""
        global _update_staged
        staged = _update_staged
        if not staged or not os.path.isdir(staged):
            return {"ok": False, "error": _te("Mise à jour non préparée.")}
        try:
            updater.apply_update(staged, os.getpid())
        except updater.UpdateError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            print(f"[update] apply KO : {e}")
            return {"ok": False, "error": _te("L'installation a échoué.")}

        # laisse l'UI peindre l'écran « fermeture » avant de mourir ; l'installateur
        # détaché attend la mort du process puis rouvre l'app à jour.
        threading.Timer(1.2, _quit_app).start()
        return {"ok": True}

    def open_url(self, url):
        """v1.0.3 — Ouvre une URL https de confiance (téléchargement d'une MAJ)
        dans le navigateur par défaut."""
        try:
            u = (url or "").strip()
            # https (téléchargement MAJ) + mailto (lien support@ -> client mail). Rien d'autre.
            if u.startswith("https://") or u.startswith("mailto:"):
                import subprocess
                subprocess.Popen(["open", u])
        except Exception:
            pass
        return {"ok": True}

    # ------------------- v3.1 : onboarding intégré (permissions) -------------
    def perms_state(self):
        """v3.1 — État des permissions macOS pour l'étape Autorisations de
        l'onboarding. mic: 0=non déterminé, 2=refusé, 3=accordé."""
        try:
            acc = bool(permissions.accessibility_ok())
        except Exception:
            acc = False
        try:
            mic = int(permissions.mic_status())
        except Exception:
            mic = 3
        return {"accessibility": acc, "mic": mic}

    def open_perm_settings(self, pane):
        """v3.1 — Bouton « Ouvrir les Réglages » de l'onboarding. Micro non
        déterminé -> dialogue natif ; sinon -> bon panneau Réglages Système."""
        p = "microphone" if str(pane or "").lower().startswith("mic") else "accessibility"
        try:
            if p == "microphone":
                permissions.mic_prompt()        # tentative propre AVFoundation (no-op si déjà décidé)
                # v1.0.13 — DÉCLENCHEUR FIABLE du prompt micro : on ouvre brièvement
                # un flux d'entrée (LE mécanisme de la dictée, prouvé sur le notarisé).
                # requestAccessForMediaType seul ne déclenchait pas toujours le dialogue
                # -> onboarding frais bloqué (app jamais inscrite dans Réglages > Micro,
                # panneau sans « + »). L'ouverture du flux force le prompt + inscrit l'app.
                def _trig_mic():
                    try:
                        import sounddevice as _sd, time as _tt
                        s = _sd.InputStream(samplerate=16000, channels=1,
                                            dtype="float32", blocksize=1600)
                        s.start(); _tt.sleep(0.3)
                        try: s.stop()
                        except Exception: pass
                        try: s.close()
                        except Exception: pass
                    except Exception as _e:
                        print(f"[perm] déclencheur micro KO : {_e}")
                threading.Thread(target=_trig_mic, name="mic-perm-trigger", daemon=True).start()
                # Déjà demandé mais refusé/restreint -> ouvrir les Réglages (l'app y est).
                try:
                    if (not permissions.mic_ok()) and permissions.mic_status() != 0:
                        permissions.open_settings("microphone")
                except Exception:
                    pass
            else:
                # Déclenche le dialogue système (AXIsProcessTrustedWithOptions) :
                # inscrit Vlocal dans la liste Accessibilité de TCC (sinon il
                # peut ne PAS y apparaître) + ouvre le bon panneau.
                permissions.accessibility_prompt()
                permissions.open_settings("accessibility")
        except Exception:
            pass
        return {"ok": True}

    def arm_hotkey(self):
        """v3.1 — Après l'octroi de l'Accessibilité, le raccourci se RÉ-ARME TOUT
        SEUL via l'auto-réparation (hotkey_mac._watch_trust surveille
        AXIsProcessTrusted toutes les 2 s et réinstalle les moniteurs). On NE
        rappelle SURTOUT PAS start_global_hotkey() ici : ça empilerait une 2e paire
        de moniteurs NSEvent -> dictée déclenchée en double. No-op volontaire :
        l'UI peut l'appeler sans risque (le raccourci s'active sous ~2 s)."""
        return {"ok": True}

    def relaunch(self):
        """v1.0.4 — Relance Vlocal PROPREMENT. INDISPENSABLE après l'octroi de
        l'Accessibilité : macOS NE met PAS à jour AXIsProcessTrusted dans un
        process DÉJÀ lancé (comportement Ventura 13.0+), donc l'insertion au
        curseur reste bloquée tant que l'app n'a pas redémarré. On quitte ce
        process puis on ouvre une instance NEUVE qui, elle, voit la permission.
        Le client ne fait qu'UN clic « Redémarrer » — pas besoin de connaître
        Cmd+Q."""
        try:
            import shlex
            try:
                from Foundation import NSBundle
                path = NSBundle.mainBundle().bundlePath() or "/Applications/Vlocal.app"
            except Exception:
                path = "/Applications/Vlocal.app"
            import subprocess
            # le shell attend que CE process meure (sleep), puis ouvre du NEUF (-n)
            # v1.3.2 — on attend la MORT RÉELLE de ce process (20 s au plus) avant de
            # rouvrir. Avec un « sleep 2 » fixe, une fermeture un peu lente laissait
            # la nouvelle instance buter sur le verrou d'instance unique et
            # s'éteindre : Vlocal disparaissait sans revenir.
            pid = os.getpid()
            subprocess.Popen(["/bin/sh", "-c",
                              "i=0; while kill -0 %d 2>/dev/null && [ $i -lt 100 ]; "
                              "do sleep 0.2; i=$((i+1)); done; /usr/bin/open %s"
                              % (pid, shlex.quote(path))])
            # quit RÉEL sur le main thread (sinon l'ancien process survit et
            # `open -n` ouvrirait une 2e instance -> 2 icônes + insertion figée).
            _quit_app()
        except Exception as e:
            print(f"[relaunch] KO : {e}")
        return {"ok": True}

    def repair_insertion(self):
        """v1.0.7 — RÉPARATION INSERTION EN 1 CLIC (zéro Terminal). Le cas vécu :
        après une réinstall / un changement de signature, la case « Vlocal » reste
        cochée dans Accessibilité mais N'AUTORISE PLUS le nouveau binaire (entrée
        TCC « fantôme »). On l'EFFACE (`tccutil reset Accessibility com.vlocal.app`,
        sans sudo, autorisé sous hardened runtime), puis on ré-inscrit Vlocal et on
        ouvre le panneau Accessibilité. L'utilisateur n'a plus qu'à RE-cocher Vlocal
        (macOS interdit à une app de s'auto-autoriser : sécurité), puis l'UI propose
        « Terminer » -> relaunch() prend en compte la coche et l'insertion repart.
        Idempotent, best-effort, ne lève jamais."""
        try:
            import subprocess
            subprocess.run(["tccutil", "reset", "Accessibility", "com.vlocal.app"],
                           check=False, capture_output=True, timeout=10)
            print("[repair] autorisation Accessibilité réinitialisée (case fantôme effacée).")
        except Exception as e:
            print(f"[repair] tccutil reset KO : {e}")
        try:
            # Ré-inscrit Vlocal dans la liste TCC (best-effort ; macOS met le statut
            # de confiance en cache PAR PROCESS, donc la ré-inscription dans CE
            # process peut ne pas suffire — cf. relaunch()). On ouvre le panneau ET
            # on révèle Vlocal.app dans le Finder : si l'app n'apparaît PAS dans la
            # liste, l'utilisateur la glisse sur le « + » (chemin 100 % fiable).
            # v1.0.18 — Fix 1 : la révélation Finder + le guidage « + » règlent le
            # cas vécu « Vlocal introuvable dans les Réglages » après un reset.
            permissions.accessibility_prompt()
            permissions.open_settings("accessibility")
            _reveal_app_in_finder()
        except Exception as e:
            print(f"[repair] ouverture Réglages Accessibilité KO : {e}")
        return {"ok": True}

    # ------------------- v1.0.6 : diagnostic notifications (rappels) ---------
    def notif_status(self):
        """v1.0.6 — Statut RÉEL de l'autorisation de notification pour Vlocal.
        Sert l'encart Réglages > Rappels (« les rappels sonnent-ils ? »).
        Renvoie un CODE stable, le JS i18n rend le libellé."""
        try:
            st = notifier.auth_status()
        except Exception:
            st = "unknown"
        return {"status": st}

    def test_notification(self):
        """v1.0.6 — Bouton « Tester la notification » des Réglages. Si
        l'autorisation n'a jamais été décidée, on déclenche le prompt natif et on
        attend ; puis on envoie une notif de test (icône Vlocal) et on renvoie le
        statut final + si l'envoi a pu partir. Tourne hors main thread (appel JS)."""
        sent = False
        try:
            st = notifier.auth_status()
            if st == "notDetermined":
                notifier.request_authorization()
                st = notifier.auth_status()
            # On tente l'envoi : si refusé, notifier replie (applet/osascript) et
            # la bannière in-app reste de toute façon le filet pour les rappels.
            sent = bool(notifier.show(
                _te("Test de notification"),
                _te("Si vous voyez ceci, les rappels Vlocal fonctionnent."),
                subtitle=None))
        except Exception as e:
            print(f"[notif] test KO : {e}")
            st = "unknown"
        return {"status": st, "sent": sent}

    def open_notif_settings(self):
        """v1.0.6 — Ouvre Réglages Système > Notifications (pour réactiver Vlocal
        si l'utilisateur avait refusé). macOS ne deep-linke pas par app de façon
        fiable -> on ouvre le panneau Notifications, Vlocal y est listé."""
        try:
            if sys.platform == "darwin":
                from AppKit import NSWorkspace
                from Foundation import NSURL
                url = NSURL.URLWithString_(
                    "x-apple.systempreferences:com.apple.Notifications-Settings.extension")
                NSWorkspace.sharedWorkspace().openURL_(url)
        except Exception as e:
            print(f"[notif] ouverture Réglages Notifications KO : {e}")
        return {"ok": True}

    # v15 — API « Puissance machine » SUPPRIMÉE (plus de tier ni de SLM).

    # ---------- v6 : API pour la persistance (consommée par l'UI) ----------
    @_api_safe(default=list)
    def list_tasks(self):
        # v9 TOP-5 #5 — on relève la limite à 50, l'UI scroll
        return appstate.store.list_tasks(limit=50) if appstate.store else []

    @_api_safe(default=False)
    def rename_task(self, task_id, new_title):
        """v9 TOP-5 #4 — édition inline du titre d'une tâche."""
        if appstate.store is None or not new_title:
            return False
        ok = appstate.store.rename_task(int(task_id), str(new_title).strip()[:200])
        _refresh_menubar()
        return ok

    @_api_safe(default=None)
    def toggle_task(self, task_id):
        if appstate.store is None:
            return None
        new = appstate.store.toggle_task(int(task_id))
        # v20 — tâche cochée -> son timer ne sonnera pas ; re-décochée avant
        # l'échéance -> ré-armement.
        try:
            if new == "done":
                _cancel_reminder_timer(int(task_id))
            elif new == "open":
                for t in appstate.store.list_tasks(limit=500, only_open=True):
                    if t.get("id") == int(task_id):
                        ts = notifier.parse_iso(t.get("echeance_iso") or "")
                        if ts and ts > time.time():
                            _schedule_reminder(ts, (t.get("titre") or "")[:120],
                                               task_id=int(task_id))
                        break
        except Exception:
            pass
        _refresh_menubar()
        return new

    @_api_safe(default=False)
    def delete_task(self, task_id):
        if appstate.store is None:
            return False
        appstate.store.delete_task(int(task_id))
        _cancel_reminder_timer(int(task_id))   # v20 — ne sonne plus après suppression
        _refresh_menubar()
        return True

    def hide_window(self):
        """v7 — masque la fenêtre sans tuer le process. L'app reste vivante via
        le menu bar (clic 'Ouvrir Vlocal' pour la rouvrir)."""
        try:
            if appstate.window is not None:
                appstate.window.hide()
                appstate.window_visible = False
        except Exception:
            pass
        return True


# v2 — Paramètres + chargement DIFFÉRÉ du moteur Whisper (démarrage instantané).
_dl_in_flight = False       # v3.3 (N2) — garde anti-double téléchargement modèles


_instance_lock = None   # garde le verrou d'instance unique en vie


def main():
    global _instance_lock

    # v3.2.3 — FIX DÉFINITIF « DEUX ICÔNES DOCK » (constaté sur le vif : 2 process
    # "Foreground"). Cause : le multiprocessing.resource_tracker (créé par numba/MLX
    # au chargement du moteur) RE-EXÉCUTE le binaire du bundle -> LaunchServices lui
    # colle une 2e icône Dock. set_start_method('fork') NE corrige PAS (le tracker se
    # ré-exécute toujours — vérifié). SOLUTION détection-free : on démarre TOUTE
    # invocation en politique "Prohibited" (aucune icône Dock), puis SEUL le process
    # principal passe en "Accessory" (icône barre de menus, AUCUNE icône Dock) APRÈS
    # freeze_support. Le child tracker est dérouté PAR freeze_support avant ce point
    # -> il reste "Prohibited" (aucune icône). v3.3 : on abandonne "Regular" (icône
    # Dock) au profit de l'icône menu-bar V -> 1 SEULE icône, dans la barre des menus
    # (pattern Wispr Flow / SuperWhisper), ce qui résout aussi le « deux icônes ».
    # v3.3.1 (bug #6) — politique d'activation à 3 états. 0 = Regular (icône DOCK
    # + minimisation possible), 1 = Accessory (icône menu-bar V seule, pas de Dock),
    # 2 = Prohibited (aucune icône). Le child resource_tracker (numba/MLX) est
    # dérouté par freeze_support AVANT tout passage en Regular -> il reste Prohibited
    # -> le bug « deux icônes Dock » ne revient pas. Icône Dock seulement pour le
    # process principal et quand la fenêtre dashboard est visible.
    def _set_dock(policy):
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication().setActivationPolicy_(policy)
        except Exception:
            pass
    # Compat : conserve l'API booléenne existante. True (fenêtre visible) -> Regular
    # (icône Dock) ; False (fenêtre masquée) -> Accessory (menu-bar V seul, pas de Dock).
    def _set_dock_visible(_visible):
        _set_dock(0 if _visible else 1)
    _set_dock(2)                      # tout démarre Prohibited -> child sans aucune icône
    try:
        import multiprocessing
        multiprocessing.freeze_support()   # le child tracker/spawn part ICI -> reste Prohibited
    except Exception:
        pass
    _set_dock_visible(True)           # process principal + fenêtre visible -> Regular (icône Dock)

    _setup_file_logging()

    if "--selftest" in sys.argv:
        # Le selftest a besoin du moteur synchrone.
        sys.exit(selftest())

    # Instance unique : si Vlocal tourne déjà, on n'ouvre pas un 2e moteur (RAM).
    # Au lieu de mourir en silence (confusion « ça ne s'ouvre pas »), on RAMÈNE
    # AU PREMIER PLAN l'instance déjà ouverte — comportement macOS attendu.
    _instance_lock = _acquire_single_instance()
    if _instance_lock is None:
        print("[vlocal] déjà en cours — demande d'affichage de l'instance existante.")
        # L'instance existante peut avoir sa fenêtre CACHÉE (agent en arrière-plan).
        # On dépose une « demande d'affichage » qu'elle surveille -> elle ré-affiche
        # sa fenêtre. (Sinon : relancer l'app ne montrait rien = « je n'arrive pas
        # à la relancer ».) Puis on active et on sort.
        try:
            with open(_SHOW_REQUEST, "w") as f:
                f.write("show")
        except Exception:
            pass
        try:
            from AppKit import (NSRunningApplication,
                                NSApplicationActivateIgnoringOtherApps)
            me = os.getpid()
            for ap in NSRunningApplication.runningApplicationsWithBundleIdentifier_(
                    "com.vlocal.app"):
                if ap.processIdentifier() != me:
                    ap.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        except Exception:
            pass
        sys.exit(0)

    import webview
    _install_lifecycle_delegate_class()   # v1.3.2 — avant toute fenêtre (voir plus haut)
    # v3.3 (B1) — pywebview remet la policy en « Regular » (icône Dock) à l'IMPORT
    # (classe BrowserView). On RÉ-IMPOSE Accessory (icône menu-bar V, AUCUNE icône
    # Dock) ici, puis ENCORE après l'affichage de la fenêtre (_activate_on_launch).
    # Sinon : Dock + menu-bar = la régression « deux icônes » que la v3.3 tue.
    _set_dock_visible(True)

    # Modèle principal : turbo int8 ; small int8 chargé à la demande (chemins
    # ABSOLUS via BASE_DIR, robustes en .app). VLOCAL_WHISPER=small force un
    # repli si jamais le turbo est absent (dev).
    chosen = os.environ.get("VLOCAL_WHISPER", "").lower()
    _resolve_model_dirs()      # v3.3 — chemins à jour (cache si complet, sinon bundle)
    _point_mlx_to_cache()
    turbo_dir = appstate.model_dir
    small_dir = appstate.small_model_dir

    def _has_model(d):
        return os.path.exists(os.path.join(d, "model.bin"))

    if chosen == "small" and _has_model(small_dir):
        whisper_dir = small_dir
    elif _has_model(turbo_dir):
        whisper_dir = turbo_dir
    elif _has_model(small_dir):
        whisper_dir = small_dir
    elif model_store.needs_download():
        # v3.3 — APP THIN, 1er lancement (ou MAJ depuis un build bundlé) : modèles
        # pas encore en cache. NE PAS quitter : l'UI affiche l'écran « préparation »
        # (download_models) AVANT toute dictée, puis _finish_model_download charge le
        # moteur. _engine_load_params reste None d'ici là (la dictée attend les modèles).
        whisper_dir = None
        print("[whisper] modèles absents -> téléchargement R2 requis (app thin).")
    else:
        print(f"[erreur]  aucun modèle Whisper trouvé sous {model_store.models_dir()}")
        sys.exit(1)
    # v2 — Threads CPU ADAPTATIFS : on exploite les cœurs disponibles (mesuré
    # ~25-40 % plus rapide qu'à 4 threads), en laissant 2 cœurs au système.
    # R&D vitesse v19 : PLAFOND À 4 (mesuré). Au-delà de 4, turbo ne gagne quasi
    # rien (−3 % idle) MAIS chauffe beaucoup (core-secondes turbo −46 % / small
    # −64 % en passant de 8 à 4) et dispute des cœurs à Slack/Chrome. À 4 : small
    # −45 % (1,8x), variance réduite, sortie BYTE-IDENTIQUE (vérifié, SHA256
    # constant 2/4/6/8 threads). Override « perf » possible via VLOCAL_THREADS.
    # v20 — base = cœurs PERFORMANCE réels (Apple Silicon : os.cpu_count()
    # compte AUSSI les E-cores ; saturer des E-cores = plus lent ET plus chaud).
    # Repli os.cpu_count() si sysctl indisponible (Intel, etc.).
    _ncpu = os.cpu_count() or 4
    try:
        import subprocess as _sp
        _pcores = int(_sp.run(["sysctl", "-n", "hw.perflevel0.logicalcpu"],
                              capture_output=True, text=True, timeout=2).stdout.strip())
        if _pcores > 0:
            _ncpu = min(_ncpu, _pcores + 2)   # P-cores + marge (ne JAMAIS élargir)
    except Exception:
        pass
    _default_threads = max(2, min(4, _ncpu - 2))
    # Mode SILENCIEUX (Réglages) : moitié des cœurs (borné [2,4]) -> les ventilos
    # se calment pendant la transcription, au prix d'un peu de vitesse. Utile sur
    # machines modestes (8 Go).
    try:
        if _load_settings().get("cpu_economy", False):
            _default_threads = max(2, min(4, _ncpu // 2))
    except Exception:
        pass
    cpu_threads = int(os.environ.get("VLOCAL_THREADS", str(_default_threads)))
    # v20 — la diarisation (ONNX) respecte le MÊME plafond mesuré (avant : elle
    # montait à min(8, cpu-2) et ignorait cpu_economy/VLOCAL_THREADS).
    try:
        import diarizer as _dz_threads
        _dz_threads.set_num_threads(cpu_threads)
    except Exception:
        pass

    # v6 — persistance locale (rapide) : chargée AVANT la fenêtre (les listes
    # s'affichent tout de suite). Le moteur Whisper (lent) charge en tâche de
    # fond -> DÉMARRAGE INSTANTANÉ, aucune attente perçue.
    try:
        appstate.store = Store()
        print(f"[base]    {appstate.store.path}")
    except Exception as e:
        appstate.store = None
        print(f"[base]    persistance désactivée ({e}) — mode brut intact.")

    # v1.0.5 — AUTO-RÉPARATION MAJ : si on tourne, on a démarré depuis
    # /Applications/Vlocal.app ; tout reste d'une MAJ (backup/incoming) est donc
    # périmé -> on le retire pour GARANTIR une seule version/icône.
    try:
        updater.cleanup_leftovers()
    except Exception as _e:
        print(f"[update] cleanup au démarrage ignoré : {_e}")

    # v3.2.9 — CORRECTIF STALE-UI (trouvé en TEST sur device) : WKWebView met en
    # cache le HTML par URL ; le chemin /Applications étant constant, une MISE À
    # JOUR servait l'ANCIENNE interface (vu : 3.2.9 installée mais design 3.2.8
    # affiché). On vide le cache WebKit UNE SEULE FOIS quand la version change ->
    # chaque MAJ montre la nouvelle UI. Best-effort, jamais bloquant, AVANT toute
    # création de WKWebView (dashboard + overlay).
    try:
        _ui_marker = os.path.join(_APP_SUPPORT, "ui_version.txt")
        try:
            with open(_ui_marker, "r") as _f:
                _last_ui = _f.read().strip()
        except Exception:
            _last_ui = None
        if _last_ui != appstate.app_version:
            import shutil as _sh
            for _wd in ("~/Library/WebKit/com.vlocal.app",
                        "~/Library/Caches/com.vlocal.app"):
                _p = os.path.expanduser(_wd)
                if os.path.isdir(_p):
                    _sh.rmtree(_p, ignore_errors=True)
            try:
                os.makedirs(_APP_SUPPORT, exist_ok=True)
                with open(_ui_marker, "w") as _f:
                    _f.write(appstate.app_version)
            except Exception:
                pass
            print(f"[ui]      cache WebKit purgé (MAJ {_last_ui} -> {appstate.app_version}) : UI fraîche garantie.")
    except Exception as _e:
        print(f"[ui]      purge cache WebKit ignorée : {_e}")

    # Paramètres pour le chargement différé du moteur (lancé dans _after_start).
    appstate.cpu_threads = cpu_threads
    if whisper_dir is not None:
        appstate.engine_load_params = (whisper_dir, cpu_threads)
    # whisper_dir None = app thin, 1er lancement : moteur chargé APRÈS le download.

    # v10b — UNIQUEMENT la carte arrondie liquid glass, rien autour.
    # transparent=True + background_color totalement transparent → la fenêtre
    # macOS est invisible sauf là où il y a du contenu CSS opaque (= la carte).
    # La carte porte SON PROPRE fond charbon opaque (rgba 92%) pour rester
    # lisible quel que soit le mode système clair/sombre.
    # v10d — Fenêtre EXACTEMENT à la taille de la carte (fenêtre WIN_W×WIN_H,
    # la carte CSS .panel fait 374 px de large).
    # Plus aucune zone tampon, plus aucune drop-shadow CSS → plus aucun halo.
    # Le border-radius:30px de .panel + NSWindow.opaque=NO découpe les coins
    # arrondis ; tout ce qui n'est pas la carte est 100% invisible.
    appstate.api = Api()
    # v1.1.0 — identifiant d'installation (UUID aléatoire, jamais dérivé de la
    # machine) + planificateur de télémétrie. Rien ne part tant que l'utilisateur
    # n'a pas fait son choix (telemetry_enabled is True), cf. telemetry.py.
    # v1.3.6, une copie de l'identifiant vit dans les préférences macOS : un
    # dossier de l'app vidé, ou une réinstallation, ne crée plus une deuxième
    # « installation » de la même personne dans la console.
    try:
        telemetry.ensure_install_id(_load_settings, _save_settings)
        import platform as _plat
        appstate.telemetry = telemetry.Telemetry(_load_settings, _save_settings,
                                         lambda: appstate.store, appstate.app_version,
                                         _plat.mac_ver()[0] or "",
                                         get_profile=_telemetry_profile)
        appstate.telemetry.start()
    except Exception as e:
        print(f"[telemetry] non démarrée : {e}")
    _start_auto_diag()   # v1.0.11 — diagnostic auto consenti (cohorte early/testeurs)
    appstate.window = webview.create_window(
        "Vlocal",
        HTML_PATH,
        js_api=appstate.api,
        width=WIN_W,
        height=WIN_H,
        min_size=(940, 640),   # v3.1 — dashboard redimensionnable
        resizable=True,
        # v3.2.2 — VRAIE fenêtre macOS : feux rouge/jaune/vert FONCTIONNELS,
        # déplaçable, bord net. _style_window_native() (après affichage) rend la
        # barre de titre transparente et étend le contenu sous les feux (look
        # premium type Linear/Things). AVANT (v3.1) : frameless+transparent avec de
        # FAUSSES pastilles non câblées (-> « je ne peux pas quitter ») + un encart
        # sombre de 16px (-> « gros contour »). Repli : si l'astuce premium échoue,
        # fenêtre macOS standard (feux + titre visibles) — jamais bloquée.
        background_color="#05060a",
        on_top=False,
    )

    # v19 — AGENT DE DICTÉE : fermer la fenêtre ne doit PAS quitter l'app (sinon
    # le raccourci global meurt = « ça quitte tout seul »). On CACHE la fenêtre
    # et l'app reste vivante en arrière-plan (icône menu bar) ; « Ouvrir Vlocal »
    # la ré-affiche. Le raccourci fonctionne donc même fenêtre fermée.
    def _on_window_closing():
        # Quitter explicitement (menu bar « Quitter Vlocal ») -> on laisse fermer.
        if appstate.quitting:
            return True
        try:
            appstate.window.hide()
            appstate.window_visible = False
        except Exception:
            pass
        return False   # fermeture fenêtre = masquage -> process maintenu en vie
    try:
        appstate.window.events.closing += _on_window_closing
        print("[window] fermeture = masquage (agent maintenu en vie).")
    except Exception as _e:
        print(f"[window] hook closing indisponible : {_e}")

    # v3.2.4 — FENÊTRE PREMIUM, PROPRE & DÉPLAÇABLE : contenu PLEIN CADRE (les vrais
    # feux macOS rouge/jaune/vert flottent dans le design, AUCUNE barre visible) +
    # une BANDE DE DÉPLACEMENT invisible en haut. Sans barre de titre, la WKWebView
    # capte la souris -> la fenêtre n'était plus déplaçable (bug terrain) ; cette vue,
    # dont tout glissé déplace la fenêtre (mouseDownCanMoveWindow=YES), corrige ça.
    # Appliqué sur le MAIN THREAD à l'affichage (avec retries). Repli : fenêtre macOS
    # standard (jamais bloquée).
    def _drag_view_cls():
        global _DRAG_VIEW_CLS
        if _DRAG_VIEW_CLS is None:
            import objc
            from AppKit import NSView
            class _VlocalDragView(NSView):
                # v1.0.1 — DRAG ROBUSTE. mouseDownCanMoveWindow=YES seul ne suffisait
                # pas : après le chargement, pywebview refait first responder de la
                # WKWebView, qui re-captait la souris -> figeage ~5s. Ici on DÉCLENCHE
                # explicitement le drag natif de la fenêtre via performWindowDragWithEvent_
                # (API macOS 10.11+, indépendante du first responder et du hit-test-move).
                def mouseDownCanMoveWindow(self):
                    return True
                def acceptsFirstMouse_(self, event):
                    return True          # attrapable même fenêtre non focus
                def mouseDown_(self, event):
                    w = self.window()
                    try:
                        if w is not None and w.respondsToSelector_("performWindowDragWithEvent:"):
                            w.performWindowDragWithEvent_(event)
                            return
                    except Exception:
                        pass
                    try:
                        objc.super(_VlocalDragView, self).mouseDown_(event)
                    except Exception:
                        pass
            _DRAG_VIEW_CLS = _VlocalDragView
        return _DRAG_VIEW_CLS
    def _style_window_native(_tries=0):
        applied = False
        try:
            from AppKit import NSApplication, NSColor
            from Foundation import NSMakeRect
            dark = NSColor.colorWithSRGBRed_green_blue_alpha_(5/255.0, 6/255.0, 10/255.0, 1.0)
            for w in NSApplication.sharedApplication().windows():
                try:
                    # Fenêtre du dashboard = TITRÉE (styleMask & 1) ; l'overlay de
                    # dictée est un NSPanel borderless -> exclu.
                    if not (w.styleMask() & 1):
                        continue
                    w.setTitlebarAppearsTransparent_(True)
                    w.setTitleVisibility_(1)                     # NSWindowTitleHidden
                    w.setStyleMask_(w.styleMask() | (1 << 15))   # FullSizeContentView -> plein cadre
                    w.setBackgroundColor_(dark)                  # tout résidu reste charbon (jamais blanc)
                    # v3.2.9 — DÉPLAÇABLE PARTOUT : en plus de la bande de drag native
                    # (ci-dessous, agrandie), on autorise le déplacement par le fond.
                    try:
                        w.setMovableByWindowBackground_(True)
                    except Exception:
                        pass
                    # v3.2.6 — APPARENCE SOMBRE forcée : sans ça, le chrome de la barre
                    # de titre reste en mode clair (liseré plus clair sous les feux,
                    # repéré à l'œil). En dark, la barre devient NOIRE -> feux fondus
                    # dans le noir, zéro démarcation (façon Apple).
                    try:
                        from AppKit import NSAppearance
                        w.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
                    except Exception:
                        pass
                    # Bande de déplacement invisible (haut, hauteur d'une barre de
                    # titre). Posée UNE seule fois (identifiant). Les feux flottent
                    # AU-DESSUS (boutons de fenêtre, z-order supérieur) ; le contenu
                    # sous ~28px reste cliquable (les en-têtes ont 30px de marge haute).
                    cv = w.contentView()
                    if cv is not None:
                        # v3.3.2 — bande de déplacement RE-CRÉÉE à CHAQUE passage (shown ET
                        # loaded). pywebview ré-installe la WKWebView en contentView à la fin
                        # du 1er chargement (~5s, cocoa didFinishNavigation) + lui donne le
                        # focus souris -> l'ancienne bande était enterrée/détachée et la
                        # fenêtre SE FIGEAIT après ~5s. On retire toute ancienne bande puis on
                        # en ajoute une fraîche EN DERNIER -> z-order AU-DESSUS de la webview.
                        for _sv in list(cv.subviews()):
                            try:
                                if (_sv.identifier() or "") == "vlocalDrag":
                                    _sv.removeFromSuperview()
                            except Exception:
                                pass
                        # v1.0.8 — FIX CLICS SIDEBAR BLOQUÉS (plein écran surtout) : la bande
                        # de drag est OPAQUE et avale tout clic dans sa zone (mouseDown ->
                        # performWindowDrag). Avant, elle était PLEINE LARGEUR x 64px -> elle
                        # recouvrait le haut de la SIDEBAR (et, en plein écran, le contenu
                        # remonte -> plus d'entrées nav sous la bande), d'où "impossible de
                        # cliquer Historique/onglets". Désormais : la bande EXCLUT la sidebar
                        # (démarre après ses 240px) et fait la hauteur d'une barre de titre.
                        # -> sidebar 100% cliquable ; on drague la fenêtre depuis le header du
                        # contenu (et via -webkit-app-region:drag côté web). Les feux flottent
                        # au-dessus. Le contenu (bouton Dicter ~y155) reste sous la bande.
                        b = cv.bounds()
                        H = 52.0
                        SIDE = 240.0                              # largeur sidebar : JAMAIS couverte
                        dv = _drag_view_cls().alloc().initWithFrame_(
                            NSMakeRect(SIDE, b.size.height - H, max(0.0, b.size.width - SIDE), H))
                        dv.setIdentifier_("vlocalDrag")
                        dv.setAutoresizingMask_(2 | 8)           # WidthSizable | MinYMargin -> ancrée en haut, à droite de la sidebar
                        cv.addSubview_(dv)
                    applied = True
                except Exception:
                    continue
        except Exception as e:
            print(f"[window] style premium indisponible ({e}) — fenêtre macOS standard.")
            return
        if applied:
            print("[window] premium : plein cadre + feux fondus + bande de déplacement.")
            return
        # Fenêtre pas encore prête -> on réessaie quelques tours de boucle.
        if _tries < 15:
            try:
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(
                    lambda: _style_window_native(_tries + 1))
            except Exception:
                pass
    def _on_window_shown():
        try:
            from Foundation import NSOperationQueue
            NSOperationQueue.mainQueue().addOperationWithBlock_(_style_window_native)
        except Exception:
            pass
    try:
        appstate.window.events.shown += _on_window_shown
    except Exception:
        pass
    # v3.3.2 — RE-APPLIQUER APRÈS CHARGEMENT (fix fenêtre figée ~5s) : pywebview fait
    # setContentView_(webview) + makeFirstResponder_(webview) à la fin de la 1re
    # navigation (cocoa didFinishNavigation), ce qui enterre la bande de drag. On
    # ré-applique style + bande sur « loaded » (postérieur au reset) pour la remettre
    # au-dessus de la webview. addOperationWithBlock_ garantit l'ordre (runloop suivant).
    try:
        appstate.window.events.loaded += _on_window_shown
    except Exception:
        pass
    # v1.0.1 — FILET ANTI-FIGEAGE : pywebview peut ré-installer la contentView à des
    # instants variables après le chargement (gros HTML + polices). On ré-affirme la
    # bande de drag plusieurs fois sur les premières secondes (toujours sur le main
    # thread) pour qu'elle soit TOUJOURS fraîche et au-dessus, quel que soit le timing.
    try:
        import threading as _thr
        from Foundation import NSOperationQueue as _NSOQ
        for _d in (0.6, 1.5, 3.0, 5.0, 7.0, 9.0):
            _thr.Timer(_d, lambda: _NSOQ.mainQueue().addOperationWithBlock_(_style_window_native)).start()
    except Exception:
        pass

    def _after_start():
        # v2 — Charge le moteur Whisper en tâche de fond TOUT DE SUITE (la
        # fenêtre est déjà affichée -> démarrage instantané).
        threading.Thread(target=_load_engine_async, daemon=True).start()

        # N'affirme « actif » QUE si le raccourci a réellement démarré (les cas
        # 'none' et échec d'installation logguent déjà leur propre état).
        # v1.3.2 — ÉTAPE ISOLÉE. En 1.2.0/1.3.0 une exception ici sautait tout le
        # démarrage (menu, délégué, chien de garde). Désormais : trace complète,
        # incident compté dans la tour de contrôle, message à l'utilisateur, et
        # le reste du démarrage continue.
        try:
            if start_global_hotkey() is not None:
                print("[hotkey]  actif : maintiens ton raccourci global pour dicter.")
        except Exception as _hk_err:
            import traceback as _tb
            _tb.print_exc()
            try:
                if _EV:
                    _EV.log(_EV.E.HOTKEY_START_FAIL, err=type(_hk_err).__name__)
            except Exception:
                pass

            def _prevenir_raccourci_ko():
                try:
                    overlay.info(_te("Raccourci indisponible"),
                                 _te("Utilisez le bouton Dicter. Envoyez un diagnostic depuis les Réglages : "
                                     "le problème nous sera signalé."))
                    _schedule_overlay_hide(9.0)
                except Exception:
                    pass
            threading.Timer(4.0, _prevenir_raccourci_ko).start()

        # v19 — Overlay de dictée flottant (NSPanel non-activant) : créé sur le
        # main thread, gardé caché jusqu'à la 1re dictée au raccourci.
        try:
            overlay.create()
            overlay.set_theme(_load_settings().get("theme", "dark"))   # v1.3.4 — Apparence
        except Exception as e:
            print(f"[overlay] indisponible ({e}) — dictée sans overlay.")

        # v19 — RÉ-AFFICHAGE : quand on relance Vlocal alors qu'il tourne déjà
        # (fenêtre cachée), la 2e instance dépose _SHOW_REQUEST. On le surveille
        # ici et on ré-affiche la fenêtre -> « relancer l'app » fonctionne.
        try:
            if os.path.exists(_SHOW_REQUEST):
                os.remove(_SHOW_REQUEST)   # purge un résidu de démarrage
        except Exception:
            pass

        def _watch_show_requests():
            import time as _t
            while True:
                try:
                    if os.path.exists(_SHOW_REQUEST):
                        os.remove(_SHOW_REQUEST)

                        def _show():
                            try:
                                if appstate.window is not None:
                                    appstate.window.show()
                                appstate.window_visible = True
                                from AppKit import NSApplication
                                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                            except Exception:
                                pass
                        try:
                            from Foundation import NSOperationQueue
                            NSOperationQueue.mainQueue().addOperationWithBlock_(_show)
                        except Exception:
                            _show()
                except Exception:
                    pass
                _t.sleep(0.6)
        threading.Thread(target=_watch_show_requests, daemon=True).start()

        # v3.3 — APP ACCESSORY (menu-bar) : contrairement à une app Regular, le
        # dashboard ne passe pas DEVANT tout seul au lancement. On force UNE
        # activation ~0.8 s après le démarrage (idempotent, sans effet si déjà au
        # premier plan) pour qu'il s'ouvre bien au premier plan.
        def _activate_on_launch():
            import time as _t
            _t.sleep(0.8)
            def _go():
                try:
                    if appstate.window is not None and not appstate.quitting:
                        appstate.window.show()
                        appstate.window_visible = True
                        from AppKit import NSApplication
                        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                        _set_dock_visible(True)   # B1 — ré-impose Accessory APRÈS l'affichage (pywebview a pu remettre Regular)
                except Exception:
                    pass
            try:
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(_go)
            except Exception:
                pass
        threading.Thread(target=_activate_on_launch, daemon=True).start()

        # v19 — RÉ-OUVERTURE PAR LE DOCK : cliquer l'icône du Dock (ou Cmd+Tab)
        # réactive l'app mais ne ré-affiche pas une fenêtre cachée. On détecte la
        # bascule inactif->actif alors que la fenêtre est masquée et on la rouvre.
        _reopen = {"prev_active": True}

        def _reopen_check():   # tourne sur le MAIN thread (lecture AppKit sûre)
            try:
                from AppKit import NSApplication
                app = NSApplication.sharedApplication()
                active = bool(app.isActive())
                if active and not _reopen["prev_active"] and not appstate.window_visible:
                    if appstate.window is not None:
                        appstate.window.show()
                    appstate.window_visible = True
                    app.activateIgnoringOtherApps_(True)
                _reopen["prev_active"] = active
            except Exception:
                pass

        def _reopen_pump():
            import time as _t
            from Foundation import NSOperationQueue
            while True:
                _t.sleep(0.4)
                try:
                    NSOperationQueue.mainQueue().addOperationWithBlock_(_reopen_check)
                except Exception:
                    pass
        threading.Thread(target=_reopen_pump, daemon=True).start()

        # v19 — Permission « Accessibilité » : sans elle, le moniteur NSEvent
        # global (hotkey_mac) ne capte rien hors focus et le bip macOS persiste
        # (le système traite la combo lui-même). On déclenche le prompt natif
        # (si indéterminé) + un toast clair.
        def _check_hotkey_perm():
            try:
                # v1.0.3 — Ne JAMAIS déclencher la permission Accessibilité (dialogue
                # natif + ouverture des Réglages Système + overlay « relancez »)
                # tant que l'écran d'activation est affiché : on repousse jusqu'à
                # ce que l'utilisateur ait activé, pour un premier contact propre.
                if not _onboarding_done() or model_store.needs_download():
                    threading.Timer(3.0, _check_hotkey_perm).start()
                    return
                # v1.0.3 — Demande proactive de la permission Micro (dialogue natif
                # AVFoundation, SANS ouvrir de flux audio) pour que la toute première
                # dictée ne meure pas en silence. No-op si déjà accordée/refusée.
                try:
                    if permissions.mic_status() == 0:   # 0 = non déterminé
                        permissions.mic_prompt()
                except Exception:
                    pass
                # v3.1 — La permission Accessibilité est désormais gérée par
                # l'onboarding intégré (étape Autorisations) ET par l'auto-
                # réparation du hotkey (watch AXIsProcessTrusted). On ne montre
                # PLUS de carte overlay « va dans les Réglages » au démarrage
                # (fini l'élément parasite qui se balade).
                # v1.3.2 — mais on le COMPTE : un raccourci muet hors de Vlocal doit
                # se voir dans la tour de contrôle, pas seulement chez l'utilisateur.
                try:
                    if _EV and not permissions.accessibility_ok():
                        _EV.log(_EV.E.HOTKEY_UNTRUSTED, level="warn")
                except Exception:
                    pass
            except Exception:
                pass
        threading.Timer(2.5, _check_hotkey_perm).start()

        # v10 — Menu bar (NSStatusItem) doit être créé sur le MAIN THREAD Cocoa.
        # Or _after_start tourne dans un worker pywebview → on dispatch via
        # NSOperationQueue.mainQueue() pour respecter la contrainte macOS
        # (sinon : NSInternalInconsistencyException "NSWindow should only be
        # instantiated on the main thread").
        def _perform_quit():
            # v29.8 — Nettoyage de quit IDEMPOTENT, SANS terminate_ : appelable
            # depuis applicationShouldTerminate_ (Cmd+Q/Dock/menu) ET depuis _on_quit
            # (icône V) sans réentrance.
            if appstate.quit_in_progress:
                return
            appstate.quit_in_progress = True
            appstate.quitting = True   # autorise la vraie fermeture (vs masquage)
            # v29.1 — DEADMAN : garantit la mort du process en <=4 s, même si la
            # suite (sauvegarde réunion) se BLOQUE sur un worker GPU figé.
            # os._exit() ne peut être bloqué par AUCUN thread (le resource_tracker
            # enfant meurt avec le parent) ; minuteur daemon -> n'empêche jamais une
            # sortie propre plus rapide.
            import threading as _th
            _dead = _th.Timer(4.0, lambda: os._exit(0))
            _dead.daemon = True
            _dead.start()
            # v20 (D9) — RÉUNION EN COURS : finaliser le WAV (stop() flushe et
            # clôt l'en-tête) et marquer la réunion récupérable, plutôt que de
            # laisser un fichier tronqué et un statut « recording » fantôme.
            try:
                rec = _meeting_state.get("recorder")   # v1.0.1 — était "rec" (clé inexistante -> finalisation morte, WAV tronqué si quit en réunion)
                if rec is not None and _meeting_state.get("recording"):
                    try:
                        live = _meeting_state.get("live")
                        if live is not None:
                            live._stop.set()        # stoppe la boucle, pas de finalize
                    except Exception:
                        pass
                    rec.stop()                      # flush + clôture du WAV
                    mid = _meeting_state.get("id")
                    if appstate.store and mid:
                        appstate.store.update_meeting(mid, status="error")
                    print("[quit]    réunion en cours sauvegardée (WAV clos, "
                          "statut error récupérable).")
            except Exception as e:
                print(f"[quit]    finalisation réunion KO : {e}")
            # Fermeture PROPRE de la base avant de quitter (WAL tronqué).
            # Store.close() est idempotent : le 2e appel (fin de boucle
            # Cocoa, après webview.start) est sans effet.
            try:
                if appstate.store:
                    appstate.store.close()
            except Exception:
                pass

        # Le nettoyage complet est enregistré TOUT DE SUITE : le délégué de cycle de
        # vie l'utilisera même si la suite du démarrage échoue.
        _register_quit_hook(_perform_quit)

        def _on_quit():
            # Icône V -> « Quitter Vlocal » : nettoyage idempotent puis terminate_.
            _perform_quit()
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().terminate_(None)
            except Exception:
                os._exit(0)

        def _install_terminate_handler():
            # v29.8 — CORRECTIF QUIT (bug terrain « ça ferme juste, ça ne quitte
            # pas »). Cmd+Q / clic-droit Dock / menu Pomme passent par
            # applicationShouldTerminate_ du délégué d'app. Celui de pywebview
            # consulte notre handler de fermeture de fenêtre — qui renvoie False
            # pour MASQUER (garder le raccourci vivant) — et ANNULE donc le
            # terminate. On installe NOTRE délégué : terminate -> vrai quit. La
            # fermeture par la croix reste un masquage (windowShouldClose_, chemin
            # Cocoa distinct, inchangé).
            global _app_delegate
            if _app_delegate is not None:
                return
            _register_quit_hook(_perform_quit)
            # v1.3.2 — cas nominal : le délégué de cycle de vie est déjà actif
            # (installé dans pywebview avant la fenêtre). On ne le remplace pas.
            try:
                from AppKit import NSApplication as _NSA
                _d = _NSA.sharedApplication().delegate()
                if lifecycle._LifecycleDelegate is not None and isinstance(_d, lifecycle._LifecycleDelegate):
                    _app_delegate = _d
                    print("[quit]    Cmd+Q / Dock / menu Pomme -> vrai quit "
                          "(délégué de cycle de vie actif dès la fenêtre).")
                    return
            except Exception:
                pass
            try:
                from AppKit import NSApplication
                from Foundation import NSObject

                class _VlocalAppDelegate(NSObject):
                    def applicationShouldTerminate_(self, sender):
                        try:
                            _perform_quit()
                        except Exception:
                            os._exit(0)
                        return 1   # NSTerminateNow

                    def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, flag):
                        # v3.2 — ICÔNE DOCK UNIQUE : la fenêtre se ferme en se MASQUANT
                        # (agent maintenu en vie pour le raccourci). Sans barre de menus,
                        # le clic sur l'icône Dock DOIT ré-afficher le dashboard (sinon
                        # « je clique sur l'icône et rien ne s'ouvre »). flag=False quand
                        # aucune fenêtre n'est visible (cas du masquage).
                        try:
                            if appstate.window is not None:
                                appstate.window.show()
                            appstate.window_visible = True
                        except Exception:
                            pass
                        try:
                            from AppKit import NSApplication
                            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                        except Exception:
                            pass
                        return True

                    def applicationSupportsSecureRestorableState_(self, sender):
                        return True

                _app_delegate = _VlocalAppDelegate.alloc().init()
                NSApplication.sharedApplication().setDelegate_(_app_delegate)
                print("[quit]    Cmd+Q / Dock / menu Pomme -> vrai quit "
                      "(délégué installé).")
            except Exception as e:
                print(f"[quit]    délégué terminate indispo ({e}) — quit via icône V.")
        def _on_open():
            try:
                if appstate.window is not None:
                    appstate.window.show()
                appstate.window_visible = True
            except Exception:
                pass
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:
                pass
        def _on_dictate_toggle():
            if appstate.controller and appstate.controller.recording_active():
                threading.Thread(target=appstate.controller.stop_and_finalize,
                                 daemon=True).start()
            elif appstate.controller:
                appstate.controller.start()
        def _on_toggle_task(tid):
            if appstate.store:
                appstate.store.toggle_task(int(tid))
            _refresh_lists_ui(); _refresh_menubar()

        def _create_menubar_on_main():
            # v3.3 — ICÔNE MENU-BAR V DE RETOUR (app de type menu-bar : 1 SEULE icône,
            # la V dans la barre des menus, AUCUNE icône Dock -> pattern Wispr Flow /
            # SuperWhisper). v3.2 avait tout retiré pour tuer le « deux icônes » ; on le
            # résout autrement : Accessory (pas de Dock) + UN seul NSStatusItem.
            #  - Fermer la fenêtre = la MASQUER (agent + raccourci maintenus en vie).
            #  - Menu V : « Dicter » / « Ouvrir Vlocal » / « Réglages » / « Quitter ».
            #  - Quitter = menu V ou Cmd+Q (applicationShouldTerminate_ -> vrai quit).
            #  - La bulle de dictée s'ANCRE sous l'icône V (le bec pointe enfin dessus).
            _install_terminate_handler()
            if appstate.menubar is not None:
                return
            try:
                import menubar as _mb

                def _open_settings():
                    _on_open()
                    _ui("if(typeof activateView==='function')activateView('reglages')")

                def _open_update():
                    _on_open()
                    _ui("if(typeof activateView==='function')activateView('maj')")

                appstate.menubar = _mb.MenuBar({
                    "dictate": _on_dictate_toggle,
                    "open": _on_open,
                    "settings": _open_settings,
                    "quit": _on_quit,
                    "update": _open_update,
                    "toggle_task": _on_toggle_task,
                }, labels=_menubar_labels())
                _refresh_menubar()                       # peuple « Rappels récents »
                try:
                    overlay.set_anchor_provider(appstate.menubar.icon_frame)
                    print("[menubar] icône V installée + overlay ancré sous l'icône.")
                except Exception as _ae:
                    print(f"[menubar] ancre overlay indispo ({_ae}).")
            except Exception as e:
                print(f"[menubar] icône indisponible ({e}) — repli sans icône menu-bar.")

        # macOS uniquement (menu bar = NSStatusItem). Sur Windows, on s'en passe.
        if sys.platform == "darwin":
            try:
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(_create_menubar_on_main)
            except Exception as e:
                print(f"[menubar] dispatch impossible ({e})")

        # v1.0.6 — DÉTECTEUR DE FIGEAGE : arme le battement de cœur sur la run
        # loop du MAIN thread (dispatch obligatoire : le NSTimer doit y vivre).
        # Le daemon de surveillance tuera l'app si l'UI gèle > MAIN_FREEZE_KILL_S
        # -> plus d'app fantôme inkillable sans icône Dock.
        if sys.platform == "darwin":
            try:
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(_arm_freeze_watchdog)
            except Exception as e:
                print(f"[watchdog] dispatch impossible ({e})")

        # v10c — Force la NSWindow vraiment transparente (pywebview ne le fait
        # qu'à moitié → rectangle gris résiduel visible). Doit tourner sur le
        # main thread Cocoa.
        def _force_clear_window():
            # v3.2.9 — DASHBOARD : fenêtre transparente (le contenu CSS .win, ~95%
            # opaque, fait tout le rendu) MAIS avec l'OMBRE macOS RÉTABLIE. Avant :
            # setHasShadow(False) (hérité de l'ère "carte 374px") -> sur le dashboard
            # plein cadre, bord plat "coupé" en haut, disgracieux. La fenêtre étant
            # titrée (coins arrondis natifs), l'ombre épouse le rectangle arrondi =
            # bord premium propre, et le haut s'intègre enfin sans coupure.
            try:
                from AppKit import NSApplication, NSColor
                app = NSApplication.sharedApplication()
                target = None
                for win in app.windows():
                    # Fenêtre titrée et large = le dashboard (l'overlay est un NSPanel borderless).
                    if (win.styleMask() & 1) and win.frame().size.width > 200:
                        target = win
                        break
                if target is None:
                    for win in app.windows():
                        if win.level() == 0 and win.frame().size.width > 100:
                            target = win
                            break
                if target is not None:
                    target.setOpaque_(False)
                    target.setBackgroundColor_(NSColor.clearColor())
                    target.setHasShadow_(True)        # v3.2.9 — ombre premium rétablie
                    target.invalidateShadow()
                    print(f"[window]  ombre premium rétablie ({target.frame().size.width:.0f}x{target.frame().size.height:.0f})")
                else:
                    print("[window]  fenêtre dashboard introuvable")
            except Exception as e:
                print(f"[window]  réglage fenêtre impossible : {e}")

        # macOS uniquement (forçage transparence NSWindow).
        if sys.platform == "darwin":
            try:
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(_force_clear_window)
            except Exception:
                pass

        # v15 — Plus aucun SLM. Vlocal = Whisper large-v3-turbo, point.
        # v15 — Message de bienvenue / note Ollama (différé, non bloquant).
        threading.Timer(1.2, _post_startup_model_checks).start()
        # v15 — Re-planifie les rappels à échéance future (les Timer ne
        # survivent pas à un redémarrage).
        threading.Timer(0.8, _rearm_pending_reminders).start()
        # v15 — Pré-construit le bundle de notification de marque "Vlocal"
        # (icône V premium) en arrière-plan : 1ʳᵉ notif instantanée + la
        # permission macOS est demandée tôt sous le nom Vlocal.
        try:
            notifier.prebuild_async()
        except Exception as _nt_err:
            print(f"[notify]  pré-construction KO ({_nt_err})")
            try:
                if _EV:
                    _EV.log(_EV.E.STARTUP_STAGE_FAIL, stage="notifier", err=type(_nt_err).__name__)
            except Exception:
                pass

        # Premier render des listes (après que la fenêtre soit prête).
        threading.Timer(0.5, _refresh_lists_ui).start()

        # v1.3.4 — Veille de mise à jour : au démarrage puis toutes les six heures,
        # un simple GET (aucune donnée personnelle). Quand une version plus récente
        # existe, le menu V gagne une entrée « Mise à jour disponible » en tête.
        # La fenêtre, elle, pose sa propre pastille (checkUpdateBadge).
        def _update_watch():
            import time as _t
            _t.sleep(30.0)
            while True:
                try:
                    info = appstate.api.check_updates() if appstate.api is not None else None
                    latest = (info or {}).get("latest") if (info or {}).get("update") else None
                    if latest != appstate.update_latest:
                        appstate.update_latest = latest
                        _refresh_menubar()
                except Exception as _uw_err:
                    print(f"[update]  veille KO (ignorée) : {_uw_err}")
                _t.sleep(6 * 3600)
        threading.Thread(target=_update_watch, name="update-watch", daemon=True).start()

    webview.start(_after_start)
    # Sortie par fin de boucle Cocoa (fermeture sans passer par « Quitter ») :
    # fermeture PROPRE de la base (WAL tronqué). Store.close() est idempotent,
    # le double appel avec _on_quit est sûr.
    try:
        if appstate.store:
            appstate.store.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
