"""CR-26: the plugins of mods 'Install mods your options need' deploys (option-choice mods) are active right
after the deployment: never turned off by Generate Patches' save and re-enabled by --full (CR-25 error K).
Grass plugins, plugins listed as disabled (by the user, or Siege at Firemoth for Tamriel Rebuilt) and merged
plugins stay out. Every path here is a temporary folder."""
import json
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import option_plugins as op  # noqa: E402
import py_compat  # noqa: E402

GRASS = ROOT / "tests" / "fixtures" / "grass_rem_style.esp"
HEADER = struct.pack("<4sIII", b"TES3", 0, 0, 0)


class OptionPluginsEnabled(unittest.TestCase):
    def setUp(self):
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.manager, self.game = base / "MWManager", base / "Game"
        self.data = self.game / "Data Files"
        self.data.mkdir(parents=True)
        self.manager.mkdir()
        for name in ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm", "Tamriel_Data.esm", "Old Mod.esp",
                     "MW Containers Animated.esp", "Glowing_Mines.ESP", "NOD - Core.esm", "Siege at Firemoth.esp",
                     "Mine.esp", "Merged In.esp"):
            (self.data / name).write_bytes(HEADER)
        shutil.copy2(GRASS, self.data / "Rem-Grass.esp")
        self.sort = self.manager / "mwmanager_sort_order.txt"
        self.sort.write_text("; MWManager active load order. Core masters are fixed at the top.\n\nMorrowind.esm\n"
                             "Tribunal.esm\nBloodmoon.esm\nTamriel_Data.esm\nOld Mod.esp\n", encoding="utf-8")
        self.disabled = self.manager / "mwmanager_disabled.txt"
        self.disabled.write_text(
            "; Plugins disabled by MWManager\n\n"
            "Siege at Firemoth.esp\tTamriel Rebuilt deployed without Remove Firemoth: conflicts with TR_Mainland.esm\n"
            "Mine.esp\tdisabled in MWManager load-order panel\n", encoding="utf-8")

    def order(self):
        return op._read_lines(self.sort)

    def test_deployed_option_plugins_are_enabled_masters_first(self):
        enabled, skipped = op.enable_option_plugins(
            self.data, ["MW Containers Animated.esp", "Data Files/Glowing_Mines.ESP", "NOD - Core.esm"],
            self.sort, self.disabled)
        self.assertEqual(enabled, ["MW Containers Animated.esp", "Glowing_Mines.ESP", "NOD - Core.esm"])
        self.assertEqual(skipped, {})
        self.assertEqual(self.order(), ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm", "Tamriel_Data.esm",
                                        "NOD - Core.esm", "Old Mod.esp", "MW Containers Animated.esp",
                                        "Glowing_Mines.ESP"])

    def test_grass_disabled_merged_missing_and_active_stay_out(self):
        enabled, skipped = op.enable_option_plugins(
            self.data, ["Rem-Grass.esp", "Siege at Firemoth.esp", "Mine.esp", "Merged In.esp", "Gone.esp",
                        "Old Mod.esp", "readme.txt"], self.sort, self.disabled, merged=["Merged In.esp"])
        self.assertEqual(enabled, [])
        self.assertEqual(set(skipped), {"Rem-Grass.esp", "Siege at Firemoth.esp", "Mine.esp", "Merged In.esp",
                                        "Gone.esp", "Old Mod.esp"})
        self.assertIn("grass", skipped["Rem-Grass.esp"])
        self.assertIn("disabled", skipped["Siege at Firemoth.esp"])
        self.assertNotIn("Rem-Grass.esp", self.order())
        self.assertNotIn("Siege at Firemoth.esp", self.order())

    def test_a_plugin_only_in_data_files_grass_is_not_enabled(self):
        (self.data / "Grass").mkdir()
        shutil.copy2(GRASS, self.data / "Grass" / "Rem-TOTSP.esp")
        enabled, skipped = op.enable_option_plugins(self.data, ["Rem-TOTSP.esp"], self.sort, self.disabled)
        self.assertEqual(enabled, [])
        self.assertEqual(skipped["Rem-TOTSP.esp"], "not at the Data Files root")

    def test_the_fixture_is_grass(self):
        self.assertTrue(op._is_grass(self.data / "Rem-Grass.esp"))
        self.assertFalse(op._is_grass(self.data / "Old Mod.esp"))

    def test_nothing_new_leaves_the_file_untouched(self):
        before = self.sort.read_bytes()
        op.enable_option_plugins(self.data, ["Old Mod.esp"], self.sort, self.disabled)
        self.assertEqual(self.sort.read_bytes(), before)

    def test_the_change_is_a_journal_run_and_no_game_file_moves(self):
        import mwmanager_journal
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.game)
        op.enable_option_plugins(self.data, ["Glowing_Mines.ESP"], self.sort, self.disabled,
                                 self.manager / "journal")
        runs = [json.loads(l) for l in (self.manager / "journal" / "runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(any(r.get("tool") == "mwmanager_gui.py" for r in runs))
        self.assertEqual(journal.entries()[0], [])
        self.assertTrue(journal.verify(self.game, against="deployed", full_hash=True)["ok"])


class GuiWiring(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "mwmanager_gui.py").read_text(encoding="utf-8")

    def test_install_mods_your_options_need_enables_its_plugins(self):
        self.assertIn('title="Install mods your options need", enable_plugins=True', self.src)

    def test_plugins_are_enabled_before_the_batch_checks(self):
        body = self.src[self.src.index("def deploy_batch("):]
        self.assertLess(body.index("enable_option_plugins("), body.index("run_batch_check(progress)"))
        self.assertIn("SORT_FILE, DISABLED_RECORD", body[:body.index("run_batch_check(progress)")])

    def test_plain_deploy_plugins_does_not(self):
        self.assertIn("enable_plugins=False", self.src)


if __name__ == "__main__":
    unittest.main()