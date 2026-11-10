"""Mise à jour : lancement de l'installateur, machine à états du démarrage,
confirmation, retour arrière par compteur, blocage anti-boucle, nettoyage.

Le chien de garde shell est couvert par test_update_install ; ici on couvre le
filet Python : la nouvelle version qui redémarre sans jamais confirmer (Mac
redémarré pendant l'attente) déclenche elle-même le retour arrière, une seule
fois, et l'ancienne version bloque ensuite la version fautive.
"""
import json
import os
import time
import unittest
from unittest import mock

from test_update_support import IsolatedCase, make_app

import updater

# La vraie programmation de la confirmation automatique (IsolatedCase la remplace).
_REAL_SCHEDULE = updater._schedule_auto_confirm

REAL_SPAWN = updater._spawn_installer      # capturé avant les doublures de setUp


class StartupCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.updates, "state")
        os.makedirs(self.state)
        self.backup = os.path.join(self.apps, ".Vlocal-backup.app")

    def pending(self, **extra):
        data = {"to_version": "9.0.0", "from_version": "1.4.3", "target": self.target,
                "staged": os.path.join(self.updates, "Vlocal.app"), "attempts": 0}
        data.update(extra)
        with open(os.path.join(self.state, "pending.json"), "w") as f:
            json.dump(data, f)

    def phase(self, value):
        with open(os.path.join(self.state, "phase"), "w") as f:
            f.write(value + "\n")

    def read_pending(self):
        path = os.path.join(self.state, "pending.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)


class ApplyUpdateTests(StartupCase):
    def test_apply_records_pending_and_spawns_detached_installer(self):
        staged = make_app(self.updates, version="9.0.0")
        open(os.path.join(self.state, "confirmed"), "w").close()   # reste périmé
        self.assertTrue(updater.apply_update(staged, 4242, from_version="1.4.3"))
        p = self.read_pending()
        self.assertEqual((p["to_version"], p["from_version"], p["target"], p["attempts"]),
                         ("9.0.0", "1.4.3", self.target, 0))
        self.assertFalse(os.path.exists(os.path.join(self.state, "confirmed")))
        updater._spawn_installer.assert_called_once_with("install", 4242, staged, self.target)

    def test_real_spawn_is_detached(self):
        staged = make_app(self.updates, version="9.0.0")
        with mock.patch("subprocess.Popen") as popen:     # implémentation réelle, Popen simulé
            REAL_SPAWN("install", 4242, staged, self.target)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], "/bin/sh")
        self.assertTrue(kwargs["start_new_session"])
        script = os.path.join(self.updates, "install.sh")
        self.assertEqual(os.stat(script).st_mode & 0o777, 0o700)
        with open(script) as f:
            self.assertIn("PID=4242", f.read())

    def test_unwritable_location_refused_before_quitting(self):
        staged = make_app(self.updates, version="9.0.0")
        os.chmod(self.apps, 0o555)
        self.addCleanup(os.chmod, self.apps, 0o755)
        with self.assertRaises(updater.UpdateError) as cm:
            updater.apply_update(staged, 4242)
        self.assertEqual(cm.exception.code, "no_permission")
        self.assertIsNone(self.read_pending())
        updater._spawn_installer.assert_not_called()

    def test_spawn_failure_clears_pending(self):
        staged = make_app(self.updates, version="9.0.0")
        updater._spawn_installer.side_effect = OSError("fork impossible")
        with self.assertRaises(updater.UpdateError) as cm:
            updater.apply_update(staged, 4242)
        self.assertEqual(cm.exception.code, "launch_failed")
        self.assertIsNone(self.read_pending())

    def test_not_staged(self):
        with self.assertRaises(updater.UpdateError) as cm:
            updater.apply_update(os.path.join(self.tmp, "absent"), 1)
        self.assertEqual(cm.exception.code, "not_staged")

    def test_refused_while_an_installer_runs(self):
        staged = make_app(self.updates, version="9.0.0")
        lock = os.path.join(self.state, "installer.lock")
        os.makedirs(lock)
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write(str(os.getpid()))
        with self.assertRaises(updater.UpdateError) as cm:
            updater.apply_update(staged, 4242)
        self.assertEqual(cm.exception.code, "busy")


class TargetTests(unittest.TestCase):
    def test_translocated_or_readonly_bundle_installs_to_applications(self):
        self.assertEqual(updater.resolve_target(
            "/private/var/folders/x/AppTranslocation/ABC/d/Vlocal.app"), updater.TARGET)
        self.assertEqual(updater.resolve_target("/Volumes/Vlocal/Vlocal.app"), updater.TARGET)
        self.assertEqual(updater.resolve_target(None), updater.TARGET)

    def test_writable_location_outside_applications_is_updated_in_place(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            app = os.path.join(os.path.realpath(d), "Vlocal.app")
            self.assertEqual(updater.resolve_target(app), app)


class StartupTransitionTests(StartupCase):
    def test_idle_without_pending(self):
        self.assertEqual(updater.cleanup_leftovers("1.4.3")["status"], "idle")

    def test_new_version_awaits_confirmation_and_schedules_auto_confirm(self):
        self.pending()
        self.phase("swapped")
        r = updater.cleanup_leftovers("9.0.0")
        self.assertEqual((r["status"], r["attempts"]), ("confirming", 1))
        updater._schedule_auto_confirm.assert_called_once_with("9.0.0")

    def _fire_auto_confirm(self):
        """Programme la confirmation automatique et déclenche son minuteur tout de suite."""
        timers = []

        class FakeTimer:
            def __init__(self, delay, fn, args=(), kwargs=None):
                timers.append(lambda: fn(*args, **(kwargs or {})))
                self.daemon = False

            def start(self):
                pass

        with mock.patch.object(updater.threading, "Timer", FakeTimer):
            _REAL_SCHEDULE("9.0.0")
        self.assertEqual(len(timers), 1)
        timers[0]()

    def test_auto_confirm_without_loaded_window_does_not_confirm(self):
        self.pending()
        with mock.patch.object(updater, "_window_loaded", __import__("threading").Event()):
            self._fire_auto_confirm()
        self.assertIsNotNone(self.read_pending(), "le retour arrière doit rester possible")
        self.assertFalse(os.path.exists(os.path.join(self.state, "confirmed")))

    def test_auto_confirm_after_window_loaded_confirms(self):
        self.pending()
        loaded = __import__("threading").Event()
        with mock.patch.object(updater, "_window_loaded", loaded):
            updater.mark_window_loaded()
            self._fire_auto_confirm()
        self.assertTrue(loaded.is_set())
        self.assertIsNone(self.read_pending())

    def test_main_window_loaded_marks_the_window(self):
        import startup
        with mock.patch.object(updater, "mark_window_loaded") as mark, \
                mock.patch.object(updater, "confirm_startup"):
            startup.on_main_window_loaded()
        mark.assert_called_once_with()

    def test_confirm_startup_finalizes_and_removes_backup(self):
        self.pending()
        make_app(self.apps, version="1.4.3", name=".Vlocal-backup.app")
        self.assertTrue(updater.confirm_startup("9.0.0"))
        self.assertIsNone(self.read_pending())
        self.assertFalse(os.path.exists(self.backup))
        self.assertFalse(updater.confirm_startup("9.0.0"), "idempotent")

    def test_confirm_from_another_version_is_ignored(self):
        self.pending()
        self.assertFalse(updater.confirm_startup("1.4.3"))
        self.assertIsNotNone(self.read_pending())

    def test_confirmed_by_watchdog_reports_updated(self):
        self.pending()
        self.phase("confirmed")
        self.assertEqual(updater.cleanup_leftovers("9.0.0")["status"], "updated")
        self.assertIsNone(self.read_pending())

    def test_unconfirmed_starts_trigger_a_single_rollback(self):
        """Machine à états : N démarrages sans confirmation -> retour arrière,
        une seule fois, jamais de boucle."""
        self.pending()
        self.phase("swapped")
        make_app(self.apps, version="1.4.3", name=".Vlocal-backup.app")
        statuses = [updater.cleanup_leftovers("9.0.0")["status"]
                    for _ in range(updater.MAX_UNCONFIRMED_STARTS)]
        self.assertEqual(statuses, ["confirming"] * updater.MAX_UNCONFIRMED_STARTS)
        updater._spawn_installer.assert_not_called()
        r = updater.cleanup_leftovers("9.0.0")
        self.assertEqual(r["status"], "rollback_started")
        updater._spawn_installer.assert_called_once_with("rollback", os.getpid(), "", self.target)
        self.assertTrue(os.path.isdir(self.backup), "la sauvegarde est le filet : intacte")
        # le retour arrière n'a pas abouti et la nouvelle version redémarre : on
        # garde la version qui tourne au lieu de boucler.
        r = updater.cleanup_leftovers("9.0.0")
        self.assertEqual((r["status"], r.get("unconfirmed")), ("updated", True))
        self.assertEqual(updater._spawn_installer.call_count, 1)
        self.assertIsNone(self.read_pending())

    def test_no_rollback_without_backup(self):
        self.pending(attempts=updater.MAX_UNCONFIRMED_STARTS)
        r = updater.cleanup_leftovers("9.0.0")
        self.assertEqual(r["status"], "updated")
        updater._spawn_installer.assert_not_called()

    def test_watchdog_alive_is_left_in_charge(self):
        self.pending(attempts=10)
        lock = os.path.join(self.state, "installer.lock")
        os.makedirs(lock)
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write(str(os.getpid()))
        make_app(self.apps, version="1.4.3", name=".Vlocal-backup.app")
        self.assertEqual(updater.cleanup_leftovers("9.0.0")["status"], "confirming")
        updater._spawn_installer.assert_not_called()
        self.assertTrue(os.path.isdir(self.backup))

    def test_old_version_after_rollback_blocks_faulty_version(self):
        self.pending()
        self.phase("rolled_back")
        r = updater.cleanup_leftovers("1.4.3")
        self.assertEqual((r["status"], r["version"]), ("rolled_back", "9.0.0"))
        self.assertTrue(updater.is_blocked("9.0.0"))
        self.assertFalse(updater.is_blocked("9.0.1"))
        self.assertIsNone(self.read_pending())
        self.assertIn("rolled_back", self.journal())

    def test_failed_install_reported_and_cleared(self):
        self.pending()
        self.phase("failed:copy")
        r = updater.cleanup_leftovers("1.4.3")
        self.assertEqual((r["status"], r["reason"]), ("failed", "copy"))
        self.assertFalse(updater.is_blocked("9.0.0"), "une panne de copie n'accuse pas la version")

    def test_corrupt_state_never_blocks_startup(self):
        with open(os.path.join(self.state, "pending.json"), "w") as f:
            f.write("{pas du json")
        self.assertEqual(updater.cleanup_leftovers("1.4.3")["status"], "idle")


class CleanupTests(StartupCase):
    def test_purges_only_our_own_leftovers(self):
        for name in (".Vlocal-incoming.app", ".Vlocal-failed.app", ".Vlocal-backup.app"):
            make_app(self.apps, name=name)
        make_app(self.apps, version="1.4.3")
        foreign = make_app(self.apps, name="Autre.app")
        user_file = os.path.join(self.updates, "notes-utilisateur.txt")
        keep_future = os.path.join(self.updates, "Vlocal-9.0.0.dmg.part")
        old_dmg = os.path.join(self.updates, "Vlocal-1.4.3.dmg")
        old_part = os.path.join(self.updates, "Vlocal-1.4.2.dmg.part")
        stale_part = os.path.join(self.updates, "Vlocal-9.9.9.dmg.part")
        for p in (user_file, keep_future, old_dmg, old_part, stale_part):
            open(p, "w").close()
        old = time.time() - updater._PART_MAX_AGE - 10
        os.utime(stale_part, (old, old))
        make_app(self.updates)                                   # staging périmé
        updater.cleanup_leftovers("1.4.3")
        self.assertEqual(sorted(os.listdir(self.apps)), ["Autre.app", "Vlocal.app"])
        self.assertTrue(os.path.isdir(foreign))
        self.assertTrue(os.path.exists(user_file))
        self.assertTrue(os.path.exists(keep_future), "un .part d'une version à venir est repris")
        for gone in (old_dmg, old_part, stale_part, os.path.join(self.updates, "Vlocal.app")):
            self.assertFalse(os.path.exists(gone), gone)
        updater._dedupe_icons.assert_called_once()

    def test_backup_kept_while_confirmation_pending(self):
        self.pending()
        self.phase("swapped")
        make_app(self.apps, name=".Vlocal-backup.app")
        updater.cleanup_leftovers("9.0.0")
        self.assertTrue(os.path.isdir(self.backup))

    def test_nothing_purged_while_an_update_holds_the_lock(self):
        make_app(self.apps, name=".Vlocal-incoming.app")
        with updater._UpdateLock():
            updater._purge("1.4.3")
        self.assertTrue(os.path.isdir(os.path.join(self.apps, ".Vlocal-incoming.app")))
        self.assertNotIn("busy", self.journal(), "renoncer au nettoyage n'est pas un échec")

    def test_orphan_mount_is_detached_at_startup(self):
        orphan = os.path.join(self.updates, "mnt", "dmg.1")
        os.makedirs(orphan)
        self.run_fake = lambda cmd: (0, "", "")
        with mock.patch("os.path.ismount", side_effect=lambda p: p == orphan):
            updater.cleanup_leftovers("1.4.3")
        self.assertIn(["/usr/bin/hdiutil", "detach", orphan], self.run_calls)


class ModesAndArchTests(IsolatedCase):
    def test_normalize_modes(self):
        app = make_app(self.tmp)
        link = os.path.join(app, "Contents", "Resources", "lien")
        os.symlink("lib/libmetal.dylib", link)
        self.assertGreaterEqual(updater.normalize_modes(app), 3)
        for dirpath, dirs, files in os.walk(app):
            for n in dirs:
                self.assertEqual(os.stat(os.path.join(dirpath, n)).st_mode & 0o7777, 0o755)
            for n in files:
                p = os.path.join(dirpath, n)
                if not os.path.islink(p):
                    self.assertIn(os.stat(p).st_mode & 0o7777, (0o644, 0o755), p)
        self.assertEqual(updater.normalize_modes(app), 0, "idempotent")

    def test_macho_archs(self):
        import struct
        p = os.path.join(self.tmp, "fat")
        with open(p, "wb") as f:
            f.write(struct.pack(">II", 0xCAFEBABE, 2))
            f.write(struct.pack(">IIIII", 0x01000007, 3, 0, 0, 0))
            f.write(struct.pack(">IIIII", 0x0100000C, 0, 0, 0, 0))
        self.assertEqual(updater.macho_archs(p), {"x86_64", "arm64"})
        with open(p, "wb") as f:
            f.write(b"#!/bin/sh\n")
        self.assertEqual(updater.macho_archs(p), set())



if __name__ == "__main__":
    unittest.main()
