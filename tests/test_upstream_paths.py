"""MWManager runs for a long time: each detection reads Vortex's settings again (the shared mwdialog_paths keeps
what it read for a command-line run). Mocked settings, no registry, nothing written."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mwdialog_paths  # noqa: E402
import mwmanager_detect as md  # noqa: E402
from test_prepreflight import FakeRegistry  # noqa: E402


class VortexSettingsAreReadAgain(unittest.TestCase):
    def setUp(self):
        mwdialog_paths.clear_vortex_settings_cache()
        self.addCleanup(mwdialog_paths.clear_vortex_settings_cache)

    def test_each_detection_reads_vortex_again(self):
        answers = [{"downloads_tmpl": "D1", "staging_tmpl": None, "tried": [], "reason": None},
                   {"downloads_tmpl": "D2", "staging_tmpl": None, "tried": [], "reason": None}]
        reads = []

        def fake(game_id=mwdialog_paths.GAME_ID):
            reads.append(1)
            return dict(answers[min(len(reads), 2) - 1])
        with mock.patch.object(mwdialog_paths, "_read_vortex_settings", fake), \
                mock.patch.object(mwdialog_paths, "resolve_downloads",
                                  lambda required=False: mwdialog_paths.read_vortex_settings()["downloads_tmpl"]):
            first = md.detect_vortex(FakeRegistry({}), read_settings=True)
            second = md.detect_vortex(FakeRegistry({}), read_settings=True)
        self.assertEqual(len(reads), 2)
        self.assertEqual((first["downloads"], second["downloads"]), ("D1", "D2"))

    def test_shared_module_offers_the_refresh(self):
        self.assertIn("refresh", mwdialog_paths.read_vortex_settings.__code__.co_varnames)
        self.assertTrue(callable(mwdialog_paths.clear_vortex_settings_cache))


if __name__ == "__main__":
    unittest.main()
