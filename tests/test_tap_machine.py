"""Double appui (v1.3.3) : la machine à états qui transforme appuis et
relâchements du raccourci en actions de dictée.

Ce qui est garanti ici :
  - en mode « maintenir », rien ne change : appui = début, relâchement = fin ;
  - en mode « double appui », maintenir marche toujours, deux appuis brefs
    verrouillent le micro ouvert, l'appui suivant termine, un appui bref isolé
    est annulé (rien à transcrire) ;
  - la machine ignore la touche : Ctrl + Cmd, Fn, Cmd droite ou Ctrl + Espace
    produisent exactement la même séquence d'actions ;
  - la réconciliation anti micro-bloqué ne coupe jamais un micro volontairement
    maintenu ouvert.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hotkey_mac  # noqa: E402
from hotkey_mac import TapMachine  # noqa: E402

T = {"flags": 12, "down": 10, "up": 11}
CTRL, CMD, FN = 1 << 18, 1 << 20, 1 << 23


def sans_attente(acts):
    """Les actions sans l'échéance, pour comparer des séquences."""
    return [a for a in acts if not (isinstance(a, tuple) and a[0] == "wait")]


class HoldModeTests(unittest.TestCase):
    def test_hold_is_unchanged(self):
        m = TapMachine("hold")
        self.assertEqual(m.press(0.0), ["begin"])
        self.assertEqual(m.press(0.1), [], "un second appui pendant la dictée ne fait rien")
        self.assertEqual(m.release(2.0), ["end"])
        self.assertEqual(m.release(2.1), [], "un relâchement sans dictée ne fait rien")
        self.assertFalse(m.locked)

    def test_unknown_mode_falls_back_to_hold(self):
        self.assertEqual(TapMachine("n'importe quoi").mode, "hold")


class TapModeTests(unittest.TestCase):
    def setUp(self):
        self.m = TapMachine("tap")

    def test_long_hold_still_dictates(self):
        self.assertEqual(self.m.press(0.0), ["begin"])
        self.assertEqual(self.m.release(1.2), ["end"])
        self.assertFalse(self.m.active)

    def test_single_short_tap_is_cancelled_after_the_wait(self):
        self.assertEqual(self.m.press(0.0), ["begin"])
        acts = self.m.release(0.2)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0][0], "wait")
        self.assertAlmostEqual(acts[0][1], 0.2 + hotkey_mac.DOUBLE_TAP_S)
        self.assertTrue(self.m.pending)
        self.assertEqual(self.m.tick(0.5), [], "avant l'échéance : on attend encore")
        self.assertEqual(self.m.tick(0.2 + hotkey_mac.DOUBLE_TAP_S), ["cancel"])
        self.assertFalse(self.m.active)
        self.assertFalse(self.m.pending)

    def test_double_tap_locks_then_next_press_ends(self):
        self.assertEqual(self.m.press(0.0), ["begin"])
        self.assertEqual(sans_attente(self.m.release(0.2)), [])
        self.assertEqual(self.m.press(0.4), ["lock"])
        self.assertTrue(self.m.locked)
        self.assertTrue(self.m.active, "la dictée commencée au premier appui continue")
        self.assertEqual(self.m.release(0.5), [], "le relâchement de l'appui qui verrouille ne compte pas")
        self.assertEqual(self.m.tick(5.0), [], "aucune échéance ne coupe un micro verrouillé")
        self.assertEqual(self.m.press(9.0), ["end", "unlock"])
        self.assertFalse(self.m.locked)
        self.assertFalse(self.m.active)
        self.assertEqual(self.m.release(9.1), [], "le relâchement de l'appui qui termine ne relance rien")
        self.assertEqual(self.m.press(10.0), ["begin"], "et on peut repartir normalement")

    def test_expired_wait_before_a_late_press_is_cancelled_first(self):
        # Le minuteur n'a pas encore tiré, mais l'échéance est passée : ce n'est
        # PAS un double appui, c'est un tap annulé puis une nouvelle dictée.
        self.m.press(0.0); self.m.release(0.2)
        self.assertEqual(self.m.press(0.2 + hotkey_mac.DOUBLE_TAP_S + 0.3), ["cancel", "begin"])
        self.assertFalse(self.m.locked)

    def test_long_second_press_in_the_window_still_locks(self):
        self.m.press(0.0); self.m.release(0.2)
        self.assertEqual(self.m.press(0.5), ["lock"])
        self.assertEqual(self.m.release(3.0), [], "peu importe la durée de l'appui qui verrouille")
        self.assertTrue(self.m.locked)

    def test_force_idle_resets_everything(self):
        self.m.press(0.0); self.m.release(0.2); self.m.press(0.4)
        self.assertTrue(self.m.locked)
        self.m.force_idle()
        self.assertFalse(self.m.locked)
        self.assertFalse(self.m.active)
        self.assertFalse(self.m.pending)
        self.assertEqual(self.m.press(1.0), ["begin"])

    def test_reconcile_conditions_are_exposed(self):
        # La réconciliation coupe une dictée « active et modificateurs relâchés »,
        # SAUF si le micro est verrouillé ou si un tap attend son second appui.
        self.m.press(0.0); self.m.release(0.2)
        self.assertTrue(self.m.active and self.m.pending)
        self.m.press(0.4)
        self.assertTrue(self.m.active and self.m.locked and not self.m.pending)


class ShortcutIndependenceTests(unittest.TestCase):
    """La machine ne voit que des appuis et des relâchements : quelle que soit
    la touche décodée par decide(), la séquence d'actions est la même."""

    def _rejouer(self, evenements, **decide_kw):
        m = TapMachine("tap")
        out = []
        for t, ev in evenements:
            action = hotkey_mac.decide(**dict(decide_kw, T=T, **ev))
            if action == "begin":
                out += m.press(t)
            elif action == "end":
                out += m.release(t)
        return sans_attente(out) + (["LOCKED"] if m.locked else [])

    def test_same_gestures_same_actions_for_every_shortcut(self):
        # Un double appui, puis un appui pour terminer.
        chord_ctrl_cmd = dict(want=CTRL | CMD, chord=True, trigger_vk=None, chord_vk=None)
        ev_chord = [(0.0, dict(event_type=T["flags"], key_code=59, flags=CTRL | CMD)),
                    (0.2, dict(event_type=T["flags"], key_code=59, flags=CMD)),
                    (0.4, dict(event_type=T["flags"], key_code=59, flags=CTRL | CMD)),
                    (0.5, dict(event_type=T["flags"], key_code=59, flags=0)),
                    (9.0, dict(event_type=T["flags"], key_code=59, flags=CTRL | CMD)),
                    (9.1, dict(event_type=T["flags"], key_code=59, flags=0))]
        fn = dict(want=FN, chord=True, trigger_vk=None, chord_vk=hotkey_mac.FN_KEYCODE)
        ev_fn = [(t, dict(event_type=T["flags"], key_code=hotkey_mac.FN_KEYCODE,
                          flags=FN if (i % 2 == 0) else 0)) for i, (t, _) in enumerate(ev_chord)]
        right_cmd = dict(want=CMD, chord=True, trigger_vk=None, chord_vk=hotkey_mac.RIGHT_CMD_KEYCODE)
        ev_rc = [(t, dict(event_type=T["flags"], key_code=hotkey_mac.RIGHT_CMD_KEYCODE,
                          flags=CMD if (i % 2 == 0) else 0)) for i, (t, _) in enumerate(ev_chord)]
        ctrl_space = dict(want=CTRL, chord=False, trigger_vk=49, chord_vk=None)
        ev_cs = [(t, dict(event_type=T["down"] if (i % 2 == 0) else T["up"], key_code=49, flags=CTRL))
                 for i, (t, _) in enumerate(ev_chord)]

        attendu = ["begin", "lock", "end", "unlock", ]
        for nom, ev, kw in (("Ctrl + Cmd", ev_chord, chord_ctrl_cmd), ("Fn", ev_fn, fn),
                            ("Cmd droite", ev_rc, right_cmd), ("Ctrl + Espace", ev_cs, ctrl_space)):
            self.assertEqual(self._rejouer(ev, **kw), attendu, nom)

    def test_a_cursor_key_never_locks_the_fn_shortcut(self):
        # Flèche gauche porte le drapeau Fn : elle ne doit ni démarrer ni verrouiller.
        fn = dict(want=FN, chord=True, trigger_vk=None, chord_vk=hotkey_mac.FN_KEYCODE)
        ev = [(0.0, dict(event_type=T["flags"], key_code=123, flags=FN)),
              (0.2, dict(event_type=T["flags"], key_code=123, flags=0)),
              (0.4, dict(event_type=T["flags"], key_code=123, flags=FN))]
        self.assertEqual(self._rejouer(ev, **fn), [])


class StartAndStopTests(unittest.TestCase):
    def test_start_exposes_mode_and_stop_is_idempotent(self):
        st = hotkey_mac.start(lambda: None, lambda: None, mods=("ctrl", "cmd"), mode="tap")
        self.assertIsNotNone(st)
        self.assertEqual(st["mode"], "tap")
        self.assertFalse(st["stopped"])
        hotkey_mac.stop(st)
        self.assertTrue(st["stopped"])
        hotkey_mac.stop(st)            # deux fois : sans effet, sans erreur
        hotkey_mac.stop(None)          # rien : sans erreur


if __name__ == "__main__":
    unittest.main()
