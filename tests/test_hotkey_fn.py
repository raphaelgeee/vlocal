"""Raccourcis à touche unique (v1.2.0) : Fn, Cmd droite, Option droite.

Le menu des Réglages proposait ces trois choix alors que le backend ne les
connaissait pas et retombait en silence sur Ctrl + Cmd. Ces tests fixent la
décision : la bonne touche démarre, une autre touche portant le même drapeau
ne démarre jamais."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hotkey_mac  # noqa: E402

T = {"flags": 12, "down": 10, "up": 11}          # NSEventType réels
FN = 1 << 23                                     # NSEventModifierFlagFunction
CTRL, CMD = 1 << 18, 1 << 20
ARROW_LEFT = 123


def d(**kw):
    base = dict(event_type=T["flags"], key_code=hotkey_mac.FN_KEYCODE, flags=0,
                want=FN, chord=True, trigger_vk=None, chord_vk=hotkey_mac.FN_KEYCODE, T=T)
    base.update(kw)
    return hotkey_mac.decide(**base)


class FnHotkeyTests(unittest.TestCase):
    def test_fn_pressed_starts_and_released_stops(self):
        self.assertEqual(d(flags=FN), "begin")
        self.assertEqual(d(flags=0), "end")

    def test_arrow_key_never_starts_a_dictation(self):
        # une flèche porte le drapeau Function : sans le filtre par keyCode,
        # elle démarrerait une dictée
        self.assertIsNone(d(key_code=ARROW_LEFT, flags=FN))
        self.assertIsNone(d(event_type=T["down"], key_code=ARROW_LEFT, flags=FN))

    def test_other_modifiers_alone_do_nothing(self):
        self.assertIsNone(d(key_code=CTRL and 59, flags=CTRL))

    def test_chord_ctrl_cmd_unchanged(self):
        chord = dict(event_type=T["flags"], key_code=59, want=CTRL | CMD, chord=True,
                     trigger_vk=None, chord_vk=None, T=T)
        self.assertEqual(hotkey_mac.decide(flags=CTRL | CMD, **chord), "begin")
        self.assertEqual(hotkey_mac.decide(flags=CTRL, **chord), "end")

    def test_key_combo_unchanged(self):
        combo = dict(want=CTRL, chord=False, trigger_vk=49, chord_vk=None, T=T)
        self.assertEqual(hotkey_mac.decide(event_type=T["down"], key_code=49,
                                           flags=CTRL, **combo), "begin")
        self.assertIsNone(hotkey_mac.decide(event_type=T["down"], key_code=48,
                                            flags=CTRL, **combo))
        self.assertEqual(hotkey_mac.decide(event_type=T["up"], key_code=49,
                                           flags=CTRL, **combo), "end")
        self.assertEqual(hotkey_mac.decide(event_type=T["flags"], key_code=59,
                                           flags=0, **combo), "end")

    def test_unknown_event_is_ignored(self):
        self.assertIsNone(d(event_type=99))

    def test_right_cmd_only_and_not_left(self):
        right = dict(event_type=T["flags"], want=CMD, chord=True, trigger_vk=None,
                     chord_vk=hotkey_mac.RIGHT_CMD_KEYCODE, T=T)
        self.assertEqual(hotkey_mac.decide(key_code=54, flags=CMD, **right), "begin")
        self.assertEqual(hotkey_mac.decide(key_code=54, flags=0, **right), "end")
        self.assertIsNone(hotkey_mac.decide(key_code=55, flags=CMD, **right),
                          "Cmd gauche ne doit rien déclencher")

    def test_right_option_only_and_not_left(self):
        OPT = 1 << 19
        right = dict(event_type=T["flags"], want=OPT, chord=True, trigger_vk=None,
                     chord_vk=hotkey_mac.RIGHT_OPT_KEYCODE, T=T)
        self.assertEqual(hotkey_mac.decide(key_code=61, flags=OPT, **right), "begin")
        self.assertIsNone(hotkey_mac.decide(key_code=58, flags=OPT, **right),
                          "Option gauche ne doit rien déclencher")

    def test_every_menu_choice_exists_in_the_backend(self):
        """Le menu des Réglages et la table du backend ne doivent jamais diverger."""
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html = open(os.path.join(root, "vlocal-interface.html"), encoding="utf-8").read()
        menu = set(re.findall(r"\{v:'([a-z_]+)'\}", html))
        app = open(os.path.join(root, "app.py"), encoding="utf-8").read()
        block = app[app.index("    HOTKEYS = {"):]
        backend = set(re.findall(r'^\s+"([a-z_]+)":\s+\(\{', block[:block.index("}\n")], re.M))
        self.assertTrue(menu, "menu introuvable")
        self.assertEqual(menu - backend, set(),
                         "des choix du menu n'existent pas côté backend")


if __name__ == "__main__":
    unittest.main()
