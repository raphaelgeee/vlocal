"""Aucun nom importé DANS une fonction ne doit y être utilisé AVANT la ligne
d'import.

Pourquoi ce test existe : en 1.2.0, `start_global_hotkey()` a utilisé
`hotkey_mac.FN_KEYCODE` alors que `import hotkey_mac` se trouvait plus bas dans
la même fonction. Python traite alors `hotkey_mac` comme une variable locale pas
encore définie : UnboundLocalError à CHAQUE lancement. Et comme cet appel est la
première ligne de `_after_start`, tout le reste sautait avec lui : plus de
raccourci global, plus de délégué d'application (impossible de rouvrir ou de
quitter Vlocal sans forcer), plus d'icône V, plus de chien de garde. Rien ne le
signalait : l'app s'ouvrait, la fenêtre marchait, le bouton Dicter aussi.

Le compilateur Python ne dit rien, les tests unitaires de `decide()` non plus.
Ce test lit l'arbre syntaxique de chaque fichier du projet et refuse le motif.
"""
import ast
import glob
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def imports_utilises_trop_tot(chemin):
    """Renvoie [(ligne d'usage, nom, fonction, ligne d'import)] pour un fichier."""
    arbre = ast.parse(open(chemin, encoding="utf-8").read(), chemin)
    fautes = []
    for fn in ast.walk(arbre):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        imports, usages = {}, []

        def visiter(noeud):
            for enfant in ast.iter_child_nodes(noeud):
                # Une fonction, une lambda ou une classe imbriquée a sa propre portée.
                if isinstance(enfant, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.Lambda, ast.ClassDef)):
                    continue
                if isinstance(enfant, (ast.Import, ast.ImportFrom)):
                    for alias in enfant.names:
                        nom = (alias.asname or alias.name).split(".")[0]
                        imports.setdefault(nom, enfant.lineno)
                if isinstance(enfant, ast.Name) and isinstance(enfant.ctx, ast.Load):
                    usages.append((enfant.id, enfant.lineno))
                visiter(enfant)

        visiter(fn)
        for nom, ligne in usages:
            if nom in imports and ligne < imports[nom]:
                fautes.append((ligne, nom, fn.name, imports[nom]))
    return fautes


class ImportsOrderTests(unittest.TestCase):
    def test_no_name_used_before_its_local_import(self):
        fichiers = sorted(glob.glob(os.path.join(ROOT, "*.py")))
        self.assertGreater(len(fichiers), 5, "aucun fichier source trouvé")
        fautes = []
        for chemin in fichiers:
            for ligne, nom, fonction, ligne_import in imports_utilises_trop_tot(chemin):
                fautes.append(f"{os.path.basename(chemin)}:{ligne} « {nom} » utilisé dans "
                              f"{fonction}() avant son import ligne {ligne_import}")
        self.assertEqual(fautes, [], "\n" + "\n".join(fautes))

    def test_the_detector_catches_the_1_2_0_bug(self):
        """Le détecteur lui-même doit attraper le motif exact qui a cassé la 1.2.0."""
        import tempfile
        code = ("def start_global_hotkey():\n"
                "    chord_vk = {'fn': hotkey_mac.FN_KEYCODE}\n"
                "    import hotkey_mac\n"
                "    return hotkey_mac.start()\n")
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(code)
        try:
            fautes = imports_utilises_trop_tot(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual([(n, fn) for _, n, fn, _ in fautes],
                         [("hotkey_mac", "start_global_hotkey")])


if __name__ == "__main__":
    unittest.main()
