"""MWManager writes nothing into the game install except what the game needs: backups, disabled plugins and
logs go to MWManager's own folder. Shared helper modules are the unchanged upstream copies. Nothing here touches
real MWManager state, Data Files, Vortex or the game: every path is a temporary folder."""
import ast
import contextlib
import hashlib
import io
import json
import os
import re
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402

# every upstream file MWManager runs: the scanner and its split modules, the asset checker, Tidy, the helpers
SHARED = tuple(sorted(p.name for p in ROOT.glob("mwdialog_*.py"))) + ("vortex_tidy.py",)


class SharedModulesAreUpstreamCopies(unittest.TestCase):
    def test_hashes_match_the_steam_upstream_snapshot(self):
        manifest = ROOT / "tools" / "steam-upstream" / "COPY_MANIFEST.json"
        try:
            files = json.loads(manifest.read_text(encoding="utf-8-sig"))["Files"]
        except (OSError, ValueError, KeyError, TypeError):
            self.skipTest("no current steam-upstream manifest")
        want = {Path(e["RelativePath"].replace("\\", "/")).name: e["SHA256"].lower() for e in files}
        for name in SHARED:
            raw = (ROOT / name).read_bytes()
            portable = raw.replace(b"\r\n", b"\n")
            hashes = {hashlib.sha256(raw).hexdigest(),
                      hashlib.sha256(portable.replace(b"\n", b"\r\n")).hexdigest()}
            self.assertIn(want.get(name), hashes, name)
            self.assertEqual(raw, (ROOT / "tools" / "steam-upstream" / name).read_bytes(), name)

    def test_every_upstream_module_is_shipped(self):
        for name in ("mwdialog_check.py", "mwdialog_assets.py", "vortex_tidy.py", "mwdialog_luafix.py",
                     "mwdialog_mastercheck.py", "mwdialog_paths.py", "mwdialog_tes3.py", "mwdialog_records.py"):
            self.assertIn(name, SHARED)
        self.assertGreaterEqual(len(SHARED), 25)

    def test_tidy_passes_mwmanagers_own_luajit(self):
        import mwmanager_install as t
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "tools" / "luajit" / ("luajit.exe" if os.name == "nt" else "luajit")
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            with mock.patch.object(t, "MANAGER_DIR", tmp):
                self.assertEqual(t.bundled_luajit(), str(exe))
        src = (ROOT / "mwmanager_tidy_ext.py").read_text(encoding="utf-8")
        self.assertIn("args.luajit = args.luajit or bundled_luajit()", src)


class BackupsStayOutOfTheGame(unittest.TestCase):
    def setUp(self):
        import mwmanager as m
        self.m = m
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.manager = base / "MWManager"
        self.game = base / "Game"
        self.data = self.game / "Data Files"
        (self.data / "textures").mkdir(parents=True)
        self.manager.mkdir()
        self.patch = mwmanager_upstream.use_manager_dir(self.manager)
        self.patch.__enter__()
        self.owner = mock.patch.object(m, "asset_owner_folder", return_value=(None, None))
        self.owner.start()

    def tearDown(self):
        self.owner.stop()
        self.patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def assertInGameOff(self, path):
        self.assertTrue(os.path.normcase(os.path.abspath(path)).startswith(
            os.path.normcase(str(self.data / "Off")) + os.sep), path)

    def assertNotInGame(self, path):
        self.assertFalse(os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(str(self.game))), path)

    def test_backup_root_never_the_game(self):
        self.assertNotInGame(self.m.backup_root_for(None, str(self.data)))
        staged = self.manager / "staging" / "Some Mod"
        self.assertEqual(self.m.backup_root_for(None, str(staged)), str(staged))
        self.assertEqual(self.m.backup_root_for(str(staged), str(self.data)), str(staged))

    def test_plugin_backup_of_a_generated_patch_goes_to_mwmanager(self):
        patch = self.data / "mwdialog_check Multipatch.esp"
        patch.write_bytes(b"TES3")
        bak = self.m.backup_path(str(patch))
        self.assertNotInGame(bak)
        Path(bak).write_bytes(b"old")
        self.assertIn(bak, self.m.backup_candidates(str(patch)))

    def test_shipped_asset_backup_stays_in_the_game_off_folder(self):
        (self.data / "textures" / "a.dds").write_bytes(b"orig")
        kept = self.m.backup_asset_original(str(self.data), os.path.join("textures", "a.dds"))
        self.assertInGameOff(kept)
        self.assertEqual(Path(kept).read_bytes(), b"orig")
        official = self.m.asset_backup_file("texture-reduction", os.path.join("textures", "b.dds"),
                                            data_dir=str(self.data))
        self.assertEqual(os.path.normcase(official), os.path.normcase(
            str(self.data / "Off" / "Found at Setup" / "texture-reduction" / "textures" / "b.dds")))

    def test_shipped_assets_use_original_install_and_patched_esps_use_manager_backups(self):
        for name in ("Siege at Firemoth.esp", "Morrowind.esm", "Tribunal.bsa"):
            path = self.m.plugin_off_backup_path(str(self.data), name, "plugin-repair", plugin=name)
            self.assertTrue(os.path.normcase(path).startswith(
                os.path.normcase(str(self.data / "Off" / "Found at Setup" / "plugin-repair")) + os.sep))
        esp = self.data / "Siege at Firemoth.esp"
        esp.write_bytes(b"TES3")
        bak = self.m.backup_path(str(esp))
        self.assertNotInGame(bak)
        self.assertIn(os.path.join("plugins", "Official Add-Ons"), bak)
        Path(bak).write_bytes(b"old")
        self.assertIn(bak, self.m.backup_candidates(str(esp)))

    def deployed_package_file(self, name="magewall.dds"):
        """A Dr_Data texture deployed by hard link from its staged package (the real owner lookup is used)."""
        import mwdialog_datatree
        package = self.manager / "staging" / "major_masters" / "Dr_Data-51776"
        (package / "textures" / "dr").mkdir(parents=True, exist_ok=True)
        (package / "Dr_Data.esm").write_bytes(b"TES3")
        staged = package / "textures" / "dr" / name
        staged.write_bytes(b"dds")
        (self.data / "textures" / "dr").mkdir(parents=True, exist_ok=True)
        os.link(staged, self.data / "textures" / "dr" / name)
        real_owner = mock.patch.object(self.m, "asset_owner_folder", mwdialog_datatree.asset_owner_folder)
        real_owner.start()
        self.addCleanup(real_owner.stop)
        return package, staged

    def test_an_asset_a_staged_package_deployed_is_kept_in_that_packages_off(self):
        package, _staged = self.deployed_package_file()
        path = self.m.plugin_off_backup_path(str(self.data), os.path.join("textures", "dr", "magewall.dds"),
                                             "Unused assets")
        self.assertEqual(os.path.normcase(path), os.path.normcase(
            str(package / "Off" / "Dr_Data" / "Unused assets" / "textures" / "dr" / "magewall.dds")))

    def test_an_unused_deployed_asset_leaves_staging_and_restore_puts_it_back(self):
        import mwdialog_assets
        import mwmanager_journal
        package, staged = self.deployed_package_file()
        mwmanager_journal.Journal(self.manager / "journal").capture_baseline(self.data.parent)
        rel = os.path.join("textures", "dr", "magewall.dds")
        mwdialog_assets.apply([("unused", str(self.data / rel), rel, "unused")],
                              str(self.data), str(self.game), self.m, [])
        kept = package / "Off" / "Dr_Data" / "Unused assets" / rel
        self.assertEqual(kept.read_bytes(), b"dds")
        self.assertFalse((self.data / rel).exists())
        self.assertFalse(staged.exists())                   # a redeploy can't link it back
        self.assertFalse((self.data / "Off").exists())      # nothing under Found at Setup
        mwdialog_assets.restore_unused(str(self.game), str(self.data))
        self.assertTrue(staged.is_file())
        self.assertTrue(os.path.samefile(staged, self.data / rel))

    def test_original_install_is_split_between_packages_and_found_at_setup(self):
        import mwmanager_journal
        package, staged = self.deployed_package_file()
        old = self.data / "Off" / "Original Install" / "Unused assets"
        (old / "textures" / "dr").mkdir(parents=True)
        os.replace(self.data / "textures" / "dr" / "magewall.dds", old / "textures" / "dr" / "magewall.dds")
        (old / "textures" / "game.dds").write_bytes(b"shipped")
        record = self.manager / "outputs" / "mwmanager_unused_assets.txt"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(f"t\ttextures\\dr\\magewall.dds\t{old / 'textures' / 'dr' / 'magewall.dds'}\tunused\n",
                          encoding="utf-8")
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.data.parent)
        self.assertEqual(journal.migrate_original_install(self.data.parent, [record]), 2)
        moved = package / "Off" / "Dr_Data" / "Unused assets" / "textures" / "dr" / "magewall.dds"
        self.assertEqual(moved.read_bytes(), b"dds")
        self.assertFalse(staged.exists())
        self.assertEqual((self.data / "Off" / "Found at Setup" / "Unused assets" / "textures" / "game.dds")
                         .read_bytes(), b"shipped")
        self.assertFalse((self.data / "Off" / "Original Install").exists())
        self.assertIn(str(moved), record.read_text(encoding="utf-8"))

    def test_initial_tidy_journals_a_shipped_asset_move(self):
        import mwdialog_assets
        import mwmanager_journal
        source = self.data / "textures" / "unused.dds"
        source.write_bytes(b"original")
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.data.parent)
        log = []
        mwdialog_assets.apply(
            [("unused", str(source), os.path.join("textures", "unused.dds"), "unused")],
            str(self.data), str(self.game), self.m, log,
        )
        destination = self.data / "Off" / "Found at Setup" / "Unused assets" / "textures" / "unused.dds"
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertFalse(source.exists())
        plan = journal.plan_rollback(self.data.parent)
        self.assertIn("Data Files/textures/unused.dds", {step["path"] for step in plan["steps"]})

    def test_generated_asset_is_logged_and_deleted_by_rollback(self):
        import mwmanager_journal
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.data.parent)
        rel = os.path.join("Sound", "missing.wav")
        made = self.m.write_generated(
            str(self.data), str(self.game), [(rel, "missing test sound", None, "Test.esp")], [],
        )
        self.assertEqual(made, [rel])
        generated = self.data / rel
        self.assertTrue(generated.is_file())
        entries, problems = journal.entries()
        self.assertEqual(problems, [])
        self.assertEqual((entries[-1]["action"], entries[-1]["tag"], entries[-1]["mod"]),
                         ("create", "generated-asset", "Test.esp"))
        plan = journal.plan_rollback(self.game)
        self.assertIn(("delete", "Data Files/Sound/missing.wav"),
                      {(step["op"], step["path"]) for step in plan["steps"]})
        journal.execute_rollback(self.data.parent, plan)
        self.assertFalse(generated.exists())

    def test_mwse_data_written_by_full_is_journaled_and_rolled_back(self):
        import types
        import mwmanager_journal
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.game)
        target = self.data / "MWSE" / "config" / self.m.COLLIDE_DATA

        def upstream(args, *rest):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{"cells": {}}', encoding="utf-8")
        with mock.patch.object(self.m, "_upstream_write_collide_data", upstream):
            self.m.write_collide_data(types.SimpleNamespace(data_dir=str(self.data)), [], {}, {}, {}, {}, {}, {})
        entries, _problems = journal.entries()
        self.assertEqual((entries[-1]["path"], entries[-1]["tag"]),
                         ("Data Files/MWSE/config/mwcollide_data.json", "mwse-data"))
        plan = journal.plan_rollback(self.game)
        self.assertEqual(plan["unexplained"], [])
        journal.execute_rollback(self.game, plan)
        self.assertFalse(target.exists())

    def test_unused_original_that_is_needed_again_is_moved_back(self):
        import mwmanager_journal
        original = self.data / "Sound" / "Cr" / "kgout" / "moan2.wav"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"RIFF vanilla")
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.data.parent)
        kept = self.data / "Off" / "Found at Setup" / "Unused assets" / "Sound" / "Cr" / "kgout" / "moan2.wav"
        kept.parent.mkdir(parents=True)
        os.replace(original, kept)                     # what the unused-asset purge did before a master needed it
        log = []
        self.m.write_generated(str(self.data), str(self.game),
                               [(os.path.join("Sound", "cr", "kgout", "moan2.wav"), "sound record", None, "TR.esm")], log)
        self.assertEqual(original.read_bytes(), b"RIFF vanilla")   # the original, not a silent stand-in
        self.assertFalse(kept.exists())
        self.assertIn("moved the original back", log[0][2])

    def test_missing_asset_is_copied_from_staging_else_built_never_stops(self):
        import mwmanager_journal
        vanilla = self.data / "Sound" / "gone.wav"            # a baseline file with no copy anywhere
        vanilla.parent.mkdir(parents=True, exist_ok=True)
        vanilla.write_bytes(b"RIFF")
        mwmanager_journal.Journal(self.manager / "journal").capture_baseline(self.data.parent)
        vanilla.unlink()
        staged = self.manager / "staging" / "major_masters" / "TD" / "Extras" / "00 Data Files" / "Icons" / "m" / "x.dds"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"real icon")
        log = []
        self.m.write_generated(str(self.data), str(self.game),
                               [(os.path.join("Icons", "m", "x.dds"), "book icon", None, "Cyr.esm"),
                                (os.path.join("Sound", "gone.wav"), "sound record", None, "TR.esm")], log)
        self.assertEqual((self.data / "Icons" / "m" / "x.dds").read_bytes(), b"real icon")
        self.assertTrue(staged.exists())                      # copied, the staged file stays
        self.assertTrue(vanilla.is_file())                    # a stand-in, not a stop

    def test_repaired_asset_is_logged_and_restored_by_rollback(self):
        import mwmanager_journal
        asset = self.data / "textures" / "repaired.dds"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"original")
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.game)
        self.m.replace_asset_in_place(str(asset), b"repaired")
        entries, problems = journal.entries()
        self.assertEqual(problems, [])
        self.assertEqual((entries[-1]["action"], entries[-1]["tag"]), ("replace", "asset-repair"))
        plan = journal.plan_rollback(self.game)
        self.assertEqual(plan["refuse"], [])
        journal.execute_rollback(self.game, plan)
        self.assertEqual(asset.read_bytes(), b"original")

    def test_repair_backup_and_hash_marker_are_deleted_by_rollback(self):
        import mwmanager_journal
        asset = self.data / "textures" / "kept.dds"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"original")
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.game)
        backup = Path(self.m.backup_asset_original(str(self.data), os.path.join("textures", "kept.dds")))
        self.m.replace_asset_in_place(str(asset), b"repaired")
        marker = Path(str(backup) + self.m.REPAIRED_SIDECAR)
        self.assertTrue(marker.is_file())
        entries, problems = journal.entries()
        self.assertEqual(problems, [])
        self.assertEqual(sum(entry.get("tag") == "asset-repair-backup" for entry in entries), 2)
        journal.execute_rollback(self.game, journal.plan_rollback(self.game))
        self.assertEqual(asset.read_bytes(), b"original")
        self.assertFalse(backup.exists())
        self.assertFalse(marker.exists())

    def test_generated_plugin_writer_is_logged_and_deleted_by_rollback(self):
        import mwmanager_journal
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.game)
        generated = self.data / "mwdialog_check Multipatch.esp"
        self.m.write_plugin_bytes(str(generated), b"TES3" + struct.pack("<III", 0, 0, 0))
        entries, problems = journal.entries()
        self.assertEqual(problems, [])
        self.assertEqual((entries[-1]["action"], entries[-1]["tag"]), ("create", "plugin-write"))
        plan = journal.plan_rollback(self.game)
        self.assertIn(("delete", "Data Files/mwdialog_check Multipatch.esp"),
                      {(step["op"], step["path"]) for step in plan["steps"]})

    def test_backups_of_files_that_are_not_shipped_go_to_mwmanager(self):
        gen = self.manager / "outputs"
        gen.mkdir()
        (gen / self.m.GENERATED_RECORD).write_text("[Some.esp]\ntextures\\gen.dds\t10\n", encoding="utf-8")
        (self.data / "textures" / "gen.dds").write_bytes(b"g")
        self.assertNotInGame(self.m.backup_asset_original(str(self.data), os.path.join("textures", "gen.dds")))
        for rel in (os.path.join("MWSE", "mods", "x", "main.lua"), os.path.join("textures", "mwmanager_x.dds"),
                    "Some Mod.esp", os.path.join("shaders", "x.fx")):
            self.assertNotInGame(self.m.plugin_off_backup_path(str(self.data), rel, "repair"))

    def test_older_mwmanager_folder_backups_are_still_found(self):
        esp = self.data / "Siege at Firemoth.esp"
        esp.write_bytes(b"TES3")
        old = self.manager / "backups" / "Data Files" / "Off" / "x" / "plugin-repair" / "Siege at Firemoth.esp.bak"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        self.assertIn(str(old), self.m.backup_candidates(str(esp)))

    def test_shipped_and_other_plugins_are_disabled_into_mwmanager(self):
        (self.data / "Siege at Firemoth.esp").write_bytes(b"TES3s")
        moved = self.m.move_plugins_to_disabled(str(self.data), ["Siege at Firemoth.esp"])
        self.assertNotInGame(moved[0][1])
        self.m.restore_disabled_plugins(str(self.data), ["Siege at Firemoth.esp"])
        self.assertTrue((self.data / "Siege at Firemoth.esp").is_file())

    def test_disabled_plugins_move_to_mwmanager_and_back(self):
        (self.data / "A.esp").write_bytes(b"TES3a")
        moved = self.m.move_plugins_to_disabled(str(self.data), ["A.esp"])
        self.assertEqual(len(moved), 1)
        self.assertNotInGame(moved[0][1])
        self.assertFalse((self.data / "Off").exists())
        self.m.restore_disabled_plugins(str(self.data), ["A.esp"])
        self.assertEqual((self.data / "A.esp").read_bytes(), b"TES3a")

    def test_plugins_disabled_by_older_versions_can_still_be_restored(self):
        legacy = self.data / "Off" / "Disabled Plugins"
        legacy.mkdir(parents=True)
        (legacy / "Old.esp").write_bytes(b"TES3o")
        self.m.restore_disabled_plugins(str(self.data), ["Old.esp"])
        self.assertTrue((self.data / "Old.esp").is_file())


class OfficialAddonDocs(unittest.TestCase):
    def test_shipped_readmes_go_to_docs_official_add_ons(self):
        import mwmanager_install as t
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "Siege at Firemoth.esp").write_bytes(b"TES3")
            (data / "Siege at Firemoth.txt").write_text("readme", encoding="utf-8")
            moves, errors = t.plan_data_files(str(data))
            self.assertEqual(errors, [])
            self.assertEqual([os.path.relpath(d, tmp) for _s, d, _w in moves],
                             [os.path.join("docs", "Official Add-Ons", "Siege at Firemoth.txt")])
            old = data / "Docs" / "Official Addon-Ons"
            old.mkdir(parents=True)
            (old / "Siege at Firemoth.txt").write_text("readme", encoding="utf-8")
            (data / "Siege at Firemoth.txt").unlink()
            moves, _ = t.plan_official_addon_docs(str(data))
            self.assertEqual([os.path.basename(os.path.dirname(d)) for _s, d, _w in moves], ["Official Add-Ons"])


class MwseCoreOriginalsStayInTheInstall(unittest.TestCase):
    """MWSE core Lua originals go to Data Files\\MWSE\\backups\\<path under MWSE>, first copy wins."""

    def setUp(self):
        import mwmanager as m
        self.m = m
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.manager = base / "MWManager"
        self.manager.mkdir()
        self.data = base / "Game" / "Data Files"
        self.mwse = self.data / "MWSE"
        for rel in ("core/a.lua", "core/lib/b.lua", "mods/somemod/main.lua"):
            (self.mwse / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.mwse / rel).write_text('event.register("loaded", f, { priority = 20000 })\n', encoding="utf-8")
        self.patch = mwmanager_upstream.use_manager_dir(self.manager)
        self.patch.__enter__()

    def tearDown(self):
        self.patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def lint(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.m.lint_installed_core_mwse(str(self.data.parent))

    def text(self, path):
        return path.read_text(encoding="utf-8-sig")

    def test_core_repairs_keep_the_first_original_inside_mwse_backups(self):
        older = self.manager / "backups" / "mwse-core" / "MWSE" / "core" / "lib" / "b.lua"
        older.parent.mkdir(parents=True)
        older.write_text("true original\n", encoding="utf-8")
        self.lint()
        self.assertIn("10000", self.text(self.mwse / "core" / "a.lua"))
        self.assertIn("20000", self.text(self.mwse / "backups" / "core" / "a.lua"))
        # an older version's kept copy is left where it is (still found), and this file gets its own first backup
        self.assertIn("10000", self.text(self.mwse / "core" / "lib" / "b.lua"))
        self.assertIn("20000", self.text(self.mwse / "backups" / "core" / "lib" / "b.lua"))
        self.assertEqual(self.text(older), "true original\n")
        report = json.loads((self.manager / "outputs" / "mwmanager_core_mwse_lua_report.json").read_text("utf-8"))
        b_item = next(i for i in report["files"] if i["path"].endswith("b.lua"))
        self.assertTrue(any(c["kind"] == "previous_repair" for c in b_item["changes"]["details"]))
        # MWSE\mods (staged mods, MWManager's own) are left alone
        self.assertIn("20000", self.text(self.mwse / "mods" / "somemod" / "main.lua"))
        # a later repair never overwrites the first original
        (self.mwse / "core" / "a.lua").write_text('event.register("x", f, { priority = 30000 })\n', encoding="utf-8")
        self.lint()
        self.assertIn("20000", self.text(self.mwse / "backups" / "core" / "a.lua"))
        self.assertFalse((self.manager / "backups" / "mwse-core" / "MWSE" / "core" / "a.lua").exists())

    def install(self, dry_run=False):
        args = types.SimpleNamespace(data_dir=str(self.data), dry_run=dry_run)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.m.install_mwse_lua_guards(args), 0)
        return out.getvalue()

    def test_the_lua_suite_is_installed_as_its_own_mods_and_the_old_package_leaves_mwse_mods(self):
        suite = ROOT / "reference" / "mwse-lua-guards"
        old = self.mwse / "mods" / "MWManager"
        (old / "modules" / "mwquickfixes").mkdir(parents=True)
        (old / "main.lua").write_text("-- consolidated\n", encoding="utf-8")
        (old / "modules" / "mwquickfixes" / "main.lua").write_text("-- quickfixes\n", encoding="utf-8")
        (old / "off" / "legacy-standalone" / "mwinfoloop").mkdir(parents=True)
        (old / "off" / "legacy-standalone" / "mwinfoloop" / "main.lua").write_text("-- older\n", encoding="utf-8")
        (self.mwse / "mods" / "mwquickfixes").mkdir(parents=True)
        (self.mwse / "mods" / "mwquickfixes" / "main.lua").write_text("-- standalone\n", encoding="utf-8")
        (self.mwse / "mods" / "mwfloatfix").mkdir(parents=True)
        (self.mwse / "mods" / "mwfloatfix" / "main.lua").write_text("-- old\n", encoding="utf-8")
        (self.mwse / "mods" / "mwfloatfix" / "old_module.lua").write_text("-- stale\n", encoding="utf-8")
        (self.mwse / "mods" / "mouseBleedFix").mkdir(parents=True)
        (self.mwse / "mods" / "mouseBleedFix" / "main.lua").write_text("-- user's own\n", encoding="utf-8")
        import mwmanager_journal
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.data.parent)
        self.install()
        manifest = json.loads((suite / "SOURCE_MANIFEST.json").read_text("utf-8"))
        for item in manifest["Files"]:
            rel = Path(item["Path"])
            installed = (self.mwse / "mods" / rel.relative_to("mods")) if rel.parts[0] == "mods" \
                else self.data / rel.name
            self.assertEqual((suite / rel).read_bytes(), installed.read_bytes(), str(rel))
        live = sorted(p.name for p in (self.mwse / "mods").iterdir())
        names = sorted(p.name for p in (suite / "mods").iterdir())
        self.assertEqual(live, sorted(names + ["mouseBleedFix", "mwmanagermenu", "somemod"]))
        menu = ROOT / "reference" / "mwmanager-menu"
        self.assertEqual((menu / "mods" / "mwmanagermenu" / "main.lua").read_bytes(),
                         (self.mwse / "mods" / "mwmanagermenu" / "main.lua").read_bytes())
        self.assertTrue((self.data / "mwmanagermenu-metadata.toml").is_file())
        self.assertEqual(sorted(p.name for p in (self.mwse / "mods" / "mwfloatfix").iterdir()), ["main.lua"])
        backups = self.mwse / "backups" / "mods"
        self.assertTrue((backups / "MWManager" / "modules" / "mwquickfixes" / "main.lua").is_file())
        self.assertTrue((backups / "MWManager" / "off" / "legacy-standalone" / "mwinfoloop" / "main.lua").is_file())
        self.assertTrue((backups / "mwquickfixes" / "main.lua").is_file())
        self.assertTrue((backups / "mwfloatfix" / "old_module.lua").is_file())
        self.assertEqual((self.mwse / "mods" / "mouseBleedFix" / "main.lua").read_text("utf-8"), "-- user's own\n")
        self.assertFalse((self.manager / "backups").exists())
        # a second run changes nothing and archives nothing more
        before = sorted(str(p.relative_to(self.data)) for p in self.data.rglob("*"))
        self.install()
        self.assertEqual(sorted(str(p.relative_to(self.data)) for p in self.data.rglob("*")), before)
        entries, problems = journal.entries()
        self.assertEqual(problems, [])
        self.assertTrue(entries)
        self.assertEqual({entry.get("tag") for entry in entries}, {"mwse-lua-suite"})
        plan = journal.plan_rollback(self.data.parent)
        self.assertEqual((plan["refuse"], plan["unexplained"]), ([], []))
        journal.execute_rollback(self.data.parent, plan)
        self.assertEqual((self.mwse / "mods" / "mwfloatfix" / "main.lua").read_text("utf-8"), "-- old\n")
        self.assertTrue((self.mwse / "mods" / "mwfloatfix" / "old_module.lua").is_file())
        self.assertTrue((self.mwse / "mods" / "MWManager" / "main.lua").is_file())
        self.assertFalse((self.data / "mwmanagermenu-metadata.toml").exists())

    def test_dry_run_writes_nothing(self):
        (self.mwse / "mods" / "MWManager").mkdir(parents=True)
        before = sorted(str(p.relative_to(self.data)) for p in self.data.rglob("*"))
        out = self.install(dry_run=True)
        self.assertIn("mwsaveguard", out)
        self.assertIn("MWManager", out)
        self.assertEqual(sorted(str(p.relative_to(self.data)) for p in self.data.rglob("*")), before)


class LuaSuiteIsTheUsersCopy(unittest.TestCase):
    """reference/mwse-lua-guards holds the Steam install's mw* suite, byte for byte; mwquickfixes is gone."""

    SUITE = ROOT / "reference" / "mwse-lua-guards"

    def test_files_match_the_source_manifest(self):
        manifest = json.loads((self.SUITE / "SOURCE_MANIFEST.json").read_text("utf-8"))
        listed = sorted(item["Path"] for item in manifest["Files"])
        present = sorted(p.relative_to(self.SUITE).as_posix() for p in self.SUITE.rglob("*")
                         if p.is_file() and p.name not in ("README.md", "SOURCE_MANIFEST.json"))
        self.assertEqual(listed, present)
        for item in manifest["Files"]:
            data = (self.SUITE / item["Path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["SHA256"], item["Path"])
            self.assertEqual(len(data), item["Length"], item["Path"])

    def test_mwguard_exempts_every_suite_mod(self):
        names = {p.name for p in (self.SUITE / "mods").iterdir() if (p / "main.lua").is_file()}
        self.assertEqual(len(names), 20)
        main = (self.SUITE / "mods" / "mwguard" / "main.lua").read_text("utf-8")
        block = main[main.index("exemptMods"):]
        block = block[:block.index("}")]
        self.assertEqual(set(re.findall(r'"([^"]+)"', block)), names)

    def test_no_quickfixes_or_consolidated_package_left(self):
        self.assertFalse((self.SUITE / "mods" / "MWManager").exists())
        self.assertFalse((self.SUITE / "legacy-standalone").exists())
        kept = {".git", "_backup", "backups"}                       # working folders, not shipped (.gitignore)
        self.assertEqual([p for p in ROOT.rglob("*quickfix*") if not kept & set(p.parts)], [])
        for lua in self.SUITE.rglob("*.lua"):
            self.assertNotIn("mwquickfixes", lua.read_text("utf-8"), str(lua))
            self.assertNotIn("mwmanagerMCM", lua.read_text("utf-8"), str(lua))


class GuiKeepsDisabledPluginsInItsFolder(unittest.TestCase):
    def test_disabled_plugins_dir_is_under_the_app(self):
        tree = ast.parse((ROOT / "mwmanager_gui.py").read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "disabled_plugins_dir")
        self.assertIn("APP_DIR", ast.unparse(fn))
        src = (ROOT / "mwmanager_gui.py").read_text(encoding="utf-8")
        self.assertNotIn('target = data_dir / "Off" / "Disabled Plugins" / name', src)


if __name__ == "__main__":
    unittest.main()
