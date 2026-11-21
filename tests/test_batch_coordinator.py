import json
import threading
import time
import tempfile
import unittest
from pathlib import Path

import mwmanager_batch as batch


class BatchCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.coordinator = batch.BatchCoordinator.start(
            self.root,
            self.root / "game",
            [{"id": "A", "name": "Package A", "priority": 10},
             {"id": "B", "name": "Package B", "priority": 20}],
        )

    def ready(self, package_id, config_hash, priority):
        self.coordinator.transition(package_id, "EXTRACTING")
        self.coordinator.transition(package_id, "EXTRACTED")
        return self.coordinator.discover_fomod(
            package_id,
            installer_path=f"{package_id}/fomod/ModuleConfig.xml",
            config_hash=config_hash,
            option_records={"groups": [package_id]},
            source="test",
            priority=priority,
        )

    def test_durable_discovery_is_deduplicated_and_priority_ordered(self):
        first = self.ready("A", "a", 50)
        repeated = self.coordinator.discover_fomod(
            "A", installer_path="A/fomod/ModuleConfig.xml", config_hash="a",
            option_records={"groups": ["A"]}, source="test", priority=50)
        self.assertEqual(first, repeated)
        self.ready("B", "b", 5)

        item = self.coordinator.next_options()
        self.assertEqual(item.package_id, "B")
        self.coordinator.submit_answers(item.discovery_id, {"selected": ["base"]})
        self.assertEqual(self.coordinator.next_options().package_id, "A")

        saved = json.loads(self.coordinator.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["discoveries"][first]["state"], "ready")

    def test_resume_rebuilds_an_active_delivery_as_pending(self):
        discovery_id = self.ready("A", "a", 10)
        self.assertEqual(self.coordinator.next_options().discovery_id, discovery_id)

        resumed = batch.BatchCoordinator.resume(self.root)
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.next_options().discovery_id, discovery_id)

    def test_answered_discovery_is_not_republished_after_resume(self):
        discovery_id = self.ready("A", "a", 10)
        self.coordinator.submit_answers(discovery_id, {"selected": []})
        resumed = batch.BatchCoordinator.resume(self.root)
        self.assertIsNone(resumed.next_options())

    def test_illegal_phase_transition_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Illegal batch transition"):
            self.coordinator.transition("A", "READY_FOR_DEPLOYMENT")

    def test_nested_depth_limit_blocks_only_that_package(self):
        self.coordinator.transition("A", "EXTRACTING")
        self.coordinator.transition("A", "EXTRACTED")
        self.assertIsNone(self.coordinator.discover_fomod(
            "A", installer_path="nested.xml", config_hash="x", option_records={},
            source="nested", depth=batch.MAX_DISCOVERY_DEPTH + 1))
        self.assertEqual(self.coordinator.package("A")["phase"], "BLOCKED_USER_ACTION")
        self.assertEqual(self.coordinator.package("B")["phase"], "QUEUED_FOR_EXTRACTION")

    def test_prompt_gate_allows_dynamic_priority_to_run_next(self):
        gate = batch.PromptGate()
        order = []
        entered = threading.Event()
        release = threading.Event()

        def initial():
            with gate("initial", 50):
                order.append("initial")
                entered.set()
                release.wait(2)

        def normal():
            entered.wait(2)
            with gate("normal", 50):
                order.append("normal")

        def dynamic():
            entered.wait(2)
            time.sleep(0.02)
            with gate("dynamic", 0):
                order.append("dynamic")

        first = threading.Thread(target=initial)
        normal_thread = threading.Thread(target=normal)
        dynamic_thread = threading.Thread(target=dynamic)
        first.start()
        normal_thread.start()
        dynamic_thread.start()
        time.sleep(0.05)
        release.set()
        for thread in (first, normal_thread, dynamic_thread):
            thread.join(2)
        self.assertEqual(order, ["initial", "dynamic", "normal"])

    def test_threaded_batch_extracts_ahead_and_prioritizes_post_apply_rescan(self):
        second_extracting = threading.Event()
        events = []
        fingerprints = {"A": "0", "B": "0"}
        applies = {"A": 0, "B": 0}
        paths = {}
        for package_id in ("A", "B"):
            path = self.root / package_id
            path.mkdir()
            paths[package_id] = path

        def extract(item, _index):
            package_id = item["id"]
            if package_id == "B":
                second_extracting.set()
            return batch.BatchUnit(package_id, paths[package_id], {"name": package_id})

        def prepare(unit, reopen):
            events.append((unit.package_id, "rescan" if reopen else "initial"))
            if not reopen and unit.package_id == "A":
                self.assertTrue(second_extracting.wait(1), "B did not extract while A options were active")
            if reopen and unit.package_id == "A" and fingerprints["A"] == "0":
                fingerprints["A"] = "1"

        def apply(unit):
            applies[unit.package_id] += 1

        result = batch.run_threaded_batch(
            self.coordinator,
            [{"id": "A"}, {"id": "B"}],
            extract=extract,
            prepare_options=prepare,
            apply=apply,
            fingerprint=lambda unit: fingerprints[unit.package_id],
            config_fingerprint=lambda unit: "config-" + unit.package_id,
            reset_after_option_change=lambda unit: None,
            on_ready=lambda unit: events.append((unit.package_id, "ready")),
            progress=lambda _message: None,
        )
        self.assertEqual([unit.package_id for unit in result], ["A", "B"])
        self.assertLess(events.index(("A", "initial")), events.index(("A", "rescan")))
        self.assertEqual(applies, {"A": 2, "B": 1})
        self.assertTrue(all(coordinator == "READY_FOR_DEPLOYMENT" for coordinator in
                            (entry["phase"] for entry in self.coordinator.manifest["packages"].values())))

    def test_answered_rescan_with_no_answer_file_does_not_wait_forever(self):
        package_path = self.root / "A"
        package_path.mkdir()

        result = batch.run_threaded_batch(
            self.coordinator,
            [{"id": "A"}, {"id": "B"}],
            extract=lambda item, _index: batch.BatchUnit(
                item["id"], package_path, {"name": item["id"]}),
            prepare_options=lambda _unit, _reopen: None,
            apply=lambda _unit: None,
            fingerprint=lambda _unit: None,
            config_fingerprint=lambda unit: "config-" + unit.package_id,
            reset_after_option_change=lambda _unit: None,
            on_ready=lambda _unit: None,
            progress=lambda _message: None,
        )

        self.assertEqual([unit.package_id for unit in result], ["A", "B"])
        self.assertTrue(all(
            package["phase"] == "READY_FOR_DEPLOYMENT"
            for package in self.coordinator.manifest["packages"].values()
        ))

    def test_corrupt_fomod_blocks_only_its_package(self):
        paths = {}
        for package_id in ("A", "B"):
            paths[package_id] = self.root / package_id
            paths[package_id].mkdir()

        with self.assertRaisesRegex(RuntimeError, "invalid ModuleConfig"):
            batch.run_threaded_batch(
                self.coordinator,
                [{"id": "A"}, {"id": "B"}],
                extract=lambda item, _index: batch.BatchUnit(
                    item["id"], paths[item["id"]], {}),
                prepare_options=lambda unit, _reopen: (
                    (_ for _ in ()).throw(ValueError("invalid ModuleConfig"))
                    if unit.package_id == "A" else None),
                apply=lambda _unit: None,
                fingerprint=lambda _unit: "same",
                config_fingerprint=lambda unit: "config-" + unit.package_id,
                reset_after_option_change=lambda _unit: None,
                on_ready=lambda _unit: None,
                progress=lambda _message: None,
            )
        self.assertEqual(self.coordinator.package("A")["phase"], "BLOCKED_USER_ACTION")
        self.assertEqual(self.coordinator.package("B")["phase"], "READY_FOR_DEPLOYMENT")

    def test_blocked_rescan_does_not_stop_the_tidy_of_other_packages(self):
        paths = {}
        for package_id in ("A", "B"):
            paths[package_id] = self.root / package_id
            paths[package_id].mkdir()
        outcome = {}

        def prepare(unit, reopen):
            if reopen and unit.package_id == "A":
                raise ValueError("invalid nested ModuleConfig")

        def run():
            try:
                batch.run_threaded_batch(
                    self.coordinator,
                    [{"id": "A"}, {"id": "B"}],
                    extract=lambda item, _index: batch.BatchUnit(item["id"], paths[item["id"]], {}),
                    prepare_options=prepare,
                    apply=lambda _unit: None,
                    fingerprint=lambda _unit: "same",
                    config_fingerprint=lambda unit: "config-" + unit.package_id,
                    reset_after_option_change=lambda _unit: None,
                    on_ready=lambda _unit: None,
                    progress=lambda _message: None,
                )
            except RuntimeError as exc:
                outcome["error"] = str(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(10)
        if worker.is_alive():
            self.coordinator.cancel()
            self.fail("the batch hung after a blocked post-apply rescan")
        self.assertIn("invalid nested ModuleConfig", outcome.get("error", ""))
        self.assertEqual(self.coordinator.package("A")["phase"], "BLOCKED_USER_ACTION")
        self.assertEqual(self.coordinator.package("B")["phase"], "READY_FOR_DEPLOYMENT")


if __name__ == "__main__":
    unittest.main()
