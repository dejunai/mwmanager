"""Status-window and scan-note wording found by the CR-28 stress run (round 1): the job window shows only lines the
user can act on, and an "N of M" count at most every 20 seconds; archive scan notes say each thing once, plainly."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core_addons  # noqa: E402
import mwmanager_gui as G  # noqa: E402
import mwmanager_workflow as W  # noqa: E402
import preinstall  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CounterCadenceTests(unittest.TestCase):
    def test_a_fast_count_shows_its_first_line_then_one_every_20_seconds(self):
        clock = Clock()
        cadence = W.CounterCadence(clock=clock)
        shown = []
        for n in range(1, 37):
            if cadence.show(f"Inspecting archive {n} of 36: Mod {n}.7z"):
                shown.append(n)
            clock.now += 0.4
        self.assertEqual(shown, [1])
        clock.now += 20
        self.assertTrue(cadence.show("Inspecting archive 36 of 36: Last.7z"))

    def test_counts_are_told_apart_by_their_words_and_total(self):
        cadence = W.CounterCadence(clock=Clock())
        self.assertTrue(cadence.show("Options for A.7z: Applying Tidy moves 0 of 159"))
        self.assertFalse(cadence.show("Options for A.7z: Applying Tidy moves 1 of 159"))
        self.assertTrue(cadence.show("Tidy changes: 158 of 159 applied, 1 already in place."))
        self.assertTrue(cadence.show("Extracting archive 1 of 24 to staging: A.7z"))
        self.assertTrue(cadence.show("Inspecting archive 1 of 36: A.7z"))

    def test_lines_without_a_count_always_show(self):
        cadence = W.CounterCadence(clock=Clock())
        for _ in range(3):
            self.assertTrue(cadence.show("Preflight passed."))


class PreflightLines(unittest.TestCase):
    def test_passing_checks_are_detail_only_and_notices_show(self):
        report = {"status": ["SHA-256 matches the original: Morrowind.esm", "MWSE latest build unknown: offline"],
                  "notices": ["MWSE latest build unknown: offline"]}
        self.assertEqual(preinstall.status_line(report, report["status"][0]),
                         "MWMANAGER-DETAIL: Preflight: SHA-256 matches the original: Morrowind.esm")
        self.assertEqual(preinstall.status_line(report, report["status"][1]),
                         "Preflight: MWSE latest build unknown: offline")


class ScanNotes(unittest.TestCase):
    def test_missing_major_masters_are_one_sentence_each_named_once(self):
        note = G.major_master_note({
            "A.esp": ["Morrowind.esm", "AATL_Data.esm"],
            "B.esp": ["Tamriel_Data.esm", "OAAB_Data.esm", "AATL_Data.esm"],
            "C.esp": ["tamriel_data.esm", "AATL_Data.esm"],
        }, {"aatl_data.esm", "tamriel_data.esm", "oaab_data.esm"}, set())
        self.assertEqual(note, "Required major master(s) must be deployed first: AATL_Data.esm, Tamriel_Data.esm, "
                               "OAAB_Data.esm")
        self.assertEqual(G.major_master_note({"A.esp": ["OAAB_Data.esm"]}, {"oaab_data.esm"}, {"oaab_data.esm"}), "")

    def test_an_archive_with_nothing_in_it_is_not_ready(self):
        self.assertTrue(G.archive_ready_note({"plugins": [], "asset_count": 0}).startswith("Nothing to deploy"))
        self.assertTrue(G.archive_ready_note({"plugins": ["A.esp"], "asset_count": 0}).startswith("Ready."))
        self.assertTrue(G.archive_ready_note({"plugins": [], "asset_count": 0, "has_mwse": True}).startswith("Ready."))

    def test_seven_zip_error_drops_the_echoed_path(self):
        path = r"G:\downloads\Zz Truncated-99003-1-0.7z"
        out = (f"ERROR: {path} : {path}\nOpen ERROR: Cannot open the file as [7z] archive\n\n\nERRORS:\n"
               "Unexpected end of archive\n")
        self.assertEqual(core_addons.seven_zip_error(out, path, 2),
                         "Cannot open the file as [7z] archive; Unexpected end of archive")
        self.assertEqual(core_addons.seven_zip_error("", path, 2), "7-Zip exited 2")


class PlayAndGenerateMessages(unittest.TestCase):
    def test_a_play_click_while_play_is_unavailable_says_why(self):
        title, text = G.play_blocked_reason({"first_run": 0, "init_complete": 1})
        self.assertEqual(title, "Generate Patches first")
        self.assertIn("Generate Patches, then Play", text)
        self.assertEqual(G.play_blocked_reason({"first_run": 1})[0], "Finish setup first")

    def test_generate_failure_names_an_altered_core_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            game, manager = Path(tmp) / "game", Path(tmp) / "manager"
            (game / "Data Files").mkdir(parents=True)
            (manager / "outputs").mkdir(parents=True)
            for name in ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm", "Morrowind.bsa", "Tribunal.bsa",
                         "Bloodmoon.bsa"):
                (game / "Data Files" / name).write_bytes(b"not the shipped file")
            hint = G.core_file_hint(str(game), manager_dir=str(manager))
            self.assertIn("Tribunal.esm", hint)
            self.assertNotIn("Play will continue", hint)
            self.assertTrue(hint.endswith("\n\n"))


if __name__ == "__main__":
    unittest.main()
