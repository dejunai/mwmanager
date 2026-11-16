"""BAIN packages are tidied before their options are offered, so the option window finds the choices in
Extras/<plugin> and their descriptions in Docs/<plugin>."""
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod
import mwmanager_install as ext
import mwmanager_upstream
import major_master_deploy as deploy

README = ('Optional Modules:\n\nAn optional module can be found in "01 Faction Integration".\n\n'
          '- 01: TR_Factions.esp: adds Telvanni Hortator quests to the mainland\n')


class PreOptionTidyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)   # deploy leaves sqlite handles to the GC
        self.addCleanup(tmp.cleanup)
        self.staging = Path(tmp.name) / "staging"
        self.mod = self.staging / "Tamriel Rebuilt 42145"
        for rel, text in {"00 Core/Data Files/TR_Mainland.esm": "", "00 Core/Data Files/Docs/TR_Readme.txt": README,
                          "00 Core/Data Files/meshes/a.nif": "", "00 Core/mge3/tr.ovr": "",
                          "01 Faction Integration/Data Files/TR_Factions.esp": ""}.items():
            path = self.mod / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        fomod = self.staging / "Has Fomod" / "fomod"
        fomod.mkdir(parents=True)
        (fomod / "ModuleConfig.xml").write_text("<config/>", encoding="utf-8")
        (self.staging / "Has Fomod" / "01 Option").mkdir()
        (self.staging / "Flat").mkdir()

    def test_read_only_flags_from_the_archive_are_cleared(self):
        import stat
        folder, file = self.mod / "00 Core" / "mge3", self.mod / "00 Core" / "mge3" / "tr.ovr"
        for path in (file, folder):
            os.chmod(path, stat.S_IREAD)
        self.assertEqual(ext.clear_readonly_flags(str(self.staging), ["Flat"]), 0)     # other packages untouched
        self.assertEqual(ext.clear_readonly_flags(str(self.staging), [self.mod.name]), 2)
        self.assertTrue(all(os.stat(path).st_mode & stat.S_IWRITE for path in (file, folder)))
        shutil.rmtree(self.mod)

    def test_packed_layouts_are_tidied_first_fomod_too(self):
        self.assertEqual(ext.unprocessed_bain_packages(str(self.staging)), ["Has Fomod", self.mod.name])
        self.assertEqual(ext.unprocessed_bain_packages(str(self.staging), ["Flat"]), [])

    def test_processing_plugin_name_overrides_archive_display_name(self):
        with mock.patch.dict(os.environ, {"MWMANAGER_PLUGIN_NAME": "tamriel_data"}):
            self.assertEqual(ext.package_name(str(self.mod)), "tamriel_data")
            self.assertEqual(mock_fomod._package_display_name(self.mod), "tamriel_data")

    def test_stage_marker_keeps_plugin_name_after_restart(self):
        (self.mod / ".mwmanager-stage.json").write_text(
            '{"PLUGIN_NAME": "Tamriel Data", "PLUGIN_VERSION": "26.11", '
            '"selected_masters": ["Tamriel_Data.esm"]}', encoding="utf-8")
        self.assertEqual(ext.package_name(str(self.mod)), "Tamriel Data")
        self.assertEqual(mock_fomod._package_display_name(self.mod), "Tamriel Data")

    def test_options_and_their_docs_are_found_after_the_pre_tidy(self):
        self.assertEqual(mock_fomod._discover(self.mod, set()), [])           # the layout as packed: nothing found
        args = ext.mwmanager_parser().parse_args(
            ["--staging", str(self.staging), "--game-data-files", str(self.staging / "Data Files"), "--no-assets"])
        args.lua_only = False
        with mwmanager_upstream.use_manager_dir(str(self.staging.parent)):   # journal and plan stay in the temp folder
            ext.run_pre_option_tidy(args)
        self.assertTrue((self.mod / "TR_Mainland.esm").is_file())
        self.assertTrue((self.mod / "mge3" / "tr.ovr").is_file())
        self.assertFalse((self.mod / "00 Core").exists())
        self.assertEqual(ext.unprocessed_bain_packages(str(self.staging)), [])
        records = mock_fomod._discover(self.mod, set())
        self.assertEqual([r["label"] for r in records], ["Faction Integration"])
        self.assertIn("Telvanni Hortator", records[0]["docs"])

    def test_mge3_folder_deploys_beside_the_executable(self):
        root = self.staging.parent
        package = root / "mm" / "Pkg"
        (package / "mge3").mkdir(parents=True)
        (package / "meshes").mkdir()
        for rel in ("mge3/x.ovr", "meshes/a.nif"):
            (package / rel).write_bytes(b"x")
        hedr = struct.pack("<fI32s256sI", 1.3, 1, b"", b"", 0)                # an empty but well-formed master
        (package / "X.esm").write_bytes(b"TES3" + struct.pack("<III", len(hedr) + 8, 0, 0)
                                        + b"HEDR" + struct.pack("<I", len(hedr)) + hedr)
        data = root / "game" / "Data Files"
        data.mkdir(parents=True)
        (root / "manager").mkdir()
        (root / "game" / "Morrowind.ini").write_text("[Archives]" + chr(10), encoding="utf-8")
        real = Path(__file__).resolve().parents[1] / "database"               # the catalog module deploy imports
        (root / "manager" / "database").mkdir()
        for name in ("catalog.py", "schema.sql"):
            shutil.copy(real / name, root / "manager" / "database" / name)
        import mwmanager_journal
        mwmanager_journal.Journal(root / "manager" / "journal").capture_baseline(root / "game")
        with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):   # no leaked import
            sys.modules.pop("catalog", None)
            deploy.deploy(root / "mm", data, root / "manager", ["X.esm"])
        self.assertTrue((root / "game" / "mge3" / "x.ovr").is_file())
        self.assertFalse((data / "mge3").exists())
        self.assertTrue((data / "meshes" / "a.nif").is_file())
        deploy.remove_deployed(root / "mm", data, root / "manager")
        self.assertFalse((root / "game" / "mge3" / "x.ovr").exists())
        self.assertFalse((data / "X.esm").exists())


class EngineCoreFoldersTests(unittest.TestCase):
    def test_00_openmw_is_swept_off_and_00_mwse_unpacked_before_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            mod = staging / "Engine Pack"
            for rel in ("00 - MWSE/MWSE/mods/x/main.lua", "00 - MWSE/Engine.esp", "00 - OpenMW/Engine.omwscripts",
                        "00 - OpenMW/Engine.esp", "01 - Extra/meshes/a.nif"):
                (mod / rel).parent.mkdir(parents=True, exist_ok=True)
                (mod / rel).write_bytes(b"x")
            args = ext.mwmanager_parser().parse_args(
                ["--staging", str(staging), "--game-data-files", str(staging / "Data Files"), "--no-assets"])
            args.lua_only = False
            with mwmanager_upstream.use_manager_dir(tmp):
                ext.run_pre_option_tidy(args)
            self.assertTrue((mod / "MWSE" / "mods" / "x" / "main.lua").is_file())
            self.assertTrue((mod / "Engine.esp").is_file())
            self.assertTrue((mod / "Off" / "Engine Pack" / "OpenMW" / "Engine.omwscripts").is_file())
            self.assertEqual([r["path"].name for r in mock_fomod._discover(mod, set())], ["01 - Extra"])


class KeptCoreTests(unittest.TestCase):
    """00 Core is linked into the root and kept whole under Extras, so the install can be repeated."""

    def test_core_is_kept_linked_and_a_placed_file_wins_without_a_numbered_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            mod = staging / "Rocks Pack"
            for rel, data in {"00 Core/Textures/rock.dds": b"standard", "00 Core/Rocks.esp": b"TES3",
                              "01 HD/Textures/rock.dds": b"hd", "Textures/rock.dds": b"hd placed by an option"}.items():
                (mod / rel).parent.mkdir(parents=True, exist_ok=True)
                (mod / rel).write_bytes(data)
            args = ext.mwmanager_parser().parse_args(
                ["--staging", str(staging), "--game-data-files", str(staging / "Data Files"), "--no-assets"])
            args.lua_only = False
            with mwmanager_upstream.use_manager_dir(tmp):
                ext.run_pre_option_tidy(args)
                kept = mod / "Extras" / "Rocks Pack" / "00 Core"
                self.assertEqual((kept / "Textures" / "rock.dds").read_bytes(), b"standard")
                self.assertTrue(os.path.samefile(kept / "Rocks.esp", mod / "Rocks.esp"))
                self.assertEqual((mod / "Textures" / "rock.dds").read_bytes(), b"hd placed by an option")
                self.assertFalse(list(mod.rglob("* (2).*")))
                self.assertEqual([r["path"].name for r in mock_fomod._discover(mod, set())], ["01 HD"])
                self.assertEqual(ext.undo(str(staging), mod.name), 0)
            self.assertFalse((mod / "Rocks.esp").exists())
            self.assertEqual((mod / "00 Core" / "Textures" / "rock.dds").read_bytes(), b"standard")

    def test_a_fomod_package_is_tidied_first_and_its_options_still_resolve(self):
        xml = ('<config><installSteps><installStep name="Main"><optionalFileGroups><group name="Core Files" '
               'type="SelectAny"><plugins><plugin name="HD Data"><description>HD.</description><files>'
               '<folder source="01 HD Data" destination="" /></files><typeDescriptor><type name="Optional"/>'
               '</typeDescriptor></plugin></plugins></group></optionalFileGroups></installStep></installSteps>'
               '</config>')
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            mod = staging / "AATL Data"
            for rel, data in {"00 Core/Textures/a/rock.dds": b"standard", "00 Core/AATL.esm": b"TES3",
                              "01 HD Data/Textures/a/rock.dds": b"hd", "fomod/ModuleConfig.xml": xml.encode()}.items():
                (mod / rel).parent.mkdir(parents=True, exist_ok=True)
                (mod / rel).write_bytes(data)
            args = ext.mwmanager_parser().parse_args(
                ["--staging", str(staging), "--game-data-files", str(staging / "Data Files"), "--no-assets"])
            args.lua_only = False
            with mwmanager_upstream.use_manager_dir(tmp):
                ext.run_pre_option_tidy(args)
            self.assertTrue(os.path.samefile(mod / "AATL.esm", mod / "Extras" / "AATL Data" / "00 Core" / "AATL.esm"))
            [hd] = mock_fomod._discover_fomod(mod, set())
            self.assertEqual({k: v.read_bytes() for k, v in hd["payload_files"].items()},
                             {str(Path("Textures/a/rock.dds")): b"hd"})

    def test_an_option_overwriting_a_linked_core_file_needs_no_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "Pack"
            kept = mod / "Extras" / "Pack" / "00 Core" / "Textures" / "rock.dds"
            kept.parent.mkdir(parents=True)
            kept.write_bytes(b"standard")
            (mod / "Textures").mkdir()
            os.link(kept, mod / "Textures" / "rock.dds")
            option = mod / "Extras" / "Pack" / "01 HD" / "Textures" / "rock.dds"
            option.parent.mkdir(parents=True)
            option.write_bytes(b"hd")
            record = {"key": "hd", "number": 1, "path": option.parents[1]}
            backups = Path(tmp) / "backups"
            with mock.patch.object(mock_fomod, "_payload_files", return_value={"Textures/rock.dds": option}):
                state = mock_fomod._activate(mod, "Pack", [record], {"hd"}, {}, backups)
            self.assertEqual((mod / "Textures" / "rock.dds").read_bytes(), b"hd")
            self.assertEqual(kept.read_bytes(), b"standard")                   # not written through the link
            self.assertFalse(backups.exists() and any(backups.rglob("*.*")))
            mock_fomod._restore_activation(mod, state)
            self.assertTrue(os.path.samefile(mod / "Textures" / "rock.dds", kept))


class SoundsFolderTests(unittest.TestCase):
    def test_a_misnamed_sounds_folder_is_renamed_and_reported(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        staging = Path(tmp.name).resolve() / "staging"
        mod = staging / "Dr_Data-51776"
        for rel in ("Data Files/Dr_Data.esm", "Data Files/Sounds/dr/a.wav"):
            (mod / rel).parent.mkdir(parents=True, exist_ok=True)
            (mod / rel).write_bytes(b"x")
        plan = ext.plan_mod(str(mod))
        wav = next((s, d, why) for s, d, why in plan if s.endswith("a.wav"))
        self.assertEqual(os.path.relpath(wav[1], mod), os.path.join("Sound", "dr", "a.wav"))
        self.assertIn(ext.PACKING_MARK, wav[2])
        with mwmanager_upstream.use_manager_dir(tmp.name):
            os.makedirs(os.path.dirname(ext.JOURNAL), exist_ok=True)
            self.assertEqual(ext.apply(plan, str(staging)), [])
        report = next((mod / "bugreports").glob("*.bugreport.txt")).read_text(encoding="utf-8")
        self.assertIn("ERROR the author packed 'Sounds'", report)
        self.assertIn(os.path.join("Data Files", "Sounds", "dr", "a.wav") + " -> "
                      + os.path.join("Sound", "dr", "a.wav"), report)


if __name__ == "__main__":
    unittest.main()
