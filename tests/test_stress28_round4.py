"""CR-28 stress run, round 4."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import plugin_install


class ReplacedFileKeepsItsName(unittest.TestCase):
    def test_deploy_and_remove_keep_the_original_name_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = Path(tmp) / "manager"
            data = Path(tmp) / "game" / "Data Files"
            package = manager / "staging" / "plugins" / "Mod"
            (data / "Meshes" / "f").mkdir(parents=True)
            (data / "Meshes" / "f" / "Furn_shrine_x_01.nif").write_bytes(b"original")
            (package / "Meshes" / "f").mkdir(parents=True)
            (package / "Meshes" / "f" / "furn_shrine_x_01.nif").write_bytes(b"replacement")
            (package / "Mod.esp").write_bytes(b"plugin")
            _linked, _skipped, operations = plugin_install.deploy_package(package, data, "Mod.esp")
            self.assertEqual(os.listdir(data / "Meshes" / "f"), ["Furn_shrine_x_01.nif"])
            self.assertEqual((data / "Meshes" / "f" / "Furn_shrine_x_01.nif").read_bytes(), b"replacement")
            state = {"version": 1, "deployments": [{
                "id": "mod", "label": "Mod", "plugins": ["Mod.esp"], "archives": [], "registered_archives": [],
                "staging_package": str(package), "mod": "Mod", "operations": operations}]}
            (manager / "plugin_deployments.json").write_text(json.dumps(state), encoding="utf-8")
            plugin_install.remove_deployments(manager, data, {"mod"})
            self.assertEqual(os.listdir(data / "Meshes" / "f"), ["Furn_shrine_x_01.nif"])
            self.assertEqual((data / "Meshes" / "f" / "Furn_shrine_x_01.nif").read_bytes(), b"original")
            self.assertFalse((data / "Mod.esp").exists())

    def test_a_new_file_keeps_the_mod_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "Other.nif").write_bytes(b"x")
            self.assertEqual(plugin_install._on_disk_name(folder / "new.nif"), folder / "new.nif")
            self.assertEqual(plugin_install._on_disk_name(folder / "other.NIF"), folder / "Other.nif")
            self.assertEqual(plugin_install._on_disk_name(folder / "gone" / "a.nif"), folder / "gone" / "a.nif")


class RecoveryCenterWords(unittest.TestCase):
    def test_codes_read_as_words(self):
        import mwmanager_gui
        self.assertEqual(mwmanager_gui.recovery_words("plugin_deployment"), "Plugin deployment")
        self.assertEqual(mwmanager_gui.recovery_words("needs-review"), "Needs review")
        self.assertEqual(mwmanager_gui.recovery_words("remove_major_master_deployments"),
                         "Remove major master deployments")
        self.assertEqual(mwmanager_gui.recovery_words(None), "")

    def test_report_names_the_item_and_skips_zero_counts(self):
        import mwmanager_gui
        item = {"label": "Run Fix-45947.7z", "action": "remove_plugin_deployment"}
        lines = mwmanager_gui.recovery_result_lines(
            item, {"action": "remove_plugin_deployment", "removed": 1, "restored": 0, "pending": 0,
                   "id": "plugin:24afc935"})
        self.assertEqual(lines, ["Remove plugin deployment: Run Fix-45947.7z", "Removed: 1"])
        self.assertNotIn("_", "\n".join(lines))
        self.assertEqual(mwmanager_gui.recovery_result_lines(item, {"recovered": False})[-1],
                         "Nothing needed changing.")


class NoNewToolStateInTheGameFolder(unittest.TestCase):
    def test_new_upstream_records_go_to_outputs(self):
        import sys
        import mwmanager_upstream
        mwmanager_upstream.compose()
        MP = sys.modules["mwdialog_paths"]
        import mwdialog_hashes
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp) / "game"
            (game / "Data Files").mkdir(parents=True)
            (game / "Morrowind.ini").write_text("[Game Files]\n", encoding="utf-8")
            manager = Path(tmp) / "manager"
            with mwmanager_upstream.use_manager_dir(manager) as scanner:
                record = mwdialog_hashes.record_path(str(game / "Data Files"))
                self.assertEqual(Path(record), Path(scanner.OUTPUT_DIR) / "mwdialog_play_hashes.json")
                self.assertNotEqual(Path(record).parent, game)
                (game / "mwdialog_luafix.json").write_text("{}", encoding="utf-8")       # one the player keeps
                self.assertEqual(Path(MP.state_path(str(game), "mwdialog_luafix.json")), game / "mwdialog_luafix.json")
                elsewhere = Path(tmp) / "not-a-game"
                self.assertEqual(Path(MP.state_path(str(elsewhere), "x.json")), elsewhere / "x.json")

class RemovalSummary(unittest.TestCase):
    def test_zero_counts_are_not_listed(self):
        import mwmanager_gui
        status, report = mwmanager_gui.removal_summary(
            {"removed": 428, "restored": 156, "pending": 0, "altered": [], "unpack_failures": []})
        self.assertEqual(status, "Removed 428 file(s); restored 156.")
        self.assertEqual(report, "Removed files: 428\nRestored files: 156")
        self.assertNotIn("<plugin>", report)

    def test_counts_that_need_attention_are_listed(self):
        import mwmanager_gui
        status, report = mwmanager_gui.removal_summary(
            {"removed": 3, "restored": 1, "pending": 2, "altered": ["a"], "unpack_failures": ["Mod.7z: gone"]})
        self.assertIn("2 changed file(s) stay tracked", status)
        self.assertIn("1 file(s) changed since deployment", status)
        self.assertIn("1 staged folder(s) could not be unpacked", status)
        self.assertTrue(report.endswith("Not unpacked again from the archive:\nMod.7z: gone"))
        self.assertNotIn(" 0 ", status)

if __name__ == "__main__":
    unittest.main()
