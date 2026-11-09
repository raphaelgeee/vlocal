"""Lot propreté — pilule et réunions (contrats 9 et 10 du lot interface).

- la pilule de repos est masquée pendant une réunion et revient à la fin ;
  une dictée lancée pendant la réunion l'affiche le temps de la dictée ;
- l'API des réunions l'annonce au démarrage réussi et à l'arrêt ;
- l'ancrage sous l'icône de la barre des menus, devenu sans effet, a disparu.
"""
import ast
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_sources  # noqa: E402
import menubar  # noqa: E402
import overlay  # noqa: E402


class _FauxPanneau:
    def __init__(self):
        self.appels = []

    def show(self):
        self.appels.append("show")

    def conceal(self):
        self.appels.append("conceal")

    def js(self, code):
        self.appels.append(code)


class TestPiluleEnReunion(unittest.TestCase):
    def setUp(self):
        self.p = _FauxPanneau()
        patch = mock.patch.multiple(overlay, _overlay=self.p, _meeting=False)
        patch.start()
        self.addCleanup(patch.stop)

    def test_reunion_masque_puis_rend_la_pilule(self):
        overlay.set_meeting(True)
        self.assertEqual(self.p.appels, ["ovReset()", "conceal"])
        self.p.appels.clear()
        overlay.set_meeting(False)
        self.assertEqual(self.p.appels, ["show"])

    def test_fin_de_dictee_pendant_la_reunion_reste_masquee(self):
        overlay.set_meeting(True)
        self.p.appels.clear()
        overlay.hide()
        self.assertEqual(self.p.appels, ["ovReset()", "conceal"])

    def test_hors_reunion_la_pilule_de_repos_reste_visible(self):
        overlay.hide()
        self.assertEqual(self.p.appels, ["ovReset()", "show"])

    def test_sans_overlay_rien_ne_casse(self):
        with mock.patch.object(overlay, "_overlay", None):
            overlay.set_meeting(True)
            overlay.hide()
            overlay.set_meeting(False)


class TestBranchementApi(unittest.TestCase):
    def _appels(self, methode):
        fichier, classe = app_sources.definitions()["MeetingsApi"]
        fn = next(n for n in classe.body if getattr(n, "name", None) == methode)
        return [ast.unparse(c) for c in ast.walk(fn) if isinstance(c, ast.Call)
                and ast.unparse(c.func) == "overlay.set_meeting"]

    def test_demarrage_et_arret(self):
        self.assertEqual(self._appels("reunion_start"), ["overlay.set_meeting(True)"])
        self.assertEqual(self._appels("reunion_stop"), ["overlay.set_meeting(False)"])


class TestAncrageRetire(unittest.TestCase):
    def test_plus_d_ancrage(self):
        self.assertFalse(hasattr(overlay, "set_anchor_provider"))
        self.assertFalse(hasattr(menubar.MenuBar, "icon_frame"))
        self.assertNotIn("set_anchor_provider", app_sources.texte_complet())


if __name__ == "__main__":
    unittest.main()
