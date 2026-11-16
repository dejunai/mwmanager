"""Readme notes shown with each option: always install, choose one, requirements checked against Data Files."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod

INSTALLED = {"tamriel_data.esm", "lefemmarmor.esp"}


class BadgeTests(unittest.TestCase):
    def badges(self, folder, docs=""):
        return mock_fomod._option_badges(folder, docs, INSTALLED)

    def test_core_and_choose_one(self):
        self.assertIn("Always install", self.badges("00 Core"))
        self.assertIn("Always install", self.badges("Main", "00 Core --> Always install"))
        self.assertIn("Choose one", self.badges("SSE Version (choose ONE)"))

    def test_requirements_are_checked_against_installed_plugins(self):
        self.assertIn("Requires TD (installed)", self.badges("03 TD to Wares", "03 TD to Wares - requires TD - adds"))
        self.assertIn("Requires OAAB DATA (not installed)", self.badges("04", "requires OAAB DATA - adds"))
        self.assertIn("Requires TR_MAINLAND (not installed)", self.badges("06", "requires TR_MAINLAND - gives lists"))
        self.assertIn("Requires LeFemmArmor (installed)", self.badges("patch for LeFemmArmor"))
        self.assertIn("Requires Glowing Bitter Coast", self.badges("11", "Install if you use Glowing Bitter Coast."))

    def test_phrases_and_engines_are_not_requirements(self):
        notes = self.badges("02 Scumpatibility", "Requires a texture replacer for the scum. Requires OpenMW.")
        self.assertFalse([note for note in notes if note.startswith("Requires")])
        self.assertEqual(self.badges("02 No MWSE - Merged"), [])
        self.assertIn("MWSE", self.badges("06 Typography Patch (MWSE)"))

    def test_install_order(self):
        self.assertIn("Install after Glowing Bitter Coast",
                      self.badges("11 Atlas", "11 Atlas - Install AFTER Glowing Bitter Coast"))


if __name__ == "__main__":
    unittest.main()
