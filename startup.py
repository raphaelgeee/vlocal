"""Démarrage de Vlocal une fois la boucle Cocoa lancée.

webview.start(after_start) appelle after_start() sur un thread de pywebview
quand la fenêtre existe : chargement du moteur, raccourci global, overlay,
icône V, chien de garde du thread principal, veilles (ré-affichage, réveil
par le Dock, mises à jour). Tout ce qui touche AppKit est confié à la file
principale ; aucune étape ne bloque ni n'empêche les suivantes.
"""
import os
import sys
import threading
import time

import applog
import appstate
import errors as _EV
import lifecycle
import model_store
import notifier
import overlay
import permissions
import updater
from engine_runtime import _load_engine_async, _post_startup_model_checks
from hotkeys import start_global_hotkey
from lifecycle import _arm_freeze_watchdog, _lifecycle_show, _register_quit_hook
from meetings import _meeting_state
from recorder import repair_wav_header
from reminder_flow import _rearm_pending_reminders
from settings import _SHOW_REQUEST, _load_settings, _onboarding_done
from texts import _te
from ui_bridge import (
    _menubar_labels,
    _refresh_lists_ui,
    _refresh_menubar,
    _schedule_overlay_hide,
    _toast,
    _ui,
)
from window_mac import _force_clear_window, _set_dock_visible

log = applog.get("startup")

# Version abandonnée par un retour arrière automatique, annoncée à l'utilisateur
# au premier chargement de la fenêtre (le message ne peut pas partir avant).
_rollback_notice = None


def settle_pending_update():
    """Au lancement, avant la fenêtre : décide du sort d'une mise à jour en
    attente (updater.cleanup_leftovers : confirmation, retour arrière, blocage)
    et nettoie les restes. Renvoie True quand un retour arrière vient d'être
    lancé : l'installateur attend la mort de ce processus pour remettre
    l'ancienne version et la relancer, l'app doit donc se fermer tout de suite."""
    global _rollback_notice
    rep = updater.cleanup_leftovers(appstate.app_version)   # ne lève jamais
    status = rep.get("status")
    if status == "rollback_started":
        log.warning(f"[update]  la version {rep.get('version')} n'a pas confirmé son "
              "démarrage : retour arrière lancé, fermeture.")
        return True
    if status == "rolled_back":
        _rollback_notice = rep.get("version")
    elif status == "failed":
        # L'app en place est intacte ; le détail est déjà au journal d'updater.
        log.warning(f"[update]  mise à jour vers {rep.get('version')} non aboutie "
              f"({rep.get('reason')}) : version en place conservée.")
    return False


def rollback_message(version):
    """Phrase dite à l'utilisateur après un retour arrière automatique."""
    if appstate.ui_lang == "en":
        return (f"Version {version} did not start correctly: Vlocal went back "
                "to the previous version.")
    return (f"La version {version} n'a pas démarré correctement : Vlocal est "
            "revenu à la version précédente.")


def on_main_window_loaded():
    """Fenêtre principale chargée : ce démarrage est réussi. updater.confirm_startup
    (idempotent, fichiers seulement) libère la sauvegarde de l'ancienne version ;
    il part sur un thread, ce rappel de pywebview ne doit rien attendre. Un retour
    arrière survenu au lancement est annoncé une seule fois."""
    global _rollback_notice
    updater.mark_window_loaded()
    threading.Thread(target=updater.confirm_startup, args=(appstate.app_version,),
                     name="update-confirm", daemon=True).start()
    version, _rollback_notice = _rollback_notice, None
    if version:
        _toast(rollback_message(version), kind="info")


def after_start():
    """Étapes du démarrage, chacune isolée : un échec n'empêche pas les suivantes."""
    # AVANT le moteur : aucune réunion ne peut être en traitement à cet instant,
    # donc tout statut transitoire en base vient d'un processus interrompu.
    _recover_interrupted_meetings()
    # v2 — Charge le moteur Whisper en tâche de fond TOUT DE SUITE (la
    # fenêtre est déjà affichée -> démarrage instantané).
    threading.Thread(target=_load_engine_async, daemon=True).start()
    _start_hotkey_or_warn()
    _create_overlay()
    _start_show_request_watch()
    threading.Thread(target=_activate_on_launch, daemon=True).start()
    threading.Thread(target=_reopen_pump, daemon=True).start()
    _daemon_timer(2.5, _check_hotkey_perm)
    # Le nettoyage complet est enregistré TOUT DE SUITE : le délégué de cycle de
    # vie l'utilisera même si la suite du démarrage échoue.
    _register_quit_hook(_perform_quit)
    if sys.platform == "darwin":
        _on_main_thread(_create_menubar_on_main, "menubar")
        # v1.0.6 — DÉTECTEUR DE FIGEAGE : le NSTimer du battement doit vivre sur
        # la run loop du thread principal ; le démon de surveillance tuera l'app
        # si l'UI gèle plus de MAIN_FREEZE_KILL_S (plus d'app fantôme).
        _on_main_thread(_arm_freeze_watchdog, "watchdog")
        _on_main_thread(_force_clear_window, "window")
    # v15 — Plus aucun SLM. Vlocal = Whisper large-v3-turbo, point.
    # v15 — Message de bienvenue / note Ollama (différé, non bloquant).
    _daemon_timer(1.2, _post_startup_model_checks)
    # v15 — Re-planifie les rappels à échéance future (les Timer ne
    # survivent pas à un redémarrage).
    _daemon_timer(0.8, _rearm_pending_reminders)
    # v15 — Pré-construit le bundle de notification de marque "Vlocal"
    # (icône V premium) en arrière-plan : 1ʳᵉ notif instantanée + la
    # permission macOS est demandée tôt sous le nom Vlocal.
    try:
        notifier.prebuild_async()
    except Exception as _nt_err:
        log.warning(f"[notify]  pré-construction KO ({_nt_err})")
        try:
            if _EV:
                _EV.log(_EV.E.STARTUP_STAGE_FAIL, stage="notifier", err=type(_nt_err).__name__)
        except Exception:
            log.debug("_EV.log : échec ignoré", exc_info=True)

    # Premier render des listes (après que la fenêtre soit prête).
    _daemon_timer(0.5, _refresh_lists_ui)

    threading.Thread(target=_update_watch, name="update-watch", daemon=True).start()


def _recover_interrupted_meetings():
    """Réunions interrompues (arrêt brutal pendant l'enregistrement ou le
    traitement) : passées en « error », récupérables par « Réessayer », et
    en-tête de leur WAV réparé pour que le réessai lise tout l'audio capté."""
    if appstate.store is None:
        return
    try:
        recovered = appstate.store.recover_interrupted_meetings()
    except Exception:
        log.warning("[réunion] récupération des réunions interrompues KO",
                    exc_info=True)
        return
    for m in recovered:
        wav = m.get("wav_path")
        if not wav or not os.path.exists(wav):
            continue
        try:
            if repair_wav_header(wav):
                log.info(f"[réunion] en-tête WAV réparé (réunion {m['id']}).")
        except Exception:
            log.warning(f"[réunion] en-tête WAV non réparé (réunion {m['id']})",
                        exc_info=True)
    if recovered:
        log.info(f"[réunion] {len(recovered)} réunion(s) interrompue(s) passée(s) "
                 "en erreur récupérable.")


def _daemon_timer(delay, fn):
    """Minuteur de démarrage en fil daemon : ne retient jamais la sortie."""
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()
    return t


def _on_main_thread(fn, tag):
    """Confie fn à la file principale (AppKit exige le thread principal)."""
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
    except Exception as e:
        log.warning(f"[{tag}] dispatch impossible ({e})")


def _start_hotkey_or_warn():
    # N'affirme « actif » QUE si le raccourci a réellement démarré (les cas
    # 'none' et échec d'installation logguent déjà leur propre état).
    # v1.3.2 — ÉTAPE ISOLÉE. En 1.2.0/1.3.0 une exception ici sautait tout le
    # démarrage (menu, délégué, chien de garde). Désormais : trace complète,
    # incident compté dans la tour de contrôle, message à l'utilisateur, et
    # le reste du démarrage continue.
    try:
        if start_global_hotkey() is not None:
            log.info("[hotkey]  actif : maintenez votre raccourci global pour dicter.")
    except Exception as _hk_err:
        log.exception("[hotkey]  démarrage du raccourci global KO")
        try:
            if _EV:
                _EV.log(_EV.E.HOTKEY_START_FAIL, err=type(_hk_err).__name__)
        except Exception:
            log.debug("_EV.log : échec ignoré", exc_info=True)

        def _prevenir_raccourci_ko():
            try:
                overlay.info(_te("Raccourci indisponible"),
                             _te("Utilisez le bouton Dicter. Envoyez un diagnostic depuis les Réglages : "
                                 "le problème nous sera signalé."))
                _schedule_overlay_hide(9.0)
            except Exception:
                log.debug("overlay.info : échec ignoré", exc_info=True)


def _create_overlay():

    # v19 — Overlay de dictée flottant (NSPanel non-activant) : créé sur le
    # main thread, gardé caché jusqu'à la 1re dictée au raccourci.
    try:
        overlay.create()
        overlay.set_theme(_load_settings().get("theme", "dark"))   # v1.3.4 — Apparence
    except Exception as e:
        log.warning(f"[overlay] indisponible ({e}) — dictée sans overlay.")


def _start_show_request_watch():
    # v19 — RÉ-AFFICHAGE : quand on relance Vlocal alors qu'il tourne déjà
    # (fenêtre cachée), la 2e instance dépose _SHOW_REQUEST. On le surveille
    # ici et on ré-affiche la fenêtre -> « relancer l'app » fonctionne.
    try:
        if os.path.exists(_SHOW_REQUEST):
            os.remove(_SHOW_REQUEST)   # purge un résidu de démarrage
    except Exception:
        log.debug("path.exists : échec ignoré", exc_info=True)
    threading.Thread(target=_watch_show_requests, daemon=True).start()


def _watch_show_requests():
    while True:
        try:
            if os.path.exists(_SHOW_REQUEST):
                os.remove(_SHOW_REQUEST)
                _on_main_thread(_lifecycle_show, "show")
        except Exception:
            log.debug("path.exists : échec ignoré", exc_info=True)
        time.sleep(0.6)



# v3.3 — APP ACCESSORY (menu-bar) : contrairement à une app Regular, le
# dashboard ne passe pas DEVANT tout seul au lancement. On force UNE
# activation ~0.8 s après le démarrage (idempotent, sans effet si déjà au
# premier plan) pour qu'il s'ouvre bien au premier plan.
def _activate_on_launch():
    time.sleep(0.8)

    def _go():
        try:
            if appstate.window is not None and not appstate.quitting:
                appstate.window.show()
                appstate.window_visible = True
                from AppKit import NSApplication
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                _set_dock_visible(True)   # B1 — ré-impose Accessory APRÈS l'affichage (pywebview a pu remettre Regular)
        except Exception:
            log.debug("window.show : échec ignoré", exc_info=True)
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(_go)
    except Exception:
        log.debug("import Foundation : échec ignoré", exc_info=True)


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
        log.debug("import AppKit : échec ignoré", exc_info=True)


def _reopen_pump():
    from Foundation import NSOperationQueue
    while True:
        time.sleep(0.4)
        try:
            NSOperationQueue.mainQueue().addOperationWithBlock_(_reopen_check)
        except Exception:
            log.debug("addOperationWithBlock_ : échec ignoré", exc_info=True)


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
            _daemon_timer(3.0, _check_hotkey_perm)
            return
        # v1.0.3 — Demande proactive de la permission Micro (dialogue natif
        # AVFoundation, SANS ouvrir de flux audio) pour que la toute première
        # dictée ne meure pas en silence. No-op si déjà accordée/refusée.
        try:
            if permissions.mic_status() == 0:   # 0 = non déterminé
                permissions.mic_prompt()
        except Exception:
            log.debug("permissions.mic_status : échec ignoré", exc_info=True)
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
            log.debug("_EV.log : échec ignoré", exc_info=True)
    except Exception:
        log.debug("model_store.needs_download : échec ignoré", exc_info=True)


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
                log.debug("_meeting_state.get : échec ignoré", exc_info=True)
            rec.stop()                      # flush + clôture du WAV
            mid = _meeting_state.get("id")
            if appstate.store and mid:
                appstate.store.update_meeting(mid, status="error")
            log.info("[quit]    réunion en cours sauvegardée (WAV clos, "
                  "statut error récupérable).")
    except Exception as e:
        log.warning(f"[quit]    finalisation réunion KO : {e}")
    # Fermeture PROPRE de la base avant de quitter (WAL tronqué).
    # Store.close() est idempotent : le 2e appel (fin de boucle
    # Cocoa, après webview.start) est sans effet.
    try:
        if appstate.store:
            appstate.store.close()
    except Exception:
        log.debug("store.close : échec ignoré", exc_info=True)

# Le nettoyage complet est enregistré TOUT DE SUITE : le délégué de cycle de
# vie l'utilisera même si la suite du démarrage échoue.


def _on_quit():
    # Icône V -> « Quitter Vlocal » : nettoyage idempotent puis terminate_.
    _perform_quit()
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().terminate_(None)
    except Exception:
        log.debug("import AppKit : échec ignoré", exc_info=True)
        os._exit(0)


def _check_lifecycle_delegate():
    """v1.3.2 — Le délégué de cycle de vie (lifecycle.py) est installé dans
    pywebview avant la fenêtre : Cmd+Q, le Dock et le menu Pomme quittent pour
    de vrai, la croix masque. On vérifie seulement qu'il est bien celui de NSApp.
    S'il manquait (pywebview introuvable), quitter reste possible par l'icône V."""
    try:
        from AppKit import NSApplication
        d = NSApplication.sharedApplication().delegate()
        actif = (lifecycle._LifecycleDelegate is not None
                 and isinstance(d, lifecycle._LifecycleDelegate))
    except Exception as e:
        log.warning(f"[quit]    délégué illisible ({e}) — quit via icône V.")
        return
    if actif:
        log.info("[quit]    Cmd+Q / Dock / menu Pomme -> vrai quit "
              "(délégué de cycle de vie actif dès la fenêtre).")
    else:
        log.warning("[quit]    délégué de cycle de vie absent — quit via icône V.")


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
    _check_lifecycle_delegate()
    if appstate.menubar is not None:
        return
    try:
        import menubar as _mb

        def _open_settings():
            _lifecycle_show()
            _ui("if(typeof activateView==='function')activateView('reglages')")

        def _open_update():
            _lifecycle_show()
            _ui("if(typeof activateView==='function')activateView('maj')")

        appstate.menubar = _mb.MenuBar({
            "dictate": _on_dictate_toggle,
            "open": _lifecycle_show,
            "settings": _open_settings,
            "quit": _on_quit,
            "update": _open_update,
            "toggle_task": _on_toggle_task,
        }, labels=_menubar_labels())
        _refresh_menubar()                       # peuple « Rappels récents »
        log.info("[menubar] icône V installée.")
    except Exception as e:
        log.warning(f"[menubar] icône indisponible ({e}) — repli sans icône menu-bar.")


# v1.3.4 — Veille de mise à jour : au démarrage puis toutes les six heures,
# un simple GET (aucune donnée personnelle). Quand une version plus récente
# existe, le menu V gagne une entrée « Mise à jour disponible » en tête.
# La fenêtre, elle, pose sa propre pastille (checkUpdateBadge).
def _update_watch():
    time.sleep(30.0)
    while True:
        try:
            info = appstate.api.check_updates() if appstate.api is not None else None
            latest = (info or {}).get("latest") if (info or {}).get("update") else None
            if latest != appstate.update_latest:
                appstate.update_latest = latest
                _refresh_menubar()
        except Exception as _uw_err:
            log.warning(f"[update]  veille KO (ignorée) : {_uw_err}")
        time.sleep(6 * 3600)
