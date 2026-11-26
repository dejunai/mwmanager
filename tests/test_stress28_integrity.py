"""Integrity drift found by the CR-28 stress run (round 1): right after a Deploy Plugins batch the GUI warned that 378
tracked files were "missing, modified, unreadable, or unverifiable". 372 were current deployments, which record the
size and time as deployed instead of a hash (plugin_install), reported as "predates content hashing"; 6 were new
plugins MWManager itself had disabled (moved to its disabled_plugins folder), reported as missing."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_integrity as I  # noqa: E402


class CurrentDeploymentsAreVerified(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.manager, self.data, self.package = root / "manager", root / "game" / "Data Files", root / "staging"
        for folder in (self.manager, self.data, self.package):
            folder.mkdir(parents=True)

    def deploy(self, *names):
        operations = []
        for name in names:
            source = self.package / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"data " + name.encode())
            target = self.data / name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, target)
            st = target.stat()
            operations.append({"source": str(source), "target": str(target), "size": st.st_size,
                               "mtime_ns": st.st_mtime_ns, "sha256": None})
        state = {"version": 1, "deployments": [{"id": "one", "label": "Mod", "operations": operations}]}
        (self.manager / "plugin_deployments.json").write_text(json.dumps(state), encoding="utf-8")

    def statuses(self):
        report = I.inspect(self.manager, self.data)
        return {Path(item["target"]).name: item["status"] for item in report["items"]}, report

    def test_a_hard_linked_deployment_with_size_and_time_is_ok(self):
        self.deploy("Mod.esp", "meshes/a.nif")
        statuses, report = self.statuses()
        self.assertEqual(statuses, {"Mod.esp": "ok", "a.nif": "ok"})
        self.assertEqual(I.changed_only(report), [])

    def test_a_changed_file_is_modified(self):
        self.deploy("meshes/a.nif")
        target = self.data / "meshes/a.nif"
        target.unlink()
        target.write_bytes(b"something else entirely")
        statuses, _report = self.statuses()
        self.assertEqual(statuses["a.nif"], "modified")

    def test_a_plugin_mwmanager_disabled_is_not_drift(self):
        self.deploy("New.esp", "Gone.esp")
        (self.manager / "disabled_plugins").mkdir()
        os.replace(self.data / "New.esp", self.manager / "disabled_plugins" / "New.esp")
        (self.data / "Gone.esp").unlink()
        statuses, report = self.statuses()
        self.assertEqual(statuses["New.esp"], "disabled")
        self.assertEqual(statuses["Gone.esp"], "missing")
        self.assertEqual([Path(i["target"]).name for i in I.changed_only(report)], ["Gone.esp"])


if __name__ == "__main__":
    unittest.main()
