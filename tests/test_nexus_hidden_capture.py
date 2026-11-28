"""Nexus pages refused to a plain request are read in MWManager's browser, hidden: no window, one read at a time,
and a real CAPTCHA becomes one actionable status line instead of a pop-up. The browser gets Back/Forward buttons."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import core_addons  # noqa: E402
import mock_fomod  # noqa: E402
import mwnexus_browser as B  # noqa: E402
import option_descriptions as od  # noqa: E402

URL = "https://www.nexusmods.com/morrowind/mods/47254"
OPEN_PAGE = B.open_page            # the real one; Isolated replaces it so no test can show a window
PAGE = {"url": URL, "title": "A mod", "text": "Option A adds lanterns to every road.\nOption B only to towns."}
MOD_PAGE = """<html><head><title>A mod at Morrowind Nexus</title></head>
<body><h1>A mod</h1><p>Option A adds lanterns to every road. Option B only to towns.</p></body></html>"""
CHALLENGE_PAGE = """<html><head><title>Just a moment...</title></head>
<body><p>Verify you are human by completing the action below.</p>
<p>Performance &amp; security by Cloudflare</p></body></html>"""


def forbidden(*_args, **_kwargs):
    raise urllib.error.HTTPError(URL, 403, "Forbidden", {}, io.BytesIO(b""))


def served(html):
    response = mock.MagicMock()
    response.__enter__.return_value = io.BytesIO(html.encode("utf-8"))
    return mock.Mock(return_value=response)


class Isolated(unittest.TestCase):
    """Every test gets its own browser folder and a fresh hidden-read state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        state = mock.patch.dict(B._BACKGROUND, {"last": 0.0, "check_needed_at": 0.0})
        state.start()
        self.addCleanup(state.stop)
        # nothing in these tests may start or show the real browser
        for name in ("open_page", "_launch_if_needed"):
            patcher = mock.patch.object(B, name, side_effect=AssertionError(f"{name} must not be called"))
            patcher.start()
            self.addCleanup(patcher.stop)


class OptionPageFallback(Isolated):
    def fetch(self, urlopen, capture):
        mod_dir = Path(self.tmp.name) / "A mod-47254"
        with mock.patch.object(mock_fomod, "_nexus_page_url", return_value=URL), \
                mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(B, "capture_page_background", capture), \
                mock.patch.object(B, "capture_page", side_effect=AssertionError("visible read")):
            text = mock_fomod._nexus_page_text(mod_dir, refresh=True)
        return text, mock_fomod._NEXUS_FETCH_ERRORS.get(str(mod_dir.resolve()).casefold())

    def test_http_403_reads_the_page_hidden_and_caches_it(self):
        capture = mock.Mock(return_value=dict(PAGE))
        text, error = self.fetch(mock.Mock(side_effect=forbidden), capture)
        self.assertIn("Option B only to towns", text)
        self.assertIsNone(error)
        capture.assert_called_once_with("47254", URL, force=False)
        self.assertEqual(B.cached_page("47254")["text"], PAGE["text"])

    def test_a_security_page_over_http_also_falls_back(self):
        capture = mock.Mock(return_value=dict(PAGE))
        text, _error = self.fetch(served(CHALLENGE_PAGE), capture)
        self.assertIn("Option A adds lanterns", text)
        capture.assert_called_once()

    def test_no_fallback_when_the_plain_request_works(self):
        capture = mock.Mock(side_effect=AssertionError("no browser read when HTTP works"))
        text, error = self.fetch(served(MOD_PAGE), capture)
        self.assertIn("Option B only to towns", text)
        self.assertIsNone(error)
        capture.assert_not_called()

    def test_a_captcha_gives_one_actionable_line_and_no_window(self):
        capture = mock.Mock(side_effect=B.NexusCheckNeeded(B.CHECK_NEEDED_STATUS))
        with mock.patch("builtins.print") as logged:
            text, error = self.fetch(mock.Mock(side_effect=forbidden), capture)
        self.assertEqual(text, "")
        self.assertEqual(error, B.CHECK_NEEDED_STATUS)
        self.assertEqual(error.count("\n"), 0)
        self.assertTrue(any("47254" in str(call) for call in logged.call_args_list))   # detail goes to the log

    def test_option_description_tool_falls_back_too(self):
        capture = mock.Mock(return_value=dict(PAGE))
        with mock.patch("urllib.request.urlopen", side_effect=forbidden), \
                mock.patch.object(B, "capture_page_background", capture):
            text, cached_at, problem = od.page_text("47254", "http", 14, log=lambda _m: None)
        self.assertIn("Option A", text)
        self.assertTrue(cached_at)
        self.assertEqual(problem, "")
        with mock.patch("urllib.request.urlopen", side_effect=forbidden), \
                mock.patch.object(B, "capture_page_background",
                                  side_effect=B.NexusCheckNeeded(B.CHECK_NEEDED_STATUS)), \
                mock.patch.object(B, "cached_page", return_value=None):
            self.assertEqual(od.page_text("47254", "http", 14, log=lambda _m: None)[2], B.CHECK_NEEDED_STATUS)


class HiddenReader(Isolated):
    def command(self):
        return json.loads((B._root() / "command.json").read_text(encoding="utf-8"))

    def test_a_closed_browser_starts_hidden_and_reads_hidden(self):
        launched = []
        pids = iter([0, 4242])
        with mock.patch.object(B.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(B, "_launch_if_needed", side_effect=lambda url, command: launched.append(command)), \
                mock.patch.object(B, "_tracked_browser_pid", side_effect=lambda: next(pids, 4242)), \
                mock.patch.object(B, "_await_capture", return_value=dict(PAGE)) as wait:
            page = B.capture_page_background("47254", URL)
        self.assertEqual(page["text"], PAGE["text"])
        self.assertEqual(len(launched), 1)
        self.assertTrue(launched[0]["hidden"])
        command = self.command()
        self.assertEqual((command["action"], command["background"], command["url"]), ("capture", True, URL))
        self.assertLessEqual(wait.call_args.kwargs["limit"], B.BACKGROUND_TIMEOUT)

    def test_a_captcha_pauses_hidden_reads_until_the_user_opens_the_browser(self):
        with mock.patch.object(B.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(B, "_tracked_browser_pid", return_value=4242), \
                mock.patch.object(B, "_await_capture", side_effect=B.NexusCheckNeeded("Just a moment...")) as wait:
            with self.assertRaises(B.NexusCheckNeeded) as raised:
                B.capture_page_background("47254", URL)
            self.assertEqual(str(raised.exception), B.CHECK_NEEDED_STATUS)
            self.assertTrue(B.check_needed())
            with self.assertRaises(B.NexusCheckNeeded):
                B.capture_page_background("47254", URL)         # paused: no second attempt at Nexus
            self.assertEqual(wait.call_count, 1)

    def test_open_page_lifts_the_pause(self):
        B._BACKGROUND["check_needed_at"] = time.monotonic()
        with mock.patch.object(B, "open_page", new=OPEN_PAGE), \
                mock.patch.object(B.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(B, "_tracked_browser_pid", return_value=4242):
            B.open_page(URL)
        self.assertFalse(B.check_needed())
        command = self.command()
        self.assertEqual((command["action"], command["url"]), ("open", URL))
        self.assertNotIn("hidden", command)                     # the user's own open shows the window

    def test_forced_read_tries_past_the_pause(self):
        B._BACKGROUND["check_needed_at"] = time.monotonic()
        with mock.patch.object(B.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(B, "_tracked_browser_pid", return_value=4242), \
                mock.patch.object(B, "_await_capture", return_value=dict(PAGE)):
            self.assertEqual(B.capture_page_background("47254", URL, force=True)["text"], PAGE["text"])
        self.assertFalse(B.check_needed())

    def test_one_read_at_a_time_with_a_gap_between_reads(self):
        active, most, starts = [0], [0], []
        guard = threading.Lock()

        def slow(*_args, **_kwargs):
            with guard:
                active[0] += 1
                most[0] = max(most[0], active[0])
                starts.append(time.monotonic())
            time.sleep(0.05)
            with guard:
                active[0] -= 1
            return dict(PAGE)

        with mock.patch.object(B, "BACKGROUND_INTERVAL", 0.1), \
                mock.patch.object(B.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(B, "_tracked_browser_pid", return_value=4242), \
                mock.patch.object(B, "_await_capture", side_effect=slow):
            threads = [threading.Thread(target=B.capture_page_background, args=("47254", URL)) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(most[0], 1)
        self.assertEqual(len(starts), 3)
        starts.sort()
        self.assertTrue(all(b - a >= 0.1 for a, b in zip(starts, starts[1:])))

    def test_a_read_waiting_too_long_for_its_turn_gives_up(self):
        with mock.patch.object(B.importlib.util, "find_spec", return_value=object()):
            B._BACKGROUND_LOCK.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "did not finish in time"):
                    B.capture_page_background("47254", URL, timeout=0.05)
            finally:
                B._BACKGROUND_LOCK.release()

    def test_only_nexus_pages_are_read(self):
        with self.assertRaises(ValueError):
            B.capture_page_background("47254", "https://example.com/mods/47254")

    def test_a_browser_challenge_reply_becomes_check_needed(self):
        response = B._root() / "response.json"
        B._write_json(response, {"id": "r1", "error": "security check", "challenge": True})
        with mock.patch.object(B, "_pid_running", return_value=True):
            with self.assertRaises(B.NexusCheckNeeded):
                B._await_capture(B._root(), "r1", 4242, first_reply=1, limit=1)
        B._write_json(response, {"id": "r2", "error": "page too short"})
        with mock.patch.object(B, "_pid_running", return_value=True):
            with self.assertRaises(RuntimeError) as raised:
                B._await_capture(B._root(), "r2", 4242, first_reply=1, limit=1)
        self.assertNotIsInstance(raised.exception, B.NexusCheckNeeded)


class ChildProcessRules(unittest.TestCase):
    """The browser process: started for MWManager's own work it stays hidden; the user's open shows it."""
    SRC = (ROOT / "mwnexus_browser.py").read_text(encoding="utf-8")

    def test_started_for_hidden_work_the_window_is_created_hidden(self):
        self.assertIn('bool(initial.get("hidden") or initial.get("background"))', self.SRC)
        self.assertIn("hidden=start_hidden,", self.SRC)

    def test_a_first_capture_command_still_runs(self):
        self.assertIn('initial.get("action") in ("download", "capture")', self.SRC)

    def test_hidden_reads_never_navigate_the_window_the_user_sees(self):
        self.assertIn('reader_window(capture_url) if background', self.SRC)
        self.assertIn('webview.create_window(READER_TITLE, url=url, hidden=True', self.SRC)

    def test_only_windows_doing_hidden_work_are_refused_activation(self):
        self.assertTrue(B.blocks_activation(7, B.READER_TITLE, 5, False))      # the hidden reader, always
        self.assertTrue(B.blocks_activation(5, "MWManager", 5, True))          # the main window while hidden work
        self.assertFalse(B.blocks_activation(5, "MWManager", 5, False))        # the user opened it
        self.assertFalse(B.blocks_activation(9, "Save As", 5, True))           # dialogs and anything else


class SamePage(unittest.TestCase):
    """WebView2 ignores navigating to the page it shows; pywebview would then wait forever for the load."""

    def window(self, current):
        loads = []
        win = types.SimpleNamespace(native=types.SimpleNamespace(browser=types.SimpleNamespace(url=current)),
                                    load_url=loads.append)
        return win, loads

    def test_the_page_already_shown_is_not_loaded_again(self):
        win, loads = self.window(URL + "/")
        B._navigate(win, URL)
        self.assertEqual(loads, [])

    def test_another_page_or_a_fresh_window_is_loaded(self):
        win, loads = self.window(URL)
        B._navigate(win, URL + "?tab=files")
        fresh, fresh_loads = self.window(None)
        B._navigate(fresh, URL)
        self.assertEqual((loads, fresh_loads), ([URL + "?tab=files"], [URL]))


class QuietWindow(unittest.TestCase):
    def test_hidden_work_never_activates_and_has_no_taskbar_button(self):
        style = B.quiet_style(B.WS_EX_APPWINDOW | 0x100, True)
        self.assertTrue(style & B.WS_EX_NOACTIVATE and style & B.WS_EX_TOOLWINDOW)
        self.assertFalse(style & B.WS_EX_APPWINDOW)
        self.assertTrue(style & 0x100)                    # other bits are left alone

    def test_the_users_window_is_normal_again(self):
        style = B.quiet_style(B.quiet_style(0x100, True), False)
        self.assertEqual(style, 0x100 | B.WS_EX_APPWINDOW)


class NavigationBar(unittest.TestCase):
    """Back/Forward live in a WinForms strip above the page; fake .NET types stand in for pythonnet."""

    def build(self, can_back=False, can_forward=False):
        class Event(list):
            def __iadd__(self, handler):
                self.append(handler)
                return self

            def fire(self):
                for handler in self:
                    handler(None, None)

        class Button:
            def __init__(self, text):
                self.Text, self.Enabled, self.Click, self.ToolTipText = text, True, Event(), ""

        class Items(list):
            def Add(self, item):
                self.append(item)

        class Strip:
            def __init__(self):
                self.Items, self.Dock, self.GripStyle = Items(), None, None

        control = types.SimpleNamespace(CanGoBack=can_back, CanGoForward=can_forward, went=[],
                                        CoreWebView2=types.SimpleNamespace(HistoryChanged=Event()))
        control.GoBack = lambda: control.went.append("back")
        control.GoForward = lambda: control.went.append("forward")
        control.BringToFront = lambda: control.went.append("front")
        controls = []
        controls_list = types.SimpleNamespace(Add=controls.append)
        form = types.SimpleNamespace(browser=types.SimpleNamespace(webview=control), Controls=controls_list,
                                     Invoke=lambda delegate: delegate())
        system = types.ModuleType("System")
        system.Object = object

        class Func:
            def __class_getitem__(cls, _item):
                return lambda fn: fn
        system.Func = Func
        forms = types.ModuleType("System.Windows.Forms")
        forms.DockStyle = types.SimpleNamespace(Top="top")
        forms.ToolStripGripStyle = types.SimpleNamespace(Hidden="hidden")
        forms.ToolStrip, forms.ToolStripButton = Strip, Button
        with mock.patch.dict(sys.modules, {"System": system, "System.Windows": types.ModuleType("System.Windows"),
                                           "System.Windows.Forms": forms}):
            B._add_navigation_bar(types.SimpleNamespace(native=form))
        bar = controls[0]
        return bar, bar.Items[0], bar.Items[1], control

    def test_buttons_start_disabled_without_history(self):
        bar, back, forward, control = self.build()
        self.assertEqual(bar.Dock, "top")
        self.assertIn("Back", back.Text)
        self.assertIn("Forward", forward.Text)
        self.assertFalse(back.Enabled)
        self.assertFalse(forward.Enabled)
        self.assertIn("front", control.went)            # the page fills the space under the strip

    def test_history_enables_back_and_it_navigates(self):
        _bar, back, forward, control = self.build()
        control.CanGoBack = True
        control.CoreWebView2.HistoryChanged.fire()
        self.assertTrue(back.Enabled)
        self.assertFalse(forward.Enabled)
        back.Click.fire()
        forward.Click.fire()                             # no forward history: nothing happens
        self.assertEqual([step for step in control.went if step != "front"], ["back"])


class FilesPageVersions(Isolated):
    def test_refused_files_pages_are_read_hidden_without_asking(self):
        class Progress(list):
            def __call__(self, message):
                self.append(message)

            def ask_yes_no(self, title, message):
                raise AssertionError("no prompt when the hidden read works")

        page = {"text": "Patch for Purists\nVersion\n5.0.7\nMain files\nPatch archive"}
        progress = Progress()
        with mock.patch.object(core_addons.urllib.request, "urlopen", side_effect=OSError("blocked")), \
                mock.patch.object(B, "capture_page_background", return_value=page) as hidden:
            versions = core_addons._scrape_nexus_versions({"patch for purists.esm"}, progress, 1,
                                                          ids={"patch for purists.esm": "45096"})
        self.assertEqual(versions["patch for purists.esm"], "5.0.7")
        hidden.assert_called_once()


if __name__ == "__main__":
    unittest.main()
