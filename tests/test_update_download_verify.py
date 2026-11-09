"""Mise à jour : téléchargement du DMG, vérification, staging, concurrence.

Aucun vrai DMG : la fausse commande hdiutil « monte » un faux Vlocal.app dans
un dossier temporaire ; codesign et spctl sont simulés. On prouve que tout
rejet laisse le DMG démonté et rien d'installé.
"""
import hashlib
import os
import plistlib
import shutil
import stat
import threading
import unittest
from unittest import mock

from test_update_support import X86_64, IsolatedCase, make_app

import model_store
import updater

URL = "https://r2.example/Vlocal-9.0.0.dmg"
def read(path):
    with open(path, "rb") as f:
        return f.read()


TEAM_OK = "Authority=Developer ID Application: Raphael Gee (LW8B2TTQ4W)\nTeamIdentifier=LW8B2TTQ4W\n"


class DownloadTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.data = b"D" * 2000
        self.server = self.serve({URL: self.data})

    def test_download_with_optional_sha256(self):
        frac = []
        path = updater.download(URL, 2000, "9.0.0", on_progress=lambda f, l: frac.append(f),
                                sha256=hashlib.sha256(self.data).hexdigest())
        self.assertEqual(path, os.path.join(self.updates, "Vlocal-9.0.0.dmg"))
        self.assertEqual(read(path), self.data)
        self.assertEqual(frac[-1], 1.0)

    def test_sha256_absent_stays_compatible(self):
        path = updater.download(URL, 2000, "9.0.0")
        self.assertEqual(os.path.getsize(path), 2000)

    def test_bad_sha256_rejected_nothing_kept(self):
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download(URL, 2000, "9.0.0", sha256="ab" * 32)
        self.assertEqual(cm.exception.code, "checksum")
        self.assertEqual(os.listdir(self.updates), ["update.lock"])
        self.assertIn('"reason": "checksum"', self.journal())

    def test_insufficient_disk_refused_before_download(self):
        with mock.patch.object(model_store, "free_bytes", return_value=3999):
            with self.assertRaises(updater.UpdateError) as cm:
                updater.download(URL, 2000, "9.0.0")      # il faut 2 × 2000
        self.assertEqual(cm.exception.code, "no_space")
        self.assertEqual(self.server.requests, [])
        self.assertIn("Espace disque insuffisant", str(cm.exception))
        self.assertIn("Not enough disk space", cm.exception.message("en"))

    def test_cut_download_resumed_on_retry(self):
        self.server.cuts[URL] = [1200]
        path = updater.download(URL, 2000, "9.0.0")
        self.assertEqual(read(path), self.data)
        self.assertEqual(self.server.requests[1][1]["Range"], "bytes=1200-")

    def test_already_downloaded_is_reused(self):
        updater.download(URL, 2000, "9.0.0")
        self.server.requests.clear()
        updater.download(URL, 2000, "9.0.0")
        self.assertEqual(self.server.requests, [])

    def test_blocked_version_is_never_downloaded_again(self):
        updater._block("9.0.0")
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download(URL, 2000, "9.0")             # 9.0 == 9.0.0
        self.assertEqual(cm.exception.code, "blocked_version")
        self.assertEqual(self.server.requests, [])

    def test_invalid_offer_rejected(self):
        for url, ver in ((URL, "../../evil"), ("http://x/Vlocal.dmg", "9.0.0")):
            with self.assertRaises(updater.UpdateError) as cm:
                updater.download(url, 2000, ver)
            self.assertEqual(cm.exception.code, "invalid_offer")

    def test_two_concurrent_updates_only_one_runs(self):
        started, release = threading.Event(), threading.Event()

        def _block_read():
            started.set()
            release.wait(5)
        self.server.on_read = _block_read
        result = {}

        def _first():
            try:
                result["path"] = updater.download(URL, 2000, "9.0.0")
            except Exception as e:  # noqa: BLE001
                result["error"] = e
        t = threading.Thread(target=_first)
        t.start()
        self.assertTrue(started.wait(5))
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download(URL, 2000, "9.0.0")
        self.assertEqual(cm.exception.code, "busy")
        self.server.on_read = None
        release.set()
        t.join(5)
        self.assertNotIn("error", result)
        self.assertEqual(len([r for r in self.server.requests if not r[1].get("Range")]), 1)


class FakeTools:
    """Simule hdiutil / codesign / spctl / ditto / xattr pour verify_and_stage."""

    def __init__(self, case, app_parent):
        self.case, self.app_parent = case, app_parent
        self.verify_code = 0
        self.team = TEAM_OK
        self.spctl = (0, "accepted\nsource=Notarized Developer ID\n")
        self.ditto_raises = False
        self.mounts, self.detached = [], []

    def __call__(self, cmd):
        tool = os.path.basename(cmd[0])
        if tool == "hdiutil" and cmd[1] == "attach":
            root = cmd[cmd.index("-mountrandom") + 1]
            mount = os.path.join(root, "dmg.%d" % len(self.mounts))
            shutil.copytree(self.app_parent, mount, symlinks=True)
            self.mounts.append(mount)
            out = plistlib.dumps({"system-entities": [
                {"dev-entry": "/dev/disk9"}, {"dev-entry": "/dev/disk9s1", "mount-point": mount}]})
            return 0, out.decode(), ""
        if tool == "hdiutil" and cmd[1] == "detach":
            self.detached.append(cmd[2])
            for m in self.mounts:
                shutil.rmtree(m, ignore_errors=True)
            return 0, "", ""
        if tool == "codesign" and "--verify" in cmd:
            return self.verify_code, "", "" if self.verify_code == 0 else "a sealed resource is missing"
        if tool == "codesign" and "-dv" in cmd:
            return 0, "", self.team
        if tool == "spctl":
            return self.spctl[0], "", self.spctl[1]
        if tool == "ditto":
            if self.ditto_raises:
                raise RuntimeError("ditto planté")
            shutil.copytree(cmd[1], cmd[2], symlinks=True)
            return 0, "", ""
        if tool == "xattr":
            return 0, "", ""
        raise AssertionError("outil inattendu %s" % cmd)


class VerifyAndStageTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.payload = os.path.join(self.tmp, "payload")
        os.makedirs(self.payload)
        self.dmg = os.path.join(self.updates, "Vlocal-9.0.0.dmg")
        os.makedirs(self.updates, exist_ok=True)
        open(self.dmg, "wb").close()
        self.staged = os.path.join(self.updates, "Vlocal.app")

    def _tools(self, **app_kwargs):
        make_app(self.payload, **app_kwargs)
        self.run_fake = tools = FakeTools(self, self.payload)
        return tools

    def _assert_rejected(self, tools, code):
        with self.assertRaises(updater.UpdateError) as cm:
            updater.verify_and_stage(self.dmg, current_version="1.4.3")
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(tools.detached, ["/dev/disk9"], "le DMG doit être démonté")
        self.assertFalse(os.path.exists(self.staged), "rien ne doit être préparé")
        self.assertEqual(os.listdir(self.apps), [], "rien ne doit être installé")
        return cm.exception

    def test_valid_update_is_staged_with_normalized_modes(self):
        tools = self._tools()
        stages = []
        staged = updater.verify_and_stage(self.dmg, on_stage=stages.append, current_version="1.4.3")
        self.assertEqual(staged, self.staged)
        self.assertEqual(stages, ["verify", "stage"])
        self.assertEqual(tools.detached, ["/dev/disk9"])
        lib = os.path.join(staged, "Contents", "Resources", "lib", "libmetal.dylib")
        exe = os.path.join(staged, "Contents", "MacOS", "Vlocal")
        self.assertEqual(stat.S_IMODE(os.stat(lib).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(exe).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(lib)).st_mode), 0o755)
        # la copie est revérifiée APRÈS normalisation
        verifies = [c for c in self.run_calls if c[0].endswith("codesign") and "--verify" in c]
        self.assertEqual(verifies[-1][-1], staged)

    def test_invalid_signature_rejected(self):
        tools = self._tools()
        tools.verify_code = 1
        self._assert_rejected(tools, "bad_signature")

    def test_other_team_rejected(self):
        tools = self._tools()
        tools.team = TEAM_OK.replace("TeamIdentifier=LW8B2TTQ4W", "TeamIdentifier=ABCDE12345")
        err = self._assert_rejected(tools, "wrong_team")
        self.assertIn("autre éditeur", str(err))

    def test_not_notarized_rejected(self):
        tools = self._tools()
        tools.spctl = (0, "accepted\nsource=Unnotarized Developer ID\n")
        self._assert_rejected(tools, "not_notarized")
        tools.spctl, tools.detached = (3, "rejected\n"), []
        self._assert_rejected(tools, "not_notarized")

    def test_lower_or_equal_version_rejected(self):
        for ver in ("1.4.3", "1.4.2", "1.4"):
            shutil.rmtree(self.payload)
            os.makedirs(self.payload)
            tools = self._tools(version=ver)
            with self.assertRaises(updater.UpdateError) as cm:
                updater.verify_and_stage(self.dmg, expected_version=ver, current_version="1.4.3")
            self.assertEqual(cm.exception.code, "downgrade", ver)
            self.assertEqual(tools.detached, ["/dev/disk9"])
            self.assertFalse(os.path.exists(self.staged))

    def test_version_differs_from_offer_rejected(self):
        tools = self._tools(version="9.1.0")          # le DMG s'appelle 9.0.0
        self._assert_rejected(tools, "bad_bundle")

    def test_wrong_bundle_id_rejected(self):
        tools = self._tools(bundle_id="com.evil.app")
        self._assert_rejected(tools, "bad_bundle")

    def test_unreadable_info_plist_rejected(self):
        tools = self._tools()
        with open(os.path.join(self.payload, "Vlocal.app", "Contents", "Info.plist"), "wb") as f:
            f.write(b"\x00garbage")
        self._assert_rejected(tools, "bad_bundle")

    def test_intel_only_build_rejected(self):
        tools = self._tools(cpu=X86_64)
        self._assert_rejected(tools, "wrong_arch")

    def test_unexpected_exception_still_detaches(self):
        tools = self._tools()
        tools.ditto_raises = True
        self._assert_rejected(tools, "stage_failed")

    def test_mount_failure_reported(self):
        self.run_fake = lambda cmd: (1, "", "hdiutil: attach failed - corrupt image")
        with self.assertRaises(updater.UpdateError) as cm:
            updater.verify_and_stage(self.dmg, current_version="1.4.3")
        self.assertEqual(cm.exception.code, "mount_failed")

    def test_unparsable_attach_output_detaches_orphans(self):
        orphan = os.path.join(self.updates, "mnt", "dmg.x")
        os.makedirs(orphan)
        calls = []

        def fake(cmd):
            calls.append(cmd)
            return 0, "pas un plist", ""
        self.run_fake = fake
        with mock.patch("os.path.ismount", side_effect=lambda p: p == orphan and not calls[1:]):
            with self.assertRaises(updater.UpdateError) as cm:
                updater.verify_and_stage(self.dmg, current_version="1.4.3")
        self.assertEqual(cm.exception.code, "mount_failed")
        self.assertIn(["/usr/bin/hdiutil", "detach", orphan], calls)

    def test_no_space_for_staging_refused(self):
        tools = self._tools()
        with mock.patch.object(model_store, "free_bytes", return_value=1):
            self._assert_rejected(tools, "no_space")


class HelpersTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(updater.parse_version("1.4.3"), (1, 4, 3))
        self.assertIsNone(updater.parse_version("1.4.3-beta"))
        self.assertIsNone(updater.parse_version(None))
        self.assertTrue(updater._same("1.4", "1.4.0"))
        self.assertFalse(updater._same("1.4.1", "1.4"))

    def test_error_messages_cover_every_code_in_both_languages(self):
        for code, (fr, en) in updater._MESSAGES.items():
            e = updater.UpdateError(code, "détail")
            self.assertEqual(str(e), fr)
            self.assertEqual(e.message("en"), en)
            self.assertTrue(fr and en)
        self.assertEqual(updater.UpdateError("code-inconnu").code, "failed")


if __name__ == "__main__":
    unittest.main()
