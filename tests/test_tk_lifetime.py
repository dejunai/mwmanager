"""Tk objects stay on their own thread: a download worker that outlives its window must not crash anything.

Regression for the Review major masters window: the download worker used to run while the window was closed by
the main thread, and the dead Tk objects were then freed by the cyclic collector on whichever thread allocated
next (often that worker), which aborts the process. The worker now only talks through a queue.Queue, the window's
poll loop and after() jobs end with the window, and the window's garbage is collected on its own thread."""
import gc
import queue
import sys
import threading
import tkinter as tk
import unittest
import warnings
from pathlib import Path
from tkinter import ttk
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core_addons  # noqa: E402
import tk_lifetime  # noqa: E402

WORKER = "MWManager master download"


def _widgets(widget):
    for child in widget.winfo_children():
        yield child
        yield from _widgets(child)


def _rows():
    return [{"name": "TR_Mainland.esm", "deps": [], "error": None, "installed": False, "selected": True,
             "path": "tr.7z", "page": "https://www.nexusmods.com/b", "reasons": [], "ready": True,
             "nexus": {"status": "outdated", "mod_id": 42145, "file_id": 1000074409}}]


def _prepared(rows):
    return {"rows": rows, "index_by_name": {r["name"].casefold(): i for i, r in enumerate(rows)},
            "readiness": lambda: None, "select_dependencies": lambda i: (True, None),
            "required_by_selection": set}


def _click_update(window):
    next(w for w in _widgets(window) if isinstance(w, ttk.Button) and "Update" in w.cget("text")).invoke()


def _destroy_quietly(root):
    try:
        root.destroy()
    except tk.TclError:             # already destroyed by the test
        pass


def _join_workers(timeout=10):
    for thread in threading.enumerate():
        if thread.name == WORKER:
            thread.join(timeout)
            assert not thread.is_alive(), "download worker did not finish"


class WorkerOutlivesWindow(unittest.TestCase):
    def setUp(self):
        self.unraisable = []
        self.thread_errors = []
        patches = [mock.patch.object(sys, "unraisablehook", self.unraisable.append),
                   mock.patch.object(threading, "excepthook", self.thread_errors.append)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _finish_and_check(self, release, closed):
        """The window is gone; let the worker finish now, then force a collection from a non-main thread."""
        release.set()
        self.assertTrue(closed.wait(10), "the worker never reached its cleanup")
        _join_workers()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            collector = threading.Thread(target=gc.collect)       # what an allocating worker would trigger
            collector.start()
            collector.join()
        # Python 3.12 aborts on a Tcl interpreter freed off its thread; 3.14+ warns that it leaked it instead.
        self.assertEqual([str(w.message) for w in caught if "Tcl interpreter" in str(w.message)], [])
        self.assertEqual(self.unraisable, [])
        self.assertEqual(self.thread_errors, [])
        self.assertTrue(gc.isenabled(), "the collector must be switched back on after the window closes")

    def test_window_destroyed_before_the_worker_finishes(self):
        started, release, closed = threading.Event(), threading.Event(), threading.Event()

        def slow_download(page, folder, progress):
            progress("halfway")
            started.set()
            release.wait(10)
            return Path("tr.7z")

        def click(window):
            _click_update(window)
            for _ in range(500):
                window.update()
                if started.wait(0.01):
                    break
            window.update()
            window.destroy()            # the user closes the window mid-download

        with mock.patch.object(tk.Tk, "mainloop", click), \
                mock.patch("mwnexus_browser.download_file", slow_download), \
                mock.patch("mwnexus_browser.close_browser", closed.set), \
                mock.patch("mwnexus_browser.close_browser_in_background"):
            try:
                result = core_addons.review_core_master_install("m", "g", {"downloads_dir": "dl"},
                                                                prepared=_prepared(_rows()))
            except tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
            self.assertTrue(started.is_set())
            self.assertIsNone(result)
            self._finish_and_check(release, closed)

    def test_no_poll_or_after_job_survives_a_destroyed_window(self):
        """A Toplevel shares its parent's interpreter, so its after() jobs would keep firing on a dead window."""
        started, release, closed = threading.Event(), threading.Event(), threading.Event()
        try:
            parent = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.addCleanup(_destroy_quietly, parent)

        def slow_download(page, folder, progress):
            started.set()
            release.wait(10)
            return Path("tr.7z")

        def driver():
            windows = [w for w in parent.winfo_children() if isinstance(w, tk.Toplevel)]
            if not windows:
                parent.after(20, driver)
                return
            window = windows[0]
            if not getattr(driver, "clicked", False):
                driver.clicked = True
                _click_update(window)
            if not started.is_set():
                parent.after(20, driver)
                return
            window.destroy()

        parent.after(20, driver)
        with mock.patch("mwnexus_browser.download_file", slow_download), \
                mock.patch("mwnexus_browser.close_browser", closed.set), \
                mock.patch("mwnexus_browser.close_browser_in_background"):
            result = core_addons.review_core_master_install("m", "g", {"downloads_dir": "dl"}, parent=parent,
                                                            prepared=_prepared(_rows()))
            self.assertIsNone(result)
            parent.update()
            pending = parent.tk.call("after", "info")
            self.assertEqual(tuple(pending), (), "after() jobs of the destroyed window are still queued")
            self._finish_and_check(release, closed)


class DownloadWorkerReportsThroughTheQueue(unittest.TestCase):
    def run_worker(self, download):
        events = queue.Queue()
        with mock.patch("mwnexus_browser.download_file", download), \
                mock.patch("mwnexus_browser.close_browser") as close:
            core_addons.start_download_worker(events, 42145, 1000074409, "dl").join(10)
        close.assert_called_once()
        out = []
        while not events.empty():
            out.append(events.get_nowait())
        return out

    def test_progress_then_file(self):
        def download(page, folder, progress):
            progress("50%")
            return Path("tr.7z")

        self.assertEqual(self.run_worker(download), [("message", "50%"), ("file", "tr.7z")])

    def test_failure_is_one_error_event(self):
        def download(page, folder, progress):
            raise RuntimeError("Nexus said no")

        self.assertEqual(self.run_worker(download), [("error", "Nexus said no")])

    def test_worker_has_no_closure_to_capture_tk_objects(self):
        self.assertIsNone(core_addons.start_download_worker.__closure__)


class PollLoopStops(unittest.TestCase):
    def test_loop_ends_with_the_window(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.addCleanup(_destroy_quietly, root)
        ticks = []
        loop = tk_lifetime.PollLoop(root, 10, lambda: ticks.append(1) or True)
        loop.start()
        for _ in range(20):
            root.update()
        self.assertTrue(ticks)
        root.destroy()
        count = len(ticks)
        self.assertTrue(loop.stopped)
        self.assertIsNone(loop.job)
        self.assertEqual(len(ticks), count)


if __name__ == "__main__":
    unittest.main()