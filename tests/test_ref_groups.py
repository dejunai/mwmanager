import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mwmanager_upstream  # noqa: E402


class ReferenceGroupFix(unittest.TestCase):
    def test_esm_temporary_reference_is_not_overridden_as_persistent(self):
        self.flip("Clean_Piran.esp", 1, (0, 1, 1))     # the one plugin allowed to flip an .esm's reference

    def test_other_plugins_keep_an_esm_reference_persistent(self):
        """A script finds the object only while the plugin keeps it persistent (huleenBookshelfScript,
        Uvirith_Verena: \"Unable to find referenced object\" and the game quit before the menu)."""
        self.flip("Uvirith's Legacy_3.53.esp", 0, (1, 0, None))

    def flip(self, plugin_name, changed, groups):
        m = mwmanager_upstream.compose()
        pack = m.pack_sub

        def record(tag, body):
            return tag + struct.pack("<III", len(body), 0, 0) + body

        def header(master=None):
            hedr = bytearray(300)
            struct.pack_into("<f", hedr, 0, 1.3)
            body = pack("HEDR", hedr)
            if master:
                body += pack("MAST", master.encode() + b"\0") + pack("DATA", struct.pack("<Q", 1))
            return record(b"TES3", body)

        block = pack("FRMR", struct.pack("<I", 0x015C60)) + pack("NAME", b"door_cavern_doors10\0")
        master_cell = (pack("NAME", b"Piran\0") + pack("DATA", struct.pack("<Iii", 1, 0, 0)) +
                       pack("NAM0", struct.pack("<I", 1)) + block)
        override = pack("FRMR", struct.pack("<I", 0x01015C60)) + pack("NAME", b"door_cavern_doors10\0")
        plugin_cell = pack("NAME", b"Piran\0") + pack("DATA", struct.pack("<Iii", 1, 0, 0)) + override

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "Data Files"
            data_dir.mkdir()
            master = data_dir / "Morrowind.esm"
            plugin = data_dir / plugin_name
            master.write_bytes(header() + record(b"CELL", master_cell))
            plugin.write_bytes(header(master.name) + record(b"CELL", plugin_cell))
            args = types.SimpleNamespace(data_dir=str(data_dir), fix_log="fix.log", dry_run=False)

            def write_inplace(path, data):
                Path(path).write_bytes(b"".join(data))
                return str(Path(path).with_suffix(".bak"))

            with mock.patch.object(m, "write_inplace", side_effect=write_inplace):
                self.assertEqual(m.fix_ref_groups(
                    args, [(master.name, str(master)), (plugin.name, str(plugin))]), changed)

            cell = next(body for _off, tag, _flags, body in m.iter_records(plugin.read_bytes()) if tag == "CELL")
            _head, persistent, nam0, temporary = m.split_cell(cell)
            self.assertEqual((len(persistent), len(temporary), struct.unpack("<I", nam0)[0] if nam0 else None), groups)


if __name__ == "__main__":
    unittest.main()
