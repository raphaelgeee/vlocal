"""Constat 2 — un fichier déposé passe par le GPU quand il est disponible.

Avant : transcription toujours sur CPU, par lots de 4 (+1,4 Go), verrou
d'inférence tenu pendant tout le fichier. Maintenant : chemin de l'import de
réunion (engine.transcribe_import_mlx), CPU seulement en repli, lot de 2 au plus.
Aucun moteur réel n'est chargé : le moteur est une doublure.
"""
import os
import shutil
import sys
import tempfile
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_files  # noqa: E402
import appstate  # noqa: E402
import mlx_engine  # noqa: E402


class _ThreadSynchrone:
    """Remplace threading.Thread : exécute la cible tout de suite."""

    def __init__(self, target=None, args=(), kwargs=None, **_):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


def _wav_1s(dossier):
    p = os.path.join(dossier, "essai.wav")
    with wave.open(p, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 16000)
    return p


class TestFichierDepose(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vlocal_test_fichier_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.wav = _wav_1s(self.dir)
        self.engine = mock.Mock()
        self.engine.transcribe_import_mlx.return_value = {"text": "bonjour GPU"}
        self.engine.transcribe_file_batched.return_value = {"text": "bonjour CPU"}
        self.engine.transcribe_file.return_value = "bonjour monobloc"
        self.ui = []
        ram = mock.Mock(available=16 * 2 ** 30)
        for p in (mock.patch.object(appstate, "controller", mock.Mock(engine=self.engine)),
                  mock.patch.object(api_files, "_ui", self.ui.append),
                  mock.patch.object(api_files, "copy_to_clipboard"),
                  mock.patch.object(api_files, "_begin_activity"),
                  mock.patch.object(api_files, "_end_activity"),
                  mock.patch.object(api_files, "_post_process_by_mode"),
                  mock.patch.object(api_files, "_load_settings", return_value={}),
                  mock.patch.object(api_files.threading, "Thread", _ThreadSynchrone),
                  mock.patch("psutil.virtual_memory", return_value=ram)):
            p.start()
            self.addCleanup(p.stop)

    def _resultat(self):
        return [c for c in self.ui if c.startswith("showFileResult")]

    def test_gpu_disponible_le_cpu_n_est_jamais_appele(self):
        with mock.patch.object(mlx_engine, "available", return_value=True):
            self.assertEqual(api_files.FilesApi().transcribe_file(self.wav), {"ok": True})
        self.engine.transcribe_import_mlx.assert_called_once()
        self.engine.transcribe_file_batched.assert_not_called()
        self.engine.transcribe_file.assert_not_called()
        self.assertIn("bonjour GPU", self._resultat()[0])

    def test_gpu_indisponible_repli_cpu_lot_de_deux_au_plus(self):
        with mock.patch.object(mlx_engine, "available", return_value=False):
            api_files.FilesApi().transcribe_file(self.wav)
        self.engine.transcribe_import_mlx.assert_not_called()
        lot = self.engine.transcribe_file_batched.call_args.kwargs["batch_size"]
        self.assertLessEqual(lot, 2)
        self.assertIn("bonjour CPU", self._resultat()[0])

    def test_gpu_en_echec_repli_cpu(self):
        self.engine.transcribe_import_mlx.side_effect = RuntimeError("Metal")
        with mock.patch.object(mlx_engine, "available", return_value=True):
            api_files.FilesApi().transcribe_file(self.wav)
        self.engine.transcribe_file_batched.assert_called_once()
        self.assertIn("bonjour CPU", self._resultat()[0])

    def test_echec_total_message_fixe_sans_detail_technique(self):
        self.engine.transcribe_file_batched.side_effect = RuntimeError("SECRET-TECH")
        self.engine.transcribe_file.side_effect = RuntimeError("SECRET-TECH")
        with mock.patch.object(mlx_engine, "available", return_value=False):
            api_files.FilesApi().transcribe_file(self.wav)
        self.assertFalse(self._resultat())
        self.assertTrue(self.ui)
        self.assertFalse(any("SECRET-TECH" in c for c in self.ui), self.ui)

    def test_fichier_trop_gros_refuse_avant_decodage(self):
        api = api_files.FilesApi()
        enorme = "A" * (api.MAX_AUDIO_BYTES * 4 // 3 + 8)
        with mock.patch("base64.b64decode") as dec:
            r = api.transcribe_dropped_audio("long.mp3", enorme)
        dec.assert_not_called()
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
