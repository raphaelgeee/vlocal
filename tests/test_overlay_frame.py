"""Position de la pilule : centrée en bas de l'écran de la souris, dans le
cadre utile (au-dessus du Dock), calculée par overlay.pill_frame (pure)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from overlay import BOTTOM_GAP, PANEL_H, PANEL_W, pill_frame  # noqa: E402

# Écran principal 1512 x 982 (MacBook 14"), barre des menus 37 px, Dock en bas 70 px.
MAIN = ((0, 0, 1512, 982), (0, 70, 1512, 875))
# Second écran 2560 x 1440 à droite, sans Dock (le Dock reste sur le principal).
RIGHT = ((1512, -200, 2560, 1440), (1512, -200, 2560, 1415))
# Second écran à gauche (x négatifs), Dock à gauche sur celui-ci.
LEFT = ((-1920, 0, 1920, 1080), (-1840, 0, 1840, 1055))


def _dans(fr, vf):
    x, y, w, h = fr
    vx, vy, vw, vh = vf
    return vx <= x and x + w <= vx + vw and vy <= y and y + h <= vy + vh


class TestPositionPilule(unittest.TestCase):
    def verifier(self, frame, screen):
        _, vf = screen
        x, y, w, h = frame
        self.assertEqual((w, h), (PANEL_W, PANEL_H))
        self.assertAlmostEqual(x + w / 2, vf[0] + vf[2] / 2)      # centré
        self.assertAlmostEqual(y, vf[1] + BOTTOM_GAP)            # en bas, au-dessus du Dock
        self.assertTrue(_dans(frame, vf), frame)

    def test_un_ecran(self):
        self.verifier(pill_frame((700, 500), [MAIN]), MAIN)

    def test_deux_ecrans_souris_sur_le_second(self):
        self.verifier(pill_frame((2600, 300), [MAIN, RIGHT]), RIGHT)

    def test_deux_ecrans_souris_sur_le_principal(self):
        self.verifier(pill_frame((20, 900), [MAIN, RIGHT]), MAIN)

    def test_ecran_a_gauche_coordonnees_negatives(self):
        self.verifier(pill_frame((-100, 40), [MAIN, LEFT]), LEFT)

    def test_dock_lateral(self):
        # Dock à gauche : le cadre utile commence à x = 80 ; centré sur ce cadre.
        dock_gauche = ((0, 0, 1512, 982), (80, 0, 1432, 945))
        self.verifier(pill_frame((700, 500), [dock_gauche]), dock_gauche)

    def test_souris_hors_de_tout_ecran_prend_le_plus_proche(self):
        self.verifier(pill_frame((5000, 100), [MAIN, RIGHT]), RIGHT)
        self.verifier(pill_frame((-50, -50), [MAIN, RIGHT]), MAIN)

    def test_bordure_commune_premier_ecran(self):
        # x = 1512 appartient aux deux cadres : l'ordre (principal d'abord) tranche.
        self.verifier(pill_frame((1512, 400), [MAIN, RIGHT]), MAIN)

    def test_ecran_trop_petit_reste_dans_le_cadre(self):
        petit = ((0, 0, 300, 100), (0, 0, 300, 100))
        x, y, w, h = pill_frame((10, 10), [petit])
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)

    def test_aucun_ecran(self):
        self.assertIsNone(pill_frame((0, 0), []))


if __name__ == "__main__":
    unittest.main()
