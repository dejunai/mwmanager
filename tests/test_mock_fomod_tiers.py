"""Option text is shared out among a package's options: each readme line goes to the option it names best."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod

README = """Example Mod
===========
Installation:
1. Extract the archive.
2. (Optional) Overwrite the .esp from 00 Core with the .esp from 01 Patch if you use Big Island.

00 Core
Always install.

02 - Merged
One plugin for everything.

- "03 Tavern Sounds" adds music to taverns
04-05 - A few texture varieties
python ./tool.py example_underwater.esp &>/dev/null
"""


def options(*specs):
    return [{"folder": folder, "number": number, "label": label, "plugins": list(plugins)}
            for folder, number, label, plugins in specs]


class TierTests(unittest.TestCase):
    def setUp(self):
        self.opts = options(("00 Core", 0, "Core", ["Example.esp"]),
                            ("01 Patch Big Island", 1, "Patch Big Island", ["Example.esp"]),
                            ("02 - Merged", 2, "Merged", ["Merged.esp"]),
                            ("03 Tavern Sounds", 3, "Tavern Sounds", []),
                            ("04 Example Textures 512", 4, "Example Textures 512", []),
                            ("Underwater", None, "Underwater", ["example_underwater.esp"]),
                            ("Example 2048", None, "Example 2048", []))
        self.text = mock_fomod._option_excerpts(README, self.opts)

    def test_a_heading_takes_its_section(self):
        self.assertIn("Always install.", self.text["00 Core"])
        self.assertIn("One plugin for everything.", self.text["02 - Merged"])
        self.assertNotIn("One plugin", self.text["00 Core"])

    def test_numbered_install_step_reaches_the_option_it_names(self):
        self.assertIn("if you use Big Island", self.text["01 Patch Big Island"])

    def test_quoted_names_and_number_ranges(self):
        self.assertIn("adds music to taverns", self.text["03 Tavern Sounds"])
        self.assertIn("A few texture varieties", self.text["04 Example Textures 512"])

    def test_command_lines_and_unmentioned_variants_get_nothing(self):
        self.assertEqual(self.text["Underwater"], "")
        variants = mock_fomod._option_excerpts(README, options(("Example 512", None, "Example 512", []),
                                                               ("Example 2048", None, "Example 2048", [])))
        self.assertEqual(variants["Example 2048"], "")          # "example" is in every label, "2048" nowhere

    def test_the_option_window_shows_the_matched_text_unchanged(self):
        record = {"label": "Core", "docs": self.text["00 Core"], "targets": []}
        shown = mock_fomod._option_description(record, Path("."))
        self.assertEqual(shown, self.text["00 Core"].strip())
        self.assertTrue(shown.startswith("00 Core\nAlways install."))   # the old filter dropped this body line


if __name__ == "__main__":
    unittest.main()
