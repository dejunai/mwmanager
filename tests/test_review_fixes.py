"""Fixes from the MWManager review: atomic settings, a locked setup step file, Vortex detection outside the GUI."""
import os
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mwmanager_gui  # noqa: E402
import mwmanager_setup_state as setup_state  # noqa: E402
import plugin_install  # noqa: E402


class AtomicSettingsTests(unittest.TestCase):
    def test_a_failed_write_leaves_the_old_settings_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini, prefs = Path(tmp, "mwmanager.ini"), Path(tmp, "prefs.json")
            with mock.patch.object(mwmanager_gui, "MWMANAGER_INI", ini), \
                    mock.patch.object(mwmanager_gui, "SETTINGS_FILE", prefs):
                mwmanager_gui.write_settings({"theme": "dark"})
                before = (ini.read_bytes(), prefs.read_bytes())
                with mock.patch.object(mwmanager_gui.os, "replace", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        mwmanager_gui.write_settings({"theme": "light"})
                self.assertEqual((ini.read_bytes(), prefs.read_bytes()), before)
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["mwmanager.ini", "prefs.json"])


class SetupStateLockTests(unittest.TestCase):
    def test_threads_changing_different_steps_lose_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, game = Path(tmp, "state.json"), Path(tmp, "game")
            setup_state.start(path, game)

            def work(step, n):
                for i in range(20):
                    setup_state.begin(path, game, step, pid=n * 100 + i)
                    setup_state.set_pid(path, game, step, n)

            threads = [threading.Thread(target=work, args=(step, n)) for n, step in enumerate(setup_state.STEPS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            steps = setup_state.load(path, game)["steps"]
            self.assertEqual({s: (steps[s]["attempts"], steps[s]["pid"]) for s in setup_state.STEPS},
                             {s: (20, n) for n, s in enumerate(setup_state.STEPS)})


class VortexDetectionTests(unittest.TestCase):
    def test_preinstall_finds_vortex_without_loading_the_gui(self):
        source = (ROOT / "preinstall.py").read_text(encoding="utf-8")
        self.assertNotIn("import mwmanager_gui", source)
        import mwmanager_detect
        self.assertIs(mwmanager_gui.vortex_install_path, mwmanager_detect.vortex_install_path)


class CatalogStepTests(unittest.TestCase):
    def test_the_catalog_step_is_begun_once_per_run(self):
        source = (ROOT / "mwmanager_gui.py").read_text(encoding="utf-8")
        body = source[source.index("def _run_initial_catalog"):]
        body = body[:body.index("\n    def ", 1)]
        self.assertEqual(body.count('setup_state.begin(state_path, game_dir, "catalog_complete"'), 1)
        loop = body[body.index("for label, command in commands:"):]
        self.assertNotIn("setup_state.begin(", loop)                      # inside the loop: set_pid only


class KeyboardToggleTests(unittest.TestCase):
    def test_space_toggles_the_selected_plugins_but_not_locked_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Data Files").mkdir()
            for name in ("A.esp", "B.esp"):
                (Path(tmp) / "Data Files" / name).write_bytes(b"")
            gui = mock.Mock(settings={"game_dir": tmp}, merge_inputs=set(), major_masters=set(),
                            active_names={"b.esp"}, _active_job=None)
            gui.tree.selection.return_value = ("A.esp", "B.esp", "Morrowind.esm")
            gui._toggle_active = lambda names: mwmanager_gui.ManagerGUI._toggle_active(gui, names)
            self.assertEqual(mwmanager_gui.ManagerGUI._toggle_active_key(gui), "break")
            self.assertEqual(gui.active_names, {"a.esp"})            # A on, B off; the core master untouched
            gui.refresh.assert_called_once()


class OptionsInstallCheckTests(unittest.TestCase):
    def test_options_checks_the_install_like_the_first_window(self):
        source = (ROOT / "mwmanager_gui.py").read_text(encoding="utf-8")
        options = source[source.index("    def open_options"):]
        options = options[:min(i for i in (options.find("\n    def ", 1), options.find("\ndef ", 1)) if i > 0)]
        self.assertIn("mwmanager_detect.valid_install(str(game))", options)


class PluginPathSafetyTests(unittest.TestCase):
    def test_archive_analysis_reports_zip_risks_without_extracting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "risky.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("Meshes/example.nif", b"one")
                zf.writestr("meshes/EXAMPLE.NIF", b"two")
                zf.writestr("installer.exe", b"payload")
                zf.writestr("large.txt", b"A" * (2 * 1024 * 1024))
                link = zipfile.ZipInfo("redirected")
                link.create_system = 3
                link.external_attr = 0o120000 << 16
                zf.writestr(link, b"outside")
                reparse = zipfile.ZipInfo("reparse")
                reparse.create_system = 0
                reparse.external_attr = 0x400
                zf.writestr(reparse, b"outside")

            report = plugin_install.analyze_archive({"archive": str(archive)}, root)

            self.assertTrue(report["blocked"])
            codes = {finding["code"] for finding in report["findings"]}
            self.assertTrue({"path_collision", "suspicious_payload",
                             "compression_ratio_anomaly", "link_member"} <= codes)
            self.assertFalse((root / "staging").exists())

    def test_archive_analysis_preserves_allowed_mwse_and_script_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "safe-scripts.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("Data Files/MWSE/mods/example/main.lua", b"safe")
                zf.writestr("OpenMW/Scripts/example.py", b"safe")
                zf.writestr("Docs/setup.exe", b"documentation-only")

            report = plugin_install.analyze_archive({"archive": str(archive)}, root)

            self.assertTrue(report["safe"])
            self.assertNotIn("suspicious_payload",
                             {finding["code"] for finding in report["findings"]})

    def test_archive_analysis_detects_encryption_from_zip_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "encrypted.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("payload.txt", b"content")
            data = bytearray(archive.read_bytes())
            for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
                offset = data.find(signature)
                while offset >= 0:
                    flags = int.from_bytes(data[offset + flag_offset:offset + flag_offset + 2],
                                           "little") | 1
                    data[offset + flag_offset:offset + flag_offset + 2] = flags.to_bytes(2, "little")
                    offset = data.find(signature, offset + 4)
            archive.write_bytes(data)

            report = plugin_install.analyze_archive({"archive": str(archive)}, root)

            self.assertTrue(report["blocked"])
            self.assertIn("encrypted_member",
                          {finding["code"] for finding in report["findings"]})

    def test_archive_analysis_reads_7z_solid_encryption_and_link_attributes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "risky.7z"
            archive.write_bytes(b"placeholder")
            listing = mock.Mock(returncode=0, stderr="", stdout=(
                f"Path = {archive}\nType = 7z\nSolid = +\nEncrypted = +\n\n"
                "Path = good.txt\nSize = 10\nPacked Size = 10\nSymbolic Link = -\n\n"
                "Path = redirected\nSize = 1\nPacked Size = 1\n"
                "Mode = lrwxrwxrwx\nSymbolic Link = outside\n\n"))
            with mock.patch.object(plugin_install, "_seven_zip", return_value="7z.exe"), \
                    mock.patch.object(plugin_install.subprocess, "run", return_value=listing) as run:
                report = plugin_install.analyze_archive({"archive": str(archive)}, root)

            codes = {finding["code"] for finding in report["findings"]}
            self.assertTrue({"solid_archive", "encrypted_member", "link_member"} <= codes)
            self.assertEqual(
                [finding["path"] for finding in report["findings"]
                 if finding["code"] == "link_member"],
                ["redirected"],
            )
            self.assertTrue(report["blocked"])
            self.assertTrue(all(call.args[0][1] == "l" for call in run.call_args_list))

    def test_archive_analysis_applies_expansion_and_member_count_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "oversized.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("one.bin", b"123456")
                zf.writestr("two.bin", b"123456")
            with mock.patch.multiple(
                    plugin_install, MAX_EXPANDED_MEMBER_BYTES=4,
                    MAX_EXPANDED_ARCHIVE_BYTES=8, MAX_ARCHIVE_MEMBERS=1):
                report = plugin_install.analyze_archive({"archive": str(archive)}, root)

            codes = {finding["code"] for finding in report["findings"]}
            self.assertTrue({"oversized_member", "oversized_archive",
                             "excessive_member_count"} <= codes)

    def test_extraction_refuses_fresh_high_severity_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "payload.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("setup.exe", b"payload")
            destination = root / "staging"

            with self.assertRaisesRegex(ValueError, "suspicious|setup.exe"):
                plugin_install.extract_archive({"archive": str(archive)}, destination, root)
            self.assertFalse(destination.exists())

    def test_regular_scan_does_not_open_payloads_after_high_risk_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads, manager = root / "downloads", root / "manager"
            downloads.mkdir()
            manager.mkdir()
            archive = downloads / "risky.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("Example.esp", b"plugin header")
                zf.writestr("setup.exe", b"payload")

            with mock.patch.object(
                    plugin_install, "_archive_esm_masters",
                    side_effect=AssertionError("payload was opened")) as inspect_master:
                items = plugin_install.scan_archives(downloads, manager)

            self.assertTrue(items[0]["blocked"])
            inspect_master.assert_not_called()
            self.assertTrue(items[0]["archive_risk"]["blocked"])

    def test_failed_extraction_does_not_leave_partial_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("good/a.txt", b"partial")
                link = zipfile.ZipInfo("link")
                link.create_system = 3
                link.external_attr = 0o120000 << 16
                zf.writestr(link, b"outside")
            destination = root / "staging" / "unsafe"
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                plugin_install.extract_archive({"archive": str(archive)}, destination, root)
            self.assertFalse(destination.exists())
            self.assertFalse(list(destination.parent.glob("*.extracting")))

    def test_deploy_refuses_a_linked_target_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = root / "package", root / "Data Files"
            (package / "linked").mkdir(parents=True)
            (package / "linked" / "file.txt").write_bytes(b"payload")
            (data / "linked").mkdir(parents=True)
            real_link = plugin_install._path_is_link
            with mock.patch.object(
                    plugin_install, "_path_is_link",
                    side_effect=lambda path: Path(path).resolve() == (data / "linked").resolve()
                    or real_link(Path(path))):
                with self.assertRaisesRegex(ValueError, "symbolic link or junction"):
                    plugin_install.deploy_package(package, data, "Test.esp")
            self.assertFalse((data / "linked" / "file.txt").exists())

    def test_deploy_refuses_a_linked_staging_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data = root / "package", root / "Data Files"
            (package / "linked").mkdir(parents=True)
            (package / "linked" / "file.txt").write_bytes(b"payload")
            data.mkdir()
            real_link = plugin_install._path_is_link
            with mock.patch.object(
                    plugin_install, "_path_is_link",
                    side_effect=lambda path: Path(path).resolve() == (package / "linked").resolve()
                    or real_link(Path(path))):
                with self.assertRaisesRegex(ValueError, "Staged package contains"):
                    plugin_install.deploy_package(package, data, "Test.esp")

    def test_transactional_deploy_rolls_back_game_and_staging_changes(self):
        from workflow_safety import DurableTransaction
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, data, package = root / "manager", root / "game" / "Data Files", root / "staging"
            manager.mkdir()
            data.mkdir(parents=True)
            package.mkdir()
            source = package / "Test.esp"
            source.write_bytes(b"new plugin")
            target = data / "Test.esp"
            target.write_bytes(b"old plugin")
            transaction = DurableTransaction(
                manager / "transactions" / "plugin-deploy", [manager, data.parent, package]).begin()
            linked, skipped, operations = plugin_install.deploy_package(
                package, data, "Test.esp", transaction=transaction)
            self.assertEqual((linked, skipped), (1, 0))
            self.assertEqual(target.read_bytes(), b"new plugin")
            self.assertEqual(len(operations), 1)
            transaction.rollback()
            self.assertEqual(target.read_bytes(), b"old plugin")
            self.assertFalse((manager / "transactions" / "plugin-deploy").exists())

    def test_seven_zip_link_entries_are_rejected_before_extraction(self):
        listing = mock.Mock(returncode=0, stderr="", stdout=(
            "Path = payload.txt\nSize = 7\n\n"
            "Path = redirected\nMode = lrwxrwxrwx\nSymbolic Link = outside\n\n"))
        with mock.patch.object(plugin_install.subprocess, "run", return_value=listing) as run:
            linked = plugin_install._seven_zip_link_members("7z.exe", Path("archive.7z"))
        self.assertEqual(linked, ["redirected"])
        self.assertEqual(run.call_count, 1)

    def test_removal_refuses_a_linked_target_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, data, package = root / "manager", root / "game" / "Data Files", root / "staging"
            manager.mkdir()
            (data / "linked").mkdir(parents=True)
            package.mkdir()
            source = package / "linked" / "file.txt"
            source.parent.mkdir()
            source.write_bytes(b"payload")
            target = data / "linked" / "file.txt"
            target.write_bytes(b"payload")
            state = {"version": 1, "deployments": [{
                "id": "unsafe", "plugins": [], "archives": [], "registered_archives": [],
                "staging_package": str(package), "operations": [{
                    "source": str(source), "target": str(target), "backup": None,
                    "size": 7, "sha256": None,
                }],
            }]}
            (manager / "plugin_deployments.json").write_text(json.dumps(state), encoding="utf-8")
            real_link = plugin_install._path_is_link
            with mock.patch.object(
                    plugin_install, "_path_is_link",
                    side_effect=lambda path: Path(path).resolve() == (data / "linked").resolve()
                    or real_link(Path(path))):
                with self.assertRaisesRegex(ValueError, "symbolic link or junction"):
                    plugin_install.remove_deployments(manager, data, {"unsafe"})
            self.assertTrue(target.exists())

    def test_removal_rolls_back_when_a_later_step_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, data, package = root / "manager", root / "game" / "Data Files", root / "staging"
            manager.mkdir()
            data.mkdir(parents=True)
            package.mkdir()
            source, target = package / "Test.esp", data / "Test.esp"
            source.write_bytes(b"plugin")
            os.link(source, target)
            state = {"version": 1, "deployments": [{
                "id": "selected", "plugins": ["Test.esp"], "archives": [],
                "registered_archives": ["Archive=0.0"], "staging_package": str(package),
                "operations": [{"source": str(source), "target": str(target), "backup": None,
                                "size": 6, "sha256": None}],
            }]}
            state_path = manager / "plugin_deployments.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with mock.patch("major_master_deploy.unregister_archives",
                            side_effect=RuntimeError("INI locked")):
                with self.assertRaisesRegex(RuntimeError, "INI locked"):
                    plugin_install.remove_deployments(manager, data, {"selected"})
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), b"plugin")
            self.assertEqual(json.loads(state_path.read_text("utf-8")), state)

    def test_removal_keeps_modified_file_but_removes_safe_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, data, package = root / "manager", root / "game" / "Data Files", root / "staging"
            manager.mkdir()
            data.mkdir(parents=True)
            package.mkdir()
            operations = []
            for name in ("Safe.esp", "Changed.esp"):
                source, target = package / name, data / name
                source.write_bytes(b"payload")
                if name == "Safe.esp":
                    os.link(source, target)
                else:
                    target.write_bytes(b"payload")
                operations.append({"source": str(source), "target": str(target), "backup": None,
                                   "size": 7,
                                   "sha256": plugin_install._sha256_file(source)
                                   if name == "Changed.esp" else None})
            (data / "Changed.esp").write_bytes(b"modified")
            state = {"version": 1, "deployments": [{
                "id": "partial", "plugins": ["Safe.esp", "Changed.esp"], "archives": [],
                "registered_archives": [], "staging_package": str(package), "operations": operations,
            }]}
            state_path = manager / "plugin_deployments.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            result = plugin_install.remove_deployments(manager, data, {"partial"})
            self.assertFalse((data / "Safe.esp").exists())
            self.assertTrue((data / "Changed.esp").is_file())
            self.assertEqual(result["pending"], 1)
            remaining = json.loads(state_path.read_text("utf-8"))["deployments"]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(len(remaining[0]["operations"]), 1)
            self.assertTrue(remaining[0]["operations"][0]["target"].endswith("Changed.esp"))


class JobCancellationTests(unittest.TestCase):
    def test_tidy_without_output_still_observes_cancellation(self):
        class BlockingOutput:
            def __init__(self):
                self.release = threading.Event()

            def __iter__(self):
                return self

            def __next__(self):
                self.release.wait()
                raise StopIteration

        output = BlockingOutput()
        process = mock.Mock(stdout=output)
        progress = mock.Mock()
        progress.check_cancelled.side_effect = [None, mwmanager_gui.JobCancelled("stop")]
        with mock.patch.object(mwmanager_gui, "_popen", return_value=process), \
                mock.patch.object(mwmanager_gui.managed_processes, "terminate",
                                  side_effect=lambda _process: output.release.set()) as terminate:
            with self.assertRaises(mwmanager_gui.JobCancelled):
                mwmanager_gui.run_tidy_steps(
                    Path(sys.executable), Path("staging"), Path("Data Files"),
                    progress, "test", quiet={})
        terminate.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
