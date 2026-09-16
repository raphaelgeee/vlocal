"""v1.4.1 : découpe de l'insertion progressive (effet d'écriture). Pure, sans clavier."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inserter  # noqa: E402


class TestMorceaux(unittest.TestCase):
    def test_reconstitution_exacte(self):
        for t in ["Bonjour Marie, la réunion est confirmée pour mardi.",
                  "Un train rapide qui est chassé par un avion en vol, avec des effets de mouvement et d'ombre dramatiques. " * 6,
                  "court", "Bonjour Marie,\n\nJe te propose de reporter la réunion.\n\nRaphael"]:
            self.assertEqual("".join(inserter._morceaux(t)), t)

    def test_bornes(self):
        self.assertEqual(len(inserter._morceaux("court")), 1)
        m = inserter._morceaux("mot " * 400)
        self.assertLessEqual(len(m), 40)
        self.assertGreaterEqual(min(len(x) for x in m), 12)
        m = inserter._morceaux("Bonjour Marie, la réunion est confirmée pour mardi à quatorze heures, salle Vendôme.")
        self.assertTrue(4 <= len(m) <= 16)


if __name__ == "__main__":
    unittest.main()
