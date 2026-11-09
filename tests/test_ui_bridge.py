"""v1.3.4 — Pont Python -> tableau de bord.

app.py pilote l'interface par _ui("if(typeof X==='function')X(...)"). Le script
du tableau de bord est une fonction fermée : une fonction qui n'est pas posée sur
window n'existe pas pour ce test, et l'appel est ignoré sans bruit. Ce garde-fou
vérifie que chaque nom appelé depuis Python est bien exposé.
"""
import os
import re
import sys
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app_sources  # noqa: E402


class TestPontUI(unittest.TestCase):
    def test_toute_fonction_appelee_par_python_est_exposee(self):
        app = app_sources.texte_complet()
        html = open(os.path.join(RACINE, "vlocal-interface.html"), encoding="utf-8").read()
        noms = sorted(set(re.findall(r"typeof ([A-Za-z_]\w*)==='function'", app)))
        self.assertTrue(noms, "aucun appel _ui trouvé : le motif a changé ?")
        manquants = [n for n in noms if not re.search(r"window\.%s\s*=" % re.escape(n), html)]
        self.assertEqual(manquants, [], "fonctions appelées par Python mais absentes de window : %s" % manquants)


if __name__ == "__main__":
    unittest.main()
