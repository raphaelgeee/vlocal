"""Compteurs d'usage v1.2.0 : réunions, reprise de l'historique, chiffres exacts."""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402


def seed_v8_db(path, dictations, meetings):
    """Crée une base au schéma 8 (avant la v1.2.0) avec de l'historique."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE dictations(id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT 0, mode TEXT, chars INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE meetings(id INTEGER PRIMARY KEY AUTOINCREMENT, titre TEXT,
            created_at REAL, duree_audio_s REAL, duree_transcription_s REAL, wav_path TEXT,
            transcription_brute TEXT, transcription_structuree TEXT, status TEXT,
            confidence_json TEXT, avg_confidence REAL, speaker_blocks_json TEXT);
        CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, titre TEXT);
        CREATE TABLE glossary(id INTEGER PRIMARY KEY AUTOINCREMENT, terme TEXT);
        CREATE TABLE snippets(id INTEGER PRIMARY KEY AUTOINCREMENT, contenu TEXT);
        CREATE TABLE usage_days(day TEXT PRIMARY KEY, dictations INTEGER NOT NULL DEFAULT 0,
            words INTEGER NOT NULL DEFAULT 0, seconds_saved REAL NOT NULL DEFAULT 0);
    """)
    for content, ts in dictations:
        c.execute("INSERT INTO dictations(content,created_at,mode,chars) VALUES(?,?,?,?)",
                  (content, ts, "DICTEE", len(content)))
    for text, ts, dur in meetings:
        c.execute("INSERT INTO meetings(titre,created_at,duree_audio_s,transcription_structuree,status) "
                  "VALUES(?,?,?,?,?)", ("R", ts, dur, text, "ready"))
    c.execute("PRAGMA user_version=8")
    c.commit(); c.close()


class UsageStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "v.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_history_is_taken_over_on_migration(self):
        # 3 dictées de 100 mots et 1 réunion de 500 mots, il y a 5 jours
        old = time.time() - 5 * 86400
        seed_v8_db(self.path, [(" ".join(["mot"] * 100), old)] * 3,
                   [(" ".join(["mot"] * 500), old, 1800.0)])
        st = storage.Store(self.path)
        s = st.usage_stats()
        self.assertEqual(s["total"]["dictations"], 3)
        self.assertEqual(s["total"]["words"], 300)
        self.assertEqual(s["total"]["meetings"], 1)
        self.assertEqual(s["total"]["meeting_words"], 500)
        self.assertEqual(s["total"]["total_words"], 800)
        self.assertAlmostEqual(s["total"]["seconds_saved"],
                               300 * storage.SECONDS_SAVED_PER_WORD, places=2)
        self.assertAlmostEqual(s["total"]["meeting_seconds"], 1800.0, places=1)

    def test_migration_never_double_counts_recent_days(self):
        # une journée déjà comptée en direct (usage_days) ne doit pas être reprise
        today = time.strftime("%Y-%m-%d")
        seed_v8_db(self.path, [(" ".join(["mot"] * 40), time.time())], [])
        c = sqlite3.connect(self.path)
        c.execute("INSERT INTO usage_days(day,dictations,words,seconds_saved) VALUES(?,1,40,?)",
                  (today, 40 * storage.SECONDS_SAVED_PER_WORD))
        c.commit(); c.close()
        st = storage.Store(self.path)
        s = st.usage_stats()
        self.assertEqual(s["total"]["dictations"], 1, "pas de double comptage")
        self.assertEqual(s["total"]["words"], 40)

    def test_migration_is_idempotent(self):
        old = time.time() - 3 * 86400
        seed_v8_db(self.path, [(" ".join(["mot"] * 10), old)], [])
        first = storage.Store(self.path).usage_stats()["total"]["words"]
        second = storage.Store(self.path).usage_stats()["total"]["words"]
        self.assertEqual(first, second, "rouvrir la base ne recompte pas")
        self.assertEqual(first, 10)

    def test_live_counters_and_month_window(self):
        st = storage.Store(self.path)
        st.record_usage(120)
        st.record_meeting(900, 2400.0)
        st.record_usage(30, day="2020-01-05")          # hors mois en cours
        s = st.usage_stats()
        self.assertEqual(s["month"]["words"], 120)
        self.assertEqual(s["total"]["words"], 150)
        self.assertEqual(s["month"]["meetings"], 1)
        self.assertEqual(s["total"]["total_words"], 150 + 900)
        self.assertEqual(s["since_day"], "2020-01-05")
        self.assertTrue(all("day" in r for r in s["series"]))

    def test_empty_database_is_safe(self):
        s = storage.Store(self.path).usage_stats()
        self.assertEqual(s["total"]["words"], 0)
        self.assertIsNone(s["since_day"])
        self.assertEqual(s["series"], [])


if __name__ == "__main__":
    unittest.main()
