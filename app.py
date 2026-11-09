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
prochain usage, pendant la parole. Voir engine_runtime._start_idle_unloader.

app.py n'assemble que le démarrage ; carte des modules : docs/ARCHITECTURE.md.

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
    except (OSError, AttributeError):
        # Framework introuvable (OSError) ou symbole absent (AttributeError) :
        # best-effort strict, le worker fonctionne comme avant (journal pas
        # encore configuré dans un worker).
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

# Verrou HORS-LIGNE HuggingFace — posé AVANT tout import applicatif (engine
# importe faster-whisper, qui embarque huggingface_hub) : sur tout chemin de
# code imprévu, le mode hors-ligne lève immédiatement au lieu d'émettre une
# requête réseau. Le chemin nominal ne télécharge jamais (modèles embarqués)
# -> aucun changement observable. download_whisper.py (outil dev qui a BESOIN
# du réseau) n'est pas affecté : l'env n'est posé que dans app.py.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import applog  # noqa: E402  (après le verrou hors-ligne, comme tout ce qui suit)
import appstate  # noqa: E402
import telemetry  # noqa: E402
from api import Api  # noqa: E402
from engine_runtime import (  # noqa: E402
    _choose_whisper_dir,
    _cpu_threads,
    _resolve_model_dirs,
    _telemetry_profile,
    selftest,
)
from lifecycle import (  # noqa: E402
    _acquire_single_instance,
    _hand_over_to_running_instance,
    _install_lifecycle_delegate_class,
    _start_auto_diag,
)
from settings import _load_settings, _save_settings  # noqa: E402
from startup import after_start, on_main_window_loaded, settle_pending_update  # noqa: E402
from storage import Store  # noqa: E402
from window_mac import (  # noqa: E402
    _purge_webkit_cache_on_update,
    _set_dock,
    _set_dock_visible,
    create_main_window,
)

log = applog.get("app")

# v16 — Base de ressources « frozen-aware » : en dev c'est le dossier du
# script ; une fois empaqueté par PyInstaller, les ressources (HTML, assets,
# modèle Whisper) sont extraites dans sys._MEIPASS. Tout chemin de ressource
# DOIT passer par BASE_DIR pour fonctionner aussi bien en dev qu'en .app.
if getattr(sys, "frozen", False):
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "vlocal-interface.html")

# v3.3 — APP THIN : les modèles Whisper vivent hors du bundle (cache App Support,
# téléchargés au premier lancement) ; model_store.models_dir() rend le cache s'il
# est complet, sinon le bundle. Résolus dès l'import (l'auto-test s'en sert),
# re-résolus au démarrage et après un téléchargement.
_resolve_model_dirs()

APP_VERSION = "1.5.0"   # 10 novembre 2026 : robustesse locale, pilule en verre, anticipation. Synchrone avec le fichier VERSION (build) ;
                        # affiché dans l'onglet « Mises à jour ». Historique : CHANGELOG.md.
appstate.app_version = APP_VERSION

_instance_lock = None   # garde le verrou d'instance unique en vie


def main():
    """Prépare le processus, ouvre la fenêtre, puis rend la main à la boucle
    Cocoa de pywebview (webview.start) jusqu'à la sortie de l'app."""
    global _instance_lock
    # Tout démarre « Prohibited » (aucune icône) : le resource_tracker relancé
    # par numba/MLX est dérouté par freeze_support avant que le processus
    # principal ne passe en icône de Dock (voir window_mac._set_dock).
    _set_dock(2)
    try:
        import multiprocessing
        multiprocessing.freeze_support()   # le child tracker/spawn part ICI -> reste Prohibited
    except Exception:
        log.debug("import multiprocessing : échec ignoré", exc_info=True)
    _set_dock_visible(True)           # process principal + fenêtre visible -> Regular (icône Dock)

    applog.setup()

    if "--selftest" in sys.argv:
        # Le selftest a besoin du moteur synchrone.
        sys.exit(selftest())

    # Instance unique : si Vlocal tourne déjà, on n'ouvre pas un 2e moteur (RAM)
    # et on ramène au premier plan l'instance déjà ouverte.
    _instance_lock = _acquire_single_instance()
    if _instance_lock is None:
        _hand_over_to_running_instance()
        sys.exit(0)

    import webview
    _install_lifecycle_delegate_class()   # v1.3.2 — avant toute fenêtre (lifecycle.py)
    # v3.3 (B1) — pywebview remet la policy en « Regular » (icône Dock) à l'IMPORT
    # (classe BrowserView). On RÉ-IMPOSE Accessory (icône menu-bar V, AUCUNE icône
    # Dock) ici, puis ENCORE après l'affichage de la fenêtre (_activate_on_launch).
    # Sinon : Dock + menu-bar = la régression « deux icônes » que la v3.3 tue.
    _set_dock_visible(True)

    whisper_dir = _choose_whisper_dir()
    cpu_threads = _cpu_threads()
    _open_store()

    # Mise à jour en attente : confirmation, retour arrière ou nettoyage. Un
    # retour arrière lancé impose de sortir : l'installateur attend notre mort.
    # La boucle Cocoa ne tourne pas encore, _quit_app() (qui passe par elle)
    # n'aurait aucun effet : on sort directement, base fermée.
    if settle_pending_update():
        _close_store()
        sys.exit(0)
    _purge_webkit_cache_on_update()   # avant toute WKWebView (tableau de bord, overlay)

    # Paramètres du chargement différé du moteur (lancé par startup.after_start).
    # whisper_dir None = app thin, 1er lancement : moteur chargé APRÈS le download.
    appstate.cpu_threads = cpu_threads
    if whisper_dir is not None:
        appstate.engine_load_params = (whisper_dir, cpu_threads)

    appstate.api = Api()
    _start_telemetry()
    _start_auto_diag()   # v1.0.11 — diagnostic auto consenti (cohorte early/testeurs)
    create_main_window(webview, HTML_PATH, appstate.api)
    try:
        appstate.window.events.loaded += on_main_window_loaded
    except Exception as e:
        log.warning(f"[update]  confirmation du démarrage non branchée : {e}")

    webview.start(after_start)
    # Sortie par fin de boucle Cocoa (fermeture sans passer par « Quitter »).
    _close_store()


def _close_store():
    """Fermeture PROPRE de la base (WAL tronqué). Store.close() est idempotent :
    le double appel avec le nettoyage de sortie (_perform_quit) est sûr."""
    try:
        if appstate.store:
            appstate.store.close()
    except Exception:
        log.debug("store.close : échec ignoré", exc_info=True)


def _open_store():
    """v6 — Persistance locale (rapide), ouverte AVANT la fenêtre : les listes
    s'affichent tout de suite. Le moteur Whisper (lent) charge en tâche de fond."""
    try:
        appstate.store = Store()
        log.info(f"[base]    {appstate.store.path}")
    except Exception as e:
        appstate.store = None
        log.warning(f"[base]    persistance désactivée ({e}) — mode brut intact.")


def _start_telemetry():
    """v1.1.0 — Identifiant d'installation (UUID aléatoire, jamais dérivé de la
    machine) et planificateur de télémétrie. Rien ne part tant que l'utilisateur
    n'a pas fait son choix (telemetry_enabled is True), cf. telemetry.py.
    v1.3.6 — une copie de l'identifiant vit dans les préférences macOS : un
    dossier de l'app vidé, ou une réinstallation, ne crée plus une deuxième
    « installation » de la même personne dans la console."""
    try:
        telemetry.ensure_install_id(_load_settings, _save_settings)
        import platform as _plat
        appstate.telemetry = telemetry.Telemetry(_load_settings, _save_settings,
                                                 lambda: appstate.store, appstate.app_version,
                                                 _plat.mac_ver()[0] or "",
                                                 get_profile=_telemetry_profile)
        appstate.telemetry.start()
    except Exception as e:
        log.warning(f"[telemetry] non démarrée : {e}")


if __name__ == "__main__":
    main()
