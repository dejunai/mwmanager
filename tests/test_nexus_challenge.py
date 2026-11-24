"""A Nexus page counts as a bot-protection page only by what it shows, not by the scripts every page loads."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import mock_fomod  # noqa: E402

MOD_PAGE = """<html><head><title>Better Telvanni Crystal at Morrowind Nexus</title>
<meta name="description" content="Retextures Telvanni crystal.">
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
<script>window.recaptcha = {sitekey: "x"}; // access denied handler</script></head>
<body><h1>Better Telvanni Crystal</h1><p>Main file: 4k textures. Optional: 2k textures.</p></body></html>"""

CHALLENGE_PAGE = """<html><head><title>Just a moment...</title></head>
<body><p>Verify you are human by completing the action below.</p>
<p>Performance &amp; security by Cloudflare</p></body></html>"""


class NexusChallenge(unittest.TestCase):
    def fetch(self, html):
        mod_dir = Path(tempfile.mkdtemp()) / "Better Telvanni Crystal-59196"
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(html.encode("utf-8"))
        with mock.patch.object(mock_fomod, "_nexus_page_url", return_value="https://example.invalid/mods/59196"), \
                mock.patch("urllib.request.urlopen", return_value=response):
            text = mock_fomod._nexus_page_text(mod_dir, refresh=True)
        return text, mock_fomod._NEXUS_FETCH_ERRORS.get(str(mod_dir.resolve()).casefold())

    def test_a_mod_page_whose_scripts_name_cloudflare_is_read(self):
        text, error = self.fetch(MOD_PAGE)
        self.assertIsNone(error)
        self.assertIn("Optional: 2k textures", text)

    def test_a_challenge_page_is_reported(self):
        text, error = self.fetch(CHALLENGE_PAGE)
        self.assertEqual(text, "")
        self.assertIn("CAPTCHA", error)


if __name__ == "__main__":
    unittest.main()
