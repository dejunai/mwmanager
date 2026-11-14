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
            got = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            self.assertEqual(got, want.get(name), name)

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
        patch = self.data / "mwmanager Multipatch.esp"
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
        self.assertInGameOff(self.m.asset_backup_file("texture-reduction", os.path.join("textures", "b.dds"),
                                                      data_dir=str(self.data)))

    def test_shipped_plugin_and_archive_backups_stay_in_the_game_off_folder(self):
        for name in ("Siege at Firemoth.esp", "Morrowind.esm", "Tribunal.bsa"):
            self.assertInGameOff(self.m.plugin_off_backup_path(str(self.data), name, "plugin-repair"))
        esp = self.data / "Siege at Firemoth.esp"
        esp.write_bytes(b"TES3")
        bak = self.m.backup_path(str(esp))
        self.assertInGameOff(bak)
        Path(bak).write_bytes(b"old")
        self.assertIn(bak, self.m.backup_candidates(str(esp)))

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
                             [os.path.join("docs", "Official Add-ons", "Siege at Firemoth.txt")])
            old = data / "Docs" / "Official Addon-Ons"
            old.mkdir(parents=True)
            (old / "Siege at Firemoth.txt").write_text("readme", encoding="utf-8")
            (data / "Siege at Firemoth.txt").unlink()
            moves, _ = t.plan_official_addon_docs(str(data))
            self.assertEqual([os.path.basename(os.path.dirname(d)) for _s, d, _w in moves], ["Official Add-ons"])


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
        self.install()
        manifest = json.loads((suite / "SOURCE_MANIFEST.json").read_text("utf-8"))
        for item in manifest["Files"]:
            rel = Path(item["Path"])
            installed = (self.mwse / "mods" / rel.relative_to("mods")) if rel.parts[0] == "mods" \
                else self.data / rel.name
            self.assertEqual((suite / rel).read_bytes(), installed.read_bytes(), str(rel))
        live = sorted(p.name for p in (self.mwse / "mods").iterdir())
        names = sorted(p.name for p in (suite / "mods").iterdir())
        self.assertEqual(live, sorted(names + ["mouseBleedFix", "somemod"]))
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
        self.assertEqual(len(names), 13)
        main = (self.SUITE / "mods" / "mwguard" / "main.lua").read_text("utf-8")
        block = main[main.index("exemptMods"):]
        block = block[:block.index("}")]
        self.assertEqual(set(re.findall(r'"([^"]+)"', block)), names)

    def test_no_quickfixes_or_consolidated_package_left(self):
        self.assertFalse((self.SUITE / "mods" / "MWManager").exists())
        self.assertFalse((self.SUITE / "legacy-standalone").exists())
        self.assertEqual([p for p in ROOT.rglob("*quickfix*") if ".git" not in p.parts], [])
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
