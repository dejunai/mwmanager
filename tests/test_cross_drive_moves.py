"""Moves between staging and Data Files work when the two are on different drives (WinError 17 / EXDEV).

Two folders under one temporary directory stand in for the drives; os.replace refuses any move between
them the way Windows does, and works inside one of them.
"""
import errno
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import plugin_install  # noqa: E402
import workflow_safety  # noqa: E402
from workflow_safety import DurableTransaction, move_file  # noqa: E402

REAL_REPLACE = os.replace


class Drives:
    def __init__(self, root):
        self.c, self.g = Path(root) / "C", Path(root) / "G"
        self.c.mkdir()
        self.g.mkdir()
        self.refused = 0

    def drive(self, path):
        path = Path(path).resolve()
        for top in (self.c, self.g):
            if path == top.resolve() or top.resolve() in path.parents:
                return top
        return None

    def replace(self, source, destination, *args, **kwargs):
        if self.drive(source) != self.drive(destination):
            self.refused += 1
            exc = OSError(errno.EXDEV if os.name != "nt" else 17,
                          "The system cannot move the file to a different disk drive")
            exc.winerror = 17
            raise exc
        return REAL_REPLACE(source, destination, *args, **kwargs)

    def patch(self):
        return mock.patch("os.replace", side_effect=self.replace)


class MoveFileTests(unittest.TestCase):
    def test_a_new_file_is_moved_across_drives(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Drives(tmp)
            source, target = d.c / "a.nif", d.g / "sub" / "a.nif"
            source.write_bytes(b"mesh")
            target.parent.mkdir()
            with d.patch():
                move_file(source, target)
            self.assertEqual(target.read_bytes(), b"mesh")
            self.assertFalse(source.exists())
            self.assertEqual(d.refused, 1)

    def test_an_existing_file_is_replaced_whole_across_drives(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Drives(tmp)
            source, target = d.c / "a.nif", d.g / "a.nif"
            source.write_bytes(b"new")
            target.write_bytes(b"old")
            with d.patch():
                move_file(source, target)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse(source.exists())
            self.assertEqual(sorted(p.name for p in d.g.iterdir()), ["a.nif"])  # no temporary left

    def test_other_errors_are_raised_and_the_source_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Drives(tmp)
            source, target = d.c / "a.nif", d.g / "a.nif"
            source.write_bytes(b"mesh")
            with mock.patch("os.replace", side_effect=PermissionError(13, "denied")):
                with self.assertRaises(PermissionError):
                    move_file(source, target)
            self.assertTrue(source.is_file())
            self.assertFalse(target.exists())

    def test_a_failed_copy_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Drives(tmp)
            source, target = d.c / "a.nif", d.g / "a.nif"
            source.write_bytes(b"mesh")

            def broken_copy(src, dst, *a, **k):
                Path(dst).write_bytes(b"me")
                raise OSError(28, "disk full")
            with d.patch(), mock.patch.object(workflow_safety.shutil, "copy2", side_effect=broken_copy):
                with self.assertRaises(OSError):
                    move_file(source, target)
            self.assertTrue(source.is_file())
            self.assertFalse(target.exists())

    def test_same_drive_moves_still_use_one_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Drives(tmp)
            source, target = d.g / "a.nif", d.g / "b.nif"
            source.write_bytes(b"mesh")
            with d.patch(), mock.patch.object(workflow_safety.shutil, "copy2") as copy2:
                move_file(source, target)
            copy2.assert_not_called()
            self.assertEqual(target.read_bytes(), b"mesh")


class CrossDriveDeploymentTests(unittest.TestCase):
    def layout(self, tmp):
        d = Drives(tmp)
        manager = d.c / "manager"
        package = manager / "staging" / "plugins" / "Mod"
        data = d.g / "game" / "Data Files"
        (package / "meshes").mkdir(parents=True)
        (data / "meshes").mkdir(parents=True)
        (package / "meshes" / "flora.nif").write_bytes(b"new-mesh")
        (data / "meshes" / "flora.nif").write_bytes(b"old-mesh")
        return d, manager, package, data

    def test_a_transaction_rollback_puts_the_original_back_across_drives(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, manager, package, data = self.layout(tmp)
            target = data / "meshes" / "flora.nif"
            with d.patch():
                transaction = DurableTransaction(
                    manager / "transactions" / "plugin-deploy", [manager, data.parent, package]).begin()
                plugin_install.deploy_package(package, data, "Mod.esp", transaction=transaction)
                self.assertEqual(target.read_bytes(), b"new-mesh")
                transaction.rollback()
            self.assertEqual(target.read_bytes(), b"old-mesh")
            self.assertTrue((package / "meshes" / "flora.nif").is_file())
            self.assertGreater(d.refused, 0)

    def test_removing_a_deployment_restores_its_backup_across_drives(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, manager, package, data = self.layout(tmp)
            target = data / "meshes" / "flora.nif"
            with d.patch():
                _linked, _skipped, operations = plugin_install.deploy_package(package, data, "Mod.esp")
                backup = Path(operations[0]["backup"])
                self.assertEqual(d.drive(backup), d.c)
                state = {"version": 1, "deployments": [{
                    "id": "mod", "label": "Mod", "plugins": [], "archives": [], "registered_archives": [],
                    "staging_package": str(package), "mod": "Mod", "operations": operations}]}
                (manager / "plugin_deployments.json").write_text(json.dumps(state), encoding="utf-8")
                result = plugin_install.remove_deployments(manager, data, {"mod"})
            self.assertEqual(target.read_bytes(), b"old-mesh")
            self.assertFalse(backup.exists())
            self.assertEqual(result.get("restored"), 1)


if __name__ == "__main__":
    unittest.main()
