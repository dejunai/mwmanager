"""Regression/failure-injection coverage. All game and archive files are temporary."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import core_addons as ca
import major_master_deploy as deploy
import mock_fomod
import mwmanager_gui as gui
import test_gui_smoke as gui_smoke


def put(root, rel, data):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def esm(*masters):
    body = b"".join(b"MAST" + struct.pack("<I", len(name) + 1) + name.encode() + b"\0" +
                    b"DATA" + struct.pack("<I", 8) + bytes(8) for name in masters)
    return struct.pack("<4sIII", b"TES3", len(body), 0, 0) + body


def prepare_deployment(root, baseline=True):
    package = root / "staging" / "major_masters" / "Package"
    put(package, "A.esm", esm())
    put(package, "Textures/a.dds", b"mod texture")
    data = root / "game" / "Data Files"
    data.mkdir(parents=True, exist_ok=True)
    put(data.parent, "Morrowind.ini", "[Game Files]\n")
    for name in ("catalog.py", "schema.sql"):
        target = root / "database" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(ROOT / "database" / name, target)
    if baseline:
        from mwmanager_journal import Journal
        Journal(root / "journal").capture_baseline(data.parent)
    return package, data


class BackendProbes(unittest.TestCase):
    def test_major_master_archive_is_localized_before_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, game = root / "manager", root / "game"
            manager.mkdir()
            game.mkdir()
            archive = root / "Vortex" / "A.zip"
            archive.parent.mkdir()
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("A.esm", esm())
            digest = ca._sha256(archive)
            plan = {"archives": [{
                "archive": str(archive), "core_masters": ["A.esm"],
                "sha256": digest, "member_count": 1, "install_blocked": False,
            }]}
            staged = ca.stage_core_master_archives(
                plan, ["A.esm"], manager, game, clear_staging=False)
            marker = json.loads((staged[0] / ".mwmanager-stage.json").read_text())
            cached = Path(marker["archive_path"])
            self.assertTrue(cached.is_file())
            self.assertTrue(cached.is_relative_to(manager / "archive_cache"))
            self.assertNotEqual(cached.resolve(), archive.resolve())
            self.assertEqual(ca._sha256(cached), digest)
            archive.unlink()
            self.assertEqual(ca.staged_source_archive(staged[0]), cached)

    def test_stale_partial_staging_tree_is_removed_despite_readonly_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / ".extract-leftover"
            child = stale / "nested"
            child.mkdir(parents=True)
            file = child / "held.txt"
            file.write_text("partial", encoding="utf-8")
            if os.name == "nt":
                os.chmod(file, 0o400)
                os.chmod(child, 0o400)
                os.chmod(stale, 0o400)
            ca._remove_staging_tree(stale)
            self.assertFalse(stale.exists())

    def test_untracked_live_hardlink_blocks_unstage_even_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root, baseline=False)
            os.link(package / "A.esm", data / "A.esm")
            self.assertFalse((root / "major_master_deployments.json").exists())
            with self.assertRaisesRegex(ValueError, "live deployed files"):
                deploy.unstage_packages(
                    [package], package.parent, data, root, ["A.esm"])
            self.assertTrue((data / "A.esm").exists())
            self.assertTrue(package.exists())

    def test_master_status_distinguishes_tracked_ready_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root, baseline=False)
            ready = deploy.deployment_status("A.esm", data, root, staged=True)
            self.assertEqual(ready["state"], "ready")
            os.link(package / "A.esm", data / "A.esm")
            untracked = deploy.deployment_status("A.esm", data, root, staged=True)
            self.assertEqual(untracked["state"], "untracked")
            digest = deploy.sha256(package / "A.esm")
            (root / "major_master_deployments.json").write_text(json.dumps({
                "version": 1,
                "deployments": [{
                    "masters": ["A.esm"],
                    "operations": [{
                        "source": str(package / "A.esm"),
                        "target": str(data / "A.esm"),
                        "sha256": digest,
                    }],
                }],
            }), encoding="utf-8")
            tracked = deploy.deployment_status("A.esm", data, root, staged=True)
            self.assertEqual(tracked["state"], "deployed")

            with mock.patch.object(deploy, "_read_deployment_state", wraps=deploy._read_deployment_state) as read:
                statuses = deploy.deployment_statuses(["A.esm", "B.esm"], data, root, staged={"b.esm"})
            self.assertEqual(read.call_count, 1)
            self.assertEqual(statuses["a.esm"]["state"], "deployed")
            self.assertEqual(statuses["b.esm"]["state"], "ready")

    def test_archive_name_collisions_keep_separate_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archives = []
            for filename, master in (("A+B.zip", "A.esm"), ("A@B.zip", "B.esm")):
                path = root / filename
                with zipfile.ZipFile(path, "w") as stream:
                    stream.writestr(master, esm())
                archives.append({"archive": str(path), "core_masters": [master],
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            staged = ca.stage_core_master_archives({"archives": archives}, ["A.esm", "B.esm"],
                                                  root, root / "game", clear_staging=False)
            self.assertEqual(len(set(staged)), 2)
            self.assertTrue((staged[0] / "A.esm").exists())
            self.assertTrue((staged[1] / "B.esm").exists())

    def test_modified_hardlinked_files_are_preserved_on_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root)
            with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
                deploy.deploy(package.parent, data, root, ["A.esm"])
            (data / "Textures/a.dds").write_bytes(b"user's in-place edit")
            result = deploy.remove_deployed(package.parent, data, root)
            self.assertGreater(result["pending"], 0)
            self.assertEqual(result["edited"], [str((data / "Textures/a.dds").resolve())])
            self.assertEqual((data / "Textures/a.dds").read_bytes(), b"user's in-place edit")
            saved = deploy.save_altered(result["edited"], data.parent, root)
            self.assertEqual((saved / "Data Files/Textures/a.dds").read_bytes(), b"user's in-place edit")
            # the user answers Yes: the edited file goes too, and nothing is left pending
            result = deploy.remove_deployed(package.parent, data, root, remove_edited=True)
            self.assertEqual(result["pending"], 0)
            self.assertFalse((data / "Textures/a.dds").exists())

    def test_unedited_files_are_removed_without_hashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root)
            with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
                deploy.deploy(package.parent, data, root, ["A.esm"])
            with mock.patch.object(deploy, "sha256", side_effect=AssertionError("hashed")):
                result = deploy.remove_deployed(package.parent, data, root)
            self.assertEqual((result["pending"], result["edited"]), (0, []))
            self.assertFalse((data / "Textures/a.dds").exists())

    def test_deploy_reports_package_scan_link_and_catalog_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root)
            progress = mock.Mock()
            with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
                deploy.deploy(package.parent, data, root, ["A.esm"], progress=progress)
            messages = [call.args[0] for call in progress.call_args_list]
            self.assertTrue(any("scanning deployable files" in message for message in messages))
            self.assertTrue(any("scan complete" in message for message in messages))
            self.assertTrue(any("Cataloguing major master" in message for message in messages))
            self.assertTrue(any("Deployment ownership recorded" in message for message in messages))

    def test_new_hardlinks_are_batched_and_keep_removal_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root)
            for index in range(600):
                put(package, f"Textures/batch-{index}.dds", b"x")
            with mock.patch("workflow_safety.os.fsync", wraps=os.fsync) as sync, \
                    mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
                deploy.deploy(package.parent, data, root, ["A.esm"])
            state = json.loads((root / "major_master_deployments.json").read_text())
            operations = state["deployments"][0]["operations"]
            self.assertTrue(all(operation.get("sha256") for operation in operations))
            self.assertLess(sync.call_count, len(operations) // 2)
            self.assertEqual(deploy.remove_deployed(package.parent, data, root)["pending"], 0)

    def test_deploy_reuses_precomputed_hashes_for_same_size_replacements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root)
            put(data, "Textures/a.dds", b"old texture")
            with mock.patch.object(deploy, "sha256", wraps=deploy.sha256) as hash_file, \
                    mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
                deploy.deploy(package.parent, data, root, ["A.esm"])
            hashes = hash_file.call_count
            self.assertLessEqual(hashes, 3, hashes)

    def test_removal_manifest_failure_restores_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root)
            put(data, "Textures/a.dds", b"old")
            with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
                deploy.deploy(package.parent, data, root, ["A.esm"])
            with mock.patch.object(deploy, "_write_deployment_state", side_effect=OSError("manifest full")):
                with self.assertRaisesRegex(OSError, "manifest full"):
                    deploy.remove_deployed(package.parent, data, root)
            self.assertTrue(os.path.samefile(data / "A.esm", package / "A.esm"))
            self.assertEqual((data / "Textures/a.dds").read_bytes(), b"mod texture")
            result = deploy.remove_deployed(package.parent, data, root)
            self.assertEqual(result["pending"], 0)
            self.assertEqual((data / "Textures/a.dds").read_bytes(), b"old")

    def test_deployment_partial_journal_failure_restores_originals(self):
        from mwmanager_journal import Journal
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root)
            put(data, "Textures/a.dds", b"old")
            record_many = Journal.record_many

            def fail_after_first(ledger, records):
                record_many(ledger, records[:1])
                raise OSError("partial journal failure")

            with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules), \
                    mock.patch.object(Journal, "record_many", fail_after_first):
                with self.assertRaisesRegex(OSError, "partial journal"):
                    deploy.deploy(package.parent, data, root, ["A.esm"])
            self.assertFalse((data / "A.esm").exists())
            self.assertEqual((data / "Textures/a.dds").read_bytes(), b"old")
            self.assertFalse((root / "journal/install.jsonl").exists())
            self.assertFalse((root / "major_master_deployments.json").exists())

    def test_archive_replaced_with_same_name_gets_new_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "downloads" / "SameName.zip"
            archive.parent.mkdir()

            def plan(payload):
                with zipfile.ZipFile(archive, "w") as stream:
                    stream.writestr("A.esm", payload)
                return {"archives": [{"archive": str(archive), "core_masters": ["A.esm"],
                                      "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}]}
            old = plan(b"old archive payload")
            package = ca.stage_core_master_archives(old, ["A.esm"], root, root / "game",
                                                     clear_staging=False)[0]
            new = plan(b"new archive payload")
            ca.stage_core_master_archives(new, ["A.esm"], root, root / "game", clear_staging=False)
            self.assertEqual((package / "A.esm").read_bytes(), b"new archive payload")
            marker = json.loads((package / ".mwmanager-stage.json").read_text())
            self.assertEqual(marker["sha256"], new["archives"][0]["sha256"])

    def test_wrapped_lua_image_stays_loose(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "Package"
            put(package, "Data Files/Textures/menu/logo.png", b"PNG")
            put(package, "Data Files/Textures/a.dds", b"DDS")
            put(package, "Data Files/MWSE/mods/x/main.lua", 'icon = "Textures/menu/logo.png"')
            packed = []

            def packer(command, cwd, **_kwargs):
                packed.extend(p.relative_to(cwd).as_posix() for p in Path(cwd).rglob("*") if p.is_file())
                put(Path(cwd), command[2], b"BSA")
                process = mock.Mock(stdout=io.StringIO(""))
                process.wait.return_value = 0
                return process
            with mock.patch.object(deploy.subprocess, "Popen", side_effect=packer):
                deploy._pack_assets(package, "A.esm", Path("bsapack.exe"))
            self.assertNotIn("Textures/menu/logo.png", packed)
            self.assertTrue((package / "Data Files/Textures/menu/logo.png").exists())

    def test_hard_tidy_error_is_not_retried_or_swallowed(self):
        outputs = [(0, "MWMANAGER-PLAN: pending plan.txt\n"),
                   (7, "FATAL: failed to persist journal\n"),
                   (0, "MWMANAGER-PLAN: empty plan.txt\n")]
        calls = []

        class Process:
            def __init__(self, command, **_kwargs):
                calls.append(command)
                self.code, text = outputs[len(calls) - 1]
                self.stdout = io.StringIO(text)

            def wait(self):
                return self.code

        progress = mock.Mock()
        with mock.patch.object(gui, "_popen", Process):
            with self.assertRaisesRegex(RuntimeError, "exit code 7"):
                gui.run_tidy_steps("python", Path("stage"), Path("data"), progress, "Adversarial")
        self.assertEqual(len(calls), 2)
        progress.ask_choice.assert_not_called()

    def test_deployment_ledger_failure_rolls_back_game_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root)
            with mock.patch("mwmanager_journal.Journal.record_deployment",
                            side_effect=OSError("simulated ledger disk full")), \
                    mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
                with self.assertRaisesRegex(OSError, "ledger disk full"):
                    deploy.deploy(package.parent, data, root, ["A.esm"], selected=["A.esm"])
            self.assertFalse((data / "A.esm").exists())
            self.assertFalse((root / "major_master_deployments.json").exists())
            self.assertFalse(deploy.recovery_pending(root))

    def test_regular_plugin_transaction_is_auto_recovered(self):
        from workflow_safety import DurableTransaction
        import mwmanager_recovery
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "staging" / "plugins" / "A"
            data = root / "game" / "Data Files"
            package.mkdir(parents=True)
            data.mkdir(parents=True)
            source, target = package / "A.esp", data / "A.esp"
            source.write_bytes(b"new")
            transaction = DurableTransaction(
                root / "transactions" / "plugin-deploy",
                [root, data.parent, package.parent]).begin()
            transaction.link(source, target, package / "Off" / "A.esp")
            inventory = mwmanager_recovery.build_recovery_inventory(root, data.parent, package.parent)
            selected = next(item for item in inventory["items"]
                            if item["kind"] == "transaction")
            result = mwmanager_recovery.recover_item(
                root, data.parent, selected, package.parent)
            self.assertTrue(result["recovered"])
            self.assertFalse(target.exists())
            self.assertFalse(transaction.root.exists())

    def test_repeated_snapshots_restore_the_transaction_start_state(self):
        from workflow_safety import DurableTransaction
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            root.mkdir(exist_ok=True)
            state.write_bytes(b"start")
            transaction = DurableTransaction(
                root / "transactions" / "snapshots", [root]).begin()
            transaction.snapshot(state)
            state.write_bytes(b"middle")
            transaction.snapshot(state)
            state.write_bytes(b"last")
            transaction.rollback()
            self.assertEqual(state.read_bytes(), b"start")

    def test_process_death_mid_deployment_recovers_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = prepare_deployment(root)
            code = textwrap.dedent("""
                import os,sys
                from pathlib import Path
                sys.path.insert(0, sys.argv[1])
                import major_master_deploy as d
                real_link=os.link
                calls=0
                def link(source,target):
                    global calls
                    calls+=1
                    if calls==2:
                        os._exit(86)
                    return real_link(source,target)
                os.link=link
                root=Path(sys.argv[2])
                d.deploy(root/'staging'/'major_masters',root/'game'/'Data Files',
                         root,['A.esm'],selected=['A.esm'])
            """)
            result = subprocess.run([sys.executable, "-c", code, str(ROOT), str(root)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 86, result.stderr)
            self.assertTrue((data / "A.esm").exists())
            self.assertFalse((root / "major_master_deployments.json").exists())
            self.assertTrue(deploy.recovery_pending(root))
            self.assertTrue(deploy.recover_deployment(root, data.parent))
            self.assertFalse((data / "A.esm").exists())
            with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
                result = deploy.deploy(package.parent, data, root, ["A.esm"], selected=["A.esm"])
            self.assertEqual(result["ready"], ["A.esm"])
            result = deploy.remove_deployed(package.parent, data, root)
            self.assertEqual(result["pending"], 0)
            self.assertFalse((data / "A.esm").exists())

    def test_fomod_cancel_is_a_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "stage" / "Package"
            put(package, "Extras/Option/Textures/a.dds", b"optional")
            data = root / "game" / "Data Files"
            data.mkdir(parents=True)
            record = {"key": "option:1", "path": package / "Extras/Option",
                      "label": "Optional", "targets": [], "number": 1}
            with mock.patch("nexus_updates.check_and_offer_update", return_value=False), \
                    mock.patch.object(mock_fomod, "_has_fomod", return_value=False), \
                    mock.patch.object(mock_fomod, "_discover", return_value=[record]), \
                    mock.patch.object(mock_fomod, "_discover_plugin_choices", return_value=[]), \
                    mock.patch.object(mock_fomod, "_show_selector", return_value=None):
                with self.assertRaises(mock_fomod.OptionsCancelled):
                    mock_fomod.process_staging(package.parent, data, root / "outputs")
            self.assertFalse((root / "outputs/mwmanager_compatibility_journal.json").exists())


@unittest.skipUnless(gui_smoke.TK_OK, "Tk display required")
class GuiProbes(unittest.TestCase):
    setUp = gui_smoke.GuiSmokeTests.setUp
    tearDown = gui_smoke.GuiSmokeTests.tearDown

    def package(self, name="A.esm", complete=False):
        self.app.major_masters = [name]
        package = self.g.APP_DIR / "staging" / "major_masters" / "Package"
        put(package, name, esm())
        put(package, ".mwmanager-stage.json",
            json.dumps({"archive": "A.zip", "selected_masters": [name]}))
        if complete:
            for step in ca.PACKAGE_STEPS:
                ca.complete_package_step(package, step)
        self.app._set_master_stage_ready([name], False)
        self.app.selected_major_masters = {name.casefold()}
        return package

    def capture_worker(self, title, description, worker, complete):
        self.job = (title, worker, complete)

    def test_successful_resume_is_ready_for_deployment(self):
        package = self.package()
        self.app._run_progress_job = self.capture_worker
        with mock.patch.object(self.g, "run_tidy_steps", return_value=None), \
                mock.patch("major_master_deploy.count_bsa_eligible", return_value=0):
            self.app.stage_major_masters()
            self.assertEqual(self.job[0], "Resume major-master staging")
            result = self.job[1](mock.Mock())
            self.assertEqual(result, 0)
            with mock.patch.object(self.app, "_after_major_master_staging"):
                self.job[2](result, None)
        self.assertEqual(ca.unfinished_packages(package.parent), [])
        self.assertEqual(self.app._unfinished_master_stages(["A.esm"]), [])
        with mock.patch.object(self.g.messagebox, "showwarning") as warning, \
                mock.patch.object(self.g.messagebox, "askyesno", return_value=False):
            self.app.deploy_major_masters()
        warning.assert_not_called()

    def test_completed_stage_clears_the_run_so_unstage_does_not_requeue_it(self):
        self.package(complete=True)
        self.app._set_staging_run(["A.esm"])
        with mock.patch.object(self.g.messagebox, "askyesno", return_value=False):
            self.app._after_major_master_staging(False, "Deploy?")
        self.assertNotIn("staging_run", self.app.gui_state)

    def test_cancel_resume_deploy_remove_end_to_end(self):
        root = self.g.APP_DIR
        # Real extraction, persisted failure, resumed package work, and real
        # deployment/removal; only the interactive option process is stubbed.
        archive = root / "A.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("A.esm", esm())
            stream.writestr("Textures/a.dds", b"texture")
        for name in ("catalog.py", "schema.sql"):
            target = root / "database" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / "database" / name, target)
        data = Path(self.app.settings["game_dir"]) / "Data Files"
        put(data.parent, "Morrowind.ini", "[Game Files]\n")
        from mwmanager_journal import Journal
        Journal(root / "journal").capture_baseline(data.parent)
        self.app.major_masters = ["A.esm"]
        self.app.selected_major_masters = {"a.esm"}
        package = ca.stage_core_master_archives(
            {"archives": [{"archive": str(archive), "core_masters": ["A.esm"]}]},
            ["A.esm"], root, data.parent)[0]
        self.app._set_master_stage_ready(["A.esm"], False)
        self.app._run_progress_job = self.capture_worker
        self.app.stage_major_masters()
        with mock.patch.object(self.g, "run_tidy_steps", side_effect=self.g.TidyPlanPaused("cancelled")):
            with self.assertRaises(self.g.TidyPlanPaused):
                self.job[1](mock.Mock())
        self.assertIsNotNone(ca.package_steps(package)["tidied"]["last_error"])
        self.assertEqual(self.app._unfinished_master_stages(["A.esm"]), ["A.esm"])
        self.app.stage_major_masters()
        with mock.patch.object(self.g, "run_tidy_steps"), \
                mock.patch.object(deploy, "count_bsa_eligible", return_value=0):
            self.job[1](mock.Mock())
        self.assertEqual(self.app._unfinished_master_stages(["A.esm"]), [])
        with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
            deploy.deploy(package.parent, data, root, ["A.esm"])
        self.assertTrue(os.path.samefile(data / "A.esm", package / "A.esm"))
        self.assertEqual(deploy.remove_deployed(package.parent, data, root)["pending"], 0)
        self.assertFalse((data / "A.esm").exists())
        self.assertEqual(deploy.tracked_deployment_masters(root), [])

    def test_reopen_is_not_redirected_by_unselected_optional_masters(self):
        self.package(complete=True)
        self.app.major_masters.append("Optional.esm")
        downloads = self.g.APP_DIR / "downloads"
        downloads.mkdir(exist_ok=True)
        self.app.settings["archive_downloads_dir"] = str(downloads)
        self.app._run_progress_job = self.capture_worker
        self.app.stage_major_masters()
        self.assertEqual(self.job[0], "Reopen major-master options")

    def test_orphan_hidden_extraction_does_not_count_as_a_finished_stage(self):
        staging = self.g.APP_DIR / "staging/major_masters"
        (staging / ".extract-interrupted").mkdir(parents=True)
        downloads = self.g.APP_DIR / "downloads"
        downloads.mkdir()
        self.app.settings["archive_downloads_dir"] = str(downloads)
        self.app._run_progress_job = self.capture_worker
        self.app.stage_major_masters()
        self.assertEqual(self.job[0], "Scanning major-master downloads")

    def test_cache_detects_new_nested_master(self):
        self.app.major_masters = ["Nested.esm"]
        package = self.g.APP_DIR / "staging/major_masters/Package"
        nested = package / "Data Files"
        nested.mkdir(parents=True)
        self.assertEqual(self.app._staged_major_master_names(), set())
        put(nested, "Nested.esm", esm())
        self.assertEqual(self.app._staged_major_master_names(), {"nested.esm"})

    def test_cache_detects_in_place_dependency_change(self):
        self.app.major_masters = ["A.esm", "B.esm"]
        package = self.g.APP_DIR / "staging/major_masters/Package"
        plugin = put(package, "B.esm", esm())
        self.assertEqual(self.app._required_major_master_names(), set())
        plugin.write_bytes(esm("A.esm"))
        self.assertEqual(self.app._required_major_master_names(), {"a.esm"})

    def test_unstaging_shared_deployed_package_is_blocked(self):
        root = self.g.APP_DIR
        package, data = prepare_deployment(root)
        put(package, "B.esm", esm())
        put(data, "Textures/a.dds", b"original game texture")
        self.app.major_masters = ["A.esm", "B.esm"]
        with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
            deploy.deploy(package.parent, data, root, ["A.esm", "B.esm"], selected=["A.esm"])
        state = json.loads((root / "major_master_deployments.json").read_text())
        backups = [Path(op["backup"]) for op in state["deployments"][0]["operations"]
                   if op.get("backup")]
        self.assertTrue(backups)
        self.assertTrue(all(path.exists() for path in backups))
        self.app.open_major_masters()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        tree = next(w for w in descendants(self.app._major_master_dialog)
                    if isinstance(w, self.g.ttk.Treeview))
        tree.selection_set("B.esm")
        self.app.update()
        self.assertTrue(self.app.unstage_master_button.instate(["disabled"]))
        with self.assertRaisesRegex(ValueError, "deployment|installed"):
            deploy.unstage_packages([package], package.parent, data, root, self.app.major_masters)
        self.assertTrue((data / "A.esm").exists())
        self.assertTrue(package.exists())
        self.assertTrue(all(path.exists() for path in backups))
        self.assertTrue(all(not path.is_relative_to(package) for path in backups))
        preserved = root / "journal/originals" / hashlib.sha256(b"original game texture").hexdigest()
        self.assertTrue(preserved.is_file())
        result = deploy.remove_deployed(package.parent, data, root)
        self.assertEqual(result["pending"], 0)
        self.assertEqual((data / "Textures/a.dds").read_bytes(), b"original game texture")

    def test_resume_leaves_rolled_back_deployment_ready_for_review(self):
        self.app.settings.update(first_run=0, init_complete=1)
        self.package("A.esm", complete=True)
        self.app._clear_major_master_scan_cache()
        self.app._setup_begin("major_masters_deployed")
        with mock.patch.object(self.app, "open_major_masters") as catalog, \
                mock.patch.object(self.app, "deploy_major_masters") as actual_deploy, \
                mock.patch.object(self.app, "after") as after:
            self.assertTrue(self.app._resume_interrupted_major_masters())
        catalog.assert_called_once()
        actual_deploy.assert_not_called()
        self.assertFalse(any(call.args[:2] == (300, actual_deploy) for call in after.call_args_list))
        self.assertIn("rolled back", self.app.status.get())
        self.assertTrue(self.app._setup_done("major_masters_deployed") is False)

    def test_interrupted_deploy_without_files_or_staging_is_reconciled(self):
        self.app.settings.update(first_run=0, init_complete=1)
        self.app.major_masters = ["A.esm"]
        self.app._setup_begin("major_masters_deployed")
        with mock.patch.object(self.app, "open_major_masters"), \
                mock.patch.object(self.app, "deploy_major_masters") as deploy_now, \
                mock.patch.object(self.app, "_initialize_plugin_baseline") as baseline:
            self.assertTrue(self.app._resume_interrupted_major_masters())
        deploy_now.assert_not_called()
        baseline.assert_called_once_with(force=True)
        self.assertTrue(self.app._setup_done("major_masters_deployed"))

    def test_enable_toggles_staged_master_but_not_a_required_one(self):
        self.app.major_masters = ["A.esm", "B.esm"]
        package = self.g.APP_DIR / "staging" / "major_masters" / "Package"
        put(package, "A.esm", esm())
        put(package, "B.esm", esm("A.esm"))
        self.app.selected_major_masters = {"a.esm", "b.esm"}
        self.app.open_major_masters()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        tree = next(widget for widget in descendants(self.app._major_master_dialog)
                    if isinstance(widget, self.g.ttk.Treeview))
        self.app._major_master_dialog.update()

        def click(name):
            x, y, w, h = tree.bbox(name, "active")
            tree.event_generate("<Button-1>", x=x + w // 2, y=y + h // 2)
            self.app.update()
            return tree.item(name)["values"][0]
        # A click must not rescan staging and Data Files: that froze the dialog for seconds per click.
        with mock.patch.object(self.app, "_staged_major_master_names", side_effect=AssertionError),                 mock.patch.object(self.app, "_required_major_master_names", side_effect=AssertionError):
            self.assertEqual(click("B.esm"), "☐")
            self.assertNotIn("b.esm", self.app.selected_major_masters)
            self.assertEqual(click("B.esm"), "☑")
            self.assertEqual(click("A.esm"), "☑")          # B needs A, so A stays locked on
        self.assertIn("a.esm", self.app.selected_major_masters)
        self.assertTrue(self.app.stage_master_button.instate(["!disabled"]))     # B is staged
        self.assertTrue(self.app.remove_master_button.instate(["disabled"]))     # nothing deployed

    def test_untracked_live_major_is_not_declared_deployed(self):
        package = self.package("A.esm", complete=True)
        data = Path(self.app.settings["game_dir"]) / "Data Files"
        put(data, "A.esm", esm())
        self.app.open_major_masters()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        tree = next(widget for widget in descendants(self.app._major_master_dialog)
                    if isinstance(widget, self.g.ttk.Treeview))
        self.assertEqual(tree.item("A.esm")["values"][1], "Untracked on disk")
        self.assertFalse(os.path.samefile(package / "A.esm", data / "A.esm"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
