import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mwmanager_upstream  # noqa: E402


class LateReferenceScaleRepair(unittest.TestCase):
    def plugin(self, tidy):
        pack = tidy._scanner.pack_sub
        hedr = bytearray(300)
        struct.pack_into("<fI", hedr, 0, 1.3, 0)
        struct.pack_into("<I", hedr, 296, 1)
        head = pack("HEDR", bytes(hedr))
        cell = (pack("NAME", b"\0") + pack("DATA", struct.pack("<Iii", 0, 11, -57))
                + pack("FRMR", struct.pack("<I", 8103)) + pack("NAME", b"flora_tree_gl_08\0")
                + pack("DATA", b"\0" * 24) + pack("XSCL", struct.pack("<f", 1.25)))
        record = lambda tag, body: tag + struct.pack("<III", len(body), 0, 0) + body
        return record(b"TES3", head) + record(b"CELL", cell)

    def test_xscl_after_reference_data_is_moved_before_it(self):
        tidy = mwmanager_upstream.compose_tidy()
        plugin = self.plugin(tidy)

        repaired, count = tidy._repair_late_reference_scales(plugin)

        self.assertEqual(count, 1)
        cell_body = next(body for _off, tag, _flags, body in tidy._scanner.iter_records(repaired) if tag == "CELL")
        self.assertEqual([name for name, _data in tidy._scanner.iter_subs(cell_body)],
                         ["NAME", "DATA", "FRMR", "NAME", "XSCL", "DATA"])
        self.assertEqual(tidy._repair_late_reference_scales(repaired), (repaired, 0))

    def test_exact_duplicate_placement_is_kept_in_its_coordinate_cell(self):
        tidy = mwmanager_upstream.compose_tidy()
        pack = tidy._scanner.pack_sub
        placement = (pack("FRMR", struct.pack("<I", 0x03B13D)) +
                     pack("NAME", b"ex_cave_entrance_wg_01\0") +
                     pack("DATA", struct.pack("<ffffff", 227365.625, -207674.96875, 1360.35205078125,
                                               0.0, 0.0, 0.5415015)))
        def cell(x, y):
            body = (pack("NAME", b"\0") + pack("DATA", struct.pack("<Iii", 0, x, y)) +
                    pack("NAM0", struct.pack("<I", 1)) + placement)
            return b"CELL" + struct.pack("<III", len(body), 0, 0) + body
        plugin = self.plugin(tidy).split(b"CELL", 1)[0] + cell(27, -24) + cell(27, -26)

        repaired, count, unresolved = tidy._repair_duplicate_exterior_references(plugin)

        self.assertEqual((count, unresolved), (1, 0))
        cells = [body for _off, tag, _flags, body in tidy._scanner.iter_records(repaired) if tag == "CELL"]
        self.assertEqual([[name for name, _data in tidy._scanner.iter_subs(body)] for body in cells],
                         [["NAME", "DATA", "NAM0"], ["NAME", "DATA", "NAM0", "FRMR", "NAME", "DATA"]])
        self.assertEqual(struct.unpack("<I", list(tidy._scanner.iter_subs(cells[0]))[-1][1])[0], 0)

    def test_plan_does_not_write_and_apply_keeps_an_undo_backup(self):
        tidy = mwmanager_upstream.compose_tidy()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, package = root / "manager", root / "staging" / "Border Fix"
            manager.mkdir()
            package.mkdir(parents=True)
            plugin = package / "Border Fix.esp"
            original = self.plugin(tidy)
            plugin.write_bytes(original)
            with mwmanager_upstream.use_manager_dir(manager):
                tidy._TIDY_BACKUP_CACHE_DATA = None
                self.assertEqual(tidy.repair_malformed_cell_references(str(root / "staging"), apply_changes=False), 1)
                self.assertEqual(plugin.read_bytes(), original)
                self.assertEqual(tidy.repair_malformed_cell_references(str(root / "staging"), apply_changes=True), 1)
            self.assertNotEqual(plugin.read_bytes(), original)
            backups = list((package / "Off").rglob("Border Fix.esp"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
