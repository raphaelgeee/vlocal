"""Jetons de marque uniques : assets/brand.css fait foi ; le tableau de bord le
charge, la pilule de dictée en recopie le bloc à l'identique, le courriel de
version et l'icône en reprennent les valeurs."""
import os
import re
import sys
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "tools"))
import make_icon  # noqa: E402


def _lire(nom):
    with open(os.path.join(RACINE, nom), encoding="utf-8") as f:
        return f.read()


def jetons(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return dict((k, v.strip()) for k, v in re.findall(r"(--vl-[\w-]+)\s*:\s*([^;]+);", css))


class TestJetons(unittest.TestCase):
    def setUp(self):
        self.source = jetons(_lire("assets/brand.css"))

    def test_source_complete(self):
        self.assertEqual(sorted(self.source), sorted([
            "--vl-night", "--vl-night-rgb", "--vl-white", "--vl-accent", "--vl-accent-rgb",
            "--vl-accent-deep", "--vl-accent-deep-rgb"]))
        for nom in ("night", "accent", "accent-deep"):
            hexa = self.source["--vl-" + nom].lstrip("#")
            rgb = ",".join(str(int(hexa[i:i + 2], 16)) for i in (0, 2, 4))
            self.assertEqual(self.source["--vl-%s-rgb" % nom], rgb, nom)

    def test_dashboard_charge_la_source_sans_la_redefinir(self):
        html = _lire("vlocal-interface.html")
        self.assertIn('<link rel="stylesheet" href="assets/brand.css">', html)
        self.assertEqual(jetons(html), {})

    def test_overlay_identique(self):
        self.assertEqual(jetons(_lire("dictation_overlay.html")), self.source)

    def test_courriel(self):
        mail = _lire("mail/nouvelle-version.html").upper()
        for nom in ("--vl-night", "--vl-white", "--vl-accent"):
            self.assertIn(self.source[nom].upper(), mail, nom)
        # Anciennes teintes hors marque (fond noir neutre, accent violet).
        for ancien in ("#08090D", "#9D7BFF"):
            self.assertNotIn(ancien, mail)

    def test_icone(self):
        r, g, b = (round(c * 255) for c in make_icon.NIGHT)
        self.assertEqual("#%02X%02X%02X" % (r, g, b), self.source["--vl-night"].upper())

    def test_police_geist_une_seule_source(self):
        html = _lire("vlocal-interface.html")
        self.assertNotIn("data:font/woff2;base64", html)
        for f in ("geist-latin.woff2", "geist-latin-ext.woff2"):
            self.assertIn("url(assets/fonts/%s)" % f, html)
            self.assertTrue(os.path.exists(os.path.join(RACINE, "assets", "fonts", f)))


if __name__ == "__main__":
    unittest.main()
