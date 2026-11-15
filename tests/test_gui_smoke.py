"""Smoke tests for the MWManager main window and its themed dialogs.

Every file the GUI reads or writes is redirected to a temporary folder with a fake game install, so
these tests never touch the real MWManager state, Data Files, Vortex or the game. They are skipped
when Tk cannot open a display.
"""
import ast
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import tkinter as tk
    _root = tk.Tk()
    _root.destroy()
    TK_OK = True
except Exception:          # no display / no Tk
    TK_OK = False


def _bound_in(node) -> set[str]:
    """Names bound directly in one scope (not inside nested functions/classes)."""
    names = set()
    stack = list(ast.iter_child_nodes(node))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        names |= {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        names |= {a.arg for a in (args.vararg, args.kwarg) if a}
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
            stack.extend(child.decorator_list)
            continue
        if isinstance(child, ast.Lambda):
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in child.names}
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            names |= set(child.names)
        elif isinstance(child, ast.MatchAs) and child.name:
            names.add(child.name)
        stack.extend(ast.iter_child_nodes(child))
    return names


def _undefined_names(path: Path) -> list[str]:
    """Scope-aware: a name loaded in a function must be bound in it, an enclosing function, the module or builtins."""
    import builtins
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing = set()

    def visit(node, visible):
        scope = visible | _bound_in(node)
        stack = list(ast.iter_child_nodes(node))
        while stack:
            child = stack.pop()
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if not isinstance(child, ast.Lambda):
                    stack.extend(child.decorator_list)
                stack.extend(d for d in child.args.defaults + child.args.kw_defaults if d)
                visit(child, scope)
                continue
            if isinstance(child, ast.ClassDef):
                stack.extend(child.bases + child.decorator_list)
                visit(child, scope)       # class bodies see the enclosing scope (close enough here)
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id not in scope:
                missing.add(f"{child.id}:{child.lineno}")
            stack.extend(ast.iter_child_nodes(child))

    visit(tree, set(dir(builtins)) | {"__file__", "__name__"})
    return sorted(missing)


class GuiSourceTests(unittest.TestCase):
    def test_gui_and_theme_have_no_unbound_names(self):
        # Catches names a function uses but never binds, like the footer_top / cancel_value regressions.
        for name in ("mwmanager_gui.py", "mwmanager_theme.py"):
            self.assertEqual(_undefined_names(HERE.parent / name), [], name)


try:
    import tkinter  # noqa: F401
    TK_IMPORT_OK = True
except Exception:
    TK_IMPORT_OK = False


@unittest.skipUnless(TK_IMPORT_OK, "tkinter not installed")
class SetupStateTests(unittest.TestCase):
    def test_steps_are_timestamped_and_a_different_game_starts_blank(self):
        import mwmanager_setup_state as state
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record, game, other = root / "setup.json", root / "game", root / "other"
            game.mkdir()
            other.mkdir()
            state.start(record, game)
            saved = state.complete(record, game, "paths_confirmed")
            self.assertTrue(saved["steps"]["paths_confirmed"]["completed_at"])
            self.assertTrue(state.done(record, game, "paths_confirmed"))
            self.assertFalse(state.done(record, game, "preflight_complete"))
            self.assertFalse(state.done(record, other, "paths_confirmed"))


class ArchiveSettingsTests(unittest.TestCase):
    """Archive Downloads settings: new key names, and older saved keys still read."""

    def setUp(self):
        import mwmanager_gui as g
        self.g = g
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.game = base / "game"
        (self.game / "Data Files").mkdir(parents=True)
        (self.game / "Morrowind.exe").write_bytes(b"MZ")
        self.archives = base / "archives"
        self.archives.mkdir()
        self._saved = {}
        for attr, value in {"SETTINGS_FILE": base / "settings.json", "MWMANAGER_INI": base / "mwmanager.ini",
                            "vortex_install_path": lambda: None, "vortex_registered": lambda: False,
                            "detect_vortex_downloads": lambda: None}.items():
            self._saved[attr] = getattr(g, attr)
            setattr(g, attr, value)

    def tearDown(self):
        for attr, value in self._saved.items():
            setattr(self.g, attr, value)
        self.tmp.cleanup()

    def test_old_keys_are_read_and_replaced_on_save(self):
        self.g.MWMANAGER_INI.write_text(f"[Paths]\ndownloads_dir = {self.archives}\ngame_dir = {self.game}\n")
        self.g.SETTINGS_FILE.write_text(json.dumps({"uses_vortex": False, "first_run": 0}))
        settings = self.g.read_settings()
        self.assertEqual(settings["archive_downloads_dir"], str(self.archives))
        self.assertEqual(settings["archive_source"], "unmanaged")
        self.assertNotIn("uses_vortex", settings)
        self.g.write_settings(settings)
        ini = self.g.MWMANAGER_INI.read_text()
        self.assertIn("archive_downloads_dir = " + str(self.archives), ini)
        self.assertNotIn("\ndownloads_dir", ini)
        saved = json.loads(self.g.SETTINGS_FILE.read_text())
        self.assertEqual(saved.get("archive_source"), "unmanaged")
        self.assertNotIn("uses_vortex", saved)

    def test_old_vortex_yes_becomes_vortex_source(self):
        self.g.SETTINGS_FILE.write_text(json.dumps({"uses_vortex": True}))
        self.assertEqual(self.g.read_settings()["archive_source"], "vortex")

    def test_new_key_wins_over_old(self):
        other = Path(self.tmp.name) / "old"
        self.g.MWMANAGER_INI.write_text(f"[Paths]\ndownloads_dir = {other}\narchive_downloads_dir = "
                                        f"{self.archives}\n")
        self.assertEqual(self.g.read_settings()["archive_downloads_dir"], str(self.archives))

    def test_saved_valid_game_marks_paths_confirmed_for_preflight_resume(self):
        self.g.MWMANAGER_INI.write_text(f"[Paths]\ngame_dir = {self.game}\n")
        self.g.SETTINGS_FILE.write_text(json.dumps({"first_run": 1, "init_complete": 0}))
        settings = self.g.read_settings()
        self.assertTrue(settings["paths_confirmed"])
        self.assertEqual(settings["game_dir"], str(self.game))

    def test_registry_default_does_not_count_as_path_confirmation(self):
        self.g.SETTINGS_FILE.write_text(json.dumps({"first_run": 1, "init_complete": 0}))
        self.assertFalse(self.g.read_settings()["paths_confirmed"])


@unittest.skipUnless(TK_OK, "Tk display unavailable")
class GuiSmokeTests(unittest.TestCase):
    def setUp(self):
        import mwmanager_gui as g
        self.g = g
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        game = base / "game"
        data = game / "Data Files"
        data.mkdir(parents=True)
        (game / "Morrowind.exe").write_bytes(b"MZ")
        for name in ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm", "Tamriel_Data.esm", "A Mod.esp",
                     "New Mod.esp", "mwmanager Multipatch.esp"):
            (data / name).write_bytes(b"TES3")
        (base / "mwmanager_core_addons.json").write_text(json.dumps({"tamriel_data.esm": ["tamriel data"]}))
        (base / "mwmanager_gui_state.json").write_text(json.dumps({
            "game_dir": str(game.resolve()),
            "known_plugins": ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm", "Tamriel_Data.esm", "A Mod.esp"]}))
        self._saved = {}
        for attr, value in {
            "APP_DIR": base, "SETTINGS_FILE": base / "mwmanager_gui_settings.json",
            "MWMANAGER_INI": base / "mwmanager.ini", "SORT_FILE": base / "mwmanager_sort_order.txt",
            "MERGE_RECORD": base / "mwmanager_merged.txt", "MAJOR_MASTER_CONFIG": base / "mwmanager_core_addons.json",
            "GUI_STATE_FILE": base / "mwmanager_gui_state.json",
            "DEPLOYMENT_JOURNAL": base / "major_master_deployments.json",
            "PLUGIN_STAGE_MANIFEST": base / "staging" / "plugins" / ".mwmanager-plugin-stages.json",
            "DISABLED_RECORD": base / "mwmanager_disabled.txt",
            "read_settings": lambda: {"archive_source": "unmanaged", "first_run": 0, "init_complete": 1,
                                      "game_dir": str(game), "archive_downloads_dir": str(base / "downloads"),
                                      "always_elevate_game_dir": "",
                                      "vortex_detected": False, "vortex_exe": ""},
        }.items():
            self._saved[attr] = getattr(g, attr)
            setattr(g, attr, value)
        self.errors = []
        self.app = g.ManagerGUI()
        self.app.report_callback_exception = lambda *exc: self.errors.append(exc)

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        for attr, value in self._saved.items():
            setattr(self.g, attr, value)
        self.tmp.cleanup()

    def pump(self, seconds=0.4):
        end = threading.Event()
        self.app.after(int(seconds * 1000), end.set)
        while not end.is_set():
            self.app.update()

    def test_main_window_lists_plugins_and_marks_new_ones(self):
        self.pump()
        rows = self.app.tree.get_children()
        self.assertEqual(rows[:3], ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"))
        self.assertIn("new_plugin", self.app.tree.item("New Mod.esp", "tags"))
        self.assertEqual(str(self.app.generate_button.cget("style")), "Primary.TButton")
        self.assertEqual(self.errors, [])

    def test_first_run_starts_full_cycle_before_major_masters(self):
        called = []
        offered = []
        self.app.settings["first_run"] = 1
        self.app._ensure_archive_downloads = lambda: True
        self.app._run_progress_job = lambda _title, _description, _worker, completed: completed(True, None)
        self.app._run_initial_tidy = lambda on_success: on_success()
        self.app.generate_patches = lambda **kwargs: called.append(kwargs)
        self.app._offer_major_master_processing = lambda: offered.append(True)
        self.app._begin_first_run()
        self.pump(0.3)
        self.assertEqual(called[0]["mode"], "full")
        self.assertTrue(called[0]["force"])
        self.assertTrue(called[0]["asset_repair"])
        self.assertEqual(self.app.settings["first_run"], 1)
        called[0]["on_success"]()
        self.assertEqual((self.app.settings["first_run"], offered), (0, [True]))

    def test_first_run_resumes_after_completed_preflight(self):
        self.app.settings["first_run"] = 1
        self.app._ensure_archive_downloads = lambda: True
        self.app._setup_complete("preflight_complete")
        resumed = []
        self.app._run_initial_tidy = lambda on_success: resumed.append(on_success)
        self.app._run_progress_job = lambda *_args: self.fail("completed preflight was run again")
        self.app._begin_first_run()
        self.pump(0.2)
        self.assertEqual(len(resumed), 1)

    def test_initial_tidy_enables_and_sorts_all_shipped_esps_before_full(self):
        data = Path(self.app.settings["game_dir"]) / "Data Files"
        for name in self.g.OFFICIAL_SHIPPED_ESPS:
            (data / name).write_bytes(b"TES3")
            self.app.active_names.discard(name.casefold())
        commands = []

        class Process:
            stdout = ()

            def wait(self):
                return 0

        self.app._run_progress_job = lambda _title, _description, worker, completed: completed(worker(lambda _line: None), None)
        with mock.patch.object(self.g.subprocess, "Popen",
                               side_effect=lambda command, **_kwargs: (commands.append(command), Process())[1]):
            self.app._run_initial_tidy(lambda: None)
            self.pump(0.2)
        self.assertTrue({name.casefold() for name in self.g.OFFICIAL_SHIPPED_ESPS} <= self.app.active_names)
        saved = self.g.SORT_FILE.read_text(encoding="utf-8")
        self.assertTrue(all(name.casefold() in saved.casefold() for name in self.g.OFFICIAL_SHIPPED_ESPS))
        self.assertEqual(commands[0][-3:], ["--sort", "--yes", "--no-follow-up"])
        self.assertIn("--init", commands[1])

    def test_post_full_major_master_handoff_is_a_stub(self):
        import mwmanager_dialogs as dialogs
        self.app.settings["init_complete"] = 0
        opened = []
        self.app.open_major_masters = lambda: opened.append(True)
        with mock.patch.object(dialogs, "showinfo") as shown:
            self.app._offer_major_master_processing()
        shown.assert_called_once()
        self.assertIn("would start now", shown.call_args.args[1])
        self.assertEqual(self.app.settings["init_complete"], 1)
        self.assertEqual(opened, [])

    def test_major_masters_dialog_builds_its_footer(self):
        self.app.open_major_masters()
        self.pump()
        for name in ("stage_master_button", "deploy_master_button", "remove_master_button",
                     "unstage_master_button"):
            self.assertTrue(self.getattr_ok(name), name)
        self.assertEqual(self.errors, [])

    def test_first_window_lists_detected_installs_and_hands_them_to_options(self):
        import mwmanager_detect
        game = Path(self.app.settings["game_dir"])
        other = Path(self.tmp.name) / "other"
        (other / "Data Files").mkdir(parents=True)
        (other / "Morrowind.exe").write_bytes(b"MZ")
        found = {"games": [{"store": "GOG", "path": str(game), "source": "test"},
                           {"store": "Steam", "path": str(other), "source": "test"}],
                 "managers": [{"name": "Vortex", "key": "vortex", "supported": True, "detected": True,
                               "exe": "", "downloads": "", "sources": [], "notes": []}]}
        opened = []
        original = mwmanager_detect.detect_all
        mwmanager_detect.detect_all = lambda reg=None: found
        self.app.open_options = lambda **kw: opened.append(kw)
        try:
            dialog = self.app._open_detected_paths(first_run=False)
            self.pump(0.2)
            radios, buttons = [], []

            def walk(widget):
                for child in widget.winfo_children():
                    (radios if child.winfo_class() == "TRadiobutton" else
                     buttons if child.winfo_class() == "TButton" else []).append(child)
                    walk(child)
            walk(dialog)
            self.assertEqual([r.cget("text") for r in radios],
                             [f"GOG: {game}", f"Steam: {other}", "Vortex: download folder not found",
                              "None / unmanaged: a folder you choose"])
            self.assertIn("disabled", radios[2].state())              # Vortex found, but no folder to use
            radios[1].invoke()
            next(b for b in buttons if b.cget("text") == "Approve").invoke()
            self.pump(0.2)
        finally:
            mwmanager_detect.detect_all = original
        self.assertEqual(opened, [{"first_run": False, "game": str(other), "downloads": "",
                                   "archive_source": "unmanaged"}])
        self.assertEqual(self.errors, [])

    def test_first_path_approval_skips_the_second_options_window(self):
        import mwmanager_detect
        game = Path(self.app.settings["game_dir"])
        archives = Path(self.app.settings["archive_downloads_dir"])
        found = {"games": [{"store": "GOG", "path": str(game), "source": "test"}],
                 "managers": [{"name": "Vortex", "key": "vortex", "supported": True, "detected": True,
                               "exe": "", "downloads": str(archives), "sources": [], "notes": []}]}
        opened, began = [], []
        with mock.patch.object(mwmanager_detect, "detect_all", return_value=found), \
                mock.patch.object(self.g, "offer_elevation"), \
                mock.patch.object(self.app, "open_options", side_effect=lambda **kwargs: opened.append(kwargs)), \
                mock.patch.object(self.app, "_begin_first_run", side_effect=lambda: began.append(True)):
            dialog = self.app._open_detected_paths(first_run=True)
            self.pump(0.2)
            buttons = []

            def walk(widget):
                for child in widget.winfo_children():
                    if child.winfo_class() == "TButton":
                        buttons.append(child)
                    walk(child)

            walk(dialog)
            next(button for button in buttons if button.cget("text") == "Approve").invoke()
            self.pump(0.3)
        self.assertEqual(opened, [])
        self.assertEqual(began, [True])
        self.assertEqual(self.app.settings["game_dir"], str(game))

    def test_first_window_offers_manager_folder_or_own_folder(self):
        import mwmanager_detect
        game = Path(self.app.settings["game_dir"])
        vortex_dl = Path(self.tmp.name) / "vortex_dl"
        own = Path(self.tmp.name) / "my archives"
        vortex_dl.mkdir()
        own.mkdir()
        self.app.settings["archive_downloads_dir"] = ""
        self.app.settings["archive_source"] = None
        found = {"games": [{"store": "GOG", "path": str(game), "source": "test"}],
                 "managers": [{"name": "Vortex", "key": "vortex", "supported": True, "detected": True,
                               "exe": "", "downloads": str(vortex_dl), "sources": [], "notes": []},
                              {"name": "Mod Organizer 2", "key": "mo2", "supported": False, "detected": True,
                               "exe": "", "downloads": "", "sources": [], "notes": []}]}
        opened = []
        saved = (mwmanager_detect.detect_all, self.g.filedialog.askdirectory)
        mwmanager_detect.detect_all = lambda reg=None: found
        self.app.open_options = lambda **kw: opened.append(kw)
        try:
            dialog = self.app._open_detected_paths(first_run=False)
            self.pump(0.2)
            widgets = []

            def walk(widget):
                for child in widget.winfo_children():
                    widgets.append(child)
                    walk(child)
            walk(dialog)
            radios = [w for w in widgets if w.winfo_class() == "TRadiobutton"]
            buttons = [w for w in widgets if w.winfo_class() == "TButton"]
            self.assertIn(f"Vortex: {vortex_dl}", [r.cget("text") for r in radios])
            self.assertIn("Mod Organizer 2: detected, not supported yet", [r.cget("text") for r in radios])
            approve = next(b for b in buttons if b.cget("text") == "Approve")
            approve.invoke()
            self.pump(0.1)
            self.assertEqual(opened[-1]["archive_source"], "vortex")
            self.assertEqual(opened[-1]["downloads"], str(vortex_dl))
            dialog = self.app._open_detected_paths(first_run=False)
            self.pump(0.1)
            widgets.clear()
            walk(dialog)
            self.g.filedialog.askdirectory = lambda **kw: str(own)
            browse = [w for w in widgets if w.winfo_class() == "TButton" and w.cget("text") == "Browse…"][-1]
            browse.invoke()
            next(w for w in widgets if w.winfo_class() == "TButton" and w.cget("text") == "Approve").invoke()
            self.pump(0.1)
        finally:
            mwmanager_detect.detect_all, self.g.filedialog.askdirectory = saved
        self.assertEqual(opened[-1]["archive_source"], "unmanaged")
        self.assertEqual(opened[-1]["downloads"], str(own))
        self.assertEqual(self.errors, [])

    def getattr_ok(self, name):
        widget = getattr(self.app, name, None)
        return widget is not None and widget.winfo_exists()

    def test_progress_dialog_keeps_indents_and_closing_a_choice_returns_cancel(self):
        answers = []

        def worker(report):
            report("MWMANAGER-STAGE: 1 of 2 — testing")
            report("  - an indented list item that the tools wrapped")
            answers.append(report.ask_choice("Pick", "Which one?", [("One", "1")], "cancel-value"))
            return True

        done = []

        attempts = []

        def close_choice_when_shown():
            # The progress poll opens the choice window and waits in it, so close it from a timer.
            attempts.append(1)
            for dialog in self.app.winfo_children():
                if isinstance(dialog, tk.Toplevel):
                    for child in dialog.winfo_children():
                        if isinstance(child, tk.Toplevel):
                            try:
                                self.app.tk.call(child.protocol("WM_DELETE_WINDOW"))   # what the window X runs
                            except tk.TclError as exc:
                                self.errors.append(exc)
                            if child.winfo_exists() and len(attempts) > 20:
                                child.destroy()       # handler broken: don't hang the test run
                            if not child.winfo_exists():
                                return
            self.app.after(100, close_choice_when_shown)

        self.app._run_progress_job("Generate Patches", "Starting…", worker, lambda r, e: done.append((r, e)))
        self.app.after(200, close_choice_when_shown)
        for _ in range(80):
            if done:
                break
            self.pump(0.1)
        self.assertEqual(answers, ["cancel-value"])
        self.assertEqual(done, [(True, None)])
        self.assertEqual(self.errors, [])

    def test_display_line_keeps_indent_and_caps_length(self):
        self.assertEqual(self.g._display_line("  - item\n"), "  - item")
        self.assertEqual(len(self.g._display_line("x" * 2000)), 600)


if __name__ == "__main__":
    unittest.main()
