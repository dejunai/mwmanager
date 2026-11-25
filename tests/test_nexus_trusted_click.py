"""Nexus browser downloads click the file page's button (CR-25 stress run).

Nexus's current file page draws "Start download" inside <mod-file-download>'s shadow root, which a plain
querySelectorAll never reached, so nothing was clicked. DOWNLOAD_JS walks shadow roots and reports where the button
is; the browser clicks it from script, and sends a trusted DevTools mouse click there if no download starts."""
import re
import unittest

import mwnexus_browser as B


class TrustedDownloadClick(unittest.TestCase):
    def test_page_script_locates_the_button_without_clicking_it(self):
        self.assertNotIn(".click()", B.DOWNLOAD_JS)
        self.assertIn("getBoundingClientRect", B.DOWNLOAD_JS)
        self.assertRegex(B.DOWNLOAD_JS, r"\(slow\|start\) download")
        self.assertIn("target", B.DOWNLOAD_JS)

    def test_page_script_looks_inside_shadow_roots(self):
        # Nexus draws "Start download" inside <mod-file-download>'s shadow root (round-4 live test)
        self.assertIn("shadowRoot", B.DOWNLOAD_JS)
        self.assertNotIn("offsetParent", B.DOWNLOAD_JS)

    def test_click_events_are_one_left_click_at_the_point(self):
        events = B.trusted_click_events(100.26, 40)
        self.assertEqual([e["type"] for e in events], ["mouseMoved", "mousePressed", "mouseReleased"])
        self.assertTrue(all(e["x"] == 100.3 and e["y"] == 40.0 for e in events))
        self.assertEqual(events[1]["button"], "left")
        self.assertEqual(events[1]["clickCount"], 1)

    def test_mark_script_clicks_from_script_when_asked(self):
        trusted, fallback = B.MARK_CLICKED_JS % "false", B.MARK_CLICKED_JS % "true"
        self.assertTrue(trusted.rstrip().endswith("(false)"))
        self.assertTrue(fallback.rstrip().endswith("(true)"))
        self.assertIn("if (fallback) el.click();", B.MARK_CLICKED_JS)
        self.assertIn("__mwmClicked", B.MARK_CLICKED_JS)


class DownloadLoopOrder(unittest.TestCase):
    def test_script_click_first_trusted_click_as_fallback(self):
        import inspect
        src = inspect.getsource(B._run_embedded_browser)
        first = src.index('run_script(MARK_CLICKED_JS % "true", 10)')
        fallback = src.index("trusted_click(last_target.get(")
        self.assertLess(first, fallback)


if __name__ == "__main__":
    unittest.main()
