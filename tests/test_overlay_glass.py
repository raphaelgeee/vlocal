"""Verre natif de la pilule : choix du matériau, cadre et rayon pour chaque
état, accord de la courbe et de la durée avec la page, et animation réelle du
verre dans une fenêtre hors écran (jamais affichée)."""
import json
import os
import re
import sys
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
import overlay  # noqa: E402
from overlay import GLASS_MARGIN, PANEL_H, PANEL_W, PILL_BOTTOM, choose_material, glass_frame  # noqa: E402


def _html():
    with open(os.path.join(RACINE, "dictation_overlay.html"), encoding="utf-8") as f:
        return f.read()


def geom_page():
    return json.loads(re.search(r"var GEOM = (\{.*?\});", _html(), flags=re.S).group(1))


class TestMateriau(unittest.TestCase):
    def test_ordre_de_preference(self):
        cas = {
            # (Liquid Glass, visual effect, réduire la transparence) -> matériau
            (True, True, False): "glass",
            (False, True, False): "visual",
            (False, False, False): "opaque",
            (True, True, True): "opaque",
            (False, True, True): "opaque",
            (True, False, False): "glass",
        }
        for flags, attendu in cas.items():
            self.assertEqual(choose_material(*flags), attendu, flags)


class TestCadreDuVerre(unittest.TestCase):
    def setUp(self):
        g = geom_page()
        p, c = g["pill"], g["card"]
        mark_w = 17

        def even(v):                     # la page arrondit au pair (centrage au pixel)
            return 2 * -(-v // 2)
        rec = p["pad"] + mark_w + p["gap"] + p["wave"] + p["pad"] + 2
        # Formes de chaque état, recomposées depuis la table de la page
        # (libellés de 80 et 150 px, carte de deux lignes).
        self.etats = {
            "repos": g["idle"],
            "ecoute": {"w": even(rec), "h": p["h"], "r": p["r"]},
            "mains_libres": {"w": even(rec + p["gap"] + 4 + 150), "h": p["h"], "r": p["r"]},
            "transcription": {"w": p["solo"], "h": p["h"], "r": p["r"]},
            "resultat": {"w": even(p["pad"] + mark_w + p["gap"] + 80 + p["pad"] + 2), "h": p["h"], "r": p["r"]},
            "carte": {"w": c["max"], "h": 36 + 2 * c["vpad"], "r": c["r"]},
        }

    def test_chaque_etat(self):
        for nom, g in self.etats.items():
            x, y, w, h, r = glass_frame(g)
            self.assertEqual((w, h, r), (g["w"], g["h"], min(g["r"], g["h"] / 2)), nom)
            self.assertAlmostEqual(x + w / 2, PANEL_W / 2, msg=nom)            # centré
            self.assertEqual(y, PILL_BOTTOM, nom)                               # même bas que le CSS
            self.assertEqual(x, int(x), nom)                                    # pixel entier
            self.assertLessEqual(y + h, PANEL_H - GLASS_MARGIN, nom)

    def test_capsules(self):
        for nom in ("repos", "ecoute", "transcription", "resultat"):
            x, y, w, h, r = glass_frame(self.etats[nom])
            self.assertEqual(r, h / 2, nom)

    def test_bornes(self):
        x, y, w, h, r = glass_frame({"w": 900, "h": 500, "r": 400})
        self.assertEqual(w, PANEL_W - 2 * GLASS_MARGIN)
        self.assertEqual(h, PANEL_H - PILL_BOTTOM - GLASS_MARGIN)
        self.assertEqual(r, h / 2)
        self.assertEqual(glass_frame({"w": -3, "h": 0, "r": 5})[2:], (0.0, 0.0, 0.0))


class TestAccordPageVerre(unittest.TestCase):
    def test_duree_et_courbe(self):
        css = _html()
        self.assertIn("--t:%dms;" % overlay.GLASS_MS, css)
        courbe = "--curve:cubic-bezier(%s);" % ",".join(("%g" % v).lstrip("0") or "0"
                                                      for v in overlay.GLASS_CURVE)
        self.assertIn(courbe, css)

    def test_bas_de_la_pilule(self):
        self.assertIn("bottom:%dpx;" % PILL_BOTTOM, _html())

    def test_repos_initial(self):
        src = open(os.path.join(RACINE, "overlay.py"), encoding="utf-8").read()
        g = geom_page()["idle"]
        self.assertIn('self._geom = {"w": %.1f, "h": %.1f, "r": %.1f}' % (g["w"], g["h"], g["r"]), src)

    def test_bulle_identique_au_symbole(self):
        with open(os.path.join(RACINE, "assets", "vlocal-symbol.svg"), encoding="utf-8") as f:
            svg = f.read()
        d_asset = re.search(r' d="([^"]+)"', svg).group(1)
        vb_asset = re.search(r'viewBox="([^"]+)"', svg).group(1)
        html = _html()
        self.assertIn('id="bubble" fill-rule="evenodd" d="%s"' % d_asset, html)
        self.assertIn('<svg viewBox="%s">' % vb_asset, html)


@unittest.skipUnless(sys.platform == "darwin", "AppKit : macOS")
class TestAnimationNative(unittest.TestCase):
    """Le verre suit cadre et rayon, pour chaque matériau, dans une fenêtre
    hors écran : rien ne s'affiche."""

    def test_le_verre_atteint_la_forme(self):
        import AppKit
        from Foundation import NSDate, NSMakeRect, NSRunLoop
        AppKit.NSApplication.sharedApplication()
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), 0, AppKit.NSBackingStoreBuffered, False)
        hote = AppKit.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, PANEL_H))
        hote.setWantsLayer_(True)
        win.setContentView_(hote)
        materiaux = ["visual", "opaque"] + (["glass"] if overlay._glass_class() else [])
        for m in materiaux:
            x, y, w, h, r = glass_frame({"w": 36, "h": 22, "r": 11})
            verre = overlay._Glass(m, "dark", (x, y, w, h), r)
            hote.addSubview_(verre.view)
            x, y, w, h, r = glass_frame({"w": 146, "h": 36, "r": 18})
            verre.animate((x, y, w, h), r, overlay.GLASS_MS / 1000.0)
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.45))
            f = verre.view.frame()
            self.assertEqual((f.origin.x, f.origin.y, f.size.width, f.size.height), (x, y, w, h), m)
            rayon = verre.view.cornerRadius() if m == "glass" else verre.rounded[0].cornerRadius()
            self.assertAlmostEqual(rayon, r, places=3, msg=m)
            verre.view.removeFromSuperview()
        win.close()


if __name__ == "__main__":
    unittest.main()
