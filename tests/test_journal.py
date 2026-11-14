import contextlib
import hashlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_journal as mj  # noqa: E402
import mwmanager_prereqs as mp  # noqa: E402


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_hashdb(path, entries):
    blob = b"\0" * 12
    for name, data in entries.items():
        blob += name.encode().ljust(1024, b"\0") + hashlib.md5(data).hexdigest().encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("goggame-1.hashdb", blob)


SHIPPED = {
    "Morrowind.exe": b"vanilla exe",
    "Morrowind.ini": b"[General]\r\nvanilla=1\r\n",
    "Data Files\\Morrowind.esm": b"esm",
    "Data Files\\Siege at Firemoth.esp": b"siege",
    "Data Files\\Textures\\a.dds": b"original texture",
    "Data Files\\Docs\\readme.txt": b"doc",
}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.game = self.tmp / "Morrowind"
        self.manager = self.tmp / "MWManager"
        self.manager.mkdir()
        for rel, data in SHIPPED.items():
            write(self.game / rel.replace("\\", "/"), data)
        make_hashdb(self.game / "goggame-1.hashdb", SHIPPED)
        write(self.game / "MWSE.dll", b"mwse")
        write(self.game / "Data Files/MWSE/core/initialize.lua", b"-- core\n")
        write(self.game / "mcpatch/describe.json", b"[]")
        write(self.game / "Saves/one.ess", b"save")
        # a pre-existing playthrough: these are part of the user's starting state
        write(self.game / "Data Files/Existing Mod.esp", b"older mod")
        write(self.game / "Data Files/MWSE/mods/old/main.lua", b"-- old mod\n")
        known = {name: (hashlib.sha256(data).hexdigest(),) for name, data in
                 (("Morrowind.esm", b"esm"),)}
        known.update({n: ("0" * 64,) for n in ("Tribunal.esm", "Bloodmoon.esm", "Morrowind.bsa", "Tribunal.bsa",
                                                 "Bloodmoon.bsa")})
        hashes = mock.patch.object(mp, "CORE_FILE_HASHES", known)
        hashes.start()
        self.addCleanup(hashes.stop)
        patcher = mock.patch.object(mj, "MANAGER_DIR", self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.journal = mj.Journal(self.manager / "journal")


class BaselineTests(Fixture):
    def test_hashdb_and_layers(self):
        self.assertEqual(len(mj.read_hashdb(self.game / "goggame-1.hashdb")), len(SHIPPED))
        record = self.journal.capture_baseline(self.game)
        layers = {rel: info["layer"] for rel, info in record["files"].items()}
        self.assertEqual(layers["Data Files/Textures/a.dds"], "start")
        self.assertTrue(record["files"]["Data Files/Textures/a.dds"]["store_match"])
        self.assertEqual(layers["Morrowind.exe"], "game-exe")
        self.assertEqual(layers["Data Files/Existing Mod.esp"], "start")
        self.assertEqual(layers["Data Files/MWSE/mods/old/main.lua"], "start")
        self.assertEqual(layers["MWSE.dll"], "prereq:mwse")
        self.assertEqual(layers["Data Files/MWSE/core/initialize.lua"], "prereq:mwse")
        self.assertEqual(layers["mcpatch/describe.json"], "prereq:mcp")
        self.assertEqual(layers["Saves/one.ess"], "user-data")
        self.assertEqual(layers["goggame-1.hashdb"], "store-meta")
        self.assertTrue(record["files"]["Data Files/Morrowind.esm"]["core_ok"])
        self.assertEqual(len(record["warnings"]), 5)                # 2 masters + 3 BSAs absent in the fixture
        self.assertTrue(all(w.endswith("is missing.") for w in record["warnings"]))
        self.assertTrue(self.journal.baseline_ini.is_file())
        with self.assertRaises(FileExistsError):
            self.journal.capture_baseline(self.game)

    def test_only_core_masters_are_validated(self):
        write(self.game / "Data Files/Textures/a.dds", b"already changed by an old mod")
        write(self.game / "Morrowind.exe", b"code-patched exe, any variation")
        write(self.game / "Data Files/Morrowind.esm", b"edited master")
        (self.game / "goggame-1.hashdb").unlink()                   # Steam/Epic: no store list at all
        record = self.journal.capture_baseline(self.game)
        self.assertEqual(record["files"]["Data Files/Textures/a.dds"]["layer"], "start")
        self.assertIsNone(record["files"]["Data Files/Textures/a.dds"]["store_match"])
        self.assertFalse(record["files"]["Data Files/Morrowind.esm"]["core_ok"])
        self.assertTrue(any(w.startswith("Data Files/Morrowind.esm has been modified") for w in record["warnings"]))
        self.assertFalse(any("a.dds" in w or "Morrowind.exe" in w for w in record["warnings"]))
        self.assertTrue(any("Tribunal.esm is missing" in w for w in record["warnings"]))


class LedgerTests(Fixture):
    def test_chain_tamper_and_torn_line(self):
        for n in range(3):
            self.journal.record("r", f"Data Files/n{n}.esp", "create", "0" * 64, {"kind": "absent"})
        entries, problems = self.journal.entries()
        self.assertEqual([e["seq"] for e in entries], [1, 2, 3])
        self.assertEqual(problems, [])
        lines = self.journal.ledger.read_text().splitlines()
        self.journal.ledger.write_text("\n".join(lines) + "\n{\"torn")
        _entries, problems = self.journal.entries()
        self.assertEqual(len(problems), 1)
        self.assertIn("incomplete", problems[0])
        lines[1] = lines[1].replace("n1.esp", "evil.esp")
        self.journal.ledger.write_text("\n".join(lines) + "\n")
        _entries, problems = self.journal.entries()
        self.assertTrue(any("chain broken" in p for p in problems))

    def test_first_original_wins(self):
        first = {"kind": "off", "location": "game:Data Files/Off/a.dds", "sha256": "1" * 64}
        self.journal.record("r1", "Data Files/Textures/a.dds", "replace", "2" * 64, first)
        later = self.journal.record("r2", "Data Files\\Textures\\A.dds", "replace", "3" * 64,
                                    {"kind": "off", "location": "game:x", "sha256": "9" * 64})
        self.assertIsNone(later["original"])
        self.assertEqual(self.journal.first_original("data files/textures/a.dds")["sha256"], "1" * 64)


class RoundTripTests(Fixture):
    def deploy(self, answers):
        j, g = self.journal, self.game
        run = j.start_run("mwmanager.py", {"fomod": answers})
        # replace a shipped texture; its original goes to Data Files\Off
        tex, off = g / "Data Files/Textures/a.dds", g / "Data Files/Off/Textures/a.dds"
        off.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tex, off)
        write(tex, b"better texture " + answers["variant"].encode())
        j.record(run["run"], "Data Files/Textures/a.dds", "replace", sha(tex),
                 {"kind": "off", "location": "game:Data Files/Off/Textures/a.dds", "sha256": sha(off)})
        # disable a shipped plugin into MWManager\disabled_plugins
        esp, kept = g / "Data Files/Siege at Firemoth.esp", self.manager / "disabled_plugins/Siege at Firemoth.esp"
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(esp, kept)
        j.record(run["run"], "Data Files/Siege at Firemoth.esp", "disable", None,
                 {"kind": "disabled", "location": "manager:disabled_plugins/Siege at Firemoth.esp",
                  "sha256": sha(kept)})
        # patch MWSE core with its original kept in Data Files\MWSE\backups
        core, backup = g / "Data Files/MWSE/core/initialize.lua", g / "Data Files/MWSE/backups/core/initialize.lua"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(core, backup)
        write(core, b"-- core, fixed\n")
        j.record(run["run"], "Data Files/MWSE/core/initialize.lua", "modify", sha(core),
                 {"kind": "mwse-backups", "location": "game:Data Files/MWSE/backups/core/initialize.lua",
                  "sha256": sha(backup)})
        # a new file
        new = write(g / "Data Files/New Mod.esp", b"new plugin")
        j.record(run["run"], "Data Files/New Mod.esp", "create", sha(new), {"kind": "absent"})
        write(g / "Morrowind.ini", b"[General]\r\nvanilla=0\r\n[Game Files]\r\nGameFile1=New Mod.esp\r\n")
        j.record(run["run"], "Morrowind.ini", "modify", sha(g / "Morrowind.ini"),
                 {"kind": "baseline", "location": "journal:baseline/Morrowind.ini", "sha256": sha(j.baseline_ini)})
        j.finish_run(run)
        return {rel: sha(g / rel) for rel in ("Data Files/Textures/a.dds", "Data Files/MWSE/core/initialize.lua",
                                               "Data Files/New Mod.esp")}

    def test_deploy_rollback_redeploy(self):
        j = self.journal
        j.capture_baseline(self.game)
        first = self.deploy({"variant": "4k"})
        self.assertTrue(j.verify(self.game, against="deployed")["ok"], j.verify(self.game, against="deployed"))
        before = j.verify(self.game)
        self.assertFalse(before["ok"])
        self.assertEqual(before["unexplained"], [], before)
        write(self.game / "Data Files/sneaky.esp", b"x")
        self.assertIn("Data Files/sneaky.esp", j.verify(self.game)["unexplained"])
        os.remove(self.game / "Data Files/sneaky.esp")

        plan = j.plan_rollback(self.game)
        self.assertEqual(plan["refuse"], [])
        ops = {(s["op"], s["path"]) for s in plan["steps"]}
        self.assertIn(("delete", "Data Files/New Mod.esp"), ops)
        self.assertIn(("restore", "Data Files/Siege at Firemoth.esp"), ops)
        self.assertIn(("restore", "Morrowind.ini"), ops)
        self.assertEqual(sorted(plan["cleanup_after_verify"]),
                         ["game:Data Files/MWSE/backups/core/initialize.lua", "game:Data Files/Off/Textures/a.dds"])
        before_files = {p: sha(p) for p in self.game.rglob("*") if p.is_file()}
        self.assertEqual(before_files, {p: sha(p) for p in self.game.rglob("*") if p.is_file()})  # plan is read-only

        j.execute_rollback(self.game, plan)
        self.assertEqual(j.cleanup_used_backups(self.game, plan), plan["cleanup_after_verify"])  # dry run
        self.assertTrue((self.game / "Data Files/Off/Textures/a.dds").exists())
        j.cleanup_used_backups(self.game, plan, apply=True)
        self.assertFalse((self.game / "Data Files/Off").exists())
        self.assertFalse((self.game / "Data Files/MWSE/backups").exists())
        result = j.verify(self.game, full_hash=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue((self.game / "MWSE.dll").exists())          # prerequisites are kept
        self.assertTrue((self.game / "Saves/one.ess").exists())
        self.assertEqual((self.game / "Data Files/Existing Mod.esp").read_bytes(), b"older mod")  # starting state
        self.assertTrue((self.game / "Data Files/MWSE/mods/old/main.lua").exists())
        j.archive_ledger()

        runs, problems = j._read_jsonl(j.runs)
        self.assertEqual(problems, [])
        answers = json.loads((j.root / runs[0]["inputs"]["fomod"]).read_text())
        self.assertEqual(self.deploy(answers), first)
        self.assertTrue(j.verify(self.game, against="deployed", full_hash=True)["ok"])

    def test_rollback_refuses_when_an_original_is_gone(self):
        self.journal.capture_baseline(self.game)
        self.deploy({"variant": "x"})
        os.remove(self.manager / "disabled_plugins/Siege at Firemoth.esp")
        plan = self.journal.plan_rollback(self.game)
        self.assertTrue(any("Siege at Firemoth" in r for r in plan["refuse"]))
        with self.assertRaises(RuntimeError):
            self.journal.execute_rollback(self.game, plan)
        self.assertFalse(self.journal.verify(self.game)["ok"])


class ModAndCliTests(Fixture):
    def test_stage_markers_and_archive_checks(self):
        archive = write(self.tmp / "downloads/Sound_Data-58854-2-0-1700000000.7z", b"archive bytes")
        stage = self.manager / "staging/Sound_Data"
        write(stage / ".mwmanager-stage.json", json.dumps({"archive": str(archive), "sha256": sha(archive),
                                                           "selected_masters": []}))
        (mod_key,) = self.journal.import_stage_markers(self.manager / "staging")
        self.assertEqual(self.journal.mods()[mod_key]["nexus_mod_id"], "58854")
        self.assertEqual(self.journal.plan_mod_rollback(mod_key)["status"], "ready")
        write(archive, b"different")
        self.assertEqual(self.journal.plan_mod_rollback(mod_key)["status"], "archive-changed")
        archive.unlink()
        plan = self.journal.plan_mod_rollback(mod_key)
        self.assertEqual(plan["status"], "archive-missing")
        self.assertEqual(plan["download_page"], "https://www.nexusmods.com/morrowind/mods/58854?tab=files")
        opened = []
        fake = types.SimpleNamespace(open_page=opened.append)
        with mock.patch.dict(sys.modules, {"mwnexus_browser": fake}), contextlib.redirect_stdout(io.StringIO()):
            code = mj.main(["--game-dir", str(self.game), "--journal-dir", str(self.journal.root),
                            "--mod-rollback", mod_key, "--dry-run", "--open-download"])
        self.assertEqual((code, opened), (1, [plan["download_page"]]))

    def test_cli_is_dry_run_only(self):
        args = ["--game-dir", str(self.game), "--journal-dir", str(self.journal.root)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mj.main(args + ["--verify"]), 2)
            self.assertEqual(mj.main(args + ["--capture-baseline"]), 0)
            self.assertEqual(mj.main(args + ["--capture-baseline"]), 2)
            self.assertEqual(mj.main(args + ["--verify"]), 0)
            write(self.game / "Data Files/New.esp", b"x")
            self.assertEqual(mj.main(args + ["--rollback"]), 2)
            self.assertEqual(mj.main(args + ["--rollback", "--dry-run"]), 0)
        self.assertTrue((self.game / "Data Files/New.esp").exists())
        self.assertIn("Not explained by the journal", out.getvalue())
        self.assertTrue(all(len(line) <= mj.WIDTH for line in out.getvalue().splitlines()))


class PrereqTests(unittest.TestCase):
    def setUp(self):
        self.game = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.game, ignore_errors=True)

    def test_nothing_installed(self):
        found = mp.detect(self.game)
        self.assertEqual(mp.missing_keys(found), ["mcp", "mcp_beta", "mge_xe", "mwse"])
        self.assertTrue(all("NOT detected" in line for line in mp.summary_lines(found)))

    def test_detects_with_versions(self):
        fixed = b"\xbd\x04\xef\xfe" + struct.pack("<IIIII", 0x10000, (0 << 16) | 18, (0 << 16) | 0, 0, 0)
        write(self.game / "MGEXEgui.exe", b"MZ" + b"\0" * 64 + fixed + b"\0" * 40)
        write(self.game / "d3d8.dll", b"MZ")
        write(self.game / "MWSE.dll", b"MZ")
        write(self.game / "Data Files/MWSE/core/initialize.lua", b"")
        write(self.game / "mwse-version.txt", "a" * 40)
        write(self.game / "mcpatch/describe.json", json.dumps([{"category": "Beta", "patches": [
            {"id": i} for i in range(180, 196)]}]))
        write(self.game / "MCP Readme.txt", "Morrowind Code Patch version 2.4\r\n")
        found = mp.detect(self.game)
        self.assertEqual(mp.missing_keys(found), [])
        self.assertEqual(found["mge_xe"]["version"], "0.18.0.0")
        self.assertEqual(found["mwse"]["build"], "a" * 40)
        self.assertEqual(found["mcp"]["version"], "2.4")
        self.assertTrue(found["mcp"]["skunk_works_beta"])

    def test_pages_are_official_and_never_downloaded(self):
        for key, page in mp.PAGES.items():
            official = ("https://github.com/Greatness7/MGE-XE/releases" if key == "mge_g7"
                        else "https://www.nexusmods.com/morrowind/mods/")
            self.assertTrue(page["page"].startswith(official), key)
        opened = []
        fake = types.SimpleNamespace(open_page=opened.append)
        with mock.patch.dict(sys.modules, {"mwnexus_browser": fake}):
            self.assertEqual(mp.open_download_page("mge_xe"), "embedded")
        self.assertEqual(opened, [mp.PAGES["mge_xe"]["page"]])

        def broken(_url):
            raise RuntimeError("no webview")
        with mock.patch.dict(sys.modules, {"mwnexus_browser": types.SimpleNamespace(open_page=broken)}), \
                mock.patch("webbrowser.open") as system:
            self.assertEqual(mp.open_download_page("mwse"), "system")
        system.assert_called_once_with(mp.PAGES["mwse"]["page"])


if __name__ == "__main__":
    unittest.main()


class FakeProgress:
    def __init__(self, answers):
        self.answers, self.asked, self.lines = list(answers), [], []

    def __call__(self, line):
        self.lines.append(line)

    def ask_yes_no(self, title, message):
        self.asked.append((title, message))
        return self.answers.pop(0)


class PreflightTests(Fixture):
    def test_missing_prerequisites_open_first_official_page_only(self):
        import preinstall
        report = {"blockers": ["The Morrowind Code Patch was not found", "MGE XE was not found",
                               "MWSE.dll was not found"],
                  "missing_keys": ["mcp", "mcp_beta", "mge_xe", "mwse"], "hash_mismatches": [],
                  "recommendations": [], "status": [], "mwse": {}}
        progress = FakeProgress([True])
        with mock.patch.object(preinstall, "inspect_install", return_value=report), \
                mock.patch.object(mp, "open_download_page", return_value="embedded") as opened:
            self.assertFalse(preinstall.run_preinstall(self.game, self.manager, progress))
        opened.assert_called_once_with("mcp")
        message = progress.asked[0][1]
        for page in ("mods/19510", "mods/26348", "mods/41102", "mods/45468"):
            self.assertIn(page, message)
        self.assertLess(message.index("mods/19510"), message.index("mods/41102"))
        self.assertFalse(self.journal.has_baseline())

    def test_baseline_offered_once_and_required(self):
        import preinstall
        self.assertFalse(preinstall._offer_baseline(self.game, self.manager, FakeProgress([False])))
        self.assertFalse(self.journal.has_baseline())
        before = {p: sha(p) for p in self.game.rglob("*") if p.is_file()}
        self.assertTrue(preinstall._offer_baseline(self.game, self.manager, FakeProgress([True])))
        self.assertTrue(self.journal.has_baseline())
        self.assertEqual(before, {p: sha(p) for p in self.game.rglob("*") if p.is_file()})  # read-only
        quiet = FakeProgress([])
        self.assertTrue(preinstall._offer_baseline(self.game, self.manager, quiet))
        self.assertEqual(quiet.asked, [])

    def test_core_masters_checked_against_known_hashes(self):
        import preinstall
        report = preinstall.inspect_install(self.game, self.manager, check_latest=None)
        self.assertIn("SHA-256 matches the original: Morrowind.esm", report["status"])
        write(self.game / "Data Files/Morrowind.esm", b"edited")
        report = preinstall.inspect_install(self.game, self.manager, check_latest=None)
        self.assertEqual([m[0] for m in report["hash_mismatches"]], ["Morrowind.esm"])


class ManagerCliTests(Fixture):
    def test_mwmanager_flags_dispatch_to_the_journal(self):
        import mwmanager
        data = str(self.game / "Data Files")
        out = io.StringIO()
        with mock.patch.object(mj, "DEFAULT_ROOT", self.journal.root), contextlib.redirect_stdout(out):
            self.assertEqual(mwmanager._main([data, "--verify"]), 2)
            self.assertEqual(mwmanager._main([data, "--capture-baseline"]), 0)
            self.assertEqual(mwmanager._main([data, "--rollback"]), 2)
            self.assertEqual(mwmanager._main([data, "--rollback", "--dry-run"]), 0)
            self.assertEqual(mwmanager._main([data, "--verify"]), 0)
        self.assertTrue(self.journal.has_baseline())
        self.assertIn("Only --rollback --dry-run", out.getvalue())
