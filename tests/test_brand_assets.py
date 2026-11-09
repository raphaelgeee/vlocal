"""Marque Vlocal : les fichiers livrés découlent de la construction unique
(tools/brand_paths.py), l'icône est écrite et lue au même chemin, et l'ancienne
marque (le V) a disparu du code."""
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "tools"))
import brand_paths  # noqa: E402
import make_icon    # noqa: E402



def _png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", head[16:24])


class TestMarque(unittest.TestCase):
    def test_ancien_glyphe_supprime(self):
        """notifier.py lit le glyphe de la barre des menus : l'ancien PNG est parti."""
        self.assertFalse(os.path.exists(os.path.join(RACINE, "assets", "vocal-glyph-v-36.png")))

    def test_svg_livres_conformes_a_la_construction(self):
        for path, subpaths in ((make_icon.WORDMARK_SVG, brand_paths.wordmark()),
                               (make_icon.SYMBOL_SVG, brand_paths.symbol())):
            with open(path, encoding="utf-8") as f:
                livre = f.read()
            self.assertEqual(livre, brand_paths.svg_document(subpaths, "Vlocal"),
                             "%s périmé : relancer tools/make_icon.py --assets" % path.name)
            self.assertIn('fill="currentColor"', livre)

    def test_proportions_du_mot(self):
        x0, y0, x1, y1 = brand_paths.bounds(brand_paths.wordmark())
        # Mot de référence : 576 x 161 px d'encre (rapport 3,58).
        self.assertAlmostEqual((x1 - x0) / (y1 - y0), 3.58, delta=0.03)

    def test_gabarits_barre_des_menus(self):
        for px, path in make_icon.MENUBAR_PNGS.items():
            self.assertTrue(path.exists(), path)
            self.assertEqual(_png_size(path), (px, px))
        menubar = open(os.path.join(RACINE, "menubar.py"), encoding="utf-8").read()
        self.assertIn('f"vlocal-menubar-{pt}.png"', menubar)

    def test_icone_ecrite_et_lue_au_meme_chemin(self):
        spec = open(os.path.join(RACINE, "Vlocal.spec"), encoding="utf-8").read()
        lu = re.search(r'icon="([^"]+)"', spec).group(1)
        ecrit = os.path.relpath(make_icon.ICNS_PATH, RACINE)
        self.assertEqual(lu, ecrit)
        build = open(os.path.join(RACINE, "build_app.sh"), encoding="utf-8").read()
        self.assertIn("$PY tools/make_icon.py\n", build)

    @unittest.skipUnless(sys.platform == "darwin", "AppKit, sips et iconutil : macOS")
    def test_icone_produite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = make_icon.build_icns(os.path.join(tmp, "AppIcon.icns"))
            with open(out, "rb") as f:
                self.assertEqual(f.read(4), b"icns")
            self.assertGreater(os.path.getsize(out), 50_000)   # 10 tailles, jusqu'à 1024

    def test_ancienne_marque_absente(self):
        suivis = subprocess.run(["git", "ls-files"], cwd=RACINE, capture_output=True,
                                text=True, check=True).stdout.split()
        fautifs = []
        for rel in suivis:
            if rel.startswith("tests/test_brand"):
                continue
            if not rel.endswith((".py", ".html", ".sh", ".spec", ".css", ".svg", ".json")):
                continue
            path = os.path.join(RACINE, rel)
            if not os.path.exists(path):
                continue
            txt = open(path, encoding="utf-8", errors="ignore").read()
            if re.search(r"vocal-glyph|vocal-logo|_appicon_1024|tools/AppIcon", txt):
                fautifs.append(rel)
        self.assertEqual(fautifs, [])


if __name__ == "__main__":
    unittest.main()
