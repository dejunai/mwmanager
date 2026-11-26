r"""Set-aside files go to <staged mod>\Off\<plugin>\<reason>\<path> (docs/OFF-LAYOUT.md): where each MWManager writer puts
them, and that every reader still finds files kept under the older spellings of a reason."""
import io
import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402
import mwdialog_offlayout as OL  # noqa: E402


def put(path, data=b"x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tes3(path):
    hedr = struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", 0)
    sub = b"HEDR" + struct.pack("<I", len(hedr)) + hedr
    return put(path, b"TES3" + struct.pack("<III", len(sub), 0, 0) + sub)


class AssetsLayerWritesTheLayout(unittest.TestCase):
    def setUp(self):
        import mwmanager as m
        self.m = m
        mwmanager_upstream.compose()
        import mwdialog_assets
        self.ah = mwdialog_assets
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.mod = self.base / "staging" / "Some Mod 1234"
        tes3(self.mod / "Plugin.esp")
        manager = self.base / "MWManager"
        manager.mkdir()
        state = mwmanager_upstream.use_manager_dir(manager)
        state.__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        owner = mock.patch.object(m, "asset_owner_folder", return_value=(None, None))
        owner.start()
        self.addCleanup(owner.stop)

    def test_reason_names_are_the_fixed_list(self):
        self.assertEqual(self.ah.FEATURE_OF, {
            "snd": "converted sounds", "tex": "converted textures", "splash": "converted splash screens",
            "keys": "fixed text keys", "abs": "fixed texture paths", "ascii": "ascii",
            "broken": "broken", "orphan": "orphaned animations", "unused": "unused assets"})
        for reason in self.ah.FEATURE_OF.values():
            self.assertTrue(OL.is_canonical(reason), reason)
        self.assertEqual(self.m.TEXT_KEY_BACKUP_FEATURE, "fixed text keys")
        self.assertEqual(self.m.REPAIR_SOUND_BACKUP_FEATURE, "converted sounds")

    def test_original_goes_to_the_plugin_then_reason_then_path(self):
        put(self.mod / "Sound" / "a.wav", b"orig")
        bak = self.ah._backup_in_mod(str(self.mod), os.path.join("Sound", "a.wav"),
                                     str(self.mod / "Sound" / "a.wav"), "Plugin.esp", self.m, "converted sounds")
        self.assertEqual(Path(bak), self.mod / "Off" / "Plugin" / "converted sounds" / "Sound" / "a.wav")
        self.assertEqual(Path(bak).read_bytes(), b"orig")
        self.assertEqual([p.name for p in (self.mod / "Off").iterdir()], ["Plugin"])
        self.assertEqual([p.name for p in (self.mod / "Off" / "Plugin").iterdir()], ["converted sounds"])

    def test_an_original_kept_in_an_older_layout_is_reused_not_copied_again(self):
        rel = os.path.join("Textures", "t.dds")
        put(self.mod / rel, b"update")
        for older in (("Reprocessed textures", "Plugin"), ("Plugin", "Reprocessed textures")):
            with self.subTest(older=older):
                legacy = put(self.mod / "Off" / older[0] / older[1] / rel, b"first")
                bak = self.ah._backup_in_mod(str(self.mod), rel, str(self.mod / rel), "Plugin.esp", self.m,
                                             "converted textures")
                self.assertEqual(Path(bak).read_bytes(), b"update")           # the update is kept beside it
                self.assertEqual(legacy.read_bytes(), b"first")
                self.assertFalse((self.mod / "Off" / "Plugin" / "converted textures" / rel).exists())
                shutil.rmtree(self.mod / "Off")

    def test_the_owner_is_the_mods_plugin_not_the_caller_s_name(self):
        rel = "x.wav"
        put(self.mod / rel)
        bak = self.ah._backup_in_mod(str(self.mod), rel, str(self.mod / rel), "Some Vortex Name", self.m,
                                     "converted sounds")
        self.assertEqual(Path(bak).parts[-4:], ("Off", "Plugin", "converted sounds", "x.wav"))

    def test_orphans_and_broken_files_go_under_their_reasons(self):
        for kind, reason in (("orphan", "orphaned animations"), ("broken", "broken")):
            with self.subTest(kind=kind):
                rel = os.path.join("Meshes", kind + ".nif")
                full = put(self.mod / rel, b"nif")
                change_log, ops = [], []
                with mock.patch.object(self.m, "stand_in_bytes", return_value=b"stand-in"):
                    self.ah.apply_folder(str(self.mod), [(kind, str(full), rel, "why")], self.m, str(self.base),
                                         "Plugin.esp", change_log=change_log, journal_ops=ops)
                kept = self.mod / "Off" / "Plugin" / reason / rel
                self.assertEqual(kept.read_bytes(), b"nif")
                self.assertEqual(change_log[0]["backup"], os.path.relpath(kept, self.mod))

    def test_data_files_unused_asset_goes_to_the_staged_mod_off(self):
        data = self.base / "Game" / "Data Files"
        rel = os.path.join("Textures", "u.dds")
        full = put(self.mod / rel, b"dds")
        with mock.patch.object(self.m, "asset_owner_folder", return_value=(str(self.mod), "Plugin.esp")):
            dest = self.ah._off_dest(self.m, str(data), rel, self.ah.FEATURE_OF["unused"])
        self.assertEqual(Path(dest), self.mod / "Off" / "Plugin" / "unused assets" / rel)
        self.assertTrue(full.is_file())
        put(dest)                                                    # an earlier original is never overwritten
        with mock.patch.object(self.m, "asset_owner_folder", return_value=(str(self.mod), "Plugin.esp")):
            again = self.ah._off_dest(self.m, str(data), rel, "unused assets")
        self.assertNotEqual(Path(again), Path(dest))
        self.assertEqual(Path(again).parent, Path(dest).parent)

    def test_non_ascii_originals_and_plugin_backups_are_under_ascii(self):
        old = os.path.join("Textures", "caf\u00e9.dds")
        new = os.path.join("Textures", "cafe.dds")
        put(self.mod / old, b"update")
        put(self.mod / new, b"fixed")
        # earlier versions kept it under Off\<plugin>\non-ascii\ and Off\<plugin>\names\
        for older in ("non-ascii", "names", "Reprocessed filenames"):
            with self.subTest(older=older):
                kept = put(self.mod / "Off" / "Plugin" / older / old, b"first")
                put(self.mod / old, b"update")
                self.ah.rename_ascii(str(self.mod), str(self.mod / old), old, new, lambda _t: None)
                self.assertFalse((self.mod / old).exists())
                self.assertEqual(kept.read_bytes(), b"first")
                shutil.rmtree(self.mod / "Off")
        put(self.mod / old, b"update")
        self.ah.rename_ascii(str(self.mod), str(self.mod / old), old, new, lambda _t: None)
        self.assertEqual((self.mod / "Off" / "Plugin" / "ascii" / old).read_bytes(), b"update")
        # the plugin that names the file keeps its original beside, as <plugin>\ascii\<plugin>.bak_ascii
        name = "caf\u00e9.nif"
        put(self.mod / "Meshes" / name, b"nif")
        hedr = struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", 1)
        body = b"NAME" + struct.pack("<I", 3) + b"s0\0" + b"MODL" + struct.pack("<I", 1 + len(name)) \
            + name.encode("cp1252") + b"\0"
        stat = b"STAT" + struct.pack("<III", len(body), 0, 0) + body
        sub = b"HEDR" + struct.pack("<I", len(hedr)) + hedr
        (self.mod / "Plugin.esp").write_bytes(b"TES3" + struct.pack("<III", len(sub), 0, 0) + sub + stat)
        rel = os.path.join("Meshes", name)
        self.ah.rename_ascii(str(self.mod), str(self.mod / rel), rel, rel.replace("\u00e9", "e"), lambda _t: None)
        self.assertTrue((self.mod / "Off" / "Plugin" / "ascii" / "Plugin.esp.bak_ascii").is_file())

    def test_no_loose_file_lands_in_the_plugin_folder(self):
        rel = "a.wav"
        put(self.mod / rel)
        self.ah._backup_in_mod(str(self.mod), rel, str(self.mod / rel), "Plugin.esp", self.m, "converted sounds")
        plugin_dir = self.mod / "Off" / "Plugin"
        self.assertEqual([p.name for p in plugin_dir.iterdir() if p.is_file()], [])


class UnusedAssetReader(unittest.TestCase):
    def test_every_spelling_of_unused_assets_counts_but_only_under_off(self):
        import mwmanager_upstream as up
        up.compose()
        import mwmanager_state as state
        sep = os.sep
        for good in ("Off|Plugin|unused assets|Textures|a.dds", "Off|Plugin|Unused assets|Textures|a.dds",
                     "Off|unused|Plugin|Textures|a.dds", "Off|Unused assets|Plugin|Textures|a.dds"):
            self.assertTrue(state._in_unused_assets(sep.join(("C:", "m") + tuple(good.split("|")))), good)
        for bad in ("Off|Plugin|broken|Textures|a.dds", "Textures|unused assets|a.dds", "Off|Plugin|a.dds"):
            self.assertFalse(state._in_unused_assets(sep.join(("C:", "m") + tuple(bad.split("|")))), bad)


class FomodUnusedOptions(unittest.TestCase):
    """Unused FOMOD plugin options: Off\\<plugin>\\unused fomod options\\, and the two older folders still read."""
    OLDER = (("Wanted", "FOMOD option plugin choices"), ("Wanted", "fomod-options", "plugin-choices"))

    def setUp(self):
        import mock_fomod
        self.fomod = mock_fomod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = Path(tmp.name) / "Wanted Package"
        self.mod.mkdir()
        env = mock.patch.dict(os.environ, {"MWMANAGER_PLUGIN_NAME": "Wanted"})
        env.start()
        self.addCleanup(env.stop)

    def record(self, rel, source, key="plugin-choice:x"):
        return {"key": key, "plugin_choice": True, "payload_files": {rel: source}}

    def test_an_unchosen_option_is_parked_under_the_new_reason_folder(self):
        rel = os.path.join("Option B", "B.esp")
        source = put(self.mod / rel, b"b")
        self.assertEqual(self.fomod._apply_plugin_choices(self.mod, [self.record(rel, source)], set()), 1)
        parked = self.mod / "Off" / "Wanted" / "unused fomod options" / rel
        self.assertEqual(parked.read_bytes(), b"b")
        self.assertEqual([p.name for p in (self.mod / "Off" / "Wanted").iterdir()], ["unused fomod options"])
        self.assertFalse(source.exists())

    def test_choosing_it_again_brings_it_back_from_the_new_and_the_older_folders(self):
        rel = os.path.join("Option B", "B.esp")
        for folder in (("Wanted", "unused fomod options"),) + self.OLDER:
            with self.subTest(folder="/".join(folder)):
                parked = put(self.mod.joinpath("Off", *folder, rel), b"b")
                record = self.record(rel, parked)
                self.assertEqual(self.fomod._apply_plugin_choices(self.mod, [record], {"plugin-choice:x"}), 1)
                self.assertEqual((self.mod / rel).read_bytes(), b"b")
                self.assertFalse(parked.exists())
                shutil.rmtree(self.mod / "Off")
                shutil.rmtree(self.mod / "Option B")

    def test_an_option_parked_by_an_older_version_stays_parked_when_still_unchosen(self):
        rel = os.path.join("Option B", "B.esp")
        parked = put(self.mod.joinpath("Off", "Wanted", "FOMOD option plugin choices", rel), b"b")
        self.assertEqual(self.fomod._apply_plugin_choices(self.mod, [self.record(rel, parked)], set()), 0)
        self.assertTrue(parked.is_file())

    def test_every_folder_that_can_hold_parked_options_is_found(self):
        for folder in (("Wanted", "unused fomod options"),) + self.OLDER + (("Other", "Unused Fomod Options"),):
            (self.mod.joinpath("Off", *folder)).mkdir(parents=True)
        (self.mod / "Off" / "Wanted" / "broken").mkdir()
        found = {p.relative_to(self.mod / "Off").as_posix() for p in self.fomod._unused_option_roots(self.mod)}
        self.assertEqual(found, {"Wanted/unused fomod options", "Wanted/FOMOD option plugin choices",
                                 "Wanted/fomod-options/plugin-choices", "Other/Unused Fomod Options"})
        self.assertEqual(self.fomod._unused_option_roots(self.mod / "nothing here"), [])

    def test_an_option_source_parked_in_off_is_found_under_either_name(self):
        import xml.etree.ElementTree as ET
        plugin = ET.fromstring('<plugin><files><file source="Option B\\B.esp" destination="B.esp"/></files></plugin>')
        for folder in (("Wanted", "unused fomod options"),) + self.OLDER:
            with self.subTest(folder="/".join(folder)):
                parked = put(self.mod.joinpath("Off", *folder, "Option B", "B.esp"), b"b")
                self.assertEqual(self.fomod._fomod_payload(self.mod, plugin), {"B.esp": parked})
                shutil.rmtree(self.mod / "Off")


class BsaPackedFilesLayout(unittest.TestCase):
    def setUp(self):
        import bsa_pack
        import major_master_deploy
        self.bsa_pack, self.deploy = bsa_pack, major_master_deploy
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_deploy_packer_keeps_files_and_its_log_under_packed_into_bsa(self):
        package = self.base / "Pack"
        put(package / "textures" / "rock.dds", b"dds")

        def popen(command, cwd, **_kwargs):
            Path(cwd, command[2]).write_bytes(b"BSA")
            process = mock.Mock(stdout=io.StringIO("packed\n"))
            process.wait.return_value = 0
            return process

        with mock.patch.object(self.deploy.subprocess, "Popen", side_effect=popen):
            self.deploy._pack_assets(package, "Pack.esp", Path("bsapack.exe"))
        reason = package / "Off" / "Pack" / "packed into bsa"
        self.assertTrue((reason / "bsapack.log").is_file())
        moved = [p.relative_to(reason).parts[1:] for p in reason.rglob("rock.dds")]
        self.assertEqual(moved, [("textures", "rock.dds")])          # <batch folder>\<original path>
        self.assertEqual([p.name for p in (package / "Off" / "Pack").iterdir()], ["packed into bsa"])
        self.assertTrue(OL.is_canonical("packed into bsa"))

    def test_staging_packer_keeps_the_loose_files_under_packed_into_bsa(self):
        data = self.base / "Data Files"
        mod = data / "Pack"
        put(mod / "Pack.esp")
        put(mod / "textures" / "rock.dds", b"dds")
        ini = put(self.base / "Morrowind.ini", b"[Archives]\r\nArchive 0=Morrowind.bsa\r\n")
        manager = self.base / "manager"
        put(manager / "tools" / "bsapack.dej.exe")

        def run(command, cwd=None, **_kwargs):
            if command[1] == "pack":
                Path(command[2] + ("" if command[2].lower().endswith(".bsa") else ".bsa")).write_bytes(b"BSA")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.bsa_pack.subprocess, "run", side_effect=run):
            code = self.bsa_pack.build_for_mod(mod, data, ini, manager, threshold=1, confirm=lambda _count: True)
        self.assertEqual(code, 0)
        self.assertEqual((mod / "Off" / "Pack" / "packed into bsa" / "textures" / "rock.dds").read_bytes(), b"dds")
        self.assertFalse((mod / "textures" / "rock.dds").exists())
        self.assertEqual([p.name for p in (mod / "Off" / "Pack").iterdir() if p.is_file()], [])


class StateOffHelpers(unittest.TestCase):
    """mwmanager_state: staged-mod writers use OL; Data Files\\Off keeps its fixed owners; readers find old names."""
    def setUp(self):
        import mwmanager as m
        self.m = m
        mwmanager_upstream.compose()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.manager = self.base / "MWManager"
        self.manager.mkdir()
        self.data = self.base / "Game" / "Data Files"
        self.data.mkdir(parents=True)
        state = mwmanager_upstream.use_manager_dir(self.manager)
        state.__enter__()
        self.addCleanup(state.__exit__, None, None, None)

    def test_off_dest_and_backup_path_use_plugin_repairs(self):
        mod = self.base / "staging" / "Some Mod 1234"
        tes3(mod / "Plugin.esp")
        dest = Path(self.m.off_dest(str(mod), "plugin-repair", "Plugin.esp", "Plugin.esp"))
        self.assertEqual(dest, mod / "Off" / "Plugin" / "plugin repairs" / "Plugin.esp")
        plugin = tes3(self.data / "Plugin.esp")
        with mock.patch.object(self.m, "asset_owner_folder", return_value=(str(mod), "Plugin.esp")):
            bak = Path(self.m.backup_path(str(plugin)))
            self.assertEqual(bak.parent, mod / "Off" / "Plugin" / "plugin repairs")
            self.assertTrue(bak.name.startswith("Plugin.esp.bak"))
            put(bak, b"v1")
            bak2 = Path(self.m.backup_path(str(plugin)))
            self.assertNotEqual(bak, bak2)
            self.assertEqual(bak2.parent, bak.parent)
            found = {os.path.normcase(p) for p in self.m.backup_candidates(str(plugin))}
        self.assertIn(os.path.normcase(str(bak)), found)
        old = put(mod / "Off" / "Plugin" / "plugin-repair" / "Plugin.esp.bak", b"legacy")
        with mock.patch.object(self.m, "asset_owner_folder", return_value=(str(mod), "Plugin.esp")):
            found = {os.path.normcase(p) for p in self.m.backup_candidates(str(plugin))}
        self.assertIn(os.path.normcase(str(old)), found)

    def test_backup_asset_original_defaults_to_repaired_originals_and_reuses_repair(self):
        mod = self.base / "staging" / "Mod"
        tes3(mod / "Mod.esp")
        rel = os.path.join("textures", "a.dds")
        put(self.data / rel, b"orig")
        put(mod / rel, b"orig")
        with mock.patch.object(self.m, "asset_owner_folder", return_value=(str(mod), "Mod.esp")):
            kept = Path(self.m.backup_asset_original(str(self.data), rel, plugin="Mod.esp"))
        self.assertEqual(kept, mod / "Off" / "Mod" / "repaired originals" / rel)
        self.assertEqual(kept.read_bytes(), b"orig")
        # an original already kept under the older "repair" folder is reused, not copied again
        shutil.rmtree(mod / "Off")
        legacy = put(mod / "Off" / "Mod" / "repair" / rel, b"first")
        put(self.data / rel, b"update")
        with mock.patch.object(self.m, "asset_owner_folder", return_value=(str(mod), "Mod.esp")):
            again = Path(self.m.backup_asset_original(str(self.data), rel, plugin="Mod.esp", feature="repair"))
        self.assertEqual(legacy.read_bytes(), b"first")
        self.assertEqual(again.read_bytes(), b"update")          # the update is kept beside it
        self.assertFalse((mod / "Off" / "Mod" / "repaired originals" / rel).exists())

    def test_shipped_files_stay_under_found_at_setup_with_the_canonical_reason(self):
        put(self.data / "textures" / "a.dds", b"orig")
        with mock.patch.object(self.m, "asset_owner_folder", return_value=(None, None)):
            kept = Path(self.m.backup_asset_original(str(self.data), os.path.join("textures", "a.dds")))
            path = Path(self.m.plugin_off_backup_path(str(self.data), "Siege at Firemoth.esp",
                                                      "plugin-repair", plugin="Siege at Firemoth.esp"))
        self.assertEqual(kept, self.data / "Off" / "Found at Setup" / "repaired originals" / "textures" / "a.dds")
        self.assertTrue(os.path.normcase(str(path)).startswith(
            os.path.normcase(str(self.data / "Off" / "Found at Setup" / "plugin repairs")) + os.sep))


class TidyAndInstallWriters(unittest.TestCase):
    def test_tidy_header_backup_uses_plugin_repairs_and_reuses_the_older_folder(self):
        mwmanager_upstream.compose_tidy()
        import vortex_tidy as tidy
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        staging = Path(tmp.name)
        package = staging / "Some Mod"
        tes3(package / "Plugin.esp")
        original = (package / "Plugin.esp").read_bytes()
        # older spelling already there: the first original is reused
        legacy = put(package / "Off" / "Plugin" / "Reprocessed plugin headers" / "Plugin.esp", original)
        backup = tidy._tidy_backup_original(str(package / "Plugin.esp"), str(staging), original)
        self.assertEqual(Path(backup), legacy)
        shutil.rmtree(package / "Off")
        backup = tidy._tidy_backup_original(str(package / "Plugin.esp"), str(staging), original + b"x")
        dest = Path(backup)
        self.assertEqual(dest, package / "Off" / "Plugin" / "plugin repairs" / "Plugin.esp")
        self.assertEqual(dest.read_bytes(), original + b"x")

    def test_plugin_install_uses_the_canonical_reason_folders(self):
        import plugin_install
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        package = Path(tmp.name) / "Pack"
        # a header that names TR_Data.esm is rewritten to Tamriel_Data.esm
        hedr = struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", 0)
        mast = b"TR_Data.esm\0"
        sub = b"HEDR" + struct.pack("<I", len(hedr)) + hedr + b"MAST" + struct.pack("<I", len(mast)) + mast
        tes3 = b"TES3" + struct.pack("<III", len(sub), 0, 0) + sub
        plugin = put(package / "Patch.esp", tes3)
        changed = plugin_install.apply_plugin_compatibility(package)
        self.assertTrue(changed)
        kept = package / "Off" / "Patch" / "plugin repairs" / "Patch.esp"
        self.assertEqual(kept.read_bytes(), tes3)
        self.assertNotEqual(plugin.read_bytes(), tes3)
        # superseded plugins and replaced files / executables / protected textures
        put(package / "Old.esp", b"TES3old")
        moved = plugin_install.remove_superseded_plugins(package, {"superseded_plugins": ["Old.esp"]})
        self.assertTrue(moved)
        # the package already has an Off owner (Patch); every reason folder goes under that one plugin name
        self.assertTrue((package / "Off" / "Patch" / "superseded plugins" / "Old.esp").is_file())
        self.assertEqual(Path(OL.off_path(str(package), "replaced files", hint="Patch")),
                         package / "Off" / "Patch" / "replaced files")
        self.assertEqual(Path(OL.off_path(str(package), "executables", hint="Patch")),
                         package / "Off" / "Patch" / "executables")
        self.assertEqual(Path(OL.off_path(str(package), "protected textures", hint="Patch")),
                         package / "Off" / "Patch" / "protected textures")
        for reason in ("plugin repairs", "superseded plugins", "replaced files",
                       "executables", "protected textures", "lua repairs"):
            self.assertTrue(OL.is_canonical(reason), reason)

    def test_mwse_lint_writes_lua_repairs_and_still_reads_luarepairs(self):
        import mwse_lint
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        staging = Path(tmp.name)
        package = staging / "Mod"
        tes3(package / "Mod.esp")
        lua = put(package / "MWSE" / "mods" / "x" / "main.lua", b"local x = nil\n")
        # an older original is found under LuaRepairs
        old = put(package / "Off" / "Mod" / "LuaRepairs" / "MWSE" / "mods" / "x" / "main.lua",
                  b"local x = nil\n")
        # the reader list includes that folder
        src = io.open(mwse_lint.__file__, encoding="utf-8").read()
        self.assertIn('"LuaRepairs"', src)
        self.assertIn('"lua repairs"', src)
        dest = Path(OL.off_path(str(package), "lua repairs", str(lua.relative_to(package)), hint="Mod.esp"))
        self.assertEqual(dest, package / "Off" / "Mod" / "lua repairs" / "MWSE" / "mods" / "x" / "main.lua")
        self.assertTrue(old.is_file())

    def test_tidy_writers_use_canonical_reason_folders_and_leave_old_spellings(self):
        mwmanager_upstream.compose_tidy()
        import vortex_tidy as tidy
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        package = Path(tmp.name) / "Pack"
        tes3(package / "Pack.esp")
        put(package / "Docs" / "tool.exe", b"exe")
        put(package / "Splash" / "load.tga", b"tga")
        put(package / "Splash" / "load.jpg", b"jpg")
        put(package / "Pack.omwaddon", b"omw")
        put(package / "desktop.ini", b"ini")
        # older spellings already under Off stay put: tidy does not walk Off
        old_exe = put(package / "Off" / "Pack" / "Executables" / "old.exe", b"old")
        old_splash = put(package / "Off" / "Pack" / "Splash" / "old.tga", b"tga")
        old_omw = put(package / "Off" / "Pack" / "OpenMW-addon-duplicate" / "Old.omwaddon", b"omw")
        old_meta = put(package / "Off" / "Pack" / "Packaging Metadata" / "thumbs.db", b"old")
        plan = tidy.plan_mod(str(package))
        owner = tidy.staging_package_name(str(package))
        moved = {os.path.relpath(s, package): os.path.relpath(d, package) for s, d, _why in plan if d}
        self.assertEqual(moved[os.path.join("Docs", "tool.exe")],
                         os.path.join("Off", owner, "executables", "Docs", "tool.exe"))
        self.assertEqual(moved[os.path.join("Splash", "load.tga")],
                         os.path.join("Off", owner, "redundant splash sources", "load.tga"))
        self.assertNotIn(os.path.join("Splash", "load.jpg"), moved)
        self.assertEqual(moved["Pack.omwaddon"],
                         os.path.join("Off", owner, "duplicate omwaddon", "Pack.omwaddon"))
        self.assertEqual(moved["desktop.ini"],
                         os.path.join("Off", owner, "packaging metadata", "desktop.ini"))
        dests = [os.path.relpath(d, package) for s, d, _why in plan if d]
        prefix = os.path.join("Off", owner) + os.sep
        for old_name in ("Executables", "OpenMW-addon-duplicate", "Packaging Metadata"):
            self.assertFalse(any(p.startswith(prefix + old_name + os.sep) or p == prefix.rstrip(os.sep) + os.sep + old_name
                                 for p in dests), old_name)
        # "Splash" as an Off reason is gone; the game Splash/ folder is a source, not a dest
        self.assertFalse(any(p.startswith(os.path.join("Off", owner, "Splash") + os.sep) for p in dests))
        self.assertTrue(old_exe.is_file())
        self.assertTrue(old_splash.is_file())
        self.assertTrue(old_omw.is_file())
        self.assertTrue(old_meta.is_file())


class ReducedTexturesWriter(unittest.TestCase):
    """asset_backup_file writes 'reduced textures'; an older texture-reduction folder is still read.

    Do not assert OL.is_canonical('reduced textures') until the next Conflict Tools sync lands.
    """
    def setUp(self):
        import mwmanager as m
        self.m = m
        mwmanager_upstream.compose()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.manager = self.base / "MWManager"
        self.manager.mkdir()
        self.data = self.base / "Game" / "Data Files"
        self.data.mkdir(parents=True)
        state = mwmanager_upstream.use_manager_dir(self.manager)
        state.__enter__()
        self.addCleanup(state.__exit__, None, None, None)

    def test_new_writes_use_reduced_textures_and_the_old_folder_is_still_found(self):
        rel = os.path.join("textures", "b.dds")
        with mock.patch.object(self.m, "asset_owner_folder", return_value=(None, None)):
            path = Path(self.m.asset_backup_file("reduced textures", rel, data_dir=str(self.data)))
            alias = Path(self.m.off_dest(str(self.data), "texture-reduction", "Found at Setup", rel))
        self.assertEqual(path, self.data / "Off" / "Found at Setup" / "reduced textures" / rel)
        self.assertEqual(alias, path)
        # an original already under the older spelling is found; a unique sibling stays in that folder
        legacy = put(self.data / "Off" / "Found at Setup" / "texture-reduction" / rel, b"first")
        with mock.patch.object(self.m, "asset_owner_folder", return_value=(None, None)):
            kept = Path(self.m.plugin_off_backup_path(str(self.data), rel, "reduced textures", unique=False))
            again = Path(self.m.asset_backup_file("reduced textures", rel, data_dir=str(self.data)))
        self.assertEqual(kept, legacy)
        self.assertEqual(again.parent, legacy.parent)
        self.assertIn("mwmanager", again.name)
        self.assertFalse((self.data / "Off" / "Found at Setup" / "reduced textures" / rel).exists())


if __name__ == "__main__":
    unittest.main()
