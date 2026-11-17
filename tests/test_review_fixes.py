"""Fixes from the MWManager review: atomic settings, a locked setup step file, Vortex detection outside the GUI."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mwmanager_gui  # noqa: E402
import mwmanager_setup_state as setup_state  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
