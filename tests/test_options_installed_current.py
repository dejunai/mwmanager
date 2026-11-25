"""Options are checked against the plugins in Data Files now, not the compatibility journal's list from earlier runs:
a master removed since (a rollback, another major-master set) is missing, so options that need it are held instead
of deploying without it (CR-26 loop 4b: Wares 'OAAB to Wares' / PC options deployed without OAAB_Data / Cyr_Main)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod  # noqa: E402


class InstalledIsCurrentTests(unittest.TestCase):
    def test_removed_master_from_an_earlier_run_counts_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "stage" / "Wares"
            (package / "Extras" / "04 OAAB to Wares").mkdir(parents=True)
            (package / "Extras" / "04 OAAB to Wares" / "a.txt").write_text("x", encoding="utf-8")
            data = root / "game" / "Data Files"
            data.mkdir(parents=True)
            (data / "Present.esp").write_bytes(b"TES3")
            out = root / "outputs"
            out.mkdir()
            (out / "mwmanager_compatibility_journal.json").write_text(json.dumps(
                {"version": 1, "known_installed_plugins": ["oaab_data.esm", "present.esp"], "mods": {}}),
                encoding="utf-8")
            seen = []

            def discover(mod_dir, installed):
                seen.append(set(installed))
                return [{"key": "option:4", "path": package / "Extras" / "04 OAAB to Wares", "label": "OAAB to Wares",
                         "targets": [], "number": 4, "missing_masters": []}]

            with mock.patch("nexus_updates.check_and_offer_update", return_value=False), \
                    mock.patch.object(mock_fomod, "_has_fomod", return_value=False), \
                    mock.patch.object(mock_fomod, "_discover", side_effect=discover), \
                    mock.patch.object(mock_fomod, "_discover_plugin_choices", return_value=[]), \
                    mock.patch.object(mock_fomod, "_show_selector", return_value=set()):
                mock_fomod.process_staging(package.parent, data, out)
            self.assertTrue(seen)
            self.assertIn("present.esp", seen[0])
            self.assertNotIn("oaab_data.esm", seen[0])
            journal = json.loads((out / "mwmanager_compatibility_journal.json").read_text(encoding="utf-8"))
            self.assertNotIn("oaab_data.esm", journal["known_installed_plugins"])

    def test_installed_plugins_default_is_data_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "A.esm").write_bytes(b"TES3")
            self.assertEqual(mock_fomod._installed_plugins(data), {"a.esm"})


if __name__ == "__main__":
    unittest.main()