"""Constat 9 — aucun texte d'exception n'est montré à l'utilisateur.

Dans les modules de ce lot, une exception capturée (`except ... as e`) ne doit
jamais atteindre une valeur renvoyée au JavaScript ni un message d'interface
(_toast, _ui, _te, _msg) : l'utilisateur reçoit une phrase fixe FR/EN, le
détail part au journal. Exceptions admises : `e.message(lang)` des erreurs de
mise à jour et `e.user_message` des refus d'import, phrases fixes elles-mêmes.
"""
import ast
import os
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ("settings", "api_files", "api_meetings", "api_system", "api_settings",
           "startup", "recorder", "inserter", "engine_runtime", "reminder_flow",
           "ui_bridge", "meetings", "storage")
VERS_UI = {"_toast", "_ui", "_te", "_msg", "showToast"}


def _nom(f):
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _mentionne(noeud, var):
    permis = set()
    for n in ast.walk(noeud):
        # UpdateError.message(lang) et _ImportRefused.user_message : phrases fixes
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "message":
            permis.update(id(x) for x in ast.walk(n.func.value))
        if isinstance(n, ast.Attribute) and n.attr == "user_message":
            permis.update(id(x) for x in ast.walk(n.value))
    return any(isinstance(n, ast.Name) and n.id == var and id(n) not in permis
               for n in ast.walk(noeud))


def fuites(module):
    arbre = ast.parse(open(os.path.join(RACINE, module + ".py"), encoding="utf-8").read())
    out = []
    for h in ast.walk(arbre):
        if not (isinstance(h, ast.ExceptHandler) and h.name):
            continue
        for n in ast.walk(ast.Module(body=h.body, type_ignores=[])):
            cible = None
            if isinstance(n, ast.Return) and n.value is not None:
                cible = n.value
            elif isinstance(n, ast.Call) and _nom(n.func) in VERS_UI:
                cible = n
            if cible is not None and _mentionne(cible, h.name):
                out.append(f"{module}:{n.lineno}")
    return sorted(set(out))


class TestAucunTexteDExceptionALUtilisateur(unittest.TestCase):
    def test_modules_du_lot(self):
        trouve = [f for m in MODULES for f in fuites(m)]
        self.assertEqual(trouve, [], "texte d'exception montré à l'utilisateur")


if __name__ == "__main__":
    unittest.main()
