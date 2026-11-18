"""Workflow tests use a temporary fake game, never the user's installation."""
import sys
import os
import shutil
import struct
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mwmanager_workflow import (JobCancelled, JobControl, checkpoint, log_tone,
                                preflight_signature, progress_fraction)
import test_gui_smoke as smoke


class WorkflowLogicTests(unittest.TestCase):
    def test_bsa_build_reports_file_progress_and_packer_wait(self):
        from major_master_deploy import _pack_assets
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "package"
            textures = package / "Textures"
            textures.mkdir(parents=True)
            for index in range(2001):
                (textures / f"asset-{index}.dds").write_bytes(b"x")
            messages = []

            class FakeProcess:
                stdout = iter(("Packed 500 files\n", "Starting segment 2\n"))

                def wait(self, timeout=None):
                    return 0

                def kill(self):
                    pass

            def fake_popen(*_args, **kwargs):
                (Path(kwargs["cwd"]) / "Example_assets.bsa").write_bytes(b"bsa")
                return FakeProcess()

            with mock.patch("major_master_deploy.subprocess.Popen", side_effect=fake_popen):
                packed = _pack_assets(package, "Example.esm", Path("bsapack.exe"), messages.append)

            self.assertTrue(packed[0].is_file())
            self.assertIn("Preparing BSA for Example.esm: 2,000 assets ready…", messages)
            self.assertIn("Building BSA for Example.esm from 2,001 assets; please be patient…", messages)
            self.assertIn("BSAPack: Packed 500 files", messages)
            self.assertIn("BSAPack: Starting segment 2", messages)

    def test_cancellation_is_cooperative_and_opt_in(self):
        control = JobControl()
        control.check()
        control.cancelled.set()
        with self.assertRaises(JobCancelled):
            control.check()
        checkpoint(lambda _message: None)   # old CLI/test progress functions

    def test_diagnostic_prefixes_not_filenames_set_severity(self):
        for text in ("0 errors", "Mod failed adventures.esp", "Textures/error.dds", "No errors found"):
            self.assertEqual(log_tone(text), "progress")
        self.assertEqual(log_tone("  ERROR: cannot read"), "error")
        self.assertEqual(log_tone("Warning: missing texture"), "attention")
        self.assertEqual(log_tone("Finished staging"), "complete")

    def test_counts_are_determinate_only_when_valid(self):
        self.assertEqual(progress_fraction("Staging master 2 of 3: X"), (2, 3))
        self.assertEqual(progress_fraction("1,000 of 2,000 files"), (1000, 2000))
        self.assertIsNone(progress_fraction("still working"))
        self.assertIsNone(progress_fraction("1 of 0"))

    def test_preflight_cache_invalidates_after_change_or_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            exe = game / "Morrowind.exe"
            exe.write_bytes(b"old")
            before = preflight_signature(game)
            self.assertEqual(before, preflight_signature(game))
            exe.write_bytes(b"new executable")
            self.assertNotEqual(before, preflight_signature(game))
            exe.unlink()
            self.assertNotEqual(before, preflight_signature(game))

    def test_native_conflict_review_uses_themed_picker_not_console_input(self):
        import mwmanager_upstream
        with tempfile.TemporaryDirectory() as tmp, mwmanager_upstream.use_manager_dir(tmp) as scanner:
            args = types.SimpleNamespace(data_dir=str(Path(tmp) / "Data Files"), dry_run=True,
                                         use_answers=False, only_new=False)
            conflict = {"place": [], "refs": [], "settings": [], "doubled": False, "dups": 0}
            with mock.patch.dict(os.environ, {"MWMANAGER_CONFLICT_GUI": "1"}), \
                    mock.patch("mwmanager_workflow.conflict_answer", return_value="s") as picker, \
                    mock.patch("builtins.input", side_effect=AssertionError("No console input in GUI mode")):
                self.assertEqual(scanner.conflict_session(
                    args, [(("a.esp", "b.esp"), conflict)],
                    {"a.esp": "A.esp", "b.esp": "B.esp"}, {}, 1), 0)
            picker.assert_called_once()
            labels = picker.call_args.args[2]
            self.assertIn(("Skip this pair", "s"), labels)

    def test_backend_selection_does_not_deploy_other_staged_packages(self):
        from major_master_deploy import deploy
        with tempfile.TemporaryDirectory() as tmp:
            manager = Path(tmp)
            staging, data = manager / "staging" / "major_masters", manager / "game" / "Data Files"
            data.mkdir(parents=True)
            (data.parent / "Morrowind.ini").write_text("[Game Files]\n")
            from mwmanager_journal import Journal
            Journal(manager / "journal").capture_baseline(data.parent)
            database = manager / "database"
            database.mkdir()
            original = Path(__file__).resolve().parents[1] / "database"
            for name in ("catalog.py", "schema.sql"):
                shutil.copy2(original / name, database / name)
            for name in ("Selected.esm", "Unselected.esm"):
                package = staging / Path(name).stem
                package.mkdir(parents=True)
                (package / name).write_bytes(struct.pack("<4sIII", b"TES3", 0, 0, 0))
                texture = package / "Textures" / f"{Path(name).stem}.dds"
                texture.parent.mkdir()
                texture.write_bytes(b"test asset")
            with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
                result = deploy(staging, data, manager, ["Selected.esm", "Unselected.esm"],
                                selected=["Selected.esm"])
            self.assertEqual(result["ready"], ["Selected.esm"])
            self.assertTrue((data / "Selected.esm").is_file())
            self.assertTrue((data / "Textures" / "Selected.dds").is_file())
            self.assertFalse((data / "Unselected.esm").exists())
            self.assertFalse((data / "Textures" / "Unselected.dds").exists())


@unittest.skipUnless(smoke.TK_OK, "Tk display unavailable")
class WorkflowGuiTests(unittest.TestCase):
    setUp = smoke.GuiSmokeTests.setUp
    tearDown = smoke.GuiSmokeTests.tearDown
    pump = smoke.GuiSmokeTests.pump

    def finish_job(self):
        for _ in range(80):
            self.pump(0.04)
            if self.app._active_job is None:
                break
        self.assertIsNone(self.app._active_job)
        self.assertEqual(self.errors, [])

    def test_setup_reuses_one_window_and_keeps_output(self):
        self.app.settings["init_complete"] = 0
        done = []
        self.app._run_progress_job("Initial setup", "Sort...", lambda report: report("First step"),
                                   lambda r, e: done.append(e))
        self.finish_job()
        first = self.app._setup_view.window
        self.app._run_progress_job("Catalog", "Index...", lambda report: report("Second step"),
                                   lambda r, e: done.append(e))
        self.finish_job()
        self.assertEqual(first, self.app._setup_view.window)
        text = self.app._setup_view.log.get("1.0", "end")
        self.assertIn("First step", text)
        self.assertIn("Second step", text)
        self.assertEqual(done, [None, None])

    def test_follow_output_stays_at_bottom_until_disabled(self):
        from mwmanager_workflow import JobView
        view = JobView(self.app)
        self.app.update_idletasks()
        for index in range(200):
            view.append(f"Log item {index}")
        self.app.update_idletasks()
        view.log.yview_moveto(0.25)
        view.append("A new log item")
        self.app.update_idletasks()
        self.assertGreater(view.log.yview()[1], 0.99)
        view.follow.set(False)
        view.log.yview_moveto(0.25)
        view.append("Read without following")
        self.assertLess(view.log.yview()[1], 0.8)

    def test_hardlink_heartbeat_updates_stage_without_log_spam(self):
        from mwmanager_workflow import JobView
        view = JobView(self.app)
        view.append("MWMANAGER-STAGE: Hardlinking OAAB: 500 files scanned; 500 linked")
        self.assertIn("500 files scanned", view.stage.get())
        self.assertNotIn("Hardlinking OAAB", view.log.get("1.0", "end"))

    def test_cancel_waits_for_safe_boundary_and_all_actions_restore(self):
        started, release = threading.Event(), threading.Event()
        done = []

        def worker(report):
            started.set()
            release.wait(3)
            report.check_cancelled()
            return "should not finish"

        self.app.settings["init_complete"] = 0
        self.app._run_progress_job("Initial setup", "Working", worker, lambda r, e: done.append(r))
        self.pump(0.1)
        self.assertTrue(started.is_set())
        self.assertTrue(self.app.generate_button.instate(["disabled"]))
        self.app._job_view.request_cancel()
        self.assertIsNotNone(self.app._active_job)
        release.set()
        self.finish_job()
        self.assertEqual(done, [])
        # Setup is still incomplete: mutations remain disabled, but Resume and
        # Options are available instead of stranding the entire application.
        self.assertTrue(self.app.generate_button.instate(["disabled"]))
        self.assertEqual(self.app.file_menu.entrycget(0, "state"), "normal")
        self.assertIn("safe boundary", self.app.status.get())

    def test_empty_downloads_is_not_the_working_directory(self):
        self.app.settings["archive_downloads_dir"] = ""
        with mock.patch.object(self.g.messagebox, "showwarning") as warning, \
                mock.patch.object(self.app, "open_options") as options, \
                mock.patch.object(self.app, "_run_progress_job") as job:
            self.app.stage_major_masters(force_scan=True)
        warning.assert_called_once()
        options.assert_called_once()
        job.assert_not_called()

    def test_same_session_resume_keeps_confirmed_paths_and_checkpoints(self):
        self.app.settings.update(first_run=1, paths_confirmed=False)
        self.app._setup_start()
        self.app._setup_complete("preflight_complete")
        with mock.patch.object(self.app, "_begin_first_run") as resume, \
                mock.patch.object(self.app, "_open_detected_paths") as choose_paths:
            self.app._resume_setup()
        resume.assert_called_once()
        choose_paths.assert_not_called()
        self.assertTrue(self.app._setup_done("preflight_complete"))

    def stage(self):
        self.app.major_masters = ["Selected.esm", "Unselected.esm"]
        self.app.selected_major_masters = {"selected.esm"}
        staging = self.g.APP_DIR / "staging" / "major_masters"
        paths = {}
        for name in self.app.major_masters:
            package = staging / Path(name).stem
            package.mkdir(parents=True)
            plugin = package / name
            plugin.write_bytes(b"TES3")
            paths[name.casefold()] = plugin
        self.app._set_master_stage_ready(self.app.major_masters, True)
        return paths

    def test_deploy_cancel_precedes_preflight(self):
        self.stage()
        with mock.patch.object(self.g.messagebox, "askyesno", return_value=False), \
                mock.patch.object(self.app, "_run_progress_job") as job:
            self.app.deploy_major_masters()
        job.assert_not_called()

    def test_deploy_passes_only_selected_masters(self):
        self.stage()
        self.app._preflight_signature = preflight_signature(self.app.settings["game_dir"])
        jobs = []
        self.app._run_progress_job = lambda title, initial, worker, complete: jobs.append((title, worker))
        with mock.patch.object(self.g.messagebox, "askyesno", return_value=True), \
                mock.patch("plugin_install.check_deploy_path_lengths"), \
                mock.patch("major_master_deploy.deploy", return_value={}) as deploy:
            self.app.deploy_major_masters()
            self.assertEqual(jobs[0][0], "Deploy Major Masters")
            jobs[0][1](lambda _line: None)
        self.assertEqual(deploy.call_args.kwargs["selected"], ["Selected.esm"])
        self.assertEqual(deploy.call_args.args[3], ["Selected.esm", "Unselected.esm"])

    def test_plugins_deployed_with_a_master_stay_active(self):
        self.stage()
        self.app._preflight_signature = preflight_signature(self.app.settings["game_dir"])
        jobs = []
        self.app._run_progress_job = lambda title, initial, worker, complete: jobs.append(complete)
        with mock.patch.object(self.g.messagebox, "askyesno", return_value=True):
            self.app.deploy_major_masters()
        result = {"ready": ["Selected.esm"], "backed_up": [], "skipped_identical": [], "archives": [],
                  "conflicts": [], "linked":["Selected.esm", "Companion.esp", "Textures\\x.dds",
                                                         "Extras\\Optional.esp"]}
        with mock.patch.object(self.app, "save") as save, mock.patch.object(self.app, "refresh"), \
                mock.patch.object(self.g, "write_gui_state"), mock.patch("mwmanager_dialogs.showinfo"), \
                mock.patch.object(self.g.messagebox, "showinfo"):
            jobs[0](result, None)
        save.assert_called_once()
        self.assertIn("companion.esp", self.app.active_names)
        self.assertIn("Companion.esp", self.app.order)
        self.assertNotIn("optional.esp", self.app.active_names)       # only Data Files' root is deployed as a plugin

    def test_master_conflicts_ask_before_any_review_window(self):
        conflicts = [("CELL", "Vivec", [], []), ("NPC_", "fargoth", [], []), ("GMST", "x", [], [])]
        with mock.patch("mwmanager_dialogs.askyesno", return_value=False) as ask, \
                mock.patch.object(self.g.tk, "Toplevel") as window:
            self.app._resolve_major_master_conflicts(conflicts)
        text = ask.call_args.args[1]
        self.assertIn("3 conflicts", text)
        self.assertIn("Extreme: 1", text)
        window.assert_not_called()

    def test_status_window_lines_are_kept_in_a_file(self):
        from mwmanager_workflow import JobView
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "status.log"
            log.write_text("last session\n", encoding="utf-8")
            Path(tmp, "status.prev.log").write_text("older session\n", encoding="utf-8")
            view = JobView.__new__(JobView)
            view.status_log = log
            view._record("Staging master 1 of 2: A.esm")
            view._record("could not move x: [WinError 3]")
            self.assertEqual(log.read_text(encoding="utf-8").splitlines(),
                             ["Staging master 1 of 2: A.esm", "could not move x: [WinError 3]"])
            self.assertEqual(Path(tmp, "status.prev.log").read_text(encoding="utf-8"), "last session\n")

    def test_incomplete_stage_never_deploys(self):
        self.stage()
        self.app._set_master_stage_ready(["Selected.esm"], False)
        with mock.patch.object(self.g.messagebox, "showwarning") as warning, \
                mock.patch.object(self.app, "_run_progress_job") as job:
            self.app.deploy_major_masters()
        warning.assert_called_once()
        job.assert_not_called()

    def test_long_prompts_are_scrollable_and_buttons_reachable(self):
        import mwmanager_dialogs as prompts
        found = []

        def close():
            for window in self.app.winfo_children():
                if isinstance(window, smoke.tk.Toplevel):
                    def visit(parent):
                        for widget in parent.winfo_children():
                            found.append(widget)
                            visit(widget)
                    visit(window)
                    window.destroy()
        self.app.after(100, close)
        prompts.showwarning("Many blockers", "A detailed diagnostic.\n" * 100, parent=self.app)
        self.assertTrue(any(isinstance(w, smoke.tk.Text) for w in found))
