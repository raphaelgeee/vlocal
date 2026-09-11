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


TAP_MAX_S = 0.35       # appui plus court que ça = un « tap », pas une dictée tenue
DOUBLE_TAP_S = 0.45    # deux taps espacés de moins que ça = double appui


class TapMachine:
    """v1.3.3 — Transforme les appuis et relâchements du raccourci en actions de
    dictée. Posée AU-DESSUS de decide() : elle ne connaît ni la touche ni la
    combinaison, donc le double appui vaut d'office pour Ctrl + Cmd, Fn, Cmd
    droite, Option droite, Ctrl + Espace et tout raccourci à venir.

    mode « hold » (défaut) : appui = begin, relâchement = end. Rien ne change.
    mode « tap » : maintenir fonctionne toujours ; en plus, deux appuis brefs
    rapprochés VERROUILLENT le micro ouvert (mains libres), et l'appui suivant
    termine. Un appui bref isolé est annulé (rien à transcrire) et sert à
    afficher le conseil « appuie deux fois ».

    Actions rendues : "begin", "end", "cancel", "lock", "unlock",
    ("wait", échéance) quand la décision dépend d'un second appui à venir.
    Pure : aucune horloge interne, `now` est fourni par l'appelant. Testable.
    """

    def __init__(self, mode="hold", tap_max=TAP_MAX_S, double_tap=DOUBLE_TAP_S):
        self.mode = "tap" if mode == "tap" else "hold"
        self.tap_max = float(tap_max)
        self.double_tap = float(double_tap)
        self.active = False            # un begin a été émis, ni end ni cancel depuis
        self.locked = False            # mains libres
        self.deadline = None           # échéance d'un tap simple en attente de suite
        self.t_press = None
        self._swallow_release = False  # le relâchement de l'appui qui verrouille/termine ne compte pas

    @property
    def pending(self):
        return self.deadline is not None

    def press(self, now):
        if self.mode == "hold":
            if self.active:
                return []
            self.active = True
            return ["begin"]
        if self.locked:
            self.locked = False
            self.active = False
            self._swallow_release = True
            return ["end", "unlock"]
        acts = self.tick(now)          # une attente expirée avant cet appui = annulée d'abord
        if self.deadline is not None:
            self.deadline = None
            self.locked = True
            self._swallow_release = True
            return acts + ["lock"]
        if self.active:
            return acts
        self.active = True
        self.t_press = now
        return acts + ["begin"]

    def release(self, now):
        if self._swallow_release:
            self._swallow_release = False
            return []
        if not self.active or self.locked:
            return []
        if self.mode == "hold":
            self.active = False
            return ["end"]
        held = now - (self.t_press if self.t_press is not None else now)
        if held <= self.tap_max:
            self.deadline = now + self.double_tap
            return [("wait", self.deadline)]
        self.active = False
        return ["end"]

    def tick(self, now):
        if self.deadline is not None and now >= self.deadline and not self.locked:
            self.deadline = None
            self.active = False
            return ["cancel"]
        return []

    def force_idle(self):
        """Arrêt externe (sécurité, réconciliation) : on repart de zéro."""
        self.active = False
        self.locked = False
        self.deadline = None
        self._swallow_release = False


def stop(st):
    """v1.3.3 — Arrête un raccourci démarré par start() : moniteurs retirés,
    minuteurs annulés, threads libérés. Permet de RÉARMER à chaud quand la
    touche ou le mode change dans les Réglages. Idempotent, ne lève jamais."""
    if not st:
        return
    try:
        st["stopped"] = True
        fn = st.get("_stop")
        if fn is not None:
            fn()
    except Exception as e:
        print(f"[hotkey] arrêt KO (ignoré) : {e}")


def start(on_begin, on_end, mods=("ctrl", "cmd"), trigger_vk=None, max_seconds=600.0,
          chord_vk=None, mode="hold", on_cancel=None, on_lock=None):
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
    st = {"active": False, "safety": None, "monitors": [], "stopped": False,
          "mode": "tap" if mode == "tap" else "hold", "locked": False}
    _st_lock = threading.Lock()        # CONC-2 : protège st["active"] (run loop + Timer)
    machine = TapMachine(st["mode"])
    tap_timer = [None]                 # minuteur de l'attente d'un second appui

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
            if fn is None:             # sentinelle posée par stop()
                return
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
        while not st["stopped"]:
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
            st["locked"] = False
            machine.force_idle()
        print(f"[hotkey] sécurité : arrêt auto après {MAX:.0f}s (relâchement non reçu).")
        _async(on_end, "end(safety)")

    def _arm_safety_locked():
        try:
            if st["safety"] is not None:
                st["safety"].cancel()
            st["safety"] = threading.Timer(MAX, _safety)
            st["safety"].daemon = True
            st["safety"].start()
        except Exception:
            pass

    def _disarm_safety_locked():
        try:
            if st["safety"] is not None:
                st["safety"].cancel()
                st["safety"] = None
        except Exception:
            pass

    def _cancel_tap_timer_locked():
        t = tap_timer[0]
        tap_timer[0] = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def _apply(actions):
        """Exécute les actions rendues par la machine. Appelé SOUS _st_lock."""
        for a in actions:
            if isinstance(a, tuple) and a[0] == "wait":
                _cancel_tap_timer_locked()
                delay = max(0.0, a[1] - time.time())
                t = threading.Timer(delay, _tap_deadline)
                t.daemon = True
                tap_timer[0] = t
                t.start()
            elif a == "begin":
                st["active"] = True
                _arm_safety_locked()
                _async(on_begin, "begin")
            elif a == "end":
                st["active"] = False
                st["locked"] = False
                _cancel_tap_timer_locked()
                _disarm_safety_locked()
                _async(on_end, "end")
            elif a == "cancel":
                st["active"] = False
                _cancel_tap_timer_locked()
                _disarm_safety_locked()
                _async(on_cancel or on_end, "cancel")
            elif a == "lock":
                st["locked"] = True
                _cancel_tap_timer_locked()
                print("[hotkey] double appui : micro maintenu ouvert (mains libres).")
                if on_lock is not None:
                    _async(on_lock, "lock")
            elif a == "unlock":
                st["locked"] = False

    def _tap_deadline():
        with _st_lock:
            _apply(machine.tick(time.time()))

    def _begin():
        with _st_lock:
            _apply(machine.press(time.time()))

    def _end():
        with _st_lock:
            _apply(machine.release(time.time()))

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
            stuck = st["active"] and not held and not machine.locked and not machine.pending
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
        while not st["stopped"]:
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

    def _stop():
        # Retire les moniteurs sur le main thread (là où ils ont été posés),
        # coupe la dictée en cours proprement, annule les minuteurs, libère le
        # consommateur de la file. Après ça, plus aucun événement n'arrive ici.
        def _remove():
            for m in st["monitors"]:
                try:
                    NSEvent.removeMonitor_(m)
                except Exception:
                    pass
            st["monitors"] = []
        _on_main(_remove)
        with _st_lock:
            if st["active"]:
                _apply(["end"])
            _cancel_tap_timer_locked()
            _disarm_safety_locked()
            machine.force_idle()
        _q.put((None, "stop"))
        print(f"[hotkey] raccourci arrêté (mode {st['mode']}).")
    st["_stop"] = _stop

    return st
