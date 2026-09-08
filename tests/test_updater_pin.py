"""Mise à jour in-app : l'éditeur est épinglé (Team ID Apple), v1.1.2."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater  # noqa: E402

SAMPLE = """Executable=/Volumes/Vlocal/Vlocal.app/Contents/MacOS/Vlocal
Identifier=org.vlocal.app
Format=app bundle with Mach-O thin (arm64)
Authority=Developer ID Application: Raphael Gee (LW8B2TTQ4W)
Authority=Developer ID Certification Authority
Authority=Apple Root CA
TeamIdentifier=LW8B2TTQ4W
Runtime Version=15.0.0
"""


class TeamIdPinTests(unittest.TestCase):
    def test_our_team_is_accepted(self):
        self.assertTrue(updater.team_id_matches(SAMPLE))

    def test_other_team_is_refused(self):
        self.assertFalse(updater.team_id_matches(SAMPLE.replace("TeamIdentifier=LW8B2TTQ4W", "TeamIdentifier=ABCDE12345")))

    def test_missing_or_adhoc_is_refused(self):
        self.assertFalse(updater.team_id_matches(""))
        self.assertFalse(updater.team_id_matches("Signature=adhoc\nTeamIdentifier=not set\n"))

    def test_spoof_in_other_field_is_refused(self):
        # un champ Authority ou Identifier contenant la chaîne ne suffit pas
        self.assertFalse(updater.team_id_matches("Identifier=TeamIdentifier=LW8B2TTQ4W\nAuthority=LW8B2TTQ4W\n"))


if __name__ == "__main__":
    unittest.main()
