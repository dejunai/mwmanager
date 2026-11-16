"""MWManager runs the unchanged upstream Steam tools with its layers on top (mwmanager_upstream). Every path here
is a temporary folder: no real MWManager state, Data Files, Vortex or game folder is touched."""
import contextlib
import io
import json
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
        self.assertTrue(any("WAV (converted to MP3" in message for _severity, _path, message in issues))
        self.assertTrue(any(kind == "snd" and job.get("mp3")
                            for kind, _full, _relative, job in fixes))
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
        self.assertIn("remove platform packaging metadata", out)
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
        self.assertEqual(progress.ask_choice.call_args.args[2], [("Try Again", "retry"), ("Stop the install", "stop")])

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
        self.assertEqual(second_choices, [("Apply Anyway", "apply_anyway"), ("Stop the install", "stop")])

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


if __name__ == "__main__":
    unittest.main()
