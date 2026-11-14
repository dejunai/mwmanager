"""Patch for Purists is a major master: first in MWManager's priority list (right after Bloodmoon.esm), and
mwmanager_rules.txt has the sort rule that puts it there. Temporary folders only; nothing real is touched."""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import core_addons  # noqa: E402
import mwmanager_upstream  # noqa: E402

PFP = "Patch for Purists.esm"


def plugin(path, masters):
    def sub(n, d):
        return n.encode() + struct.pack("<I", len(d)) + d
    body = sub("HEDR", struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", 0))
    for m in masters:
        body += sub("MAST", m.encode() + b"\0") + sub("DATA", struct.pack("<Q", 1))
    Path(path).write_bytes(b"TES3" + struct.pack("<III", len(body), 0, 0) + body)


class PriorityList(unittest.TestCase):
    def test_patch_for_purists_is_the_first_major_master(self):
        self.assertEqual(next(iter(core_addons.BUILTIN_MASTERS)), PFP.lower())
        shipped = json.loads((ROOT / "mwmanager_core_addons.json").read_text(encoding="utf-8"))
        self.assertEqual(next(iter(shipped)), PFP.lower())
        self.assertEqual(set(shipped), set(core_addons.BUILTIN_MASTERS) | {"cyr_main.esm", "sky_main.esm"})
        with tempfile.TemporaryDirectory() as tmp:            # no JSON yet: the built-in list
            self.assertEqual(core_addons.configured_master_names(tmp)[0], PFP.lower())


class SortPlacesIt(unittest.TestCase):
    def test_rules_file_puts_it_right_after_bloodmoon(self):
        import mwmanager as m
        with tempfile.TemporaryDirectory() as tmp, mwmanager_upstream.use_manager_dir(tmp):
            df = Path(tmp) / "game" / "Data Files"
            df.mkdir(parents=True)
            layout = [("Morrowind.esm", []), ("Tribunal.esm", ["Morrowind.esm"]), ("Bloodmoon.esm", ["Morrowind.esm"]),
                      ("AATL_Data.esm", ["Morrowind.esm"]), ("Tamriel_Data.esm", ["Morrowind.esm"]),
                      ("OAAB_Data.esm", ["Morrowind.esm"]), (PFP, ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"]),
                      ("A Mod.esp", ["Morrowind.esm"])]
            plugins = []
            for name, masters in layout:
                plugin(df / name, masters)
                plugins.append((name, str(df / name)))
            rules = m.parse_rules(str(ROOT / "mwmanager_rules.txt"))[0]          # the shipped rules file
            self.assertTrue(any(kind == "nearstart" and PFP in entries for kind, entries, _src in rules))
            order, _notes = m.compute_order(plugins, [rules], [n for n, _p in plugins])
        self.assertEqual(order[:4], ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm", PFP])
        self.assertLess(order.index("Tamriel_Data.esm"), order.index("OAAB_Data.esm"))


class LongRunningCaches(unittest.TestCase):
    def test_mastercheck_caches_can_be_cleared(self):
        import mwdialog_mastercheck as mc
        mc._STAGING_INDEX["x"] = {}
        mc.clear_caches()
        self.assertEqual(mc._STAGING_INDEX, {})


if __name__ == "__main__":
    unittest.main()
