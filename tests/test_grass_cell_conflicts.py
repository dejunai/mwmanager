"""A grass plugin never gets a cell-conflict question (CR-25 stress run follow-up). Rem-TOTSP.esp was asked about
against Bloodmoon.esm and Solstheim Tomb of the Snow Prince.esm because the list of unanswered pairs was left from
a run before grass plugins were left out, and a later scan that found nothing never cleared it. Everything here is
a temporary folder plus tests/fixtures."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import mwmanager_upstream  # noqa: E402

GRASS = ROOT / "tests" / "fixtures" / "grass_rem_style.esp"
STALE = "rem-totsp.esp|solstheim tomb of the snow prince.esm\nbloodmoon.esm|rem-totsp.esp\n"


class GrassDetection(unittest.TestCase):
    def test_a_remiros_style_groundcover_plugin_is_grass(self):
        import vortex_tidy
        self.assertTrue(vortex_tidy.is_grass_plugin(str(GRASS)))

    def test_a_grass_plugin_in_the_load_order_is_left_out_of_every_check(self):
        from test_core_master_order import plugin
        m = mwmanager_upstream.compose()
        with tempfile.TemporaryDirectory() as tmp:
            normal = Path(tmp) / "Normal.esp"
            plugin(normal, [])
            grass = Path(tmp) / "Rem-TOTSP.esp"
            shutil.copy2(GRASS, grass)
            kept, dropped = m.drop_grass_plugins([str(normal), str(grass)])
        self.assertEqual([Path(p).name for p in kept], ["Normal.esp"])
        self.assertEqual([Path(p).name for p in dropped], ["Rem-TOTSP.esp"])


class PendingPairs(unittest.TestCase):
    def setUp(self):
        from test_core_master_order import plugin
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.manager = root / "manager"
        (self.manager / "outputs").mkdir(parents=True)
        self.data = root / "game" / "Data Files"
        self.data.mkdir(parents=True)
        self.ini = self.data.parent / "Morrowind.ini"
        self.ini.write_text("[Game Files]\n", encoding="cp1252")
        scope = mwmanager_upstream.use_manager_dir(str(self.manager))
        self.m = scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        for name in ("Bloodmoon.esm", "Solstheim Tomb of the Snow Prince.esm"):
            plugin(self.data / name, [])
        shutil.copy2(GRASS, self.data / "Rem-TOTSP.esp")
        self.pending = Path(self.m.output_file(self.m.NEW_CONFLICTS_FILE))
        self.pending.write_text(STALE, encoding="utf-8")

    def args(self, **kw):
        base = dict(data_dir=str(self.data), ini=str(self.ini), dry_run=False, no_interactive=True,
                    only_incompatible=False, only=None)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_a_full_scan_starts_from_an_empty_list(self):
        self.m.clear_pending_cell_conflicts(self.args())
        self.assertFalse(self.pending.exists())

    def test_partial_interactive_or_preview_runs_keep_the_list(self):
        for kw in ({"only_incompatible": True}, {"only": ["Bloodmoon.esm"]}, {"no_interactive": False},
                   {"dry_run": True}):
            self.m.clear_pending_cell_conflicts(self.args(**kw))
            self.assertEqual(self.pending.read_text(encoding="utf-8"), STALE, kw)

    def test_pairs_with_a_plugin_outside_the_load_order_are_not_reported(self):
        self.m.write_load_order(str(self.data.parent), ["Bloodmoon.esm", "Solstheim Tomb of the Snow Prince.esm"],
                                "test")
        self.assertEqual(self.m.pending_cell_conflicts(self.args()), [])
        self.pending.write_text("bloodmoon.esm|solstheim tomb of the snow prince.esm\n", encoding="utf-8")
        self.assertEqual(self.m.pending_cell_conflicts(self.args()),
                         ["bloodmoon.esm|solstheim tomb of the snow prince.esm"])

    def test_the_gui_scan_drops_the_grass_pairs(self):
        """The Generate Patches scan (--cell-conflicts --no-interactive) with Rem-TOTSP.esp listed in the load
        order: the grass plugin is left out, nothing conflicts, and the old pairs are gone."""
        self.m.write_load_order(str(self.data.parent),
                                ["Bloodmoon.esm", "Solstheim Tomb of the Snow Prince.esm", "Rem-TOTSP.esp"], "test")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                mock.patch.dict(os.environ, {"MWMANAGER_CONFLICT_GUI": ""}):
            rc = self.m.main([str(self.data), "--cell-conflicts", "--no-interactive", "--ini", str(self.ini)])
        self.assertEqual(rc, 0, out.getvalue()[-3000:])
        self.assertIn("left out of this run", out.getvalue())          # named as a grass plugin left out
        self.assertIn("Reading cells in 2 plugins", out.getvalue())
        pairs = self.pending.read_text(encoding="utf-8").split() if self.pending.exists() else []
        self.assertFalse([p for p in pairs if "rem-totsp" in p], pairs)


class PlainPrompt(unittest.TestCase):
    def test_choices_say_who_wins_in_plain_words(self):
        m = mwmanager_upstream.compose()
        conflict = {"place": [], "refs": [], "settings": [], "doubled": False, "dups": 0}
        opts = {"1": ("order", "A.esp", "B.esp", "keep B.esp winning"),
                "2": ("order", "B.esp", "A.esp", "make A.esp win"),
                "3": ("off", "A.esp", "B.esp", "turn off A.esp and 2 dependent plugin(s)")}
        with mock.patch.dict(os.environ, {"MWMANAGER_CONFLICT_GUI": "1"}), \
                mock.patch("mwmanager_workflow.conflict_answer", return_value="s") as picker:
            m.conflict_choice(1, 1, "minor", "A.esp", "B.esp", conflict, {}, opts, "", {}, {}, {}, set())
        title, details, choices = picker.call_args.args[:3]
        self.assertIn("Right now B.esp wins: it loads after A.esp", details)
        labels = dict((key, label) for label, key in choices)
        self.assertTrue(labels["1"].startswith("B.esp wins") and labels["1"].endswith("(no change)"))
        self.assertTrue(labels["2"].startswith("A.esp wins") and "(no change)" not in labels["2"])
        self.assertEqual(labels["3"], "Keep only B.esp: turn off A.esp and 2 dependent plugin(s)")
        self.assertTrue(labels["s"].startswith("Decide later"))

    def test_an_esm_esp_pair_says_nothing_changes(self):
        m = mwmanager_upstream.compose()
        self.assertEqual(m.plain_conflict_choice(("keep", "Rem.esp", "Bloodmoon.esm", "keep Rem.esp winning"),
                                                 "Rem.esp"),
                         "Rem.esp wins (an .esm always loads before an .esp, so nothing changes)")


if __name__ == "__main__":
    unittest.main()