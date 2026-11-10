"""Constat 1 — settings.json ne doit jamais être vidé ni perdre de clé.

Avant : la lecture-modification-écriture de _save_settings n'était pas sous
verrou et l'écriture tronquait le fichier avant de le remplir. Quatre secondes
d'écritures concurrentes laissaient 2 clés sur 66 et les lecteurs voyaient {}.
Tout se passe dans un dossier temporaire : le vrai dossier de support n'est
jamais touché.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings as S  # noqa: E402


class _DossierTemporaire(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vlocal_test_settings_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        fichier = os.path.join(self.dir, "settings.json")
        for p in (mock.patch.object(S, "_APP_SUPPORT", self.dir),
                  mock.patch.object(S, "_SETTINGS_FILE", fichier),
                  mock.patch.object(S, "_MODE_FILE", os.path.join(self.dir, "mode.txt")),
                  mock.patch.object(S, "_GLOSSARY_FILE", os.path.join(self.dir, "glossary.txt")),
                  mock.patch.dict(S._SETTINGS_CACHE, {"mtime": None, "data": None})):
            p.start()
            self.addCleanup(p.stop)
        self.fichier = fichier


class TestEcrituresConcurrentes(_DossierTemporaire):
    def test_aucune_cle_perdue_aucune_lecture_vide(self):
        base = {"onboarded": True, "hotkey": "ctrl+cmd", "dictation_speed": "fidele",
                "audio_device_name": "Jabra", **{f"k{i}": "x" * 40 for i in range(60)}}
        with open(self.fichier, "w", encoding="utf-8") as f:
            json.dump(base, f)
        stop = time.time() + 2.0
        mauvaises, lectures = [0], [0]

        def lecteur():
            while time.time() < stop:
                d = S._load_settings()
                lectures[0] += 1
                if d.get("onboarded") is not True:
                    mauvaises[0] += 1

        def ecrivain(cle):
            n = 0
            while time.time() < stop:
                S._save_settings({cle: n})
                n += 1

        fils = [threading.Thread(target=lecteur) for _ in range(2)]
        fils += [threading.Thread(target=ecrivain, args=(k,))
                 for k in ("last_used_at", "history_scope")]
        for t in fils:
            t.start()
        for t in fils:
            t.join()
        with open(self.fichier, encoding="utf-8") as f:
            final = json.load(f)
        self.assertGreater(lectures[0], 0)
        self.assertEqual(mauvaises[0], 0, "un lecteur a vu un fichier vide ou tronqué")
        self.assertEqual(len(final), len(base) + 2, "des clés ont été perdues")


class TestEcritureAtomique(_DossierTemporaire):
    def test_echec_en_cours_d_ecriture_garde_l_ancien_fichier(self):
        S._save_settings({"onboarded": True, "hotkey": "fn"})

        def _casse(*a, **k):
            if len(a) > 1 and hasattr(a[1], "write"):
                a[1].write("{")          # json.dump : écrit à moitié puis casse
            raise OSError("disque plein")

        with mock.patch.object(S.json, "dump", _casse), \
                mock.patch.object(S.json, "dumps", _casse):
            S._save_settings({"hotkey": "ctrl"})
        with open(self.fichier, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"onboarded": True, "hotkey": "fn"})
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])

    def test_mode_et_glossaire_passent_par_l_ecriture_atomique(self):
        with mock.patch.object(S, "write_text_atomic", wraps=S.write_text_atomic) as w:
            S._set_mode("REUNION")
            with mock.patch.dict(sys.modules, {"engine": mock.Mock()}):
                S._save_glossary_text("Vlocal")
        self.assertEqual([c.args[0] for c in w.call_args_list],
                         [S._MODE_FILE, S._GLOSSARY_FILE])
        self.assertEqual(S._current_mode(), "REUNION")


class TestAucuneEcritureDirecteDansLesFichiersDEtat(unittest.TestCase):
    """Les fichiers d'état de l'équipe passent par write_text_atomic."""

    def test_pas_d_open_en_ecriture(self):
        racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import re
        for nom in ("settings.py", "api_meetings.py"):
            src = open(os.path.join(racine, nom), encoding="utf-8").read()
            self.assertIsNone(re.search(r"(?<![\w.])open\([^)]*[\"']w[\"']", src),
                              f"{nom} : écriture non atomique")


if __name__ == "__main__":
    unittest.main()


class TestMicroHerite143(unittest.TestCase):
    """Réglage 1.4.3 (index seul) : nom retrouvé une fois via CoreAudio, jamais PortAudio."""

    def _run(self, settings, names):
        import startup
        saved = []

        class FakeCA:
            def device_name_at(self, i):
                return names[i] if 0 <= i < len(names) else None

        with mock.patch.object(startup, "_load_settings", lambda: dict(settings)), \
                mock.patch.object(startup, "_save_settings", saved.append), \
                mock.patch.dict(sys.modules, {"sounddevice": None}):
            startup._migrate_legacy_audio_device(open_coreaudio=FakeCA)
        return saved

    def test_index_resolved_to_its_name_once(self):
        saved = self._run({"audio_device": 1}, ["Haut-parleurs", "Micro USB"])
        self.assertEqual(saved, [{"audio_device_name": "Micro USB"}])
        self.assertEqual(self._run({"audio_device": 1, "audio_device_name": "Micro USB"},
                                   ["Haut-parleurs", "Micro USB"]), [])

    def test_unknown_index_falls_back_to_system_default(self):
        saved = self._run({"audio_device": 9}, ["Micro intégré"])
        self.assertEqual(saved, [{"audio_device": None, "audio_device_name": None}])

    def test_nothing_chosen_nothing_written(self):
        self.assertEqual(self._run({"audio_device": None}, []), [])

    def test_started_at_launch(self):
        import inspect
        import startup
        self.assertIn("_migrate_legacy_audio_device", inspect.getsource(startup.after_start))
