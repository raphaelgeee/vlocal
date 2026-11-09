"""Isolates the whole test suite from the real data of the Mac running it.

Why: errors.py, settings.py, storage.py and others compute their paths from
`~` when they are imported. A test that forgets to fake one of them writes into
the user's real ~/Library (twice on 9 November 2026: three fake incidents in
events.jsonl). Discovery imports test modules in alphabetical order; this
module is named so that it is imported first, before any module of the app,
and points HOME to a temporary folder for the whole run.

A test that really needs a fixed folder still sets its own.
"""
import atexit
import os
import shutil
import tempfile
import unittest

_REAL_HOME = os.path.expanduser("~")
_TEST_HOME = tempfile.mkdtemp(prefix="vlocal-tests-home-")
os.environ["HOME"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)


class TestIsolation(unittest.TestCase):
    def test_app_paths_point_to_the_test_home(self):
        import errors
        import settings
        for path in (errors._LOG, errors._DIR):
            self.assertTrue(path.startswith(_TEST_HOME), path)
            self.assertFalse(path.startswith(os.path.join(_REAL_HOME, "Library")), path)
        settings_paths = [v for k, v in vars(settings).items()
                          if isinstance(v, str) and "Library" in v]
        for path in settings_paths:
            self.assertFalse(path.startswith(os.path.join(_REAL_HOME, "Library")), path)


if __name__ == "__main__":
    unittest.main()
