"""Réglages persistants de Vlocal et chemins du dossier de support.

Tout vit dans ~/Library/Application Support/Vlocal : settings.json (lu avec un
cache invalidé par mtime), mode.txt (DICTÉE ou RÉUNION), glossary.txt. Les
drapeaux d'environnement (VLOCAL_REUNION, VLOCAL_VISIO) sont lus à l'import.
"""
import json
import os
import threading

import applog
import appstate

log = applog.get("settings")


# v2 — DEUX modes seulement : DICTÉE (rappels auto-détectés) et RÉUNION.
# Persisté dans ~/Library/Application Support/Vlocal/mode.txt entre sessions.
_APP_SUPPORT = os.path.expanduser("~/Library/Application Support/Vlocal")
_MODE_FILE = os.path.join(_APP_SUPPORT, "mode.txt")
_SETTINGS_FILE = os.path.join(_APP_SUPPORT, "settings.json")
_SHOW_REQUEST = os.path.join(_APP_SUPPORT, ".show_request")
_GLOSSARY_FILE = os.path.join(_APP_SUPPORT, "glossary.txt")
_VALID_MODES = ("DICTEE", "REUNION")


# v12 — Mode RÉUNION ACTIVÉ par défaut (était feature flag en V11).
# Override possible : VLOCAL_REUNION=0 pour le désactiver explicitement.
# Lecture aussi de settings.json (clé reunion_enabled) prise en compte
# dans `_init_reunion_flag()` plus bas.
appstate.reunion_enabled = os.environ.get("VLOCAL_REUNION", "1") not in ("0", "false", "False")
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
        log.debug("open : échec ignoré", exc_info=True)
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
        log.debug("os.makedirs : échec ignoré", exc_info=True)
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
        log.debug("path.getmtime : échec ignoré", exc_info=True)
        mtime = None
    with _SETTINGS_LOCK:
        if _SETTINGS_CACHE["data"] is not None and _SETTINGS_CACHE["mtime"] == mtime:
            return dict(_SETTINGS_CACHE["data"])
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data = data if isinstance(data, dict) else {}
    except Exception:
        log.debug("open : échec ignoré", exc_info=True)
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
        log.debug("bool : échec ignoré", exc_info=True)
        return True


def _voice_memory_enabled() -> bool:
    """v29.9 — Mémorisation des voix activée ? Défaut True. Toggle dans Réglages
    > Voix connues. Si False : aucune empreinte vocale n'est enregistrée
    (la diarisation de la réunion en cours marche toujours, seul l'apprentissage
    cross-réunions est désactivé)."""
    try:
        return bool(_load_settings().get("voice_memory_enabled", True))
    except Exception:
        log.debug("bool : échec ignoré", exc_info=True)
        return True


def _save_settings(patch: dict) -> dict:
    try:
        os.makedirs(_APP_SUPPORT, exist_ok=True)
    except Exception:
        log.debug("os.makedirs : échec ignoré", exc_info=True)
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
        log.debug("open : échec ignoré", exc_info=True)
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE["data"] = None
    return current


def _load_glossary_text() -> str:
    try:
        with open(_GLOSSARY_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        log.debug("open : échec ignoré", exc_info=True)
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
            log.debug("import engine : échec ignoré", exc_info=True)
        return True
    except Exception:
        log.debug("os.makedirs : échec ignoré", exc_info=True)
        return False


def _onboarding_done() -> bool:
    """v1.1.0 : l'écran de premier lancement a été terminé (réglage `onboarded`).
    Sert à différer les messages et demandes de permission tant qu'il est affiché.
    En cas de doute, True : ne jamais retenir un utilisateur."""
    try:
        return bool(_load_settings().get("onboarded"))
    except Exception:
        log.debug("bool : échec ignoré", exc_info=True)
        return True
