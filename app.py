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

Le modèle Whisper est chargé PARESSEUSEMENT au 1er usage puis DÉCHARGÉ après
inactivité (idle-unload, délai _IDLE_UNLOAD_S) pour tenir sur 8 Go ; rechargé
tout seul au prochain usage (~0,7 s). Voir _start_idle_unloader.

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

import functools
import json
import os
import re
import subprocess
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

import processor
import notifier
import overlay
import permissions
import reminders
import obsidian         # v1.0.13 — connecteur Obsidian local (capture des dictées dans un coffre, ADDITIF)
import telemetry        # v1.1.0 — télémétrie minimale déclarée (installs, usage par jour)
from supabase_config import edge_url, auth_headers  # v1.1.0 — backend centralisé
import updater          # v1.0.5 — mise à jour in-app (téléchargement + swap atomique)
from engine import (SAMPLE_RATE, SMALL_DIR_NAME, TURBO_DIR_NAME, VlocalEngine,
                    assemble_segments)
from storage import Store

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

def _resolve_model_dirs():
    """(Re)calcule MODEL_DIR/SMALL_MODEL_DIR depuis model_store. Appelé à l'import,
    en main(), et après un téléchargement (les modèles passent du néant au cache)."""
    global MODEL_DIR, SMALL_MODEL_DIR
    _root = model_store.models_dir()
    MODEL_DIR = os.path.join(_root, TURBO_DIR_NAME)
    SMALL_MODEL_DIR = os.path.join(_root, SMALL_DIR_NAME)

def _point_mlx_to_cache():
    """Pointe le moteur GPU (mlx) sur le modèle q8 du dossier résolu (cache ou
    bundle) au lieu de sa résolution bundle interne. Sans effet si le q8 est absent
    (mlx gardera son repli). N'importe mlx_engine que si le modèle existe."""
    try:
        _q8 = os.path.join(model_store.models_dir(), "whisper-large-v3-turbo-mlx-q8")
        if os.path.exists(os.path.join(_q8, "config.json")):
            import mlx_engine as _mlx_set
            _mlx_set._model_ref = _q8
    except Exception:
        pass

MODEL_DIR = None
SMALL_MODEL_DIR = None
_resolve_model_dirs()

_window = None
_quitting = False        # True quand l'utilisateur quitte vraiment (menu « Quitter »)
_quit_in_progress = False  # v29.8 — garde-fou réentrance du quit (idempotent)
_update_busy = False       # v1.0.5 — une préparation de MAJ est en cours (anti-double)
_update_staged = None      # v1.0.5 — chemin de la réplique vérifiée prête à installer
_app_delegate = None       # v29.8 — notre délégué d'app NSApp (terminate -> vrai quit)
_DRAG_VIEW_CLS = None       # v3.2.4 — classe NSView "bande de déplacement" (enregistrée 1 fois)
_window_visible = True   # suivi de visibilité (fenêtre naît affichée)
# Géométrie de la fenêtre principale — point de vérité unique. NB : la CARTE
# CSS (.panel) fait 374 px, soit 6 px de moins que la fenêtre native.
WIN_W = 1180            # v3.1 — dashboard plein écran (était 380, carte flottante)
WIN_H = 780             # v3.1 — (était 720)
PANEL_W = 840            # (hérité ; set_meeting_panel neutralisé en v3.1)
APP_VERSION = "1.1.0"   # 6 septembre 2026. Synchrone avec le fichier VERSION (build) ;
                        # affiché dans l'onglet « Mises à jour ». Historique : CHANGELOG.md.
# Libellé humain du raccourci global actif (posé par start_global_hotkey,
# consommé par le message de permission _check_hotkey_perm).
_hotkey_label = "Ctrl + Espace"


def _ui(code: str):
    """Exécute du JS dans la fenêtre, NON BLOQUANT (fire-and-forget).

    Bug #1 (hang « ne répond pas ») : NE JAMAIS passer par
    _window.evaluate_js — sur le backend Cocoa il est SYNCHRONE et bloque le
    thread appelant sur un sémaphore SANS timeout jusqu'à ce que le completion
    handler WKWebView se déclenche. Quand le dashboard est masqué (orderOut,
    cas de la dictée au raccourci), la WKWebView ne sert plus ce handler -> le
    worker (boucle audio 10 Hz / finalisation) gèle À VIE, le verrou _busy
    reste tenu et l'app finit par ne plus répondre. On dispatche donc le JS
    directement sur la WKWebView via le main thread, sans attendre (exactement
    comme overlay.py). Aucun appelant de _ui n'utilise sa valeur de retour.
    """
    if _window is None:
        return
    try:
        from webview.platforms.cocoa import BrowserView
        from Foundation import NSOperationQueue
        inst = BrowserView.instances.get(getattr(_window, "uid", "master"))
        web = getattr(inst, "webview", None) if inst is not None else None
        if web is not None:
            NSOperationQueue.mainQueue().addOperationWithBlock_(
                lambda: web.evaluateJavaScript_completionHandler_(code, None))
            return
    except Exception:
        pass
    try:
        # v1.0.22 — repli ultime (accès natif indisponible) rendu NON BLOQUANT :
        # evaluate_js reste synchrone sur un sémaphore SANS timeout (cf. Bug #1
        # ci-dessus) ; tant qu'il était appelé dans le thread courant, ce repli
        # pouvait geler la boucle audio, la finalisation, ou pire le verrou de
        # dictée pendant un _toast. Aucun appelant n'utilise la valeur de retour.
        threading.Thread(target=lambda: _window.evaluate_js(code),
                         name="ui-fallback", daemon=True).start()
    except Exception:
        pass


def _quit_app():
    """v1.0.5 — Termine Vlocal PROPREMENT. `terminate_` DOIT être invoqué sur le
    main thread Cocoa, sinon c'est un no-op silencieux (bug historique : on
    passait une fonction Python à _ui() qui attend du JS -> le quit ne partait
    jamais, l'ancien process survivait au « Redémarrer » et l'insertion restait
    figée). On dispatche donc explicitement sur la mainQueue."""
    def _do():
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication().terminate_(None)
        except Exception:
            os._exit(0)
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(_do)
    except Exception:
        _do()


def _api_safe(default=None):
    """v16.1 — Décorateur : une méthode d'API ne doit JAMAIS lever vers le pont
    JS (sinon la promesse est rejetée et l'UI peut rester bloquée). On capture
    tout, on log côté technique, on renvoie une valeur sûre."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                _r = fn(*a, **k)
                # v3.3 — i18n : tout payload {"error": "<FR>"} renvoyé au JS est
                # traduit à la volée selon la langue UI (sinon affiché en FR en mode EN).
                if isinstance(_r, dict) and isinstance(_r.get("error"), str):
                    _r["error"] = _te(_r["error"])
                return _r
            except Exception as e:
                print(f"[api] {fn.__name__} a échoué : {e}")
                # v3.2.9 — tour de contrôle : tout échec d'API journalisé (code
                # stable + méthode + type d'exception, SANS donnée sensible).
                if _EV: _EV.log(_EV.E.API_GUARD, fn=fn.__name__, err=type(e).__name__)
                return default() if callable(default) else default
        return wrapper
    return deco


def _toast(message: str, kind: str = "info"):
    """v15.1 — Affiche un toast clair à l'utilisateur. Helper centralisé :
    une seule façon de parler à l'utilisateur, vouvoiement cohérent.
    kind ∈ {info, success, error} (l'UI peut styliser ; défaut neutre)."""
    try:
        _ui("if(typeof showToast==='function') showToast(%s, %s);"
            % (json.dumps(_te(message or "")), json.dumps(kind)))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v3.3 — i18n BACKEND (fr/en). Messages utilisateur côté Python (toasts, overlay,
# erreurs renvoyées au JS). Carte de traduction INVERSE : on garde le FR au point
# d'appel (source unique, lisible), _te() traduit à la volée si la langue UI est EN.
# La langue est fixée par l'API set_lang (poussée par le dashboard au démarrage et
# au changement). Idempotent : une chaîne EN n'est pas une clé FR -> renvoyée telle
# quelle. Les chaînes interpolées (variables) restent gérées au point d'appel.
# ---------------------------------------------------------------------------
_UI_LANG = "fr"
# v1.0.11 — comptes "dev" : le DÉTAIL technique du diagnostic (état moteur, nombre
# d'incidents) n'est affiché QUE pour ces licences. Un utilisateur lambda ne voit que
# le formulaire d'envoi (l'envoi transmet quand même les métadonnées sanitisées au
# support). Override possible par VLOCAL_DEV=1 (build dev local).
_BT = {
  "Microphone indisponible. Vérifiez Réglages Système > Confidentialité et sécurité > Microphone (et qu'aucune autre app ne le capte).":
    "Microphone unavailable. Check System Settings > Privacy & Security > Microphone (and that no other app is using it).",
  "La transcription a calé un instant (GPU), c'est réinitialisé. Relancez votre dictée, ça repart.":
    "Transcription stalled briefly (GPU) and has been reset. Start your dictation again and it picks right back up.",
  "Aucun son n'a été capté. Vérifiez que le microphone est autorisé : menu Pomme > Réglages Système > Confidentialité et sécurité > Microphone, puis autorisez Vlocal et relancez.":
    "No sound was captured. Make sure the microphone is allowed: Apple menu > System Settings > Privacy & Security > Microphone, then allow Vlocal and try again.",
  "Le son était très faible. Rapprochez-vous du micro et réessayez.":
    "The sound was very faint. Move closer to the microphone and try again.",
  "Le texte n'a pas pu être copié dans le presse-papier. Réessayez votre dictée.":
    "The text could not be copied to the clipboard. Please try your dictation again.",
  "La transcription de la réunion a échoué. L'audio est conservé, vous pouvez réessayer.":
    "The meeting transcription failed. The audio has been kept, you can try again.",
  "Le fichier de la réunion est introuvable. L'enregistrement n'a pas pu être retrouvé sur le disque.":
    "The meeting file could not be found. The recording could not be located on disk.",
  "La transcription de la réunion n'a pas pu aboutir. L'enregistrement audio est conservé, réessayez.":
    "The meeting transcription could not be completed. The audio recording has been kept, please try again.",
  "Aucune parole n'a été détectée dans cet enregistrement. Vérifiez que le micro captait bien le son.":
    "No speech was detected in this recording. Make sure the microphone was picking up sound.",
  "Réunion prête. Cliquez dessus dans la liste pour la lire ou la copier.":
    "Meeting ready. Click it in the list to read or copy it.",
  "Le moteur de transcription finit de se préparer, un instant…":
    "The transcription engine is finishing getting ready, one moment…",
  "Micro refusé. Autorisez Vlocal dans Réglages système > Confidentialité > Microphone.":
    "Microphone denied. Allow Vlocal in System Settings > Privacy > Microphone.",
  "Le mode Réunion n'est pas activé. Activez-le dans Réglages.":
    "Meeting mode is not enabled. Turn it on in Settings.",
  "La réunion précédente est encore en cours de traitement. Patientez qu'elle apparaisse comme prête dans la liste.":
    "The previous meeting is still being processed. Please wait until it shows as ready in the list.",
  "Un incident d'écriture a interrompu l'enregistrement ; la partie déjà captée va être transcrite.":
    "A write error interrupted the recording; the portion already captured will be transcribed.",
  "Enregistrement trop court, rien à transcrire.":
    "Recording too short, nothing to transcribe.",
  "Le traitement de la réunion n'a pas pu démarrer. Réessayez.":
    "The meeting could not start processing. Please try again.",
  "L'audio de cette réunion n'est plus disponible : impossible de relancer l'identification des locuteurs.":
    "This meeting's audio is no longer available, so speaker identification cannot be run again.",
  "La dictée a calé, réessayez.":
    "The dictation stalled. Please try again.",
  "La transcription a calé, réessayez.":
    "The transcription stalled. Please try again.",
  "Vlocal démarre": "Vlocal is starting",
  "Le moteur se charge, réessayez dans un instant.": "The engine is loading, try again in a moment.",
  "Un instant": "One moment",
  "Transcription précédente en cours.": "Previous transcription in progress.",
  "Réessayez.": "Please try again.",
  "Aucun son capté. Réessayez.": "No sound captured. Please try again.",
  "Échec d'insertion, réessayez.": "Insertion failed. Please try again.",
  "Insertion à autoriser": "Insertion needs permission",
  "Texte copié : Cmd + V. Autorisez Vlocal dans Réglages > Confidentialité et sécurité > Accessibilité, puis relancez Vlocal.":
    "Text copied: Cmd + V. Allow Vlocal in System Settings > Privacy & Security > Accessibility, then relaunch Vlocal.",
  "Texte copié": "Text copied",
  "Collez avec Cmd + V.": "Paste with Cmd + V.",
  "Texte copié dans le presse-papier, collez avec Cmd + V. Pour l'insertion automatique au curseur, autorisez Vlocal dans Réglages Système > Confidentialité et sécurité > Accessibilité.":
    "Text copied to the clipboard, paste with Cmd + V. For automatic insertion at the cursor, allow Vlocal in System Settings > Privacy & Security > Accessibility.",
  "Transcription du fichier en cours...": "Transcribing the file…",
  "Aucun son détectable dans le fichier.": "No detectable sound in the file.",
  "Espace disque insuffisant : l'enregistrement a été tronqué. La partie déjà captée va être transcrite.":
    "Not enough disk space: the recording was cut short. The portion already captured will be transcribed.",
  "Le moteur de transcription n'a pas pu démarrer. Réessayez de lancer Vlocal.":
    "The transcription engine could not start. Please try launching Vlocal again.",
  "Rappel": "Reminder",
  "Téléchargement des modèles échoué. Vérifiez votre connexion et réessayez.":
    "Model download failed. Check your connection and try again.",
  "Modèles téléchargés mais moteur introuvable. Réessayez.":
    "Models downloaded but the engine could not be found. Please try again.",
  "Téléchargement déjà en cours.": "Download already in progress.",
  "Espace disque insuffisant : libérez environ 3 Go puis réessayez.":
    "Not enough disk space: free up about 3 GB and try again.",
  "Réunion prête": "Meeting ready",
  # --- payloads {"error": ...} surfacés par le JS ---
  "Fichier vide.": "Empty file.",
  "Aucun fichier.": "No file.",
  "Le moteur de transcription se prépare encore, réessayez dans un instant.":
    "The transcription engine is still getting ready, try again in a moment.",
  "Import indisponible.": "Import unavailable.",
  "Le moteur de transcription se prépare encore.": "The transcription engine is still getting ready.",
  "Stockage indisponible.": "Storage unavailable.",
  "Une réunion est déjà en cours de traitement.": "A meeting is already being processed.",
  "Aucune piste audio exploitable dans le fichier.": "No usable audio track in the file.",
  "Audio trop court.": "Audio too short.",
  "Erreur interne (réunion).": "Internal error (meeting).",
  "Moteur en préparation.": "Engine getting ready.",
  "Mode Réunion désactivé.": "Meeting mode disabled.",
  "Enregistrement déjà en cours.": "Recording already in progress.",
  "Pipeline réunion précédente encore en cours.": "Previous meeting pipeline still running.",
  "Aucun enregistrement actif.": "No active recording.",
  "Enregistrement trop court.": "Recording too short.",
  "Le traitement n'a pas pu démarrer.": "Processing could not start.",
  "Le moteur se prépare encore, un instant.": "The engine is still getting ready, one moment.",
  "Réunion introuvable.": "Meeting not found.",
  "L'audio de cette réunion n'est plus disponible, réessai impossible.":
    "This meeting's audio is no longer available, retry is not possible.",
  "Un traitement est déjà en cours, réessayez dans un instant.":
    "A task is already running, try again in a moment.",
  "Export SRT impossible : cette réunion n'a pas de blocs locuteurs (diarisation absente).":
    "SRT export not possible: this meeting has no speaker blocks (no diarization).",
  # --- v1.0.13 Connexion Obsidian ---
  "Aucun coffre Obsidian détecté. Choisissez-le manuellement.":
    "No Obsidian vault detected. Choose it manually.",
  "Ce dossier n'est pas un coffre Obsidian.": "This folder is not an Obsidian vault.",
  "Connexion à Obsidian impossible.": "Could not connect to Obsidian.",
  "Module Obsidian indisponible.": "Obsidian module unavailable.",
}

# v3.3 (S3) — préfixes des messages d'erreur INTERPOLÉS (f"<préfixe> : {détail}") :
# le préfixe FR est traduit, le détail dynamique ({e}, {path}, {ext}…) est conservé.
_BT_PREFIX = {
    "Erreur de transcription : ": "Transcription error: ",
    "Format non supporté : ": "Unsupported format: ",
    "Décodage échoué : ": "Decoding failed: ",
    "Écriture tmp échouée : ": "Temporary write failed: ",
    "Fichier introuvable : ": "File not found: ",
    "Import en réunion échoué : ": "Import as meeting failed: ",
    "Réessai impossible : ": "Retry failed: ",
    "Dialogue d'enregistrement indisponible : ": "Save dialog unavailable: ",
    "Écriture impossible : ": "Could not write the file: ",
    "Impossible de démarrer l'enregistrement : ": "Couldn't start the recording: ",
}

def _te(s):
    """Traduit un message backend FR -> EN si la langue UI est EN ; sinon inchangé.
    Correspondance EXACTE d'abord (_BT), puis par PRÉFIXE (_BT_PREFIX) pour les
    messages interpolés. Idempotent (une chaîne EN n'est pas une clé FR)."""
    if _UI_LANG == "en" and isinstance(s, str):
        v = _BT.get(s)
        if v is not None:
            return v
        for _fr, _en in _BT_PREFIX.items():
            if s.startswith(_fr):
                return _en + s[len(_fr):]
    return s


def copy_to_clipboard(text: str) -> bool:
    """Copie le texte dans le presse-papier (macOS).
    La copie auto EST le produit (coller avec Cmd+V)."""
    if not text:
        return False
    try:
        import clipboard
        return clipboard.set_clipboard(text)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# v1.0.18 — Garde-fous micro / insertion : messages CLAIRS (jamais de spam),
# 100 % best-effort, in-process (aucun subprocess), JAMAIS d'exception propagée.
# Holders mutables (pas de `global` dans de grosses méthodes) :
# --------------------------------------------------------------------------- #
_acc_warned = [False]     # Accessibilité OFF : signalée UNE SEULE FOIS / session
_mic_fail_count = [0]     # échecs micro consécutifs -> détection coreaudiod figé
# v1.0.22 — chaînage : sérialise les INSERTIONS au curseur (deux dictées
# enchaînées ne doivent jamais entrelacer leurs collages ; l'ordre FIFO est déjà
# garanti par les tickets de finalisation, ce verrou empêche le chevauchement).
_insert_serial_lock = threading.Lock()


def _time_left(deadline: float) -> float:
    """v1.0.22 — secondes restantes avant `deadline` (horloge monotone).
    Socle du budget « jamais bloqué > 2 s » : chaque étape de l'escalade micro
    n'est tentée que si le budget de l'appui le permet encore."""
    return deadline - time.monotonic()

# Enregistreurs / visio connus qui tiennent souvent le micro -> message ciblé.
_MIC_GRABBERS = (
    ("screen studio", "Screen Studio"), ("obs", "OBS"), ("zoom", "Zoom"),
    ("microsoft teams", "Teams"), ("quicktime", "QuickTime Player"),
    ("loom", "Loom"), ("cleanshot", "CleanShot"), ("screenflow", "ScreenFlow"),
    ("camtasia", "Camtasia"), ("webex", "Webex"), ("discord", "Discord"),
    ("facetime", "FaceTime"),
)


def _mic_grabber_name():
    """Nom d'un enregistreur/visio connu actuellement lancé (ou None). Best-effort,
    in-process (NSWorkspace.runningApplications, pas de `ps`), jamais d'exception.
    Appelé seulement sur le chemin d'ÉCHEC micro (rare) -> coût négligeable."""
    try:
        from AppKit import NSWorkspace
        for app in (NSWorkspace.sharedWorkspace().runningApplications() or []):
            try:
                low = (app.localizedName() or "").lower()
            except Exception:
                continue
            for needle, label in _MIC_GRABBERS:
                if needle in low:
                    return label
    except Exception:
        pass
    return None


def _reveal_app_in_finder():
    """Révèle le bundle Vlocal.app dans le Finder (pour le glisser sur le « + » de
    la liste Accessibilité quand il n'y apparaît pas). Best-effort, jamais d'exception."""
    try:
        from Foundation import NSBundle
        path = NSBundle.mainBundle().bundlePath() or "/Applications/Vlocal.app"
    except Exception:
        path = "/Applications/Vlocal.app"
    try:
        from AppKit import NSWorkspace
        NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(path, "")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Contrôleur de dictée — partagé par le bouton/espace ET le raccourci global.
# Gère : capture, niveau audio (wave UI), transcription finale, copie presse-papier.
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# v21 — Anti-App Nap : pendant la dictée puis la transcription, la fenêtre est
# cachée et le micro FERMÉ (après la relâche) -> macOS a le droit de throttler
# le process (timers coalescés, démotion) = latence ALÉATOIRE (2-10 s pour la
# même dictée). L'assertion NSActivity (UserInitiated + LatencyCritical) le
# lui interdit LE TEMPS DU TRAVAIL uniquement (aucun coût énergie au repos).
# --------------------------------------------------------------------------- #
def _begin_activity(reason: str):
    try:
        from Foundation import NSProcessInfo
        try:
            from Foundation import (NSActivityUserInitiated,
                                    NSActivityLatencyCritical)
            opts = NSActivityUserInitiated | NSActivityLatencyCritical
        except Exception:
            opts = 0x00FFFFFF | 0xFF00000000   # valeurs documentées NSProcessInfo.h
        return NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            opts, reason)
    except Exception:
        return None


def _end_activity(token) -> None:
    try:
        if token is not None:
            from Foundation import NSProcessInfo
            NSProcessInfo.processInfo().endActivity_(token)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# v1.0.6 — DÉTECTEUR DE FIGEAGE (« deadman switch » du thread principal).
# --------------------------------------------------------------------------- #
# INCIDENT TERRAIN : après un usage intensif, le thread principal Cocoa s'est
# figé (finalisation / insertion coincée AU SEIN du main thread). L'app est un
# agent menu-bar (politique Accessory) -> AUCUNE icône Dock -> impossible de la
# « forcer à quitter » sans ouvrir le Moniteur d'activité. Le watchdog de
# finalisation (30 s) libère bien _busy, mais il NE débloque PAS le main thread
# figé : l'UI reste morte et l'app fantôme tourne indéfiniment.
#
# PARADE : un battement de cœur estampillé par un NSTimer SUR LE MAIN THREAD
# (~1 s). Un thread DAEMON (qui SURVIT au gel du main thread) le surveille. Si le
# battement n'a pas avancé depuis MAIN_FREEZE_KILL_S, le main thread est
# irrécupérablement figé -> on JOURNALISE puis os._exit() : l'app se ferme
# d'elle-même, l'utilisateur la relance. Plus jamais d'app fantôme inkillable.
#
# FAUX POSITIFS ÉCARTÉS :
#  - Veille système / suspension du process (App Nap au repos) : le daemon mesure
#    SON PROPRE retard de réveil ; si sa boucle a dormi BEAUCOUP plus que prévu,
#    c'est que le process a été suspendu (pas un gel) -> on réarme la base sans
#    tuer. Au réveil, le NSTimer ré-estampille sous 1 s.
#  - Suivi d'événement (menu ouvert, glissement de fenêtre) : timer ajouté en
#    NSRunLoopCommonModes -> continue de battre, pas de faux gel.
#  - PHASE DE CONFIRMATION : avant de tuer, on redonne ~4 s au main thread pour
#    re-battre. S'il re-bat (throttling passager) OU si NOTRE propre sommeil a
#    dépassé (process suspendu), on s'abstient. Tuer exige un battement figé
#    AVANT et APRÈS confirmation, daemon non suspendu -> faux positif quasi nul.
#  - Seuil 60 s : très au-dessus du légitime (le main thread ne porte aucun gros
#    calcul — transcription/GPU/IO tournent sur des workers) ET au-dessus du
#    watchdog de finalisation (30 s), qui a sa chance de récupérer d'abord.
#    Récupération plus rapide qu'à 90 s sans sacrifier la sûreté (confirmation).
MAIN_FREEZE_KILL_S = 60.0
_main_heartbeat = [0.0]          # estampille (time.time) posée par le main thread
_freeze_guard_armed = [False]    # vrai une fois le 1er battement posé
_heartbeat_obj = None            # ref forte vers la cible NSTimer (sinon GC)
_heartbeat_timer = None          # ref forte vers le NSTimer (sinon GC)


def _arm_freeze_watchdog():
    """Pose le battement de cœur main-thread + lance le daemon de surveillance.
    À DISPATCHER sur la main queue (le NSTimer DOIT vivre sur la run loop du
    main thread). Best-effort, idempotent, ne lève jamais."""
    global _heartbeat_obj, _heartbeat_timer
    if _freeze_guard_armed[0]:
        return
    try:
        from Foundation import (NSObject, NSTimer, NSRunLoop,
                                 NSRunLoopCommonModes)

        class _Heartbeat(NSObject):
            def tick_(self, timer):
                _main_heartbeat[0] = time.time()

        hb = _Heartbeat.alloc().init()
        _main_heartbeat[0] = time.time()   # 1er battement immédiat (run loop active)
        _freeze_guard_armed[0] = True
        t = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, hb, "tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(t, NSRunLoopCommonModes)
        _heartbeat_obj = hb
        _heartbeat_timer = t
    except Exception as e:
        print(f"[watchdog] battement main-thread indispo ({e}) — détecteur OFF.")
        return

    def _freeze_monitor():
        CHECK = 5.0
        CONFIRM = 4.0
        last_wake = time.time()
        while True:
            time.sleep(CHECK)
            now = time.time()
            overslept = (now - last_wake) - CHECK
            last_wake = now
            # Process suspendu (veille / App Nap profond) : notre propre boucle a
            # pris bien plus que CHECK -> ce n'est PAS un gel. On réarme la base.
            if overslept > 30.0:
                _main_heartbeat[0] = now
                continue
            hb_ts = _main_heartbeat[0]
            if hb_ts <= 0.0:
                continue
            if (now - hb_ts) <= MAIN_FREEZE_KILL_S:
                continue
            # SUSPICION DE GEL -> CONFIRMATION : on redonne une chance de battre.
            t_confirm = time.time()
            time.sleep(CONFIRM)
            if (time.time() - t_confirm) - CONFIRM > 10.0:
                # NOTRE sommeil a dérapé -> process suspendu, pas un gel. On réarme.
                _main_heartbeat[0] = time.time()
                last_wake = time.time()
                continue
            if _main_heartbeat[0] != hb_ts:
                # Le main thread a re-battu pendant la confirmation -> vivant.
                last_wake = time.time()
                continue
            # Battement TOUJOURS figé après confirmation, daemon non suspendu :
            # gel réel et irrécupérable -> on journalise puis on force l'arrêt.
            age = time.time() - hb_ts
            try:
                if _EV:
                    _EV.log(_EV.E.MAIN_FREEZE_KILL, age=round(age, 1))
            except Exception:
                pass
            print(f"[watchdog] thread principal figé > {age:.0f}s -> arrêt "
                  "forcé de Vlocal (à relancer).")
            os._exit(70)

    threading.Thread(target=_freeze_monitor, name="freeze-watchdog",
                     daemon=True).start()


# v1.1.0 : le live-tail vit dans son module (livetail.py), testable sans
# charger l'app. Alias conservé pour les appelants historiques.
from livetail import DictationLiveTail as _DictationLiveTail  # noqa: E402


# Plancher du budget de finalisation d'une dictée (live-tail + fusion + insertion
# compris). Le budget réel est _finalize_budget_s(durée) : proportionnel à l'audio
# depuis la v1.1.0, jamais sous ce plancher. Filet silencieux : il libère le
# verrou si la finalisation ne rend pas la main, sans contredire le superviseur.
FINALIZE_WATCHDOG_S = 25.0

# --------------------------------------------------------------------------- #
# v1.0.9 — SUPERVISEUR DE DICTÉE (auto-heal). Garantit qu'un utilisateur n'est
# JAMAIS bloqué plus de quelques secondes. La cause racine (figeage audio au
# stop/open) est déjà neutralisée par les fermetures/ouvertures BORNÉES du moteur
# (récupération ~3 s). Ce superviseur est le filet GLOBAL pour TOUT le reste :
#   1) si la finalisation reste coincée > seuil (au-dessus du timeout MLX 22 s
#      pour ne jamais couper une vraie transcription) -> reset propre + notif ;
#   2) si ça se recoince malgré plusieurs resets -> dernier recours : l'app se
#      relance toute seule (garde-fou anti-boucle, l'utilisateur n'a rien à faire).
# --------------------------------------------------------------------------- #
from engine import dictation_gpu_timeout as _dictation_gpu_timeout  # noqa: E402


def _finalize_budget_s(rec_dur_s: float) -> float:
    """v1.1.0 : plafond de la finalisation d'une dictée de rec_dur_s secondes.
    Plancher FINALIZE_WATCHDOG_S ; au-delà, suit le timeout GPU proportionnel
    (engine.dictation_gpu_timeout) plus 3 s pour la fusion et l'insertion.
    Le plafond fixe de 25 s coupait les dictées de plusieurs minutes (finalisation
    légitime de 30 à 100 s) et les déclarait figées."""
    return max(FINALIZE_WATCHDOG_S, _dictation_gpu_timeout(rec_dur_s) + 3.0)


_DICT_RESET_MIN_S = 9.0       # v1.0.11 : 12 -> 9 s. PLANCHER SÛR : la formule garde +8 de base (marge au-dessus du pire cas de transcription LÉGITIME ~6-8 s : modèles rechargés + plancher turbo + ladder température) -> on ne descend PAS sous 9 sinon risque de couper une vraie dictée. Les dictées longues montent via durée×0.10+8.
_last_self_relaunch = [0.0]   # anti-boucle : 1 relance / 5 min max


def _self_relaunch(reason: str = "") -> None:
    """Dernier recours : relance proprement l'app (open -n) puis quitte. Anti-
    boucle (1 relance / 5 min). L'utilisateur n'a RIEN à faire : Vlocal réapparaît
    sain en ~1 s. Réservé au cas où un reset logiciel ne suffit pas (sous-système
    audio OS réellement irrécupérable)."""
    import time as _t
    now = _t.time()
    if now - _last_self_relaunch[0] < 300.0:
        print("[auto-heal] relance déjà tentée récemment -> abstention (anti-boucle).")
        return
    try:
        exe = sys.executable or ""
        i = exe.find(".app/Contents/MacOS/")
        bundle = exe[:i + 4] if i > 0 else None
        if not bundle or not os.path.isdir(bundle):
            print("[auto-heal] bundle introuvable -> relance annulée.")
            return
        _last_self_relaunch[0] = now
        print(f"[auto-heal] RELANCE de l'app ({reason}) : {bundle}")
        try:
            notifier.show("Vlocal redémarre",
                          _te("La dictée était bloquée. Vlocal se relance, réessayez dans un instant."),
                          subtitle=None)
        except Exception:
            pass
        import subprocess
        subprocess.Popen(["/bin/sh", "-c", 'sleep 1.2; /usr/bin/open -n "%s"' % bundle],
                         start_new_session=True)
        _t.sleep(0.4)
    except Exception as e:
        print(f"[auto-heal] relance KO ({e}).")
        return
    os._exit(0)


def _dictation_supervisor():
    """Daemon : surveille l'état de la dictée et garantit la récupération."""
    import time as _t
    resets = []          # horodatages des resets récents (détection re-coincement)
    last_reset = 0.0
    last_health = 0.0    # v1.0.22 — dernier check de santé du worker micro
    while True:
        _t.sleep(1.0)
        try:
            c = _controller
            if c is None:
                continue
            busy = bool(getattr(c, "_busy", False))
            active = bool(getattr(c, "_active", False))
            # v1.0.22 — SANTÉ PROACTIVE du worker micro : toutes les ~10 s AU
            # REPOS, ping du worker ; figé/mort -> respawn préventif EN FOND.
            # L'utilisateur ne rencontre (presque) jamais un worker coincé à
            # l'appui : il a été remplacé pendant l'inactivité.
            if (not busy) and (not active) and (_t.time() - last_health) > 10.0:
                last_health = _t.time()
                try:
                    _eng = getattr(c, "engine", None)
                    if _eng is not None and hasattr(_eng, "mic_health_heal"):
                        threading.Thread(target=_eng.mic_health_heal,
                                         name="mic-health", daemon=True).start()
                except Exception:
                    pass
            ft0 = float(getattr(c, "_finalize_t0", 0.0) or 0.0)
            t0 = float(getattr(c, "_t0", ft0) or ft0)
            now = _t.time()
            # COINCÉ = occupé, plus en enregistrement (utilisateur a relâché), et la
            # finalisation ne rend pas la main depuis trop longtemps. On mesure depuis
            # le DÉBUT de la finalisation (ft0). PLAFOND ADAPTATIF : 12 s pour une
            # dictée courte/moyenne (récup < 15 s visée), un peu plus pour une dictée
            # très longue dont la finalisation légitime dure plus. La marge
            # (0,10 x durée + 8) reste TOUJOURS au-dessus du temps de transcription
            # réel (~0,045 x durée) et du timeout moteur 22 s -> on ne coupe JAMAIS
            # une vraie transcription.
            rec_dur = max(0.0, ft0 - t0)
            ceiling = max(_DICT_RESET_MIN_S, rec_dur * 0.10 + 8.0)
            # v1.0.22 — on mesure l'INACTIVITÉ (dernier battement), pas la durée
            # totale : une transcription lente mais qui progresse n'est plus
            # déclarée coincée (sinon l'epoch était bumpé et le texte, arrivé
            # après, n'était jamais inséré). Repli sur ft0 si aucun battement.
            beat = float(getattr(c, "_fin_beat", 0.0) or 0.0) or ft0
            is_stuck = busy and (not active) and ft0 > 0 and (now - beat) > ceiling
            # v1.0.11 — FILET SUPPLÉMENTAIRE : un verrou « occupé » coincé SANS qu'une
            # finalisation ait démarré (ft0 == 0) n'était couvert par aucun garde-fou
            # -> « transcription précédente en cours » à vie. On le récupère aussi
            # (seuil prudent 6 s, bien au-dessus d'une finalisation qui démarre
            # normalement dans la seconde -> aucun faux positif sur une vraie dictée).
            bs = float(getattr(c, "_busy_since", 0.0) or 0.0)
            stale_busy = busy and (not active) and ft0 <= 0 and bs > 0 and (now - bs) > 5.0
            # v1.0.11 — DICTÉE ACTIVE FIGÉE : enregistrement « actif » mais le worker
            # audio est MORT (crash inattendu) -> plus de wave, plus de détection, et
            # stop_and_finalize ne viendra jamais. Cas genuinement cassé (en dictée
            # normale le worker tourne jusqu'au stop). Garde 2 s pour ignorer la
            # fenêtre de démarrage (active posé juste avant le lancement du worker).
            t0_rec = float(getattr(c, "_t0", 0.0) or 0.0)
            wkr = getattr(c, "_audio_worker", None)
            active_dead = active and (wkr is None or not wkr.is_alive()) and t0_rec > 0 and (now - t0_rec) > 2.0
            if not (is_stuck or stale_busy or active_dead):
                continue
            if now - last_reset < 8.0:   # ne pas spammer
                continue
            last_reset = now
            resets[:] = [t for t in resets if now - t < 60.0] + [now]
            print(f"[auto-heal] dictée coincée ({now - ft0:.0f}s) -> reset auto (#{len(resets)}).")
            try:
                c.force_reset("superviseur")
            except Exception:
                pass
            try:
                overlay.info(_te("Dictée réparée"), _te("Vous pouvez recommencer."))
                _schedule_overlay_hide(4.0)
            except Exception:
                pass
            try:
                # v1.0.22 — EN THREAD : notifier.show peut construire le bundle
                # de marque (sips/PlistBuddy, plusieurs secondes). Appelé en
                # synchrone, il retardait — voire tuait — la boucle du
                # superviseur, c'est-à-dire l'auto-réparation elle-même.
                threading.Thread(
                    target=lambda: notifier.show(
                        _te("Dictée réparée"),
                        _te("Vous pouvez recommencer, votre dictée est de nouveau prête."),
                        subtitle=None),
                    name="heal-notify", daemon=True).start()
            except Exception:
                pass
            # Re-coincement répété MALGRÉ les resets -> sous-système audio OS
            # irrécupérable -> dernier recours : relance auto (garde-fou interne).
            if len(resets) >= 3:
                _self_relaunch("dictée coincée à répétition")
        except Exception:
            continue


def _start_dictation_supervisor():
    try:
        threading.Thread(target=_dictation_supervisor, name="dict-supervisor",
                         daemon=True).start()
        print("[auto-heal] superviseur de dictée actif.")
    except Exception as e:
        print(f"[auto-heal] superviseur indispo ({e}).")


# v1.0.11 — DIAGNOSTIC AUTO (CONSENTI). Pour la cohorte early (clés 'comp'/testeurs),
# Vlocal envoie un diagnostic ANONYME (le MÊME payload sanitisé que l'envoi manuel :
# zéro audio, zéro texte dicté, zéro chemin/nom) ~1×/semaine vers l'admin, pour
# fignoler le produit. Défaut ON pour 'comp', OFF pour les payants (vie privée).
# Désactivable (réglage `auto_diag`), disclosure visible dans Réglages > Diagnostic.
# Ne casse rien : best-effort total, ne lève jamais, réutilise l'envoi existant.
_AUTO_DIAG_DAYS = 7

def _auto_diag_enabled() -> bool:
    try:
        s = _load_settings()
        return bool(s.get("auto_diag", False))
    except Exception:
        return False

def _auto_diag_loop():
    import time as _t
    _t.sleep(90)   # laisse le démarrage tranquille
    while True:
        try:
            if _auto_diag_enabled() and _api_instance is not None:
                try:
                    last = float(_load_settings().get("auto_diag_last", 0) or 0)
                except Exception:
                    last = 0.0
                if (_t.time() - last) > _AUTO_DIAG_DAYS * 86400:
                    try:
                        r = _api_instance.send_support_diagnostic(
                            "[auto] rapport periodique testeur", "", source="auto-diag")
                        if isinstance(r, dict) and r.get("ok"):
                            _save_settings({"auto_diag_last": _t.time()})
                    except Exception:
                        pass
        except Exception:
            pass
        _t.sleep(6 * 3600)   # re-vérifie toutes les 6 h

def _start_auto_diag():
    try:
        threading.Thread(target=_auto_diag_loop, name="auto-diag", daemon=True).start()
    except Exception:
        pass


class DictationController:
    def __init__(self, engine: VlocalEngine):
        self.engine = engine
        self._stop_evt = threading.Event()
        self._audio_worker = None       # v13.1 — référence du worker audio_level
        self._active = False
        # v19 — VERROU ANTI-CHEVAUCHEMENT : True de start() jusqu'à la FIN de
        # stop_and_finalize (transcription comprise). Empêche une 2e dictée
        # (bouton OU raccourci) de démarrer pendant qu'une transcription tourne
        # encore -> le moteur n'est pas réentrant, le chevauchement le bloquait
        # (« ça tourne dans le vide »). Auto-réparation si bloqué trop longtemps.
        self._busy = False
        self._busy_since = 0.0
        self._gen = 0   # CONC-5 : n° de dictée (un _finalize tardif ne doit pas
                        # écraser l'overlay d'une dictée plus récente)
        self._lock = threading.Lock()
        self._activity = None   # v21 — assertion anti-App Nap (begin/end)
        self._tail = None       # v21 — live-tail des dictées longues
        self._finalize_t0 = 0.0  # v1.0.9 — horloge de finalisation (superviseur auto-heal)
        self._mic_lost = False   # v1.0.11 — flux audio mort détecté -> ré-init profonde au prochain start
        # v1.0.22 — DICTÉE ENCHAÎNÉE : la capture (worker micro, process séparé)
        # est indépendante de la transcription -> on peut ENREGISTRER la dictée
        # suivante PENDANT que la précédente se finalise (usage intensif : fini
        # le refus « transcription précédente en cours » = fini l'audio perdu).
        # Ordre garanti par tickets FIFO ; validité d'insertion par _epoch
        # (bumpé UNIQUEMENT par force_reset — un start enchaîné n'invalide PLUS
        # la finalisation précédente, contrairement à _gen qui garde son rôle
        # de fraîcheur d'overlay).
        self._fin_beat = 0.0     # v1.0.22 — battement de cœur de la finalisation
        self._fin_cv = threading.Condition()
        self._fin_inflight = 0   # finalisations en vol (plafond 3)
        self._fin_next = 0       # prochain ticket FIFO à attribuer
        self._fin_serving = 0    # ticket FIFO en cours de service
        self._epoch = 0          # invalidation d'insertion (force_reset seul)

    def recording_active(self) -> bool:
        # v1.0.22 — LECTURE SANS VERROU (atomique sous le GIL). Avant, ce
        # getter prenait self._lock, que start() tient pendant TOUTE l'escalade
        # micro -> le end() du raccourci (relâchement) se bloquait derrière un
        # begin() en difficulté, y compris le consommateur de secours du pump
        # watchdog : le mécanisme censé secourir la dictée était lui-même
        # bloqué. C'était la cause du « je dois relancer l'appli ».
        return self._active

    def recording_duration(self) -> float:
        """Durée écoulée depuis le début de la dictée la plus récente (s).
        ≈0 si aucune dictée n'a encore démarré."""
        return max(0.0, time.time() - getattr(self, "_t0", time.time()))

    def start(self) -> bool:
        with self._lock:
            if self._active:
                return False
            # v1.0.22 — DICTÉE ENCHAÎNÉE : on ne refuse PLUS quand une
            # finalisation est en cours (l'ancien « démarrage refusé :
            # finalisation en cours » faisait PERDRE la dictée suivante en
            # usage intensif — l'utilisateur parlait dans le vide). La capture
            # (worker micro) est indépendante de la transcription ; l'ordre des
            # insertions est garanti par les tickets FIFO de stop_and_finalize.
            # Seul garde-fou : plafond de 3 finalisations en vol (sanité RAM ;
            # humainement inatteignable sauf pathologie -> le superviseur agit).
            # Le rattrapage « verrou périmé » de la v1.0.9 disparaît avec le
            # verrou lui-même (le filet stale_busy du superviseur reste).
            if self._fin_inflight >= 3:
                print("[dictée] démarrage refusé : 3 finalisations déjà en vol.")
                return False
            # v1.0.11 — si le flux micro est mort à la dictée précédente (détecteur de
            # flux mort), on ré-initialise PortAudio EN PROFONDEUR AVANT de rouvrir ->
            # 1re tentative propre, l'utilisateur ne subit pas un échec + retry.
            # v1.0.22 — BUDGET DUR DE L'APPUI : quoi qu'il arrive, start() rend
            # la main sous ~2 s. Au-delà, on ne fait pas patienter l'utilisateur
            # (il parlait dans le vide, parfois 100 s : 22 appuis en 107 s
            # observés au pire incident) — on rend la main, on l'informe, et on
            # RÉPARE EN FOND pour l'appui suivant (terrain : il réappuie sous 3 s).
            _budget_dl = time.monotonic() + 2.0
            if getattr(self, "_mic_lost", False):
                self._mic_lost = False
                try:
                    # respawn borné (process neuf) au lieu de la ré-init 9 s
                    self.engine.hard_mic_reset(timeout=1.0)
                except Exception:
                    pass
            # v13.1 — Bug observé : si on relançait une dictée alors qu'un
            # ancien worker audio était encore en train de boucler (cas rare
            # mais possible si stop_and_finalize a été interrompu), on
            # accumulait des threads zombies qui spammaient setAudioLevel
            # → latence UI cumulative. On nettoie l'éventuel ancien worker.
            if self._audio_worker is not None and self._audio_worker.is_alive():
                self._stop_evt.set()
                # v1.0.22 — 2,0 -> 0,3 s : ce join est sur le chemin de l'APPUI.
                # Le worker ne fait que pousser un niveau à 10 Hz ; s'il traîne,
                # son event est déjà posé et il meurt de lui-même (daemon).
                self._audio_worker.join(timeout=0.3)
            self._audio_worker = None
            # v1.0.22 — événement d'arrêt PAR DICTÉE (objet NEUF, pas un clear) :
            # avec le chaînage, la finalisation de la dictée précédente a posé
            # set() sur SON event ; un clear() partagé pouvait le lui retirer
            # avant que son worker de niveau ne l'ait vu (worker zombie).
            self._stop_evt = threading.Event()
            # P1-2 — micro indisponible / permission révoquée : start_recording LÈVE
            # (engine.py). v1.0.11 — AUTO-HEAL MICRO : un device CoreAudio coincé fait
            # échouer l'ouverture. Au lieu d'abandonner (et de forcer un redémarrage
            # de l'app), on ré-initialise PortAudio EN PROFONDEUR puis on RÉESSAIE une
            # fois -> récupération sur place, l'utilisateur n'a rien à faire. Le « micro
            # indisponible » est désormais AUSSI journalisé (avant : print/toast seuls
            # -> invisible dans le diagnostic). (_active/_busy pas encore posés : rien
            # à nettoyer en cas d'échec.)
            ok = False
            try:
                ok = self.engine.start_recording(deadline=_budget_dl)
                # v1.0.22 — chaînage : juste après un relâchement, la capture
                # précédente peut être en train de se refermer (fenêtre ~0,3 s :
                # jointure du worker de niveau + stop du flux). False ici =
                # « moteur encore en enregistrement » -> on re-tente jusqu'à
                # épuisement du budget (résolu en ~0,1 s en pratique).
                while (not ok) and _time_left(_budget_dl) > 0.05:
                    time.sleep(0.05)
                    ok = self.engine.start_recording(deadline=_budget_dl)
                if not ok:
                    print("[dictée] capture précédente toujours ouverte (budget épuisé).")
            except Exception as e:
                print(f"[dictée] micro indisponible : {e} -> reset dur + retry.")
                if _EV: _EV.log(_EV.E.MIC_DEVICE_FAIL, where="start", err=str(e)[:120])
                # v1.0.22 — ESCALADE BÉTON. Terrain 1.0.21 (diagnostics 24/07-10/08) :
                # 28 échecs « start », CHAQUE retry en échec aussi, 0 mic_recovered —
                # la ré-init in-process ne récupère JAMAIS un PortAudio corrompu dans
                # ce process (threads de close abandonnés coincés dedans). Le geste qui
                # marche toujours, c'est un PROCESS NEUF (relancer l'app). Désormais :
                #   1) hard_mic_reset = kill -9 + respawn du WORKER micro (état
                #      CoreAudio neuf, borné ~1 s) — plus une ré-init à l'aveugle ;
                #   2) retry après un délai (CoreAudio relâche le device) ;
                #   3) repli micro INTÉGRÉ résolu CÔTÉ WORKER (l'énumération
                #      in-process d'un parent coincé mentait) ;
                #   4) CHAQUE étape journalisée — le trou qui a rendu la 1.0.21
                #      indiagnosticable (échec du repli 100% silencieux) est bouché.
                # v1.0.22 — chaque étape est conditionnée au BUDGET RESTANT :
                # mieux vaut rendre la main en 2 s et réparer en fond que faire
                # patienter l'utilisateur (jusqu'à 134 s au pire cas théorique).
                _reset_ok = False
                if _time_left(_budget_dl) > 0.5:
                    try:
                        _reset_ok = bool(self.engine.hard_mic_reset(
                            timeout=min(1.2, _time_left(_budget_dl))))
                    except Exception:
                        pass
                    time.sleep(min(0.2, max(0.0, _time_left(_budget_dl))))  # CoreAudio relâche
                try:
                    if _time_left(_budget_dl) <= 0.05:
                        raise RuntimeError("budget épuisé")
                    ok = self.engine.start_recording(deadline=_budget_dl)
                    if ok:
                        print("[dictée] micro récupéré après reset dur (worker neuf).")
                        if _EV: _EV.log(_EV.E.MIC_RECOVERED, level="info", via="hard_reset")
                except Exception as e2:
                    print(f"[dictée] micro toujours indisponible après reset dur : {e2}")
                    if _EV: _EV.log(_EV.E.MIC_DEVICE_FAIL, where="start_retry", err=str(e2)[:120])
                    # Repli 1 : micro intégré, résolu dans le worker (process sain).
                    if _time_left(_budget_dl) > 0.05:
                        try:
                            ok = self.engine.start_recording(prefer="builtin",
                                                             deadline=_budget_dl)
                            if ok:
                                print("[dictée] micro récupéré sur le micro intégré (worker).")
                                if _EV: _EV.log(_EV.E.MIC_RECOVERED, level="info", via="builtin")
                        except Exception as e3:
                            if _EV: _EV.log(_EV.E.MIC_DEVICE_FAIL, where="fallback_builtin",
                                            err=str(e3)[:120])
                    # Repli 2 (dernier recours, chemin in-process historique) :
                    # candidats de l'énumération parent, tant que le budget tient.
                    if not ok:
                        for _dev in (self.engine.list_input_devices() or [])[:2]:
                            if _time_left(_budget_dl) <= 0.05:
                                break
                            try:
                                ok = self.engine.start_recording(device=_dev,
                                                                 deadline=_budget_dl)
                            except Exception:
                                continue
                            if ok:
                                print(f"[dictée] micro récupéré sur le périphérique {_dev}.")
                                if _EV: _EV.log(_EV.E.MIC_RECOVERED, level="info", device=_dev)
                                break
                        if not ok and _EV:
                            _EV.log(_EV.E.MIC_DEVICE_FAIL, where="fallback_exhausted")
                if not ok:
                    _mic_fail_count[0] += 1
                    _grab = _mic_grabber_name()
                    # v1.0.22 — RÉPARATION EN ARRIÈRE-PLAN : on a rendu la main
                    # dans le budget, mais on ne laisse pas le micro cassé. Un
                    # worker NEUF est préparé pendant que l'utilisateur lit le
                    # message ; le terrain montre qu'il réappuie sous ~3 s
                    # (médiane mesurée) et il retombera alors sur un micro sain.
                    # `_mic_lost` garantit en plus un départ propre au prochain
                    # start même si ce respawn échoue.
                    self._mic_lost = True
                    try:
                        threading.Thread(
                            target=lambda: self.engine.hard_mic_reset(timeout=5.0),
                            name="mic-repair-bg", daemon=True).start()
                    except Exception:
                        pass
                    # v1.0.21 — DIAGNOSTIC HONNÊTE. On vient d'essayer la ré-init PROFONDE
                    # PUIS le micro intégré EXPLICITEMENT. Si ça échoue ENCORE, une app
                    # « simplement lancée » (OBS en fond, etc.) n'est presque jamais la
                    # vraie cause : macOS ne verrouille pas le micro intégré à une app
                    # tierce. C'est le sous-système audio qui est figé. On ARRÊTE donc
                    # d'accuser OBS/Zoom à tort (plainte terrain : « ça me met qu'OBS est
                    # ouvert, beaucoup de fois ») et on donne le bon diagnostic.
                    if _mic_fail_count[0] >= 3:
                        # v1.0.22 — le « redémarrez votre Mac » ne tombe plus au
                        # 2e échec : avec le worker jetable, deux échecs d'affilée
                        # sont normaux le temps qu'un process neuf soit prêt. On
                        # ne parle d'impasse système qu'au 3e, et on dit d'abord
                        # la chose utile : réessayer (le micro vient d'être remis à neuf).
                        _toast("Le système audio de macOS semble bloqué. Réessayez "
                               "votre dictée : le micro vient d'être réinitialisé. Si "
                               "ça persiste, fermez les applications qui captent le "
                               "micro (visio, enregistrement).", kind="error")
                    elif _grab:
                        # 1er échec, sous-système sain : un enregistreur PEUT tenir le
                        # micro. On le mentionne au conditionnel (on n'en est pas certain).
                        _toast(f"{_grab} est peut-être en train d'utiliser le micro. "
                               "Réglez son micro sur « Aucun » ou fermez-le, puis "
                               "relancez votre dictée.", kind="error")
                    else:
                        _toast("Micro occupé une seconde, on le remet à neuf. "
                               "Réessayez votre dictée tout de suite.", kind="info")
                    return False
            if not ok:
                return False
            _mic_fail_count[0] = 0   # Fix 4 — micro OK : on remet le compteur à zéro
            self._active = True
            # v1.0.22 — _busy n'est PLUS posé au start : il signifie désormais
            # « finalisation(s) en vol » (posé par stop_and_finalize, compteur
            # _fin_inflight). Le start enchaîné ne doit pas le toucher.
            self._gen += 1            # CONC-5 : nouvelle dictée
            self._t0 = time.time()   # horodatage début (durée d'enregistrement)
            # v11 — Aperçu live SUPPRIMÉ : il était lent et faisait perdre confiance.
            # Pendant la dictée, seule la wave anime ; la transcription apparaît
            # avec un effet "reveal mot-à-mot" au stop.
            # v11 — Nouveau worker : pousse le RMS audio à l'UI à 10 Hz pour
            # que la wave "respire" en suivant la voix (sinon c'est juste un sinus
            # déterministe qui ne réagit pas → impression de bug).
            self._audio_worker = threading.Thread(
                target=self._audio_level_loop, args=(self._stop_evt,), daemon=True
            )
            self._audio_worker.start()
            # v21 — ANTI-VARIANCE (trois mesures, sortie texte inchangée) :
            # 1) assertion anti-App Nap le temps de la dictée + transcription ;
            # 2) préchargement ANTICIPÉ des modèles pendant que l'utilisateur
            #    parle (si l'éco-RAM les avait déchargés, le rechargement est
            #    masqué par la parole au lieu de s'ajouter à l'attente) ;
            # 3) live-tail (dictées longues, modes auto/fidèle) : fenêtres
            #    transcrites PENDANT la parole, reliquat seul à la relâche.
            self._activity = _begin_activity("Vlocal dictation")
            speed = "auto"
            try:
                speed = _load_settings().get("dictation_speed", "auto")
            except Exception:
                pass
            threading.Thread(
                target=lambda: self.engine.prewarm(
                    want_small=(speed in ("auto", "rapide"))),
                daemon=True).start()
            self._tail = (_DictationLiveTail(self.engine)
                          if speed in ("auto", "fidele") else None)
            return True

    def _audio_level_loop(self, stop_evt=None):
        """Pousse le RMS du buffer audio à l'UI à 10 Hz pendant la dictée.
        Permet à la wave UI de suivre la voix en temps réel.
        v1.0.22 — `stop_evt` = l'event de CETTE dictée (chaînage : self._stop_evt
        peut déjà appartenir à la dictée suivante au moment où on le lirait)."""
        stop_evt = stop_evt if stop_evt is not None else self._stop_evt
        _warned_no_audio = False
        while not stop_evt.is_set():
            stop_evt.wait(0.1)  # 10 Hz
            if stop_evt.is_set():
                break
            try:
                rms = self.engine.rms_recent(0.06)
                _ui("setAudioLevel(%.4f)" % rms)
                overlay.level(rms)   # alimente la waveform de l'overlay (no-op si absent)
                # v1.0.11 — DÉTECTEUR DE FLUX MORT : si plus aucun échantillon micro
                # n'arrive depuis >3 s (débranché, sortie de veille, casque BT coupé),
                # on PRÉVIENT l'utilisateur (une seule fois) au lieu de le laisser
                # dicter dans le vide, et on marque le micro pour ré-init profonde au
                # prochain démarrage. SILENCE normale = l'écart n'augmente pas (le
                # callback livre des échantillons silencieux) -> zéro faux positif.
                if not _warned_no_audio and self.engine.seconds_since_audio() > 3.0:
                    _warned_no_audio = True
                    self._mic_lost = True
                    if _EV: _EV.log(_EV.E.MIC_DEVICE_FAIL, where="stream_dead")
                    try:
                        overlay.info(_te("Aucun son"),
                                     _te("Micro déconnecté ? Relâchez et réessayez."))
                    except Exception:
                        pass
            except Exception:
                continue

    def cancel(self) -> bool:
        """v2 — ANNULE la dictée en cours : stoppe les workers, JETTE l'audio,
        AUCUNE transcription. Pour la touche Échap / bouton Annuler. L'utilisateur
        qui veut juste arrêter (dictée de 5 min) n'attend rien. False si rien à
        annuler. Idempotent et thread-safe (verrou _lock comme start/stop)."""
        with self._lock:
            if not self._active:
                return False
            self._active = False
            # v1.0.22 — on ne touche PLUS _busy ici : il signifie désormais
            # « finalisation(s) en vol » (chaînage) — annuler la capture en
            # cours ne doit pas masquer une transcription précédente au
            # superviseur. Le compteur _fin_inflight fait foi.
        # Stoppe le worker de niveau audio avant de jeter l'audio.
        self._stop_evt.set()
        if self._audio_worker is not None:
            self._audio_worker.join(timeout=2.0)
            self._audio_worker = None
        # v21 — live-tail + assertion : tout relâcher (annulation = rien à garder).
        tail = getattr(self, "_tail", None)
        if tail is not None:
            tail.abort()
            self._tail = None
        _end_activity(getattr(self, "_activity", None))
        self._activity = None
        # Ferme le micro et JETTE l'audio — pas de transcription.
        try:
            self.engine.cancel_recording()
        except Exception:
            pass
        return True

    def force_reset(self, reason: str = "") -> None:
        """v1.0.9 — AUTO-HEAL : ramène la dictée à un état PRÊT en une fraction de
        seconde, même si un thread est coincé dans le sous-système audio. N'attend
        AUCUN verrou (assignations atomiques sous le GIL) -> ne peut pas se bloquer
        à son tour. Bumpe `_gen` (toute finalisation en vol devient périmée et
        n'insère plus rien), coupe le worker audio, et REMET LE MICRO À NEUF
        (engine.reset_audio, fermeture bornée). Le prochain raccourci repart sain.
        Idempotent. Piloté par le superviseur de dictée et le reset manuel."""
        try:
            print(f"[auto-heal] reset dictée ({reason}).")
        except Exception:
            pass
        # 1) état -> libre immédiatement (un démarrage ultérieur passe)
        self._active = False
        self._busy = False
        self._busy_since = 0.0
        try:
            self._gen = int(getattr(self, "_gen", 0)) + 1
        except Exception:
            self._gen = 0
        # v1.0.22 — chaînage : invalide TOUTES les finalisations en vol (epoch :
        # texte périmé jamais inséré, attentes FIFO abandonnées) et remet la
        # file à zéro (aucun waiter ne reste suspendu). Assignations d'abord
        # (atomiques sous le GIL, jamais bloquantes), notification best-effort.
        self._epoch = int(getattr(self, "_epoch", 0)) + 1
        self._fin_inflight = 0
        self._fin_serving = int(getattr(self, "_fin_next", 0))
        try:
            with self._fin_cv:
                self._fin_cv.notify_all()
        except Exception:
            pass
        # 2) coupe le worker de niveau audio + relâche live-tail / assertion App Nap
        try:
            self._stop_evt.set()
        except Exception:
            pass
        self._audio_worker = None
        try:
            tail = getattr(self, "_tail", None)
            if tail is not None:
                tail.abort()
        except Exception:
            pass
        self._tail = None
        try:
            _end_activity(getattr(self, "_activity", None))
        except Exception:
            pass
        self._activity = None
        # 3) MICRO À NEUF : abandonne un flux figé, repart propre (jamais bloquant)
        try:
            self.engine.reset_audio()
        except Exception:
            pass

    def stop_and_finalize(self, notify_ui: bool = True) -> str:
        """Arrête tout, produit le texte FINAL propre, le copie, renvoie le texte.
        notify_ui=False : ne touche PAS la fenêtre principale (chemin raccourci ->
        seul l'overlay s'anime, pas de « double »).
        Le verrou anti-chevauchement (_busy) est TOUJOURS libéré à la fin (finally),
        même si la transcription lève une exception -> jamais bloqué « occupé »."""
        # v1.0.22 — ACQUISITION BORNÉE : si un start() est coincé dans l'escalade
        # micro (verrou tenu), le relâchement ne doit PAS attendre derrière lui —
        # c'était le scénario « je parle, je relâche, rien ne se passe, je dois
        # relancer l'appli ». Au-delà de 2 s, on déclenche l'auto-réparation.
        if not self._lock.acquire(timeout=2.0):
            print("[dictée] verrou de démarrage figé -> auto-réparation forcée.")
            if _EV: _EV.log(_EV.E.MIC_DEVICE_FAIL, where="start_lock_stuck")
            threading.Thread(target=self.force_reset, args=("verrou start figé",),
                             name="heal-lock", daemon=True).start()
            return ""
        try:
            if not self._active:
                return ""
            self._active = False
            # v1.0.22 — CHAÎNAGE : on DÉTACHE l'état de CETTE dictée (worker de
            # niveau, live-tail, assertion App Nap, event d'arrêt) : une dictée
            # suivante peut démarrer pendant cette finalisation et poser les
            # SIENS — le teardown ci-dessous ne doit toucher que les nôtres.
            _my_evt = self._stop_evt
            _my_worker = self._audio_worker
            self._audio_worker = None
            _my_tail = self._tail
            self._tail = None
            _my_act = self._activity
            self._activity = None
            # v1.1.0 : durée d'audio de CETTE dictée, pour dimensionner le budget
            # de finalisation (_t0 peut appartenir à la suivante après le release).
            _my_dur = max(0.0, time.time() - float(getattr(self, "_t0", 0.0) or time.time()))
        finally:
            self._lock.release()
        # v1.0.22 — ticket FIFO (ordre de transcription ET d'insertion garanti
        # entre dictées enchaînées) + compteur « en vol » (_busy dérivé).
        with self._fin_cv:
            _my_ticket = self._fin_next
            self._fin_next += 1
            self._fin_inflight += 1
            _my_epoch = self._epoch
            self._busy = True
            self._busy_since = time.time()
        self._finalize_t0 = time.time()   # v1.0.9 : départ de la finalisation (horloge superviseur)
        # v1.0.22 — BATTEMENT DE CŒUR : horodatage rafraîchi à chaque étape de la
        # finalisation. Le superviseur mesure désormais l'INACTIVITÉ, pas la
        # durée totale. Avant, son plafond (9-12 s) tombait SOUS les bornes
        # réelles du moteur (MLX 22 s) : une transcription lente mais SAINE
        # était déclarée « coincée », l'epoch bumpé, et le texte — qui finissait
        # par arriver — n'était JAMAIS inséré au curseur, avec en prime un
        # message « Dictée réparée » trompeur.
        self._fin_beat = time.time()
        # v29.2 — WATCHDOG DE FINALISATION (anticipe TOUTE la classe « dictée
        # infinie ») : la finalisation peut se figer AILLEURS que dans le worker
        # GPU (live-tail, fusion, verrou, attente disque...) — le timeout MLX ne
        # couvre que lui. Si _do_finalize ne rend pas la main en FINALIZE_WATCHDOG_S,
        # on RÉCUPÈRE de force : libère _busy + nettoie l'overlay -> la dictée
        # suivante repart, plus jamais de blocage infini. Le thread figé est
        # abandonné (daemon) ; son éventuel réveil tardif est sans effet utile.
        _wd_s = _finalize_budget_s(_my_dur)   # v1.1.0 : proportionnel à l'audio
        _fin_done = threading.Event()

        _wd_fired = [False]   # le watchdog a-t-il déjà rendu notre ticket ?

        def _finalize_watchdog():
            if _fin_done.wait(_wd_s):
                return
            print(f"[dictée] finalisation FIGÉE > {_wd_s:.0f}s -> "
                  "récupération forcée (watchdog).")
            if _EV: _EV.log(_EV.E.FINALIZE_TIMEOUT, s=round(_wd_s))
            # v1.0.22 — chaînage : libère le compteur + AVANCE la file FIFO
            # (une finalisation figée ne doit jamais retenir la suivante).
            with self._fin_cv:
                if _fin_done.is_set():
                    return   # la finalisation vient de finir (course au 25e s pile)
                _wd_fired[0] = True
                self._fin_inflight = max(0, self._fin_inflight - 1)
                self._fin_serving = max(self._fin_serving, _my_ticket + 1)
                self._busy = self._fin_inflight > 0
                self._fin_cv.notify_all()
            _end_activity(_my_act)
            # v1.0.9 — message confié au SUPERVISEUR de dictée (« Dictée réparée »,
            # qui agit plus tôt, ~12 s) : ce watchdog reste un filet SILENCIEUX
            # (il libère juste le verrou) pour ne JAMAIS contredire « réparée » par
            # « a calé » quelques secondes plus tard.
        threading.Thread(target=_finalize_watchdog, name="finalize-watchdog",
                         daemon=True).start()
        # v1.0.22 — BATTEUR BORNÉ. La transcription est un appel ATOMIQUE (GPU) :
        # sans battement pendant cette étape, le superviseur couperait à 9 s une
        # transcription parfaitement saine (et le texte, arrivé après, ne serait
        # jamais inséré). Le batteur couvre donc le TEMPS LÉGITIME du moteur
        # (FINALIZE_WATCHDOG_S, calé au-dessus du timeout MLX 22 s) — et PAS
        # au-delà : passé ce délai il se tait, l'inactivité redevient visible et
        # l'auto-réparation reprend tous ses droits sur une VRAIE panne.
        _beat_until = time.time() + _wd_s

        def _fin_heartbeat():
            while not _fin_done.wait(2.0):
                if time.time() >= _beat_until:
                    return   # au-delà du temps légitime : on laisse voir la panne
                self._fin_beat = time.time()
        threading.Thread(target=_fin_heartbeat, name="finalize-beat",
                         daemon=True).start()
        try:
            return self._do_finalize(notify_ui, _my_evt, _my_worker, _my_tail,
                                     _my_ticket, _my_epoch)
        finally:
            _fin_done.set()
            # v1.0.22 — rendre le ticket UNE seule fois : si le watchdog l'a déjà
            # rendu (finalisation qui se réveille tardivement), on n'avance que
            # la file (max idempotent), sans re-décrémenter le compteur.
            with self._fin_cv:
                if not _wd_fired[0]:
                    self._fin_inflight = max(0, self._fin_inflight - 1)
                self._fin_serving = max(self._fin_serving, _my_ticket + 1)
                self._busy = self._fin_inflight > 0
                self._fin_cv.notify_all()
            # v21 — fin de l'assertion anti-App Nap (succès OU échec).
            _end_activity(_my_act)

    def _do_finalize(self, notify_ui: bool = True, my_evt=None, my_worker=None,
                     my_tail=None, my_ticket: int = 0, my_epoch: int = 0) -> str:
        # v1.0.22 — chaînage : on opère sur l'état DÉTACHÉ de CETTE dictée
        # (my_evt/my_worker/my_tail, détachés sous verrou par stop_and_finalize) —
        # les champs self.* peuvent déjà appartenir à la dictée suivante.
        # Signale l'arrêt au worker de niveau audio (le NÔTRE).
        (my_evt or self._stop_evt).set()
        # v13.1 — On joint le worker audio_level (sinon thread zombie
        # qui continue à appeler _ui("setAudioLevel(...)") jusqu'à l'extinction
        # du process, et qui se cumule avec le worker de la dictée suivante
        # — c'est ce qui causait la latence croissante d'enregistrement en
        # enregistrement).
        if my_worker is not None:
            my_worker.join(timeout=2.0)

        # Transcription FINALE (vad on) + assemblage propre des espaces.
        # Vlocal 2 — Vitesse de dictée :
        #   auto   : CASCADE INTELLIGENTE tunée — small si l'audio est net
        #            (~1,3 s, 2,6x plus rapide, fidélité quasi identique sur le
        #            français courant), turbo au moindre doute GLOBAL (seuils
        #            calibrés sur la confiance réelle : fraction de mots <0,3,
        #            pas un seul mot comme l'ancienne cascade cassée). Le
        #            glossaire corrige les termes connus quel que soit le modèle.
        #   rapide : small forcé (vitesse max).
        #   fidele : turbo direct (fidélité max, plancher ~3,5 s).
        speed = "auto"
        try:
            speed = _load_settings().get("dictation_speed", "auto")
        except Exception:
            pass
        # v21 — QoS USER_INITIATED sur CE thread (et le décodage qu'il pilote) :
        # pas de démotion E-core/background pendant que l'utilisateur attend.
        from engine import _qos_user_initiated
        _qos_user_initiated()
        # v21 — live-tail : récupère les fenêtres déjà transcrites pendant la
        # parole (dictée longue). ([], 0) si la dictée était courte -> chemin
        # actuel inchangé.
        # v1.0.22 — ORDRE CORRIGÉ : on FERME LA CAPTURE D'ABORD (worker micro,
        # borné) -> le créneau est LIBRE pour la dictée suivante en ~0,3 s après
        # le relâchement, et la pastille micro s'éteint tout de suite. Avant, la
        # collecte du live-tail (join jusqu'à 20 s) passait AVANT la fermeture :
        # sur une dictée longue, le micro restait ouvert et la dictée suivante
        # était refusée pendant tout ce temps — la promesse d'enchaînement
        # tombait précisément là où elle est le plus utile.
        audio = self.engine.stop_recording()
        self._fin_beat = time.time()      # v1.0.22 : battement (capture fermée)
        lt = None
        tail = my_tail
        if tail is not None:
            texts, tpos = tail.stop_and_collect()
            if texts and tpos > 0:
                lt = (texts, tpos)
        self._fin_beat = time.time()      # v1.0.22 : battement (live-tail collecté)
        # Durée mesurée sur l'AUDIO (self._t0 peut déjà appartenir à la dictée
        # suivante en cas d'enchaînement).
        _rec_dur = float(len(audio)) / max(1, int(self.engine.sample_rate))
        # v1.0.22 — ORDRE GARANTI : les dictées enchaînées se transcrivent et
        # s'insèrent dans l'ordre des relâchements (tickets FIFO). Attente
        # BORNÉE (le watchdog/la fin du prédécesseur avance toujours la file) ;
        # un force_reset (epoch) invalide l'attente -> abandon propre.
        with self._fin_cv:
            _deadline = time.time() + _finalize_budget_s(_rec_dur) + 5.0
            while (self._fin_serving < my_ticket
                   and self._epoch == my_epoch
                   and time.time() < _deadline):
                self._fin_cv.wait(0.25)
            if self._epoch != my_epoch:
                print("[dictée] finalisation invalidée par un reset -> abandon.")
                return ""
        _tt0 = time.perf_counter()
        try:
            raw = self.engine.stop_and_transcribe(
                fast=(speed == "rapide"), adaptive=(speed == "auto"),
                live_tail=lt, audio=audio)
        except TimeoutError as e:
            # v29.1 — figeage GPU rare sous dictée intensive : mlx_engine a borné
            # l'inférence (60 s) et RÉINITIALISÉ le worker. On ne bloque pas : le
            # verrou _busy est libéré par le finally de stop_and_finalize, et la
            # dictée suivante repart sur un worker neuf (fini le blocage 180 s).
            print(f"[dictée] transcription interrompue ({e}).")
            if _EV: _EV.log(_EV.E.GPU_TIMEOUT)
            # v29.2 — et on NETTOIE l'overlay (sinon il restait coincé en
            # « transcription… » et chevauchait l'UI -> panneau fantôme).
            try:
                overlay.error(_te("La transcription a calé, réessayez."))
                _schedule_overlay_hide(3.0)
            except Exception:
                pass
            _toast("La transcription a calé un instant (GPU), c'est réinitialisé. "
                   "Relancez votre dictée, ça repart.", kind="error")
            return ""
        # v21 — ligne de DIAGNOSTIC par dictée (décomposition de la variance) :
        # visible dans vlocal.log pour vérifier la stabilité en conditions réelles.
        print(f"[diag] dictée {_rec_dur:.1f}s -> transcription "
              f"{time.perf_counter() - _tt0:.2f}s "
              f"(branche {getattr(self.engine, 'last_model', '?')}"
              f"{', live-tail ' + str(len(lt[0])) + ' fen.' if lt else ''})")

        # --- CHEMIN BRUT : instantané, prioritaire ---
        # Le texte brut nettoyé EST le produit garanti : copié IMMÉDIATEMENT.
        # GARDE-FOU : aucun post-traitement LLM ici — le reformatage SLM a été
        # supprimé en v15, le brut est le seul produit.
        # v9 TOP-5 #2 / v11 — Feedback explicite micro non autorisé / silence.
        # Aucun emoji (cf. brief V11). Texte clair sans jargon.
        if not raw:
            try:
                rms = getattr(self.engine, "last_rms", 0.0)
                peak = getattr(self.engine, "last_peak", 0.0)
                if rms < 0.001 and peak < 0.005:
                    # Aucun signal du tout = micro non autorisé ou mauvais
                    # périphérique. Message clair + chemin exact, sans jargon.
                    if _EV: _EV.log(_EV.E.MIC_DEVICE_FAIL, rms=round(float(rms), 5))
                    _toast(
                        "Aucun son n'a été capté. Vérifiez que le microphone "
                        "est autorisé : menu Pomme > Réglages Système > "
                        "Confidentialité et sécurité > Microphone, puis "
                        "autorisez Vlocal et relancez.", kind="error")
                else:
                    if _EV: _EV.log(_EV.E.SILENCE, rms=round(float(rms), 5))
                    _toast("Le son était très faible. Rapprochez-vous du "
                           "micro et réessayez.", kind="error")
            except Exception:
                pass
            return raw

        # v3.2.8 — FILTRE anti-hallucination POST-moteur (en plus de celui d'engine.py,
        # qu'on NE touche PAS). Attrape ce qui glisse : crédits de sous-titres hallucinés
        # ("Transcription by CastingWords", "Sous-titres réalisés par…", amara.org) et
        # boucles de répétition ("Er Er Er…" : un mot court répété 6x+ -> 1). Ne déclenche
        # JAMAIS sur une vraie phrase (seuil 6 répétitions consécutives identiques).
        try:
            import re as _re_h
            raw = _re_h.sub(r"(?i)(transcription by castingwords|sous-titr\w*\s+(réalisé|fait|par|pour)\b[^.\n]*|sous-titrage\s+société radio-canada|amara\.org|♪+)", "", raw)
            raw = _re_h.sub(r"(?i)\b(\w{1,4})(?:[\s,]+\1\b){5,}", r"\1", raw)
            raw = _re_h.sub(r"\s{2,}", " ", raw).strip()
        except Exception:
            pass
        if not raw:
            return ""   # hallucination pure scrubée -> rien à insérer (mieux que du faux)

        # Vlocal 2 — Correction GLOSSAIRE sur la dictée (noms propres/acronymes),
        # quel que soit le modèle utilisé par la cascade. Fidélité garantie même
        # quand small a fast-pathé.
        try:
            if _store is not None:
                gl = _store.glossary_pairs()
                if gl:
                    raw = processor.apply_glossary(raw, gl)
        except Exception:
            pass

        # Vlocal 2 — Raccourcis vocaux (snippets) : on dicte « ma signature »
        # -> insertion du bloc enregistré. Appliqué sur TOUS les modes.
        try:
            if _store is not None:
                pairs = _store.snippet_pairs()
                if pairs:
                    raw = processor.apply_glossary(raw, pairs)
        except Exception:
            pass

        # v1.0.14 — Commandes vocales de mise en forme (« à la ligne », « virgule »…),
        # APRÈS le glossaire/snippets et AVANT la copie/insertion. DICTÉE uniquement
        # (jamais en RÉUNION, qui a son propre pipeline). Isolé, déterministe, et
        # protégé : la moindre erreur -> texte inchangé (la fonction retourne
        # l'original). Aucune autre étape du pipeline n'est modifiée.
        try:
            if _current_mode() != "REUNION":
                raw = processor.apply_vocal_commands(raw)
        except Exception:
            pass

        # v29.10 — la copie EST le produit (collage Cmd+V). Si elle échoue (rare),
        # on NE ment PAS avec flashCopied : on prévient clairement, sinon le texte
        # serait perdu en silence. Le chemin SUCCÈS est INCHANGÉ (= dictée validée).
        _copy_ok = copy_to_clipboard(raw)
        if _copy_ok:
            if notify_ui:
                _ui("flashCopied()")
            # v1.0.18 — Fix 2 : si l'Accessibilité est OFF, le texte est copié mais
            # PAS inséré automatiquement au curseur. On le signale UNE SEULE FOIS
            # (jamais de spam), sans rien changer au repli presse-papier (= produit).
            try:
                if notify_ui and not _acc_warned[0] \
                        and not permissions.accessibility_ok():
                    _acc_warned[0] = True
                    _toast("Texte copié — collez avec Cmd+V. Pour l'insertion "
                           "automatique au curseur, autorisez Vlocal dans Réglages "
                           "Système > Confidentialité et sécurité > Accessibilité.",
                           kind="info")
            except Exception:
                pass
        else:
            print("[dictee] copie presse-papier ÉCHOUÉE -> texte non collable.")
            if _EV: _EV.log(_EV.E.CLIPBOARD_FAIL)
            _toast("Le texte n'a pas pu être copié dans le presse-papier. "
                   "Réessayez votre dictée.", "error")

        # v1.1.0 — compteur d'usage (dictées, mots, temps gagné) : aucun contenu,
        # alimenté quel que soit le réglage d'historique. Sert l'accueil et la
        # télémétrie déclarée (telemetry.py), qui est prévenue sans bloquer.
        try:
            if _store is not None and raw:
                _store.record_usage(_store.word_count(raw))
                if _telemetry is not None:
                    _telemetry.notify_usage()
        except Exception:
            pass
        # v18.4 — Historique des DICTÉES (texte seul, très léger). Persisté
        # UNIQUEMENT si le réglage de stockage = "all" (les rappels sont déjà
        # rangés dans leur table). Jamais d'audio pour une dictée.
        try:
            # v1.0.7 — un VRAI rappel daté est rangé dans Rappels, PAS aussi en
            # dictée (fini le doublon « rappel + dictée » remonté par Raphael).
            if _current_mode() == "DICTEE" and _store is not None \
                    and _load_settings().get("history_scope", "all") == "all" \
                    and not _looks_like_timed_reminder(raw):
                _store.add_dictation(raw, mode="DICTEE")
                # Bug #4 — rafraîchit l'accueil (mots / temps gagné / sessions du
                # mois) après une dictée, comme le fait déjà le chemin réunion.
                # Non bloquant (via _ui fire-and-forget), hors chemin de copie,
                # zéro impact latence/RAM.
                _refresh_lists_ui()
        except Exception as e:
            print(f"[dictee] historique non sauvegardé (sans gravité) : {e}")

        # --- POST-PROCESS (asynchrone, hors chemin brut) ------------------
        # v2 — En DICTÉE, on lance l'AUTO-DÉTECTION de rappel (« rappelle-moi… »
        # -> rappel créé). _post_process_by_mode est sans effet en RÉUNION.
        # Le texte brut est déjà copié (chemin presse-papier instantané).
        threading.Thread(
            target=_post_process_by_mode,
            args=(raw, _current_mode()),
            daemon=True,
        ).start()
        return raw


_controller = None  # type: DictationController | None
_store: "Store | None" = None
_menubar = None  # MenuBar (lazy import depuis menubar.py)
# v12 — état du mode Réunion (recorder + meeting id en cours)
_meeting_state = {
    "recorder": None,
    "recording": False,
    "id": None,
    "pipeline_busy": False,   # v13.1 — pipeline réunion en cours sur la précédente
    "live": None,             # v16 — transcripteur incrémental en cours
    "mode": "presentiel",     # v1.0.12 — 'presentiel' | 'visio'
}
# v16.1 — Verrou RÉENTRANT pour rendre ATOMIQUES les transitions réunion.
# pywebview peut exécuter deux appels JS->Python en parallèle (threads
# distincts) : sans ce verrou, un double-clic sur « Démarrer » pouvait passer
# deux fois le test `recording` et créer DEUX enregistrements/flux micro.
_meeting_lock = threading.RLock()

# v2 — DEUX modes seulement : DICTÉE (rappels auto-détectés) et RÉUNION.
# Persisté dans ~/Library/Application Support/Vlocal/mode.txt entre sessions.
_APP_SUPPORT = os.path.expanduser("~/Library/Application Support/Vlocal")
_MODE_FILE = os.path.join(_APP_SUPPORT, "mode.txt")
_SETTINGS_FILE = os.path.join(_APP_SUPPORT, "settings.json")
_LOG_FILE = os.path.join(_APP_SUPPORT, "vlocal.log")
_SHOW_REQUEST = os.path.join(_APP_SUPPORT, ".show_request")


def _setup_file_logging():
    """En .app empaqueté, stdout/stderr partent dans /dev/null (console=False) :
    impossible de diagnostiquer. On redirige les descripteurs 1 et 2 vers un
    fichier log (capture aussi les messages C natifs, ex. avertissements
    d'Accessibilité). Tronqué à chaque démarrage. En dev (non frozen), on
    laisse la console."""
    try:
        os.makedirs(_APP_SUPPORT, exist_ok=True)
        if getattr(sys, "frozen", False):
            fd = os.open(_LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.close(fd)
            # Flush à chaque ligne (sinon les print() Python restent en tampon
            # et le journal n'affiche que les messages C non bufferisés).
            try:
                sys.stdout.reconfigure(line_buffering=True)
                sys.stderr.reconfigure(line_buffering=True)
            except Exception:
                pass
            print("[log] Vlocal démarre — journal de diagnostic actif.")
    except Exception:
        pass
_GLOSSARY_FILE = os.path.join(_APP_SUPPORT, "glossary.txt")
_VALID_MODES = ("DICTEE", "REUNION")

# v12 — Mode RÉUNION ACTIVÉ par défaut (était feature flag en V11).
# Override possible : VLOCAL_REUNION=0 pour le désactiver explicitement.
# Lecture aussi de settings.json (clé reunion_enabled) prise en compte
# dans `_init_reunion_flag()` plus bas.
REUNION_ENABLED = os.environ.get("VLOCAL_REUNION", "1") not in ("0", "false", "False")
# v1.0.12 — Mode VISIO de la réunion (capture audio système, 100% additif).
# Kill-switch sans rebuild : VLOCAL_VISIO=0 -> seul le présentiel reste actif.
VISIO_MODE_ENABLED = os.environ.get("VLOCAL_VISIO", "1") not in ("0", "false", "False")


def _current_mode() -> str:
    try:
        with open(_MODE_FILE, "r") as f:
            m = (f.read() or "").strip().upper()
            if m in _VALID_MODES:
                return m
    except Exception:
        pass
    return "DICTEE"


def _set_mode(mode: str) -> str:
    mode = (mode or "DICTEE").upper()
    if mode not in _VALID_MODES:
        mode = "DICTEE"
    try:
        os.makedirs(os.path.dirname(_MODE_FILE), exist_ok=True)
        with open(_MODE_FILE, "w") as f:
            f.write(mode)
    except Exception:
        pass
    return mode


# v12 — Settings JSON simples (hotkey, reunion_enabled, ...)
# v20 — CACHE mémoire : _load_settings était relu + parsé sur tous les chemins
# chauds (finalisation de dictée, boucle de niveau audio, idle-unloader...).
# Invalidation par mtime (fichier modifié à la main) et par _save_settings.
# On retourne une COPIE (les appelants mutent parfois le dict reçu).
_SETTINGS_CACHE = {"mtime": None, "data": None}
_SETTINGS_LOCK = threading.Lock()


def _load_settings() -> dict:
    try:
        mtime = os.path.getmtime(_SETTINGS_FILE)
    except Exception:
        mtime = None
    with _SETTINGS_LOCK:
        if _SETTINGS_CACHE["data"] is not None and _SETTINGS_CACHE["mtime"] == mtime:
            return dict(_SETTINGS_CACHE["data"])
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data = data if isinstance(data, dict) else {}
    except Exception:
        data = {}
    # v3.2.1 — Migration vitesse : le dashboard exposait des valeurs (smart/fast/
    # accurate) que le moteur IGNORAIT (il n'accepte que auto/rapide/fidele) ->
    # tous les choix retombaient silencieusement sur « fidèle ». On normalise à la
    # lecture : on migre les réglages déjà enregistrés et tout choix inconnu.
    _sp = data.get("dictation_speed")
    if _sp is not None and _sp not in ("auto", "rapide", "fidele"):
        data["dictation_speed"] = {"smart": "auto", "fast": "rapide",
                                   "accurate": "fidele"}.get(_sp, "auto")
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE["mtime"] = mtime
        _SETTINGS_CACHE["data"] = dict(data)
    return data


def _diarization_enabled() -> bool:
    """v18 — Diarisation activée ? Défaut True. Toggle dans Réglages > Réunion."""
    try:
        return bool(_load_settings().get("diarization_enabled", True))
    except Exception:
        return True


def _voice_memory_enabled() -> bool:
    """v29.9 — Mémorisation des voix activée ? Défaut True. Toggle dans Réglages
    > Voix connues. Si False : aucune empreinte vocale n'est enregistrée
    (la diarisation de la réunion en cours marche toujours, seul l'apprentissage
    cross-réunions est désactivé)."""
    try:
        return bool(_load_settings().get("voice_memory_enabled", True))
    except Exception:
        return True


def _save_settings(patch: dict) -> dict:
    try:
        os.makedirs(_APP_SUPPORT, exist_ok=True)
    except Exception:
        pass
    # v20 — read-modify-write SOUS verrou (appels JS->Python concurrents) +
    # invalidation du cache (le mtime relu après écriture revalide).
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE["data"] = None
    current = _load_settings()
    if isinstance(patch, dict):
        current.update(patch)
    try:
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE["data"] = None
    return current


# v15 — Toute la machinerie « Puissance machine » / tier / téléchargement de
# moteur SLM (Ollama) a été SUPPRIMÉE. Vlocal n'a plus qu'un seul moteur :
# Whisper large-v3-turbo, déjà embarqué. Aucun appel réseau, aucun Ollama.


def _load_glossary_text() -> str:
    try:
        with open(_GLOSSARY_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _save_glossary_text(text: str) -> bool:
    try:
        os.makedirs(_APP_SUPPORT, exist_ok=True)
        with open(_GLOSSARY_FILE, "w", encoding="utf-8") as f:
            f.write(text or "")
        # Reset cache engine pour rechargement immédiat (helper verrouillé,
        # un seul écrivain de la sentinelle — best-effort comme avant).
        try:
            from engine import invalidate_glossary_cache
            invalidate_glossary_cache()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _refresh_lists_ui():
    """Pousse la liste des rappels à la fenêtre (silencieux si pas prête)."""
    if _store is None:
        return
    try:
        _ui("renderTasks(%s)" % json.dumps(_store.list_tasks(limit=10)))
    except Exception:
        pass
    # v1.0.6 — STATS LIVE : rafraîchit aussi la vue ouverte (Accueil compris) pour
    # que les compteurs se mettent à jour SANS avoir à quitter/revenir sur l'onglet.
    # No-op sûr si l'UI n'est pas encore prête (garde && côté JS).
    try:
        _ui("window.refreshLists && window.refreshLists()")
    except Exception:
        pass


def _refresh_whisper_bias():
    """v15 — Réinjecte les formes correctes du glossaire dans le biais
    hotwords de Whisper (passe 1, v20). Appelé après toute modif du glossaire."""
    if _store is None:
        return
    try:
        from engine import set_correction_terms
        set_correction_terms([c for _, c in _store.glossary_pairs()])
    except Exception:
        pass


def _menubar_labels():
    """v3.3 — libellés i18n du menu NSStatusItem (suivent la langue UI backend)."""
    if _UI_LANG == "en":
        return {"dictate": "Dictate", "open": "Open Vlocal", "reminders": "Recent reminders",
                "none": "  (none)", "settings": "Settings...", "quit": "Quit Vlocal"}
    return {"dictate": "Dicter", "open": "Ouvrir Vlocal", "reminders": "Rappels récents",
            "none": "  (aucune)", "settings": "Réglages...", "quit": "Quitter Vlocal"}


def _refresh_menubar():
    """Recharge les items + les libellés i18n du menu bar (best-effort)."""
    if _menubar is None:
        return
    try:
        _tasks = _store.list_tasks(only_open=True, limit=5) if _store else []
        _menubar.refresh(tasks=_tasks, labels=_menubar_labels())
    except Exception:
        pass


# v2 — Déclenchement d'un rappel : notification système FIABLE + bannière in-app
# garantie (si la fenêtre est ouverte) + réveil de l'app. On ne rate JAMAIS un
# rappel : si la notif système est bloquée, l'utilisateur voit quand même la
# bannière à l'écran.
def _fire_reminder(titre: str):
    titre = (titre or "").strip() or "Rappel"
    # 1) Notification système (osascript, fiable)
    try:
        notifier.show("Rappel", titre, subtitle=None)
    except Exception:
        pass
    # 2) Bannière in-app prominente + réveil de la fenêtre (macOS)
    try:
        _ui("if(typeof showReminder==='function') showReminder(%s);"
            % json.dumps(titre))
    except Exception:
        pass
    try:
        if sys.platform == "darwin":
            from AppKit import NSApplication
            NSApplication.sharedApplication().requestUserAttention_(1)  # rebond Dock
    except Exception:
        pass


# v20 — Timers de rappel TRAÇABLES : avant, un rappel supprimé ou coché
# SONNAIT quand même (le threading.Timer n'était ni annulé ni revalidé).
# Annulation à la suppression/au cochage + REVALIDATION au déclenchement
# (filet pour les timers ré-armés et les courses).
_REMINDER_TIMERS = {}
_REMINDER_TIMERS_LOCK = threading.Lock()


def _task_still_open(task_id) -> bool:
    if task_id is None:
        return True            # timer sans id (héritage) : on sonne
    try:
        for t in (_store.list_tasks(limit=500, only_open=True) if _store else []):
            if t.get("id") == task_id:
                return True
    except Exception:
        return True            # doute -> on sonne (ne jamais perdre un rappel)
    return False


def _cancel_reminder_timer(task_id):
    with _REMINDER_TIMERS_LOCK:
        t = _REMINDER_TIMERS.pop(task_id, None)
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass


def _schedule_reminder(at_ts: float, titre: str, task_id=None):
    """Planifie un rappel à at_ts via _fire_reminder (notif + bannière)."""
    import threading as _th
    delay = max(0.0, at_ts - time.time())

    def _fire():
        with _REMINDER_TIMERS_LOCK:
            _REMINDER_TIMERS.pop(task_id, None)
        if _task_still_open(task_id):
            _fire_reminder((titre or "")[:120])
        else:
            print(f"[rappel]  #{task_id} coché/supprimé avant l'échéance — silencieux.")

    t = _th.Timer(delay, _fire)
    t.daemon = True
    if task_id is not None:
        _cancel_reminder_timer(task_id)      # remplace un timer existant
        with _REMINDER_TIMERS_LOCK:
            _REMINDER_TIMERS[task_id] = t
    t.start()
    return t


# v9 — Déduplication tâches : empêche qu'une même dictée crée 2 tâches
# identiques. Critère : même source_raw_text ajouté dans les 10 dernières s.
_DEDUP_WINDOW_S = 10


def _recently_added_same(raw: str, store) -> bool:
    if not store or not raw:
        return False
    try:
        import time as _t
        recent = store.list_tasks(limit=5)
        now = _t.time()
        for t in recent:
            if t.get("source_raw_text") == raw and (now - (t.get("created_at") or 0)) < _DEDUP_WINDOW_S:
                return True
    except Exception:
        pass
    return False


def _looks_like_timed_reminder(raw: str) -> bool:
    """v1.0.7 — True UNIQUEMENT si la dictée deviendra un VRAI rappel : déclencheur
    (« rappelle-moi… ») ET heure détectable. Même verdict déterministe que
    _create_reminder_from_text -> sert à NE PAS aussi l'enregistrer en dictée
    (fini le doublon « rappel + dictée »). Une dictée banale au déclencheur ambigu
    SANS heure renvoie False -> elle reste une dictée normale (jamais perdue)."""
    try:
        if not raw or not reminders.is_reminder(raw):
            return False
        glossary = _store.glossary_pairs() if _store else []
        raw_gloss = processor.apply_glossary(raw, glossary) if glossary else raw
        data = reminders.parse_reminder(raw_gloss, time.time()) or {}
        return bool(data.get("datetime_iso"))
    except Exception:
        return False


def _post_process_by_mode(raw: str, mode: str):
    """v2 — DEUX modes seulement : DICTÉE et RÉUNION.

    DICTÉE : si le texte dicté est MANIFESTEMENT un rappel (« rappelle-moi… »,
    « n'oublie pas… », « pense à… »), on crée AUTOMATIQUEMENT le rappel (parseur
    local déterministe, instantané, hors-ligne) ; sinon c'est une dictée normale
    (le texte brut est déjà copié, rien d'autre à faire). RÉUNION a son propre
    pipeline. Silencieux en cas d'échec : le brut reste disponible."""
    if not raw or _store is None:
        return
    if (mode or "DICTEE").upper() == "REUNION":
        return
    try:
        if reminders.is_reminder(raw):
            _create_reminder_from_text(raw)
    except Exception as e:
        print(f"[dictée] auto-rappel ignoré ({e}).")
    # v1.0.13 — Capture Obsidian (ADDITIF, 100% local) : si un coffre est connecté,
    # chaque dictée est rangée au bon endroit via un routeur à mots-clés déterministe.
    # Hors du chemin critique (on est déjà sur un thread daemon) ; échec silencieux,
    # le brut reste copié/inséré quoi qu'il arrive.
    try:
        _s = _load_settings()
        if _s.get("obsidian_enabled") and _s.get("obsidian_vault"):
            obsidian.capture(raw, _s.get("obsidian_vault"), time.time())
    except Exception as e:
        print(f"[dictée] capture Obsidian ignorée ({e}).")


def _obsidian_meeting_sync(meeting_id):
    """v1.1.0 — Écrit (ou met à jour) la note Obsidian d'une réunion si un coffre
    est connecté. Appelé à la fin du pipeline, après une ré-analyse et après un
    renommage de locuteur. Thread daemon, échec silencieux : la réunion est déjà
    en base quoi qu'il arrive."""
    try:
        _s = _load_settings()
        if not (_s.get("obsidian_enabled") and _s.get("obsidian_vault")) or _store is None:
            return
        vault = _s.get("obsidian_vault")
        def _run():
            try:
                m = _store.get_meeting(int(meeting_id))
                if not m:
                    return
                blocks, names = None, {}
                try:
                    payload = json.loads(m.get("speaker_blocks_json") or "null")
                    if isinstance(payload, dict):
                        blocks = payload.get("blocks") or None
                        names = payload.get("names") or {}
                except Exception:
                    pass
                obsidian.capture_meeting(m, vault, blocks=blocks, names=names)
            except Exception as e:
                print(f"[reunion] note Obsidian ignorée ({e}).")
        threading.Thread(target=_run, name="obsidian-meeting", daemon=True).start()
    except Exception:
        pass


def _create_reminder_from_text(raw: str):
    """Crée un rappel depuis un texte dicté (parseur local). Notif planifiée à
    l'heure dite ; confirmation in-app QUOI + QUAND."""
    if _recently_added_same(raw, _store):
        return
    glossary = _store.glossary_pairs() if _store else []
    raw_gloss = processor.apply_glossary(raw, glossary) if glossary else raw
    now_ts = time.time()
    data = reminders.parse_reminder(raw_gloss, now_ts) or {}
    titre = (data.get("titre") or raw_gloss[:60]).strip() or "Rappel"
    titre = processor.apply_glossary(titre, glossary) if glossary else titre
    datetime_iso = data.get("datetime_iso")
    echeance_naturelle = data.get("echeance_naturelle")
    # v1.0.1 — ANTI FAUX POSITIF : un « rappel » SANS heure détectée n'est pas un vrai
    # rappel (il ne pourra jamais sonner) -> on ne le crée pas. Évite qu'une dictée
    # banale au déclencheur ambigu atterrisse dans Rappels avec « aucune » échéance.
    if not datetime_iso:
        print("[dictée] rappel sans horaire -> ignoré (anti faux positif).")
        return
    try:
        tid = _store.add_task(titre=titre, echeance=echeance_naturelle,
                              echeance_iso=datetime_iso, details=None,
                              source_raw_text=raw)
    except Exception:
        return
    # v1.1.0 — coffre « Vlocal, ma voix » : le rappel y est aussi consigné.
    try:
        _s = _load_settings()
        if _s.get("obsidian_enabled") and _s.get("obsidian_vault"):
            obsidian.capture_reminder(titre, echeance_naturelle, _s.get("obsidian_vault"))
    except Exception:
        pass
    iso_ts = notifier.parse_iso(datetime_iso)
    if iso_ts and iso_ts > now_ts:
        when = reminders.humanize_when(datetime_iso, now_ts)
        _schedule_reminder(iso_ts, titre, task_id=tid)
        try:
            _store.mark_notif_scheduled(tid)
        except Exception:
            pass
        _toast((f"C'est noté : « {titre} ». Vous serez prévenu {when}."
                if when else f"C'est noté : « {titre} »."), kind="success")
    elif iso_ts and iso_ts <= now_ts:
        _toast(f"Rappel « {titre} » enregistré, mais l'heure indiquée est déjà "
               "passée : aucune alerte ne sera envoyée.", kind="error")
    else:
        _toast(f"Rappel « {titre} » enregistré.", kind="info")
    _refresh_lists_ui(); _refresh_menubar()


# v18.5 — Plafond AUTO de locuteurs côté app (3 sites : frise live, repli WAV,
# re-diarisation). 6 en auto ; l'UI permet de FORCER jusqu'à 8 et le forçage
# bypasse ce plafond. NB : les défauts du moteur divergent entre eux
# (diarizer.diarize / finalize_stream = 8, _diarize_offline = 6) — à arbitrer
# en Phase 2.
MAX_AUTO_SPEAKERS = 6
# v27 — plafond de durée de la diarisation GUIDÉE-TRANSCRIPTION (voie primaire).
# L'extraction tourne en SOUS-PROCESS par lots (diarize_from_segments_subproc) :
# la RAM du process principal reste PLATE (~100-200 Mo, l'arène ONNX meurt avec
# chaque worker). Le plafond n'est plus qu'une borne de bon sens (temps
# d'extraction ~0,2-0,6 s/min d'audio). Mesuré : 34 min -> frise+attribution en
# ~20 s, RSS principal 207 Mo.
GUIDED_MAX_MIN = 240
# v3.4.0 — GARDE ANTI-OOM du repli diarisation IN-PROCESS : ce chemin (dia.diarize /
# tuiles) lit tout le WAV en RAM + ~1 inférence CAM++/s (fuite arène ONNX ~3 Go à
# 34 min, ~10 Go+ à 2h). Il ne sert QUE si la voie sous-process échoue (spawn KO) ou
# si la réunion dépasse GUIDED_MAX_MIN. Au-delà de ce plafond (aligné sur le cap live
# de 40 min) on DÉGRADE en « sans diarisation » plutôt que risquer un OOM sur 8 Go.
DIAR_INPROC_MAX_MIN = 40
# v29.3 — RÉUNION LIVE : au stop, on RE-TRANSCRIT proprement le WAV complet
# (segments Whisper nets) au lieu de diariser sur la transcription incrémentale
# (fenêtres ~25 s qui fusionnent des tours -> interjections courtes avalées).
# Mesuré sur réunion live réelle : 97,6 % (incrémental) -> 98,9 % (re-transcrit).
# Réservé aux réunions <= ce seuil (les très longues gardent l'incrémental : une
# re-transcription de 2 h au stop serait trop longue/chaude). +temps assumé pour
# la précision (choix utilisateur : « précision ultime »).
LIVE_RETRANSCRIBE_MAX_MIN = 45


def _recognize_voices(voices_map, log=False):
    """WhoTalks v1.2 — RECONNAISSANCE : compare le centroïde de chaque voix aux
    voix connues ; si match confiant, le nom apparaît tout seul. Conservateur
    (seuil + marge) : en cas de doute on laisse « Locuteur N ». Partagé par la
    pipeline réunion et rediarize_meeting. log=True : trace les voix reconnues
    et les échecs (chemin pipeline uniquement, comme avant factorisation)."""
    auto_names = {}
    try:
        import voiceid
        known = voiceid.load(_APP_SUPPORT)
        if known and voices_map:
            # v29 — ASSIGNATION GLOBALE par confiance décroissante. L'ancienne
            # boucle en ordre de dict + exclude_ids pouvait « voler » la bonne
            # voix au mauvais cluster -> noms INVERSÉS entre deux participants. On
            # collecte d'abord les matchs CONFIANTS (seuil + marge de
            # voiceid.match), puis on attribue la paire la PLUS sûre en premier,
            # en retirant cluster ET voix utilisés. Un cluster sans match
            # confiant reste « Locuteur N » (fail-safe, jamais un nom faux).
            cands = []
            for spk, info in voices_map.items():
                v, sc = voiceid.match(info.get("centroid"), known)
                if v:
                    cands.append((float(sc), spk, v["id"], v["name"]))
            cands.sort(reverse=True, key=lambda t: t[0])
            used_v = set()
            for sc, spk, vid, name in cands:
                if spk in auto_names or vid in used_v:
                    continue
                auto_names[spk] = name
                used_v.add(vid)
            if auto_names and log:
                print(f"[whotalks] voix reconnues : {auto_names}")
    except Exception as e:
        if log:
            print(f"[whotalks] reconnaissance ignorée ({e}).")
    return auto_names



def _attribute_blocks(diar_obj, units, wav_path, spk_segs):
    """Attribution au MOT (≥98 % mesuré sur réunions réelles) : empreinte
    serrée par mot -> centroïde le plus proche (capte les interruptions /
    chevauchements). Replis successifs : labels de fenêtre (assign_words),
    puis fusion par segments (offline). Partagé pipeline / rediarize."""
    import diarizer
    blocks = None
    # v1.0.23 — VOIE PRIMAIRE : attribution par UNITÉ DE PAROLE. L'ancien étage
    # décidait mot par mot sur des empreintes de 0,4 s — sous le plancher où une
    # empreinte vocale identifie encore quelqu'un (DER x2,7 entre 1,5 s et 0,5 s,
    # Park et al. ICASSP 2021) -> 15,4 changements de locuteur/min et 56 % de
    # tours coupés en pleine phrase sur une réunion réelle. On réutilise la frise
    # (déjà ~3,4 s d'audio par empreinte) au lieu de la jeter : zéro inférence en
    # plus, zéro RAM en plus, et plus rapide. VLOCAL_DIAR_UNITS=0 restaure
    # intégralement l'ancien comportement.
    # ⚠️ DÉSACTIVÉ PAR DÉFAUT (13/08). Mesuré sur la réunion réelle de 69 min
    # contre un diariseur indépendant (segmentation neuronale pyannote + autre
    # modèle d'empreintes) : l'attribution par unité AMÉLIORE la pureté
    # (88,9 % contre 84,4 % — elle tient mieux un seul locuteur dans un vrai
    # tour) mais DÉGRADE la couverture (80,5 % contre 87,6 % — elle fusionne
    # des tours de locuteurs différents ; vu à l'œil : deux personnes qui se
    # saluent réunies en un seul bloc). F1 84,5 contre 86,0 : ce n'est PAS un
    # progrès net, donc on ne l'impose pas aux clients sur une intuition.
    # VLOCAL_DIAR_UNITS=1 pour l'activer et poursuivre l'évaluation.
    if diar_obj is not None and os.environ.get("VLOCAL_DIAR_UNITS", "0") == "1":
        try:
            blocks = diar_obj.assign_units(units)
        except Exception as e:
            print(f"[whotalks] assign_units KO ({e}) -> repli attribution au mot.")
            blocks = None
    if diar_obj is not None and not blocks:
        try:
            # v27 — empreintes-mot extraites en SOUS-PROCESS (l'arène ONNX à
            # tailles variables meurt avec le worker ; blocs bit-identiques à
            # l'in-process, vérifié). Si le spawn échoue, les mots incertains
            # retombent sur leur LABEL-FENÊTRE (~98 % mesuré) — pas d'extraction
            # in-process ici (c'est voulu : RAM bornée avant tout).
            _fn = None
            try:
                import wave as _wv
                with _wv.open(wav_path, "rb") as _wf:
                    _fn = diarizer.make_subproc_embedder(
                        wav_path, _wf.getframerate(), mode="raw")
            except Exception:
                _fn = None
            blocks = diar_obj.assign_words_precise(units, wav_path,
                                                   embed_batch_fn=_fn)
        except Exception as e:
            print(f"[whotalks] assign_words_precise KO ({e}).")
        if not blocks:
            try:
                blocks = diar_obj.assign_words(units)
            except Exception as e:
                print(f"[whotalks] assign_words KO ({e}).")
    if not blocks:
        blocks = diarizer.merge_transcript_with_speakers(units, spk_segs)
    # v28 — filet anti-boucles (« c'est c'est ... » x60) sur le texte AFFICHÉ
    # des blocs (construit depuis les MOTS, donc pas couvert par le nettoyage
    # des segments). Réunion uniquement (_attribute_blocks n'est jamais dictée).
    try:
        from engine import collapse_repetition_loops as _crl
        for b in blocks or []:
            b["text"] = _crl(b.get("text") or "")
    except Exception:
        pass
    return blocks


def _visio_relabel_blocks(blocks, dom_path):
    """v1.0.12 — VISIO : réétiquette les blocs par SOURCE plutôt que de diariser
    le mix (la diarisation du mix donne des frontières fausses / labels qui
    swappent en plein milieu — CHECK 3). Le sidecar .dom contient (mic_rms,
    sys_rms) par bloc de 50 ms : micro dominant -> « Toi », système dominant ->
    « Interlocuteur ». On fusionne ensuite les blocs voisins du même locuteur
    (recolle les phrases coupées). Réservé à la visio (sidecar présent).
    Best-effort : renvoie les blocs inchangés si quoi que ce soit échoue.
    NB v1 : optimisé 1:1 (toi vs interlocuteur). Le multi-locuteurs (séparer les
    différents participants système) reste à brancher (diarisation système-seul)."""
    import numpy as _np
    try:
        dom = _np.fromfile(dom_path, dtype="<f4")
        if dom.size < 4:
            return blocks
        dom = dom.reshape(-1, 2)               # [N,2] : (mic_rms, sys_rms) / bloc 50 ms
    except Exception:
        return blocks
    _BS = 0.05
    relabeled = []
    for b in (blocks or []):
        try:
            s = max(0, int(float(b.get("start", 0.0)) / _BS))
            e = max(s + 1, int(float(b.get("end", 0.0)) / _BS))
            seg = dom[s:e]
            mic_e = float(_np.mean(seg[:, 0])) if seg.size else 0.0
            sys_e = float(_np.mean(seg[:, 1])) if seg.size else 0.0
        except Exception:
            mic_e = sys_e = 0.0
        nb = dict(b)
        # Ids au format SPEAKER_NN (l'UI les reconnaît : couleur stable + renommage).
        # Le nom affiché « Toi »/« Interlocuteur » est fourni via `names` (cf. pipeline).
        nb["speaker"] = "SPEAKER_00" if mic_e > sys_e else "SPEAKER_01"
        relabeled.append(nb)
    # Fusion des blocs voisins du même locuteur (recolle les phrases coupées).
    merged = []
    for nb in relabeled:
        if merged and merged[-1].get("speaker") == nb.get("speaker"):
            merged[-1]["end"] = nb.get("end", merged[-1].get("end"))
            t0 = (merged[-1].get("text") or "").rstrip()
            t1 = (nb.get("text") or "").lstrip()
            merged[-1]["text"] = (t0 + " " + t1).strip()
        else:
            merged.append(dict(nb))
    return merged


# v12 — Pipeline Réunion : transcription incrémentale (live.finalize() /
# transcribe_incremental), repli transcribe_detailed -> diarisation -> SQLite.
def _reunion_transcribe_pipeline(meeting_id, wav_path, audio_duree):
    """Asynchrone. Notifie l'UI à chaque étape via _ui.

    v13.1 — Marque `_meeting_state["pipeline_busy"]` à True pendant toute la
    durée, à False (try/finally) à la fin succès OU échec. Sans ce verrou,
    un 2e reunion_start pendant une pipeline qui dure 30-60 s sur un long
    enregistrement écrasait `_meeting_state["id"]` et l'UI mélangeait les
    statuts.
    """
    _meeting_state["pipeline_busy"] = True
    # v21 — anti-App Nap aussi pendant la pipeline réunion (fenêtre cachée +
    # micro fermé = throttling possible en pleine transcription/diarisation).
    _act = _begin_activity("Vlocal meeting pipeline")
    try:
        return _reunion_transcribe_pipeline_inner(meeting_id, wav_path, audio_duree)
    except Exception as e:
        # v22.2 — GARDE-FOU ULTIME : une exception non prévue (ce thread n'a
        # personne au-dessus pour la rattraper) ne doit JAMAIS laisser le timer
        # « Transcription en cours » tourner à l'infini. On la convertit en
        # erreur propre côté UI.
        import traceback as _tb
        print(f"[reunion] pipeline EXCEPTION : {e}\n{_tb.format_exc()}")
        try:
            _meeting_fail(meeting_id,
                          "La transcription de la réunion a échoué. L'audio est "
                          "conservé, vous pouvez réessayer.")
        except Exception:
            pass
    finally:
        _meeting_state["pipeline_busy"] = False
        _end_activity(_act)
        # relâche aussi l'assertion posée pendant l'ENREGISTREMENT (reunion_start)
        _end_activity(_meeting_state.pop("activity", None))


def _meeting_fail(meeting_id, message, extra_fields=None):
    """v22.2 — Sortie d'erreur TERMINALE du pipeline réunion. CORRIGE LE BUG DU
    « timer infini » : chaque chemin d'échec doit ARRÊTER le compteur UI
    (setMeetingStage 'error'), sinon « Transcription en cours » tournait sans
    fin (l'utilisateur ne voyait jamais la réunion aboutir ni échouer). On
    marque la réunion en erreur (récupérable), on prévient, on rafraîchit."""
    try:
        if _store and meeting_id:
            _store.update_meeting(meeting_id, status="error", **(extra_fields or {}))
    except Exception as e:
        print(f"[reunion] update_meeting(error) KO : {e}")
    if _EV: _EV.log(_EV.E.MEETING_FAIL, mid=str(meeting_id))
    if message:
        _toast(message, kind="error")
    _ui("setMeetingStage(%s)" % json.dumps("error"))   # ARRÊTE le timer UI
    _refresh_lists_ui()


def _reunion_transcribe_pipeline_inner(meeting_id, wav_path, audio_duree):
    import time as _t
    if not wav_path or not os.path.exists(wav_path):
        _meeting_fail(meeting_id,
                      "Le fichier de la réunion est introuvable. "
                      "L'enregistrement n'a pas pu être retrouvé sur le disque.")
        return

    # Étape 1 : transcription. v16 — On privilégie la transcription INCRÉMENTALE
    # déjà faite pendant l'enregistrement (live.finalize() ne traite que la
    # courte fenêtre finale). Repli sur la transcription complète du fichier si
    # le live est absent / a échoué / n'a rien produit.
    _ui("setMeetingStage(%s)" % json.dumps("transcribing"))
    t0 = _t.perf_counter()
    detailed = None
    live = _meeting_state.get("live")
    wt_diar = _meeting_state.get("diar")     # WhoTalks : empreintes accumulées
    is_import = bool(_meeting_state.get("import"))
    _meeting_state["live"] = None
    _meeting_state["diar"] = None
    _meeting_state["import"] = False
    # IMPORT d'un fichier : transcription INCRÉMENTALE par fenêtres + PACING
    # (charge CPU lissée -> pas de chauffe) au lieu du monobloc transcribe_detailed,
    # ET alimentation du diariseur EN LIGNE -> diarisation quasi instantanée ensuite.
    if is_import:
        imp_diar = None
        try:
            import diarizer as _dz
            import wave as _wave
            import numpy as _np
            # Durée/format via l'EN-TÊTE seulement (aucun chargement audio).
            with _wave.open(wav_path, "rb") as _wf:
                _sr = _wf.getframerate(); _ch = _wf.getnchannels()
                _nfr = _wf.getnframes()
            _dur_min = _nfr / float(_sr * 60.0)
            _econ = _load_settings().get("cpu_economy", False)
            # v22.3 — POLITIQUE RAM/THERMIQUE mesurée (pic RSS sur 12,6 min,
            # turbo seul = 1760 Mo) :
            #   batched 4 : 3536 Mo (+1776), 71 % CPU -> swap sur 8 Go +
            #               ventilateurs MÊME sur 24 Go. ET 0 gain de temps vs
            #               batch 2 quand c'est pacé (le pacing domine). À BANNIR.
            #   batched 2 : 2672 Mo (+912), pour les fichiers COURTS sur machine
            #               LARGE (rafale brève, vrai gain de vitesse à pace 0).
            #   incrémental : 1760 Mo (turbo seul), 64 % CPU = LE PLUS FRAIS et
            #               RAM-minimal, +15 % de temps seulement. C'est le choix
            #               par défaut pour 8 Go ET pour tout fichier long.
            # Le batched n'est donc retenu QUE : machine > 8 Go ET fichier court
            # (<= 5 min) ET pas en mode silencieux. Sinon -> incrémental.
            # v23 — si le GPU MLX est dispo, on N'utilise PAS le batched
            # (spécifique CPU/faster-whisper) : l'incrémental route vers
            # transcribe_window -> MLX par fenêtre (~7x, bref) et nourrit le
            # diariseur en ligne. Le batched reste pour les machines CPU-only.
            import mlx_engine as _mlx
            _total_gb = _total_ram_gb()
            _use_batched = ((not _econ) and not _mlx.available()
                            and _total_gb > 8.0 and _dur_min <= 5.0)
            # v27 — la diarisation des IMPORTS passe par la voie GUIDÉE en fin
            # de pipeline (sous-process, RAM plate, DER 0,039) : nourrir les
            # TUILES pendant la transcription est devenu redondant (temps + RAM
            # arène pour un simple repli). On ne les nourrit plus QUE pour les
            # fichiers au-delà du plafond guidé (replis historiques).
            _feed_tiles = _dur_min > GUIDED_MAX_MIN
            if _use_batched:
                _bs = 2   # JAMAIS 4 (gâchis RAM/thermique pour 0 gain mesuré)
                if _feed_tiles and _diarization_enabled() and _dz.available():
                    imp_diar = _dz.Diarizer(); imp_diar.reset_stream()
                # fichier court (<= 5 min) : rafale brève -> peu/pas de pacing.
                _pace_b = 0.0 if _dur_min <= 3 else 0.45
                detailed = _controller.engine.transcribe_import(
                    wav_path, batch_size=_bs, pace=_pace_b, mode="reunion",
                    on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p),
                    on_window=((lambda pcm, s, off: imp_diar.feed_window(pcm, s, off))
                               if imp_diar is not None else None))
                if detailed and detailed.get("text"):
                    wt_diar = imp_diar
                    print(f"[import] batched x{_bs} pace={_pace_b} "
                          f"({len(detailed.get('segments', []))} segments, "
                          f"RAM totale {_total_gb:.0f} Go).")
                else:
                    detailed = None
                    if imp_diar is not None and wt_diar is not imp_diar:
                        try:
                            imp_diar.unload()
                        except Exception:
                            pass
                        imp_diar = None
            if detailed is None:
                # VOIE LISSE (mode silencieux, RAM serrée, ou échec batched) :
                # incrémental par fenêtres + PACING + diarisation EN LIGNE
                # (tuiles seulement au-delà du plafond guidé, cf. _feed_tiles).
                if _feed_tiles and _diarization_enabled() and _dz.available():
                    imp_diar = _dz.Diarizer(); imp_diar.reset_stream()
                # v20 (RAM) — np.memmap int16 du WAV : ~0 RAM audio côté app
                # (avant : tout le fichier en float32, ~460 Mo pour 2 h). Le
                # moteur convertit PAR FENÊTRE (byte-identique). Offset 44 sûr :
                # WAV écrit par import_meeting_audio (wave, en-tête standard).
                _au = _np.memmap(wav_path, dtype="<i2", mode="r", offset=44)
                _au = _dz._downmix(_au, _ch)
                # v27 — PLEIN DÉBIT GPU pour l'import : gros blocs ~10 min vers
                # mlx_whisper (fenêtrage 30 s natif) = ×28 temps réel mots
                # inclus, vs ~×13 en fenêtres 25 s. Seulement quand les tuiles
                # ne sont pas nourries (la diarisation guidée en sous-process
                # prend le relais en fin de pipeline). Repli incrémental sinon.
                # <= 45 min : enveloppe RAM mesurée/verrouillée du plein débit.
                # Au-delà : fenêtré incrémental (RAM stable éprouvée, ~×13).
                if _mlx.available() and not _feed_tiles and _dur_min <= 45:
                    try:
                        detailed = _controller.engine.transcribe_import_mlx(
                            _au, sr=_sr,
                            on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p))
                        print(f"[import] plein débit GPU "
                              f"({len(detailed.get('segments', []))} segments).")
                    except Exception as e:
                        print(f"[import] plein débit GPU KO ({e}) -> incrémental.")
                        detailed = None
                if detailed is None:
                    # PACING ADAPTATIF v22.4 : MESURÉ sur fichier réel de 8,7 min,
                    # pace=0 donnait un duty de 0,99 SOUTENU ~2 min -> ventilateurs.
                    # Seuls les fichiers très courts (<= 3 min, rafale brève sous
                    # l'inertie thermique) passent sans pause ; au-delà : PLANCHER
                    # 0,3 puis rampe avec la durée. Le pacing n'affecte que le
                    # rythme, jamais le texte.
                    if _mlx.available():
                        # GPU : rafales brèves par fenêtre, pas de chauffe
                        # soutenue -> aucun pacing (toute la vitesse).
                        _pace = 0.0
                    elif _dur_min <= 3:
                        _pace = 0.3 if _econ else 0.0
                    else:
                        _ramp = max(0.3, min(0.8, (_dur_min - 12) / 45.0 + 0.3))
                        _pace = max(_ramp, 0.6) if _econ else _ramp
                    detailed = _controller.engine.transcribe_incremental(
                        _au, sr=_sr,
                        on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p),
                        on_window=((lambda pcm, s, off: imp_diar.feed_window(pcm, s, off))
                                   if imp_diar is not None else None),
                        pace=_pace)
                    print(f"[import] transcription incrémentale + diarisation en "
                          f"ligne ({len(detailed.get('segments', []))} segments, "
                          f"pacing={_pace}).")
                wt_diar = imp_diar
        except Exception as e:
            print(f"[import] incrémental KO ({e}) -> repli monobloc.")
            detailed = None
            # Libère CAM++ si l'incrémental a échoué AVANT la passation à
            # wt_diar : sinon l'extracteur resterait chargé pendant tout le
            # repli monobloc (turbo + CAM++ simultanés sur 8 Go). La garde
            # « wt_diar is not imp_diar » préserve les empreintes si l'échec
            # survient APRÈS la passation.
            if imp_diar is not None and wt_diar is not imp_diar:
                try:
                    imp_diar.unload()
                except Exception:
                    pass
                imp_diar = None
    if live is not None and not getattr(live, "failed", False):
        try:
            detailed = live.finalize()
            if detailed and detailed.get("text"):
                print(f"[reunion] transcription incrémentale OK "
                      f"({len(detailed.get('segments', []))} segments).")
            else:
                detailed = None
        except Exception as e:
            print(f"[reunion] finalize live KO : {e} — repli complet.")
            detailed = None
    elif live is not None:
        try:
            live.finalize()   # arrête proprement la boucle
        except Exception:
            pass
    # v29.3 — QUALITÉ DIARISATION live : remplace les segments incrémentaux
    # (grossiers) par une RE-TRANSCRIPTION propre du WAV complet pour une réunion
    # LIVE COURTE -> diarisation guidée sur segments Whisper nets (interjections
    # courtes plus avalées). Le texte affiché en direct pendant l'enregistrement
    # n'est pas touché ; on ne re-transcrit qu'AU STOP, et seulement pour les
    # courtes. Échec -> on garde l'incrémental (sans régression).
    if not is_import and detailed is not None:
        try:
            import wave as _wvq
            import numpy as _npq
            import mlx_engine as _mlxq
            # FIX v29.3.1 : durée calculée LOCALEMENT depuis le WAV (l'ancien code
            # référençait _dur_min, défini SEULEMENT dans la branche import ->
            # UnboundLocalError en live -> réunion en erreur).
            with _wvq.open(wav_path, "rb") as _wfq:
                _srq = _wfq.getframerate()
                _durq_min = _wfq.getnframes() / float(_srq * 60.0)
            if _mlxq.available() and _durq_min <= LIVE_RETRANSCRIBE_MAX_MIN:
                _auq = _npq.memmap(wav_path, dtype="<i2", mode="r", offset=44)
                _clean = _controller.engine.transcribe_import_mlx(
                    _auq, sr=_srq,
                    on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p))
                if _clean and _clean.get("segments"):
                    detailed = _clean
                    print("[reunion] live court -> re-transcription propre pour "
                          f"diarisation ({len(_clean.get('segments', []))} seg).")
            elif (not _mlxq.available()) and _durq_min <= LIVE_RETRANSCRIBE_MAX_MIN:
                # v1.0.1 — REPLI CPU (sans GPU MLX) : les segments live incrémentaux
                # sont trop grossiers (~18 s) -> le diariseur est affamé et s'effondre
                # à 1 SEUL locuteur (« catastrophique »). On re-transcrit proprement au
                # CPU (transcribe_detailed = segments mot-à-mot fins) pour nourrir la
                # diarisation. Restaure la qualité v29.3 sur machine sans GPU (clients
                # macOS < 14 / Intel + bascule CPU transitoire). Aucun effet si MLX actif.
                _cleancpu = _controller.engine.transcribe_detailed(
                    wav_path, mode="reunion",
                    on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p))
                if _cleancpu and _cleancpu.get("segments"):
                    detailed = _cleancpu
                    print("[reunion] live court (repli CPU) -> re-transcription propre "
                          f"pour diarisation ({len(_cleancpu.get('segments', []))} seg).")
        except Exception as e:
            print(f"[reunion] re-transcription propre KO ({e}) -> incrémental gardé.")
    if detailed is None:
        # v27 — repli ROUTÉ GPU d'abord : transcribe_detailed chargerait le
        # turbo CT2 (~1,7 Go) même sur machine MLX (contrat RAM violé). On
        # tente le plein débit GPU (memmap, RAM bornée) avant le monobloc CPU.
        try:
            import mlx_engine as _mlxf
            if _mlxf.available():
                import wave as _wvf
                import numpy as _npf
                with _wvf.open(wav_path, "rb") as _wff:
                    _srf = _wff.getframerate()
                _auf = _npf.memmap(wav_path, dtype="<i2", mode="r", offset=44)
                detailed = _controller.engine.transcribe_import_mlx(
                    _auf, sr=_srf,
                    on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p))
                print("[reunion] repli plein débit GPU OK.")
        except Exception as e:
            print(f"[reunion] repli GPU KO ({e}) -> monobloc CPU.")
            detailed = None
    if detailed is None:
        try:
            detailed = _controller.engine.transcribe_detailed(
                wav_path, mode="reunion",
                on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p),
            )
        except Exception as e:
            print(f"[reunion] transcription échouée : {e}")
            _meeting_fail(meeting_id,
                          "La transcription de la réunion n'a pas pu aboutir. "
                          "L'enregistrement audio est conservé, réessayez.")
            return
    dur_transcribe = _t.perf_counter() - t0
    raw_text = detailed.get("text", "")
    segments = detailed.get("segments", [])
    avg_conf = detailed.get("avg_conf", 1.0)
    if not raw_text:
        _meeting_fail(meeting_id,
                      "Aucune parole n'a été détectée dans cet enregistrement. "
                      "Vérifiez que le micro captait bien le son.",
                      extra_fields={"duree_transcription_s": dur_transcribe})
        return

    glossary = _store.glossary_pairs() if _store else []
    # Brut = transcription Whisper + glossaire (sécurité noms propres).
    brut = processor.apply_glossary(raw_text, glossary) if glossary else raw_text

    if _store and meeting_id:
        _store.update_meeting(
            meeting_id,
            transcription_brute=brut,
            duree_transcription_s=dur_transcribe,
            status="formatting",
        )

    # Étape 2 : v15 — MISE EN FORME PAR RÈGLES (déterministe, fidèle) :
    # paragraphage par pauses (segments), ponctuation, tics, puis glossaire.
    # Plus aucune IA générative.
    _ui("setMeetingStage(%s)" % json.dumps("structuring"))
    try:
        structured = processor.format_by_rules(raw_text, segments=segments)
    except Exception as e:
        print(f"[reunion] mise en forme KO : {e} — brut conservé.")
        structured = brut
    if glossary:
        structured = processor.apply_glossary(structured, glossary)

    # v15 — Données de confidence pour l'affichage (mots peu fiables en gris).
    conf_json = None
    try:
        conf_json = json.dumps({"segments": segments, "avg_conf": avg_conf},
                               ensure_ascii=False)
    except Exception:
        conf_json = None

    # v18 — DIARISATION (qui parle quand), EN RELAIS, optionnelle, repli
    # silencieux. Backend LÉGER sherpa-onnx (~0,35 Go) : turbo + diarisation =
    # ~2,05 Go < 2,5 Go, donc on NE décharge PAS Whisper (le rechargement
    # coûterait plus que ce qu'on économise). Garde-fous : si désactivé dans les
    # Réglages, si sherpa/modèles absents, ou si la diarisation échoue -> on
    # stocke speaker_blocks=None et l'UI affiche le texte continu comme avant.
    speaker_blocks_json = None
    _visio_dom = (wav_path or "") + ".dom"
    if os.path.exists(_visio_dom):
        # v1.0.12 — VISIO : diarisation par SOURCE (dominance micro/système),
        # CAM++ NON chargé (RAM ↓, sous 2,10 Go). « Toi » = micro, « Interlocuteur »
        # = système. Présentiel (pas de sidecar .dom) garde CAM++ -> branche elif.
        _ui("setMeetingStage(%s)" % json.dumps("diarizing"))
        try:
            _vb = _visio_relabel_blocks(segments, _visio_dom)
            if glossary:
                for _b in _vb:
                    _b["text"] = processor.apply_glossary(_b["text"], glossary)
            if _vb:
                speaker_blocks_json = json.dumps(
                    {"blocks": _vb,
                     "names": {"SPEAKER_00": "Toi", "SPEAKER_01": "Interlocuteur"},
                     "voices": {}}, ensure_ascii=False)
                print(f"[visio] diarisation par source (sans CAM++) -> {len(_vb)} blocs")
        except Exception as _e:
            print(f"[visio] diarisation source KO ({_e}) -> texte continu.")
        _meeting_state.pop("n_spk_forced", None)   # purge (sinon contamine la réunion suivante)
    elif _diarization_enabled():
        try:
            import diarizer
            if diarizer.available():
                _ui("setMeetingStage(%s)" % json.dumps("diarizing"))
                td0 = _t.perf_counter()
                # v18.5 — nombre de locuteurs : "auto" (défaut) ou un entier fixé
                # par l'utilisateur (Réglages > Mode Réunion). Le forcer est la
                # voie la plus FIABLE (ex. 2 pour un appel à deux).
                try:
                    _sp = _load_settings().get("meeting_speakers", "auto")
                    n_spk_set = int(_sp) if str(_sp).isdigit() else 0
                except Exception:
                    n_spk_set = 0
                # v26 — choix PER-RÉUNION du sélecteur « Combien de participants ? »
                # (posé par reunion_start, consommé ICI une seule fois). Prioritaire
                # sur le réglage global ; les imports/rediarisations, qui ne passent
                # pas par le sélecteur, gardent le réglage des Réglages (pas de
                # contamination d'un choix ponctuel).
                _forced = _meeting_state.pop("n_spk_forced", None)
                if _forced is not None:
                    n_spk_set = int(_forced)
                # WhoTalks v1.1 — VOIE RAPIDE : si le diariseur live a accumulé
                # les empreintes PENDANT la réunion, il ne reste que le clustering
                # (~0,1 s). Aucun relais RAM, aucun re-scan du WAV, zéro
                # ralentissement au stop (fin du beachball par construction).
                spk_segs = None
                voices_map = {}          # WhoTalks v1.2 — centroïdes par voix
                diar_obj = None          # diariseur ayant produit spk_segs (labels fenêtre)
                dia = None               # diariseur de REPLI (offline)
                _reloaded = False        # turbo déchargé le temps du repli
                # Fix « CAM++ chargé deux fois par réunion » : la libération de
                # CAM++ et le rechargement de turbo sont DIFFÉRÉS après
                # l'attribution au mot (assign_words_precise réutilise
                # l'extracteur ; le décharger avant le rechargeait aussitôt).
                # Le finally élargi garantit la libération sur TOUTES les
                # sorties (succès, frise vide, exception).
                try:
                    # v26 — VOIE PRIMAIRE : diarisation GUIDÉE PAR LA TRANSCRIPTION
                    # (DER 0,039 vs 0,35 en tuiles, + fusion-liens anti « voix
                    # inventée par l'intonation »). Les tuiles (live puis offline)
                    # deviennent des REPLIS. RAM : on RÉUTILISE le diariseur live
                    # s'il existe (même extracteur CAM++, jamais deux instances) ;
                    # turbo déchargé pendant l'extraction (relais, rechargé au
                    # finally). En cas d'échec/frise vide, l'état stream du live
                    # est INTACT -> replis inchangés.
                    # PLAFOND RAM (revue v26) : l'extraction CAM++ in-process fuit
                    # ~6-12 Mo/inférence (arène ONNX, non corrigeable in-process).
                    # Enveloppe VALIDÉE : 34 min ~ 3 Go de pic. Au-delà de
                    # GUIDED_MAX_MIN on garde les replis historiques (pas de
                    # régression RAM sur réunion longue) en attendant le fix
                    # complet (extraction en sous-process par lots, prototypé).
                    if segments and (audio_duree or 0) <= GUIDED_MAX_MIN * 60:
                        # Relais RAM : ne recharger turbo au finally QUE s'il
                        # était réellement chargé (sur MLX, model est None par
                        # design -> sinon on chargerait 1,7 Go de CT2 pour rien).
                        try:
                            _reloaded = getattr(_controller.engine, "model", None) is not None
                            _controller.engine.unload_model()
                        except Exception:
                            pass
                        # v27 — relais GPU : la transcription est finie, on rend
                        # la RAM MLX (~0,8-1,5 Go) pendant la diarisation. Rechargé
                        # paresseusement au prochain usage (dictée/réunion), comme
                        # l'idle-unloader. Jamais pendant une dictée en cours.
                        try:
                            import mlx_engine as _mlxe
                            if _mlxe.available() and not getattr(_controller, "_busy", False):
                                _mlxe.unload()
                        except Exception:
                            pass
                        _dg = None
                        _tg0 = _t.perf_counter()
                        try:
                            _dg = wt_diar if wt_diar is not None else diarizer.Diarizer()
                            try:
                                import wave as _wv
                                with _wv.open(wav_path, "rb") as _wf:
                                    _sr16 = _wf.getframerate()
                            except Exception:
                                _sr16 = SAMPLE_RATE
                            # v27 — extraction en SOUS-PROCESS (RAM principale
                            # plate ; repli in-process automatique si spawn KO).
                            spk_segs = _dg.diarize_from_segments_subproc(
                                wav_path, _sr16, segments,
                                num_speakers=n_spk_set,
                                max_speakers=MAX_AUTO_SPEAKERS)
                            if spk_segs:
                                voices_map = _dg.last_voices()
                                diar_obj = _dg
                                if _dg is not wt_diar:
                                    dia = _dg   # libéré au finally (après l'attribution)
                                print(f"[whotalks] frise GUIDÉE-TRANSCRIPTION : "
                                      f"{len(spk_segs)} segments en "
                                      f"{_t.perf_counter()-_tg0:.2f}s.")
                            else:
                                if _dg is not wt_diar:
                                    dia = _dg
                                print("[whotalks] guidé-transcription vide -> repli tuiles.")
                        except Exception as e:
                            # même transfert que la branche frise-vide : pas
                            # d'extracteur orphelin si une étape future lève.
                            if _dg is not None and _dg is not wt_diar and dia is None:
                                dia = _dg
                            print(f"[reunion] diarisation guidée KO ({e}) -> repli tuiles.")
                            spk_segs = None
                    elif segments:
                        print(f"[whotalks] réunion > {GUIDED_MAX_MIN} min -> "
                              "diarisation tuiles (plafond RAM, fix sous-process à venir).")
                    if not spk_segs and wt_diar is not None and wt_diar.stream_count >= 3:
                        n_emb = wt_diar.stream_count
                        _tl0 = _t.perf_counter()   # chrono LOCAL (td0 inclurait la tentative guidée)
                        try:
                            spk_segs = wt_diar.finalize_stream(
                                num_speakers=n_spk_set,
                                max_speakers=MAX_AUTO_SPEAKERS)
                            voices_map = wt_diar.last_voices()
                            diar_obj = wt_diar
                            print(f"[whotalks] frise EN LIGNE : {n_emb} empreintes "
                                  f"-> {len(spk_segs)} segments en "
                                  f"{_t.perf_counter()-_tl0:.2f}s.")
                        except Exception as e:
                            print(f"[whotalks] finalize_stream KO ({e}) -> repli.")
                            spk_segs = None
                        if not spk_segs:
                            # Échec / frise vide : on libère CAM++ live TOUT DE
                            # SUITE, AVANT le repli qui charge son propre
                            # extracteur (sinon deux CAM++ coexisteraient
                            # pendant dia.diarize). En cas de succès, la
                            # libération est différée au finally (après
                            # l'attribution au mot).
                            diar_obj = None
                            try:
                                wt_diar.unload()
                            except Exception:
                                pass
                    # REPLI (live absent / échoué) : diarisation complète du WAV
                    # avec relais RAM (décharge turbo le temps de scanner avec
                    # CAM++ ; turbo est rechargé dans le finally, après
                    # l'attribution au mot).
                    if not spk_segs and (audio_duree or 0) > DIAR_INPROC_MAX_MIN * 60:
                        # v3.4.0 — GARDE ANTI-OOM : le repli in-process lit tout le WAV
                        # + ~1 inférence CAM++/s (fuite ONNX ~10 Go+ à 2h). Sur une
                        # réunion longue (voie sous-process KO), on DÉGRADE proprement
                        # en « sans diarisation » plutôt que crasher. La transcription
                        # reste complète, affichée en continu.
                        print(f"[reunion] repli diarisation in-process SAUTÉ "
                              f"(> {DIAR_INPROC_MAX_MIN} min, garde RAM) -> texte continu.")
                    elif not spk_segs:
                        try:
                            # relais RAM : recharger au finally SEULEMENT si un
                            # modèle CT2 était chargé (sur MLX, model est None).
                            if not _reloaded:
                                _reloaded = getattr(_controller.engine, "model",
                                                    None) is not None
                            _controller.engine.unload_model()
                        except Exception:
                            pass
                        try:
                            # v26 — réutilise l'instance de la voie guidée si elle
                            # existe (même extracteur CAM++ ; sinon fuite RAM).
                            if dia is None:
                                dia = diarizer.Diarizer()
                            spk_segs = dia.diarize(
                                wav_path, num_speakers=n_spk_set,
                                max_speakers=MAX_AUTO_SPEAKERS,
                                on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p))
                            voices_map = dia.last_voices()
                            diar_obj = dia
                        except Exception as e:
                            print(f"[reunion] diarisation repli KO ({e}).")
                            spk_segs = None
                    if spk_segs:
                        # Fusion au MOT (timestamps mot Whisper) -> les tours courts
                        # et le va-et-vient rapide sont capturés (au lieu d'attribuer
                        # un segment entier, qui noyait les interjections).
                        units = diarizer.words_from_segments(segments)
                        # Attribution au MOT (≥98 % mesuré sur réunions réelles),
                        # replis successifs : cf. _attribute_blocks.
                        blocks = _attribute_blocks(diar_obj, units, wav_path,
                                                   spk_segs)
                        # v1.0.12 — VISIO : réétiquetage par SOURCE (micro=Toi,
                        # système=interlocuteur) au lieu de la diarisation du mix
                        # (CHECK 3). Détecté par le sidecar dominance .dom ->
                        # visio UNIQUEMENT, présentiel strictement intact.
                        _is_visio = False
                        try:
                            _domp = (wav_path or "") + ".dom"
                            if blocks and os.path.exists(_domp):
                                blocks = _visio_relabel_blocks(blocks, _domp)
                                voices_map = {}     # centroïdes du mix non fiables -> pas d'apprentissage
                                _is_visio = True
                                print(f"[visio] réétiquetage par source -> {len(blocks)} blocs")
                        except Exception as _e:
                            print(f"[visio] réétiquetage dominance ignoré : {_e}")
                        if glossary:
                            for b in blocks:
                                b["text"] = processor.apply_glossary(b["text"], glossary)
                        n_spk = len({b["speaker"] for b in blocks})
                        # WhoTalks v1.2 — RECONNAISSANCE des voix connues (SAUTÉE en
                        # visio : centroïdes du mix non fiables ; cf. _recognize_voices,
                        # log=True : seul ce chemin imprime le détail).
                        auto_names = ({"SPEAKER_00": "Toi", "SPEAKER_01": "Interlocuteur"}
                                      if _is_visio
                                      else _recognize_voices(voices_map, log=True))
                        # v1.0.23 — FIABILITÉ D'ATTRIBUTION, distincte de la
                        # confiance de TRANSCRIPTION (`avg_confidence`, qui est
                        # la moyenne des probabilités des mots Whisper). Les deux
                        # étaient confondues : une réunion dont les locuteurs
                        # étaient largement mal attribués affichait quand même
                        # « 86 % », ce qui rassurait à tort. On stocke donc le
                        # score propre à l'attribution (part du vote gagnant,
                        # pondérée par la durée) pour pouvoir le dire à l'utilisateur.
                        _attr = getattr(diar_obj, "_last_attr_conf", None)
                        speaker_blocks_json = json.dumps(
                            {"blocks": blocks, "names": auto_names,
                             "voices": voices_map,
                             "attr_conf": _attr}, ensure_ascii=False)
                        print(f"[reunion] diarisation : {n_spk} locuteur(s), "
                              f"{len(blocks)} blocs en {_t.perf_counter()-td0:.1f}s"
                              + (f", fiabilité attribution {100*_attr:.0f} %."
                                 if _attr is not None else "."))
                    else:
                        print("[reunion] diarisation vide -> affichage continu.")
                finally:
                    # Libération GARANTIE de CAM++ (live OU repli) + rechargement
                    # de turbo, sur toutes les sorties du bloc diarisation
                    # (sinon fuite RAM cumulée sur réunions successives -> OOM
                    # sur 8 Go, ou Whisper resté déchargé).
                    # INCONDITIONNEL (revue v26) : un cas limite (guidé vide +
                    # live < 3 empreintes) laissait l'extracteur du live chargé
                    # jusqu'au GC. unload() est idempotent -> toujours libérer.
                    if wt_diar is not None:
                        try:
                            wt_diar.unload()
                        except Exception:
                            pass
                    if dia is not None:
                        try:
                            dia.unload()
                        except Exception:
                            pass
                    if _reloaded:
                        try:
                            _controller.engine.load_model()
                        except Exception:
                            pass
            else:
                print("[reunion] diarisation indisponible (sherpa/modèles absents)"
                      " -> affichage continu.")
        except Exception as e:
            print(f"[reunion] diarisation ignorée ({e}) -> affichage continu.")
            speaker_blocks_json = None

    if _store and meeting_id:
        fields = {"transcription_structuree": structured, "status": "ready"}
        if conf_json is not None:
            fields["confidence_json"] = conf_json
            fields["avg_confidence"] = avg_conf
        if speaker_blocks_json is not None:
            fields["speaker_blocks_json"] = speaker_blocks_json
        _store.update_meeting(meeting_id, **fields)
        _obsidian_meeting_sync(meeting_id)   # v1.1.0 — coffre « Vlocal, ma voix »

    # Notif + refresh UI.
    mins = int(audio_duree // 60)
    secs = int(audio_duree % 60)
    duree_txt = (f"{mins} min" if mins else f"{secs} s")
    notifier.show("Réunion prête",
                  f"Transcription terminée ({duree_txt}).",
                  subtitle=None)
    _toast("Réunion prête. Cliquez dessus dans la liste pour la lire ou la "
           "copier.", kind="success")
    _ui("setMeetingStage(%s)" % json.dumps("done"))
    _refresh_lists_ui()


# --------------------------------------------------------------------------- #
# API exposée à l'interface (window.pywebview.api.*)
# --------------------------------------------------------------------------- #
class Api:
    @_api_safe(default=False)
    def start_dictation(self):
        if _controller is None:
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
        return bool(_controller.start())

    def engine_ready(self):
        """v2 — True si le moteur Whisper est chargé et prêt."""
        return _controller is not None and _engine_ready.is_set()

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
        if _controller is None:
            return ""
        return _controller.stop_and_finalize()

    @_api_safe(default=False)
    def cancel_dictation(self):
        """v2 — Annule la dictée en cours sans transcrire (touche Échap / bouton
        Annuler). Jette l'audio, instantané. True si une dictée a été annulée."""
        if _controller is None:
            return False
        cancelled = _controller.cancel()
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
        if (mode or "").upper() == "REUNION" and not REUNION_ENABLED:
            _toast("Le mode Réunion n'est pas activé. Activez-le dans "
                   "Réglages.", kind="info")
            new = _set_mode("DICTEE")
        else:
            new = _set_mode(mode)
        _refresh_menubar()
        return new

    def is_reunion_enabled(self):
        return REUNION_ENABLED

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
        if _UI_LANG == "en":
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
        if _controller is None:
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
                                det = _controller.engine.transcribe_file_batched(
                                    path, mode="dictee", batch_size=_bs,
                                    on_progress=lambda p: _ui("setFileProgress(%.2f)" % p))
                                text = det.get("text", "")
                    except Exception:
                        text = ""
                    if not text:
                        # mode="dictee" : voie dictée par fichier (kwargs identiques
                        # à l'ancien défaut "reunion" — word_timestamps est forcé à
                        # False dans engine.transcribe_file).
                        text = _controller.engine.transcribe_file(
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
        if _controller is None:
            return {"ok": False, "error": "Le moteur de transcription se prépare encore."}
        if _store is None:
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
            mid = _store.add_meeting(titre, wav_path, "transcribing")
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
        if _controller is None:
            _toast("Le moteur de transcription finit de se préparer, un "
                   "instant…", kind="info")
            return {"ok": False, "error": "Moteur en préparation."}
        if not REUNION_ENABLED:
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
                mid = _store.add_meeting(titre, wav_path, "recording") if _store else None
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
                    lt = LiveMeetingTranscriber(_controller.engine, wav_path,
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
            if _store and mid0:
                try:
                    _store.update_meeting(mid0, duree_audio_s=duree,
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
        if _store and mid:
            try:
                _store.update_meeting(
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
        except Exception as _te:
            # v3.2.8 — lancement du thread KO (épuisement ressources) : NE JAMAIS
            # laisser pipeline_busy coincé à True, sinon les Réunions sont bloquées
            # à vie ("réunion précédente en cours") jusqu'au redémarrage. On nettoie.
            _meeting_state["pipeline_busy"] = False
            print(f"[reunion] lancement pipeline KO : {_te}")
            if _store and mid:
                try:
                    _store.update_meeting(mid, status="error")
                except Exception:
                    pass
            _toast("Le traitement de la réunion n'a pas pu démarrer. Réessayez.",
                   kind="error")
            _end_activity(_meeting_state.pop("activity", None))
            return {"ok": False, "error": "Le traitement n'a pas pu démarrer."}
        return {"ok": True, "id": mid, "duree": duree}

    # ------------------------ v2 : périphériques audio ----------------------
    _LOOPBACK_KW = ["blackhole", "aggregate", "multi-output", "loopback",
                    "stereo mix", "soundflower", "vb-audio", "voicemeeter",
                    "what u hear", "wave out"]

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
                is_lb = any(k in name.lower() for k in self._LOOPBACK_KW)
                out.append({"index": i, "name": name, "loopback": is_lb})
        return out

    @_api_safe(default=False)
    def set_audio_device(self, index):
        """Mémorise le périphérique d'entrée choisi (None/-1 = défaut système)."""
        try:
            idx = int(index)
        except Exception:
            idx = -1
        _save_settings({"audio_device": (None if idx < 0 else idx)})
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
        return _store.list_meetings(limit=10) if _store else []

    def get_meeting(self, mid):
        """v12e — Retourne le détail complet d'une réunion (transcription
        brute + propre) pour la vue détaillée avec toggle.
        v20 — accès SQL ciblé : O(1) au lieu de charger 50 réunions, et les
        réunions au-delà des 50 dernières redeviennent consultables."""
        if _store is None:
            return None
        try:
            return _store.get_meeting(int(mid))
        except Exception:
            return None

    @_api_safe(default=False)
    def delete_meeting(self, mid):
        if _store:
            _store.delete_meeting(int(mid), delete_wav=True)
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
        if _controller is None or _store is None:
            return {"ok": False, "error": "Le moteur se prépare encore, un instant."}
        m = _store.get_meeting(mid)
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
            _store.update_meeting(mid, status="transcribing")
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
        if not m or _store is None:
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
                if _controller is not None:
                    _rl = getattr(_controller.engine, "model", None) is not None
                    _controller.engine.unload_model()
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
                gl = _store.glossary_pairs() if _store else []
                if gl:
                    for b in blocks:
                        b["text"] = processor.apply_glossary(b["text"], gl)
                # v1.2 — reconnaissance des voix connues (idem pipeline,
                # silencieuse ici : pas de log).
                auto_names = _recognize_voices(voices_map)
                _store.update_meeting(int(mid), speaker_blocks_json=json.dumps(
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
                        _controller.engine.load_model()
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
        if _store is None:
            return []
        items = []
        try:
            for m in _store.list_meetings(limit=limit):
                txt = (m.get("transcription_structuree")
                       or m.get("transcription_brute") or "")
                items.append({"type": "reunion", "id": m.get("id"),
                              "titre": m.get("titre") or "Réunion",
                              "apercu": (txt or "")[:140],
                              "ts": m.get("created_at") or 0})
        except Exception:
            pass
        try:
            for d in _store.list_dictations(limit=limit):
                _c = d.get("content") or ""
                items.append({"type": "dictee", "id": d.get("id"),
                              "titre": "Dictée",
                              "apercu": _c[:140],
                              "full": _c,        # v29.6 — copie directe au clic
                              "ts": d.get("created_at") or 0})
        except Exception:
            pass
        try:
            for t in _store.list_tasks(limit=limit):
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
        if _store is None:
            return 0
        n = 0
        if scope in ("dictees", "tout"):
            n += _store.clear_dictations()
        if scope in ("reunions", "tout"):
            n += _store.clear_meetings(delete_wav=True)
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
            if _store is not None and os.path.exists(_store.path):
                info["db_mo"] = round(os.path.getsize(_store.path) / 1e6, 2)
        except Exception:
            pass
        try:
            if _store is not None:
                # v20 — comptages/chemins LÉGERS : plus aucune transcription ni
                # JSON par mot matérialisés juste pour compter (RAM transitoire).
                info["n_reunions"] = _store.count_meetings()
                info["n_dictees"] = _store.count_dictations()
                # v18.5 — on somme les VRAIS fichiers audio référencés par les
                # réunions (wav_path), où qu'ils soient, plutôt qu'un dossier fixe.
                tot = 0
                n = 0
                for _mid, wp in _store.list_meeting_wavs():
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
            dest = _window.create_file_dialog(
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
        if data is None or _store is None:
            return False
        name = (name or "").strip()
        if name:
            data["names"][speaker_id] = name
        else:
            data["names"].pop(speaker_id, None)
        _store.update_meeting(int(mid), speaker_blocks_json=json.dumps(
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
        if _store is None:
            return False
        _store.update_meeting(int(mid), transcription_structuree=(text or ""))
        return True

    @_api_safe(default=False)
    def update_speaker_block_text(self, mid, block_index, text):
        """v18.1 — Édition inline du texte d'un bloc locuteur. Persiste dans
        speaker_blocks_json."""
        data = self._load_speaker_payload(mid)
        if data is None or _store is None:
            return False
        try:
            data["blocks"][int(block_index)]["text"] = (text or "").strip()
        except Exception:
            return False
        _store.update_meeting(int(mid), speaker_blocks_json=json.dumps(
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
        return _store.list_glossary() if _store else []

    @_api_safe(default=list)
    def list_glossary_suggestions(self):
        """Suggestions non encore présentes dans la base (case insensitive)."""
        if _store is None:
            return list(self.GLOSSARY_SUGGESTIONS)
        existing = {
            (e.get("incorrect") or "").strip().lower()
            for e in _store.list_glossary()
        }
        return [
            s for s in self.GLOSSARY_SUGGESTIONS
            if s["incorrect"].strip().lower() not in existing
        ]

    @_api_safe(default=None)
    def add_glossary_entry(self, incorrect, correct, notes=""):
        if _store is None:
            return None
        new_id = _store.add_glossary_entry(incorrect, correct, notes or "")
        _refresh_whisper_bias()   # v15 — réinjecte le biais lexical (hotwords)
        return new_id

    @_api_safe(default=False)
    def delete_glossary_entry(self, entry_id):
        if _store is None:
            return False
        _store.delete_glossary_entry(int(entry_id))
        _refresh_whisper_bias()
        return True

    # ------------------------ Vlocal 2 : raccourcis vocaux -------------------
    @_api_safe(default=list)
    def list_snippets(self):
        return _store.list_snippets() if _store else []

    @_api_safe(default=None)
    def add_snippet(self, trigger, expansion):
        if _store is None:
            return None
        return _store.add_snippet(trigger, expansion)

    @_api_safe(default=False)
    def delete_snippet(self, snippet_id):
        if _store is None:
            return False
        _store.delete_snippet(int(snippet_id))
        return True

    @_api_safe(default=0)
    def import_glossary_suggestions(self, entries):
        """v13 — Import en lot des suggestions cochées par l'utilisateur.
        entries : liste de dicts {incorrect, correct, notes?}."""
        if _store is None or not entries:
            return 0
        n = 0
        for e in entries:
            try:
                if _store.add_glossary_entry(
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
        global _UI_LANG
        _UI_LANG = "en" if str(lang).lower().startswith("en") else "fr"
        try:
            overlay.set_lang(_UI_LANG)
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
        d = {"version": APP_VERSION, "lang": _UI_LANG}
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
            "Vlocal " + str(d.get("version") or APP_VERSION),
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
                   "app_version": APP_VERSION, "os_version": os_ver}
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
        s.setdefault("reunion_enabled", REUNION_ENABLED)
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
        return s

    def save_settings(self, patch):
        new = _save_settings(patch or {})
        # v1.1.0 — partage activé depuis les Réglages : premier envoi tout de suite.
        if (patch or {}).get("telemetry_enabled") is True and _telemetry is not None:
            threading.Thread(target=_telemetry.sync_now, name="telemetry-now",
                             daemon=True).start()
        # Application en chaud quand possible
        try:
            if "reunion_enabled" in patch:
                global REUNION_ENABLED
                REUNION_ENABLED = bool(patch["reunion_enabled"])
                # Synchroniser l'UI principale si ouverte
                _ui("if(typeof _applyReunionFlag==='function')"
                    "_applyReunionFlag(%s);" %
                    ("true" if REUNION_ENABLED else "false"))
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
            dest = _window.create_file_dialog(
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
    # ------------------- v1.1.0 : identité et télémétrie déclarée -------------
    def telemetry_state(self):
        """Ce que l'utilisateur a choisi de partager, et les compteurs locaux.
        `enabled` vaut None tant qu'aucun choix n'a été fait (l'UI le demande)."""
        try:
            s = _load_settings()
            totals = _store.usage_totals() if _store is not None else {}
            return {"enabled": s.get("telemetry_enabled"),
                    "first_name": s.get("first_name") or "",
                    "last_name": s.get("last_name") or "",
                    "install_id": s.get("install_id") or "",
                    "last_sync": float(s.get("telemetry_last_sync") or 0),
                    "totals": totals}
        except Exception:
            return {"enabled": None, "first_name": "", "last_name": "",
                    "install_id": "", "last_sync": 0, "totals": {}}

    def save_identity(self, first_name="", last_name="", enabled=True):
        """Enregistre prénom, nom et le choix de partage. Si le partage est
        activé, un envoi part tout de suite (thread, jamais bloquant)."""
        patch = {"first_name": (first_name or "").strip()[:80],
                 "last_name": (last_name or "").strip()[:80],
                 "telemetry_enabled": bool(enabled)}
        try:
            _save_settings(patch)
        except Exception:
            return {"ok": False}
        if patch["telemetry_enabled"] and _telemetry is not None:
            threading.Thread(target=_telemetry.sync_now, name="telemetry-now",
                             daemon=True).start()
        return {"ok": True}

    def check_updates(self):
        """v1.0.3 — Y a-t-il une version plus récente ? + éventuel message d'annonce
        (broadcast admin) via get-latest-version. Fail-open total : hors-ligne ->
        offline=True (on n'affirme PAS « à jour »), aucune donnée utilisateur envoyée
        (simple GET). C'est ce qui rend la base installée « rattrapable »."""
        import urllib.request as _u
        cur = APP_VERSION
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
            subprocess.Popen(["/bin/sh", "-c",
                              "sleep 2; /usr/bin/open -n " + shlex.quote(path)])
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
        return _store.list_tasks(limit=50) if _store else []

    @_api_safe(default=False)
    def rename_task(self, task_id, new_title):
        """v9 TOP-5 #4 — édition inline du titre d'une tâche."""
        if _store is None or not new_title:
            return False
        ok = _store.rename_task(int(task_id), str(new_title).strip()[:200])
        _refresh_menubar()
        return ok

    @_api_safe(default=None)
    def toggle_task(self, task_id):
        if _store is None:
            return None
        new = _store.toggle_task(int(task_id))
        # v20 — tâche cochée -> son timer ne sonnera pas ; re-décochée avant
        # l'échéance -> ré-armement.
        try:
            if new == "done":
                _cancel_reminder_timer(int(task_id))
            elif new == "open":
                for t in _store.list_tasks(limit=500, only_open=True):
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
        if _store is None:
            return False
        _store.delete_task(int(task_id))
        _cancel_reminder_timer(int(task_id))   # v20 — ne sonne plus après suppression
        _refresh_menubar()
        return True

    def hide_window(self):
        """v7 — masque la fenêtre sans tuer le process. L'app reste vivante via
        le menu bar (clic 'Ouvrir Vlocal' pour la rouvrir)."""
        global _window_visible
        try:
            if _window is not None:
                _window.hide()
                _window_visible = False
        except Exception:
            pass
        return True


# v13.2 — Ouverture robuste du modal Réglages.
# Bug terrain v13.1 : clic menu bar "Réglages..." et Cmd+, : rien à l'écran.
# Cause : evaluate_js sur fenêtre cachée = no-op silencieux (pywebview ne
# raise pas), et selon le chemin d'appel l'activation Cocoa pouvait être
# faite hors du main thread.
#
# Solution : un seul point d'entrée robuste, qui :
#   1) Dispatche show() + activateIgnoringOtherApps_ sur main thread Cocoa
#   2) Retente l'injection JS à 3 instants (50/180/420 ms) — la 1ʳᵉ est
#      synchrone (au cas où le DOM est prêt), les 2 autres laissent à
#      WKWebView le temps de rendre la fenêtre visible et le pywebview
#      bridge le temps de se reconnecter
#   3) Loggue chaque étape pour qu'on voie noir sur blanc ce qui se passe
def _open_settings_modal_robust():
    print("[settings] ouverture demandée")

    def _cocoa_show():
        try:
            if _window is not None:
                _window.show()
            from AppKit import NSApplication
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception as e:
            print(f"[settings] show/activate : {e}")

    if sys.platform == "darwin":
        try:
            from Foundation import NSOperationQueue
            NSOperationQueue.mainQueue().addOperationWithBlock_(_cocoa_show)
        except Exception:
            _cocoa_show()
    else:
        try:
            if _window is not None:
                _window.show()
        except Exception:
            pass

    def _inject_later():
        import time as _t
        for delay in (0.05, 0.18, 0.42):
            _t.sleep(delay)
            if _window is None:
                print("[settings] _window None, abandon")
                return
            try:
                # On vérifie que la fonction existe AVANT de l'appeler.
                # evaluate_js synchrone retourne la valeur — None si le JS
                # n'a pas tourné. typeof renvoie 'function' si tout va bien.
                kind = _window.evaluate_js(
                    "typeof openSettingsModal"
                )
                print(f"[settings] tentative t+{delay}s : "
                      f"typeof openSettingsModal = {kind!r}")
                if kind == "function":
                    _window.evaluate_js("openSettingsModal();")
                    print(f"[settings] OK : modal ouvert (t+{delay}s)")
                    return
            except Exception as e:
                print(f"[settings] evaluate_js exception (t+{delay}s) : {e}")
        print("[settings] ÉCHEC FINAL : impossible d'ouvrir le modal "
              "après 3 tentatives.")

    threading.Thread(target=_inject_later, daemon=True).start()


# v2 — L'ancienne 2e fenêtre Réglages (webview) a été supprimée : les Réglages
# sont un modal HTML dans la fenêtre principale (pywebview ne crée pas de 2e
# fenêtre fiable après start()).


# --------------------------------------------------------------------------- #
# Raccourci clavier GLOBAL : Ctrl + Espace, push-to-talk.
# Maintien = enregistre (+ aperçu live) ; relâche = final + copie presse-papier.
# --------------------------------------------------------------------------- #
# v19 — Minuterie de fermeture de l'overlay (résultat -> fade -> orderOut).
# Annulable si une nouvelle dictée démarre pendant l'affichage du résultat.
_overlay_hide_timers = None


def _cancel_overlay_hide():
    global _overlay_hide_timers
    if _overlay_hide_timers:
        for t in _overlay_hide_timers:
            try:
                t.cancel()
            except Exception:
                pass
    _overlay_hide_timers = None


def _schedule_overlay_hide(delay: float):
    """Fade-out à delay-0.3 s, puis masque réellement le panneau à delay s."""
    global _overlay_hide_timers
    _cancel_overlay_hide()

    def _fade():
        overlay.fade_out()

    def _done():
        # v29.2 — masquage DUR (alpha fenêtre 0) en plus du reset de contenu :
        # garantit que l'overlay disparaît même si une trame JS résiduelle/
        # interrompue laissait la carte semi-visible. Pas d'orderOut (le panneau
        # reste prêt ; show() le ré-opacifie).
        overlay.hide()
    t1 = threading.Timer(max(0.0, delay - 0.3), _fade)
    t2 = threading.Timer(max(0.05, delay), _done)
    for t in (t1, t2):
        t.daemon = True
        t.start()
    _overlay_hide_timers = (t1, t2)


def start_global_hotkey():
    """Démarre le raccourci global push-to-talk.

    Mécanisme : moniteurs NSEvent (hotkey_mac — moniteur GLOBAL + moniteur
    local), AUCUNE interception : les événements ne sont JAMAIS consommés.
    Limitation connue : avec ctrl_space et plusieurs sources de saisie
    actives, macOS peut traiter la combo lui-même (bip / bascule de source
    de saisie) — le moniteur n'intercepte rien.

    Permission requise : ACCESSIBILITÉ (pour le moniteur GLOBAL). Sans elle,
    aucun événement clavier ne remonte hors focus : on guide l'utilisateur au
    démarrage (cf. _check_hotkey_perm).

    Raccourci lu depuis les Réglages (clé 'hotkey'), override via env VLOCAL_HOTKEY.
    Valeurs : ctrl_space (défaut) | ctrl_cmd (chord de modificateurs) |
    ctrl_alt_space | ctrl_cmd_space | cmd_shift_x | none.
    """
    global _hotkey_label
    hotkey_name = (os.environ.get("VLOCAL_HOTKEY")
                   or _load_settings().get("hotkey")
                   or "ctrl_cmd").lower()   # v20 — même défaut que l'UI/la table
    if hotkey_name == "none":
        _hotkey_label = ""   # raccourci désactivé -> pas de libellé (toast/UI)
        print("[hotkey] désactivé via VLOCAL_HOTKEY=none — utilise le bouton Dicter.")
        # Pas de raccourci : pastille masquée + hint adapté côté UI.
        _ui("if(typeof setHotkeyLabel==='function')setHotkeyLabel(%s,%s)"
            % (json.dumps(""), json.dumps("Clique Dicter pour dicter")))
        return None

    # Configs : nom -> (modificateurs requis, vk de la touche déclencheur,
    # libellé, badge symboles pour l'UI). vk = virtual keycode macOS
    # (Espace=49, X=7).
    #
    # CHORD DE MODIFICATEURS (trigger_vk = None) : pas de touche déclencheur, on
    # démarre quand TOUS les modificateurs sont maintenus et on arrête au premier
    # relâché (ex. ctrl_cmd = maintenir Ctrl+Cmd). Aucun bip (les modificateurs
    # seuls ne déclenchent aucune action système) et aucune interception (on ne
    # consomme JAMAIS Ctrl/Cmd, sinon on casserait tout le clavier).
    HOTKEYS = {
        "ctrl_cmd":       ({"ctrl", "cmd"}, None, "Ctrl + Cmd", "⌃ ⌘"),
        "ctrl_space":     ({"ctrl"},        49, "Ctrl + Espace", "⌃ ␣"),
        "ctrl_alt_space": ({"ctrl", "alt"}, 49, "Ctrl + Option + Espace", "⌃ ⌥ ␣"),
        "ctrl_cmd_space": ({"ctrl", "cmd"}, 49, "Ctrl + Cmd + Espace", "⌃ ⌘ ␣"),
        "cmd_shift_x":    ({"cmd", "shift"}, 7, "Cmd + Maj + X", "⌘ ⇧ X"),
    }
    required, trigger_vk, hotkey_label, hotkey_badge = \
        HOTKEYS.get(hotkey_name, HOTKEYS["ctrl_cmd"])
    _hotkey_label = hotkey_label
    # Pousse le libellé RÉEL du raccourci à l'UI (la pastille « ⌃ ⌘ » statique
    # du markup n'est qu'un défaut d'affichage). Best-effort, garde typeof.
    _ui("if(typeof setHotkeyLabel==='function')setHotkeyLabel(%s,%s)"
        % (json.dumps(hotkey_badge),
           json.dumps("Maintiens %s (%s) ou clique Dicter"
                      % (hotkey_badge, hotkey_label))))
    # Même libellé dans le pied de l'overlay (« Relâchez … pour terminer »),
    # faux en dur pour 5 raccourcis sur 6 avant v20.
    try:
        overlay.set_hotkey_label(hotkey_label)
    except Exception:
        pass

    def begin():
        # Nouvelle dictée -> annule un éventuel fade-out de résultat en cours.
        _cancel_overlay_hide()
        # NB : on NE pré-bloque PAS sur le statut micro. Le statut TCC peut être
        # "indéterminé" (binaire fraîchement signé) sans être "refusé" : laisser
        # l'ouverture du flux audio déclencher le PROMPT MICRO NATIF de macOS.
        # Le micro réellement refusé est géré en aval (aucun son -> message clair).
        print(f"[hotkey] BEGIN ({hotkey_label})")
        if not _controller:
            # DICT-02 — moteur encore en chargement : feedback au lieu du silence.
            overlay.info(_te("Vlocal démarre"), _te("Le moteur se charge, réessayez dans un instant."))
            _schedule_overlay_hide(2.0)
            return
        # v3.2.8 — micro EXPLICITEMENT refusé (status 2, PAS "indéterminé") : message
        # clair immédiat + on n'enregistre pas du silence (un tap court tombait sinon
        # sur « trop court » et l'utilisateur ne comprenait pas). Le cas "indéterminé"
        # (status 0, binaire fraîchement signé) passe au prompt micro NATIF, inchangé.
        try:
            if permissions.mic_status() == 2:
                overlay.mic_needed()
                _schedule_overlay_hide(5.0)
                return
        except Exception:
            pass
        # v1.0.11 — ZÉRO LATENCE PERÇUE : l'overlay s'affiche IMMÉDIATEMENT, AVANT
        # l'ouverture du micro (qui peut prendre un instant au 1er appui, en
        # Bluetooth, ou quand le device bascule). Avant, il n'apparaissait qu'APRÈS
        # le retour de start() -> délai visible à l'appui. La pastille est désormais
        # instantanée ; le micro s'ouvre derrière, et on bascule l'overlay seulement
        # si ça échoue (micro indispo) ou si c'est refusé (occupé).
        overlay.recording()             # overlay État 1 (waveform live) — INSTANTANÉ
        try:
            started = bool(_controller.start())
        except Exception as e:
            # DICT-04 — micro indisponible / périphérique monopolisé : on prévient.
            print(f"[hotkey] micro indisponible : {e}")
            overlay.mic_needed()
            _schedule_overlay_hide(4.0)
            return
        if not started:
            # v1.0.22 — le refus « transcription précédente en cours » n'existe
            # plus (dictée enchaînée) : si start() rend False, c'est que le micro
            # n'a pas pu s'ouvrir dans le budget de 2 s. Il est en cours de remise
            # à neuf en fond -> on dit à l'utilisateur la seule chose utile.
            overlay.info(_te("Un instant"), _te("Micro en cours de remise à neuf, réessayez."))
            _schedule_overlay_hide(1.5)

    def end():
        if not _controller:
            return
        if not _controller.recording_active():
            # DICT-01 — anti-race : le worker begin a peut-être posé _active
            # juste après. On re-teste sur une courte fenêtre bornée avant
            # d'abandonner (jamais de dictée fantôme micro-ouvert).
            deadline = time.time() + 0.5
            while time.time() < deadline:
                time.sleep(0.02)
                if _controller.recording_active():
                    break
            else:
                return
        rec_dur = _controller.recording_duration()
        print(f"[hotkey] END dur={rec_dur:.2f}s")
        # ÉTAPE 4.4 — capture trop courte : pas d'appel Whisper (hallucinations).
        if rec_dur < 0.3:
            # v1.0.22 — cancel() ferme le micro (join worker 2 s + stop borné) :
            # exécuté ici en synchrone, il RETENAIT LE THREAD PUMP du raccourci,
            # donc retardait l'appui suivant de plusieurs secondes sur un simple
            # tap court. En thread daemon, comme _finalize.
            threading.Thread(target=_controller.cancel, name="tap-cancel",
                             daemon=True).start()
            _ui("dictationCancelled()")
            overlay.too_short()
            _schedule_overlay_hide(2.0)
            return
        # Overlay État 2 (transcription) AVANT le travail bloquant.
        overlay.transcribing(rec_dur)

        # stop_and_finalize() est BLOQUANT (Whisper). On le sort du callback du
        # moniteur NSEvent (sinon il gèle) -> overlay réactif + relâchement non
        # bloqué.
        def _finalize():
            t_rel = time.time()
            my_gen = getattr(_controller, "_gen", 0)   # CONC-5
            # v1.0.22 — chaînage : la VALIDITÉ D'INSERTION ne dépend plus de la
            # génération (une dictée enchaînée qui démarre pendant cette
            # finalisation est désormais NORMALE et ne doit pas faire perdre ce
            # texte) mais de l'epoch, bumpé UNIQUEMENT par force_reset.
            my_epoch = getattr(_controller, "_epoch", 0)

            def _own():
                # True tant que cette dictée reste la plus récente (sinon une
                # nouvelle dictée a démarré -> on ne touche plus l'overlay).
                return my_gen == getattr(_controller, "_gen", my_gen)

            # INS-1 — on capture le presse-papier ORIGINAL AVANT stop_and_finalize
            # (qui va y copier le texte dicté) -> restauration correcte ensuite.
            try:
                import clipboard as _clip
                _orig_clip = _clip.get_clipboard()
            except Exception:
                _orig_clip = None
            try:
                # notify_ui=False : la fenêtre principale ne réagit pas (overlay seul).
                text = _controller.stop_and_finalize(notify_ui=False)
            except Exception as e:
                print(f"[hotkey]  transcription KO : {e}")
                if _own():
                    overlay.error(_te("Réessayez."))
                    _schedule_overlay_hide(4.0)
                return
            if not text:
                # Silence / micro muet : stop_and_finalize a déjà toasté le détail.
                if _own():
                    overlay.error(_te("Aucun son capté. Réessayez."))
                    _schedule_overlay_hide(4.0)
                return
            # CONC-5 / v29.2 / v1.0.22 — GARDE-FOU D'INSERTION, réécrit pour le
            # chaînage : une dictée plus récente qui a démarré pendant cette
            # finalisation est désormais le cas NORMAL (l'utilisateur enchaîne)
            # -> on INSÈRE quand même, dans l'ordre FIFO (avant : le texte était
            # jeté au presse-papier = « ça se perd » en usage intensif). Seul un
            # force_reset (epoch bumpé : figeage récupéré par le superviseur)
            # invalide ce texte — c'était le vrai cas visé par l'anti-pollution.
            if getattr(_controller, "_epoch", my_epoch) != my_epoch:
                print("[hotkey] finalisation invalidée par un reset -> "
                      "insertion au curseur IGNORÉE (anti-pollution). "
                      "Texte au presse-papier.")
                return
            # INSERTION AU CURSEUR (façon Wispr) ; repli presse-papier garanti.
            # Le raccourci global EST de la dictée (jamais réunion) : on insère
            # toujours, quel que soit le mode affiché dans la fenêtre.
            inserted = False
            _insert_guided = False   # une invite d'échec est-elle affichée via l'overlay ?
            try:
                if _load_settings().get("insert_at_cursor", True):
                    import inserter
                    # v1.0.22 — chaînage : collages sérialisés (jamais entrelacés).
                    with _insert_serial_lock:
                        inserted = bool(inserter.insert_at_cursor(text, original=_orig_clip))
                    if not inserted:
                        # v1.0.6 — OBSERVABILITÉ (tour de contrôle) : on compte les
                        # échecs d'insertion, friction n°1 connue (Accessibilité non
                        # accordée). Métadonnée seule (jamais le texte). Permet de
                        # voir combien de clients sont coincés AVANT 100 messages.
                        if _EV:
                            _EV.log(_EV.E.INSERT_FAIL,
                                    acc=bool(permissions.accessibility_ok()))
                        # Le _toast part dans la fenêtre PRINCIPALE, souvent CACHÉE
                        # en mode raccourci -> invisible. On guide donc via l'OVERLAY
                        # (sa propre fenêtre, toujours présente). On VÉRIFIE d'abord
                        # que le texte est réellement au presse-papier (sinon le
                        # message « copié » mentirait).
                        import clipboard as _ck
                        try:
                            copied_ok = (_ck.get_clipboard() == text)
                        except Exception:
                            copied_ok = True
                        # v1.0.22 — chaînage : ne toucher l'overlay QUE si cette
                        # dictée est toujours la plus récente (sinon on écraserait
                        # la wave de la dictée suivante en cours d'enregistrement).
                        if _own():
                            if not copied_ok:
                                overlay.error(_te("Échec d'insertion, réessayez."))
                            elif not permissions.accessibility_ok():
                                overlay.info(_te("Insertion à autoriser"),
                                             _te("Texte copié : Cmd + V. Autorisez Vlocal dans "
                                                 "Réglages > Confidentialité et sécurité > Accessibilité, "
                                                 "puis relancez Vlocal."))
                                try:
                                    permissions.open_settings("accessibility")
                                except Exception:
                                    pass
                            else:
                                overlay.info(_te("Texte copié"), _te("Collez avec Cmd + V."))
                            _insert_guided = True
                        _toast("Texte copié dans le presse-papier, collez avec "
                               "Cmd + V. Pour l'insertion automatique au curseur, "
                               "autorisez Vlocal dans Réglages Système > "
                               "Confidentialité et sécurité > Accessibilité.",
                               kind="info")
            except Exception as e:
                print(f"[insert]  insertion au curseur KO : {e}")
            # Overlay État 3 : chrono de transcription + branche cascade + action.
            # Si une invite d'échec est déjà à l'écran (canal fiable), on NE
            # l'écrase PAS avec le résultat -> on prolonge juste l'affichage.
            if _own():
                if _insert_guided:
                    _schedule_overlay_hide(9.0)   # laisser lire l'invite
                else:
                    trans_dur = time.time() - t_rel
                    branch = getattr(_controller.engine, "last_model", None)
                    overlay.result(trans_dur, inserted, branch, text)   # texte fidèle à l'app
                    _schedule_overlay_hide(3.0)
        threading.Thread(target=_finalize, daemon=True).start()

    # FIABILITÉ : begin/end encapsulés -> aucune exception ne se propage au tap.
    def _begin_safe():
        try:
            begin()
        except Exception as e:
            print(f"[hotkey] begin KO (ignoré) : {e}")

    def _end_safe():
        try:
            end()
        except Exception as e:
            print(f"[hotkey] end KO (ignoré) : {e}")

    # Raccourci global via moniteurs NSEvent natifs (hotkey_mac), PLUS de
    # pynput : pynput crashait l'app (résolution TSM du caractère hors main
    # thread -> SIGTRAP). Les moniteurs ne lisent que les modificateurs /
    # keycodes -> aucun crash.
    import hotkey_mac
    try:
        print(f"[hotkey] '{hotkey_name}' ({hotkey_label}) | "
              f"accessibility_ok={permissions.accessibility_ok()} "
              f"mic_ok={permissions.mic_ok()}")
    except Exception:
        pass
    # max_seconds = garde-fou anti-runaway SI le relâchement est perdu, PAS un
    # plafond de dictée. 600 s (10 min) : on n'interrompt plus une vraie dictée
    # longue, tout en restant protégé contre une touche bloquée.
    t = hotkey_mac.start(_begin_safe, _end_safe,
                         mods=tuple(required), trigger_vk=trigger_vk,
                         max_seconds=600.0)
    if t is None:
        print("[hotkey] raccourci inactif — accorde l'Accessibilité à Vlocal, "
              "ou utilise le bouton Dicter.")
    return t


# --------------------------------------------------------------------------- #
def selftest():
    """Validation sans fenêtre ni micro :
      1) preuve du fix des espaces (segments synthétiques SANS espace de tête)
      2) transcription FINALE multi-phrases (espaces/ponctuation corrects)
      3) mesure latence aperçu (slice) vs final, sur l'audio `say`.
    """
    import wave

    import numpy as np

    print("Vlocal — auto-test v2 (sans fenêtre, sans micro)")
    print()

    # 1) Preuve déterministe du fix des espaces (cas qui causait "motscollés")
    glued = ["bonjour", "salut", "comment ça va", ", c'est un test."]
    fixed = assemble_segments(glued)
    print("1) assemble_segments(sans espaces) ->", repr(fixed))
    assert fixed == "bonjour salut comment ça va, c'est un test.", fixed
    print("   [ok] espaces ajoutés + ponctuation collée correctement")
    print()

    # Audio multi-phrases. v16 — écrit dans un dossier TEMP (jamais dans
    # BASE_DIR : en .app empaqueté ce serait dans le bundle, ce qui casse la
    # signature de code).
    import tempfile as _tf
    audio_file = os.path.join(_tf.gettempdir(), "vlocal_test_multi.wav")
    if not os.path.exists(audio_file):
        subprocess.run(
            ["say", "-o", audio_file, "--data-format=LEI16@16000",
             "Bonjour. Comment allez-vous aujourd'hui ? Ceci est un test de dictée vocale, cent pour cent locale. Merci beaucoup."],
            check=True,
        )

    eng = VlocalEngine(model_dir=MODEL_DIR)
    with wave.open(audio_file, "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    dur = len(audio) / SAMPLE_RATE

    # 2) FINAL
    t0 = time.perf_counter()
    final = eng.transcribe(audio, vad=True)
    dt_final = time.perf_counter() - t0
    print(f"2) FINAL ({dur:.1f}s audio, vad=on) en {dt_final:.2f}s :")
    print("   ", repr(final))
    ok = "dictée" in final.lower() and "locale" in final.lower() and "  " not in final
    print("   [ok] texte propre" if ok else "   [ko] problème d'espaces/contenu")
    print(f"   Mesure (cette machine) : transcription finale ≈ {dt_final:.2f}s")
    return 0 if ok else 1


# v14 — Vérifications modèle au démarrage (différées, hors chemin critique).
def _rearm_pending_reminders():
    """v15 — Au démarrage, re-planifie les notifs des rappels à échéance future.

    Les threading.Timer ne survivent PAS à un redémarrage de l'app. Sans ce
    ré-armement, un rappel créé hier pour aujourd'hui 14h ne se déclencherait
    jamais si l'app a été relancée entretemps. On relit la base et on re-arme
    tout rappel ouvert dont l'échéance est encore dans le futur."""
    if _store is None:
        return
    rearmed = 0
    now = time.time()
    try:
        for t in _store.list_tasks(limit=200, only_open=True):
            iso = t.get("echeance_iso")
            ts = notifier.parse_iso(iso) if iso else None
            if ts and ts > now:
                _schedule_reminder(ts, (t.get("titre") or "")[:120],
                                   task_id=t.get("id"))
                rearmed += 1
    except Exception as e:
        print(f"[rappel]  ré-armement KO : {e}")
        return
    if rearmed:
        print(f"[rappel]  {rearmed} rappel(s) re-planifié(s) au démarrage.")


def _onboarding_done() -> bool:
    """v1.1.0 : l'écran de premier lancement a été terminé (réglage `onboarded`).
    Sert à différer les messages et demandes de permission tant qu'il est affiché.
    En cas de doute, True : ne jamais retenir un utilisateur."""
    try:
        return bool(_load_settings().get("onboarded"))
    except Exception:
        return True


def _post_startup_model_checks():
    """v15 — Tourne ~1,2 s après le démarrage : message de bienvenue au tout
    premier lancement. (Aucun appel réseau : Vlocal ne contacte rien, même pas
    en localhost — l'ancien sondage Ollama a été retiré, le SLM n'existe plus.)
    v1.0.3 — repoussé tant que l'app n'est pas activée : sinon le toast s'affiche
    DERRIÈRE l'écran d'activation (invisible) et welcome_shown passerait quand
    même à True, donc le message ne serait jamais revu après activation."""
    try:
        if not _onboarding_done() or model_store.needs_download():
            threading.Timer(2.0, _post_startup_model_checks).start()
            return
        s = _load_settings()
        if not s.get("welcome_shown"):
            # v1.0 — utilise le libellé RÉEL du raccourci (défaut Ctrl + Cmd),
            # plus le « Ctrl + Espace » codé en dur qui était faux.
            _hk = (_hotkey_label or "").strip()
            if _UI_LANG == "en":
                _how = ('click "Dictate" (or hold %s)' % _hk) if _hk else 'click "Dictate"'
                _toast('Welcome to Vlocal. Choose a mode above, then %s and speak. '
                       'Everything stays on your Mac, nothing is sent online.'
                       % _how, kind="info")
            else:
                _how = ("cliquez sur « Dicter » (ou maintenez %s)" % _hk) if _hk \
                    else "cliquez sur « Dicter »"
                _toast("Bienvenue dans Vlocal. Choisissez un mode ci-dessus, puis "
                       "%s et parlez. Tout reste sur votre Mac, rien n'est "
                       "envoyé en ligne." % _how, kind="info")
            _save_settings({"welcome_shown": True})
    except Exception:
        pass


# v2 — Paramètres + chargement DIFFÉRÉ du moteur Whisper (démarrage instantané).
_engine_load_params = None
_cpu_threads_resolved = 4   # v3.3 — threads CPU retenus en main(), réutilisés après un download thin
_dl_in_flight = False       # v3.3 (N2) — garde anti-double téléchargement modèles
_telemetry = None           # v1.1.0 — planificateur de télémétrie (telemetry.Telemetry)
_api_instance = None        # v3.3 — handle de l'instance Api (re-vérif licence en arrière-plan)
_engine_ready = threading.Event()


# Phase 2 (RAM) — DÉCHARGEMENT À L'INACTIVITÉ : Whisper turbo (~1,6 Go) est le
# poids lourd. On le libère après quelques minutes sans usage (RAM au repos
# ~0,2 Go au lieu de ~1,6) ; la prochaine dictée/transcription le recharge tout
# seul (chargement paresseux). Décisif pour tenir sur 8 Go sans swap.
# 90 s : audit RAM v18 — le rechargement mesuré coûte 0,74 s (sub-seconde), donc
# libérer turbo plus tôt est sans coût UX perceptible. Ne pas descendre sous ~60 s.
_IDLE_UNLOAD_S = 60        # turbo : plancher quand la RAM est SERRÉE (8 Go chargé)
_IDLE_UNLOAD_FAST_S = 45   # small : délai de grâce avant déchargement séparé


def _total_ram_gb():
    """v22.3 — RAM PHYSIQUE totale (Go). On NE se fie PAS à la mémoire « dispo »
    de psutil : macOS la sur-estime (compression mémoire) -> on choisissait
    batch 4 sur une machine pourtant chargée, d'où swap/ventilateurs. La RAM
    physique est la borne dure de la cible « 8 Go avec d'autres apps »."""
    try:
        import subprocess as _sp
        out = _sp.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                      text=True, timeout=2).stdout.strip()
        return int(out) / 2 ** 30
    except Exception:
        try:
            import psutil as _ps
            return _ps.virtual_memory().total / 2 ** 30
        except Exception:
            return 8.0   # hypothèse PRUDENTE (cible basse) si indéterminé


def _idle_thresholds():
    """v21 — Délais d'éco-RAM ADAPTATIFS à la mémoire DISPONIBLE : décharger
    turbo après 60 s n'a de sens que quand la RAM manque. Sur une machine à
    l'aise, ça créait LA variance de latence (rechargement + dictée suivante
    plus lente, « la 1re dictée est toujours plus lente »). RAM abondante ->
    on garde les modèles chauds bien plus longtemps ; RAM serrée (la cible
    8 Go avec Chrome/Slack) -> comportement frugal inchangé."""
    try:
        import psutil as _ps
        avail_gb = _ps.virtual_memory().available / 2 ** 30
    except Exception:
        avail_gb = 0.0
    if avail_gb >= 8.0:
        return 900, 900       # 15 min : machine à l'aise, latence stable prime
    if avail_gb >= 4.0:
        return 240, 180
    return _IDLE_UNLOAD_S, _IDLE_UNLOAD_FAST_S


def _start_idle_unloader():
    import time as _t

    def _check():
        try:
            eng = _controller.engine if _controller is not None else None
            if eng is not None:
                busy = (_meeting_state.get("recording")
                        or _meeting_state.get("pipeline_busy")
                        or _meeting_state.get("file_busy")
                        or _controller.recording_active())
                idle_turbo_s, idle_fast_s = _idle_thresholds()
                # turbo (le poids lourd ~1,6 Go)
                if getattr(eng, "model", None) is not None and not busy:
                    idle = _t.time() - getattr(eng, "last_use", 0)
                    if idle > idle_turbo_s:
                        eng.unload_model()
                        print(f"[whisper] turbo déchargé après {idle:.0f}s "
                              f"d'inactivité (rechargement auto au prochain usage).")
                # small (~480 Mo) : déchargé SÉPARÉMENT après un délai de grâce
                # (évite de re-payer le cold-start sur des dictées rapprochées).
                if getattr(eng, "_fast_model", None) is not None and not busy:
                    idle_f = _t.time() - getattr(eng, "_fast_last_use", 0)
                    if idle_f > idle_fast_s:
                        eng.unload_fast_model()
                # v23 — modèle GPU MLX (~1,5 Go) : MÊME logique d'idle-unload.
                # Restaure la « redescente de RAM » à l'arrêt sur la cible 8 Go
                # (sinon le modèle GPU restait résident à vie). Rechargé
                # paresseusement (cold ~1,2 s, masqué par le préchauffe).
                try:
                    import mlx_engine as _mlx
                    # v23.1 — seuil GPU DÉDIÉ, bien plus court que le turbo CPU :
                    # le rechargement GPU (~1 s) est MASQUÉ par le prewarm au
                    # début de la parole (engine.py), donc décharger vite ne coûte
                    # aucune latence perceptible, mais rend ~1,5 Go à l'OS. La
                    # « redescente de RAM » devient quasi immédiate après l'arrêt.
                    try:
                        import psutil as _ps2
                        _avail_gb = _ps2.virtual_memory().available / 2 ** 30
                    except Exception:
                        _avail_gb = 0.0
                    # v29.1 — seuils RELEVÉS pour casser le CHURN Metal : décharger
                    # /recharger le modèle GPU à ~25 s entre des dictées rapprochées
                    # multipliait les transitions Metal et finissait par FIGER une
                    # inférence (ralentissement progressif puis « dictée infinie »
                    # sous usage intensif). On garde le modèle chaud tant que les
                    # dictées s'enchaînent ; la RAM n'est rendue qu'après une vraie
                    # pause. (mlx_engine borne en plus toute inférence figée à 60 s.)
                    # v1.0.6 — seuils RELEVÉS (compromis ~2 min validé) : casse encore
                    # plus le churn Metal, cause prouvée du gel de finalisation sous usage
                    # intensif (logs finalize_timeout : décharge à ~60 s entre rafales de
                    # dictées -> rechargements GPU répétés -> command buffer figé). On garde
                    # le GPU chaud ~2 min ; la RAM n'est rendue qu'après une vraie pause.
                    if _avail_gb >= 8.0:
                        _mlx_idle_s = 150
                    elif _avail_gb >= 4.0:
                        _mlx_idle_s = 120       # ~2 min (était 60 s) — réduit fortement le churn
                    else:
                        _mlx_idle_s = 60        # RAM serrée : churn réduit, RAM rendue après 1 min
                    _idle_mlx = _mlx.idle_seconds()
                    if not busy and _mlx.loaded() and _idle_mlx > _mlx_idle_s:
                        _mlx.unload()
                        print(f"[mlx] modèle GPU déchargé après "
                              f"{_idle_mlx:.0f}s d'inactivité (RAM rendue).")
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            t = threading.Timer(12.0, _check)   # v23.1.1 — poll 12 s : redescente RAM rapide
            t.daemon = True
            t.start()
    t = threading.Timer(60.0, _check)
    t.daemon = True
    t.start()


def _finish_model_download():
    """v3.3 — après le téléchargement R2 des modèles : recalcule les chemins puis
    lance le moteur Whisper en tâche de fond (comme au démarrage normal)."""
    global _engine_load_params
    _resolve_model_dirs()
    _point_mlx_to_cache()
    _wd = None
    if os.path.exists(os.path.join(MODEL_DIR, "model.bin")):
        _wd = MODEL_DIR
    elif os.path.exists(os.path.join(SMALL_MODEL_DIR, "model.bin")):
        _wd = SMALL_MODEL_DIR
    if _wd is None:
        print("[models] post-download : modèle introuvable malgré le téléchargement.")
        return False
    _engine_load_params = (_wd, _cpu_threads_resolved)
    threading.Thread(target=_load_engine_async, daemon=True).start()
    return True


def _load_engine_async():
    """Charge le moteur Whisper en tâche de fond (la fenêtre est déjà affichée).
    Met à jour _controller + injecte le glossaire + signale l'UI quand prêt."""
    global _controller
    if not _engine_load_params:
        return
    whisper_dir, cpu_threads = _engine_load_params
    print(f"[whisper] chargement en arrière-plan "
          f"({os.path.basename(whisper_dir)}, int8, cpu_threads={cpu_threads})…")
    _ui("if(typeof setEngineReady==='function') setEngineReady(false);")
    try:
        engine = VlocalEngine(model_dir=whisper_dir, cpu_threads=cpu_threads)
    except Exception as e:
        print(f"[whisper] échec chargement moteur : {e}")
        if _EV: _EV.log(_EV.E.ENGINE_LOAD_FAIL, err=type(e).__name__)   # S2 — capté par la tour de contrôle
        _toast("Le moteur de transcription n'a pas pu démarrer. Réessayez de "
               "lancer Vlocal.", kind="error")
        return
    _controller = DictationController(engine)
    # Glossaire -> biais lexical (hotwords)
    try:
        if _store is not None:
            from engine import set_correction_terms
            terms = [c for _, c in _store.glossary_pairs()]
            set_correction_terms(terms)
            print(f"[glossaire] {len(terms)} terme(s) injecté(s).")
    except Exception as e:
        print(f"[glossaire] injection KO : {e}")
    try:
        import psutil as _ps
        rss = _ps.Process().memory_info().rss / 1e9
        print(f"[whisper] prêt — turbo en mémoire, RAM {rss:.2f} Go.")
    except Exception:
        print("[whisper] prêt.")
    _engine_ready.set()
    _start_dictation_supervisor()   # v1.0.9 — auto-heal : la dictée ne reste jamais bloquée
    # v1.0.22 — MICRO BÉTON : préchauffe le worker micro (process jetable) pour
    # que la 1re dictée n'attende pas son spawn. Best-effort, en thread.
    try:
        threading.Thread(target=engine.prewarm_mic_worker,
                         name="micworker-prewarm", daemon=True).start()
    except Exception:
        pass
    _ui("if(typeof setEngineReady==='function') setEngineReady(true);")
    _start_idle_unloader()   # libère Whisper de la RAM après inactivité prolongée
    # v2 — Préchargement de small EN FOND si la dictée l'utilisera (modes
    # « auto » et « rapide »), pour que la PREMIÈRE dictée soit déjà rapide
    # (sinon elle paie ~1 s de chargement small et ça ressemble à une latence).
    # Mode « fidele » (turbo seul) -> on ne charge PAS small (zéro RAM gaspillée).
    try:
        speed = _load_settings().get("dictation_speed", "auto")
    except Exception:
        speed = "auto"
    def _preload_and_warm():
        try:
            # v23.1 RAM — quand le GPU MLX est actif, la dictée passe EXCLUSIVEMENT
            # par lui (transcribe_adaptive court-circuite la cascade CPU). Le
            # modèle small CPU (~480 Mo) ne servirait JAMAIS : on évite de le
            # précharger. Économie nette ~480 Mo, zéro impact dictée. (En repli
            # CPU pur — Intel/Windows — on garde le préchargement small.)
            try:
                import mlx_engine as _mlx
                _gpu = _mlx.available()
            except Exception:
                _gpu = False
            if speed in ("auto", "rapide") and not _gpu:
                if engine._ensure_fast_model():
                    print("[whisper] small préchargé en fond (dictée « auto »/« rapide »).")
            # PRÉCHAUFFE : amortit la 1re inférence (cold-start) -> la PREMIÈRE
            # dictée réelle est aussi rapide que les suivantes.
            engine.warmup()
        except Exception:
            pass
    threading.Thread(target=_preload_and_warm, daemon=True).start()


_instance_lock = None   # garde le verrou d'instance unique en vie


def _acquire_single_instance():
    """Empêche DEUX instances de Vlocal de tourner (= RAM doublée, ce qui tue une
    machine 8 Go). Verrou de fichier exclusif non bloquant. Renvoie le handle (à
    garder ouvert) ou None si une autre instance le détient déjà. No-op silencieux
    si indisponible (ex. Windows sans fcntl)."""
    try:
        import fcntl
        os.makedirs(_APP_SUPPORT, exist_ok=True)
        lockf = open(os.path.join(_APP_SUPPORT, "vlocal.lock"), "w")
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lockf
    except OSError:
        return None          # déjà verrouillé -> autre instance active
    except Exception:
        return True          # mécanisme indispo -> on ne bloque pas le démarrage


def main():
    global _window, _controller, _instance_lock, _api_instance

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
    turbo_dir = MODEL_DIR
    small_dir = SMALL_MODEL_DIR

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
    global _store
    try:
        _store = Store()
        print(f"[base]    {_store.path}")
    except Exception as e:
        _store = None
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
        if _last_ui != APP_VERSION:
            import shutil as _sh
            for _wd in ("~/Library/WebKit/com.vlocal.app",
                        "~/Library/Caches/com.vlocal.app"):
                _p = os.path.expanduser(_wd)
                if os.path.isdir(_p):
                    _sh.rmtree(_p, ignore_errors=True)
            try:
                os.makedirs(_APP_SUPPORT, exist_ok=True)
                with open(_ui_marker, "w") as _f:
                    _f.write(APP_VERSION)
            except Exception:
                pass
            print(f"[ui]      cache WebKit purgé (MAJ {_last_ui} -> {APP_VERSION}) : UI fraîche garantie.")
    except Exception as _e:
        print(f"[ui]      purge cache WebKit ignorée : {_e}")

    # Paramètres pour le chargement différé du moteur (lancé dans _after_start).
    global _engine_load_params, _cpu_threads_resolved
    _cpu_threads_resolved = cpu_threads
    if whisper_dir is not None:
        _engine_load_params = (whisper_dir, cpu_threads)
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
    _api_instance = Api()
    # v1.1.0 — identifiant d'installation (UUID aléatoire, jamais dérivé de la
    # machine) + planificateur de télémétrie. Rien ne part tant que l'utilisateur
    # n'a pas fait son choix (telemetry_enabled is True), cf. telemetry.py.
    global _telemetry
    try:
        if not _load_settings().get("install_id"):
            _save_settings({"install_id": telemetry.new_install_id()})
        import platform as _plat
        _telemetry = telemetry.Telemetry(_load_settings, _save_settings,
                                         lambda: _store, APP_VERSION,
                                         _plat.mac_ver()[0] or "")
        _telemetry.start()
    except Exception as e:
        print(f"[telemetry] non démarrée : {e}")
    _start_auto_diag()   # v1.0.11 — diagnostic auto consenti (cohorte early/testeurs)
    _window = webview.create_window(
        "Vlocal",
        HTML_PATH,
        js_api=_api_instance,
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
        if _quitting:
            return True
        global _window_visible
        try:
            _window.hide()
            _window_visible = False
        except Exception:
            pass
        return False   # fermeture fenêtre = masquage -> process maintenu en vie
    try:
        _window.events.closing += _on_window_closing
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
        _window.events.shown += _on_window_shown
    except Exception:
        pass
    # v3.3.2 — RE-APPLIQUER APRÈS CHARGEMENT (fix fenêtre figée ~5s) : pywebview fait
    # setContentView_(webview) + makeFirstResponder_(webview) à la fin de la 1re
    # navigation (cocoa didFinishNavigation), ce qui enterre la bande de drag. On
    # ré-applique style + bande sur « loaded » (postérieur au reset) pour la remettre
    # au-dessus de la webview. addOperationWithBlock_ garantit l'ordre (runloop suivant).
    try:
        _window.events.loaded += _on_window_shown
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
        if start_global_hotkey() is not None:
            print("[hotkey]  actif : maintiens ton raccourci global pour dicter.")

        # v19 — Overlay de dictée flottant (NSPanel non-activant) : créé sur le
        # main thread, gardé caché jusqu'à la 1re dictée au raccourci.
        try:
            overlay.create()
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
                            global _window_visible
                            try:
                                if _window is not None:
                                    _window.show()
                                _window_visible = True
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
                global _window_visible
                try:
                    if _window is not None and not _quitting:
                        _window.show()
                        _window_visible = True
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
            global _window_visible
            try:
                from AppKit import NSApplication
                app = NSApplication.sharedApplication()
                active = bool(app.isActive())
                if active and not _reopen["prev_active"] and not _window_visible:
                    if _window is not None:
                        _window.show()
                    _window_visible = True
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
            global _quitting, _quit_in_progress
            if _quit_in_progress:
                return
            _quit_in_progress = True
            _quitting = True   # autorise la vraie fermeture (vs masquage)
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
                    if _store and mid:
                        _store.update_meeting(mid, status="error")
                    print("[quit]    réunion en cours sauvegardée (WAV clos, "
                          "statut error récupérable).")
            except Exception as e:
                print(f"[quit]    finalisation réunion KO : {e}")
            # Fermeture PROPRE de la base avant de quitter (WAL tronqué).
            # Store.close() est idempotent : le 2e appel (fin de boucle
            # Cocoa, après webview.start) est sans effet.
            try:
                if _store:
                    _store.close()
            except Exception:
                pass

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
                        global _window_visible
                        try:
                            if _window is not None:
                                _window.show()
                            _window_visible = True
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
            global _window_visible
            try:
                if _window is not None:
                    _window.show()
                _window_visible = True
            except Exception:
                pass
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:
                pass
        def _on_dictate_toggle():
            if _controller and _controller.recording_active():
                threading.Thread(target=_controller.stop_and_finalize,
                                 daemon=True).start()
            elif _controller:
                _controller.start()
        def _on_toggle_task(tid):
            if _store:
                _store.toggle_task(int(tid))
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
            global _menubar
            if _menubar is not None:
                return
            try:
                import menubar as _mb

                def _open_settings():
                    _on_open()
                    _ui("if(typeof activateView==='function')activateView('reglages')")

                _menubar = _mb.MenuBar({
                    "dictate": _on_dictate_toggle,
                    "open": _on_open,
                    "settings": _open_settings,
                    "quit": _on_quit,
                    "toggle_task": _on_toggle_task,
                }, labels=_menubar_labels())
                _refresh_menubar()                       # peuple « Rappels récents »
                try:
                    overlay.set_anchor_provider(_menubar.icon_frame)
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
        notifier.prebuild_async()

        # Premier render des listes (après que la fenêtre soit prête).
        threading.Timer(0.5, _refresh_lists_ui).start()

    webview.start(_after_start)
    # Sortie par fin de boucle Cocoa (fermeture sans passer par « Quitter ») :
    # fermeture PROPRE de la base (WAL tronqué). Store.close() est idempotent,
    # le double appel avec _on_quit est sûr.
    try:
        if _store:
            _store.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
