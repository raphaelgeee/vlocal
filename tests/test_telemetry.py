"""Télémétrie déclarée (v1.1.0) : contenu exact des envois et compteurs d'usage."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402
import telemetry  # noqa: E402

ALLOWED_INSTALL_KEYS = {"install_id", "first_name", "last_name", "app_version",
                        "os_version", "last_seen_at"}
ALLOWED_USAGE_KEYS = {"install_id", "day", "dictations", "words", "seconds_saved"}


class UsageCountersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = storage.Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close() if hasattr(self.store, "close") else None
        self.tmp.cleanup()

    def test_record_usage_accumulates_per_day(self):
        self.store.record_usage(100, day="2026-09-06")
        self.store.record_usage(50, day="2026-09-06")
        self.store.record_usage(10, day="2026-09-05")
        rows = self.store.usage_days("2026-09-05")
        self.assertEqual([r["day"] for r in rows], ["2026-09-05", "2026-09-06"])
        today = rows[1]
        self.assertEqual(today["dictations"], 2)
        self.assertEqual(today["words"], 150)
        self.assertAlmostEqual(today["seconds_saved"],
                               150 * storage.SECONDS_SAVED_PER_WORD, places=3)
        tot = self.store.usage_totals()
        self.assertEqual((tot["dictations"], tot["words"]), (3, 160))

    def test_seconds_saved_matches_dashboard_formula(self):
        # accueil : mots/40 - mots/150 minutes
        words = 1000
        expected_min = words / 40 - words / 150
        self.assertAlmostEqual(words * storage.SECONDS_SAVED_PER_WORD / 60, expected_min)

    def test_word_count(self):
        self.assertEqual(storage.Store.word_count("  Bonjour à  tous\nça va "), 5)
        self.assertEqual(storage.Store.word_count(""), 0)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = storage.Store(os.path.join(self.tmp.name, "t.db"))
        self.settings = {"install_id": telemetry.new_install_id(),
                         "first_name": " Raphael ", "last_name": "Gee",
                         "telemetry_enabled": True,
                         # champs qui ne doivent JAMAIS partir
                         "license_email": "x@y.z", "obsidian_vault": "/Users/x/vault",
                         "hotkey": "ctrl_cmd"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_contain_only_declared_fields(self):
        self.store.record_usage(20)
        install, usage = telemetry.build_rows(self.settings, self.store, "1.1.0", "15.5")
        self.assertEqual(set(install), ALLOWED_INSTALL_KEYS)
        self.assertEqual(install["first_name"], "Raphael")
        self.assertEqual(len(usage), 1)
        self.assertEqual(set(usage[0]), ALLOWED_USAGE_KEYS)
        self.assertEqual(usage[0]["dictations"], 1)
        self.assertEqual(usage[0]["words"], 20)
        blob = repr((install, usage))
        for forbidden in ("x@y.z", "/Users/x/vault", "ctrl_cmd"):
            self.assertNotIn(forbidden, blob)

    def test_usage_window_is_last_three_days(self):
        old = time.strftime("%Y-%m-%d", time.localtime(time.time() - 10 * 86400))
        self.store.record_usage(5, day=old)
        self.store.record_usage(7)
        _, usage = telemetry.build_rows(self.settings, self.store, "1.1.0", "15.5")
        self.assertEqual([u["words"] for u in usage], [7])

    def test_disabled_or_undecided_sends_nothing(self):
        calls = []
        fake = lambda table, rows, oc: calls.append(table)
        for value in (None, False):
            s = dict(self.settings, telemetry_enabled=value)
            self.assertFalse(telemetry.sync_once(s, self.store, "1.1.0", "15", upsert=fake))
        self.assertEqual(calls, [])

    def test_enabled_upserts_both_tables_and_never_raises(self):
        calls = []
        fake = lambda table, rows, oc: calls.append((table, oc, len(rows)))
        self.store.record_usage(3)
        self.assertTrue(telemetry.sync_once(self.settings, self.store, "1.1.0", "15",
                                            upsert=fake))
        self.assertEqual(calls, [("installs", "install_id", 1),
                                 ("usage_days", "install_id,day", 1)])

        def boom(table, rows, oc):
            raise OSError("offline")
        self.assertFalse(telemetry.sync_once(self.settings, self.store, "1.1.0", "15",
                                             upsert=boom))


if __name__ == "__main__":
    unittest.main()
