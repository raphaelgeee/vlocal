"""Lot propreté — les modules issus d'app.py restent rangés.

- aucun module n'importe app.py (app.py assemble, il n'est importé par personne) ;
- les imports de premier niveau entre modules du dépôt ne forment aucun cycle ;
- chaque module s'importe seul, dans un interpréteur neuf (aucune dépendance
  cachée à l'ordre d'import d'app.py) ;
- aucune fonction de plus de 150 lignes dans ces modules, sauf exception
  justifiée ci-dessous ;
- aucun module de plus de 1 500 lignes, app.py sous 1 200 ;
- aucune exception avalée en silence : un « except Exception » journalise,
  signale ou relance (les échecs volontairement ignorés vont en DEBUG).
"""
import ast
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

import app_sources

RACINE = app_sources.RACINE
LOCAUX = {f[:-3] for f in os.listdir(RACINE) if f.endswith(".py")}
MODULES = [f[:-3] for f in app_sources.FICHIERS]

# Fonctions longues gardées d'un bloc, avec la raison. Toute nouvelle entrée
# doit être justifiée ici.
LONGUES = {
    # Pipeline séquentiel de fin de réunion (finalisation, diarisation guidée,
    # replis, attribution, enregistrement) couvert seulement par le smoke-test
    # de build hors dépôt : découpé sans filet, il risquerait une régression
    # silencieuse sur les réunions des utilisateurs.
    "_reunion_transcribe_pipeline_inner",
    # Escalade de l'ouverture du micro et finalisation FIFO : chaque étape
    # partage des variables d'état locales ; couvertes par test_incidents et
    # test_mic_app, à découper dans un lot dédié avec ces tests pour filet.
    "start", "_do_finalize",
    # Démarrage du raccourci global : fermetures (callbacks NSEvent) qui
    # partagent l'état de l'appui ; vérifiable seulement dans l'app réelle.
    "start_global_hotkey",
    # Démarrage d'une réunion : transitions atomiques sous _meeting_lock.
    "reunion_start",
}


def _imports(arbre, premier_niveau_seulement):
    noeuds = arbre.body if premier_niveau_seulement else list(ast.walk(arbre))
    for n in noeuds:
        if isinstance(n, ast.Import):
            for a in n.names:
                yield a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            yield n.module.split(".")[0]


def _arbre(module):
    return ast.parse(app_sources.texte(module + ".py"), module + ".py")


class TestModulesApp(unittest.TestCase):
    def test_personne_n_importe_app(self):
        fautifs = [m for m in MODULES if m != "app"
                   and "app" in set(_imports(_arbre(m), False))]
        self.assertEqual(fautifs, [])

    def test_aucun_cycle_d_import(self):
        graphe = {m: {i for i in _imports(_arbre(m), True) if i in LOCAUX and i != m}
                  for m in LOCAUX}
        etat, chemin = {}, []

        def visite(m):
            etat[m] = "en cours"
            chemin.append(m)
            for v in sorted(graphe.get(m, ())):
                if etat.get(v) == "en cours":
                    self.fail("cycle d'import : " + " -> ".join(chemin[chemin.index(v):] + [v]))
                if v not in etat:
                    visite(v)
            chemin.pop()
            etat[m] = "fini"

        for m in sorted(graphe):
            if m not in etat:
                visite(m)

    def test_import_a_froid(self):
        def importe(m):
            r = subprocess.run([sys.executable, "-c", f"import {m}"], cwd=RACINE,
                               capture_output=True, text=True, timeout=120)
            return m, r.returncode, r.stderr[-800:]

        with ThreadPoolExecutor(max_workers=4) as pool:
            echecs = [(m, err) for m, rc, err in pool.map(importe, MODULES) if rc != 0]
        self.assertEqual(echecs, [])

    def test_fonctions_courtes(self):
        """Une fonction justifiée couvre ses fermetures (fonctions imbriquées)."""
        longues = []

        def visite(noeud, m, justifiee):
            for n in ast.iter_child_nodes(noeud):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    taille = n.end_lineno - n.lineno + 1
                    ok = justifiee or n.name in LONGUES
                    if taille > 150 and not ok:
                        longues.append(f"{m}.{n.name} ({taille} lignes)")
                    visite(n, m, ok)
                else:
                    visite(n, m, justifiee)

        for m in MODULES:
            visite(_arbre(m), m, False)
        self.assertEqual(longues, [])

    def test_aucune_exception_avalee(self):
        signale = {"debug", "info", "warning", "error", "exception", "critical",
                   "log", "print", "_toast"}

        def large(h):
            types = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
            return h.type is None or any(isinstance(t, ast.Name) and t.id in
                                         ("Exception", "BaseException") for t in types)

        def muet(h):
            for n in ast.walk(ast.Module(body=h.body, type_ignores=[])):
                if isinstance(n, ast.Raise):
                    return False
                if isinstance(n, ast.Call):
                    f = n.func
                    if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) in signale:
                        return False
            return True

        muets = [f"{m}:{h.lineno}" for m in MODULES for h in ast.walk(_arbre(m))
                 if isinstance(h, ast.ExceptHandler) and large(h) and muet(h)]
        self.assertEqual(muets, [])

    def test_modules_declares_a_pyinstaller(self):
        spec = app_sources.texte("Vlocal.spec")
        manquants = [m for m in MODULES if m != "app" and f'"{m}"' not in spec]
        self.assertEqual(manquants, [])

    def test_tailles_des_modules(self):
        tailles = {m: app_sources.texte(m + ".py").count("\n") for m in MODULES}
        self.assertLessEqual(tailles["app"], 1200)
        trop = {m: t for m, t in tailles.items() if t > 1500}
        self.assertEqual(trop, {})


if __name__ == "__main__":
    unittest.main()
