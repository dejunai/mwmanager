"""Close-path tests for the MWManager Nexus browser (auto-close when the options dialog closes).

A small fake browser process stands in for the WebView2 child: it speaks the same
browser.pid / command.json / response.json mailbox, either honouring "close"
(responsive) or ignoring everything (frozen, like the old "Not Responding" hang).
"""
import ast
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import mwnexus_browser as B  # noqa: E402

FAKE = textwrap.dedent(r'''
    import json, os, sys, time
    from pathlib import Path
    root = Path(sys.argv[1]); mode = sys.argv[2]
    (root / "browser.pid").write_text(str(os.getpid()), encoding="ascii")
    (root / "webview2-profile").mkdir(exist_ok=True)
    (root / "webview2-profile" / "Cookies").write_text("session", encoding="utf-8")
    if mode == "frozen":
        while True:
            time.sleep(1)
    last = ""
    while True:
        try:
            cmd = json.loads((root / "command.json").read_text(encoding="utf-8"))
        except Exception:
            cmd = {}
        if cmd.get("id") and cmd["id"] != last:
            last = cmd["id"]
            if cmd.get("action") == "close":
                (root / "response.json").write_text(json.dumps({"id": last, "closed": True}), encoding="utf-8")
                (root / "browser.pid").unlink()
                sys.exit(0)
        time.sleep(0.05)
''')


class CloseBrowser(unittest.TestCase):
    def test_embedded_browser_enables_interactive_downloads(self):
        source = Path(B.__file__).read_text(encoding="utf-8")
        self.assertIn('webview.settings["ALLOW_DOWNLOADS"] = True', source)
        self.assertIn('args.ResultFilePath = str(target)', source)

    def test_github_is_approved_and_component_download_folder_is_sent(self):
        target = Path(self.tmp.name) / "pre-reqs" / "mge_xe"
        with mock.patch.object(B, "_tracked_browser_pid", return_value=123):
            B.open_page("https://github.com/Greatness7/MGE-XE/releases", download_dir=target)
        command = B._read_json(self.root / "command.json")
        self.assertEqual(command["url"], "https://github.com/Greatness7/MGE-XE/releases")
        self.assertEqual(command["download_dir"], str(target.resolve()))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_lad = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self.tmp.name
        self.root = B._root()
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if self.old_lad is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self.old_lad
        self.tmp.cleanup()

    def start_fake(self, mode):
        script = Path(self.tmp.name) / "fake_browser.py"
        script.write_text(FAKE, encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(script), str(self.root), mode])
        self.procs.append(proc)
        for _ in range(100):
            if (self.root / "browser.pid").exists() and (self.root / "webview2-profile" / "Cookies").exists():
                break
            time.sleep(0.05)
        time.sleep(0.2)
        return proc

    def test_no_browser_running(self):
        t0 = time.monotonic()
        self.assertEqual(B.close_browser(timeout=2), "not-running")
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertFalse((self.root / "command.json").exists())

    def test_stale_pid_file_is_cleaned_without_signalling(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (self.root / "browser.pid").write_text(str(dead.pid), encoding="ascii")
        self.assertEqual(B.close_browser(timeout=1), "not-running")
        self.assertFalse((self.root / "browser.pid").exists())

    def test_responsive_browser_closes_gracefully(self):
        proc = self.start_fake("responsive")
        self.assertEqual(B.close_browser(timeout=5), "closed")
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, 0)  # exited by itself, not terminated
        self.assertEqual((self.root / "webview2-profile" / "Cookies").read_text(), "session")

    def test_frozen_browser_is_terminated_after_timeout_profile_kept(self):
        proc = self.start_fake("frozen")
        t0 = time.monotonic()
        self.assertEqual(B.close_browser(timeout=1.0), "terminated")
        self.assertLess(time.monotonic() - t0, 5)
        proc.wait(timeout=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.root / "browser.pid").exists())
        self.assertEqual((self.root / "webview2-profile" / "Cookies").read_text(), "session")

    def test_pid_not_ours_is_never_killed(self):
        # A live process whose start time is after browser.pid was written = recycled PID.
        (self.root / "browser.pid").write_text("0", encoding="ascii")
        time.sleep(2.5)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.procs.append(other)
        time.sleep(0.3)
        old = os.stat(self.root / "browser.pid")
        (self.root / "browser.pid").write_text(str(other.pid), encoding="ascii")
        os.utime(self.root / "browser.pid", (old.st_atime, old.st_mtime))
        self.assertEqual(B._tracked_browser_pid(), 0)
        self.assertEqual(B.close_browser(timeout=0.5), "not-running")
        time.sleep(0.3)
        self.assertIsNone(other.poll(), "an unrelated process was killed")

    def test_background_close_returns_immediately_when_frozen(self):
        proc = self.start_fake("frozen")
        t0 = time.monotonic()
        thread = B.close_browser_in_background(timeout=1.0)
        self.assertLess(time.monotonic() - t0, 0.3)  # dialog close/Apply is not delayed
        thread.join(10)
        self.assertEqual(thread.result.get("status"), "terminated")
        proc.wait(timeout=5)


class DialogHook(unittest.TestCase):
    def test_dialog_closes_browser_in_finally_after_mainloop(self):
        src = (HERE.parent / "mock_fomod.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any("mainloop" in ast.unparse(stmt) for stmt in node.body):
                self.assertTrue(any("_close_nexus_browser_after_dialog" in ast.unparse(stmt) for stmt in node.finalbody))
                found = True
        self.assertTrue(found, "root.mainloop() is not wrapped in try/finally with the browser close")

    def test_opt_out_env(self):
        import mock_fomod
        os.environ["MWMANAGER_KEEP_NEXUS_BROWSER"] = "1"
        try:
            self.assertIsNone(mock_fomod._close_nexus_browser_after_dialog())
        finally:
            os.environ.pop("MWMANAGER_KEEP_NEXUS_BROWSER")

    def test_child_handles_close_command(self):
        src = (HERE.parent / "mwnexus_browser.py").read_text(encoding="utf-8")
        body = src[src.index("def service_requests():"):src.index("def on_closed():")]
        self.assertIn('command.get("action") == "close"', body)
        self.assertIn("window.destroy()", body)
        self.assertNotIn("rmtree", src)  # the profile is never deleted



class DownloadTracking(unittest.TestCase):
    """Closing the browser mid-download cancels it, so callers must see running downloads and their end."""

    def test_running_downloads_are_reported_until_they_finish(self):
        class Event(list):
            def __iadd__(self, handler):
                self.append(handler)
                return self

        class Operation:
            def __init__(self):
                self.State = "CoreWebView2DownloadState.InProgress"
                self.BytesReceived, self.TotalBytesToReceive = 0, 100
                self.StateChanged, self.BytesReceivedChanged = Event(), Event()

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                mock.patch.object(B, "_tracked_browser_pid", return_value=1234):
            root, op = B._root(), Operation()
            state = {"active": set(), "progress": {}, "finished": [], "failed": []}
            B._track_download(root, state, r"K:\dl\TR.7z", op)
            self.assertEqual(B.active_downloads(), [r"K:\dl\TR.7z"])
            op.BytesReceived = 40
            op.BytesReceivedChanged[0](op, None)
            self.assertEqual(state["progress"][r"K:\dl\TR.7z"], [40, 100])
            op.State = "CoreWebView2DownloadState.Completed"
            op.StateChanged[0](op, None)
            self.assertEqual(B.active_downloads(), [])
            self.assertEqual(state["finished"], [r"K:\dl\TR.7z"])
            B._track_download(root, state, "x.7z", Operation())
            with mock.patch.object(B, "_tracked_browser_pid", return_value=0):
                self.assertEqual(B.active_downloads(), [])  # stale file from a closed browser

    def test_download_file_returns_the_finished_file_and_reports_progress(self):
        def browser_answers(url, command):
            B._write_json(B._root() / "response.json",
                          {"id": command["id"], "message": "Downloading TR.7z: 5 MB"})
            B._write_json(B._root() / "response.json", {"id": command["id"], "file": r"K:\dl\TR.7z"})

        messages = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                mock.patch.object(B, "_tracked_browser_pid", side_effect=[0, 1234, 1234]), \
                mock.patch.object(B, "_launch_if_needed", side_effect=browser_answers) as launch, \
                mock.patch.object(B.importlib.util, "find_spec", return_value=object()):
            found = B.download_file("https://www.nexusmods.com/morrowind/mods/42145?tab=files&file_id=1",
                                    tmp, messages.append)
        self.assertEqual(found, Path(r"K:\dl\TR.7z"))
        self.assertEqual(launch.call_args.args[1]["action"], "download")
        with self.assertRaises(ValueError):
            B.download_file("https://example.com/x.7z", ".")


if __name__ == "__main__":
    unittest.main()
