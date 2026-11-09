"""Lot propreté — icône de l'applet de notification (contrat 12 du lot interface).

L'applet « Vlocal » des notifications porte une icône dessinée au premier
lancement : le glyphe de la barre des menus, peint en blanc sur un carré
charbon. Rendu hors écran réel (AppKit sans fenêtre), dans un dossier
temporaire.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notifier  # noqa: E402


@unittest.skipUnless(sys.platform == "darwin", "rendu AppKit")
class TestIconeNotification(unittest.TestCase):
    def test_glyphe_de_la_barre_des_menus_visible(self):
        from AppKit import NSBitmapImageRep, NSImage
        with tempfile.TemporaryDirectory() as d:
            png = Path(d) / "icone.png"
            self.assertTrue(notifier._generate_icon_png(png))
            image = NSImage.alloc().initWithContentsOfFile_(str(png))
            rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        cote = rep.pixelsWide()
        blancs = 0
        for x in range(0, cote, 8):
            for y in range(0, cote, 8):
                c = rep.colorAtX_y_(x, y)
                if min(c.redComponent(), c.greenComponent(), c.blueComponent()) > 0.9 \
                        and c.alphaComponent() > 0.9:
                    blancs += 1
        # Le V couvre quelques pour cent du carré ; noir, il n'en couvrait aucun.
        self.assertGreater(blancs, 200)

    def test_glyphe_de_la_barre_des_menus_livre(self):
        self.assertTrue((notifier.ASSETS_DIR / "vlocal-menubar-36.png").exists())

    def test_bundle_reconstruit_chez_les_utilisateurs(self):
        self.assertNotEqual(notifier._BUNDLE_VERSION, "1")


class TestGitignore(unittest.TestCase):
    def test_plus_de_ligne_perimee(self):
        racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(racine, ".gitignore"), encoding="utf-8") as f:
            self.assertNotIn("!build/make_icon.py", f.read())


if __name__ == "__main__":
    unittest.main()
