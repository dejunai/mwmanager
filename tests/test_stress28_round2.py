"""CR-28 stress run, round 2: startup status and routine confirmations kept out of the job window."""
import ast
import unittest
from pathlib import Path

import mwmanager_gui

ROOT = Path(__file__).resolve().parents[1]


class StartupStatus(unittest.TestCase):
    def test_ready_when_play_is_available(self):
        self.assertEqual(mwmanager_gui.startup_ready_status({"init_complete": 1, "first_run": 0}, True),
                         "Ready to play.")

    def test_says_what_to_do_when_patches_are_stale(self):
        text = mwmanager_gui.startup_ready_status({"init_complete": 1, "first_run": 0}, False)
        self.assertIn("Generate Patches", text)
        self.assertNotIn("reconciling", text)

    def test_says_resume_setup_when_setup_is_unfinished(self):
        text = mwmanager_gui.startup_ready_status({"init_complete": 0, "first_run": 1}, False)
        self.assertIn("Resume setup", text)

    def test_startup_replaces_the_loading_line(self):
        source = (ROOT / "mwmanager_gui.py").read_text(encoding="utf-8")
        loading = source.index('self.status.set("MWManager loaded; reconciling the current game state')
        tail = source[loading:loading + 1500]
        self.assertIn("startup_ready_status(", tail)


def progress_texts(path):
    """(text, has_detail_prefix) for every progress("...") call whose first argument is a literal or f-string."""
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("progress", "_status", "_detail")
                and node.args):
            continue
        arg = node.args[0] if node.func.id == "progress" else (node.args[1:2] or [None])[0]
        parts = arg.values if isinstance(arg, ast.JoinedStr) else [arg]
        text, prefixed = "", node.func.id == "_detail"
        for i, part in enumerate(parts):
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                text += part.value
            elif isinstance(part, ast.FormattedValue) and i == 0 and getattr(part.value, "id", "") == "DETAIL_PREFIX":
                prefixed = True
            else:
                text += "{}"
        out.append((text, prefixed))
    return out


class RoutineConfirmationsAreDetailOnly(unittest.TestCase):
    ROUTINE = ("Preflight passed.", "Starting state recorded:", "Prerequisite checkpoint recorded:",
               "Preflight: first launch skipped by the user", "Preflight: using the mlox rules already")

    def test_routine_lines_carry_the_detail_prefix(self):
        found = {}
        for name in ("preinstall.py", "mwmanager_mlox.py"):
            for text, prefixed in progress_texts(ROOT / name):
                for routine in self.ROUTINE:
                    if text.startswith(routine):
                        found[routine] = prefixed
        self.assertEqual(set(found), set(self.ROUTINE))
        self.assertTrue(all(found.values()), found)

    def test_the_long_wait_is_still_announced(self):
        texts = [text for text, prefixed in progress_texts(ROOT / "preinstall.py") if not prefixed]
        self.assertIn("Recording the starting state of the install (reading every file)\u2026", texts)


class LeftOverStagingFolders(unittest.TestCase):
    def test_one_folder_gets_the_plain_choices(self):
        values = [value for _, value in mwmanager_gui.staged_folder_choices(0)]
        self.assertEqual(values, ["reprocess", "ready", "cancel"])

    def test_several_folders_can_be_answered_once(self):
        choices = mwmanager_gui.staged_folder_choices(15)
        self.assertIn("reprocess_all", [value for _, value in choices])
        self.assertIn("15 other left-over folders", dict((v, k) for k, v in choices)["reprocess_all"])
        self.assertIn("1 other left-over folder", dict((v, k) for k, v in mwmanager_gui.staged_folder_choices(1))
                      ["reprocess_all"])
        self.assertNotIn("folders", dict((v, k) for k, v in mwmanager_gui.staged_folder_choices(1))["reprocess_all"])

    def test_the_answer_is_remembered_for_the_batch(self):
        source = (ROOT / "mwmanager_gui.py").read_text(encoding="utf-8")
        start = source.index('"Staged folder already exists",')
        block = source[start - 1200:start + 600]
        self.assertIn("leftover_answer[0] if leftover_answer else None", block)
        self.assertIn('leftover_answer.append("reprocess")', block)

class CleanAssetScan(unittest.TestCase):
    def test_clean_scan_is_detail_only_and_a_scan_with_repairs_is_shown(self):
        from mwmanager_workflow import UserOutput
        out = UserOutput()
        self.assertEqual(out.feed("Tidy and options for A.zip: Asset scan complete: 0 files checked; "
                                  "0 in need of repairs"), [])
        self.assertEqual(out.feed("Asset scan complete: 1,204 files checked; 0 in need of repairs"), [])
        line = "Tidy A Mod: Asset scan complete: 61 files checked; 3 in need of repairs"
        self.assertEqual(out.feed(line), [line])

DEEP_PATH_RUN = r"""MWMANAGER-PROGRESS: Applying the reviewed tidy plan
MWMANAGER-PROGRESS: Planning Tidy rules for Zz Deep Path-99007
vortex_tidy plan, 2026-11-26 09:15: 1 mod(s), 0 to change, 0 move(s); 0 left alone
0 planned move(s); each is listed in G:\m\outputs\mwmanager_vortex_tidy_plan.txt
======================================================================
ASSET CHECK (each mod's own files)
======================================================================
MWMANAGER-PROGRESS: Asset scan complete: 1 files checked; 1 in need of repairs
MWMANAGER-PLAN: pending G:\m\outputs\plan.txt
2 asset issue(s), 2 repairable; 1 broken file(s) to move to Off\...\broken\ with a dummy put in each one's place;
The plan matches the one you reviewed.
Nothing to move.
-> 1 file(s) moved to a short path (listed in mwdialog_longpaths.json; mwdialog_check --longpaths-undo puts
Plan
G:\m\outputs\mwmanager_vortex_tidy_plan.txt: cut down to the 3 line(s) NOT applied""".splitlines()


class TidyFailureMessage(unittest.TestCase):
    def test_says_what_to_do_and_hides_the_cli_report(self):
        text = mwmanager_gui.tidy_failure_text("Tidy and options for Zz Deep Path-99007-1-0.zip", 1, DEEP_PATH_RUN)
        self.assertIn("exit code 1", text)
        self.assertIn("select Zz Deep Path-99007-1-0.zip in the list and choose Deploy All Except Selected", text)
        for hidden in ("MWMANAGER-", "=====", "G:\\", "--longpaths-undo", "Nothing to move."):
            self.assertNotIn(hidden, text, hidden)
        self.assertIn("Asset scan complete: 1 files checked; 1 in need of repairs", text)
        self.assertIn("The plan matches the one you reviewed.", text)

    def test_tail_is_bounded(self):
        self.assertEqual(len(mwmanager_gui.tool_output_tail([f"line {n}" for n in range(100)])), 12)

class DeployedFilesLine(unittest.TestCase):
    def test_wording(self):
        line = mwmanager_gui.deployed_files_line
        self.assertEqual(line("Run Fix", 1, 0), "Deployed 1 file from Run Fix.")
        self.assertEqual(line("Hortator", 228, 3), "Deployed 228 files from Hortator (3 already in place).")
        self.assertEqual(line("Skills Module", 0, 18), "Skills Module was already deployed: its 18 files are in place.")
        self.assertIn("options chosen leave no files", line("Bloodmoon Rebalance", 0, 0))
        for args in ((0, 0), (0, 5), (7, 0)):
            self.assertNotIn("Deployed 0", line("X", *args))

GENERATE_REPORT = r"""Running --all checks and generating patches…
Load order from G:\m\mwmanager_sort_order.txt
  Reading cells in 4 plugins...
======================================================================
LOAD ORDER
======================================================================
  ..\app\mlox\mlox_base.txt: 3 order rule(s)
  Reading plugins to find patches that must follow the mods they change...
  6 active plugins; 2 change position, 2 need a new date
       6 -> 5    XE Sky Variations.esp
  Dates set on 2 plugin(s). Old dates saved in G:\m\mwmanager_loadorder_undo.txt (undo with
    --restore-dates).
======================================================================
CELL FIXES: rebuilt interiors, door arrivals, objects on reshaped land
======================================================================
  Pathgrids: 18 cell(s) unchanged since an earlier check: taken from
    G:\m\outputs\mwmanager_pathgrid_cache.json
  Nothing to change.
=== XE Sky Variations.esp ===
  [file]
    ERROR header says 11 record(s), the file has 10
Found 1 ERROR, 0 WARN, 0 INFO.
(9 issue(s) in masters are already fixed by 'mwmanager - ESM fixes.esp' and not listed; they are carried over when
  the patch is rebuilt.)
  XE Sky Variations.esp: 1 change(s) written
Final check (patches in their load-order positions):
  XE Sky Variations.esp: 0 ERROR(s) remaining
  Other.esp: 2 ERROR(s) remaining
  other plugins: 0 ERROR(s)
Morrowind.ini's [Game Files] updated from G:\m\mwmanager_sort_order.txt (6 plugins;
  previous ini kept as Morrowind.ini.mwdialog-20261126-092137.bak), so MGE XE's distant land generator and other
  tools see this load order.
REARRANGED INTERIORS: 0 rebuilt by a plugin; 0 finding(s) worth a look
  mwcollide data: 0 doubled object(s) in 0 cell(s) for the game to hide (MWSE\config\mwcollide_data.json)
WARN Some.esp: a warning outside any section""".splitlines()


class GeneratePatchesReport(unittest.TestCase):
    def test_job_window_keeps_findings_and_results_only(self):
        from mwmanager_workflow import UserOutput
        out = UserOutput()
        shown = [line for raw in GENERATE_REPORT for line in out.feed(raw)]
        self.assertEqual(shown, [
            "Running --all checks and generating patches…",
            "  6 active plugins; 2 change position, 2 need a new date",
            "       6 -> 5    XE Sky Variations.esp",
            "=== XE Sky Variations.esp ===",
            "  [file]",
            "    ERROR header says 11 record(s), the file has 10",
            "Found 1 ERROR, 0 WARN, 0 INFO.",
            "  XE Sky Variations.esp: 1 change(s) written",
            "  Other.esp: 2 ERROR(s) remaining",
            "WARN Some.esp: a warning outside any section",
        ])

class RedeploymentRecord(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = Path(self.tmp.name)
        self.archive = self.manager / "MULE-47452.zip"
        self.archive.write_bytes(b"x")
        self.package = self.manager / "staging" / "MULE-47452"
        self.package.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def op(self, rel, backup=None, size=1):
        return {"source": str(self.package / rel), "target": "D:\\Data Files\\" + rel, "backup": backup,
                "source_relative": rel, "target_relative": rel, "backup_relative": backup, "size": size}

    def test_deploying_the_same_archive_again_keeps_one_record(self):
        import plugin_install
        item = {"archive": str(self.archive), "plugins": ["MULE.esp"]}
        first = plugin_install.record_deployment(
            self.manager, dict(item), self.package,
            [self.op("MULE.esp", backup="Off\\MULE.esp"), self.op("MWSE\\mods\\MULE\\main.lua")], [], [])
        again = plugin_install.record_deployment(
            self.manager, dict(item), self.package,
            [self.op("mule.esp", backup="Off\\replaced\\MULE.esp", size=2), self.op("Textures\\new.dds")], [], [])
        records = plugin_install.list_deployments(self.manager)
        self.assertEqual(len(records), 1)
        self.assertEqual(again["id"], first["id"])
        ops = {op["target_relative"].casefold(): op for op in records[0]["operations"]}
        self.assertEqual(set(ops), {"mule.esp", "mwse\\mods\\mule\\main.lua", "textures\\new.dds"})
        self.assertEqual(ops["mule.esp"]["backup"], "Off\\MULE.esp")      # the file the mod replaced first
        self.assertEqual(ops["mule.esp"]["size"], 2)                      # the file now in place

    def test_other_archives_keep_their_own_records(self):
        import plugin_install
        other = self.manager / "Run Fix-45947.7z"
        other.write_bytes(b"y")
        plugin_install.record_deployment(self.manager, {"archive": str(self.archive)}, self.package,
                                         [self.op("a.esp")], [], [])
        plugin_install.record_deployment(self.manager, {"archive": str(other)}, self.package,
                                         [self.op("b.esp")], [], [])
        self.assertEqual(len(plugin_install.list_deployments(self.manager)), 2)


if __name__ == "__main__":
    unittest.main()
