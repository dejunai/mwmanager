"""The step record in a staged package's .mwmanager-stage.json."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core_addons as ca


class StageStepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.staging = Path(self.tmp.name)

    def package(self, name, marker=None):
        folder = self.staging / name
        folder.mkdir()
        if marker is not None:
            (folder / ".mwmanager-stage.json").write_text(json.dumps(marker), encoding="utf-8")
        return folder

    def test_no_marker_needs_every_step(self):
        folder = self.package("partial")
        self.assertIsNone(ca.package_steps(folder))
        self.assertEqual(ca.unfinished_packages(self.staging), [(folder, list(ca.PACKAGE_STEPS))])

    def test_marker_from_before_steps_counts_as_extracted_only(self):
        folder = self.package("old", {"archive": "a.7z", "selected_masters": ["A.esm"]})
        steps = ca.package_steps(folder)
        self.assertTrue(steps["extracted"]["completed_at"])
        self.assertEqual(ca.unfinished_packages(self.staging), [(folder, ["tidied", "bsa_checked"])])

    def test_steps_are_stamped_and_attempts_counted(self):
        folder = self.package("pkg", {"archive": "a.7z", "selected_masters": ["A.esm"]})
        ca.begin_package_step(folder, "tidied")
        ca.fail_package_step(folder, "tidied", "it broke")
        entry = ca.package_steps(folder)["tidied"]
        self.assertEqual((entry["attempts"], entry["completed_at"], entry["last_error"]), (1, None, "it broke"))
        ca.begin_package_step(folder, "tidied")
        ca.complete_package_step(folder, "tidied")
        entry = ca.package_steps(folder)["tidied"]
        self.assertEqual((entry["attempts"], entry["last_error"]), (2, None))
        self.assertTrue(entry["started_at"] and entry["completed_at"])
        ca.complete_package_step(folder, "bsa_checked")
        self.assertEqual(ca.unfinished_packages(self.staging), [])
        data = json.loads((folder / ".mwmanager-stage.json").read_text(encoding="utf-8"))
        self.assertEqual(data["archive"], "a.7z")                    # the rest of the marker is kept
        with self.assertRaises(ValueError):
            ca.begin_package_step(folder, "nonsense")

    def test_partial_extraction_is_emptied_and_keeps_its_attempts(self):
        blank = {"started_at": None, "completed_at": None, "attempts": 0, "last_error": None}
        folder = self.package("cut", {"archive": "a.7z", "steps": {
            "extracted": {**blank, "started_at": "t", "attempts": 2}}})
        (folder / "half.nif").write_bytes(b"x")
        ca.reset_partial_package(folder)
        self.assertEqual([p.name for p in folder.iterdir()], [".mwmanager-stage.json"])
        self.assertEqual(ca.package_steps(folder)["extracted"]["attempts"], 2)
        bare = self.package("bare")
        (bare / "half.nif").write_bytes(b"x")
        ca.reset_partial_package(bare)
        self.assertFalse(bare.exists())

    def test_backups_are_time_stamped_and_never_overwrite(self):
        when = 1_800_000_000                                     # a fixed time, for a known stamp
        import time
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
        first = ca.stamped_backup_path(self.staging / "rock.dds", when)
        self.assertEqual(first.name, f"rock.dds.{stamp}")
        first.write_bytes(b"1")
        second = ca.stamped_backup_path(self.staging / "rock.dds", when)
        self.assertEqual(second.name, f"rock.dds.{stamp}-2")
        second.write_bytes(b"2")
        self.assertEqual(ca.stamped_backup_path(self.staging / "rock.dds", when).name, f"rock.dds.{stamp}-3")


if __name__ == "__main__":
    unittest.main()
