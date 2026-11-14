"""Checks for the 2026-11-13 safe-fix pass: job lock, quiet child stdin, no fixed drive paths, and the
repaired helpers in the forked tools. Nothing here touches real MWManager state, Data Files or Vortex."""
import ast
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402

try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.destroy()
    TK_OK = True
except Exception:
    TK_OK = False

DRIVE = re.compile(r"""(?:r?["'])[A-Za-z]:\\""")


class NoFixedDrivePathTests(unittest.TestCase):
    def test_code_has_no_drive_letter_paths(self):
        found = []
        for path in list(ROOT.glob("*.py")) + list((ROOT / "database").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                    and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
                        and re.match(r"^[A-Za-z]:\\", node.value)):
                    found.append(f"{path.name}:{node.lineno}: {node.value}")
        self.assertEqual(found, [])


class HiddenRunsHaveNoConsoleInput(unittest.TestCase):
    def test_piped_tool_runs_get_devnull_stdin(self):
        tree = ast.parse((ROOT / "mwmanager_gui.py").read_text(encoding="utf-8"))
        missing = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("Popen", "run"):
                kw = {k.arg: k.value for k in node.keywords}
                piped = ("capture_output" in kw or ("stdout" in kw and getattr(kw["stdout"], "attr", "") == "PIPE"))
                if piped and getattr(kw.get("stdin"), "attr", "") != "DEVNULL":
                    missing.append(node.lineno)
        self.assertEqual(missing, [])


class ForkHelperTests(unittest.TestCase):
    def test_tidy_imports_shutil(self):
        tree = ast.parse((ROOT / "mwmanager_tidy_ext.py").read_text(encoding="utf-8"))
        names = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
        self.assertIn("shutil", names)

    def test_assets_defines_plugin_path_rename(self):
        tree = ast.parse((ROOT / "mwdialog_assets.py").read_text(encoding="utf-8"))
        funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertIn("plugin_path_rename", funcs)
        self.assertIn("rename_ascii", funcs)

    def test_backup_asset_original_keeps_first_copy(self):
        import mwmanager as m
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "Data Files"
            (data / "textures").mkdir(parents=True)
            asset = data / "textures" / "a.dds"
            asset.write_bytes(b"original")
            with mock.patch.object(m, "asset_owner_folder", return_value=(None, "Some.esp")), \
                    mwmanager_upstream.use_manager_dir(tmp):
                kept = m.backup_asset_original(str(data), os.path.join("textures", "a.dds"), feature="repair")
                self.assertTrue(os.path.isfile(kept))
                self.assertEqual(Path(kept).read_bytes(), b"original")
                again = m.backup_asset_original(str(data), os.path.join("textures", "a.dds"), feature="other")
                self.assertEqual(Path(again).read_bytes(), b"original")

    def test_bsapack_comes_from_tools_folder(self):
        import major_master_deploy as mmd
        tool = mmd.find_bsapack()
        bundled = ROOT / "tools" / "bsapack.dej.exe"
        if bundled.is_file():
            self.assertEqual(tool, bundled)
        else:
            self.assertTrue(tool is None or tool.name.lower().startswith("bsapack"))

    def test_steam_roots_read_libraryfolders(self):
        import plugin_install as pi
        with tempfile.TemporaryDirectory() as tmp:
            steam = Path(tmp) / "Steam"
            (steam / "steamapps").mkdir(parents=True)
            lib = Path(tmp) / "Lib2"
            (steam / "steamapps" / "libraryfolders.vdf").write_text(
                '"libraryfolders"\n{\n\t"1"\n\t{\n\t\t"path"\t\t"' + str(lib).replace("\\", "\\\\") + '"\n\t}\n}\n')
            # no registry entry (winreg import fails), only the ProgramFiles(x86)\Steam guess
            with mock.patch.dict(os.environ, {"ProgramFiles(x86)": tmp, "ProgramFiles": ""}, clear=False), \
                    mock.patch.dict(sys.modules, {"winreg": None}):
                roots = pi.steam_morrowind_roots()
        self.assertIn(steam / "steamapps" / "common" / "Morrowind", roots)
        self.assertIn(lib / "steamapps" / "common" / "Morrowind", roots)

    def test_catalog_needs_game_dir_and_keeps_db_beside_manager(self):
        sys.path.insert(0, str(ROOT / "database"))
        import catalog
        self.assertEqual(catalog.DEFAULT_DB, ROOT / "game_catalog.sqlite3")
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            catalog.main([])


@unittest.skipUnless(TK_OK, "Tk display unavailable")
class JobLockTests(unittest.TestCase):
    def setUp(self):
        import mwmanager_gui as g
        self.g = g
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        game = base / "game"
        (game / "Data Files").mkdir(parents=True)
        (game / "Morrowind.exe").write_bytes(b"MZ")
        for name in ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"):
            (game / "Data Files" / name).write_bytes(b"TES3")
        self._saved = {}
        for attr, value in {
            "APP_DIR": base, "SETTINGS_FILE": base / "s.json", "MWMANAGER_INI": base / "mwmanager.ini",
            "SORT_FILE": base / "sort.txt", "MERGE_RECORD": base / "merged.txt",
            "MAJOR_MASTER_CONFIG": base / "core.json", "GUI_STATE_FILE": base / "state.json",
            "DEPLOYMENT_JOURNAL": base / "deploy.json",
            "PLUGIN_STAGE_MANIFEST": base / "staging" / "plugins" / "m.json",
            "DISABLED_RECORD": base / "disabled.txt",
            "read_settings": lambda: {"archive_source": "unmanaged", "first_run": 0, "init_complete": 1,
                                      "game_dir": str(game), "archive_downloads_dir": str(base / "dl"),
                                      "always_elevate_game_dir": "", "vortex_detected": False, "vortex_exe": ""},
        }.items():
            self._saved[attr] = getattr(g, attr)
            setattr(g, attr, value)
        self.app = g.ManagerGUI()

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        for attr, value in self._saved.items():
            setattr(self.g, attr, value)
        self.tmp.cleanup()

    def pump(self, seconds):
        end = threading.Event()
        self.app.after(int(seconds * 1000), end.set)
        while not end.is_set():
            self.app.update()

    def test_second_job_is_refused_and_close_asks_while_running(self):
        release = threading.Event()
        done = []

        def slow(report):
            release.wait(5)
            return "first"

        self.app._run_progress_job("Generate Patches", "…", slow, lambda r, e: done.append(r))
        self.pump(0.3)
        with mock.patch.object(self.g.messagebox, "showinfo") as info:
            self.app._run_progress_job("Install", "…", lambda report: "second", lambda r, e: done.append(r))
            self.assertTrue(info.called)
        with mock.patch.object(self.g.messagebox, "askyesno", return_value=False) as ask:
            self.app._on_close()
            self.assertTrue(ask.called)
        self.assertTrue(self.app.winfo_exists())
        release.set()
        for _ in range(50):
            if done:
                break
            self.pump(0.1)
        self.assertEqual(done, ["first"])
        self.assertIsNone(self.app._active_job)
        # after it finishes, a new job may start again
        self.app._run_progress_job("Install", "…", lambda report: "second", lambda r, e: done.append(r))
        for _ in range(50):
            if len(done) > 1:
                break
            self.pump(0.1)
        self.assertEqual(done, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
