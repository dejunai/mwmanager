"""The setup step file records enough to resume an interrupted install, and a closed MWManager leaves no step
running behind it."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mwmanager_setup_state as state  # noqa: E402


class StepFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.game = Path(self.tmp.name) / "Morrowind"
        self.game.mkdir()
        self.path = Path(self.tmp.name) / "mwmanager_setup_state.json"

    def test_an_interrupted_step_is_told_from_one_never_started(self):
        state.start(self.path, self.game)
        self.assertEqual(state.interrupted(self.path, self.game), (None, None))
        state.complete(self.path, self.game, "paths_confirmed")
        state.begin(self.path, self.game, "preflight_complete")
        step, entry = state.interrupted(self.path, self.game)
        self.assertEqual(step, "preflight_complete")
        self.assertEqual(entry["attempts"], 1)
        self.assertIsNone(entry["completed_at"])

        state.fail(self.path, self.game, "preflight_complete", "stopped at a prompt")
        state.begin(self.path, self.game, "preflight_complete")          # the retry
        step, entry = state.interrupted(self.path, self.game)
        self.assertEqual((step, entry["attempts"], entry["last_error"]), ("preflight_complete", 2, None))

        state.complete(self.path, self.game, "preflight_complete")
        self.assertTrue(state.done(self.path, self.game, "preflight_complete"))
        self.assertEqual(state.interrupted(self.path, self.game), (None, None))

    def test_failure_keeps_the_reason(self):
        state.start(self.path, self.game)
        state.begin(self.path, self.game, "tidy_complete", pid=4321)
        state.fail(self.path, self.game, "tidy_complete", "Tidying Data Files failed (exit 1)")
        entry = json.loads(self.path.read_text("utf-8"))["steps"]["tidy_complete"]
        self.assertEqual(entry["last_error"], "Tidying Data Files failed (exit 1)")
        self.assertIsNone(entry["pid"])
        self.assertFalse(state.done(self.path, self.game, "tidy_complete"))

    def test_the_full_pass_checkpoint_can_be_taken_back(self):
        state.start(self.path, self.game)
        state.complete(self.path, self.game, "initial_full_pass_complete")
        state.reset(self.path, self.game, "initial_full_pass_complete")   # a conflict review started
        self.assertFalse(state.done(self.path, self.game, "initial_full_pass_complete"))

    def test_a_version_1_file_is_read_and_gains_the_new_fields(self):
        old = {"version": 1, "game_dir": str(self.game.resolve()),
               "steps": {"paths_confirmed": {"completed_at": "2026-11-14T21:31:00-08:00"},
                         "preflight_complete": {"completed_at": None}}}
        self.path.write_text(json.dumps(old), encoding="utf-8")
        self.assertTrue(state.done(self.path, self.game, "paths_confirmed"))
        loaded = state.load(self.path, self.game)
        self.assertEqual(set(loaded["steps"]), set(state.STEPS))
        self.assertEqual(loaded["steps"]["initial_full_pass_complete"]["attempts"], 0)

    def test_an_unreadable_file_is_kept_aside_not_overwritten(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertFalse(state.done(self.path, self.game, "paths_confirmed"))
        kept = list(self.path.parent.glob("mwmanager_setup_state.json.corrupt-*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text("utf-8"), "{ not json")

    def test_another_installs_record_does_not_apply(self):
        state.start(self.path, self.game)
        state.complete(self.path, self.game, "paths_confirmed")
        other = Path(self.tmp.name) / "Other"
        other.mkdir()
        self.assertFalse(state.done(self.path, other, "paths_confirmed"))

    def test_a_step_whose_process_is_still_alive_is_found(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertTrue(state.pid_alive(child.pid))
            state.start(self.path, self.game)
            state.begin(self.path, self.game, "initial_full_pass_complete")
            state.begin(self.path, self.game, "initial_full_complete", pid=child.pid)
            step, entry = state.running(self.path, self.game)
            self.assertEqual((step, entry["pid"]), ("initial_full_complete", child.pid))
            state.end_process(child.pid)
            child.wait(timeout=15)
            self.assertFalse(state.pid_alive(child.pid))
            self.assertEqual(state.running(self.path, self.game), (None, None))
        finally:
            if child.poll() is None:
                child.kill()


class ClosingEndsTheStep(unittest.TestCase):
    def test_children_are_ended_and_only_one_instance_holds_the_lock(self):
        try:
            import mwmanager_gui as gui
        except Exception as exc:                                    # no Tk on this machine
            self.skipTest(f"GUI module unavailable: {exc}")
        child = gui._popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.assertIsNone(child.poll())
        gui._end_children()
        self.assertIsNotNone(child.poll())
        self.assertNotIn(child, gui._CHILDREN)

        with tempfile.TemporaryDirectory() as tmp:
            real = gui.APP_DIR
            gui.APP_DIR = Path(tmp)
            try:
                first = gui._single_instance(wait_seconds=0)
                self.assertIsNotNone(first)
                self.assertIsNone(gui._single_instance(wait_seconds=0.3))    # a second MWManager is refused
                first.close()
                again = gui._single_instance(wait_seconds=0)                  # free again once the first is gone
                self.assertIsNotNone(again)
                again.close()
            finally:
                gui.APP_DIR = real


if __name__ == "__main__":
    unittest.main()
