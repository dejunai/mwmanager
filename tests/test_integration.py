"""MWManager runs the unchanged upstream Steam tools with its layers on top (mwmanager_upstream). Every path here
is a temporary folder: no real MWManager state, Data Files, Vortex or game folder is touched."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402


def mk(path, data=b"x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class Composition(unittest.TestCase):
    def test_import_names_are_the_composed_upstream_modules(self):
        import mwmanager
        import mwmanager_install
        self.assertIs(mwmanager, sys.modules["mwdialog_check"])
        self.assertIs(mwmanager_install, sys.modules["vortex_tidy"])
        self.assertTrue(mwmanager.MWMANAGER_COMPOSED)
        self.assertTrue(mwmanager_install.MWMANAGER_COMPOSED)
        for mod in (mwmanager, mwmanager_install, sys.modules["mwdialog_assets"]):
            self.assertEqual(Path(mod.__file__).resolve().parent, ROOT)

    def test_upstream_modules_call_mwmanagers_versions(self):
        import mwmanager as m
        import mwdialog_restore
        for name in ("backup_path", "off_dest", "read_load_order"):
            if name in vars(mwdialog_restore):
                self.assertIs(vars(mwdialog_restore)[name], getattr(m, name), name)
        self.assertTrue(issubclass(m.World, m.World.__mro__[1]))
        self.assertIn("_check_info", vars(m.World))

    def test_every_state_path_is_in_the_manager_folder(self):
        import mwmanager as m
        with tempfile.TemporaryDirectory() as tmp:
            own = os.path.normcase(os.path.abspath(tmp)) + os.sep
            names = dict(m.state_names(tmp))
            for per in m.module_state_names(tmp).values():
                names.update(per)
            names.update(m.tidy_state_names(tmp))
            for key, value in names.items():
                if isinstance(value, str) and os.path.isabs(value):
                    self.assertTrue(os.path.normcase(value + os.sep).startswith(own), (key, value))

    def test_use_manager_dir_moves_upstream_records_and_puts_them_back(self):
        import mwmanager_install as t
        import mwdialog_mastercheck as mc
        before = (mc.CACHE_FILE, t.PLAN)
        with tempfile.TemporaryDirectory() as tmp:
            with mwmanager_upstream.use_manager_dir(tmp):
                self.assertTrue(mc.CACHE_FILE.startswith(tmp))
                self.assertTrue(t.PLAN.startswith(tmp))
        self.assertEqual((mc.CACHE_FILE, t.PLAN), before)


class AssetRepairSwitch(unittest.TestCase):
    def test_off_unless_asked(self):
        import mwmanager as m
        with tempfile.TemporaryDirectory() as tmp, mwmanager_upstream.use_manager_dir(tmp):
            self.assertFalse(m.asset_repair_enabled(None))
            self.assertFalse(m.asset_repair_enabled(mock.Mock(asset_repair=None)))
            self.assertTrue(m.asset_repair_enabled(mock.Mock(asset_repair=True)))
            Path(tmp, "mwmanager.ini").write_text("[Repair]\nasset_repair = true\n", encoding="utf-8")
            self.assertTrue(m.asset_repair_enabled(mock.Mock(asset_repair=None)))

    def test_fix_leaves_asset_files_out_when_off(self):
        src = (ROOT / "mwmanager_overrides.py").read_text(encoding="utf-8")
        self.assertIn("if not asset_repair_enabled(args):", src)
        self.assertIn('"--asset-repair"', src)

    def test_full_pass_propagates_requested_asset_repairs(self):
        src = (ROOT / "mwmanager_overrides.py").read_text(encoding="utf-8")
        self.assertIn('if asset_repair_enabled(args):\n        common.append("--asset-repair")', src)


class TidySteps(unittest.TestCase):
    """options -> plan -> --apply, the refusal (exit 2) and --undo, in a temporary staging folder."""

    def setUp(self):
        import mwmanager_install as t
        self.t = t
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.manager = base / "MWManager"
        self.manager.mkdir()
        self.staging = base / "staging"
        self.data = base / "Game" / "Data Files"
        self.data.mkdir(parents=True)
        mod = self.staging / "ModA-1-1-0"
        mk(mod / "ModA.esp", b"TES3" + b"\0" * 12)
        mk(mod / "ModA Readme.txt", b"readme")
        mk(mod / "textures" / "rock.dds", b"DDS ")
        self.ctx = contextlib.ExitStack()
        self.ctx.enter_context(mwmanager_upstream.use_manager_dir(self.manager))
        self.ctx.enter_context(mock.patch.object(t.MT, "install", lambda *a, **k: None))
        self.ctx.enter_context(mock.patch.dict(os.environ, {"MWMANAGER_DATA_FILES": str(self.data)}))

    def tearDown(self):
        self.ctx.close()
        self.tmp.cleanup()

    def run_tidy(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = self.t.mwmanager_main(["--staging", str(self.staging), "--no-lua", "--no-assets", *extra])
        return rc, out.getvalue()

    def files(self):
        return sorted(p.relative_to(self.staging).as_posix() for p in self.staging.rglob("*") if p.is_file())

    def test_init_targets_live_data_files_and_auto_applies(self):
        with mock.patch.object(self.t, "run_initial_tidy", return_value=0) as run:
            rc = self.t.mwmanager_main(["--init", "--game-data-files", str(self.data), "--apply", "--yes"])
        self.assertEqual(rc, 0)
        run.assert_called_once_with(str(self.data), apply_changes=True)

    def test_initial_tidy_commits_asset_cache_before_full(self):
        assets = mock.Mock()
        with mock.patch.object(self.t, "run_data_files_tidy", return_value=0), \
                mock.patch.object(self.t._scanner, "main", return_value=0) as scan, \
                mock.patch.object(self.t._scanner, "asset_health_module", return_value=assets):
            rc = self.t.run_initial_tidy(str(self.data), apply_changes=True)
        self.assertEqual(rc, 0)
        self.assertIn("--asset-check", scan.call_args.args[0])
        self.assertIn("--asset-repair", scan.call_args.args[0])
        assets.close_caches.assert_called_once_with()

    def test_initial_tidy_continues_after_repairs(self):
        assets = mock.Mock()
        with mock.patch.object(self.t, "run_data_files_tidy", return_value=0), \
                mock.patch.object(self.t._scanner, "main", return_value=1), \
                mock.patch.object(self.t._scanner, "asset_health_module", return_value=assets):
            rc = self.t.run_initial_tidy(str(self.data), apply_changes=True)
        self.assertEqual(rc, 0)
        assets.close_caches.assert_called_once_with()

    def test_plan_then_apply_then_undo(self):
        start = self.files()
        rc, out = self.run_tidy()
        self.assertEqual(rc, 0, out)
        self.assertIn("MWMANAGER-PLAN: pending", out)
        self.assertEqual(self.files(), start)                    # plan mode changes nothing
        self.assertTrue(Path(self.t.PLAN).is_file())
        self.assertTrue(str(self.t.PLAN).startswith(str(self.manager)))
        rc, out = self.run_tidy("--apply", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(self.files(), start)
        self.assertTrue(any(f.startswith("ModA-1-1-0/Docs/") and f.endswith("/ModA Readme.txt") for f in self.files()))
        rc, out = self.run_tidy("--undo")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.files(), start)
        self.assertEqual(sorted(os.listdir(self.data)), [])       # nothing written into the game folder

    def test_apply_refuses_a_changed_or_missing_plan(self):
        rc, out = self.run_tidy("--apply", "--yes")
        self.assertEqual(rc, 2, out)
        rc, _ = self.run_tidy()
        self.assertEqual(rc, 0)
        mk(self.staging / "ModA-1-1-0" / "Changelog.txt", b"new")
        start = self.files()
        rc, out = self.run_tidy("--apply", "--yes")
        self.assertEqual(rc, 2, out)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.files(), start)

    def test_options_cannot_skip_the_review(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.t.mwmanager_main(["--staging", str(self.staging), "--options", "--apply"])

    def test_a_file_inside_a_moved_folder_is_not_moved_twice(self):
        root = self.staging / "ModA-1-1-0"
        res = [(str(root / "scripts" / "a.lua"), str(root / "Off" / "scripts" / "a.lua"), "file"),
               (str(root / "scripts"), str(root / "Off" / "scripts"), "folder")]
        mk(root / "scripts" / "a.lua")
        self.assertEqual([r[2] for r in self.t.drop_covered_moves(res)], ["folder"])


class GuiTidySteps(unittest.TestCase):
    def setUp(self):
        try:
            import mwmanager_gui
        except ImportError as e:                      # no tkinter on this Python
            self.skipTest(f"GUI not importable: {e}")
        self.gui = mwmanager_gui

    def fake(self, outputs):
        calls = []

        class Proc:
            def __init__(self, command, **kw):
                calls.append(command)
                code, lines = outputs[len(calls) - 1]
                self.stdout = iter(line + "\n" for line in lines)
                self._code = code

            def wait(self):
                return self._code
        return calls, mock.patch.object(self.gui.subprocess, "Popen", Proc)

    def progress(self, answer):
        messages = []

        def progress(message):
            messages.append(message)
        progress.ask_choice = lambda *a, **k: answer
        return progress, messages

    def test_options_plan_apply_and_refusal(self):
        pending = ["vortex_tidy plan, 2026-01-01 00:00: 1 mod(s), 1 to change, 0 left alone",
                   "MWMANAGER-PLAN: pending plan.txt"]
        calls, patch = self.fake([(0, pending), (0, ["done"])])
        progress, _ = self.progress("apply")
        with patch:
            n = self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(n, 1)
        self.assertIn("--options", calls[0])
        self.assertNotIn("--apply", calls[0])
        self.assertIn("--apply", calls[1])
        calls, patch = self.fake([(0, pending), (2, ["REFUSED: the plan ..."])])
        with patch, self.assertRaises(RuntimeError):
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")

    def test_cancel_applies_nothing(self):
        pending = ["vortex_tidy plan, x: 1 mod(s)", "MWMANAGER-PLAN: pending plan.txt"]
        calls, patch = self.fake([(0, pending)])
        progress, _ = self.progress("cancel")
        with patch, self.assertRaises(self.gui.TidyCancelled):
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(len(calls), 1)

    def test_partial_apply_halts_deployment(self):
        pending = ["vortex_tidy plan, x: 1 mod(s)", "MWMANAGER-PLAN: pending plan.txt"]
        calls, patch = self.fake([(0, pending), (1, ["one action failed"])])
        progress, _ = self.progress("apply")
        with patch, self.assertRaisesRegex(RuntimeError, "deployment is halted"):
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(len(calls), 2)

    def test_load_order_write_failure_restores_moved_plugins_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "game" / "Data Files"
            data.mkdir(parents=True)
            for name in (*self.gui.CORE, "Extra.esp"):
                (data / name).write_bytes(b"TES3")
            disabled = base / "mwmanager_disabled.txt"
            order = base / "mwmanager_sort_order.txt"
            rules = base / "mwmanager_rules.txt"
            disabled.write_bytes(b"old disabled\n")
            order.write_bytes(b"old order\n")
            rules.write_bytes(b"old rules\n")
            owner = mock.Mock()
            owner.settings = {"game_dir": str(data.parent)}
            owner.order = list(self.gui.CORE)
            owner.active_names = {name.casefold() for name in self.gui.CORE}
            owner.merge_inputs = []
            owner.pending_order_rules = []
            with (mock.patch.object(self.gui, "APP_DIR", base),
                  mock.patch.object(self.gui, "DISABLED_RECORD", disabled),
                  mock.patch.object(self.gui, "SORT_FILE", order),
                  mock.patch.object(self.gui, "write_order", side_effect=OSError("disk full")),
                  mock.patch.object(self.gui.messagebox, "showerror")):
                self.gui.ManagerGUI.save(owner)
            self.assertTrue((data / "Extra.esp").is_file())
            self.assertFalse((base / "disabled_plugins" / "Extra.esp").exists())
            self.assertEqual(disabled.read_bytes(), b"old disabled\n")
            self.assertEqual(order.read_bytes(), b"old order\n")
            self.assertEqual(rules.read_bytes(), b"old rules\n")


if __name__ == "__main__":
    unittest.main()
