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

import os
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
import notifier
import overlay
import permissions
# v1.3.2 — au niveau module, et nulle part ailleurs. En 1.2.0, start_global_hotkey()
# utilisait hotkey_mac.FN_KEYCODE AVANT sa ligne `import hotkey_mac` : Python en
# faisait une variable locale non encore définie, UnboundLocalError à chaque
# lancement. tests/test_imports_order.py interdit désormais ce motif.
import telemetry        # v1.1.0 — télémétrie minimale déclarée (installs, usage par jour)
import updater          # v1.0.5 — mise à jour in-app (téléchargement + swap atomique)
from storage import Store
from api_files import FilesApi
from api_meetings import MeetingsApi
from api_settings import SettingsApi
from api_system import SystemApi
from hotkeys import start_global_hotkey
from engine_runtime import (
    _engine_ready,
    _load_engine_async,
    _point_mlx_to_cache,
    _post_startup_model_checks,
    _resolve_model_dirs,
    _telemetry_profile,
    selftest,
)
from meetings import _meeting_state
from dictation import copy_to_clipboard
from reminder_flow import (
    _cancel_reminder_timer,
    _rearm_pending_reminders,
    _schedule_reminder,
)
from lifecycle import (
    _acquire_single_instance,
    _arm_freeze_watchdog,
    _install_lifecycle_delegate_class,
    _register_quit_hook,
    _start_auto_diag,
)
from ui_bridge import (
    _api_safe,
    _cancel_overlay_hide,
    _menubar_labels,
    _refresh_lists_ui,
    _refresh_menubar,
    _schedule_overlay_hide,
    _toast,
    _ui,
)
from settings import (
    _APP_SUPPORT,
    _SHOW_REQUEST,
    _current_mode,
    _load_settings,
    _onboarding_done,
    _save_settings,
    _set_mode,
    _setup_file_logging,
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
class Api(SystemApi, SettingsApi, MeetingsApi, FilesApi):
    """Objet `js_api` de pywebview : chaque méthode publique est appelable par
    le tableau de bord (window.pywebview.api.<nom>). Liste figée par
    tests/test_api_exposee.py."""

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
