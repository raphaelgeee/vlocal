#!/usr/bin/env python3
"""
Vlocal — Raccourci global NATIF macOS via moniteurs NSEvent, SANS pynput.

POURQUOI NSEvent (et pas pynput ni un CGEventTap) :
- pynput résout le caractère de chaque touche via TSM sur son thread -> crash
  natif (SIGTRAP) sur macOS récent.
- Un CGEventTap en écoute seule se révélait LIVRÉ UNIQUEMENT à l'app active ici
  (le raccourci ne marchait que quand Vlocal était au premier plan).
- NSEvent fournit DEUX moniteurs complémentaires, exactement pour ce besoin :
    * addGlobalMonitorForEventsMatchingMask: -> événements destinés aux AUTRES
      apps (le cas qui manquait : Notes, Word, Chrome, Claude...).
    * addLocalMonitorForEventsMatchingMask:  -> événements destinés à Vlocal.
  Les deux ensemble = omniprésent, quelle que soit l'app au premier plan.

On ne lit QUE les modificateurs (flagsChanged) et le keycode brut — jamais le
caractère -> aucun appel TSM -> aucun crash. Les moniteurs tournent sur la main
run loop ; on déporte begin/end sur un thread worker pour ne jamais la bloquer.

Accessibilité requise (pour le moniteur GLOBAL). Auto-réparation : si l'utilisateur
accorde l'Accessibilité APRÈS le lancement, on RÉINSTALLE les moniteurs tout seul
(surveillance d'AXIsProcessTrusted) -> pas besoin de relancer l'app.

Sécurité : arrêt auto si le relâchement n'est jamais reçu (jamais de dictée infinie).
macOS uniquement.
"""

import sys
import threading
import time

_IS_MAC = sys.platform == "darwin"


# v1.2.0 — DÉCISION PURE (testable sans Cocoa) : faut-il démarrer, arrêter, ou
# ignorer cet événement ? Sortie : "begin", "end" ou None.
#
# `chord_vk` sert au raccourci TOUCHE FN (Globe) : le drapeau Function est aussi
# posé par les flèches et les touches F1-F12, donc on n'accepte le changement de
# modificateurs QUE s'il vient de la touche Fn elle-même (keyCode 63). Sans ce
# filtre, une flèche pourrait démarrer une dictée.
FN_KEYCODE = 63          # kVK_Function (touche Fn / Globe)
RIGHT_CMD_KEYCODE = 54   # kVK_RightCommand (Cmd gauche = 55)
RIGHT_OPT_KEYCODE = 61   # kVK_RightOption  (Option gauche = 58)


def decide(event_type, key_code, flags, want, chord, trigger_vk=None,
           chord_vk=None, T=None):
    T = T or {}
    if event_type == T.get("flags"):
        if chord_vk is not None and key_code != chord_vk:
            return None
        mods_ok = (flags & want) == want
        if chord:
            return "begin" if mods_ok else "end"
        return None if mods_ok else "end"
    if chord:
        return None
    if event_type == T.get("down"):
        return "begin" if (key_code == trigger_vk and (flags & want) == want) else None
    if event_type == T.get("up"):
        return "end" if key_code == trigger_vk else None
    return None


def start(on_begin, on_end, mods=("ctrl", "cmd"), trigger_vk=None, max_seconds=600.0,
          chord_vk=None):
    if not _IS_MAC:
        return None
    try:
        import AppKit
        from AppKit import NSEvent
        from Foundation import NSOperationQueue
    except Exception as e:
        print(f"[hotkey] AppKit indisponible ({e}) — raccourci désactivé.")
        return None

    FLAG = {
        "ctrl":  AppKit.NSEventModifierFlagControl,
        "cmd":   AppKit.NSEventModifierFlagCommand,
        "alt":   AppKit.NSEventModifierFlagOption,
        "shift": AppKit.NSEventModifierFlagShift,
        # v1.2.0 — touche Fn / Globe (raccourci « fn »). Voir decide() et
        # FN_KEYCODE : on exige que l'événement vienne de la touche elle-même.
        "fn":    AppKit.NSEventModifierFlagFunction,
    }
    want = 0
    for m in mods:
        want |= FLAG.get(m, 0)
    chord = trigger_vk is None
    MAX = float(max_seconds)
    st = {"active": False, "safety": None, "monitors": []}
    _st_lock = threading.Lock()        # CONC-2 : protège st["active"] (run loop + Timer)

    # CONC-1 : begin/end SÉRIALISÉS via une file FIFO à UN seul consommateur.
    # Sinon begin() et end() partaient sur deux threads indépendants pouvant se
    # réordonner -> end() pouvait s'exécuter avant que begin()->start() ait ouvert
    # le micro -> dictée fantôme micro-ouvert. La file garantit l'ordre begin->end.
    import queue as _queue
    _q = _queue.Queue()

    # v1.0.9 — PUMP WATCHDOG : si un job (begin/end) se fige (ex. start micro
    # bloqué), le consommateur unique mourrait -> plus aucune dictée possible
    # (« je n'arrive pas à redémarrer »). On surveille la durée du job courant ;
    # au-delà de PUMP_JOB_MAX, on lance un consommateur de SECOURS qui draine la
    # file (le job figé est abandonné en daemon). Le raccourci ne meurt jamais.
    _pump_job_since = [0.0]      # début du job courant (0.0 = inactif)
    _pump_jlock = threading.Lock()

    def _pump():
        while True:
            fn, label = _q.get()
            with _pump_jlock:
                _pump_job_since[0] = time.time()
            try:
                fn()
            except Exception as e:
                print(f"[hotkey] {label} KO (ignoré) : {e}")
            finally:
                with _pump_jlock:
                    _pump_job_since[0] = 0.0
    threading.Thread(target=_pump, name="hk-pump", daemon=True).start()

    def _pump_watchdog():
        PUMP_JOB_MAX = 8.0
        replaced_for = 0.0
        while True:
            time.sleep(1.0)
            try:
                with _pump_jlock:
                    since = _pump_job_since[0]
                if since > 0.0 and (time.time() - since) > PUMP_JOB_MAX and since != replaced_for:
                    replaced_for = since
                    print(f"[hotkey] pump figé (>{PUMP_JOB_MAX:.0f}s) -> consommateur de secours.")
                    threading.Thread(target=_pump, name="hk-pump-relief", daemon=True).start()
            except Exception:
                pass
    threading.Thread(target=_pump_watchdog, name="hk-pump-wd", daemon=True).start()

    def _async(fn, label):
        _q.put((fn, label))

    def _safety():
        with _st_lock:
            if not st["active"]:
                return
            st["active"] = False
        print(f"[hotkey] sécurité : arrêt auto après {MAX:.0f}s (relâchement non reçu).")
        _async(on_end, "end(safety)")

    def _begin():
        with _st_lock:
            if st["active"]:
                return
            st["active"] = True
            try:
                if st["safety"] is not None:
                    st["safety"].cancel()
                st["safety"] = threading.Timer(MAX, _safety)
                st["safety"].daemon = True
                st["safety"].start()
            except Exception:
                pass
        _async(on_begin, "begin")

    def _end():
        with _st_lock:
            if not st["active"]:
                return
            st["active"] = False
            try:
                if st["safety"] is not None:
                    st["safety"].cancel()
                    st["safety"] = None
            except Exception:
                pass
        _async(on_end, "end")

    _TYPES = {"flags": int(AppKit.NSEventTypeFlagsChanged),
              "down": int(AppKit.NSEventTypeKeyDown),
              "up": int(AppKit.NSEventTypeKeyUp)}

    def _handle(event):
        try:
            action = decide(int(event.type()), int(event.keyCode()),
                            int(event.modifierFlags()), want, chord,
                            trigger_vk, chord_vk, _TYPES)
            if action == "begin":
                _begin()
            elif action == "end":
                _end()
        except Exception as e:
            print(f"[hotkey] handler KO (ignoré) : {e}")

    def _gh(event):          # moniteur GLOBAL (autres apps) : retour ignoré
        _handle(event)

    def _lh(event):          # moniteur LOCAL (Vlocal) : renvoyer l'event = ne pas le consommer
        _handle(event)
        return event

    mask = AppKit.NSEventMaskFlagsChanged
    if not chord:
        mask |= AppKit.NSEventMaskKeyDown | AppKit.NSEventMaskKeyUp

    def _on_main(fn):
        try:
            NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
        except Exception:
            try:
                fn()
            except Exception:
                pass

    def _reconcile():
        # ANTI MICRO-BLOQUÉ — recale l'état sur la RÉALITÉ des modificateurs.
        # Un _begin() sans _end() (relâchement du chord PERDU pendant la fenêtre
        # retrait->ré-ajout des moniteurs lors d'un _install, ou événement manqué)
        # laisserait le micro OUVERT jusqu'au filet de sécurité (600 s). Ici : si on
        # se croit actif mais que les modificateurs ne sont plus tenus, on COUPE tout
        # de suite. N'affecte JAMAIS une vraie dictée : pendant celle-ci le chord est
        # maintenu, donc `held` reste vrai. Chord uniquement (combo à touche : le
        # KeyUp gère déjà l'arrêt et n'est pas perdu par une réinstallation).
        if not chord:
            return
        try:
            held = (int(NSEvent.modifierFlags()) & want) == want
        except Exception:
            return
        with _st_lock:
            stuck = st["active"] and not held
        if stuck:   # _end() pris HORS verrou (threading.Lock non réentrant)
            print("[hotkey] réconciliation : modificateurs relâchés -> arrêt dictée fantôme.")
            _end()

    def _install():
        for m in st["monitors"]:
            try:
                NSEvent.removeMonitor_(m)
            except Exception:
                pass
        st["monitors"] = []
        try:
            # Tracker chaque moniteur DÈS sa création : si l'ajout du local lève,
            # le global déjà installé reste retirable (pas de doublon ensuite).
            g = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, _gh)
            if g is not None:
                st["monitors"].append(g)
            l = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, _lh)
            if l is not None:
                st["monitors"].append(l)
            print(f"[hotkey] moniteurs NSEvent actifs ({len(st['monitors'])}) — "
                  "global (autres apps) + local (Vlocal).")
        except Exception as e:
            print(f"[hotkey] installation des moniteurs KO : {e}")
        # Après (ré)installation : recaler l'état. Si le relâchement du chord a été
        # perdu pendant la fenêtre retrait->ré-ajout, on coupe le micro fantôme ICI
        # (instantané, sans attendre le poll de _watch_trust).
        _reconcile()

    # État d'Accessibilité capturé AVANT l'install initiale : sert à seeder
    # _watch_trust pour éviter une réinstallation redondante au premier poll
    # quand l'Accessibilité est déjà accordée au lancement.
    try:
        from ApplicationServices import AXIsProcessTrusted
        trusted0 = bool(AXIsProcessTrusted())
    except Exception:
        trusted0 = None
    _on_main(_install)

    # AUTO-RÉPARATION : le moniteur GLOBAL reste muet tant que l'Accessibilité
    # n'est pas accordée. Dès qu'elle l'est (bascule), on réinstalle -> le
    # raccourci devient omniprésent SANS relancer l'app.
    def _watch_trust():
        import time as _t
        try:
            from ApplicationServices import AXIsProcessTrusted
        except Exception:
            return
        last = trusted0
        while True:
            try:
                cur = bool(AXIsProcessTrusted())
                if cur and cur != last:
                    print("[hotkey] Accessibilité OK -> (ré)installation des moniteurs.")
                    _on_main(_install)
                last = cur
                # Réconciliation périodique : borne un micro-bloqué à ~1 s même si
                # le relâchement a été perdu HORS de toute réinstallation (filet
                # rapide vs le garde-fou 600 s, sans jamais couper une vraie dictée).
                _reconcile()
            except Exception:
                pass
            _t.sleep(1.0)   # v1.0.9 : 2 -> 1 s (récupération « touche perdue » plus rapide)
    threading.Thread(target=_watch_trust, daemon=True).start()

    return st
