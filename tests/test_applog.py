"""Lot propreté — journal configuré une fois, jamais de fichier pendant les tests.

setup() redirige les descripteurs 1 et 2 du processus : il est exercé dans un
sous-processus, sur un dossier temporaire.
"""
import logging
import os
import subprocess
import sys
import tempfile
import unittest

import app_sources

RACINE = app_sources.RACINE


def _lit(chemin):
    with open(chemin, encoding="utf-8") as f:
        return f.read()


def _lance(code, dossier):
    return subprocess.run([sys.executable, "-c", code, dossier], cwd=RACINE,
                          capture_output=True, text=True, timeout=60)


class TestJournal(unittest.TestCase):
    def test_app_empaquetee_fichier_tournant_et_console(self):
        code = ("import sys, logging, applog\n"
                "p = applog.setup(frozen=True, log_dir=sys.argv[1])\n"
                "h = logging.getLogger('vlocal').handlers[0]\n"
                "assert (h.maxBytes, h.backupCount) == (applog.MAX_BYTES, applog.BACKUPS)\n"
                "assert applog.setup(frozen=True, log_dir=sys.argv[1]) is None  # une seule fois\n"
                "applog.get('dictation').info('[diag] dictée 2.0s')\n"
                "applog.get('dictation').debug('invisible au niveau INFO')\n"
                "print('message natif ou print restant')\n")
        with tempfile.TemporaryDirectory() as d:
            r = _lance(code, d)
            self.assertEqual(r.returncode, 0, r.stderr)
            journal, console = (_lit(os.path.join(d, n)) for n in ("vlocal.log", "console.log"))
        self.assertIn("INFO vlocal.dictation.<module>: [diag] dictée 2.0s", journal)
        self.assertIn("journal de diagnostic actif", journal)
        self.assertNotIn("invisible", journal)
        self.assertIn("message natif ou print restant", console)

    def test_developpement_console_sans_fichier(self):
        code = ("import sys, applog\n"
                "assert applog.setup(frozen=False, log_dir=sys.argv[1]) is None\n"
                "applog.get('x').info('bonjour')\n")
        with tempfile.TemporaryDirectory() as d:
            r = _lance(code, d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(os.listdir(d), [])
        self.assertIn("INFO vlocal.x.<module>: bonjour", r.stdout)

    def test_importer_l_app_ne_configure_rien(self):
        import app  # noqa: F401  (importe tous les modules de l'app)
        import applog
        self.assertFalse(applog._configured)
        self.assertEqual(logging.getLogger(applog.ROOT).handlers, [])


if __name__ == "__main__":
    unittest.main()
