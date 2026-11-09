"""Socle commun des tests de mise à jour et de modèles : isolation et doublures.

Chaque test tourne dans un dossier temporaire : App Support, la zone de mise à
jour, le dossier « Applications » et le journal structuré y sont redirigés.
Aucun réseau (faux serveur HTTP), aucun vrai DMG (fausse commande hdiutil),
aucune attente réelle (sommeils neutralisés), aucun outil système inattendu :
un appel à updater._run que le test n'a pas prévu fait échouer le test.
"""
import os
import plistlib
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import errors  # noqa: E402
import model_store  # noqa: E402
import updater  # noqa: E402

ARM64 = 0x0100000C
X86_64 = 0x01000007


def macho(cpu):
    """En-tête Mach-O 64 bits minimal pour une architecture donnée."""
    return struct.pack("<II", 0xFEEDFACF, cpu) + b"\0" * 24


def make_app(parent, version="9.0.0", bundle_id=updater.BUNDLE_ID, cpu=ARM64, name="Vlocal.app"):
    """Faux Vlocal.app : Info.plist, exécutable Mach-O, une bibliothèque en 640
    et un dossier en 750 (les modes relevés en production)."""
    app = os.path.join(parent, name)
    os.makedirs(os.path.join(app, "Contents", "MacOS"))
    os.makedirs(os.path.join(app, "Contents", "Resources", "lib"))
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "Vlocal",
                       "CFBundleShortVersionString": version}, f)
    exe = os.path.join(app, "Contents", "MacOS", "Vlocal")
    with open(exe, "wb") as f:
        f.write(macho(cpu))
    os.chmod(exe, 0o750)
    lib = os.path.join(app, "Contents", "Resources", "lib", "libmetal.dylib")
    with open(lib, "wb") as f:
        f.write(b"metal")
    os.chmod(lib, 0o640)
    os.chmod(os.path.join(app, "Contents", "Resources", "lib"), 0o750)
    return app


def bundle_version(app):
    return updater.read_bundle_info(app)["CFBundleShortVersionString"]


class FakeResponse:
    def __init__(self, data, status=200, headers=None, cut_after=None, on_read=None):
        self._data, self._pos = data, 0
        self.status = status
        self.headers = headers or {}
        self._cut = cut_after
        self._on_read = on_read

    def read(self, n):
        if self._on_read:
            self._on_read()
        if self._cut is not None and self._pos >= self._cut:
            raise ConnectionResetError("coupure simulée")
        end = self._pos + min(n, 64)
        if self._cut is not None:
            end = min(end, self._cut)
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeServer:
    """Faux serveur HTTP pour model_store._open : sert des fichiers par URL,
    honore Range (sauf ignore_range), peut couper une réponse à un octet donné."""

    def __init__(self, files):
        self.files = dict(files)
        self.requests = []          # (url, headers)
        self.cuts = {}              # url -> [octet de coupure, une valeur par réponse]
        self.ignore_range = False
        self.on_read = None

    def __call__(self, url, timeout, headers=None):
        headers = dict(headers or {})
        self.requests.append((url, headers))
        data = self.files[url]
        cut = None
        if self.cuts.get(url):
            cut = self.cuts[url].pop(0)
        rng = headers.get("Range")
        if rng and not self.ignore_range:
            start = int(rng.split("=")[1].rstrip("-"))
            body = data[start:]
            return FakeResponse(body, 206, {"Content-Length": str(len(body)),
                                            "Content-Range": "bytes %d-%d/%d" % (start, len(data) - 1, len(data))},
                                None if cut is None else cut - start, self.on_read)
        return FakeResponse(data, 200, {"Content-Length": str(len(data))}, cut, self.on_read)


class IsolatedCase(unittest.TestCase):
    """Redirige tous les chemins réels vers un dossier temporaire."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="vlocal-maj-")
        self.tmp = os.path.realpath(self._tmp.name)
        self.support = os.path.join(self.tmp, "support")
        self.updates = os.path.join(self.support, "updates")
        self.apps = os.path.join(self.tmp, "Applications")
        os.makedirs(self.apps)
        self.target = os.path.join(self.apps, "Vlocal.app")
        self.run_calls = []
        self.run_fake = None

        def _guarded_run(cmd, timeout=180):
            self.run_calls.append(list(cmd))
            if self.run_fake is None:
                raise AssertionError("commande système inattendue : %s" % cmd)
            return self.run_fake(list(cmd))

        patches = [
            mock.patch.object(updater, "SUPPORT", self.support),
            mock.patch.object(updater, "UPDATES", self.updates),
            mock.patch.object(updater, "TARGET", self.target),
            mock.patch.object(updater, "_sleep", lambda s: None),
            mock.patch.object(updater, "_run", _guarded_run),
            mock.patch.object(updater, "_self_bundle", lambda: None),
            mock.patch.object(updater, "_dedupe_icons", mock.Mock(return_value=0)),
            mock.patch.object(updater, "_in_background", lambda fn: fn()),
            mock.patch.object(updater, "_schedule_auto_confirm", mock.Mock()),
            mock.patch.object(updater, "_spawn_installer", mock.Mock()),
            mock.patch.object(model_store, "SUPPORT", self.support),
            mock.patch.object(model_store, "CACHE", os.path.join(self.support, "models")),
            mock.patch.object(model_store, "_sleep", lambda s: None),
            mock.patch.object(model_store, "_open", mock.Mock(side_effect=AssertionError("réseau réel interdit"))),
            mock.patch.object(model_store, "DISK_MARGIN", 0),
            mock.patch.object(errors, "_DIR", os.path.join(self.support, "journal")),
            mock.patch.object(errors, "_LOG", os.path.join(self.support, "journal", "events.jsonl")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def serve(self, files):
        server = FakeServer(files)
        model_store._open = server        # restauré par le patch de setUp
        return server

    def journal(self):
        path = errors._LOG
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()


def hold_event():
    """(event, callback) : le callback bloque la lecture tant que l'event n'est pas levé."""
    ev = threading.Event()
    return ev, (lambda: ev.wait(5))


def rmtree(path):
    shutil.rmtree(path, ignore_errors=True)
