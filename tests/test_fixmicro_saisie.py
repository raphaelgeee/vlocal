"""Lot fix-micro, saisie et cycle de vie (Fiabilité #10, #11, #12 ; Micro #9, #12).

A. Dictées enchaînées : le presse-papier d'origine (image comprise) est capturé
   au premier appui de la chaîne et rendu une seule fois, à la fin.
B. Relance pendant la fermeture de l'instance précédente : on réessaie le
   verrou quelques secondes avant de passer la main.
C. Consommateur figé du raccourci : le secours prend le relais, l'ancien
   s'efface en repartant ; un seul consommateur, ordre des jobs garanti.
D. Limite de sécurité de 600 s : mains libres uniquement, avertissement avant.
E. Réveil pendant une dictée : renouvellement du micro au prochain repos.

Aucun vrai presse-papier, clavier, micro ni AppKit : tout est doublé.
"""
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hotkey_mac  # noqa: E402
import inserter  # noqa: E402
import lifecycle  # noqa: E402


def _wait(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# --------------------------------------------------------------------------- #
# Doublure du presse-papier macOS (NSPasteboard / NSPasteboardItem / NSData)
# --------------------------------------------------------------------------- #
class _Item:
    def __init__(self, reps=None):
        self.reps = dict(reps or {})

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def types(self):
        return list(self.reps)

    def dataForType_(self, t):
        return self.reps.get(t)

    def setData_forType_(self, data, t):
        self.reps[t] = bytes(data)
        return True


class _Pasteboard:
    def __init__(self):
        self.items = []
        self.change = 0

    def changeCount(self):
        return self.change

    def clearContents(self):
        self.items = []
        self.change += 1

    def setString_forType_(self, s, t):
        self.items = [_Item({t: s.encode("utf-8")})]
        return True

    def stringForType_(self, t):
        for it in self.items:
            if t in it.reps:
                return it.reps[t].decode("utf-8")
        return None

    def pasteboardItems(self):
        return list(self.items)

    def writeObjects_(self, objs):
        self.items = list(objs)
        return True

    def user_copies(self, reps):
        """Une autre app (ou l'utilisateur) copie quelque chose."""
        self.items = [_Item(reps)]
        self.change += 1


def _fake_appkit_clipboard(pb):
    appkit = types.ModuleType("AppKit")
    appkit.NSPasteboard = types.SimpleNamespace(generalPasteboard=lambda: pb)
    appkit.NSPasteboardTypeString = "public.utf8-plain-text"
    appkit.NSPasteboardItem = _Item
    foundation = types.ModuleType("Foundation")
    foundation.NSData = types.SimpleNamespace(dataWithBytes_length_=lambda b, n: bytes(b[:n]))
    return {"AppKit": appkit, "Foundation": foundation}


class ChainedDictationClipboardTests(unittest.TestCase):
    """A — Fiabilité #10."""

    PNG = {"public.png": b"\x89PNG-image-de-l-utilisateur"}

    def setUp(self):
        self.pb = _Pasteboard()
        self.pasted = []
        patches = [
            mock.patch.dict(sys.modules, _fake_appkit_clipboard(self.pb)),
            mock.patch.object(inserter, "_secure_input_active", return_value=False),
            mock.patch.object(inserter, "_paste_keystroke", side_effect=self._paste),
            mock.patch.object(inserter, "RESTORE_DELAY_S", 0.15),
            mock.patch("permissions.accessibility_ok", return_value=True),
            mock.patch("clipboard._IS_MAC", True),
            mock.patch("clipboard._own_change", [None]),
            mock.patch.dict(inserter._chain, {"snap": None, "gen": 0}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _paste(self):
        self.pasted.append(self.pb.stringForType_("public.utf8-plain-text"))
        return True

    def _contents(self):
        return [dict(it.reps) for it in self.pb.items]

    def test_two_chained_dictations_give_back_the_original_image(self):
        self.pb.user_copies(self.PNG)
        inserter.begin_chain()                      # premier appui
        self.assertTrue(inserter.insert_at_cursor("un"))
        inserter.begin_chain()                      # second appui, avant la restauration
        self.assertTrue(inserter.insert_at_cursor("deux"))
        self.assertEqual(self.pasted, ["un", "deux"])
        self.assertTrue(_wait(lambda: self._contents() == [self.PNG]),
                        f"presse-papier final : {self._contents()}")

    def test_chain_never_restores_the_previous_dictation(self):
        self.pb.user_copies({"public.utf8-plain-text": "original".encode()})
        inserter.begin_chain()
        inserter.insert_at_cursor("un")
        time.sleep(0.3)                             # la restauration de « un » a eu lieu
        inserter.begin_chain()
        inserter.insert_at_cursor("deux")
        time.sleep(0.3)
        self.assertEqual(self.pb.stringForType_("public.utf8-plain-text"), "original")

    def test_user_copy_between_dictations_is_respected_and_becomes_the_new_origin(self):
        self.pb.user_copies({"public.utf8-plain-text": "A".encode()})
        inserter.begin_chain()
        inserter.insert_at_cursor("un")
        self.pb.user_copies({"public.utf8-plain-text": "B".encode()})
        time.sleep(0.3)
        self.assertEqual(self.pb.stringForType_("public.utf8-plain-text"), "B",
                         "une copie de l'utilisateur n'est jamais écrasée")
        inserter.begin_chain()
        inserter.insert_at_cursor("deux")
        self.assertTrue(_wait(lambda: self.pb.stringForType_("public.utf8-plain-text") == "B"))

    def test_empty_original_keeps_the_dictated_text(self):
        inserter.begin_chain()
        inserter.insert_at_cursor("un")
        time.sleep(0.3)
        self.assertEqual(self.pb.stringForType_("public.utf8-plain-text"), "un")


# --------------------------------------------------------------------------- #
# B — Fiabilité #11
# --------------------------------------------------------------------------- #
class SingleInstanceRetryTests(unittest.TestCase):
    def test_lock_freed_by_a_closing_instance_lets_us_start(self):
        with mock.patch("instance_lock.acquire", side_effect=[None, None, ["h"]]) as acq:
            r = lifecycle._acquire_single_instance()   # vrai délai : deux pas courts
        self.assertEqual(r, ["h"])
        self.assertEqual(acq.call_count, 3)

    def test_gives_up_after_the_bounded_delay(self):
        now = [0.0]
        naps = []

        def sleep(s):
            naps.append(s)
            now[0] += s

        with mock.patch("instance_lock.acquire", return_value=None):
            r = lifecycle._acquire_single_instance(sleep=sleep, clock=lambda: now[0])
        self.assertIsNone(r)
        self.assertGreaterEqual(now[0], 3.0)
        self.assertLessEqual(now[0], 5.0)
        self.assertTrue(all(s <= 0.5 for s in naps))

    def test_free_lock_is_taken_at_once(self):
        with mock.patch("instance_lock.acquire", return_value=["h"]) as acq:
            r = lifecycle._acquire_single_instance(sleep=self.fail)
        self.assertEqual(r, ["h"])
        self.assertEqual(acq.call_count, 1)


# --------------------------------------------------------------------------- #
# C — Fiabilité #12
# --------------------------------------------------------------------------- #
class PumpReliefTests(unittest.TestCase):
    def test_frozen_consumer_steps_aside_and_order_is_kept(self):
        now = [100.0]
        pump = hotkey_mac._SerialPump(job_max_s=8.0, clock=lambda: now[0])
        self.addCleanup(pump.stop)
        log = []
        log_lock = threading.Lock()
        unfreeze = threading.Event()

        def job(name, wait=None, nap=0.0):
            def fn():
                with log_lock:
                    log.append(("start", name, threading.current_thread().name))
                if wait is not None:
                    wait.wait(5)
                time.sleep(nap)
                with log_lock:
                    log.append(("done", name, threading.current_thread().name))
            return fn

        pump.put(job("begin1", wait=unfreeze), "begin")
        self.assertTrue(_wait(lambda: any(e[1] == "begin1" for e in log)))
        self.assertFalse(pump.check_stall(), "pas encore figé")
        now[0] += 9.0
        self.assertTrue(pump.check_stall(), "figé au-delà de 8 s -> secours")
        self.assertFalse(pump.check_stall(), "un seul secours par job figé")
        pump.put(job("end1"), "end")
        self.assertTrue(_wait(lambda: ("done", "end1") in [(e[0], e[1]) for e in log]))
        unfreeze.set()                                  # le consommateur figé repart
        self.assertTrue(_wait(lambda: ("done", "begin1") in [(e[0], e[1]) for e in log]))
        self.assertTrue(_wait(lambda: pump.consumers == 1),
                        f"consommateurs actifs : {pump.consumers}")
        for i in range(5):
            pump.put(job(f"begin{i + 2}", nap=0.01), "begin")
            pump.put(job(f"end{i + 2}", nap=0.01), "end")
        self.assertTrue(_wait(lambda: sum(1 for e in log if e[0] == "done") == 12))
        tail = [(e[0], e[1]) for e in log if e[1] not in ("begin1", "end1")]
        expected = []
        for i in range(5):
            for n in (f"begin{i + 2}", f"end{i + 2}"):
                expected += [("start", n), ("done", n)]
        self.assertEqual(tail, expected, "jobs exécutés un par un, dans l'ordre")
        self.assertEqual(len({e[2] for e in log if e[1] not in ("begin1", "end1")}), 1)


# --------------------------------------------------------------------------- #
# D — Micro #12 : doublure d'AppKit pour hotkey_mac.start()
# --------------------------------------------------------------------------- #
CTRL, CMD = 1 << 18, 1 << 20


class _Event:
    def __init__(self, flags, key_code=59):
        self._flags = flags
        self._kc = key_code

    def type(self):
        return 12

    def keyCode(self):
        return self._kc

    def modifierFlags(self):
        return self._flags


def _fake_appkit_hotkey(handlers, held):
    appkit = types.ModuleType("AppKit")
    appkit.NSEventModifierFlagControl = CTRL
    appkit.NSEventModifierFlagCommand = CMD
    appkit.NSEventModifierFlagOption = 1 << 19
    appkit.NSEventModifierFlagShift = 1 << 17
    appkit.NSEventModifierFlagFunction = 1 << 23
    appkit.NSEventTypeFlagsChanged = 12
    appkit.NSEventTypeKeyDown = 10
    appkit.NSEventTypeKeyUp = 11
    appkit.NSEventMaskFlagsChanged = 1 << 12
    appkit.NSEventMaskKeyDown = 1 << 10
    appkit.NSEventMaskKeyUp = 1 << 11

    def add_global(mask, fn):
        handlers.append(fn)
        return object()

    appkit.NSEvent = types.SimpleNamespace(
        addGlobalMonitorForEventsMatchingMask_handler_=add_global,
        addLocalMonitorForEventsMatchingMask_handler_=lambda m, fn: object(),
        removeMonitor_=lambda m: None,
        modifierFlags=lambda: held[0])
    foundation = types.ModuleType("Foundation")
    foundation.NSOperationQueue = types.SimpleNamespace(
        mainQueue=lambda: types.SimpleNamespace(addOperationWithBlock_=lambda fn: fn()))
    appserv = types.ModuleType("ApplicationServices")
    appserv.AXIsProcessTrusted = lambda: True
    return {"AppKit": appkit, "Foundation": foundation, "ApplicationServices": appserv}


class SafetyLimitTests(unittest.TestCase):
    def setUp(self):
        self.handlers = []
        self.held = [0]
        p = mock.patch.dict(sys.modules, _fake_appkit_hotkey(self.handlers, self.held))
        p.start()
        self.addCleanup(p.stop)
        self.p_mac = mock.patch.object(hotkey_mac, "_IS_MAC", True)
        self.p_mac.start()
        self.addCleanup(self.p_mac.stop)
        self.calls = []

    def _cb(self, name):
        return lambda: self.calls.append(name)

    def _key(self, down):
        self.held[0] = (CTRL | CMD) if down else 0
        self.handlers[0](_Event(self.held[0]))

    def test_hold_mode_is_never_cut_by_the_safety_limit(self):
        st = hotkey_mac.start(self._cb("begin"), self._cb("end"), mods=("ctrl", "cmd"),
                              max_seconds=0.3, mode="hold")
        self.addCleanup(hotkey_mac.stop, st)
        self._key(True)
        self.assertTrue(_wait(lambda: "begin" in self.calls))
        time.sleep(0.8)
        self.assertEqual(self.calls, ["begin"], "maintien : jamais coupé par la limite")
        self._key(False)
        self.assertTrue(_wait(lambda: self.calls == ["begin", "end"]))

    def test_hands_free_warns_then_stops_at_the_limit(self):
        st = hotkey_mac.start(self._cb("begin"), self._cb("end"), mods=("ctrl", "cmd"),
                              max_seconds=0.6, mode="tap", on_lock=self._cb("lock"),
                              on_limit_warning=self._cb("warn"), warn_before_s=0.3)
        self.addCleanup(hotkey_mac.stop, st)
        self._key(True)
        self._key(False)          # appui bref
        self._key(True)           # second appui : mains libres
        self._key(False)
        self.assertTrue(_wait(lambda: "lock" in self.calls))
        self.assertTrue(_wait(lambda: "warn" in self.calls, 0.5))
        self.assertNotIn("end", self.calls, "avertir AVANT d'arrêter")
        self.assertTrue(_wait(lambda: "end" in self.calls, 1.0))
        self.assertEqual(self.calls, ["begin", "lock", "warn", "end"])

    def test_ending_hands_free_early_cancels_warning_and_limit(self):
        st = hotkey_mac.start(self._cb("begin"), self._cb("end"), mods=("ctrl", "cmd"),
                              max_seconds=0.4, mode="tap", on_lock=self._cb("lock"),
                              on_limit_warning=self._cb("warn"), warn_before_s=0.2)
        self.addCleanup(hotkey_mac.stop, st)
        for down in (True, False, True, False):
            self._key(down)
        self.assertTrue(_wait(lambda: "lock" in self.calls))
        self._key(True)           # appui suivant : termine
        self.assertTrue(_wait(lambda: "end" in self.calls))
        time.sleep(0.6)
        self.assertEqual(self.calls, ["begin", "lock", "end"])


# --------------------------------------------------------------------------- #
# E — Micro #9
# --------------------------------------------------------------------------- #
class _Engine:
    def __init__(self, recording):
        self.recording = recording
        self.calls = []

    def request_mic_renewal(self):
        self.calls.append("renewal")

    def mic_after_wake(self):
        self.calls.append("after_wake")


class WakeDuringDictationTests(unittest.TestCase):
    def _wake(self, engine):
        blocks = []
        appkit = types.ModuleType("AppKit")
        appkit.NSWorkspaceDidWakeNotification = "wake"
        center = types.SimpleNamespace(
            addObserverForName_object_queue_usingBlock_=lambda n, o, q, b: blocks.append(b) or b)
        appkit.NSWorkspace = types.SimpleNamespace(
            sharedWorkspace=lambda: types.SimpleNamespace(notificationCenter=lambda: center))
        with mock.patch.dict(sys.modules, {"AppKit": appkit}), \
                mock.patch.object(lifecycle, "WAKE_SETTLE_S", 0.01), \
                mock.patch.object(lifecycle, "_wake_observers", []):
            lifecycle._watch_system_wake(engine)
            blocks[0](None)
            self.assertTrue(_wait(lambda: engine.calls))
        return engine.calls

    def test_wake_during_dictation_asks_for_renewal_at_next_rest(self):
        self.assertEqual(self._wake(_Engine(recording=True)), ["renewal"])

    def test_wake_at_rest_renews_now(self):
        self.assertEqual(self._wake(_Engine(recording=False)), ["after_wake"])


if __name__ == "__main__":
    unittest.main()
