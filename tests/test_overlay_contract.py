"""Contrat Python <-> pilule de dictée.

overlay.py pilote dictation_overlay.html par evaluateJavaScript ; une fonction
absente de la page est ignorée sans bruit (l'erreur JS ne remonte pas). Ce test
vérifie que chaque fonction ov* appelée par overlay.py existe dans la page, que
le contrat publié reste complet, et que chaque fonction d'overlay appelée par
app.py existe dans le module."""
import os
import re
import sys
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
import overlay  # noqa: E402
import app_sources  # noqa: E402

# Contrat publié aux autres lots : noms figés.
CONTRAT_JS = ("ovRecording", "ovLevel", "ovTranscribing", "ovResult", "ovTooShort",
              "ovTooShortTap", "ovLocked", "ovError", "ovMicNeeded", "ovInfo",
              "ovFadeOut", "ovReset", "ovTheme", "ovLang", "ovHotkey")


def _lire(nom):
    with open(os.path.join(RACINE, nom), encoding="utf-8") as f:
        return f.read()


def fonctions_exposees(html):
    return set(re.findall(r"window\.(ov\w+)\s*=\s*function", html))


def fonctions_appelees(py):
    return set(re.findall(r"\b(ov[A-Z]\w*)\(", py))


class TestContratOverlay(unittest.TestCase):
    def setUp(self):
        self.html = _lire("dictation_overlay.html")
        self.py = _lire("overlay.py")

    def test_chaque_appel_python_existe_dans_la_page(self):
        appelees = fonctions_appelees(self.py)
        self.assertTrue(appelees, "aucun appel ov* trouvé : le motif a changé ?")
        manquantes = sorted(appelees - fonctions_exposees(self.html))
        self.assertEqual(manquantes, [], "appelées par overlay.py, absentes de la page")

    def test_contrat_complet(self):
        self.assertEqual(sorted(set(CONTRAT_JS) - fonctions_exposees(self.html)), [])
        self.assertEqual(sorted(set(CONTRAT_JS) - fonctions_appelees(self.py)), [])

    def test_le_test_echoue_si_une_fonction_disparait(self):
        amputee = self.html.replace("window.ovLocked = function", "var ovLocked = function")
        self.assertIn("ovLocked", fonctions_appelees(self.py) - fonctions_exposees(amputee))

    def test_api_python_appelee_par_app(self):
        app = app_sources.texte_complet()
        noms = set(re.findall(r"\boverlay\.([a-z_]+)\(", app))
        self.assertTrue(noms)
        manquants = sorted(n for n in noms if not callable(getattr(overlay, n, None)))
        self.assertEqual(manquants, [])


if __name__ == "__main__":
    unittest.main()
