"""Sources de l'application (app.py et les modules qui en sont issus).

Les tests qui vérifient le code livré sans démarrer l'app lisent ces fichiers :
définitions extraites telles quelles (ast) et exécutées dans un espace de noms
de doublures, ou motifs cherchés dans le texte. app.py a été découpé en modules
à responsabilité unique ; cette liste dit où chercher, et un nom ne doit être
défini qu'une fois dans l'ensemble (sinon l'extraction serait ambiguë).
"""
import ast
import os

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Dans l'ordre des dépendances : un module avant ceux qui l'importent.
FICHIERS = (
    "appstate.py",
    "texts.py",
    "settings.py",
    "ui_bridge.py",
    "lifecycle.py",
    "reminder_flow.py",
    "dictation.py",
    "meetings.py",
    "engine_runtime.py",
    "hotkeys.py",
    "api_system.py",
    "api_settings.py",
    "api_meetings.py",
    "api_files.py",
    "app.py",
)


def chemin(nom):
    return os.path.join(RACINE, nom)


def texte(nom):
    with open(chemin(nom), encoding="utf-8") as f:
        return f.read()


def texte_complet():
    """Le texte de tous les fichiers, concaténé (recherche de motifs)."""
    return "\n".join(texte(nom) for nom in FICHIERS)


def _nom_defini(n):
    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        return n.name
    if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
        return n.targets[0].id
    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
        return n.target.id
    return None


def definitions():
    """{nom: (fichier, noeud)} des définitions de premier niveau. Lève si un nom
    est défini dans deux fichiers."""
    vus = {}
    for nom_fichier in FICHIERS:
        arbre = ast.parse(texte(nom_fichier), chemin(nom_fichier))
        for n in arbre.body:
            nom = _nom_defini(n)
            if nom is None:
                continue
            if nom in vus and vus[nom][0] != nom_fichier:
                raise AssertionError(f"{nom} défini dans {vus[nom][0]} et {nom_fichier}")
            vus[nom] = (nom_fichier, n)
    return vus


def charge(noms, espace):
    """Exécute dans `espace` les définitions de premier niveau nommées dans
    `noms`, chacune compilée sous son fichier d'origine (traces lisibles)."""
    defs = definitions()
    manquants = set(noms) - set(defs)
    if manquants:
        raise AssertionError(f"définitions absentes des sources de l'app : {sorted(manquants)}")
    # Ordre du fichier, puis de la liste FICHIERS : une classe avant ses usages.
    rang = {nom: i for i, nom in enumerate(FICHIERS)}
    choisis = sorted((defs[nom] for nom in noms),
                     key=lambda fn: (rang[fn[0]], fn[1].lineno))
    for nom_fichier, noeud in choisis:
        code = compile(ast.Module(body=[noeud], type_ignores=[]), chemin(nom_fichier), "exec")
        exec(code, espace)
    return espace


def litteral(nom):
    """Valeur d'une affectation littérale de premier niveau."""
    defs = definitions()
    if nom not in defs:
        raise AssertionError(f"{nom} absent des sources de l'app")
    return ast.literal_eval(defs[nom][1].value)
