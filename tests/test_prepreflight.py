"""Pre-preflight detection (mocked registry) and the trimmed preflight, on temporary folders only."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_detect as md  # noqa: E402
import mwmanager_prereqs as mp  # noqa: E402
import preinstall  # noqa: E402

CORE = {name: f"original {name}".encode() for name in preinstall.CORE_FILES}
KNOWN = {name: (hashlib.sha256(data).hexdigest(),) for name, data in CORE.items()}
LATEST = "1dce8de6b87c7b216c6809fbc261a176157764ce"


def write(path, data=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def make_game(root: Path, *, mcp_first="Beta", mge="g7", mwse_build=LATEST, updater=True, ini=True,
              logs=True) -> Path:
    write(root / "Morrowind.exe", b"MZ any store")
    if ini:
        write(root / "Morrowind.ini", "[General]\r\n")
    for name, data in CORE.items():
        write(root / "Data Files" / name, data)
    if mcp_first is not None:
        write(root / "Morrowind Code Patch.exe", b"MZ")
        write(root / "mcpatch/describe.json", json.dumps([{"category": mcp_first, "patches": [{"id": 180}]},
                                                          {"category": "Bug fixes", "patches": []}]))
    if mge:
        write(root / "MGEXEgui.exe", b"MZ")
        write(root / "d3d8.dll", b"MZ")
        write(root / "mge3/MGE XE Default Statics Classifiers.toml", "x = 1\n")
        if mge == "g7":
            write(root / "mgeXE.toml", "schema_version = 3\n")
    write(root / "MWSE.dll", b"MZ")
    if updater:
        write(root / "MWSE-Update.exe", b"MZ")
    if mwse_build:
        write(root / "mwse-version.txt", mwse_build)
    if logs:
        write_logs(root)
    return root


def write_logs(root: Path, age: float = -60):
    """Startup logs; age < 0 makes them newer than the setup files, age > 0 older."""
    stamp = time.time() - age
    write(root / "mgeXE.log", "MGE-XE G7 Fork v0.21.0\nMWSE.dll injected.\n")
    write(root / "MWSE.log", "Morrowind Script Extender v2.1.0-3400 hooked.\nFound MGE XE. Version: 0.21.0.0\n"
                             "Morrowind Code Patch installed features: 1, " + ", ".join(map(str, range(180, 196)))
          + "\n")
    for name in ("mgeXE.log", "MWSE.log"):
        os.utime(root / name, (stamp, stamp))


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class FakeRegistry:
    def __init__(self, keys):
        self.keys = {(h, p.casefold()): v for (h, p), v in keys.items()}

    def subkeys(self, hive, path):
        prefix = path.casefold() + "\\"
        return sorted({p[len(prefix):].split("\\")[0] for (h, p) in self.keys
                       if h == hive and p.startswith(prefix)}, key=str.casefold)

    def values(self, hive, path):
        return dict(self.keys.get((hive, path.casefold()), {}))

    def exists(self, hive, path):
        return (hive, path.casefold()) in self.keys


class Progress:
    def __init__(self, yes_no=(), choices=()):
        self.yes_no, self.choices, self.asked, self.lines = list(yes_no), list(choices), [], []

    def __call__(self, line):
        self.lines.append(line)

    def ask_yes_no(self, title, message):
        self.asked.append(("yes_no", title, message))
        return self.yes_no.pop(0)

    def ask_choice(self, title, message, choices, cancel=None):
        self.asked.append(("choice", title, message, choices))
        return self.choices.pop(0) if self.choices else cancel


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.object(mp, "CORE_FILE_HASHES", KNOWN)
        patcher.start()
        self.addCleanup(patcher.stop)


class ArchiveToolTests(unittest.TestCase):
    class Response:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, *_):
            return self.data

    def test_downloads_official_full_7zip_into_manager_tools(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        page = b'<a href="https://github.com/ip7z/7zip/releases/download/26.06/7z2606-x64.exe">Download</a>'
        opened = []

        def opener(url, **_):
            opened.append(url)
            return self.Response(page if url == preinstall.SEVEN_ZIP_DOWNLOAD_PAGE else b"MZinstaller")

        def runner(command, **_):
            destination = Path(command[-1].split("=", 1)[1])
            write(destination / "7z.exe", b"MZ")
            return types.SimpleNamespace(returncode=0)

        progress = Progress(yes_no=[True])
        with mock.patch.object(preinstall, "_seven_zip", return_value=None), \
                mock.patch.object(preinstall, "IS_WINDOWS", True), \
                mock.patch.object(preinstall.platform, "machine", return_value="AMD64"):
            self.assertTrue(preinstall.ensure_archive_tool(root, progress, opener=opener, runner=runner))
        self.assertEqual(opened[0], preinstall.SEVEN_ZIP_DOWNLOAD_PAGE)
        self.assertTrue((root / "tools/7zip/7z.exe").is_file())
        self.assertTrue((root / "tools/7zip-installer.exe").is_file())

    def test_declining_missing_archive_tool_stops(self):
        progress = Progress(yes_no=[False])
        with mock.patch.object(preinstall, "_seven_zip", return_value=None), \
                mock.patch.object(preinstall, "IS_WINDOWS", True):
            self.assertFalse(preinstall.ensure_archive_tool(Path("manager"), progress))


class DetectTests(Base):
    def registry(self, gog, steam_lib, vortex_exe, downloads_vortex=True):
        un = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
        return FakeRegistry({
            ("HKLM", rf"Software\WOW6432Node\GOG.com\Games\1435828767"): {
                "gameName": "The Elder Scrolls III: Morrowind GotY", "path": str(gog), "workingDir": str(gog)},
            ("HKLM", rf"{un}\1435828767_is1"): {"DisplayName": "The Elder Scrolls III: Morrowind GotY",
                                                 "InstallLocation": str(gog) + "\\", "Publisher": "GOG.com"},
            ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 22320"): {
                "DisplayName": "The Elder Scrolls III: Morrowind",
                "InstallLocation": str(steam_lib / "steamapps" / "common" / "Morrowind")},
            ("HKCU", r"Software\Valve\Steam"): {"SteamPath": str(self.tmp / "Steam")},
            ("HKLM", r"Software\WOW6432Node\Bethesda Softworks\Morrowind"): {"Installed Path": str(gog)},
            ("HKLM", rf"{un}\57979c68-f490-55b8-8fed-8b017a5af2fe"): {
                "DisplayName": "Vortex", "InstallLocation": "", "DisplayIcon": str(vortex_exe) + ",0"},
            ("HKCU", r"Software\Classes\nxm\shell\open\command"): {"": f'"{vortex_exe}" -d "%1"'},
        })

    def test_finds_gog_and_steam_once_each_and_vortex(self):
        gog = make_game(self.tmp / "GOG Galaxy/Games/Morrowind")
        steam_lib = self.tmp / "SteamLib"
        steam = make_game(steam_lib / "steamapps/common/Morrowind")
        write(self.tmp / "Steam/steamapps/libraryfolders.vdf",
              '"libraryfolders"\n{\n "1"\n {\n  "path" "%s"\n }\n}\n' % str(steam_lib).replace("\\", "\\\\"))
        vortex_exe = write(self.tmp / "Vortex/Vortex.exe", b"MZ")
        downloads = self.tmp / "VortexDownloads" / "morrowind"
        downloads.mkdir(parents=True)
        reg = self.registry(gog, steam_lib, vortex_exe)
        before = snapshot(self.tmp)
        with mock.patch("mwdialog_paths.resolve_downloads", return_value=str(downloads)), \
                mock.patch("mwdialog_paths.read_vortex_settings",
                           return_value={"downloads_tmpl": str(downloads.parent / "{game}")}):
            found = md.detect_all(reg)
        self.assertEqual(before, snapshot(self.tmp))                       # read-only
        stores = [(g["store"], Path(g["path"])) for g in found["games"]]
        self.assertEqual(stores, [("GOG", gog), ("Steam", steam)])
        vortex = found["managers"][0]
        self.assertTrue(vortex["detected"] and vortex["supported"])
        self.assertEqual(Path(vortex["exe"]), vortex_exe)                  # from DisplayIcon / nxm handler
        self.assertEqual(vortex["downloads"], str(downloads))
        mo2 = found["managers"][1]
        self.assertEqual((mo2["key"], mo2["detected"], mo2["supported"]), ("mo2", False, False))
        self.assertEqual(md.preferred_game(found["games"]), str(gog))
        self.assertEqual(md.preferred_game(found["games"], str(steam)), str(steam))
        self.assertEqual(md.preferred_downloads(found["managers"]), str(downloads))
        lines = md.report_lines(found)
        self.assertTrue(any(line.startswith("Morrowind (Steam): ") for line in lines))

    def test_stale_registry_paths_are_ignored(self):
        reg = self.registry(self.tmp / "gone", self.tmp / "nolib", self.tmp / "nope/Vortex.exe")
        with mock.patch("mwdialog_paths.resolve_downloads", return_value=None), \
                mock.patch("mwdialog_paths.read_vortex_settings", return_value={"reason": "no settings"}):
            found = md.detect_all(reg)
        self.assertEqual(found["games"], [])
        self.assertEqual(found["managers"][0]["exe"], "")
        self.assertIn("no settings", found["managers"][0]["notes"])
        self.assertEqual(md.preferred_game([]), "")

    def test_mo2_stub_is_pluggable(self):
        reg = FakeRegistry({("HKCU", r"Software\Classes\nxm\shell\open\command"):
                            {"": r'"C:\MO2\nxmhandler.exe" "%1"'}})
        mo2 = md.detect_mo2(reg)
        self.assertTrue(mo2["detected"])
        self.assertFalse(mo2["supported"])
        self.assertIn(md.detect_mo2, md.MOD_MANAGER_DETECTORS)

    def test_no_registry_off_windows(self):
        self.assertEqual(md.detect_game_installs(md.NoRegistry()), [])


class ArchiveSourceTests(unittest.TestCase):
    managers = [{"key": "vortex", "supported": True, "downloads": "/dl/vortex/morrowind"},
                {"key": "mo2", "supported": False, "downloads": "/dl/mo2"}]

    def test_folder_owner(self):
        self.assertEqual(md.source_for_folder(self.managers, "/dl/vortex/morrowind/"), "vortex")
        self.assertEqual(md.source_for_folder(self.managers, "/dl/mo2"), "unmanaged")      # not supported yet
        self.assertEqual(md.source_for_folder(self.managers, "/my/archives"), "unmanaged")

    def test_preferred_source(self):
        self.assertEqual(md.preferred_source(self.managers, None, ""), "vortex")
        self.assertEqual(md.preferred_source(self.managers, "unmanaged", ""), "unmanaged")
        self.assertEqual(md.preferred_source(self.managers, "vortex", "/my/archives"), "unmanaged")
        self.assertEqual(md.preferred_source([], None, ""), "unmanaged")
        self.assertIs(md.detect_managers.__defaults__[0], None)


class FirstLaunchTests(Base):
    def setUp(self):
        super().setUp()
        self.manager = self.tmp / "MWManager"
        self.manager.mkdir()

    def preflight(self, game, progress, launcher):
        import mwmanager_journal
        journal = mwmanager_journal.Journal(self.manager / "journal")
        if not journal.has_baseline():
            journal.capture_baseline(game)
        with mock.patch.object(preinstall, "_offer_baseline", return_value=True) as baseline:
            result = preinstall.run_preinstall(game, self.manager, progress, check_latest=lambda: (LATEST, None),
                                               launcher=launcher)
        return result, baseline

    def test_status_missing_stale_fresh(self):
        game = make_game(self.tmp / "g", logs=False)
        info = mp.startup_logs(game)
        self.assertEqual((info["fresh"], info["missing"]), (False, ["mgeXE.log", "MWSE.log"]))
        write_logs(game, age=3600)                                  # older than MWSE.dll etc.
        info = mp.startup_logs(game)
        self.assertEqual(info["stale"], ["mgeXE.log", "MWSE.log"])
        write_logs(game)
        info = mp.startup_logs(game)
        self.assertTrue(info["fresh"])
        lines = mp.startup_log_lines(info)
        self.assertIn("mgeXE.log: MGE-XE G7 Fork v0.21.0", lines)
        self.assertIn("MWSE.log: Morrowind Script Extender v2.1.0-3400 hooked.", lines)

    def test_launch_then_recheck_proceeds(self):
        game = make_game(self.tmp / "g", logs=False)
        before = snapshot(game)
        launched = []

        def launcher(path):
            launched.append(Path(path))
            return ""
        progress = Progress(choices=["launch", "recheck"])
        original = progress.ask_choice

        def ask_choice(title, message, choices, cancel=None):
            if progress.choices == ["recheck"]:
                write_logs(game)                                    # the user played to the main menu and quit
            return original(title, message, choices, cancel)
        progress.ask_choice = ask_choice
        result, baseline = self.preflight(game, progress, launcher)
        self.assertTrue(result)
        self.assertEqual(launched, [game.resolve()])
        baseline.assert_called_once()
        self.assertEqual([c[1] for c in progress.asked[0][3]], ["launch", "recheck", "skip", "stop"])
        self.assertIn("Preflight: startup logs found; the setup started successfully.", progress.lines)
        self.assertTrue(any("all 16 Skunk Works" in line for line in progress.lines))
        audit = json.loads((self.manager / "outputs/mwmanager_preinstall_audit.json").read_text())
        self.assertTrue(any("all 16 Skunk Works" in line for line in audit["runs"][-1]["status"]))
        self.assertFalse(any("no log yet" in line for line in audit["runs"][-1]["status"]))
        self.assertEqual(set(before), set(snapshot(game)) - {"mgeXE.log", "MWSE.log"})

    def test_recheck_without_logs_keeps_asking_and_stop_blocks_baseline(self):
        game = make_game(self.tmp / "g", logs=False)
        progress = Progress(choices=["recheck", "stop"])
        result, baseline = self.preflight(game, progress, mock.Mock(side_effect=AssertionError("launched")))
        self.assertFalse(result)
        baseline.assert_not_called()
        self.assertIn("still missing", progress.asked[1][2])

    def test_skip_needs_confirmation(self):
        game = make_game(self.tmp / "g", logs=False)
        progress = Progress(yes_no=[False, True], choices=["skip", "skip"])
        result, baseline = self.preflight(game, progress, mock.Mock(side_effect=AssertionError("launched")))
        self.assertTrue(result)
        baseline.assert_called_once()
        self.assertEqual([a[0] for a in progress.asked], ["choice", "yes_no", "choice", "yes_no"])

    def test_stale_logs_after_update_ask_again(self):
        game = make_game(self.tmp / "g", logs=False)
        write_logs(game, age=3600)
        progress = Progress(choices=["stop"])
        self.assertFalse(self.preflight(game, progress, mock.Mock())[0])
        self.assertIn("predate the current setup", progress.asked[0][2])

    def test_launcher_uses_selected_exe_without_waiting(self):
        game = make_game(self.tmp / "g")
        with mock.patch("subprocess.Popen") as popen:
            self.assertEqual(preinstall.launch_morrowind(game), "")
        args, kwargs = popen.call_args
        self.assertEqual(Path(args[0][0]), game / "Morrowind.exe")
        self.assertEqual(Path(kwargs["cwd"]), game)
        popen.return_value.wait.assert_not_called()
        popen.return_value.kill.assert_not_called()
        bad = OSError("elevation")
        bad.winerror = 740
        with mock.patch("subprocess.Popen", side_effect=bad):
            self.assertIn("administrator", preinstall.launch_morrowind(game))


class CodePatchLogTests(Base):
    def test_no_log_says_run_the_game(self):
        info = mp.code_patch_log(self.tmp)
        self.assertFalse(info["found"])
        self.assertIn("Run the game once with MGE XE", mp.code_patch_log_line(info))

    def test_mge_log_second_line(self):
        write(self.tmp / "mgeXE.log", "MGE XE v0.21\nCode Patch features: 1, 3, " +
              ", ".join(map(str, range(180, 196))) + "\nmore\n")
        info = mp.code_patch_log(self.tmp)
        self.assertEqual((info["source"], info["skunk_works_ids_missing"]), ("mgeXE.log line 2", []))
        self.assertIn("all 16 Skunk Works", mp.code_patch_log_line(info))

    def test_falls_back_to_mwse_log_header(self):
        write(self.tmp / "mgeXE.log", "MGE-XE G7 Fork v0.21.0\nMWSE.dll injected.\n")
        write(self.tmp / "MWSE.log", "MWSE hooked.\nFound MGE XE. Version: 0.21.0.0\n"
                                     "Morrowind Code Patch installed features: 1, 2, 180, 181\n")
        info = mp.code_patch_log(self.tmp)
        self.assertEqual(info["source"], "MWSE.log line 3")
        self.assertEqual(info["mge_version"], "0.21.0.0")
        self.assertEqual(len(info["skunk_works_ids_missing"]), 14)
        self.assertEqual(info["mge_log_line2"], "MWSE.dll injected.")

    def test_preflight_reports_it_without_blocking(self):
        game = make_game(self.tmp / "g", logs=False)
        report = preinstall.inspect_install(game, check_latest=lambda: (LATEST, None))
        self.assertEqual(report["blockers"], [])
        self.assertTrue(any("no log yet" in line for line in report["status"]))


class PrereqTests(Base):
    def test_nexus_metadata_marks_older_timestamped_cache_outdated(self):
        def response(payload):
            item = mock.MagicMock()
            item.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            return item

        replies = [response({"data": {"game": {"id": "100"}}}),
                   response({"data": {"modFiles": [
                       {"name": "MCP Beta", "version": "2.5 beta 20", "date": 1700000000,
                        "primary": 1, "category": "MAIN"}]}})]
        archive = self.tmp / "MCP beta-26348-2-5beta19-1631467698.7z"
        with mock.patch("urllib.request.urlopen", side_effect=replies):
            status, version = mp.cached_archive_status("mcp_beta", archive)
        self.assertEqual((status, version), ("outdated", "2.5 beta 20"))

    def test_nexus_version_ignores_browser_duplicate_suffix(self):
        def response(payload):
            item = mock.MagicMock()
            item.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            return item

        replies = [response({"data": {"game": {"id": "100"}}}),
                   response({"data": {"modFiles": [
                       {"name": "Morrowind Code Patch", "version": "2.4", "date": 1494630880,
                        "primary": 1, "category": "MAIN"}]}})]
        archive = self.tmp / "Morrowind Code Patch-19510-2-4 (2).7z"
        with mock.patch("urllib.request.urlopen", side_effect=replies):
            self.assertEqual(mp.cached_archive_status("mcp", archive), ("current", "2.4"))

    def test_mcp_first_entry_must_be_beta(self):
        game = make_game(self.tmp / "g")
        mcp = mp.detect(game)["mcp"]
        self.assertTrue(mcp["skunk_works_beta"])
        self.assertEqual(mcp["first_category"], "Beta")
        write(game / "mcpatch/describe.json", json.dumps([{"category": "Game mechanics"}, {"category": "Beta"}]))
        self.assertFalse(mp.detect(game)["mcp"]["skunk_works_beta"])
        self.assertEqual(mp.missing_keys(mp.detect(game)), ["mcp_beta"])
        (game / "mcpatch/describe.json").rename(game / "mcpatch/description.json")
        write(game / "mcpatch/description.json", json.dumps([{"category": "Beta", "patches": []}]))
        self.assertTrue(mp.detect(game)["mcp"]["skunk_works_beta"])

    def test_mge_any_build_g7_by_toml(self):
        game = make_game(self.tmp / "g", mge="other")
        mge = mp.detect(game)["mge_xe"]
        self.assertTrue(mge["present"])
        self.assertFalse(mge["g7"])
        self.assertEqual(mge["g7_files_without_settings"], ["MGE XE Default Statics Classifiers.toml"])
        write(game / "mgeXE.toml", "schema_version = 3\n")
        self.assertTrue(mp.detect(game)["mge_xe"]["g7"])

    def test_mwse_status(self):
        self.assertEqual(mp.mwse_status({"build": LATEST}, LATEST), "current")
        self.assertEqual(mp.mwse_status({"build": "a" * 40}, LATEST), "outdated")
        self.assertEqual(mp.mwse_status({"build": None}, LATEST), "outdated")
        self.assertEqual(mp.mwse_status({"build": LATEST}, None), "unknown")

    def test_latest_build_offline_is_unknown(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            sha, why = mp.latest_mwse_build()
        self.assertIsNone(sha)
        self.assertIn("offline", why)

    def test_latest_build_reads_release_target(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"tag_name": "build-automatic", "target_commitish": LATEST.upper()}).encode()
        with mock.patch("urllib.request.urlopen", return_value=Response()):
            self.assertEqual(mp.latest_mwse_build(), (LATEST, None))


class PreflightTests(Base):
    def setUp(self):
        super().setUp()
        self.manager = self.tmp / "MWManager"
        self.manager.mkdir()

    def run_preflight(self, game, progress, latest=LATEST, runner=None):
        import mwmanager_journal
        journal = mwmanager_journal.Journal(self.manager / "journal")
        if not journal.has_baseline():
            journal.capture_baseline(game)
        with mock.patch.object(preinstall, "_offer_baseline", return_value=True) as baseline:
            result = preinstall.run_preinstall(game, self.manager, progress,
                                               check_latest=lambda: (latest, None if latest else "offline"),
                                               runner=runner or mock.Mock(side_effect=AssertionError("ran")))
        return result, baseline

    def test_clean_install_passes_without_questions_and_reads_only(self):
        game = make_game(self.tmp / "g")
        before = snapshot(game)
        progress = Progress()
        result, baseline = self.run_preflight(game, progress)
        self.assertTrue(result)
        self.assertEqual(progress.asked, [])
        baseline.assert_called_once()
        self.assertEqual(before, snapshot(game))
        self.assertIn("Preflight: MWSE is the latest build (1dce8de6b87c)", progress.lines)
        self.assertIn("Preflight: Morrowind.ini found (contents are not checked)", progress.lines)

    def test_report_is_only_the_listed_checks(self):
        game = make_game(self.tmp / "g")
        write(game / "Data Files/Some Existing Mod.esp", b"TES3")           # pre-existing playthrough
        report = preinstall.inspect_install(game, check_latest=lambda: (LATEST, None))
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["recommendations"], [])
        self.assertNotIn("unmanaged_plugins", report)

    def test_official_addon_docs_move_without_touching_other_readmes(self):
        game = make_game(self.tmp / "g")
        write(game / "Data Files/README - Area Effect Arrows 1.0.txt", "official")
        write(game / "Data Files/My Mod Readme.txt", "mod")
        import mwmanager_journal as journal
        ledger = journal.Journal(self.manager / "journal")
        ledger.capture_baseline(game)
        lines = preinstall.move_official_addon_docs(game, self.manager)
        destination = game / "Data Files/docs/Official Add-Ons/README - Area Effect Arrows 1.0.txt"
        self.assertTrue(destination.is_file())
        self.assertTrue((game / "Data Files/My Mod Readme.txt").is_file())
        self.assertEqual(len(lines), 1)
        entries, problems = ledger.entries()
        self.assertEqual(problems, [])
        self.assertEqual([(entry["action"], entry.get("tag")) for entry in entries],
                         [("move", "official-add-on-document"), ("create", "official-add-on-document")])
        plan = ledger.plan_rollback(game)
        ledger.execute_rollback(game, plan)
        self.assertTrue((game / "Data Files/README - Area Effect Arrows 1.0.txt").is_file())
        self.assertFalse(destination.exists())

    def test_official_plugin_override_is_journaled_for_rollback(self):
        import core_addons
        import mwmanager_journal as journal
        game = make_game(self.tmp / "g")
        source = write(game / "Data Files/Siege at Firemoth.esp", b"official plugin")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        (self.manager / core_addons.PLUGIN_OVERRIDE_CONFIG).write_text(json.dumps({"rules": [{
            "trigger_master": "TR_Mainland.esm", "replaced_plugin": source.name,
            "expected_original_sha256": digest, "off_path": "Off/Official Add-Ons",
        }]}), encoding="utf-8")
        ledger = journal.Journal(self.manager / "journal")
        ledger.capture_baseline(game)
        core_addons.apply_plugin_replacement_overrides("TR_Mainland.esm", game / "Data Files", self.manager)
        self.assertFalse(source.exists())
        self.assertTrue((self.manager / "Off/Official Add-Ons/Siege at Firemoth.esp").is_file())
        entries, problems = ledger.entries()
        self.assertEqual(problems, [])
        self.assertEqual([(entry["action"], entry.get("tag")) for entry in entries],
                         [("move", "official-add-on-plugin")])
        ledger.execute_rollback(game, ledger.plan_rollback(game))
        self.assertEqual(source.read_bytes(), b"official plugin")

    def test_gog_manifest_mod_files_can_be_quarantined_recoverably(self):
        game = make_game(self.tmp / "g")
        mod = game / "Data Files/A Mod.esp"
        write(mod, b"TES3")
        import mwmanager_journal as journal
        original = {journal.key(journal.rel_of(game, path)): (journal.rel_of(game, path), "")
                    for path in journal.walk_files(game) if path != mod}
        with mock.patch.object(journal, "find_hashdb", return_value=game / "goggame-1.hashdb"), \
                mock.patch.object(journal, "read_hashdb", return_value=original):
            files, complete = preinstall.detected_mod_files(game)
        self.assertEqual((files, complete), ([mod], True))
        root = preinstall.quarantine_mod_files(game, self.manager, files)
        self.assertFalse(mod.exists())
        self.assertTrue((root / "Data Files/A Mod.esp").is_file())
        self.assertIn("Data Files/A Mod.esp", json.loads((root / "manifest.json").read_text())["files"])

    def test_first_run_quarantines_preinstalled_prerequisites_and_restores_unpatched_exe(self):
        import mwmanager_journal as journal
        game = make_game(self.tmp / "g")
        write(game / "Morrowind.Original.exe", b"MZ store")
        store = {"Morrowind.exe", "Morrowind.ini", *(f"Data Files/{name}" for name in CORE)}
        original = {journal.key(rel): (rel, "") for rel in store}
        stopped = mock.patch.object(preinstall, "_prepare_first_run", return_value=False)
        with mock.patch.object(journal, "find_hashdb", return_value=game / "goggame-1.hashdb"), \
                mock.patch.object(journal, "read_hashdb", return_value=original), stopped as prepare:
            self.assertFalse(preinstall.run_preinstall(game, self.manager, Progress(), first_run=True,
                                                       check_latest=lambda: (LATEST, None)))
            prepare.assert_not_called()                           # no choice given: Stop
            self.assertTrue((game / "MGEXEgui.exe").exists())
            progress = Progress(choices=["quarantine"])
            preinstall.run_preinstall(game, self.manager, progress, first_run=True,
                                      check_latest=lambda: (LATEST, None))
        title, choices = progress.asked[0][1], progress.asked[0][3]
        self.assertEqual((title, choices[0][1]), ("Setup needs an unmodded game", "quarantine"))
        prepare.assert_called_once()
        self.assertFalse((game / "MGEXEgui.exe").exists())
        self.assertFalse((game / "Morrowind.Original.exe").exists())
        self.assertEqual((game / "Morrowind.exe").read_bytes(), b"MZ store")
        root = next((self.manager / "quarantine").iterdir())
        self.assertEqual((root / "Morrowind.exe").read_bytes(), b"MZ any store")

    def test_gog_generated_runtime_files_are_not_reported_as_mods(self):
        import mwmanager_journal as journal
        game = make_game(self.tmp / "g")
        generated = [write(game / name, b"generated") for name in
                     ("MWSE-Update.exe.tmp", "Newtonsoft.Json.dll.tmp", "Warnings.txt",
                      "mwedit/customfunctions.dat")]
        mod = write(game / "Data Files/A Mod.esp", b"TES3")
        original = {journal.key(journal.rel_of(game, path)): (journal.rel_of(game, path), "")
                    for path in journal.walk_files(game) if path not in [*generated, mod]}
        with mock.patch.object(journal, "find_hashdb", return_value=game / "goggame-1.hashdb"), \
                mock.patch.object(journal, "read_hashdb", return_value=original):
            files, complete = preinstall.detected_mod_files(game)
        self.assertEqual((files, complete), ([mod], True))

    def test_resumed_first_run_does_not_report_journaled_plugins_as_mods(self):
        import mwmanager_journal as journal
        game = make_game(self.tmp / "g")
        generated = write(game / "Data Files/mwmanager - ESM fixes.esp", b"generated")
        mod = write(game / "Data Files/A Mod.esp", b"TES3")
        ledger = journal.Journal(self.manager / "journal")
        ledger.capture_baseline(game)
        run = ledger.start_run("mwmanager.py")
        ledger.record(run["run"], journal.rel_of(game, generated), "create",
                      hashlib.sha256(generated.read_bytes()).hexdigest(), {"kind": "absent"})
        ledger.finish_run(run)
        original = {journal.key(journal.rel_of(game, path)): (journal.rel_of(game, path), "")
                    for path in journal.walk_files(game) if path not in (generated, mod)}
        with mock.patch.object(journal, "find_hashdb", return_value=game / "goggame-1.hashdb"), \
                mock.patch.object(journal, "read_hashdb", return_value=original):
            files, complete = preinstall.detected_mod_files(game, self.manager)
        self.assertEqual((files, complete), ([mod], True))

    def test_first_run_installs_lua_suite_after_plugin_decision_and_baseline(self):
        game = make_game(self.tmp / "g")
        write(game / "Morrowind Launcher.exe", b"MZ")
        import mwmanager_journal
        mwmanager_journal.Journal(self.manager / "journal").capture_baseline(game)
        order = []
        progress = Progress()
        with mock.patch.object(preinstall, "confirm_first_launch", return_value=True), \
                mock.patch.object(preinstall, "_prepare_first_run",
                                  side_effect=lambda *args: order.append("plugins") or True), \
                mock.patch.object(preinstall, "_offer_baseline",
                                  side_effect=lambda *args: order.append("baseline") or True), \
                mock.patch.object(preinstall, "move_official_addon_docs",
                                  side_effect=lambda *args: order.append("docs") or []), \
                mock.patch.object(preinstall, "_install_mwse_lua_suite",
                                  side_effect=lambda *args: order.append("lua") or True):
            self.assertTrue(preinstall.run_preinstall(
                game, self.manager, progress, first_run=True, check_latest=lambda: (LATEST, None)))
        self.assertEqual(order, ["plugins", "baseline", "docs", "lua"])

    def test_store_recovery_instructions_match_install(self):
        game = self.tmp / "Steam/steamapps/common/Morrowind"
        game.mkdir(parents=True)
        self.assertIn("Steam > Morrowind", preinstall.recovery_text(game))

    def test_missing_exe_ini_and_core_files_block(self):
        game = make_game(self.tmp / "g", ini=False)
        (game / "Morrowind.exe").unlink()
        (game / "Data Files/Tribunal.bsa").unlink()
        report = preinstall.inspect_install(game, check_latest=lambda: (LATEST, None))
        text = "\n".join(report["blockers"])
        for needle in ("Morrowind.exe", "Morrowind.ini", "Tribunal.bsa"):
            self.assertIn(needle, text)

    def test_modified_master_warns_and_can_continue(self):
        game = make_game(self.tmp / "g")
        write(game / "Data Files/Bloodmoon.esm", b"edited")
        progress = Progress(yes_no=[True])
        result, _ = self.run_preflight(game, progress)
        self.assertTrue(result)
        self.assertIn("Bloodmoon.esm", progress.asked[0][2])
        stopped = Progress(yes_no=[False])
        self.assertFalse(self.run_preflight(game, stopped)[0])

    def test_missing_skunk_works_offers_its_page(self):
        game = make_game(self.tmp / "g", mcp_first="Game mechanics")
        progress = Progress(yes_no=[False], choices=["mcp_beta"])
        with mock.patch.object(mp, "open_download_page", return_value="embedded") as opened:
            result, baseline = self.run_preflight(game, progress)
        self.assertFalse(result)
        baseline.assert_not_called()
        download_dir = opened.call_args.args[1]
        self.assertEqual(download_dir.parent, self.manager / "pre-reqs" / "mcp_beta")
        self.assertTrue(download_dir.name.startswith("download-"))
        self.assertIn('"Game mechanics", not "Beta"', progress.asked[0][2])
        self.assertIn("mods/26348", progress.asked[0][2])

    def test_prerequisite_archive_is_kept_safely_and_installed(self):
        game = self.tmp / "game"
        game.mkdir()
        downloads = self.tmp / "downloads"
        downloads.mkdir()
        archive = downloads / "MWSE-45468.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("MWSE/MWSE.dll", b"dll")
            package.writestr("MWSE/MWSE-Update.exe", b"updater")
            package.writestr("MWSE/Data Files/MWSE/core/initialize.lua", b"-- core")
        progress = Progress()
        self.assertTrue(preinstall.install_prerequisite_archive(
            "mwse", archive, game, self.manager, progress,
            runner=mock.Mock(side_effect=AssertionError("portable archive should not launch an installer"))))
        self.assertEqual((game / "MWSE.dll").read_bytes(), b"dll")
        self.assertTrue((game / "Data Files/MWSE/core/initialize.lua").is_file())
        self.assertTrue((self.manager / "pre-reqs/mwse/MWSE-45468.zip").is_file())

    def test_archive_detection_separates_mcp_base_and_beta(self):
        downloads = self.tmp / "downloads"
        downloads.mkdir()
        base = write(downloads / "Morrowind Code Patch-19510.zip", b"base")
        beta = write(downloads / "MCP Beta Skunk Works-26348.7z", b"beta")
        self.assertEqual(mp.prerequisite_archives("mcp", downloads), [base])
        self.assertEqual(mp.prerequisite_archives("mcp_beta", downloads), [beta])

    def test_archive_detection_does_not_treat_mwse_mods_as_the_runtime(self):
        downloads = self.tmp / "downloads"
        downloads.mkdir()
        runtime = write(downloads / "MWSE-Update for Nightly Builds-45468.zip", b"runtime")
        write(downloads / "MWSE Logger-51423.7z", b"mod")
        write(downloads / "Graphic Herbalism MWSE-46599.7z", b"mod")
        self.assertEqual(mp.prerequisite_archives("mwse", downloads), [runtime])

    def test_mge_archive_detection_rejects_hrnchamd_nexus_download(self):
        downloads = self.tmp / "downloads"
        downloads.mkdir()
        write(downloads / "MGE XE-41102-0-15-0.7z", b"wrong fork")
        g7 = write(downloads / "MGE-XE G7 Fork-59957.7z", b"g7")
        self.assertEqual(mp.prerequisite_archives("mge_xe", downloads), [g7])

    def test_mcp_and_mge_launch_prompts_include_requested_guidance(self):
        game = self.tmp / "game"
        write(game / "Morrowind Code Patch.exe", b"MZ")
        write(game / "MGEXEgui.exe", b"MZ")
        progress = Progress(yes_no=[True, True])
        runner = mock.Mock(return_value=types.SimpleNamespace(returncode=0))
        self.assertTrue(preinstall._post_prerequisite("mcp_beta", game, progress, LATEST, runner=runner))
        self.assertTrue(preinstall._post_prerequisite("mge_xe", game, progress, LATEST, runner=runner))
        mcp_text, mge_text = progress.asked[0][2], progress.asked[1][2]
        for text in ("Beta", "Game mechanics", "Over the shoulder 3rd person camera",
                     "NPC AI casts zero cost powers", "Don't loot on dispose", "Level-up skills tooltip",
                     "International", "Bugs", "Apply chosen patches"):
            self.assertIn(text, mcp_text)
        for text in ("VSync off", "Enable shaders", "Very High", "Depth of Field", "frame limiter to 300",
                     "Pause rendering in menus", "Crosshair autohide", "Allow yes to all", "Allow screenshots",
                     "Constant 0.382", "Linear 0", "Quadratic 2.619"):
            self.assertIn(text, mge_text)
        self.assertEqual([Path(call.args[0][0]).name for call in runner.call_args_list],
                         ["Morrowind Code Patch.exe", "MGEXEgui.exe"])

    def test_gui_launch_keeps_guidance_visible_and_waits_for_each_program(self):
        game = self.tmp / "game"
        write(game / "Morrowind Code Patch.exe", b"MZ")
        write(game / "MGEXEgui.exe", b"MZ")
        progress = Progress(yes_no=[True, True])
        launched = []

        def run_with_reference(path, title, guidance):
            launched.append((Path(path).name, title, guidance))
            return 0, None

        progress.run_program_with_reference = run_with_reference
        runner = mock.Mock(side_effect=AssertionError("GUI launch path should own the process"))
        self.assertTrue(preinstall._post_prerequisite("mcp_beta", game, progress, LATEST, runner=runner))
        self.assertTrue(preinstall._post_prerequisite("mge_xe", game, progress, LATEST, runner=runner))
        self.assertEqual([item[0] for item in launched], ["Morrowind Code Patch.exe", "MGEXEgui.exe"])
        self.assertIn("Apply chosen patches", launched[0][2])
        self.assertIn("frame limiter to 300", launched[1][2])
        self.assertIn("Quadratic 2.619", launched[1][2])

    def test_nothing_installed_lists_pages_in_install_order(self):
        game = self.tmp / "bare"
        write(game / "Morrowind.exe", b"MZ")
        write(game / "Morrowind.ini", "")
        for name, data in CORE.items():
            write(game / "Data Files" / name, data)
        progress = Progress(choices=["none"])
        with mock.patch.object(mp, "open_download_page") as opened:
            self.assertFalse(self.run_preflight(game, progress)[0])
        opened.assert_not_called()
        message = progress.asked[0][2]
        order = [message.index(p) for p in
                 ("mods/45468", "mods/19510", "mods/26348", "mods/59957")]
        self.assertEqual(order, sorted(order))

    def test_missing_mge_offers_g7_first(self):
        game = make_game(self.tmp / "g", mge=None)
        progress = Progress(yes_no=[False], choices=["mge_g7"])
        with mock.patch.object(mp, "open_download_page", return_value="system") as opened:
            self.assertFalse(self.run_preflight(game, progress)[0])
        download_dir = opened.call_args.args[1]
        self.assertEqual(download_dir.parent, self.manager / "pre-reqs" / "mge_xe")
        self.assertTrue(download_dir.name.startswith("download-"))
        self.assertEqual(progress.asked[0][3][0][1], "mge_g7")
        self.assertNotIn("mge_xe", [value for _label, value in progress.asked[0][3]])

    def test_manager_cache_suppresses_mod_manager_archive_choice(self):
        game = make_game(self.tmp / "g", mge=None)
        cached = self.manager / "pre-reqs" / "mge_xe" / "MGE XE cached.7z"
        vortex = self.tmp / "vortex" / "MGE XE vortex.7z"
        progress = Progress(choices=["none"])
        report = {"missing_keys": ["mge_xe"], "blockers": ["MGE XE is missing"],
                  "game_dir": str(game)}

        def archives(_key, root):
            return [cached] if Path(root) == self.manager / "pre-reqs" else [vortex]

        with mock.patch.object(mp, "prerequisite_archives", side_effect=archives), \
                mock.patch.object(mp, "cached_archive_status", return_value=("current", "1.0")):
            self.assertFalse(preinstall._offer_missing(report, progress, game, self.manager,
                                                       archive_dir=vortex.parent))
        choices = progress.asked[0][3]
        self.assertEqual(choices[0][1], "cache")
        self.assertIn("MWManager cached", choices[0][0])
        self.assertNotIn("archive", [value for _label, value in choices])

    def test_newer_nexus_file_suppresses_stale_cache_and_vortex(self):
        game = make_game(self.tmp / "g", mge=None)
        cached = self.manager / "pre-reqs" / "mge_xe" / "MGE XE old.7z"
        vortex = self.tmp / "vortex" / "MGE XE vortex.7z"
        progress = Progress(choices=["none"])
        report = {"missing_keys": ["mge_xe"], "blockers": ["MGE XE is missing"],
                  "game_dir": str(game)}

        def archives(_key, root):
            return [cached] if Path(root) == self.manager / "pre-reqs" else [vortex]

        with mock.patch.object(mp, "prerequisite_archives", side_effect=archives), \
                mock.patch.object(mp, "cached_archive_status", return_value=("outdated", "0.16")):
            self.assertFalse(preinstall._offer_missing(report, progress, game, self.manager,
                                                       archive_dir=vortex.parent))
        values = [value for _label, value in progress.asked[0][3]]
        self.assertNotIn("cache", values)
        self.assertNotIn("archive", values)
        self.assertIn("newer MGE XE, Greatness7 build (0.16)", progress.lines[0])

    def test_missing_prerequisite_can_be_installed_and_rechecked_in_place(self):
        game = make_game(self.tmp / "g", mcp_first="Game mechanics")

        class InstallingProgress(Progress):
            def ask_yes_no(inner, title, message):
                inner.asked.append(("yes_no", title, message))
                write(game / "mcpatch/describe.json", json.dumps([{"category": "Beta"}]))
                return True

        progress = InstallingProgress(choices=["mcp_beta"])
        runner = mock.Mock(return_value=types.SimpleNamespace(returncode=0))
        with mock.patch.object(mp, "open_download_page", return_value="system"), \
                mock.patch.object(preinstall, "confirm_first_launch", return_value=True):
            result, _ = self.run_preflight(game, progress, runner=runner)
        self.assertTrue(result)
        self.assertEqual(Path(runner.call_args.args[0][0]).name, "Morrowind Code Patch.exe")
        self.assertIn("Preflight: re-checking after prerequisite installation…", progress.lines)

    def test_non_g7_mge_is_a_recommendation_not_a_block(self):
        game = make_game(self.tmp / "g", mge="other")
        progress = Progress(choices=["continue"])
        result, _ = self.run_preflight(game, progress)
        self.assertTrue(result)
        self.assertIn("run MGEXEgui.exe once", progress.asked[0][2])

    def test_outdated_mwse_offers_updater_only_on_yes(self):
        game = make_game(self.tmp / "g", mwse_build="a" * 40)
        declined = Progress(yes_no=[False])
        result, _ = self.run_preflight(game, declined)                 # runner would raise if started
        self.assertTrue(result)
        self.assertIn("aaaaaaaaaaaa", declined.asked[0][2])

        def updater(cmd, cwd, check):
            self.assertEqual(Path(cmd[0]).name, "MWSE-Update.exe")
            write(Path(cwd) / "mwse-version.txt", LATEST)
            return types.SimpleNamespace(returncode=0)
        accepted = Progress(yes_no=[True])
        runner = mock.Mock(side_effect=updater)
        result, _ = self.run_preflight(game, accepted, runner=runner)
        self.assertTrue(result)
        runner.assert_called_once()
        self.assertIn("Preflight: MWSE is now the latest build (1dce8de6b87c).", accepted.lines)

    def test_never_updated_mwse_is_offered_the_updater(self):
        game = make_game(self.tmp / "g", mwse_build=None)
        progress = Progress(yes_no=[False])
        self.assertTrue(self.run_preflight(game, progress)[0])
        self.assertIn("no mwse-version.txt", progress.asked[0][2])

    def test_offline_mwse_is_unknown_and_does_not_ask(self):
        game = make_game(self.tmp / "g", mwse_build="a" * 40)
        progress = Progress()
        self.assertTrue(self.run_preflight(game, progress, latest=None)[0])
        self.assertEqual(progress.asked, [])
        self.assertTrue(any("latest build unknown" in line for line in progress.lines))

    def test_missing_mwse_dll_blocks(self):
        game = make_game(self.tmp / "g")
        (game / "MWSE.dll").unlink()
        progress = Progress(choices=["none"])
        self.assertFalse(self.run_preflight(game, progress)[0])
        self.assertIn("MWSE.dll was not found", progress.asked[0][2])


if __name__ == "__main__":
    unittest.main()
