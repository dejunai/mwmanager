"""Tests for the Nexus page capture/parse path used by mock_fomod's option descriptions.

Fixtures in tests/fixtures are *modelled* on Nexus markup (see header comments);
the AURA text is excerpted from the public mod page.
"""
import ast
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import mwnexus_parse as P  # noqa: E402

FIX = HERE / "fixtures"
try:
    import websocket  # noqa: F401
    HAVE_WS = True
except ImportError:
    HAVE_WS = False
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


def run_capture_js(markup: str) -> dict:
    """Load markup in headless Chrome (CDP) and evaluate CAPTURE_JS exactly as the browser would."""
    import socket
    import time
    import urllib.request

    import websocket  # websocket-client
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(markup, encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
                                 f"--user-data-dir={tmp}/prof", f"--remote-debugging-port={port}", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1).read())
                    target = next(t for t in targets if t.get("type") == "page")
                    break
                except Exception:
                    time.sleep(0.1)
            ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20)
            ids = iter(range(1, 1000))

            def call(method, **params):
                mid = next(ids)
                ws.send(json.dumps({"id": mid, "method": method, "params": params}))
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == mid:
                        return msg.get("result", {})

            call("Page.navigate", url=page.as_uri())
            for _ in range(100):
                if call("Runtime.evaluate", expression="document.readyState", returnByValue=True)["result"].get("value") == "complete":
                    break
                time.sleep(0.05)
            value = call("Runtime.evaluate", expression=P.CAPTURE_JS, returnByValue=True)["result"]["value"]
            ws.close()
        finally:
            proc.kill()
            proc.wait()
    return P.decode_script_result(value)


def aura_capture(**overrides):
    markup = (FIX / "nexus_aura_48255.html").read_text(encoding="utf-8")
    page = {"url": "https://www.nexusmods.com/morrowind/mods/48255?tab=description",
            "title": "AURA - A Sound Overhaul Mod at Morrowind Nexus", "readyState": "complete",
            "description": "", "descriptionHtml": markup.split('mod_description_container condensed">', 1)[1].split("</div>", 1)[0],
            "about": "Lua-based sound overhaul. Ambient sounds, interior weather, extended voices, and more.",
            "jsonld": [], "text": P.html_to_text(markup)}
    page.update(overrides)
    return page


class DecodeAndChallenge(unittest.TestCase):
    def test_decode_variants(self):
        d = {"url": "u", "text": "t"}
        self.assertEqual(P.decode_script_result(d), d)
        self.assertEqual(P.decode_script_result(json.dumps(d)), d)
        self.assertEqual(P.decode_script_result(json.dumps(json.dumps(d))), d)  # double-encoded bridge
        self.assertIsNone(P.decode_script_result(None))
        self.assertIsNone(P.decode_script_result("not json"))

    def test_challenge_detected(self):
        text = P.html_to_text((FIX / "nexus_challenge.html").read_text())
        self.assertTrue(P.is_challenge("Just a moment...", text))

    def test_real_page_with_captcha_comment_is_not_challenge(self):
        page = aura_capture()
        self.assertFalse(P.is_challenge(page["title"], page["text"] + "\n" + "filler " * 1200))


class PageText(unittest.TestCase):
    def test_description_first_then_full_page(self):
        text = P.page_text_from_capture(aura_capture())
        self.assertIn('Replacer - a replacer for some of the "worst offenders"', text)
        self.assertLess(text.index("worst offenders"), text.index("--- full page ---"))

    def test_jsonld_and_meta_used_when_no_description(self):
        page = aura_capture(descriptionHtml="", text="", jsonld=[json.dumps({"description": "A sound overhaul described in JSON-LD form only."})])
        text = P.page_text_from_capture(page)
        self.assertIn("JSON-LD form only", text)
        self.assertIn("Lua-based sound overhaul", text)


class OptionMatching(unittest.TestCase):
    def setUp(self):
        self.text = P.page_text_from_capture(aura_capture())

    def test_aura_replacer_maps_to_worst_offenders_sentence(self):
        best = P.best_option_sentences(self.text, "01 Replacer")
        self.assertTrue(best)
        self.assertTrue(best[0].startswith("Replacer - a replacer for some of the \"worst offenders\""), best)
        self.assertNotIn("Replacer fixes (stereo/mono, hz)", best[0])

    def test_stemming_replace_family(self):
        self.assertEqual({P.stem(w) for w in ("replacer", "replace", "replaces", "replacers", "replacement")}, {"replac"})

    def test_plain_replace_wording_still_matches(self):
        page = "Changelogs\nVersion 2.0 Replacer fixes\nThese sound files replace the worst vanilla sounds in the game."
        self.assertEqual(P.best_option_sentences(page, "Replacer")[0],
                         "These sound files replace the worst vanilla sounds in the game.")

    def test_other_aura_folders(self):
        self.assertIn("Passage of Prayers", P.best_option_sentences(self.text, "02 Prayer wheel sound replacer")[0])
        self.assertIn("Animated Morrowind", P.best_option_sentences(self.text, "03 Animated Morrowind Music Replacer")[0])

    def test_mock_fomod_option_description_uses_page(self):
        import mock_fomod
        record = {"label": "Replacer", "targets": [], "docs": "",
                  "path": Path("staging/AURA/Extras/01 Replacer"),
                  "payload_files": {f"Sound/Fx/envrn/{n}.wav": 1 for n in ("bell1", "fire", "wind")}}
        desc = mock_fomod._option_description(record, Path("staging/AURA"), self.text)
        self.assertTrue(desc.startswith("Replacer - a replacer for some of the \"worst offenders\""), desc)


class BrowserModule(unittest.TestCase):
    def test_js_api_has_no_public_window_reference(self):
        """Regression: pywebview walks public js_api attributes; api.window froze the browser."""
        src = (HERE.parent / "mwnexus_browser.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "BrowserApi":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "self":
                        self.assertTrue(sub.attr.startswith("_"), f"public js_api attribute self.{sub.attr}")
        self.assertNotIn("api.window =", src)

    def test_no_os_kill_probe_on_windows(self):
        src = (HERE.parent / "mwnexus_browser.py").read_text(encoding="utf-8")
        body = src[src.index("def _pid_running"):src.index("def _startup_error")]
        self.assertLess(body.index('os.name == "nt"'), body.rindex("os.kill(pid, 0)"))
        self.assertIn("GetExitCodeProcess", body)


@unittest.skipUnless(CHROME and HAVE_WS, "headless Chrome / websocket-client not available")
class CaptureScriptInChrome(unittest.TestCase):
    def test_classic_container(self):
        page = run_capture_js((FIX / "nexus_aura_48255.html").read_text(encoding="utf-8"))
        self.assertEqual(page["selector"], ".mod_description_container")
        self.assertIn("worst offenders", page["description"])
        self.assertNotIn("Version 4.0.0", page["description"])
        self.assertIn("Lua-based sound overhaul", page["about"])
        self.assertTrue(page["jsonld"])
        best = P.best_option_sentences(P.page_text_from_capture(page), "01 Replacer")
        self.assertTrue(best[0].startswith("Replacer - a replacer"), best)

    def test_unknown_layout_uses_heuristic(self):
        markup = (FIX / "nexus_aura_48255.html").read_text(encoding="utf-8")
        markup = markup.replace("mod_description_container condensed", "x9f-body").replace("tab-description", "x9f-tab")
        page = run_capture_js(markup)
        self.assertTrue(page["selector"].startswith("heuristic:"), page["selector"])
        self.assertIn("worst offenders", P.page_text_from_capture(page).split("--- full page ---")[0])


if __name__ == "__main__":
    unittest.main()
