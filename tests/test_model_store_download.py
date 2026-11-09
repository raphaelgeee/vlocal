"""Téléchargement robuste (primitive partagée) et cache de modèles vérifié.

Critères : coupure à 60 % reprise à l'octet exact ; espace disque refusé avant
tout réseau ; modèle interrompu repris au lancement suivant ; modèle corrompu
(taille ou SHA) supprimé, retéléchargé, jamais chargé entre-temps.
"""
import hashlib
import os
import unittest
from unittest import mock

from test_update_support import IsolatedCase

import model_store

URL = "https://r2.example/file.bin"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FetchResumableTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.data = bytes(range(256)) * 4            # 1024 octets
        self.dst = os.path.join(self.tmp, "dl", "file.bin")

    def test_cut_at_60_percent_resumes_from_exact_byte(self):
        server = self.serve({URL: self.data})
        server.cuts[URL] = [614]                      # 60 % de 1024
        model_store.fetch_resumable(URL, self.dst, size=1024, sha256=sha(self.data))
        with open(self.dst, "rb") as f:
            self.assertEqual(f.read(), self.data)
        self.assertEqual(len(server.requests), 2)
        self.assertNotIn("Range", server.requests[0][1])
        self.assertEqual(server.requests[1][1]["Range"], "bytes=614-")
        self.assertFalse(os.path.exists(self.dst + ".part"))

    def test_part_left_by_previous_launch_is_resumed(self):
        os.makedirs(os.path.dirname(self.dst))
        with open(self.dst + ".part", "wb") as f:
            f.write(self.data[:700])
        server = self.serve({URL: self.data})
        model_store.fetch_resumable(URL, self.dst, size=1024, sha256=sha(self.data))
        self.assertEqual(server.requests[0][1]["Range"], "bytes=700-")
        with open(self.dst, "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_server_ignoring_range_rewrites_from_zero(self):
        os.makedirs(os.path.dirname(self.dst))
        with open(self.dst + ".part", "wb") as f:
            f.write(b"x" * 300)                       # contenu périmé
        server = self.serve({URL: self.data})
        server.ignore_range = True
        model_store.fetch_resumable(URL, self.dst, size=1024)
        with open(self.dst, "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_checksum_mismatch_never_exposes_file_then_fails(self):
        self.serve({URL: self.data})
        with self.assertRaises(model_store.DownloadError) as cm:
            model_store.fetch_resumable(URL, self.dst, size=1024, sha256="0" * 64, attempts=2)
        self.assertEqual(cm.exception.code, "checksum")
        self.assertFalse(os.path.exists(self.dst))
        self.assertFalse(os.path.exists(self.dst + ".part"))

    def test_announced_size_mismatch_is_final(self):
        server = self.serve({URL: self.data})
        with self.assertRaises(model_store.DownloadError) as cm:
            model_store.fetch_resumable(URL, self.dst, size=2048)
        self.assertEqual(cm.exception.code, "size_mismatch")
        self.assertEqual(len(server.requests), 1)     # aucune insistance inutile
        self.assertFalse(os.path.exists(self.dst))

    def test_http_404_is_final(self):
        import urllib.error
        model_store._open = mock.Mock(side_effect=urllib.error.HTTPError(URL, 404, "nf", {}, None))
        with self.assertRaises(model_store.DownloadError) as cm:
            model_store.fetch_resumable(URL, self.dst, size=1024)
        self.assertEqual(cm.exception.code, "http")
        self.assertEqual(model_store._open.call_count, 1)

    def test_stalled_connection_is_abandoned(self):
        server = self.serve({URL: self.data})
        clock = iter(range(0, 10_000, 61))            # 61 s entre deux lectures de 64 o
        with mock.patch.object(model_store, "_now", lambda: next(clock)):
            with self.assertRaises(model_store.DownloadError) as cm:
                model_store.fetch_resumable(URL, self.dst, size=1024, attempts=2)
        self.assertEqual(cm.exception.code, "stalled")
        self.assertEqual(len(server.requests), 2)
        self.assertFalse(os.path.exists(self.dst))

    def test_no_space_refused_before_any_request(self):
        with mock.patch.object(model_store, "free_bytes", return_value=10):
            with self.assertRaises(model_store.DownloadError) as cm:
                model_store.ensure_free_space(self.dst, 1024)
        self.assertEqual(cm.exception.code, "no_space")


def manifest(files):
    """Manifest au format R2 pour {"modèle/fichier": données}."""
    models = {}
    for path, data in files.items():
        model, rel = path.split("/", 1)
        models.setdefault(model, {})[rel] = {"size": len(data), "sha256": sha(data)}
    return {"schema": 1, "base_url": "https://r2.example/models", "models": models}


class ModelCacheTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.files = {
            "whisper-large-v3-turbo-mlx-q8/weights.safetensors": b"w" * 900,
            "whisper-large-v3-turbo-mlx-q8/config.json": b"{}",
            "whisper-large-v3-turbo-int8/model.bin": b"m" * 700,
            "whisper-large-v3-turbo-int8/tokenizer.json": b"t" * 300,
            "whisper-small-int8/model.bin": b"s" * 500,
        }
        self.man = manifest(self.files)
        self.server = self.serve({"https://r2.example/models/" + p: d for p, d in self.files.items()})
        self.cache = model_store.CACHE

    def test_full_download_makes_cache_complete(self):
        self.assertTrue(model_store.needs_download())
        seen = []
        model_store.download_models(progress=lambda d, t, l: seen.append((d, t)), manifest=self.man)
        self.assertFalse(model_store.needs_download())
        self.assertEqual(model_store.models_dir(), self.cache)
        self.assertEqual(seen[-1], (2402, 2402))

    def test_interrupted_model_never_loaded_then_resumed(self):
        url = "https://r2.example/models/whisper-large-v3-turbo-int8/tokenizer.json"
        self.server.cuts[url] = [120] * model_store.ATTEMPTS     # coupe à chaque tentative
        with self.assertRaises(model_store.DownloadError):
            model_store.download_models(manifest=self.man)
        # model.bin (sentinelle historique) est là, tokenizer.json non : jamais chargé.
        self.assertTrue(os.path.exists(os.path.join(self.cache, "whisper-large-v3-turbo-int8", "model.bin")))
        self.assertFalse(model_store._complete(self.cache))
        self.assertNotEqual(model_store.models_dir(), self.cache)
        self.assertTrue(os.path.exists(os.path.join(self.cache, "whisper-large-v3-turbo-int8",
                                                    "tokenizer.json.part")))
        # « lancement suivant » : la connexion tient, le .part est repris.
        self.server.cuts.clear()
        self.server.requests.clear()
        model_store.download_models(manifest=self.man)
        self.assertTrue(model_store._complete(self.cache))
        ranges = [h.get("Range") for u, h in self.server.requests if u == url]
        self.assertEqual(ranges, ["bytes=120-"])
        # les fichiers déjà vérifiés ne sont pas redemandés
        self.assertEqual([u for u, _h in self.server.requests], [url, url.replace(
            "whisper-large-v3-turbo-int8/tokenizer.json", "whisper-small-int8/model.bin")])

    def test_corrupt_file_same_size_is_replaced(self):
        model_store.download_models(manifest=self.man)
        path = os.path.join(self.cache, "whisper-small-int8", "model.bin")
        with open(path, "wb") as f:
            f.write(b"X" * 500)                       # même taille, contenu altéré
        os.utime(path, ns=(1, 1))                     # date changée : l'état ne vaut plus
        self.assertFalse(model_store._complete(self.cache), "un fichier réécrit ne doit pas être chargé")
        self.server.requests.clear()
        model_store.download_models(manifest=self.man)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.files["whisper-small-int8/model.bin"])
        self.assertEqual(len(self.server.requests), 1)
        self.assertTrue(model_store._complete(self.cache))
        self.assertIn("corrupt", self.journal())

    def test_truncated_file_is_not_loaded_and_redownloaded(self):
        model_store.download_models(manifest=self.man)
        path = os.path.join(self.cache, "whisper-large-v3-turbo-mlx-q8", "weights.safetensors")
        with open(path, "r+b") as f:
            f.truncate(100)
        self.assertNotEqual(model_store.models_dir(), self.cache)
        model_store.download_models(manifest=self.man)
        self.assertEqual(os.path.getsize(path), 900)
        self.assertEqual(model_store.models_dir(), self.cache)

    def test_legacy_cache_without_state_keeps_working(self):
        for name, sentinel in model_store.REQUIRED.items():
            os.makedirs(os.path.join(self.cache, name))
            open(os.path.join(self.cache, name, sentinel), "w").close()
        self.assertTrue(model_store._complete(self.cache))
        self.assertFalse(model_store.needs_download())

    def test_no_space_refused_before_network(self):
        with mock.patch.object(model_store, "free_bytes", return_value=100):
            with self.assertRaises(model_store.DownloadError) as cm:
                model_store.download_models(manifest=self.man)
        self.assertEqual(cm.exception.code, "no_space")
        self.assertEqual(self.server.requests, [])
        self.assertIn("disk_full", self.journal())

    def test_manifest_path_escape_is_refused(self):
        bad = manifest({"whisper-small-int8/../../evil": b"x"})
        with self.assertRaises(model_store.DownloadError) as cm:
            model_store.download_models(manifest=bad)
        self.assertEqual(cm.exception.code, "manifest")
        self.assertEqual(self.server.requests, [])

    def test_errors_remain_runtime_errors_for_app(self):
        self.assertTrue(issubclass(model_store.DownloadError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
