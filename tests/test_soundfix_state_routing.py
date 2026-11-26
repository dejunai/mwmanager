"""Soundfix's report and the patch-inputs record stay out of the game install under MWManager: they go to its
outputs and its own folder. Outside a game folder every other state file keeps the path upstream gives it
(tests/test_stress28_round4.py covers game folders)."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402


class StateRouting(unittest.TestCase):
    def test_new_upstream_files_go_to_mwmanagers_folders(self):
        scanner = mwmanager_upstream.compose()
        MP = sys.modules["mwdialog_paths"]
        game = str(ROOT / "no-such-game")
        report = MP.state_path(game, "mwdialog_soundfix_report.txt")
        inputs = MP.state_path(game, "mwdialog_patch_inputs.json")
        self.assertEqual(report, os.path.join(scanner.OUTPUT_DIR, "mwdialog_soundfix_report.txt"))
        self.assertEqual(inputs, os.path.join(scanner.MANAGER_DIR, "mwdialog_patch_inputs.json"))
        self.assertEqual(MP.state_path(game, "mwdialog_load_order.txt"), os.path.join(game, "mwdialog_load_order.txt"))
        mwmanager_upstream.compose()                               # composing twice wraps it once
        self.assertTrue(MP.state_path.mwmanager_routed)
        self.assertEqual(MP.state_path(game, "mwdialog_soundfix_report.txt"), report)


if __name__ == "__main__":
    unittest.main()
