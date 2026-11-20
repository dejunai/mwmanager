"""MWManager's own sort (mwmanager_overrides.compute_order) reads [NearStart]/[NearEnd] blocks of
mwmanager_rules.txt as [Order] rules too, like upstream: [NearStart] as listed, [NearEnd] reversed. mlox's
files keep mlox's meaning. Temporary folders only; nothing real is touched."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import mwmanager_upstream  # noqa: E402
from test_core_master_order import plugin  # noqa: E402


class NearBlocksOrder(unittest.TestCase):
    def _sort(self, layout, *rule_sets):
        import mwmanager as m
        with tempfile.TemporaryDirectory() as tmp, mwmanager_upstream.use_manager_dir(tmp):
            df = Path(tmp) / "game" / "Data Files"
            df.mkdir(parents=True)
            plugins = []
            for name, masters in [("Morrowind.esm", [])] + layout:
                plugin(df / name, masters)
                plugins.append((name, str(df / name)))
            return m.compute_order(plugins, list(rule_sets), [n for n, _p in plugins])

    LAYOUT = [("Late.esp", ["Morrowind.esm"]), ("X.esp", ["Morrowind.esm"]),
              ("Y.esp", ["Morrowind.esm", "Late.esp"]), ("Z.esp", ["Morrowind.esm"])]

    def test_near_end_block_in_the_rules_file_orders_its_plugins(self):
        order, notes = self._sort(self.LAYOUT, [("nearend", ["Late.esp"], "mwmanager_rules.txt:1"),
                                                ("nearend", ["X.esp", "Y.esp"], "mwmanager_rules.txt:4")])
        self.assertEqual(order, ["Morrowind.esm", "Z.esp", "Late.esp", "Y.esp", "X.esp"])
        self.assertEqual(notes["applied"], 1)

    def test_mlox_near_blocks_do_not_order(self):
        order, notes = self._sort(self.LAYOUT, [("nearend", ["Late.esp"], "mwmanager_rules.txt:1")], [],
                                  [("nearend", ["X.esp", "Y.esp"], "mlox_base.txt:1")])
        self.assertLess(order.index("X.esp"), order.index("Y.esp"))
        self.assertEqual(notes["applied"], 0)

    def test_contradiction_is_reported(self):
        order, notes = self._sort([("A.esp", ["Morrowind.esm"]), ("B.esp", ["Morrowind.esm", "A.esp"])],
                                  [("nearstart", ["B.esp", "A.esp"], "mwmanager_rules.txt:1")])
        self.assertLess(order.index("A.esp"), order.index("B.esp"))
        self.assertEqual(notes["cycles"], [("B.esp", "A.esp", "mwmanager_rules.txt:1")])

    def test_generated_tail_is_hardwired_and_conditional(self):
        tail = ["XE Sky Variations.esp", "mwdialog_check AutoMerge.esp", "mwdialog_check Cell Fixes.esp",
                "mwdialog_check PlaceholderCell ESP Patch.esp", "mwdialog_check Multipatch.esp"]
        layout = [("Extra.esm", ["Morrowind.esm"])] + [(name, ["Morrowind.esm"]) for name in
                  ["Other.esp", "mwmanager - ESM fixes.esp", "mwmanager Performers Patch.esp", *reversed(tail)]]
        order, _notes = self._sort(layout, [])
        self.assertEqual(order[2], "mwmanager - ESM fixes.esp")
        self.assertEqual(order[-5:], tail)
        self.assertLess(order.index("mwmanager Performers Patch.esp"), order.index("XE Sky Variations.esp"))

        order, _notes = self._sort([item for item in layout if item[0] != tail[2]], [])
        self.assertEqual(order[-4:], [name for name in tail if name != tail[2]])


if __name__ == "__main__":
    unittest.main()
