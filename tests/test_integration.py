"""MWManager runs the unchanged upstream Steam tools with its layers on top (mwmanager_upstream). Every path here
is a temporary folder: no real MWManager state, Data Files, Vortex or game folder is touched."""
import contextlib
import io
import json
import errno
import os
import sys
import shutil
import tempfile
import types
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
            self.assertIn("DOOR_OWNERS_LIST", names)     # upstream writes it next to the game otherwise
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


class IncompatibleWorkflow(unittest.TestCase):
    def setUp(self):
        from test_core_master_order import plugin
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.manager = root / "manager"
        (self.manager / "outputs").mkdir(parents=True)
        self.data = root / "game" / "Data Files"
        self.data.mkdir(parents=True)
        self.ini = self.data.parent / "Morrowind.ini"
        self.ini.write_text("[Game Files]\n", encoding="cp1252")
        self.scope = mwmanager_upstream.use_manager_dir(str(self.manager))
        self.scanner = self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.plugin = plugin

    def family(self):
        m = self.scanner
        names = ["A.esm", "B.esp", "A addon.esp", m.CELL_WINNER_PATCH]
        for name, masters in (("A.esm", []), ("B.esp", []),
                              ("A addon.esp", ["A.esm"]), (m.CELL_WINNER_PATCH, [])):
            self.plugin(self.data / name, masters)
        m.write_load_order(str(self.data.parent), names, "test")
        args = types.SimpleNamespace(data_dir=str(self.data), ini=str(self.ini), dry_run=False,
                                     use_answers=False, only_new=False, no_interactive=False,
                                     defer_follow_up=True)
        paths = [(name, str(self.data / name)) for name in names]
        masters = {name.lower(): [] for name in names}
        masters["a addon.esp"] = ["a.esm"]
        conflict = {"place": [], "refs": [], "settings": [], "doubled": False, "dups": 0,
                    "incompatible": {"cells": {(1, 2)}, "evidence": {"terrain", "pathgrid"}}}
        return args, paths, masters, conflict

    @contextlib.contextmanager
    def different_volumes(self):
        data, disabled = self.data, self.manager / "disabled_plugins"
        replace, rename = os.replace, os.rename

        def cross_volume(source, target):
            parents = {Path(source).parent, Path(target).parent}
            return parents == {data, disabled}

        def checked(operation):
            def move(source, target):
                if cross_volume(source, target):
                    raise OSError(errno.EXDEV, "different volumes")
                return operation(source, target)
            return move

        with mock.patch.object(os, "replace", side_effect=checked(replace)), \
                mock.patch.object(os, "rename", side_effect=checked(rename)):
            yield

    def test_gui_choice_disables_the_family_and_reenables_only_with_its_master(self):
        m = self.scanner
        args, plugins, masters, conflict = self.family()
        names = {name.lower(): name for name, _path in plugins}
        with mock.patch.dict(os.environ, {"MWMANAGER_CONFLICT_GUI": "1"}), \
                mock.patch("mwmanager_workflow.conflict_answer", return_value="3") as picker, \
                mock.patch.object(m, "IN_FULL", False):
            self.assertEqual(m.conflict_session(
                args, [(("a.esm", "b.esp"), conflict)], names, masters, 1, plugins), 0)
        details, choices = picker.call_args.args[1:3]
        self.assertIn("terrain", details)
        self.assertIn("A addon.esp", details)
        self.assertEqual({key for _label, key in choices}, {"3", "4", "s", "q"})
        self.assertEqual(m.read_load_order(args), ["B.esp"])
        self.assertTrue((self.manager / "disabled_plugins" / "A.esm").is_file())
        self.assertTrue((self.manager / "disabled_plugins" / "A addon.esp").is_file())
        self.assertTrue((self.data / m.CELL_WINNER_PATCH).is_file())
        self.assertNotIn(m.CELL_WINNER_PATCH, m.read_disabled(str(self.data.parent)))

        args.reenable = ["A addon.esp"]
        self.assertEqual(m.run_reenable(args), 1)
        self.assertTrue((self.manager / "disabled_plugins" / "A addon.esp").is_file())
        args.reenable = ["A.esm", "A addon.esp"]
        self.assertEqual(m.run_reenable(args), 0)
        order = m.read_load_order(args)
        self.assertLess(order.index("A.esm"), order.index("A addon.esp"))
        self.assertTrue((self.data / "A.esm").is_file())
        self.assertTrue((self.data / "A addon.esp").is_file())

    def test_gui_shows_why_a_merge_blocks_both_choices(self):
        m = self.scanner
        args, plugins, masters, conflict = self.family()
        self.plugin(self.data / m.AUTO_MERGE_NAME, [])
        plugins.append((m.AUTO_MERGE_NAME, str(self.data / m.AUTO_MERGE_NAME)))
        masters[m.AUTO_MERGE_NAME.lower()] = []
        names = {name.lower(): name for name, _path in plugins}
        with mock.patch.dict(os.environ, {"MWMANAGER_CONFLICT_GUI": "1"}), \
                mock.patch("mwmanager_workflow.conflict_answer", return_value="s") as picker:
            self.assertEqual(m.conflict_session(
                args, [(("a.esm", "b.esp"), conflict)], names, masters, 1, plugins), 2)
        self.assertIn("unmerge", picker.call_args.args[1].lower())
        self.assertEqual({key for _label, key in picker.call_args.args[2]}, {"s", "q"})
        self.assertTrue((self.data / "A.esm").is_file())
        self.assertFalse((self.manager / "disabled_plugins").exists())

    def test_conflict_family_moves_across_volumes_and_can_be_reenabled(self):
        m = self.scanner
        args, plugins, masters, conflict = self.family()
        names = {name.lower(): name for name, _path in plugins}
        with self.different_volumes(), mock.patch.dict(os.environ, {"MWMANAGER_CONFLICT_GUI": "1"}), \
                mock.patch("mwmanager_workflow.conflict_answer", return_value="3"), \
                mock.patch.object(m, "IN_FULL", False):
            self.assertEqual(m.conflict_session(
                args, [(("a.esm", "b.esp"), conflict)], names, masters, 1, plugins), 0)
            args.reenable = ["A.esm", "A addon.esp"]
            self.assertEqual(m.run_reenable(args), 0)
        self.assertTrue((self.data / "A.esm").is_file())
        self.assertTrue((self.data / "A addon.esp").is_file())

    def test_failed_conflict_choice_rolls_back_cross_volume_moves(self):
        m = self.scanner
        args, plugins, masters, conflict = self.family()
        names = {name.lower(): name for name, _path in plugins}
        with self.different_volumes(), mock.patch.dict(os.environ, {"MWMANAGER_CONFLICT_GUI": "1"}), \
                mock.patch("mwmanager_workflow.conflict_answer", return_value="3"), \
                mock.patch.object(m, "set_game_files", side_effect=OSError("cannot write load order")):
            with self.assertRaisesRegex(OSError, "cannot write load order"):
                m.conflict_session(args, [(("a.esm", "b.esp"), conflict)], names, masters, 1, plugins)
        self.assertEqual(m.read_load_order(args), [name for name, _path in plugins])
        self.assertTrue((self.data / "A.esm").is_file())
        self.assertTrue((self.data / "A addon.esp").is_file())

    def test_cross_volume_copy_failure_preserves_the_original(self):
        m = self.scanner
        self.family()
        source = self.data / "A.esm"
        original = source.read_bytes()

        def fail_copy(_source, target):
            target.write(b"partial")
            raise OSError("copy failed")

        with self.different_volumes(), mock.patch.object(shutil, "copyfileobj", side_effect=fail_copy):
            with self.assertRaisesRegex(OSError, "copy failed"):
                m.move_plugins_to_disabled(str(self.data), ["A.esm"])
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((self.manager / "disabled_plugins" / "A.esm").exists())

    def test_cross_volume_source_removal_failure_preserves_the_original(self):
        m = self.scanner
        self.family()
        source = self.data / "A.esm"
        original = source.read_bytes()
        unlink = os.unlink

        def fail_source(path, *args, **kwargs):
            if Path(path) == source:
                raise PermissionError("source is locked")
            return unlink(path, *args, **kwargs)

        with self.different_volumes(), mock.patch.object(os, "unlink", side_effect=fail_source):
            with self.assertRaisesRegex(PermissionError, "source is locked"):
                m.move_plugins_to_disabled(str(self.data), ["A.esm"])
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((self.manager / "disabled_plugins" / "A.esm").exists())

    def test_direct_builds_refuse_unresolved_incompatibilities(self):
        m = self.scanner
        args, plugins, _masters, _conflict = self.family()
        for in_full in (False, True):
            for action in (["--fix"], ["--multipatch"], ["--auto-merge"], ["--merge", "Merged.esp", "B.esp"]):
                with self.subTest(action=action, in_full=in_full), \
                        mock.patch.object(m, "IN_FULL", in_full), \
                        mock.patch.object(m, "run_cell_conflicts", return_value=2) as guard, \
                        mock.patch.object(m, "run_auto_merge") as merge:
                    rc = m.main([str(self.data), *action, "--no-vortex", "--no-interactive", "--yes"])
                    self.assertEqual(rc, 2)
                    self.assertTrue(guard.call_args.kwargs["incompatible_only"])
                    merge.assert_not_called()
        self.assertEqual(m.read_load_order(args), [name for name, _path in plugins])

    def test_gui_build_guard_opens_only_mandatory_choices(self):
        m = self.scanner
        args, plugins, _masters, _conflict = self.family()
        with mock.patch.dict(os.environ, {"MWMANAGER_CONFLICT_GUI": "1"}), \
                mock.patch.object(m, "run_cell_conflicts", return_value=0) as scan:
            self.assertEqual(m.build_compatibility_guard(args, plugins), 0)
        checked = scan.call_args.args[0]
        self.assertTrue(checked.only_incompatible)
        self.assertTrue(checked.defer_follow_up)
        self.assertTrue(scan.call_args.kwargs["incompatible_only"])

    def test_bug_reports_exclude_interactions_and_keep_own_repairs_in_manager_outputs(self):
        m = self.scanner
        args, plugins, masters, _conflict = self.family()
        world = m.World(m.Report())
        world.masters = masters
        world.r.add("ERROR", "A addon.esp", "", "", "B.esp deleted the last placed copy",
                    fix=(0, ("drop_ref", 1), "dropped the reference after the deletion"))
        world.r.add("ERROR", "A addon.esp", "", "", "requires master 'A.esm', which is not loaded before it")
        with mock.patch.object(m, "OWN_FIXES", {"A addon.esp": [(True, "own misplaced CELL reference repaired")]}):
            m.write_bug_reports(args, plugins, world.r, world)
        bugreports = list((self.manager / "outputs" / "bugreports").glob("*.bugreport.txt"))
        self.assertEqual(len(bugreports), 1)
        text = bugreports[0].read_text(encoding="utf-8")
        self.assertIn("======== mwmanager ========", text)
        self.assertIn("own misplaced CELL reference repaired", text)
        self.assertIn("requires master 'A.esm'", text)
        self.assertNotIn("B.esp", text)
        self.assertNotIn("dropped the reference", text)
        self.assertFalse((self.data.parent / "bugreports").exists())

    def test_full_build_guard_can_ask_new_gui_choices_after_sorting(self):
        m = self.scanner
        args, plugins, _masters, _conflict = self.family()
        args.no_interactive = True
        top_args = types.SimpleNamespace(dry_run=False, no_interactive=False)
        with mock.patch.dict(os.environ, {"MWMANAGER_CONFLICT_GUI": "1"}), \
                mock.patch.object(m, "IN_FULL", True), \
                mock.patch.object(m, "TOP_ARGS", top_args), \
                mock.patch.object(m, "run_cell_conflicts", return_value=0) as scan:
            self.assertEqual(m.build_compatibility_guard(args, plugins), 0)
        checked = scan.call_args.args[0]
        self.assertFalse(checked.no_interactive)
        self.assertTrue(checked.only_incompatible)

    def test_sort_rechecks_before_a_combined_build(self):
        m = self.scanner
        self.family()
        for extra in ([], ["--no-follow-up"]):
            with self.subTest(extra=extra), contextlib.ExitStack() as stack:
                guard = stack.enter_context(mock.patch.object(m, "build_compatibility_guard", side_effect=[0, 2]))
                stack.enter_context(mock.patch.object(m, "run_sort",
                    side_effect=lambda args, _plugins: setattr(args, "sort_changed", True)))
                repairs = []
                for name in ("fix_ref_groups", "fix_cell_layouts", "fix_cell_winners", "retire_performers_patch"):
                    repairs.append(stack.enter_context(mock.patch.object(m, name)))
                for name in ("fix_omwaddon_masters", "fix_long_names"):
                    stack.enter_context(mock.patch.object(m, name, return_value=False))
                stack.enter_context(mock.patch.object(m, "scan", side_effect=AssertionError("built after sorting")))
                self.assertEqual(m.main([str(self.data), "--sort", "--all", "--no-vortex", "--no-sounds",
                                         "--no-doubled", "--no-interactive", "--yes", *extra]), 2)
                self.assertEqual(guard.call_count, 2)
                self.assertTrue(all(not repair.called for repair in repairs))

    def test_sort_revalidates_after_repairs_change_plugin_state(self):
        m = self.scanner
        self.family()
        events = []

        def guard(_args, _plugins):
            events.append("guard")
            return 2 if events.count("guard") == 3 else 0

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(m, "build_compatibility_guard", side_effect=guard))
            stack.enter_context(mock.patch.object(m, "run_sort",
                side_effect=lambda args, _plugins: (events.append("sort"), setattr(args, "sort_changed", True))))
            for name in ("fix_ref_groups", "fix_cell_layouts", "retire_performers_patch"):
                stack.enter_context(mock.patch.object(m, name, side_effect=lambda *args, _name=name: events.append(_name)))
            for name in ("fix_omwaddon_masters", "fix_long_names"):
                stack.enter_context(mock.patch.object(m, name, return_value=False))
            stack.enter_context(mock.patch.object(m, "fix_cell_winners",
                                                   side_effect=AssertionError("patch built after post-repair conflict")))
            stack.enter_context(mock.patch.object(m, "scan",
                                                   side_effect=AssertionError("report built after post-repair conflict")))
            rc = m.main([str(self.data), "--sort", "--fix", "--no-vortex", "--no-sounds", "--no-doubled",
                         "--no-follow-up", "--no-interactive", "--yes"])
        self.assertEqual(rc, 2)
        self.assertEqual(events, ["guard", "sort", "guard", "fix_ref_groups", "fix_cell_layouts",
                                  "retire_performers_patch", "guard"])

    def test_master_repair_sort_rechecks_before_fixing(self):
        m = self.scanner
        self.family()
        with mock.patch.object(m, "build_compatibility_guard", side_effect=[0, 2]) as guard, \
                mock.patch.object(m, "fix_omwaddon_masters", return_value=True), \
                mock.patch.object(m, "run_sort",
                                  side_effect=lambda args, _plugins: setattr(args, "sort_changed", True)), \
                mock.patch.object(m, "fix_ref_groups") as fix:
            self.assertEqual(m.main([str(self.data), "--fix", "--no-vortex", "--no-sounds",
                                     "--no-interactive", "--yes"]), 2)
            self.assertEqual(guard.call_count, 2)
        fix.assert_not_called()

    def test_play_is_not_launched_after_an_incompatible_build_is_refused(self):
        m = self.scanner
        with mock.patch.object(m, "main", return_value=2), \
                mock.patch.object(m, "PLAY_REQUEST", [object()]), \
                mock.patch.object(m, "launch_game") as launch:
            self.assertEqual(mwmanager_upstream.run_cli([]), 2)
        launch.assert_not_called()

    def test_failed_reenable_restores_files_to_the_disabled_folder(self):
        m = self.scanner
        args, plugins, masters, conflict = self.family()
        names = {name.lower(): name for name, _path in plugins}
        with mock.patch.dict(os.environ, {"MWMANAGER_CONFLICT_GUI": "1"}), \
                mock.patch("mwmanager_workflow.conflict_answer", return_value="3"), \
                mock.patch.object(m, "IN_FULL", False):
            self.assertEqual(m.conflict_session(
                args, [(("a.esm", "b.esp"), conflict)], names, masters, 1, plugins), 0)
        args.reenable = ["A.esm", "A addon.esp"]
        with self.different_volumes(), \
                mock.patch.object(m, "set_game_files", side_effect=OSError("cannot write load order")):
            with self.assertRaisesRegex(OSError, "cannot write load order"):
                m.run_reenable(args)
        self.assertEqual(m.read_load_order(args), ["B.esp"])
        self.assertEqual(set(m.read_disabled(str(self.data.parent))), {"A.esm", "A addon.esp"})
        for name in args.reenable:
            self.assertTrue((self.manager / "disabled_plugins" / name).is_file())
            self.assertFalse((self.data / name).exists())

    def test_fix_scan_includes_cell_fixes_for_script_placeholders(self):
        m = self.scanner
        _args, _plugins, _masters, _conflict = self.family()
        scanned = []

        class ScanReached(Exception):
            pass

        def capture(_args, plugins, _bsas, _dirs, _report):
            scanned.extend(name for name, _path in plugins)
            raise ScanReached

        with contextlib.ExitStack() as stack:
            for name in ("fix_ref_groups", "fix_cell_layouts", "fix_cell_winners", "retire_performers_patch"):
                stack.enter_context(mock.patch.object(m, name))
            for name in ("fix_omwaddon_masters", "fix_long_names"):
                stack.enter_context(mock.patch.object(m, name, return_value=False))
            stack.enter_context(mock.patch.object(m, "build_compatibility_guard", return_value=0))
            stack.enter_context(mock.patch.object(m, "scan", side_effect=capture))
            with self.assertRaises(ScanReached):
                m.main([str(self.data), "--fix", "--no-doubled", "--no-vortex",
                        "--no-sounds", "--no-interactive", "--yes"])
        self.assertIn(m.CELL_WINNER_PATCH, scanned)

    def test_full_unmerges_before_reset_and_stops_before_building(self):
        m = self.scanner
        args = types.SimpleNamespace(data_dir=str(self.data), init=True, dry_run=False, yes=True,
                                     fix_log=str(self.manager / "fix.log"), encoding="cp1252",
                                     vortex_dir=None, add_all_esp=False, no_redundant=True,
                                     fresh_answers=True, use_answers=False, no_interactive=True,
                                     ini=str(self.ini), collide_radius=m.COLLIDE_ANY_RADIUS, play=False)
        phases = []

        def fake_main(argv):
            phases.append(argv[1])
            return 2 if argv[1] == "--cell-conflicts" else 0

        with mock.patch.object(m, "main", side_effect=fake_main), \
                mock.patch.object(m, "full_missing_masters", return_value=[]), \
                mock.patch.object(m, "read_merge_record", return_value={"Merged.esp": ["A.esp"]}), \
                mock.patch.object(m, "read_load_order", return_value=[]), \
                mock.patch.object(m, "full_reset", side_effect=lambda *_a: phases.append("--reset") or 0):
            self.assertEqual(m.run_full(args), 2)
        self.assertEqual(phases, ["--unmerge", "--reset", "--cell-conflicts"])

    def test_unmerge_dry_run_does_not_change_files_or_start_a_follow_up(self):
        m = self.scanner
        names = ["Morrowind.esm", "Small.esp", "Other.esp", m.AUTO_MERGE_NAME]
        for name in names:
            self.plugin(self.data / name, [])
        m.write_load_order(str(self.data.parent), [names[0], names[-1]], "test")
        m.write_merge_record(str(self.data.parent), {names[-1]: names[1:3]})
        paths = [self.ini, self.manager / "mwmanager_sort_order.txt",
                 self.manager / "mwmanager_merged.txt", self.data / names[-1]]
        before = {path: path.read_bytes() for path in paths}
        before_names = {path.name for path in self.data.iterdir()}
        with mock.patch.object(m, "run_add_all_esp", side_effect=AssertionError("unexpected follow-up")):
            self.assertEqual(m.main([str(self.data), "--unmerge", "--dry-run", "--no-vortex"]), 0)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual({path.name for path in self.data.iterdir()}, before_names)


class AssetRepairAlwaysOn(unittest.TestCase):
    """Asset repairs are part of every fix and every Tidy: there is no switch to turn them off."""
    def test_there_is_no_switch(self):
        import mwmanager as m
        self.assertFalse(hasattr(m, "asset_repair_enabled"))
        for name in ("mwmanager_overrides.py", "mwmanager_tidy_ext.py", "mwmanager_gui.py", "mwmanager_state.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("--asset-repair", src, name)
            self.assertNotIn("asset_repair_enabled", src, name)

    def test_full_pass_always_regenerates_missing_assets(self):
        src = (ROOT / "mwmanager_overrides.py").read_text(encoding="utf-8")
        self.assertIn('"--generate-missing-assets"]', src)


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

    def test_post_option_tidy_restores_reopen_all_scope(self):
        args = mock.Mock(post_option_mods=["ModA-1-1-0"], only=[], staging=str(self.staging))
        seen = []
        with mock.patch.object(self.t, "run_plan", side_effect=lambda current: seen.append(list(current.only)) or []), \
                mock.patch.object(self.t, "review", return_value=([], [])), \
                mock.patch.object(self.t, "prune_empty_directories", return_value=0):
            self.t.run_post_option_tidy(args)
        self.assertEqual(seen, [["ModA-1-1-0"]])
        self.assertEqual(args.only, [])

    def test_init_targets_live_data_files_and_auto_applies(self):
        with mock.patch.object(self.t, "run_initial_tidy", return_value=0) as run:
            rc = self.t.mwmanager_main(["--init", "--game-data-files", str(self.data), "--apply", "--yes"])
        self.assertEqual(rc, 0)
        run.assert_called_once_with(str(self.data), apply_changes=True)

    def test_staged_tidy_refuses_an_invented_data_files_default(self):
        with mock.patch.dict(os.environ, {"MWMANAGER_DATA_FILES": ""}):
            with self.assertRaises(SystemExit):
                self.t.mwmanager_main(["--staging", str(self.staging), "--no-lua", "--no-assets"])

    def test_initial_tidy_commits_asset_cache_before_full(self):
        assets = mock.Mock()
        with mock.patch.object(self.t, "run_data_files_tidy", return_value=0), \
                mock.patch.object(self.t._scanner, "main", return_value=0) as scan, \
                mock.patch.object(self.t._scanner, "asset_health_module", return_value=assets):
            rc = self.t.run_initial_tidy(str(self.data), apply_changes=True)
        self.assertEqual(rc, 0)
        self.assertIn("--asset-check", scan.call_args.args[0])
        self.assertNotIn("--asset-repair", scan.call_args.args[0])
        assets.close_caches.assert_called_once_with()

    def test_initial_tidy_continues_after_repairs(self):
        assets = mock.Mock()
        with mock.patch.object(self.t, "run_data_files_tidy", return_value=0), \
                mock.patch.object(self.t._scanner, "main", return_value=1), \
                mock.patch.object(self.t._scanner, "asset_health_module", return_value=assets):
            rc = self.t.run_initial_tidy(str(self.data), apply_changes=True)
        self.assertEqual(rc, 0)
        assets.close_caches.assert_called_once_with()

    def test_staged_asset_repair_returns_exact_restore_record(self):
        root = self.staging / "ModA-1-1-0"
        asset = root / "Textures" / "broken.dds"
        original = b"not a DDS"
        mk(asset, original)
        operations = []
        assets = self.t._scanner.asset_health_module()
        done, skipped = assets.apply_folder(
            str(root), [("broken", str(asset), "Textures\\broken.dds", None)],
            self.t._scanner, str(self.data.parent), "ModA", journal_ops=operations)
        self.assertEqual((done, skipped), (1, []))
        self.assertEqual(len(operations), 1)
        self.assertEqual(Path(operations[0]["restore"]), asset)
        self.assertEqual(Path(operations[0]["backup"]).read_bytes(), original)

    def test_catalogued_animation_is_a_replacement_not_an_orphan(self):
        import sqlite3
        root = self.staging / "ModA-1-1-0"
        animation = root / "Meshes" / "xbase_anim.kf"
        mk(animation, self.t._scanner.asset_health_module().NIF_HEAD + b"\n" + b"\0" * 600)
        database = sqlite3.connect(self.manager / "game_catalog.sqlite3")
        try:
            database.execute("CREATE TABLE bsa_asset (asset_path_norm TEXT NOT NULL)")
            database.execute("INSERT INTO bsa_asset VALUES (?)", ("meshes/xbase_anim.kf",))
            database.commit()
        finally:
            database.close()
        issues, fixes = self.t._scanner.asset_health_module().check_folder(
            str(root), self.t._scanner, str(self.data), set())
        self.assertFalse(any("orphaned animation" in message for _severity, _path, message in issues))
        self.assertFalse(any(kind == "orphan" for kind, _full, _relative, _job in fixes))

    def test_gui_asset_scan_reports_every_two_thousand_files(self):
        assets = self.t._scanner.asset_health_module()

        def fake_walk(*_args, **_kwargs):
            for index in range(2001):
                yield "textures", f"Textures\\{index}.dds", f"file-{index}"

        def upstream(*_args, **_kwargs):
            list(assets._walk("unused", set()))
            return [], []

        output = io.StringIO()
        with mock.patch.object(assets, "_walk", fake_walk), \
                mock.patch.object(assets, "_upstream_check_folder", side_effect=upstream), \
                mock.patch.dict(os.environ, {"MWMANAGER_GUI": "1"}), contextlib.redirect_stdout(output):
            assets.check_folder(str(self.staging / "ModA-1-1-0"), self.t._scanner, str(self.data), set())
        self.assertIn("MWMANAGER-PROGRESS: Checked 2,000 asset files", output.getvalue())
        self.assertIn("Asset scan complete: 2,001 files checked", output.getvalue())

    def test_staged_wav_becomes_journaled_same_stem_mp3(self):
        root = self.staging / "ModA-1-1-0"
        sound = root / "Sound" / "ambient.wav"
        mk(sound, b"RIFF original WAV")
        operations = []

        def convert(command, **_kwargs):
            Path(command[-1]).write_bytes(b"MP3 converted")
            return mock.Mock(returncode=0, stdout="", stderr="")

        assets = self.t._scanner.asset_health_module()
        issues, fixes = assets.check_folder(str(root), self.t._scanner, str(self.data), set())
        self.assertFalse(any(kind == "snd" and job.get("mp3") for kind, _full, _relative, job in fixes))
        fixes = [("snd", str(sound), "Sound\\ambient.wav", {"mp3": True})]   # the apply path, for the later repathing
        with mock.patch.object(assets, "find_ffmpeg", return_value="ffmpeg.exe"), \
                mock.patch.object(assets.subprocess, "run", side_effect=convert):
            done, skipped = assets.apply_folder(
                str(root), fixes,
                self.t._scanner, str(self.data.parent), "ModA", journal_ops=operations)
        self.assertEqual((done, skipped), (1, []))
        self.assertFalse(sound.exists())
        self.assertEqual(sound.with_suffix(".mp3").read_bytes(), b"MP3 converted")
        self.assertTrue(any(op.get("restore") == str(sound) for op in operations))
        self.assertTrue(any(op.get("generated") == str(sound.with_suffix(".mp3")) for op in operations))

    def test_tga_beside_a_dds_of_the_same_name_is_moved_to_off(self):
        root = self.staging / "ModA-1-1-0"
        mk(root / "Textures" / "wall.dds", b"dds")
        mk(root / "Textures" / "wall.TGA", b"tga")
        mk(root / "Textures" / "floor.tga", b"tga")                # no .dds: in use
        assets = self.t._scanner.asset_health_module()
        _issues, fixes = assets.check_folder(str(root), self.t._scanner, str(self.data), set())
        unused = [f for f in fixes if f[0] == "orphan"]
        self.assertEqual([Path(f[1]).name for f in unused], ["wall.TGA"])
        operations = []
        self.assertEqual(assets.apply_folder(str(root), unused, self.t._scanner, str(self.data.parent), "ModA",
                                             journal_ops=operations), (1, []))
        self.assertFalse((root / "Textures" / "wall.TGA").exists())
        self.assertTrue((root / "Textures" / "floor.tga").exists())
        self.assertTrue(Path(operations[0]["dst"]).is_file() and "Off" in Path(operations[0]["dst"]).parts)

    def test_a_wide_splash_tga_becomes_a_2048_wide_rle_tga_with_the_original_in_off(self):
        """The game shows only TGA splashes (a DDS beside one was never used): at most 2048 wide, RLE when smaller,
        rewritten in place and journaled, the original kept in the mod's Off."""
        import struct
        texconv = Path(__file__).resolve().parents[1] / "tools" / "texconv.exe"
        if not texconv.is_file():
            self.skipTest("texconv.exe not available")
        root = self.staging / "ModA-1-1-0"
        big, small = root / "Splash" / "big.tga", root / "Splash" / "small.tga"
        for path, width in ((big, 4096), (small, 1024)):
            mk(path, struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, width, 8, 24, 0)
               + (bytes([40, 80, 120]) * (width * 8) if width > 2048 else bytes(range(256)) * (width * 8 * 3 // 256)))
        operations = []
        assets = self.t._scanner.asset_health_module()
        _issues, fixes = assets.check_folder(str(root), self.t._scanner, str(self.data), set())
        splash = [f for f in fixes if f[0] == "splash"]
        self.assertEqual([Path(f[1]).name for f in splash], ["big.tga"])
        with tempfile.TemporaryDirectory() as tools:
            shutil.copy2(texconv, Path(tools, "texconv.exe"))
            with mock.patch.object(assets, "TOOLS_DIR", tools):
                self.assertEqual(assets.apply_folder(str(root), splash, self.t._scanner, str(self.data.parent),
                                                     "ModA", journal_ops=operations), (1, []))
        data = big.read_bytes()
        self.assertEqual(struct.unpack_from("<HH", data, 12) + (data[16],), (2048, 4, 24))
        self.assertEqual(len(operations), 1)
        self.assertTrue(Path(operations[0]["backup"]).is_file() and "Off" in Path(operations[0]["backup"]).parts)
        _issues, again = assets.check_folder(str(root), self.t._scanner, str(self.data), set())
        self.assertFalse([f for f in again if f[0] == "splash"])                     # nothing left to do

    def test_staged_png_and_jpeg_become_dds_or_tga_with_the_texture_repairs(self):
        root = self.staging / "ModA-1-1-0"
        texture, page, shot = root / "Textures" / "ring.png", root / "BookArt" / "page.jpg", root / "shot.png"
        has_own = root / "Textures" / "own.png"
        lua_png = root / "Textures" / "menu" / "Logo.png"
        for path in (texture, page, shot, has_own, root / "MWSE" / "mods" / "x" / "icon.png",
                     root / "Extras" / "ModA" / "Images" / "title.jpg", root / "fomod" / "images" / "x.png", lua_png):
            mk(path, b"image")
        mk(root / "Textures" / "own.dds", b"the mod's own")
        mk(root / "MWSE" / "mods" / "x" / "main.lua", b'local image = "textures/menu/logo.png"')
        operations = []

        def convert(command, **_kwargs):
            out, kind = command[command.index("-o") + 1], command[command.index("-ft") + 1]
            Path(out, Path(command[-1]).stem + "." + kind).write_bytes(b"converted " + kind.encode())
            return mock.Mock(returncode=0, stdout="", stderr="")

        assets = self.t._scanner.asset_health_module()
        _issues, fixes = assets.check_folder(str(root), self.t._scanner, str(self.data), set())
        jobs = {Path(full).name: job for kind, full, _rel, job in fixes if kind == "tex" and job.get("convert")}
        self.assertEqual(jobs, {"ring.png": {"convert": ".dds"}, "page.jpg": {"convert": ".tga"},
                                "own.png": {"convert": ".dds"}})   # not root, MWSE, Extras, fomod or Lua-named
        self.assertTrue(any(rel.endswith("Logo.png") and "Lua" in message for _s, rel, message in _issues))
        self.assertEqual([p.casefold() for p in assets.lua_used_images(str(root))],
                         [str(Path("textures/menu/logo.png"))])
        with tempfile.TemporaryDirectory() as tools:
            Path(tools, "texconv.exe").write_bytes(b"")
            with mock.patch.object(assets, "TOOLS_DIR", tools), \
                    mock.patch.object(assets.subprocess, "run", side_effect=convert):
                done, skipped = assets.apply_folder(str(root), [f for f in fixes if isinstance(f[3], dict) and f[3].get("convert")],
                                                    self.t._scanner, str(self.data.parent), "ModA",
                                                    journal_ops=operations)
        self.assertEqual((done, skipped), (3, []))
        self.assertEqual(texture.with_suffix(".dds").read_bytes(), b"converted dds")
        self.assertEqual(page.with_suffix(".tga").read_bytes(), b"converted tga")
        self.assertEqual((root / "Textures" / "own.dds").read_bytes(), b"the mod's own")
        self.assertFalse(texture.exists() or page.exists() or has_own.exists())
        self.assertTrue(shot.exists())
        kept = [Path(op["backup"]) for op in operations if op.get("backup")]
        self.assertEqual(len(kept), 3)
        self.assertTrue(all(path.is_file() and "Off" in path.parts for path in kept))

    def test_staged_asset_report_is_kept_in_the_package(self):
        root = self.staging / "ModA-1-1-0"
        assets = mock.Mock()
        assets.nexus_id.return_value = None
        assets.bug_report_header.return_value = ["header"]
        assets.package_bug_report_subject.return_value = ("ModA", ["header"])
        assets.package_asset_bug_lines.side_effect = self.t._scanner.asset_health_module().package_asset_bug_lines
        assets.package_bug_report_path.return_value = str(root / "bugreports" / "ModA.bugreport.txt")
        ares = {"ah": assets, "m": self.t._scanner,
                "mods": {root.name: {"root": str(root),
                                     "issues": [("ERROR", "Textures\\missing.dds", "missing asset")]}}}
        with mock.patch.dict(os.environ, {"MWMANAGER_PLUGIN_NAME": "ModA"}):
            self.t._write_package_asset_bug_reports(ares)
        report_path, section, _header, lines = assets.update_bug_report.call_args.args
        self.assertEqual(Path(report_path).parent, root / "bugreports")
        self.assertEqual(section, "vortex_tidy (files)")
        self.assertTrue(any("missing asset" in line for line in lines))

    def test_lua_originals_are_kept_in_the_package(self):
        root = self.staging / "ModA-1-1-0"
        lua = mock.Mock()
        lua.apply_report.return_value = (1, [])
        lua.format_report.return_value = (["  repaired main.lua"], {"G1": 1})
        report = mock.Mock()
        args = mock.Mock(staging=str(self.staging), yes=True, _journal_extra={})
        lres = {"n_ok": 1, "checker": object(), "lf": lua, "reps": [(root.name, report)], "done": []}
        with mock.patch.dict(os.environ, {"MWMANAGER_PLUGIN_NAME": "ModA"}):
            self.t.lua_apply(args, lres)
        self.assertEqual(Path(lua.apply_report.call_args.kwargs["backup_root"]),
                         root / "Off" / "ModA" / "LuaRepairs")
        self.assertEqual(Path(lua.apply_report.call_args.kwargs["backup_relative_to"]), root)
        bugreport = root / "bugreports" / "ModA.mwse.bugreport.txt"
        self.assertTrue(bugreport.is_file())
        self.assertIn("repaired main.lua", bugreport.read_text(encoding="utf-8"))

    def test_rejected_lua_repair_still_writes_package_bugreport(self):
        root = self.staging / "ModA-1-1-0"
        lua = mock.Mock()
        lua.apply_report.return_value = (0, [(str(root / "MWSE" / "mods" / "A" / "main.lua"),
                                              "syntax check failed")])
        lua.format_report.return_value = (["  considered main.lua"], {"G1": 1})
        args = mock.Mock(staging=str(self.staging), yes=True, _journal_extra={})
        lres = {"n_ok": 1, "checker": object(), "lf": lua,
                "reps": [(root.name, mock.Mock())], "done": []}
        self.t.lua_apply(args, lres)
        report = root / "bugreports" / "ModA.mwse.bugreport.txt"
        self.assertIn("considered main.lua", report.read_text(encoding="utf-8"))
        self.assertEqual(set(lua.format_report.call_args.kwargs["refused"].values()), {"syntax check failed"})

    def test_lua_apply_does_not_treat_mwse_core_as_a_staged_package(self):
        """CORE_KEY is a lua_check label; applying it must not listdir a missing staging folder."""
        lua = mock.Mock()
        lua.apply_report.return_value = (1, [])
        lua.format_report.return_value = (["  repaired exclusionspage.lua"], {"KB4": 1})
        later = self.staging / "ModA-1-1-0"
        args = mock.Mock(staging=str(self.staging), yes=True, _journal_extra={})
        lres = {"n_ok": 2, "checker": object(), "lf": lua, "done": [],
                "reps": [(self.t.CORE_KEY, mock.Mock()), (later.name, mock.Mock())]}
        fake = Path(args.staging) / self.t.CORE_KEY
        with mock.patch.dict(os.environ, {"MWMANAGER_PLUGIN_NAME": "ModA"}):
            self.t.lua_apply(args, lres)
        self.assertFalse(fake.exists())
        self.assertEqual(lua.apply_report.call_count, 2)
        core_kwargs = lua.apply_report.call_args_list[0].kwargs
        self.assertNotIn("backup_relative_to", core_kwargs)
        self.assertNotIn("backup_root", core_kwargs)
        pkg_kwargs = lua.apply_report.call_args_list[1].kwargs
        self.assertEqual(Path(pkg_kwargs["backup_root"]), later / "Off" / "ModA" / "LuaRepairs")
        self.assertTrue((later / "bugreports" / "ModA.mwse.bugreport.txt").is_file())

    def test_asset_and_lua_reports_share_plugin_name_and_nexus_id(self):
        assets = self.t._scanner.asset_health_module()
        root = self.staging / "OAAB_Data 49042 2.7.5 2026-11-05T04-50Z sZJEZCNJ"
        asset = Path(assets.package_bug_report_path(str(root), "OAAB_Data.esm"))
        lua = Path(assets.package_bug_report_path(str(root), "OAAB_Data.esm", "mwse"))
        self.assertEqual(asset.name, "OAAB_Data [Nexus 49042].bugreport.txt")
        self.assertEqual(lua.name, "OAAB_Data [Nexus 49042].mwse.bugreport.txt")

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

    def test_platform_packaging_debris_is_quarantined_and_undoable(self):
        root = self.staging / "ModA-1-1-0"
        debris = [root / "__MACOSX" / "._resource", root / "Textures" / ".DS_Store",
                  root / "Meshes" / "._mesh.nif", root / "Thumbs.db", root / ".directory"]
        for path in debris:
            mk(path, b"metadata")
        rc, out = self.run_tidy()
        self.assertEqual(rc, 0, out)
        self.assertIn("remove platform packaging metadata", Path(self.t.PLAN).read_text(encoding="utf-8"))  # the plan file, not the window
        rc, out = self.run_tidy("--apply", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertTrue(all(not path.exists() for path in debris))
        quarantined = list((root / "Off").rglob("Packaging Metadata"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(len([path for path in quarantined[0].rglob("*") if path.is_file()]), len(debris))
        rc, out = self.run_tidy("--undo")
        self.assertEqual(rc, 0, out)
        self.assertTrue(all(path.is_file() for path in debris))

    def test_force_plan_only_bypasses_plan_comparison(self):
        original_compare = self.t.PF.compare

        def upstream(_args):
            self.assertEqual(self.t.PF.compare("reviewed", "fresh", None), ([], []))
            return 7

        with mock.patch.object(self.t, "_upstream_apply_main", side_effect=upstream):
            self.assertEqual(self.t.apply_main(mock.Mock(force_plan=True)), 7)
        self.assertIs(self.t.PF.compare, original_compare)

    def test_undo_only_restores_one_staged_plugin(self):
        mod_b = self.staging / "ModB-2-0"
        mk(mod_b / "ModB.esp", b"TES3" + b"\0" * 12)
        mk(mod_b / "ModB Readme.txt", b"readme b")
        rc, out = self.run_tidy()
        self.assertEqual(rc, 0, out)
        rc, out = self.run_tidy("--apply", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.staging / "ModA-1-1-0" / "ModA Readme.txt").exists())
        self.assertFalse((mod_b / "ModB Readme.txt").exists())

        plugin = self.staging / "ModA-1-1-0" / "ModA.esp"
        backup = self.manager / "ModA.esp.before-repair"
        backup.write_bytes(plugin.read_bytes())
        plugin.write_bytes(b"repaired")
        with Path(self.t.JOURNAL).open("a", encoding="utf-8") as journal:
            journal.write(json.dumps({"run": "second pass", "staging": str(self.staging),
                                      "plugins": ["ModA-1-1-0"]}) + "\n")
            journal.write(json.dumps({"restore": str(plugin), "backup": str(backup),
                                      "plugin": "ModA-1-1-0"}) + "\n")

        rc, out = self.run_tidy("--undo", "--only", "ModA-1-1-0")
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.staging / "ModA-1-1-0" / "ModA Readme.txt").is_file())
        self.assertEqual(plugin.read_bytes(), b"TES3" + b"\0" * 12)
        self.assertFalse((mod_b / "ModB Readme.txt").exists())
        self.assertTrue(any((mod_b / "Docs").rglob("ModB Readme.txt")))
        records = [json.loads(line) for line in Path(self.t.JOURNAL).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(record.get("plugin") == "ModB-2-0" for record in records))
        self.assertFalse(any(record.get("plugin") == "ModA-1-1-0" for record in records))

    def test_identical_duplicate_is_recorded_as_applied(self):
        root = self.staging / "ModA-1-1-0"
        source = root / "duplicate.dds"
        survivor = root / "Textures" / "duplicate.dds"
        mk(source, b"same")
        mk(survivor, b"same")
        Path(self.t.JOURNAL).parent.mkdir(parents=True, exist_ok=True)
        progress = []
        failed = self.t.apply(
            [(str(source), None, "identical duplicate from " + str(survivor))],
            str(self.staging), progress)
        self.assertEqual(failed, [])
        self.assertEqual(progress, [0])
        self.assertFalse(source.exists())
        self.assertTrue(survivor.is_file())

    def test_reopen_keeps_active_file_when_tidy_consolidated_its_source(self):
        import mock_fomod
        root = self.staging / "ModA-1-1-0"
        active = root / "ModA.esm"
        mk(active, b"TES3")
        prior = {"files": {"ModA.esm": {"backup": None,
                                          "source": str(root / "Extras" / "00 Core" / "ModA.esm")}}}
        self.assertEqual(mock_fomod._restore_activation(root, prior), {"ModA.esm": None})
        self.assertEqual(active.read_bytes(), b"TES3")

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

    def progress(self):
        messages = []

        def progress(message):
            messages.append(message)
        return progress, messages

    def test_options_plan_auto_applies_and_replans_after_refusal(self):
        pending = ["vortex_tidy plan, 2026-01-01 00:00: 1 mod(s), 1 to change, 0 left alone",
                   "MWMANAGER-PLAN: pending plan.txt"]
        calls, patch = self.fake([(0, pending), (0, ["done"])])
        progress, messages = self.progress()
        progress.ask_choice = mock.Mock(side_effect=AssertionError("GUI Tidy must not ask to apply its plan"))
        with patch:
            n = self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(n, 1)
        progress.ask_choice.assert_not_called()
        self.assertTrue(any("automatically" in message for message in messages))
        self.assertIn("--options", calls[0])
        self.assertNotIn("--asset-repair", calls[0])
        self.assertNotIn("--apply", calls[0])
        self.assertIn("--apply", calls[1])
        self.assertIn("--yes", calls[1])
        self.assertIn("done", messages)
        calls, patch = self.fake([
            (0, pending), (2, ["REFUSED: the plan ..."]), (0, pending), (0, ["done after rebuild"]),
        ])
        with patch:
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(len(calls), 4)
        self.assertNotIn("--options", calls[2])
        self.assertNotIn("--apply", calls[2])
        self.assertIn("--apply", calls[3])

    def test_three_changed_plans_offer_retry_or_stop(self):
        pending = ["vortex_tidy plan, x: 1 mod(s)", "MWMANAGER-PLAN: pending plan.txt"]
        calls, patch = self.fake([
            (0, pending), (2, ["refused 1"]), (0, pending), (2, ["refused 2"]),
            (0, pending), (2, ["refused 3"]),
        ])
        progress, _messages = self.progress()
        progress.ask_choice = mock.Mock(return_value="stop")
        with patch, self.assertRaises(self.gui.TidyPlanPaused):
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(len(calls), 6)
        progress.ask_choice.assert_called_once()
        self.assertEqual(progress.ask_choice.call_args.args[2], [("Try Again", "retry"), ("Stop staging", "stop")])

    def test_three_changed_plans_can_retry(self):
        pending = ["vortex_tidy plan, x: 1 mod(s)", "MWMANAGER-PLAN: pending plan.txt"]
        calls, patch = self.fake([
            (0, pending), (2, ["refused 1"]), (0, pending), (2, ["refused 2"]),
            (0, pending), (2, ["refused 3"]), (0, pending), (0, ["done"]),
        ])
        progress, _messages = self.progress()
        progress.ask_choice = mock.Mock(return_value="retry")
        with patch:
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(len(calls), 8)
        progress.ask_choice.assert_called_once()

    def test_second_failed_series_offers_apply_anyway(self):
        pending = ["vortex_tidy plan, x: 1 mod(s)", "MWMANAGER-PLAN: pending plan.txt"]
        outputs = [(0, pending)]
        for number in range(1, 7):
            outputs.append((2, [f"refused {number}"]))
            if number != 6:
                outputs.append((0, pending))
        outputs.append((0, ["applied current plan"]))
        calls, patch = self.fake(outputs)
        progress, _messages = self.progress()
        progress.ask_choice = mock.Mock(side_effect=["retry", "apply_anyway"])
        with patch:
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(len(calls), 13)
        self.assertIn("--force-plan", calls[-1])
        self.assertEqual(progress.ask_choice.call_count, 2)
        first_choices = progress.ask_choice.call_args_list[0].args[2]
        second_choices = progress.ask_choice.call_args_list[1].args[2]
        self.assertNotIn(("Apply Anyway", "apply_anyway"), first_choices)
        self.assertEqual(second_choices, [("Apply Anyway", "apply_anyway"), ("Stop staging", "stop")])

    def test_partial_apply_failure_stops_without_hiding_the_error(self):
        pending = ["vortex_tidy plan, x: 1 mod(s)", "MWMANAGER-PLAN: pending plan.txt"]
        calls, patch = self.fake([(0, pending), (1, ["111 repair(s) appeared after the moves"]),
                                  (0, pending), (0, ["done"])])
        progress, _ = self.progress()
        progress.ask_choice = mock.Mock(side_effect=AssertionError("one partial pass must not ask"))
        with patch, self.assertRaisesRegex(RuntimeError, "exit code 1"):
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(len(calls), 2)

    def test_apply_anyway_that_still_fails_halts_deployment(self):
        pending = ["vortex_tidy plan, x: 1 mod(s)", "MWMANAGER-PLAN: pending plan.txt"]
        outputs = [(0, pending)]
        for number in range(1, 7):
            outputs.append((2, [f"plan changed {number}"]))
            if number != 6:
                outputs.append((0, pending))
        outputs.append((1, ["one action failed"]))
        calls, patch = self.fake(outputs)
        progress, _ = self.progress()
        progress.ask_choice = mock.Mock(side_effect=["retry", "apply_anyway"])
        with patch, self.assertRaisesRegex(RuntimeError, "deployment is halted"):
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(len(calls), 13)

    def test_missing_plan_marker_cannot_be_treated_as_ready(self):
        calls, patch = self.fake([(0, ["unexpected tool output"])])
        progress, _messages = self.progress()
        with patch, self.assertRaisesRegex(RuntimeError, "valid plan state"):
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(len(calls), 1)

    def test_empty_plan_needs_no_apply(self):
        calls, patch = self.fake([(0, ["MWMANAGER-PLAN: empty plan.txt"])])
        progress, messages = self.progress()
        with patch:
            self.gui.run_tidy_steps("py", Path("s"), Path("d"), progress, "Tidy X", only="X")
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("nothing to tidy" in line for line in messages))

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

    def test_first_manual_move_creates_only_that_user_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            owner = mock.Mock()
            owner.pending_order_rules = [("A.esp", "B.esp")]
            with mock.patch.object(self.gui, "APP_DIR", base):
                self.gui.ManagerGUI._write_manual_order_rules(owner)
            text = (base / "mwmanager_rules.txt").read_text(encoding="cp1252")
            self.assertIn("[Order]\nA.esp\nB.esp", text.replace("\r\n", "\n"))
            self.assertFalse(any(name in text for name in self.gui.END_PATCHES))
            self.assertNotIn(self.gui.ESM_FIX_PATCH, text)


if __name__ == "__main__":
    unittest.main()
