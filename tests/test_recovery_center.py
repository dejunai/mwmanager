"""Focused recovery-center inventory and recovery coverage."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mwmanager_recovery as recovery
from workflow_safety import DurableTransaction


class RecoveryCenterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manager = self.root / "manager"
        self.game = self.root / "game"
        self.data = self.game / "Data Files"
        self.data.mkdir(parents=True)
        self.manager.mkdir()

    def put(self, root: Path, relative: str, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def test_inventory_is_deterministic_and_does_not_mutate_files(self):
        package = self.manager / "staging/plugins/Example [1]"
        package.mkdir(parents=True)
        self.put(self.manager, "plugin_deployments.json", json.dumps({
            "version": 1,
            "deployments": [{
                "id": "plugin-1", "label": "Example\nplugin", "plugins": ["Example.esp"],
                "staging_package": str(package), "operations": [],
            }],
        }))
        self.put(self.manager, "major_master_deployments.json", json.dumps({
            "version": 1,
            "deployments": [{"masters": ["B.esm", "A.esm"], "operations": [], "moves": []}],
        }))
        self.put(package.parent, ".mwmanager-plugin-stages.json", json.dumps({
            "version": 1,
            "ready": [{
                "archive": "Example.zip", "staging_package": str(package),
                "selected_plugins": ["Example.esp"],
            }],
        }))
        transaction = DurableTransaction(
            self.manager / "transactions/major-master-deploy",
            [self.manager, self.game]).begin()
        transaction.state["phase"] = "active"
        transaction.save()
        journal = self.manager / "journal/install.jsonl"
        self.put(journal.parent, journal.name, '{"broken":true}\n')
        tracked = [
            self.manager / "plugin_deployments.json",
            self.manager / "major_master_deployments.json",
            self.manager / "staging/plugins/.mwmanager-plugin-stages.json",
            self.manager / "transactions/major-master-deploy/state.json",
            journal,
        ]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked}

        first = recovery.build_recovery_inventory(self.manager, self.game)
        second = recovery.scan_recovery(self.manager, self.game)

        self.assertEqual(first, second)
        json.dumps(first, sort_keys=True)
        self.assertEqual(before, {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked
        })
        self.assertEqual(first["counts"]["items"], 6)
        labels = {item["label"] for item in first["items"]}
        self.assertIn("Example plugin", labels)
        self.assertTrue(all("\n" not in label and "\r" not in label
                            for label in labels))
        journal_labels = [item["label"] for item in first["items"]
                          if item["kind"] == "problem" and item["source"] == "journal"]
        self.assertTrue(any("no path" in label for label in journal_labels))
        self.assertTrue(any("hash chain" in label for label in journal_labels))

    def test_selected_transaction_is_recovered_safely(self):
        source = self.put(self.manager, "staging/source", b"new")
        target = self.put(self.game, "Data Files/Test.esp", b"old")
        transaction = DurableTransaction(
            self.manager / "transactions/major-master-deploy",
            [self.manager, self.game]).begin()
        transaction.link(source, target, self.manager / "deployment_backups/Test.esp")
        inventory = recovery.build_recovery_inventory(self.manager, self.game)
        selected = next(item for item in inventory["items"] if item["kind"] == "transaction")

        result = recovery.recover_item(self.manager, self.game, selected)

        self.assertTrue(result["recovered"])
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(transaction.root.exists())

    def test_plugin_deployment_recovery_preserves_existing_api_scope(self):
        self.put(self.manager, "plugin_deployments.json", json.dumps({
            "version": 1,
            "deployments": [
                {"id": "chosen", "label": "Chosen", "plugins": ["A.esp"], "operations": []},
                {"id": "other", "label": "Other", "plugins": ["B.esp"], "operations": []},
            ],
        }))
        inventory = recovery.build_recovery_inventory(self.manager, self.game)
        selected = next(item for item in inventory["items"]
                        if item.get("details", {}).get("deployment_id") == "chosen")
        with mock.patch("plugin_install.remove_deployments",
                        return_value={"removed": 0, "restored": 0, "pending": 0,
                                      "tidy_packages": []}) as remove:
            recovery.recover_selected(self.manager, self.game, selected["id"])

        remove.assert_called_once_with(self.manager, self.data, {"chosen"})

    def test_recovery_rejects_review_only_and_changed_selection(self):
        self.put(self.manager, "journal/install.jsonl", "[]\n")
        inventory = recovery.build_recovery_inventory(self.manager, self.game)
        problem = next(item for item in inventory["items"] if item["kind"] == "problem")
        with self.assertRaisesRegex(RuntimeError, "needs review"):
            recovery.recover_item(self.manager, self.game, problem)

        self.put(self.manager, "plugin_deployments.json", json.dumps({
            "version": 1, "deployments": [
                {"id": "safe", "label": "../unsafe\nname", "plugins": [],
                 "operations": []},
            ],
        }))
        item = next(value for value in recovery.scan_recovery(
            self.manager, self.game)["items"] if value["kind"] == "plugin_deployment")
        (self.manager / "plugin_deployments.json").write_text(
            '{"version":1,"deployments":[]}\n', encoding="utf-8")
        with self.assertRaisesRegex(KeyError, "not found"):
            recovery.recover_item(self.manager, self.game, item)


if __name__ == "__main__":
    unittest.main()
