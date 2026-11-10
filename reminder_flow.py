"""Rappels créés par la voix, et ce qui suit une dictée.

Une dictée qui ressemble à un rappel daté (« rappelle-moi demain à 9 h… »)
devient une tâche avec une alerte planifiée ; les alertes sont ré-armées au
démarrage, annulées quand la tâche est cochée ou supprimée. Avec un coffre
Obsidian connecté, la dictée y est aussi rangée. Analyse : reminders.py
(déterministe, hors ligne) ; alerte : notifier.py.
"""
import json
import sys
import threading
import time

import applog
import appstate
import notifier
import obsidian
import processor
import reminders
from settings import _load_settings
from texts import _te
from ui_bridge import _msg, _refresh_lists_ui, _refresh_menubar, _toast, _ui

log = applog.get("reminder_flow")


# v2 — Déclenchement d'un rappel : notification système FIABLE + bannière in-app
# garantie (si la fenêtre est ouverte) + réveil de l'app. On ne rate JAMAIS un
# rappel : si la notif système est bloquée, l'utilisateur voit quand même la
# bannière à l'écran.
def _fire_reminder(titre: str):
    titre = (titre or "").strip() or "Rappel"
    # 1) Notification système (osascript, fiable)
    try:
        notifier.show(_te("Rappel"), titre, subtitle=None)
    except Exception:
        log.debug("notifier.show : échec ignoré", exc_info=True)
    # 2) Bannière in-app prominente + réveil de la fenêtre (macOS)
    try:
        _ui("if(typeof showReminder==='function') showReminder(%s);"
            % json.dumps(titre))
    except Exception:
        log.debug("_ui : échec ignoré", exc_info=True)
    if sys.platform == "darwin":
        _request_dock_attention()


def _request_dock_attention():
    """Rebond de l'icône du Dock. AppKit n'est sûr que sur le thread
    principal : l'appel y est confié (le rappel sonne sur un fil Timer)."""
    def _bounce():
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication().requestUserAttention_(1)
        except Exception:
            log.warning("requestUserAttention_ KO", exc_info=True)
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(_bounce)
    except Exception:
        log.warning("rebond du Dock : file principale indisponible", exc_info=True)


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
        for t in (appstate.store.list_tasks(limit=500, only_open=True) if appstate.store else []):
            if t.get("id") == task_id:
                return True
    except Exception:
        log.debug("store.list_tasks : échec ignoré", exc_info=True)
        return True            # doute -> on sonne (ne jamais perdre un rappel)
    return False


def _cancel_reminder_timer(task_id):
    with _REMINDER_TIMERS_LOCK:
        t = _REMINDER_TIMERS.pop(task_id, None)
    if t is not None:
        try:
            t.cancel()
        except Exception:
            log.debug("t.cancel : échec ignoré", exc_info=True)


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
            log.info(f"[rappel]  #{task_id} coché/supprimé avant l'échéance — silencieux.")

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
        log.debug("import time : échec ignoré", exc_info=True)
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
        glossary = appstate.store.glossary_pairs() if appstate.store else []
        raw_gloss = processor.apply_glossary(raw, glossary) if glossary else raw
        data = reminders.parse_reminder(raw_gloss, time.time()) or {}
        return bool(data.get("datetime_iso"))
    except Exception:
        log.debug("reminders.is_reminder : échec ignoré", exc_info=True)
        return False


def _post_process_by_mode(raw: str, mode: str):
    """v2 — DEUX modes seulement : DICTÉE et RÉUNION.

    DICTÉE : si le texte dicté est MANIFESTEMENT un rappel (« rappelle-moi… »,
    « n'oublie pas… », « pense à… »), on crée AUTOMATIQUEMENT le rappel (parseur
    local déterministe, instantané, hors-ligne) ; sinon c'est une dictée normale
    (le texte brut est déjà copié, rien d'autre à faire). RÉUNION a son propre
    pipeline. Silencieux en cas d'échec : le brut reste disponible."""
    if not raw or appstate.store is None:
        return
    if (mode or "DICTEE").upper() == "REUNION":
        return
    try:
        if reminders.is_reminder(raw):
            _create_reminder_from_text(raw)
    except Exception as e:
        log.warning(f"[dictée] auto-rappel ignoré ({e}).")
    # v1.0.13 — Capture Obsidian (ADDITIF, 100% local) : si un coffre est connecté,
    # chaque dictée est rangée au bon endroit via un routeur à mots-clés déterministe.
    # Hors du chemin critique (on est déjà sur un thread daemon) ; échec silencieux,
    # le brut reste copié/inséré quoi qu'il arrive.
    try:
        _s = _load_settings()
        if _s.get("obsidian_enabled") and _s.get("obsidian_vault"):
            obsidian.capture(raw, _s.get("obsidian_vault"), time.time())
    except Exception as e:
        log.warning(f"[dictée] capture Obsidian ignorée ({e}).")


def _create_reminder_from_text(raw: str):
    """Crée un rappel depuis un texte dicté (parseur local). Notif planifiée à
    l'heure dite ; confirmation in-app QUOI + QUAND."""
    if _recently_added_same(raw, appstate.store):
        return
    glossary = appstate.store.glossary_pairs() if appstate.store else []
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
        log.info("[dictée] rappel sans horaire -> ignoré (anti faux positif).")
        return
    try:
        tid = appstate.store.add_task(titre=titre, echeance=echeance_naturelle,
                              echeance_iso=datetime_iso, details=None,
                              source_raw_text=raw)
    except Exception:
        log.debug("store.add_task : échec ignoré", exc_info=True)
        return
    # v1.1.0 — coffre « Vlocal, ma voix » : le rappel y est aussi consigné.
    try:
        _s = _load_settings()
        if _s.get("obsidian_enabled") and _s.get("obsidian_vault"):
            obsidian.capture_reminder(titre, echeance_naturelle, _s.get("obsidian_vault"))
    except Exception:
        log.debug("_load_settings : échec ignoré", exc_info=True)
    iso_ts = notifier.parse_iso(datetime_iso)
    if iso_ts and iso_ts > now_ts:
        when = reminders.humanize_when(datetime_iso, now_ts)
        _schedule_reminder(iso_ts, titre, task_id=tid)
        try:
            appstate.store.mark_notif_scheduled(tid)
        except Exception:
            log.debug("store.mark_notif_scheduled : échec ignoré", exc_info=True)
        _toast((f"C'est noté : « {titre} ». Vous serez prévenu {when}."
                if when else f"C'est noté : « {titre} »."), kind="success")
    elif iso_ts and iso_ts <= now_ts:
        _toast(_msg(f"Rappel « {titre} » enregistré, mais l'heure indiquée est déjà "
                    "passée : aucune alerte ne sera envoyée.",
                    f"Reminder “{titre}” saved, but the time given has already "
                    "passed: no alert will be sent."), kind="error")
    else:
        _toast(_msg(f"Rappel « {titre} » enregistré.", f"Reminder “{titre}” saved."),
               kind="info")
    _refresh_lists_ui(); _refresh_menubar()


# v14 — Vérifications modèle au démarrage (différées, hors chemin critique).
def _rearm_pending_reminders():
    """v15 — Au démarrage, re-planifie les notifs des rappels à échéance future.

    Les threading.Timer ne survivent PAS à un redémarrage de l'app. Sans ce
    ré-armement, un rappel créé hier pour aujourd'hui 14h ne se déclencherait
    jamais si l'app a été relancée entretemps. On relit la base et on re-arme
    tout rappel ouvert dont l'échéance est encore dans le futur."""
    if appstate.store is None:
        return
    rearmed = 0
    now = time.time()
    try:
        for t in appstate.store.list_tasks(limit=200, only_open=True):
            iso = t.get("echeance_iso")
            ts = notifier.parse_iso(iso) if iso else None
            if ts and ts > now:
                _schedule_reminder(ts, (t.get("titre") or "")[:120],
                                   task_id=t.get("id"))
                rearmed += 1
    except Exception as e:
        log.warning(f"[rappel]  ré-armement KO : {e}")
        return
    if rearmed:
        log.info(f"[rappel]  {rearmed} rappel(s) re-planifié(s) au démarrage.")
