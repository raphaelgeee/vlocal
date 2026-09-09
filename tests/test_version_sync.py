"""Le fichier VERSION et APP_VERSION ne doivent jamais diverger (v1.2.0).

Le 9 septembre 2026, un build a produit « Vlocal-1.1.2.dmg » alors que l'app
s'annonçait en 1.2.0 : le DMG est nommé d'après VERSION (lu par Vlocal.spec)
et l'app se compare à app_versions avec APP_VERSION. Deux sources, une seule
vérité attendue.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VersionSyncTests(unittest.TestCase):
    def test_version_file_matches_app_version(self):
        version = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
        app = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        m = re.search(r'^APP_VERSION = "([0-9.]+)"', app, re.M)
        self.assertIsNotNone(m, "APP_VERSION introuvable")
        self.assertEqual(version, m.group(1),
                         "VERSION (nom du DMG) et APP_VERSION (mise à jour) divergent")

    def test_version_is_three_numbers(self):
        version = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_changelog_mentions_current_version(self):
        version = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
        changelog = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
        self.assertIn("## " + version, changelog, "version absente du CHANGELOG")


if __name__ == "__main__":
    unittest.main()
