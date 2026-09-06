"""Coffre « Vlocal, ma voix » (v1.1.0) : création idempotente, notes de réunion, rappels."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import obsidian  # noqa: E402


class VoiceVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = os.path.join(self.tmp.name, "Vlocal, ma voix")
        # ne jamais toucher la vraie config Obsidian pendant les tests
        self._cfg = obsidian._OBSIDIAN_CONFIG
        obsidian._OBSIDIAN_CONFIG = os.path.join(self.tmp.name, "obsidian.json")

    def tearDown(self):
        obsidian._OBSIDIAN_CONFIG = self._cfg
        self.tmp.cleanup()

    def test_create_is_idempotent_and_structured(self):
        r = obsidian.create_voice_vault(self.vault)
        self.assertTrue(r["ok"] and r["created"])
        for sub in (".obsidian", ".vlocal", "Dictées", "Réunions", "Rappels"):
            self.assertTrue(os.path.isdir(os.path.join(self.vault, sub)), sub)
        for f in ("README.md", "CLAUDE.md", ".obsidian/daily-notes.json", ".vlocal/routing.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.vault, f)), f)
        readme = os.path.join(self.vault, "README.md")
        open(readme, "a", encoding="utf-8").write("\nnote perso\n")
        r2 = obsidian.create_voice_vault(self.vault)
        self.assertTrue(r2["ok"] and not r2["created"])
        self.assertIn("note perso", open(readme, encoding="utf-8").read())
        self.assertTrue(obsidian.is_vault(self.vault))

    def test_dictation_goes_to_daily_note_in_dictees(self):
        obsidian.create_voice_vault(self.vault)
        r = obsidian.capture("Penser à relancer le devis", self.vault, now_ts=1_800_000_000)
        self.assertTrue(r["ok"])
        self.assertTrue(r["path"].startswith(os.path.join(self.vault, "Dictées")))
        self.assertIn("- [ ]", open(r["path"], encoding="utf-8").read())

    def test_meeting_note_is_stable_across_rewrites(self):
        obsidian.create_voice_vault(self.vault)
        meeting = {"id": 91, "titre": "Point projet / client:test", "created_at": 1_800_000_000,
                   "duree_audio_s": 3600}
        blocks = [{"speaker": "SPEAKER_00", "start": 0, "end": 5, "text": "Bonjour."},
                  {"speaker": "SPEAKER_01", "start": 5, "end": 9, "text": "Salut."}]
        r1 = obsidian.capture_meeting(meeting, self.vault, blocks, {"SPEAKER_00": "Raphael"})
        self.assertTrue(r1["ok"])
        txt = open(r1["path"], encoding="utf-8").read()
        self.assertIn("**Raphael** (00:00)", txt)
        self.assertIn("**Voix 2** (00:05)", txt)
        self.assertIn("vlocal_meeting_id: 91", txt)
        self.assertNotIn("/", os.path.basename(r1["path"]).replace(".md", ""))
        meeting["titre"] = "Titre changé"
        r2 = obsidian.capture_meeting(meeting, self.vault, blocks, {})
        self.assertEqual(r1["path"], r2["path"], "une note par réunion, retrouvée par son id")
        self.assertEqual(len(os.listdir(os.path.join(self.vault, "Réunions"))), 1)

    def test_reminder_appended(self):
        obsidian.create_voice_vault(self.vault)
        for i in range(2):
            r = obsidian.capture_reminder("Appeler Max", "demain 9h", self.vault, now_ts=1_800_000_000)
            self.assertTrue(r["ok"])
        txt = open(os.path.join(self.vault, "Rappels", "Rappels.md"), encoding="utf-8").read()
        self.assertEqual(txt.count("- [ ] Appeler Max"), 2)
        self.assertIn("## Rappels", txt)

    def test_never_raises_on_missing_vault(self):
        self.assertFalse(obsidian.capture_meeting({"id": 1}, "/nonexistent/x")["ok"])
        self.assertFalse(obsidian.capture_reminder("x", "y", "/nonexistent/x")["ok"])


if __name__ == "__main__":
    unittest.main()
