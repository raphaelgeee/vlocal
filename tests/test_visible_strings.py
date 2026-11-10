"""Chaînes visibles du backend : toutes traduites, aucune ne porte de tiret cadratin.

Analyse statique (AST) de tous les modules Python de l'app. Une chaîne est
« visible » quand elle part à l'écran :
  - argument littéral de `_te(...)`, `_toast(...)`, `_msg(...)`,
    `notifier.show(...)`, `overlay.info(...)` / `overlay.error(...)` ;
  - valeur littérale d'une clé "error" dans un dict (renvoyé à l'interface,
    traduit par `_api_safe`).

Règles :
  - un littéral passé à `_te`/`_toast` (directement ou via un "error") a une
    entrée exacte dans `texts._BT` ou commence par une clé de `texts._BT_PREFIX` ;
  - une f-string n'est acceptée que si son début littéral est une clé de
    `_BT_PREFIX` (sinon elle doit passer par `_msg(fr, en)`) ;
  - `notifier.show` et `overlay.*` ne reçoivent jamais de littéral brut ;
  - aucune chaîne visible (ni le dictionnaire `_BT` lui-même, ni les exports de
    réunion) ne contient « — ».
"""
import ast
import glob
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import texts  # noqa: E402
DASH = "—"
TRANSLATED = {"_te", "_toast"}
RAW_FORBIDDEN = {("notifier", "show"), ("overlay", "info"), ("overlay", "error")}


def _modules():
    return sorted(glob.glob(os.path.join(ROOT, "*.py")))


def _name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id, None
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f.attr, f.value.id
    return None, None


def _lead(js):
    """Début littéral d'une f-string (avant le premier champ interpolé)."""
    out = ""
    for v in js.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            out += v.value
        else:
            break
    return out


def _all_text(node):
    """Tout le texte littéral d'une chaîne ou f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def _known(s):
    return s in texts._BT or any(s.startswith(p) for p in texts._BT_PREFIX)


def scan():
    """Renvoie (non_traduits, bruts, tirets) : listes de 'fichier:ligne : texte'."""
    untranslated, raw, dashes = [], [], []
    for path in _modules():
        rel = os.path.basename(path)
        tree = ast.parse(open(path, encoding="utf-8").read(), rel)
        for node in ast.walk(tree):
            visible, check = [], []
            if isinstance(node, ast.Call):
                fn, owner = _name(node)
                args = list(node.args)
                if fn in TRANSLATED and owner is None and args:
                    visible.append(args[0])
                    check.append(args[0])
                elif fn == "_msg" and owner is None:
                    visible.extend(args)
                elif (owner, fn) in RAW_FORBIDDEN:
                    visible.extend(args)
                    for a in args:
                        if _all_text(a) is not None:
                            raw.append(f"{rel}:{a.lineno} : {_all_text(a)[:70]}")
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "error":
                        visible.append(v)
                        check.append(v)
            for a in check:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    if a.value and not _known(a.value):
                        untranslated.append(f"{rel}:{a.lineno} : {a.value[:70]}")
                elif isinstance(a, ast.JoinedStr):
                    if not any(_lead(a).startswith(p) for p in texts._BT_PREFIX):
                        untranslated.append(f"{rel}:{a.lineno} : f\"{_lead(a)[:60]}…\"")
            for a in visible:
                t = _all_text(a)
                if t and DASH in t:
                    dashes.append(f"{rel}:{a.lineno} : {t[:70]}")
    return untranslated, raw, dashes


class VisibleStringsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.untranslated, cls.raw, cls.dashes = scan()

    def test_every_translated_literal_has_an_english_entry(self):
        self.assertEqual(self.untranslated, [], "\n".join(self.untranslated))

    def test_notifier_and_overlay_never_get_a_raw_literal(self):
        self.assertEqual(self.raw, [], "\n".join(self.raw))

    def test_no_em_dash_in_visible_strings(self):
        self.assertEqual(self.dashes, [], "\n".join(self.dashes))

    def test_no_em_dash_in_backend_dictionary(self):
        bad = [s for pair in texts._BT.items() for s in pair if DASH in s]
        bad += [s for pair in texts._BT_PREFIX.items() for s in pair if DASH in s]
        self.assertEqual(bad, [])

    def test_meeting_exports_have_no_em_dash(self):
        from api_meetings import MeetingsApi
        m = {"titre": "Point", "created_at": "2026-11-10",
             "transcription_brute": "Bonjour."}
        data = {"blocks": [{"speaker": "SPEAKER_00", "start": 0, "end": 1,
                            "text": "Bonjour."}], "names": {}}
        for fmt in ("md", "txt", "srt"):
            for d in (data, None):
                out = MeetingsApi._export_content(MeetingsApi, m, d, fmt) or ""
                self.assertNotIn(DASH, out, fmt)

    def test_backend_dictionary_has_no_duplicate_key(self):
        tree = ast.parse(open(os.path.join(ROOT, "texts.py"), encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and node.targets[0].id in ("_BT", "_BT_PREFIX"):
                keys = [k.value for k in node.value.keys]
                dup = sorted({k for k in keys if keys.count(k) > 1})
                self.assertEqual(dup, [], node.targets[0].id)

    def test_every_aria_label_of_the_dashboard_is_translated(self):
        import re
        html = open(os.path.join(ROOT, "vlocal-interface.html"), encoding="utf-8").read()
        bad = [tag for tag in re.findall(r"<[^>]*\baria-label=\"[^\"]+\"[^>]*>", html)
               if 'aria-label="Vlocal"' not in tag and "data-i18n-aria=" not in tag
               and "aria-label=\"'+" not in tag]
        self.assertEqual(bad, [])


class ObsidianErrorTests(unittest.TestCase):
    """Le chemin Obsidian ne remonte jamais un texte d'exception à l'écran."""
    SECRET = "/Users/secret/Permission denied"

    def setUp(self):
        import tempfile
        self.vault = tempfile.mkdtemp(prefix="vlocal-obs-")
        self.addCleanup(__import__("shutil").rmtree, self.vault, True)

    def _boom(self, *a, **k):
        raise PermissionError(13, self.SECRET)

    def test_fixed_phrases_are_translated(self):
        import obsidian
        for c in (obsidian.ERR_EMPTY, obsidian.ERR_NO_VAULT, obsidian.ERR_WRITE,
                  obsidian.ERR_CREATE):
            self.assertIn(c, texts._BT)

    def test_write_failures_return_a_fixed_phrase(self):
        from unittest import mock
        import obsidian
        with mock.patch.object(obsidian, "_append_under_section", self._boom), \
                self.assertLogs("vlocal.obsidian", "WARNING") as logs:
            r1 = obsidian.capture("une idée", self.vault)
            r2 = obsidian.capture_reminder("payer", "demain", self.vault)
        self.assertEqual((r1["error"], r2["error"]), (obsidian.ERR_WRITE,) * 2)
        self.assertIn(self.SECRET, "\n".join(logs.output))
        with mock.patch("builtins.open", self._boom), self.assertLogs("vlocal.obsidian"):
            r3 = obsidian.capture_meeting({"id": 1, "titre": "x"}, self.vault)
        self.assertEqual(r3["error"], obsidian.ERR_WRITE)

    def test_vault_creation_failure_reaches_the_ui_as_a_fixed_phrase(self):
        from unittest import mock
        import appstate
        import obsidian
        from api_settings import SettingsApi
        blocker = os.path.join(self.vault, "fichier")
        open(blocker, "w").close()           # un fichier là où le coffre doit naître
        with mock.patch.object(obsidian, "default_voice_vault_path",
                               lambda: os.path.join(blocker, "Vlocal")), \
                mock.patch.object(appstate, "ui_lang", "en"), \
                self.assertLogs("vlocal.obsidian"):
            r = SettingsApi.create_obsidian_vault(SettingsApi())
        self.assertEqual(r, {"ok": False, "error": "Could not create the vault."})


if __name__ == "__main__":
    unittest.main()
