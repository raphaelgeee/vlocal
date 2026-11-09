"""Un journal de module ne doit jamais être masqué par un nom local.

Pourquoi : le passage des print à un journal de module (`log = applog.get(...)`)
a laissé `_recognize_voices(voices_map, log=False)`. Le paramètre booléen
masquait le journal : dès qu'une voix nommée existait, `log.warning` levait
« 'bool' object has no attribute 'warning' » et toute la diarisation d'une
réunion était abandonnée en silence (vu par la porte de build du 10 nov. 2026).
"""
import ast
import glob
import os
import sys
import unittest
from unittest import mock

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)


def _noms_de_journal(arbre):
    noms = set()
    for n in arbre.body:
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call)):
            appel = ast.unparse(n.value.func)
            if appel.endswith("applog.get") or appel.endswith("getLogger"):
                noms.add(n.targets[0].id)
    return noms


class TestJournalJamaisMasque(unittest.TestCase):
    def test_aucun_parametre_ni_variable_locale_ne_masque_le_journal(self):
        fautes = []
        for chemin in sorted(glob.glob(os.path.join(RACINE, "*.py"))):
            arbre = ast.parse(open(chemin, encoding="utf-8").read())
            noms = _noms_de_journal(arbre)
            if not noms:
                continue
            for f in ast.walk(arbre):
                if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                args = {a.arg for a in f.args.args + f.args.kwonlyargs + f.args.posonlyargs}
                locaux = {n.id for n in ast.walk(f)
                          if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                for nom in noms & (args | locaux):
                    fautes.append(f"{os.path.basename(chemin)}:{f.lineno} masque `{nom}`")
        self.assertEqual(fautes, [])

    def test_voix_reconnue_en_mode_bavard_rend_le_nom(self):
        import meetings
        voix = mock.MagicMock()
        voix.load.return_value = [{"id": 1, "name": "Marie"}]
        voix.match.return_value = ({"id": 1, "name": "Marie"}, 0.91)
        with mock.patch.dict(sys.modules, {"voiceid": voix}), \
                mock.patch.object(meetings, "log") as journal:
            noms = meetings._recognize_voices({"A": {"centroid": [0.1, 0.2]}}, verbose=True)
        self.assertEqual(noms, {"A": "Marie"})
        journal.info.assert_called_once()

    def test_echec_de_la_base_des_voix_en_mode_bavard_ne_leve_pas(self):
        import meetings
        voix = mock.MagicMock()
        voix.load.side_effect = RuntimeError("base des voix illisible")
        with mock.patch.dict(sys.modules, {"voiceid": voix}), \
                mock.patch.object(meetings, "log") as journal:
            noms = meetings._recognize_voices({"A": {"centroid": [0.1]}}, verbose=True)
        self.assertEqual(noms, {})
        journal.warning.assert_called_once()

if __name__ == "__main__":
    unittest.main()
