import json
import os
import tempfile
import unittest
from pathlib import Path

import mwmanager_integrity


class IntegrityTests(unittest.TestCase):
    def test_reports_ok_modified_and_missing_deployments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, data, package = root / "manager", root / "game" / "Data Files", root / "staging"
            manager.mkdir()
            data.mkdir(parents=True)
            package.mkdir()
            (package / "A.esp").write_bytes(b"same")
            (package / "B.esp").write_bytes(b"source")
            os.link(package / "A.esp", data / "A.esp")
            (data / "B.esp").write_bytes(b"changed")
            state = {"version": 1, "deployments": [{"id": "one", "label": "Test", "operations": [
                {"source": str(package / "A.esp"), "target": str(data / "A.esp"),
                 "sha256": mwmanager_integrity.sha256(package / "A.esp")},
                {"source": str(package / "B.esp"), "target": str(data / "B.esp"),
                 "sha256": mwmanager_integrity.sha256(package / "B.esp")},
                {"source": str(package / "Missing.esp"), "target": str(data / "Missing.esp")},
            ]}]}
            (manager / "plugin_deployments.json").write_text(json.dumps(state), encoding="utf-8")
            report = mwmanager_integrity.inspect(manager, data)
            by_name = {Path(item["target"]).name: item for item in report["items"]}
            self.assertEqual(by_name["A.esp"]["status"], "ok")
            self.assertEqual(by_name["A.esp"]["link"], "same-file")
            self.assertEqual(by_name["B.esp"]["status"], "modified")
            self.assertEqual(by_name["Missing.esp"]["status"], "missing")
            self.assertEqual(report["counts"], {"ok": 1, "modified": 1, "missing": 1})

    def test_latest_matching_override_is_the_active_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, data, staging = root / "manager", root / "Data Files", root / "staging"
            manager.mkdir()
            data.mkdir()
            staging.mkdir()
            (staging / "old.dds").write_bytes(b"old")
            (staging / "new.dds").write_bytes(b"new")
            (data / "shared.dds").write_bytes(b"new")
            state = {"version": 1, "deployments": [
                {"id": "old", "label": "Old", "operations": [{
                    "source": str(staging / "old.dds"), "target": str(data / "shared.dds"),
                    "sha256": mwmanager_integrity.sha256(staging / "old.dds")}]},
                {"id": "new", "label": "New", "operations": [{
                    "source": str(staging / "new.dds"), "target": str(data / "shared.dds"),
                    "sha256": mwmanager_integrity.sha256(staging / "new.dds")}]},
            ]}
            (manager / "plugin_deployments.json").write_text(json.dumps(state), encoding="utf-8")
            report = mwmanager_integrity.inspect(manager, data)
            self.assertEqual(report["counts"], {"ok": 1})
            self.assertEqual(report["items"][0]["deployment_id"], "new")

            (data / "shared.dds").write_bytes(b"old")
            restored_old = mwmanager_integrity.inspect(manager, data)
            self.assertEqual(restored_old["counts"], {"modified": 1})
            self.assertEqual(restored_old["items"][0]["deployment_id"], "new")

    def test_untracked_inventory_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, data = root / "manager", root / "Data Files"
            manager.mkdir()
            data.mkdir()
            (data / "loose.dds").write_bytes(b"loose")
            report = mwmanager_integrity.inspect(manager, data, include_untracked=True)
            self.assertEqual(len(report["untracked"]), 1)

    def test_snapshot_finds_added_removed_and_changed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, data = root / "manager", root / "Data Files"
            manager.mkdir()
            data.mkdir()
            (data / "same.txt").write_bytes(b"same")
            (data / "changed.txt").write_bytes(b"before")
            (data / "removed.txt").write_bytes(b"gone")
            mwmanager_integrity.capture_snapshot(manager, data, "before")
            (data / "changed.txt").write_bytes(b"after")
            (data / "removed.txt").unlink()
            (data / "added.txt").write_bytes(b"new")
            delta = mwmanager_integrity.compare_snapshot(manager, data, "before")
            self.assertEqual(delta["added"], ["added.txt"])
            self.assertEqual(delta["removed"], ["removed.txt"])
            self.assertEqual(delta["changed"], ["changed.txt"])
            self.assertEqual(delta["unchanged"], 1)


if __name__ == "__main__":
    unittest.main()
