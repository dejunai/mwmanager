"""Prerequisite programs (the Morrowind Code Patch, MGEXEgui) are started in their own folder and waited on.

The Code Patch reads mcpatch\\ and Morrowind.exe relative to the working directory: started anywhere else it shows
"Installation error" and closes. Windows also refuses to start it with CreateProcess (error 740), so it goes through
the shell."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@unittest.skipUnless(os.name == "nt", "Windows shell launcher")
class ShellProgram(unittest.TestCase):
    def test_it_starts_in_the_programs_own_folder_and_reports_the_exit_code(self):
        try:
            import mwmanager_gui as gui
        except Exception as exc:
            self.skipTest(f"GUI module unavailable: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "probe.py"
            script.write_text("import os, sys\n"
                              "open(os.path.join(os.path.dirname(__file__), 'cwd.txt'), 'w').write(os.getcwd())\n"
                              "sys.exit(7)\n", encoding="utf-8")
            program = Path(tmp) / "probe.cmd"
            program.write_text(f'@echo off\r\n"{sys.executable}" "{script}"\r\nexit /b %errorlevel%\r\n',
                               encoding="utf-8")
            started = gui._ShellProgram(program)
            self.assertTrue(started.waitable)
            deadline = time.time() + 30
            while started.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            self.assertEqual(started.poll(), 7)
            self.assertEqual(os.path.normcase((Path(tmp) / "cwd.txt").read_text()), os.path.normcase(tmp))

    def test_the_gui_no_longer_starts_programs_without_a_folder(self):
        text = (ROOT / "mwmanager_gui.py").read_text("utf-8")
        self.assertNotIn("os.startfile(path)", text)


if __name__ == "__main__":
    unittest.main()
