"""Lot propreté — textes de l'app (contrats 14 à 16).

- la carte d'erreur de la pilule n'invite pas deux fois à réessayer ;
- l'app vouvoie partout, journaux compris (« Vous » pour le locuteur du micro) ;
- le réglage du micro dit qu'il sert à la dictée comme aux réunions ;
- les pages de confidentialité (README, Réglages > Données partagées) disent que
  la cause technique d'un incident est transmise (contrat 17).
"""
import ast
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_sources  # noqa: E402
import texts  # noqa: E402

TUTOIEMENT = re.compile(
    r"\b(tu|toi|ton|ta|tes)\b|\bt'|-toi\b"
    r"|\b(maintiens|appuie|relâche|accorde|utilise le|vérifie|réessaie|recommence)\b",
    re.IGNORECASE)


def _chaines_hors_docstrings(arbre):
    docs = set()
    for n in ast.walk(arbre):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                docs.add(id(n.body[0].value))
    for n in ast.walk(arbre):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            yield n


class TestTextes(unittest.TestCase):
    def test_une_seule_invitation_a_reessayer(self):
        src = app_sources.texte("dictation.py")
        self.assertNotIn("La transcription a calé, réessayez.", src)
        self.assertIn('overlay.error(_te("La transcription a calé."))', src)
        self.assertEqual(texts._BT["La transcription a calé."], "The transcription stalled.")
        # La carte d'erreur ajoute elle-même l'invitation (ovError -> retry_hk).
        page = app_sources.texte("dictation_overlay.html")
        self.assertIn("tt('retry_hk', hotkey)", page)

    def test_vouvoiement(self):
        fautes = []
        for nom in app_sources.FICHIERS:
            arbre = ast.parse(app_sources.texte(nom))
            for c in _chaines_hors_docstrings(arbre):
                if TUTOIEMENT.search(c.value):
                    fautes.append(f"{nom}:{c.lineno}: {c.value[:80]!r}")
        self.assertEqual(fautes, [])

    def test_locuteur_du_micro_vouvoye(self):
        src = app_sources.texte("meetings.py")
        self.assertNotIn('"Toi"', src)
        self.assertEqual(src.count('"SPEAKER_00": "Vous"'), 2)

    def test_libelle_du_micro(self):
        page = app_sources.texte("vlocal-interface.html")
        self.assertNotIn("Micro de la réunion", page)
        self.assertNotIn("Meeting microphone", page)
        self.assertIn('"set.mic.d":"Entrée audio de la dictée et des réunions.', page)
        self.assertIn('"set.mic.d":"Audio input for dictation and meetings.', page)

    def test_confidentialite_annonce_la_cause_technique(self):
        import telemetry
        # Le code transmis porte bien l'étape : « code:where ».
        self.assertIn('f"{code}:{where}"', app_sources.texte("telemetry.py"))
        self.assertTrue(telemetry.__doc__)
        for readme, attendu in (("README.fr.md", "cause technique"),
                                ("README.md", "technical cause")):
            txt = app_sources.texte(readme)
            self.assertIn("mic_device_fail:start", txt)
            self.assertIn(attendu, txt)
        page = app_sources.texte("vlocal-interface.html")
        self.assertNotIn("jamais le détail d'un incident", page)
        self.assertNotIn("never the detail of an incident", page)
        self.assertEqual(page.count("avec leur cause technique"), 2)
        self.assertEqual(page.count("with their technical cause"), 1)


if __name__ == "__main__":
    unittest.main()
