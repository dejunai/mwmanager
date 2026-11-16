"""MWManager's own option notes (mock_fomod_notes.json) are found by Nexus id or package name and option pattern."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod


class MaintainerNotesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = Path(tmp.name) / "Some Pack 123 1.0"
        self.plugin = self.mod / "Extras" / "Some Pack" / "02 Resource ESPs" / "Res.esp"
        self.notes = Path(tmp.name) / "notes.json"
        self.notes.write_text(json.dumps({"mods": [
            {"nexus_id": 123, "options": {"*Res.esp": "Modder's resource.", "*": "Whole package note."}},
            {"packages": ["Other *"], "options": {"*": "Not this one."}}]}), encoding="utf-8")
        patches = [mock.patch.object(mock_fomod, "MAINTAINER_NOTES", self.notes),
                   mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="123")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def item(self, path, label):
        return {"path": path, "label": label, "key": "nested-plugin:" + label.casefold(), "mod_dir": self.mod}

    def test_option_and_package_notes_are_found(self):
        self.assertEqual(mock_fomod._maintainer_notes(self.item(self.plugin, "02 Resource ESPs\\Res.esp")),
                         "Modder's resource.\n\nWhole package note.")
        other = self.item(self.mod / "Extras" / "Some Pack" / "01 HD", "01 HD")
        self.assertEqual(mock_fomod._maintainer_notes(other), "Whole package note.")

    def test_package_name_match_and_no_match(self):
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value=""):
            self.assertEqual(mock_fomod._maintainer_notes(self.item(self.plugin, "Res.esp")), "")
            renamed = Path(str(self.mod).replace("Some Pack", "Other Pack"))
            self.assertEqual(mock_fomod._maintainer_notes({"path": renamed / "x", "label": "x", "mod_dir": renamed}),
                             "Not this one.")

    def test_the_shipped_notes_file_is_valid(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "mock_fomod_notes.json").read_text(encoding="utf-8"))
        for mod in data["mods"]:
            self.assertTrue(mod.get("nexus_id") or mod.get("packages"))
            self.assertTrue(all(isinstance(text, str) and text for text in mod["options"].values()))


if __name__ == "__main__":
    unittest.main()
