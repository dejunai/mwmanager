"""A job window whose stop request goes unanswered offers Force stop, which ends the job's processes and lets the
window go."""
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_workflow  # noqa: E402


class ForceStopTests(unittest.TestCase):
    def view(self):
        view = mwmanager_workflow.JobView.__new__(mwmanager_workflow.JobView)
        view.window, view.stage, view.cancel = mock.Mock(), mock.Mock(), mock.Mock()
        view.force, view.close = mock.Mock(), mock.Mock()
        view.control = types.SimpleNamespace(cancelled=threading.Event())
        return view

    def test_force_stop_is_offered_only_while_the_same_job_still_runs(self):
        view = self.view()
        view.request_cancel()
        delay, offer = view.window.after.call_args.args
        self.assertEqual(delay, mwmanager_workflow.FORCE_STOP_DELAY_MS)
        offer()
        view.force.pack.assert_called_once()
        finished = self.view()
        finished.request_cancel()
        callback = finished.window.after.call_args.args[1]
        finished.control = None                                   # the job finished on its own meanwhile
        callback()
        finished.force.pack.assert_not_called()

    def test_force_stop_ends_the_processes_and_releases_the_window(self):
        view = self.view()
        with mock.patch("managed_processes.end_all") as end_all:
            view.force_stop()
        end_all.assert_called_once()
        view.window.grab_release.assert_called_once()
        view.close.pack.assert_called_once()


if __name__ == "__main__":
    unittest.main()
