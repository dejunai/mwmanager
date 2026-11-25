"""Nexus browser downloads click the file page's button with a trusted click (CR-25 stress run).

Nexus's current file page ignores a script's el.click() on "Start download"; only a real click starts the
download. DOWNLOAD_JS now reports where the button is, and the browser clicks it through WebView2's DevTools
Input.dispatchMouseEvent, falling back to the script click only when that is unavailable or did nothing."""
import re
import unittest

import mwnexus_browser as B


class TrustedDownloadClick(unittest.TestCase):
    def test_page_script_locates_the_button_without_clicking_it(self):
        self.assertNotIn(".click()", B.DOWNLOAD_JS)
        self.assertIn("getBoundingClientRect", B.DOWNLOAD_JS)
        self.assertRegex(B.DOWNLOAD_JS, r"\(slow\|start\) download")
        self.assertIn("target", B.DOWNLOAD_JS)

    def test_click_events_are_one_left_click_at_the_point(self):
        events = B.trusted_click_events(100.26, 40)
        self.assertEqual([e["type"] for e in events], ["mouseMoved", "mousePressed", "mouseReleased"])
        self.assertTrue(all(e["x"] == 100.3 and e["y"] == 40.0 for e in events))
        self.assertEqual(events[1]["button"], "left")
        self.assertEqual(events[1]["clickCount"], 1)

    def test_mark_script_clicks_from_script_only_as_fallback(self):
        trusted, fallback = B.MARK_CLICKED_JS % "false", B.MARK_CLICKED_JS % "true"
        self.assertTrue(trusted.rstrip().endswith("(false)"))
        self.assertTrue(fallback.rstrip().endswith("(true)"))
        self.assertIn("if (fallback) el.click();", B.MARK_CLICKED_JS)
        self.assertIn("__mwmClicked", B.MARK_CLICKED_JS)


if __name__ == "__main__":
    unittest.main()