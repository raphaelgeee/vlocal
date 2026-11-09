"""Installateur détaché : le VRAI script shell, exécuté sur une arborescence
factice dans un dossier temporaire. open, pgrep, pkill, lsregister et xattr sont
remplacés par de petits scripts ; ditto est le vrai (copie locale) ; mv est le
vrai, ou un mv qui échoue à l'appel N pour simuler une panne au remplacement.

Critères : exception pendant le remplacement -> l'ancienne app reste intacte et
relancée ; nouvelle version qui ne confirme pas son démarrage -> retour arrière
automatique ; deux installations concurrentes -> une seule s'exécute.
"""
import os
import subprocess
import unittest

from test_update_support import IsolatedCase, bundle_version, make_app

import updater

TIMING = {"poll": 0.05, "pid_wait": 2, "confirm_timeout": 3, "grace": 0.2, "max_launches": 2}


class InstallerTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.opens = os.path.join(self.tmp, "opens.log")
        self.state = os.path.join(self.updates, "state")
        os.makedirs(self.state)
        make_app(self.apps, version="1.4.3")                  # version en place
        staging_parent = os.path.join(self.tmp, "staging")
        os.makedirs(staging_parent)
        self.staged = make_app(staging_parent, version="9.0.0")
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        self.pid = dead.pid                                    # app déjà fermée
        self.tools = {
            "OPEN": self._script("open", 'echo "$*" >> %s' % self.opens),
            "PGREP": self._script("pgrep", "exit 1"),          # aucun process vivant
            "PKILL": self._script("pkill", "exit 0"),
            "LSREGISTER": "/usr/bin/true", "XATTR": "/usr/bin/true",
        }

    def _script(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n%s\n" % body)
        os.chmod(path, 0o755)
        return path

    def _confirming_open(self):
        """La nouvelle version démarre et confirme (comme confirm_startup)."""
        self.tools["OPEN"] = self._script(
            "open", 'echo "$*" >> %s\necho 9.0.0 > %s/confirmed' % (self.opens, self.state))

    def _failing_mv(self, fail_on_call):
        counter = os.path.join(self.tmp, "mv.count")
        self.tools["MV"] = self._script("mv", (
            'n=$(cat %s 2>/dev/null || echo 0); n=$((n+1)); echo $n > %s\n'
            'if [ "$n" -eq %d ]; then echo "mv: panne simulée" >&2; exit 1; fi\n'
            'exec /bin/mv "$@"') % (counter, counter, fail_on_call))

    def _run(self, mode="install", pid=None, **timing):
        script = updater.render_installer(mode, self.pid if pid is None else pid, self.staged,
                                          self.target, tools=self.tools, timing=dict(TIMING, **timing))
        path = os.path.join(self.updates, "install.sh")
        with open(path, "w") as f:
            f.write(script)
        return subprocess.run(["/bin/sh", path], capture_output=True, text=True, timeout=60).returncode

    def _phase(self):
        with open(os.path.join(self.state, "phase")) as f:
            return f.read().strip()

    def _opens(self):
        if not os.path.exists(self.opens):
            return []
        with open(self.opens) as f:
            return f.read().splitlines()

    def _siblings(self):
        return sorted(n for n in os.listdir(self.apps) if n != "Vlocal.app")

    def test_confirmed_start_keeps_new_version_and_drops_backup(self):
        self._confirming_open()
        self.assertEqual(self._run(), 0)
        self.assertEqual(bundle_version(self.target), "9.0.0")
        self.assertEqual(self._phase(), "confirmed")
        self.assertEqual(self._siblings(), [], "sauvegarde et copies supprimées")
        self.assertEqual(self._opens(), ["-n " + self.target], "une seule relance")
        self.assertFalse(os.path.exists(self.staged))
        self.assertFalse(os.path.exists(os.path.join(self.state, "installer.lock")))

    def test_unconfirmed_start_rolls_back_to_previous_version(self):
        self.assertEqual(self._run(), 2)
        self.assertEqual(bundle_version(self.target), "1.4.3")
        self.assertEqual(self._phase(), "rolled_back")
        self.assertEqual(self._siblings(), [])
        # lancement, une relance après l'arrêt, puis l'ancienne version
        self.assertEqual(len(self._opens()), 3)

    def test_hung_new_version_is_stopped_and_rolled_back(self):
        self.tools["PGREP"] = self._script("pgrep", "exit 0")   # vivant mais muet
        kills = os.path.join(self.tmp, "kills.log")
        self.tools["PKILL"] = self._script("pkill", 'echo "$*" >> %s' % kills)
        self.assertEqual(self._run(confirm_timeout=0.6), 2)
        self.assertEqual(bundle_version(self.target), "1.4.3")
        with open(kills) as f:
            self.assertIn(self.target + "/Contents/MacOS/", f.read())
        self.assertEqual(len(self._opens()), 2, "pas de relance d'une version vivante")

    def test_copy_failure_leaves_old_app_intact_and_relaunched(self):
        self.tools["DITTO"] = self._script("ditto", "exit 1")
        self.assertEqual(self._run(), 1)
        self.assertEqual(bundle_version(self.target), "1.4.3")
        self.assertEqual(self._phase(), "failed:copy")
        self.assertEqual(self._siblings(), [])
        self.assertEqual(self._opens(), ["-n " + self.target])

    def test_failure_moving_old_app_aside_leaves_it_in_place(self):
        self._failing_mv(1)
        self.assertEqual(self._run(), 1)
        self.assertEqual(bundle_version(self.target), "1.4.3")
        self.assertEqual(self._phase(), "failed:permission")
        self.assertEqual(self._siblings(), [])

    def test_failure_between_the_two_renames_restores_old_app(self):
        self._failing_mv(2)                                     # incoming -> target
        self.assertEqual(self._run(), 1)
        self.assertEqual(bundle_version(self.target), "1.4.3")
        self.assertEqual(self._phase(), "failed:swap")
        self.assertEqual(self._siblings(), [])
        self.assertEqual(self._opens(), ["-n " + self.target])

    def test_interrupted_previous_install_is_repaired_first(self):
        os.rename(self.target, os.path.join(self.apps, ".Vlocal-backup.app"))
        self.tools["DITTO"] = self._script("ditto", "exit 1")
        self._run()
        self.assertEqual(bundle_version(self.target), "1.4.3")

    def test_app_still_running_aborts_without_touching_anything(self):
        self.assertEqual(self._run(pid=os.getpid(), pid_wait=0.3), 1)        # ce process ne meurt pas
        self.assertEqual(bundle_version(self.target), "1.4.3")
        self.assertEqual(self._phase(), "failed:still_running")
        self.assertEqual(self._opens(), [])

    def test_second_concurrent_installer_exits_without_acting(self):
        lock = os.path.join(self.state, "installer.lock")
        os.makedirs(lock)
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write(str(os.getpid()))                           # verrou tenu par un vivant
        self.assertEqual(self._run(), 3)
        self.assertEqual(bundle_version(self.target), "1.4.3")
        self.assertEqual(self._opens(), [])
        self.assertTrue(os.path.isdir(lock))

    def test_stale_lock_of_dead_installer_is_taken_over(self):
        lock = os.path.join(self.state, "installer.lock")
        os.makedirs(lock)
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write(str(self.pid))
        self._confirming_open()
        self.assertEqual(self._run(), 0)
        self.assertEqual(bundle_version(self.target), "9.0.0")

    def test_rollback_mode_restores_backup(self):
        backup = os.path.join(self.apps, ".Vlocal-backup.app")
        os.rename(self.target, backup)
        make_app(self.apps, version="9.0.0")                    # nouvelle version en place
        self.assertEqual(self._run(mode="rollback"), 2)
        self.assertEqual(bundle_version(self.target), "1.4.3")
        self.assertEqual(self._phase(), "rolled_back")
        self.assertFalse(os.path.exists(backup))

    def test_paths_with_spaces_and_quotes_are_safe(self):
        weird = os.path.join(self.tmp, "Mes Apps l'été")
        os.makedirs(weird)
        make_app(weird, version="1.4.3")
        self.target = os.path.join(weird, "Vlocal.app")
        self._confirming_open()
        self.assertEqual(self._run(), 0)
        self.assertEqual(bundle_version(self.target), "9.0.0")


if __name__ == "__main__":
    unittest.main()
