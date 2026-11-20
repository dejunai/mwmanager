"""BAIN packages are tidied before their options are offered, so the option window finds the choices in
Extras/<plugin> and their descriptions in Docs/<plugin>."""
import os
import io
import contextlib
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

    def test_core_openmw_file_is_planned_once_before_it_moves_to_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            mod = staging / "Tamriel Data"
            for rel, data in {
                "00 Data Files/Tamriel_Data.esm": b"TES3",
                "00 Data Files/Tamriel_Data.omwscripts": b"scripts",
                "00 Data Files/l10n/TamrielData/en.yaml": b"language",
                "00 Data Files/Textures/rock.dds": b"texture",
            }.items():
                (mod / rel).parent.mkdir(parents=True, exist_ok=True)
                (mod / rel).write_bytes(data)
            with mwmanager_upstream.use_manager_dir(tmp):
                Path(tmp, "outputs").mkdir(exist_ok=True)
                plan = ext.plan_mod(str(mod))
                self.assertNotIsInstance(plan, str)
                sources = [os.path.normcase(os.path.abspath(src)) for src, _dst, _why in plan]
                self.assertEqual(len(sources), len(set(sources)))
                omitted = [(src, dst) for src, dst, _why in plan
                           if src.endswith("Tamriel_Data.omwscripts")]
                self.assertEqual(len(omitted), 1)
                failed = ext.apply(plan, str(staging))
                self.assertEqual(failed, [])
            self.assertTrue(list((mod / "Off").rglob("Tamriel_Data.omwscripts")))

    def test_fomod_openmw_payload_is_quarantined_and_never_offered(self):
        xml = (
            '<config><installSteps><installStep name="Engine"><optionalFileGroups>'
            '<group name="Engine support" type="SelectOne"><plugins>'
            '<plugin name="OpenMW"><files><file source="Engine/OpenMW/engine.omwscripts" '
            'destination="engine.omwscripts"/></files></plugin>'
            '<plugin name="MWSE"><files><file source="Engine/MWSE/engine.lua" '
            'destination="MWSE/mods/engine/engine.lua"/></files></plugin>'
            '</plugins></group></optionalFileGroups></installStep></installSteps></config>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            mod = staging / "Engine Pack"
            for relative, data in {
                "fomod/ModuleConfig.xml": xml.encode(),
                "Engine/OpenMW/engine.omwscripts": b"openmw",
                "Engine/MWSE/engine.lua": b"mwse",
                "Engine.esp": b"TES3",
            }.items():
                (mod / relative).parent.mkdir(parents=True, exist_ok=True)
                (mod / relative).write_bytes(data)
            with mwmanager_upstream.use_manager_dir(tmp):
                Path(tmp, "outputs").mkdir(exist_ok=True)
                plan = ext.plan_mod(str(mod))
                self.assertNotIsInstance(plan, str)
                self.assertEqual(ext.apply(plan, str(staging)), [])
                records = mock_fomod._discover_fomod(mod, {"engine.esp"})
                visible = mock_fomod._select_vanilla_technology(records)
            self.assertTrue(list((mod / "Off").rglob("engine.omwscripts")))
            self.assertFalse((mod / "Engine" / "OpenMW").exists())
            self.assertFalse(any(item["label"] == "OpenMW" for item in visible))
            self.assertTrue(any(item["label"] == "MWSE" for item in visible))

    def test_gui_apply_reports_determinate_tidy_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            mod = staging / "Pack"
            mod.mkdir(parents=True)
            moves = []
            for index in range(4):
                source, target = mod / f"a{index}.txt", mod / "Docs" / f"a{index}.txt"
                target.parent.mkdir(exist_ok=True)
                source.write_text(str(index), encoding="utf-8")
                moves.append((str(source), str(target), "document"))
            output = io.StringIO()
            with mwmanager_upstream.use_manager_dir(tmp), \
                    mock.patch.dict(os.environ, {"MWMANAGER_GUI": "1"}), \
                    contextlib.redirect_stderr(output):
                Path(tmp, "outputs").mkdir(exist_ok=True)
                self.assertEqual(ext.apply(moves, str(staging)), [])
            progress = [line for line in output.getvalue().splitlines()
                        if line.startswith("MWMANAGER-PROGRESS:")]
            self.assertEqual(progress[0], "MWMANAGER-PROGRESS: Applying Tidy moves 0 of 4")
            self.assertEqual(progress[-1], "MWMANAGER-PROGRESS: Applying Tidy moves 4 of 4")

    def test_gui_apply_reports_only_every_two_thousand_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            mod = staging / "Pack"
            mod.mkdir(parents=True)
            moves = []
            for index in range(4002):
                source, target = mod / f"a{index}.txt", mod / "Docs" / f"a{index}.txt"
                target.parent.mkdir(exist_ok=True)
                source.write_text("", encoding="utf-8")
                moves.append((str(source), str(target), "document"))
            output = io.StringIO()
            with mwmanager_upstream.use_manager_dir(tmp), \
                    mock.patch.dict(os.environ, {"MWMANAGER_GUI": "1"}), \
                    contextlib.redirect_stderr(output):
                Path(tmp, "outputs").mkdir(exist_ok=True)
                self.assertEqual(ext.apply(moves, str(staging)), [])
            progress = [line for line in output.getvalue().splitlines()
                        if line.startswith("MWMANAGER-PROGRESS:")]
            self.assertEqual(progress, [
                "MWMANAGER-PROGRESS: Applying Tidy moves 0 of 4,002",
                "MWMANAGER-PROGRESS: Applying Tidy moves 1 of 4,002",
                "MWMANAGER-PROGRESS: Applying Tidy moves 2,000 of 4,002",
                "MWMANAGER-PROGRESS: Applying Tidy moves 4,000 of 4,002",
                "MWMANAGER-PROGRESS: Applying Tidy moves 4,002 of 4,002",
            ])

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


class TotspOptionTests(unittest.TestCase):
    """Solstheim Tomb of the Snow Prince: a FOMOD whose sources Tidy keeps in Extras, and a newer patch in place."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = Path(tmp.name) / "TOTSP"
        self.src = self.mod / "Extras" / "totsp"
        for rel in ("100 Patch - Patch for Purists/TOTSP_Patch_for_Purists_4.0.2.esp",
                    "102 Patch - abot's Boats/TOTSP_abotBoats.esp"):
            (self.src / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.src / rel).write_bytes(b"TES3")
        self.mod.joinpath("TOTSP_Patch_for_Purists_5.0.6.esp").write_bytes(b"TES3")

    def option(self, key, label, rel):
        name = Path(rel).name
        return {"key": key, "label": label, "number": 101, "required": False, "group_key": "fomod:1:1",
                "path": self.src / "fomod", "payload_files": {}, "all_payload_files": {name: self.src / rel},
                "targets": [], "compatible": [], "missing_masters": [], "mod_dir": self.mod}

    def test_fomod_sources_in_extras_are_not_offered_again(self):
        records = [self.option("pfp", "Patch for Purists 4.0.2", "100 Patch - Patch for Purists/"
                                                                 "TOTSP_Patch_for_Purists_4.0.2.esp"),
                   self.option("boats", "AbotBoats", "102 Patch - abot's Boats/TOTSP_abotBoats.esp")]
        nested = mock_fomod._discover_plugin_choices(self.mod, set(), mock_fomod._represented_option_sources(records))
        self.assertEqual([n["label"] for n in nested if n["label"].startswith("Extras")], [])

    def test_an_option_for_an_older_version_of_a_plugin_here_is_left_out(self):
        records = [self.option("pfp", "Patch for Purists 4.0.2", "100 Patch - Patch for Purists/"
                                                                 "TOTSP_Patch_for_Purists_4.0.2.esp"),
                   self.option("boats", "AbotBoats", "102 Patch - abot's Boats/TOTSP_abotBoats.esp")]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as said:
            kept = mock_fomod._drop_superseded_choices(self.mod, records)
        self.assertEqual([r["key"] for r in kept], ["boats"])
        self.assertIn("TOTSP_Patch_for_Purists_5.0.6.esp in this install is a newer version", said.getvalue())
        self.mod.joinpath("TOTSP_Patch_for_Purists_5.0.6.esp").unlink()   # without the newer one: offered
        self.assertEqual(len(mock_fomod._drop_superseded_choices(self.mod, records)), 2)
        self.assertEqual([r["key"] for r in mock_fomod._drop_superseded_choices(
            self.mod, records, {"TOTSP_Patch_for_Purists_5.0.6.esp"})], ["boats"])
        self.assertEqual(mock_fomod._plugin_version("TOTSP_Patch_for_Purists_4.0.2.esp"),
                         ("totsp_patch_for_purists", (4, 0, 2)))


class WaitingOptionMods(unittest.TestCase):
    def test_a_mod_staged_as_a_plugin_counts_for_a_masters_waiting_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            oaab = staging / "major_masters" / "OAAB_Data 49042"
            oaab.mkdir(parents=True)
            self.assertFalse(mock_fomod._mod_is_staged("glow in the dahrk (mod)", oaab))
            (staging / "plugins" / "Glow in the Dahrk-45886-3-3-0").mkdir(parents=True)
            self.assertTrue(mock_fomod._mod_is_staged("glow in the dahrk (mod)", oaab))


class OptionFileSpeedTests(unittest.TestCase):
    def test_option_files_are_linked_not_copied_and_undo_keeps_the_source(self):
        """A voice add-on's thousands of files were copied byte for byte once its options were chosen."""
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "Pack"
            source = mod / "Extras" / "Pack" / "01 Voices" / "Sound" / "Vo" / "hello.mp3"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"voice")
            record = {"key": "voices", "number": 1, "path": source.parents[2]}
            with mock.patch.object(mock_fomod, "_payload_files", return_value={"Sound/Vo/hello.mp3": source}), \
                    mock.patch.object(mock_fomod.shutil, "copy2", side_effect=AssertionError("copied")):
                state = mock_fomod._activate(mod, "Pack", [record], {"voices"}, {}, Path(tmp) / "backups")
            self.assertTrue(os.path.samefile(mod / "Sound" / "Vo" / "hello.mp3", source))
            mock_fomod._restore_activation(mod, state)
            self.assertFalse((mod / "Sound" / "Vo" / "hello.mp3").exists())
            self.assertEqual(source.read_bytes(), b"voice")

    def test_adding_option_files_does_no_resolve_or_mkdir_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "Pack"
            option = mod / "Extras" / "Pack" / "01 Voices"
            files = {}
            for i in range(40):
                source = option / "Sound" / "Vo" / f"v{i}.mp3"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"v")
                files[f"Sound/Vo/v{i}.mp3"] = source
            record = {"key": "voices", "number": 1, "path": option}
            resolves, mkdirs = [], []
            real_resolve, real_mkdir = Path.resolve, Path.mkdir
            with mock.patch.object(mock_fomod, "_payload_files", return_value=files), \
                    mock.patch.object(Path, "resolve", lambda self, *a, **k: resolves.append(self)
                                      or real_resolve(self, *a, **k)), \
                    mock.patch.object(Path, "mkdir", lambda self, *a, **k: mkdirs.append(self)
                                      or real_mkdir(self, *a, **k)):
                mock_fomod._activate(mod, "Pack", [record], {"voices"}, {}, Path(tmp) / "backups")
            self.assertEqual(sorted(p.name for p in mod.joinpath("Sound", "Vo").iterdir())[:2],
                             ["v0.mp3", "v1.mp3"])
            self.assertLess(len(resolves), 5)                 # was 2 per file
            self.assertLess(len(mkdirs), 5)                   # was 1 per file


class ExecutablesTests(unittest.TestCase):
    def test_tidy_moves_programs_and_scripts_to_off_executables(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        mod = Path(tmp.name).resolve() / "staging" / "Tamriel Data"
        for rel in ("Tamriel_Data.esm", "Docs/tamriel_data/MWLathe.exe", "Docs/tamriel_data/readme.txt",
                    "Textures/tool.bat", "MWSE/mods/td/main.lua"):
            (mod / rel).parent.mkdir(parents=True, exist_ok=True)
            (mod / rel).write_bytes(b"x")
        plan = ext.plan_mod(str(mod))
        moved = {os.path.relpath(s, mod): os.path.relpath(d, mod) for s, d, _why in plan if d}
        owner = ext.package_name(str(mod))
        self.assertEqual(moved[os.path.join("Docs", "tamriel_data", "MWLathe.exe")],
                         os.path.join("Off", owner, "Executables", "Docs", "tamriel_data", "MWLathe.exe"))
        self.assertEqual(moved[os.path.join("Textures", "tool.bat")],
                         os.path.join("Off", owner, "Executables", "Textures", "tool.bat"))
        self.assertNotIn(os.path.join("MWSE", "mods", "td", "main.lua"), moved)       # Lua is no program


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


class ApplyAfterFolderMove(unittest.TestCase):
    def test_a_file_planned_inside_a_folder_moved_earlier_still_moves(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        staging = Path(tmp.name).resolve() / "staging"
        mod = staging / "AATL Data"
        (mod / "00 Core" / "Meshes").mkdir(parents=True)
        (mod / "00 Core" / "Meshes" / "desktop.ini").write_bytes(b"x")
        plan = [(str(mod / "00 Core"), str(mod / "Extras" / "00 Core"), "keep the 00 folder under Extras"),
                (str(mod / "00 Core" / "Meshes" / "desktop.ini"), str(mod / "Off" / "desktop.ini"), "metadata")]
        out = io.StringIO()
        with mwmanager_upstream.use_manager_dir(tmp.name), contextlib.redirect_stdout(out):
            os.makedirs(os.path.dirname(ext.JOURNAL), exist_ok=True)
            self.assertEqual(ext.apply(plan, str(staging)), [])
        self.assertTrue((mod / "Off" / "desktop.ini").is_file())
        self.assertIn("2 of 2 applied", out.getvalue())


class Heartbeat(unittest.TestCase):
    def test_it_speaks_only_after_silence(self):
        import time
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with ext._long_phase_heartbeat("Scanning", interval=2):
                for _ in range(3):              # output every second: never silent long enough
                    print("progress")
                    time.sleep(1)
                time.sleep(3)                   # silent: one heartbeat
        text = out.getvalue()
        self.assertEqual(text.count("Working... please be patient"), 1)
        self.assertNotIn("hung", text)


if __name__ == "__main__":
    unittest.main()
