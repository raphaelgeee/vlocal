"""Cycle de vie du processus Vlocal.

Délégué d'application (rouvrir, quitter pour de vrai), sortie propre, instance
unique, détecteur de gel du thread principal, relance de dernier recours,
reprise après la veille, assertions anti-App Nap et diagnostic automatique
consenti. Tout ce qui touche AppKit est appelé sur le thread principal ou y est
confié ; rien ici ne bloque la boucle d'événements de pywebview.
"""
import os
import sys
import threading
import time

import applog
import appstate
import errors as _EV
import notifier
from settings import _APP_SUPPORT, _SHOW_REQUEST, _load_settings, _save_settings
from texts import _te

log = applog.get("lifecycle")


# --------------------------------------------------------------------------- #
# v1.3.2 — CYCLE DE VIE : rouvrir et quitter ne dépendent plus du démarrage.
#
# Jusqu'en 1.3.0, le délégué d'application qui sait rouvrir la fenêtre et
# quitter pour de vrai était posé par _after_start (aujourd'hui startup.after_start), tout à la fin. Si une étape
# précédente levait, il n'était jamais posé, et celui de pywebview restait
# actif : fermer la fenêtre la masquait pour de bon, le clic sur le Dock ne
# faisait rien, et « Quitter » était refusé (pywebview consulte notre gestionnaire
# de fermeture, qui répond « masquer »). Seul « Forcer à quitter » en sortait.
#
# On remplace donc la CLASSE de délégué que pywebview instancie lui-même, avant
# que la fenêtre n'existe : quel que soit l'ordre de démarrage et quoi qu'il
# arrive ensuite, le délégué actif sait rouvrir et quitter.
_lifecycle_quit_hook = None     # nettoyage complet (startup._perform_quit), posé par startup.after_start
_LifecycleDelegate = None       # la classe installée dans pywebview


def _register_quit_hook(fn):
    global _lifecycle_quit_hook
    _lifecycle_quit_hook = fn


def _lifecycle_quit():
    """Quitter pour de vrai. Utilise le nettoyage complet s'il est enregistré ;
    sinon (démarrage incomplet), marque la sortie, ferme la base et garantit la
    mort du process en 4 s. Ne lève jamais."""
    hook = _lifecycle_quit_hook
    if hook is not None:
        hook()
        return
    appstate.quitting = True
    t = threading.Timer(4.0, lambda: os._exit(0))
    t.daemon = True
    t.start()
    try:
        if appstate.store:
            appstate.store.close()
    except Exception:
        log.debug("store.close : échec ignoré", exc_info=True)


def _lifecycle_show():
    """Ré-affiche la fenêtre masquée et ramène Vlocal au premier plan."""
    try:
        if appstate.window is not None:
            appstate.window.show()
        appstate.window_visible = True
    except Exception:
        log.debug("window.show : échec ignoré", exc_info=True)
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        log.debug("import AppKit : échec ignoré", exc_info=True)


def _install_lifecycle_delegate_class():
    """À appeler après `import webview` et AVANT create_window/start."""
    global _LifecycleDelegate
    if _LifecycleDelegate is not None:
        return True
    try:
        import webview.platforms.cocoa as _cocoa

        class VlocalLifecycleDelegate(_cocoa.BrowserView.AppDelegate):
            def applicationShouldTerminate_(self, sender):
                try:
                    _lifecycle_quit()
                except Exception:
                    log.debug("_lifecycle_quit : échec ignoré", exc_info=True)
                    os._exit(0)
                return 1   # NSTerminateNow

            def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, flag):
                _lifecycle_show()
                return True

        _cocoa.BrowserView.AppDelegate = VlocalLifecycleDelegate
        _LifecycleDelegate = VlocalLifecycleDelegate
        log.info("[cycle]   délégué de cycle de vie installé dans pywebview (rouvrir + quitter).")
        return True
    except Exception as e:
        log.warning(f"[cycle]   délégué de cycle de vie indisponible ({e}) — Cmd+Q masquera la fenêtre, quitter par l'icône V.")
        return False


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
            log.debug("import AppKit : échec ignoré", exc_info=True)
            os._exit(0)
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(_do)
    except Exception:
        log.debug("import Foundation : échec ignoré", exc_info=True)
        _do()


def _reveal_app_in_finder():
    """Révèle le bundle Vlocal.app dans le Finder (pour le glisser sur le « + » de
    la liste Accessibilité quand il n'y apparaît pas). Best-effort, jamais d'exception."""
    try:
        from Foundation import NSBundle
        path = NSBundle.mainBundle().bundlePath() or "/Applications/Vlocal.app"
    except Exception:
        log.debug("import Foundation : échec ignoré", exc_info=True)
        path = "/Applications/Vlocal.app"
    try:
        from AppKit import NSWorkspace
        NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(path, "")
    except Exception:
        log.debug("import AppKit : échec ignoré", exc_info=True)


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
            log.debug("import Foundation : échec ignoré", exc_info=True)
            opts = 0x00FFFFFF | 0xFF00000000   # valeurs documentées NSProcessInfo.h
        return NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            opts, reason)
    except Exception:
        log.debug("import Foundation : échec ignoré", exc_info=True)
        return None


def _end_activity(token) -> None:
    try:
        if token is not None:
            from Foundation import NSProcessInfo
            NSProcessInfo.processInfo().endActivity_(token)
    except Exception:
        log.debug("endActivity_ : échec ignoré", exc_info=True)


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
        log.warning(f"[watchdog] battement main-thread indispo ({e}) — détecteur OFF.")
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
                log.debug("_EV.log : échec ignoré", exc_info=True)
            log.warning(f"[watchdog] thread principal figé > {age:.0f}s -> arrêt "
                  "forcé de Vlocal (à relancer).")
            os._exit(70)

    threading.Thread(target=_freeze_monitor, name="freeze-watchdog",
                     daemon=True).start()


_last_self_relaunch = [0.0]   # anti-boucle : 1 relance / 5 min max


def _self_relaunch(reason: str = "") -> None:
    """Dernier recours : relance proprement l'app (open -n) puis quitte. Anti-
    boucle (1 relance / 5 min). L'utilisateur n'a RIEN à faire : Vlocal réapparaît
    sain en ~1 s. Réservé au cas où un reset logiciel ne suffit pas (sous-système
    audio OS réellement irrécupérable)."""
    import time as _t
    now = _t.time()
    if now - _last_self_relaunch[0] < 300.0:
        log.warning("[auto-heal] relance déjà tentée récemment -> abstention (anti-boucle).")
        return
    try:
        exe = sys.executable or ""
        i = exe.find(".app/Contents/MacOS/")
        bundle = exe[:i + 4] if i > 0 else None
        if not bundle or not os.path.isdir(bundle):
            log.warning("[auto-heal] bundle introuvable -> relance annulée.")
            return
        _last_self_relaunch[0] = now
        log.warning(f"[auto-heal] RELANCE de l'app ({reason}) : {bundle}")
        try:
            notifier.show("Vlocal redémarre",
                          _te("La dictée était bloquée. Vlocal se relance, réessayez dans un instant."),
                          subtitle=None)
        except Exception:
            log.debug("notifier.show : échec ignoré", exc_info=True)
        import subprocess
        subprocess.Popen(["/bin/sh", "-c", 'sleep 1.2; /usr/bin/open -n "%s"' % bundle],
                         start_new_session=True)
        _t.sleep(0.4)
    except Exception as e:
        log.warning(f"[auto-heal] relance KO ({e}).")
        return
    os._exit(0)


# v1.5 — Au réveil, CoreAudio republie ses périphériques pendant une à deux
# secondes : un worker relancé trop tôt figerait une liste encore incomplète.
WAKE_SETTLE_S = 2.0
_wake_observers = []   # jeton de l'observateur : le garder vivant


def _watch_system_wake(engine) -> None:
    """v1.5 — Sortie de veille -> worker micro neuf, AU REPOS (engine.
    mic_after_wake), pour que le premier appui après le réveil n'ouvre ni un
    PortAudio périmé ni l'ancien micro, et ne paie pas la réparation."""
    try:
        from AppKit import NSWorkspace, NSWorkspaceDidWakeNotification
    except ImportError as e:
        log.warning(f"[micworker] suivi du réveil indisponible : {e}")
        return

    def _on_wake(_note):
        t = threading.Timer(WAKE_SETTLE_S, engine.mic_after_wake)
        t.daemon = True
        t.start()

    try:
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        _wake_observers.append(center.addObserverForName_object_queue_usingBlock_(
            NSWorkspaceDidWakeNotification, None, None, _on_wake))
    except Exception as e:   # fonction d'appoint : le démarrage continue sans elle
        log.warning(f"[micworker] suivi du réveil non installé : {e}")


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
        log.debug("_load_settings : échec ignoré", exc_info=True)
        return False


def _auto_diag_loop():
    import time as _t
    _t.sleep(90)   # laisse le démarrage tranquille
    while True:
        try:
            if _auto_diag_enabled() and appstate.api is not None:
                try:
                    last = float(_load_settings().get("auto_diag_last", 0) or 0)
                except Exception:
                    log.debug("float : échec ignoré", exc_info=True)
                    last = 0.0
                if (_t.time() - last) > _AUTO_DIAG_DAYS * 86400:
                    try:
                        r = appstate.api.send_support_diagnostic(
                            "[auto] rapport periodique testeur", "", source="auto-diag")
                        if isinstance(r, dict) and r.get("ok"):
                            _save_settings({"auto_diag_last": _t.time()})
                    except Exception:
                        log.debug("api.send_support_diagnostic : échec ignoré", exc_info=True)
        except Exception:
            log.debug("_auto_diag_enabled : échec ignoré", exc_info=True)
        _t.sleep(6 * 3600)   # re-vérifie toutes les 6 h


def _start_auto_diag():
    try:
        threading.Thread(target=_auto_diag_loop, name="auto-diag", daemon=True).start()
    except Exception:
        log.debug("start : échec ignoré", exc_info=True)


def _acquire_single_instance():
    """Empêche DEUX instances de Vlocal de tourner : deux raccourcis globaux, deux
    bulles, le même texte inséré deux fois, et la RAM doublée. Renvoie les handles
    à garder vivants, ou None si une autre instance tourne déjà.
    v1.3.3 — deux couches (dossier de support ET dossier privé de l'utilisateur,
    indépendant de $HOME), et seul EWOULDBLOCK vaut « déjà lancé » : un HOME sur
    un partage réseau sans flock ne peut plus empêcher Vlocal de s'ouvrir.
    Détail et raisons dans instance_lock.py."""
    import instance_lock
    return instance_lock.acquire(_APP_SUPPORT)


def _hand_over_to_running_instance():
    """Vlocal tourne déjà : on lui demande de ré-afficher sa fenêtre et on
    l'active, plutôt que de mourir en silence (« ça ne s'ouvre pas »)."""
    log.info("[vlocal] déjà en cours — demande d'affichage de l'instance existante.")
    # L'instance existante peut avoir sa fenêtre CACHÉE (agent en arrière-plan).
    # On dépose une « demande d'affichage » qu'elle surveille -> elle ré-affiche
    # sa fenêtre. (Sinon : relancer l'app ne montrait rien = « je n'arrive pas
    # à la relancer ».) Puis on active et on sort.
    try:
        with open(_SHOW_REQUEST, "w") as f:
            f.write("show")
    except Exception:
        log.debug("open : échec ignoré", exc_info=True)
    try:
        from AppKit import (NSRunningApplication,
                            NSApplicationActivateIgnoringOtherApps)
        me = os.getpid()
        for ap in NSRunningApplication.runningApplicationsWithBundleIdentifier_(
                "com.vlocal.app"):
            if ap.processIdentifier() != me:
                ap.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    except Exception:
        log.debug("import AppKit : échec ignoré", exc_info=True)
