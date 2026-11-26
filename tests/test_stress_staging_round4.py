"""Staging errors found by the CR-25 stress run (round 4): a planned folder move onto a folder that is already
there (MW Containers Animated: Data Files/Docs -> Docs) failed with WinError 183."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_install as ext  # noqa: E402
import mwmanager_upstream  # noqa: E402
import py_compat  # noqa: E402


class FolderMoveOntoExistingFolder(unittest.TestCase):
    def _apply(self, files, moves):
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        staging = Path(tmp.name) / "staging"
        mod = staging / "MW Containers Animated-42238"
        for rel, data in files.items():
            (mod / rel).parent.mkdir(parents=True, exist_ok=True)
            (mod / rel).write_bytes(data)
        with mwmanager_upstream.use_manager_dir(tmp.name):
            Path(tmp.name, "outputs").mkdir(exist_ok=True)
            failed = ext.apply([(str(mod / s), str(mod / d), "test") for s, d in moves], str(staging))
        return mod, failed

    def test_folder_joins_the_existing_folder(self):
        mod, failed = self._apply({"Docs/MWCA/readme.txt": b"readme",
                                   "Data Files/Docs/changes.txt": b"changes",
                                   "Data Files/Docs/sub/notes.txt": b"notes"},
                                  [("Data Files/Docs", "Docs")])
        self.assertEqual(failed, [])
        self.assertEqual((mod / "Docs/changes.txt").read_bytes(), b"changes")
        self.assertEqual((mod / "Docs/sub/notes.txt").read_bytes(), b"notes")
        self.assertEqual((mod / "Docs/MWCA/readme.txt").read_bytes(), b"readme")
        self.assertFalse((mod / "Data Files/Docs").exists())

    def test_identical_file_is_dropped_and_a_different_one_is_kept_and_reported(self):
        mod, failed = self._apply({"Docs/same.txt": b"same", "Docs/clash.txt": b"root",
                                   "Data Files/Docs/same.txt": b"same", "Data Files/Docs/clash.txt": b"other",
                                   "Data Files/Docs/new.txt": b"new"},
                                  [("Data Files/Docs", "Docs")])
        self.assertEqual(len(failed), 1)
        self.assertEqual((mod / "Docs/clash.txt").read_bytes(), b"root")
        self.assertEqual((mod / "Data Files/Docs/clash.txt").read_bytes(), b"other")
        self.assertFalse((mod / "Data Files/Docs/same.txt").exists())
        self.assertEqual((mod / "Docs/new.txt").read_bytes(), b"new")

    def test_plain_folder_move_still_renames(self):
        mod, failed = self._apply({"Data Files/Docs/a.txt": b"a"}, [("Data Files/Docs", "Docs")])
        self.assertEqual(failed, [])
        self.assertEqual((mod / "Docs/a.txt").read_bytes(), b"a")


if __name__ == "__main__":
    unittest.main()
