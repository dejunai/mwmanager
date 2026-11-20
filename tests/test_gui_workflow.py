"""Workflow tests use a temporary fake game, never the user's installation."""
import sys
import json
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
        self.assertEqual(progress_fraction("[3/12] reading Tribunal.esm"), (3, 12))
        self.assertEqual(progress_fraction("BSAPack: Processed 40 / 80 files"), (40, 80))

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

    def test_a_grass_plugin_deploys_to_data_files_grass_not_the_root(self):
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
            package = staging / "TOTSP"
            (package / "Grass").mkdir(parents=True)
            (package / "TOTSP.esm").write_bytes(struct.pack("<4sIII", b"TES3", 0, 0, 0))
            (package / "Grass" / "Rem-TOTSP.esp").write_bytes(struct.pack("<4sIII", b"TES3", 0, 0, 0))
            with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
                deploy(staging, data, manager, ["TOTSP.esm"], selected=["TOTSP.esm"])
            self.assertTrue((data / "Grass" / "Rem-TOTSP.esp").is_file())   # where MGE XE reads it
            self.assertFalse((data / "Rem-TOTSP.esp").exists())              # never loaded as a game plugin


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

    def test_action_line_follows_current_work(self):
        from mwmanager_workflow import JobView
        view = JobView(self.app)
        view.append("Staging master 3 of 10: OAAB_Data")
        view.append("Tidy OAAB_Data: Applying Tidy moves 2,000 of 6,578")
        self.assertIn("Staging master 3 of 10", view.stage.get())
        self.assertIn("Applying Tidy moves 2,000", view.activity.get())
        self.assertEqual((str(view.progress["mode"]), int(view.progress["value"])), ("determinate", 2000))
        for detail in ("  indented detail", "####################", "ASSET CHECK (each mod's own files)",
                       "G:\\projects\\MWManager\\outputs\\plan.txt"):
            view.append(detail)
        self.assertIn("Applying Tidy moves 2,000", view.activity.get())

    def test_tiny_counters_do_not_flip_the_bar_and_a_finished_counter_lets_it_move(self):
        from mwmanager_workflow import JobView
        view = JobView(self.app)
        view.begin("Deploy", "", None)
        for package in ("A", "B", "C"):                          # one 0-of-1 / 1-of-1 pair per package
            view.append(f"Tidy {package}: Applying Tidy moves 0 of 1")
            view.append(f"Tidy {package}: Applying Tidy moves 1 of 1")
        self.assertEqual(str(view.progress["mode"]), "indeterminate")
        view.append("Tidy D: Applying Tidy moves 6,578 of 6,578")
        self.assertEqual(str(view.progress["mode"]), "determinate")
        view.append("Hardlinking selected masters and their assets")
        self.assertEqual(str(view.progress["mode"]), "indeterminate")
        view.finish()

    def test_a_full_step_header_replaces_the_stale_title(self):
        from mwmanager_workflow import JobView
        view = JobView(self.app)
        view.append("MWMANAGER-STAGE: Sorting — indexing plugin 46/46: Wares_TR_npcs.esp")
        view.append("# --full step 6/11: clean and fix with the asset check (no Multipatch yet)")
        self.assertEqual(view.stage.get(), "--full step 6/11: clean and fix with the asset check (no Multipatch yet)")
        self.assertIn("--full step 6/11", view.log.get("1.0", "end"))

    def test_siege_set_aside_for_tr_names_tr_mainland_as_its_winner(self):
        import mwmanager_upstream
        scanner = mwmanager_upstream.compose()
        winner = scanner.WINNER_RE.search(self.g.FIREMOTH_DISABLED_REASON).group("w").strip()
        self.assertEqual(winner, "TR_Mainland.esm")      # a real plugin: Siege stays off while TR is active

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

    def test_master_suggestions_run_only_with_dev_options(self):
        downloads = Path(self.app.settings["game_dir"]).parent / "downloads"
        downloads.mkdir(exist_ok=True)
        self.app.settings["archive_downloads_dir"] = str(downloads)
        for dev, expected in ((False, 0), (True, 1)):
            self.app.settings["dev_options"] = dev
            with mock.patch.object(self.app, "_run_progress_job") as job,                     mock.patch("core_addons.scan_downloads", return_value={"archives": []}),                     mock.patch("core_addons.verify_nexus_versions", side_effect=lambda plan, **_: plan),                     mock.patch("core_addons.find_companions"),                     mock.patch("core_addons.prepare_core_master_review"),                     mock.patch("core_addons.suggest_masters", return_value=[]) as suggest:
                self.app.stage_major_masters(force_scan=True)
                job.call_args.args[2](mock.Mock())
            self.assertEqual(suggest.call_count, expected)

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

    def test_failed_save_puts_turned_off_plugins_back(self):
        data = Path(self.app.settings["game_dir"]) / "Data Files"
        (data / "Extra.esp").write_bytes(b"TES3")
        self.app.active_names.discard("extra.esp")
        before = self.g.read_disabled_plugins()
        with mock.patch.object(self.g, "write_order", side_effect=OSError("disk full")), \
                mock.patch.object(self.g.messagebox, "showerror") as error:
            self.app.save()
        error.assert_called_once()
        self.assertTrue((data / "Extra.esp").is_file())
        self.assertFalse((self.g.disabled_plugins_dir() / "Extra.esp").exists())
        self.assertEqual(self.g.read_disabled_plugins(), before)

    def test_turning_a_plugin_off_is_journaled(self):
        data = Path(self.app.settings["game_dir"]) / "Data Files"
        (data / "Extra.esp").write_bytes(b"TES3")
        self.app.active_names.discard("extra.esp")
        journal = mock.Mock()
        journal.has_baseline.return_value = True
        journal.start_run.return_value = {"run": "r1"}
        journal.move_original.side_effect = lambda game, src, dst, run, tag: os.replace(src, dst)
        with mock.patch("mwmanager_journal.Journal", return_value=journal):
            self.app.save()
        moved = [call.args[1].name for call in journal.move_original.call_args_list]
        self.assertIn("Extra.esp", moved)
        journal.finish_run.assert_called_once()
        self.assertTrue((self.g.disabled_plugins_dir() / "Extra.esp").is_file())

    def test_distant_land_notice_opens_mge_until_regeneration_is_confirmed(self):
        game = Path(self.app.settings["game_dir"])
        (game / "MGEXEgui.exe").write_bytes(b"MZ")
        status = self.g.APP_DIR / "outputs" / "distant_land_status.json"
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps({"regeneration_required": True, "game_dir": str(game.resolve())}), "utf-8")
        with mock.patch("mwmanager_dialogs.askyesno", side_effect=[False, True]), \
                mock.patch.object(self.g, "_ShellProgram") as opened:
            self.app._acknowledge_distant_land()                 # not finished yet: MGE XE opens
        opened.assert_called_once_with(game / "MGEXEgui.exe")
        self.assertTrue(json.loads(status.read_text("utf-8"))["regeneration_required"])
        with mock.patch("mwmanager_dialogs.askyesno", return_value=True):
            self.app._acknowledge_distant_land()                 # finished: the notice goes
        self.assertFalse(json.loads(status.read_text("utf-8"))["regeneration_required"])

    def test_generate_patches_says_how_it_finished(self):
        summary = ["--full summary", "=" * 70,
                   "turn back on what earlier runs turned off                      ok                    0s",
                   "clean and fix with the asset check (no Multipatch yet)         issues reported     235s",
                   "Active plugins now: 26", "--full finished with no errors."]
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.txt"
            report.write_text("report", encoding="utf-8")
            with mock.patch("mwmanager_dialogs.askyesno", return_value=True) as ask, \
                    mock.patch.object(self.app, "_show_report") as shown:
                self.app._report_generation({"summary": summary}, report)
            text = ask.call_args.args[1]
            self.assertIn("Finished with no errors. Active plugins now: 26.", text)
            self.assertIn("• clean and fix with the asset check (no Multipatch yet)", text)
            self.assertNotIn("turn back on", text)
            shown.assert_called_once_with("MWManager report", report)
            with mock.patch("mwmanager_dialogs.showinfo") as info:          # nothing to look at: just the result
                self.app._report_generation({"summary": ["Found 0 ERROR, 2 WARN, 0 INFO."]}, report)
            self.assertEqual(info.call_args.args[1], "Finished with no errors.")

    def test_edited_master_files_are_deleted_only_on_yes(self):
        jobs = []
        self.app._run_progress_job = lambda title, initial, worker, complete: jobs.append(complete)
        self.app.remove_master_button = mock.Mock()
        self.app._remove_masters_job(Path("staging"), Path("data"))
        result = {"masters": ["A.esm"], "removed": 5, "restored": 0, "registrations": 0,
                  "pending": 1, "edited": ["data/Textures/a.dds"]}
        with mock.patch("mwmanager_dialogs.askyesno", side_effect=[True, True]) as ask, \
                mock.patch("major_master_deploy.save_altered", return_value=Path("saved")) as save, \
                mock.patch.object(self.app, "refresh"), \
                mock.patch.object(self.app, "_remove_masters_job") as again:
            jobs[0](result, None)
        self.assertEqual(ask.call_args_list[0].args[0], "Save Altered Files?")
        self.assertIn("no longer match the originals", ask.call_args_list[0].args[1])
        save.assert_called_once()
        again.assert_called_once_with(Path("staging"), Path("data"), remove_edited=True)

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
                mock.patch.object(self.g.messagebox, "showinfo"), \
                mock.patch("mwmanager_dialogs.askyesno", return_value=True) as ask, \
                mock.patch.object(self.app, "generate_patches") as full:
            jobs[0](result, None)
        self.assertIn("--full", ask.call_args.args[1])
        full.assert_called_once_with(mode="full", force=True)
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
        self.assertEqual(ask.call_args.kwargs["default"], "no")     # accepting the load order is recommended
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
