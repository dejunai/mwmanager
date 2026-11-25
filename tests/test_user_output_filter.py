"""The job window shows Tidy plainly: no luafix replacement Lua, PATCHED dumps, per-file asset lines, plan/journal
paths or CLI hints; one summary line per package for the Lua fixes. The detail log keeps every line."""
import tempfile
import unittest
from pathlib import Path

from mwmanager_workflow import JobView, UserOutput

PKG = "Patch for Purists 45096 5.0.7 2026-09-30T01-36Z Le0Pe4QA"
TIDY_RUN = f"""Running Tidy for patch for purists.esm; saved option answers will be reused.
Tidy {PKG}: Planning Tidy rules for {PKG}
vortex_tidy plan, 2026-11-25 15:06: 1 mod(s), 1 to change, 3 move(s); 0 left alone
  3 planned move(s); each is listed in G:\\m\\outputs\\mwmanager_vortex_tidy_plan.txt
Lua check: 1 file(s) to patch (details in G:\\m\\outputs\\mwmanager_vortex_tidy_plan.txt)
======================================================================
ASSET CHECK (each mod's own files)
======================================================================
Tidy {PKG}: Asset scan complete: 61 files checked; 3 in need of repairs
{PKG}
    ERROR Meshes\\r\\xhunger.kf: 1 animation text key(s) with Unix line endings -> CR LF
  3 asset issue(s), 3 repairable (run with --apply to repair them)
  1 texture(s) without mipmaps left alone: no world model of their own mod draws them (menu / MCM / icon / effect
    pictures, or a texture pack). Patterns: G:\\m\\mwmanager_mip_exempt.txt
Full plan: G:\\m\\outputs\\mwmanager_vortex_tidy_plan.txt
Nothing changed. Purge in Vortex, review the plan, run again with --apply, then Deploy.
The plan matches the one you reviewed.
Tidy {PKG}: Applying Tidy moves 4 of 4
Tidy changes: 3 of 4 applied, 1 already in place. Journal:
  G:\\m\\outputs\\mwmanager_vortex_tidy_journal.jsonl (--undo --only <package> puts back that
  package's changes from every run, --undo alone the whole last run).
    patched MWSE Core: core/mcm/components/pages/exclusionspage.lua (KB3, KB4)
      lua core/mcm/components/pages/exclusionspage.lua
          PATCHED: (Known Bugs) KB4: line 24: MWSE Core: ExclusionsPage builds one UI element per item -> 
local PAGE_SIZE = 100
local function addPager(self, content, listName, total)
    return first, last
end
Tidy is not a word Lua starts a line with, but this line is replacement text too
          PATCHED: (Known Bugs) KB3: line 88: MWSE Core: ExclusionsPage destroys outerContainer  -> local parent
    parent:destroyChildren()
          => PATCHED (KB3, KB4) [eda5739b]
  patched 1 Lua file(s) (originals kept in each package's Off tree; journal
    G:\\m\\outputs\\mwmanager_vortex_tidy_journal.jsonl; --undo --only <package> puts one package's
    back)
Asset repairs complete for patch for purists: 3 repaired, 0 skipped
    -> 3 file(s) repaired (originals in Off\\Patch for Purists\\)
Plan
  G:\\m\\outputs\\mwmanager_vortex_tidy_plan.txt: applied and blanked, so it can't be applied twice.
Checking BSA eligibility for patch for purists.esm after final Tidy pass…
Finished tidy and options for patch for purists.esm.
"""


def shown(text, output=None):
    output = output or UserOutput()
    lines = []
    for line in text.splitlines():
        lines += output.feed(line)
    return lines


class UserOutputTests(unittest.TestCase):
    def test_tidy_run_is_plain(self):
        lines = shown(TIDY_RUN)
        joined = "\n".join(lines)
        for hidden in ("PAGE_SIZE", "addPager", "parent:destroyChildren", "PATCHED", "KB3", "lua core/",
                       "G:\\", "--apply", "--undo", "ERROR", "xhunger", "=====", "ASSET CHECK", "vortex_tidy plan",
                       "Journal:", "Full plan", "Purge in Vortex", "mipmaps", "Lua check", "replacement text",
                       "originals"):
            self.assertNotIn(hidden, joined, hidden)
        self.assertEqual([l for l in lines if l.startswith("Fixed")],
                         ["Fixed 2 known issue(s) in 1 Lua script(s) for Patch for Purists (details in the Tidy journal)"])
        for kept in ("Running Tidy for patch for purists.esm; saved option answers will be reused.",
                     f"Tidy {PKG}: Asset scan complete: 61 files checked; 3 in need of repairs",
                     "Tidy changes: 3 of 4 applied, 1 already in place.",
                     "Asset repairs complete for patch for purists: 3 repaired, 0 skipped",
                     "Finished tidy and options for patch for purists.esm."):
            self.assertIn(kept, lines)
        self.assertLess(lines.index("Fixed 2 known issue(s) in 1 Lua script(s) for Patch for Purists "
                                    "(details in the Tidy journal)"),
                        lines.index("Asset repairs complete for patch for purists: 3 repaired, 0 skipped"))

    def test_lua_report_without_closing_line_ends_at_the_next_step(self):
        lines = shown("Tidy A Mod 12345 1.0: Applying the reviewed tidy plan\n"
                      "      lua scripts/x/main.lua\n"
                      "local x = 1\n"
                      "Checking BSA eligibility for a.esm after final Tidy pass…\n")
        self.assertEqual(lines, ["Tidy A Mod 12345 1.0: Applying the reviewed tidy plan",
                                 "Checking BSA eligibility for a.esm after final Tidy pass…"])

    def test_output_outside_tidy_is_untouched(self):
        text = ("# --full step 3: checks\n"
                "    ERROR Some.esp: a real problem the report explains\n"
                "G:\\Games\\Morrowind\\Data Files\n"
                "--full: no errors.\n")
        self.assertEqual(shown(text), text.splitlines())

    def test_stage_lines_pass_through(self):
        self.assertEqual(shown("MWMANAGER-STAGE: Recording deployment: 1 of 3 files"),
                         ["MWMANAGER-STAGE: Recording deployment: 1 of 3 files"])

    def test_detail_log_keeps_every_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "outputs" / "mwmanager_status.log"
            view = JobView.__new__(JobView)
            view.status_log = status
            view.user_output = UserOutput()
            view.control = None
            recorded = []
            view._show = lambda text: (view._record(text), recorded.append(text))
            for line in TIDY_RUN.splitlines():
                view.append(line)
            detail = JobView.detail_log_path(status)
            self.assertEqual(detail.name, "mwmanager_status_detail.log")
            self.assertIn("local PAGE_SIZE = 100", detail.read_text(encoding="utf-8"))
            window_log = status.read_text(encoding="utf-8")
            self.assertNotIn("PAGE_SIZE", window_log)
            self.assertIn("Fixed 2 known issue(s)", window_log)
            self.assertEqual(window_log.strip().splitlines(), recorded)


if __name__ == "__main__":
    unittest.main()