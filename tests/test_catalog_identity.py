"""The record catalog keys records by upstream's record_id, not by the first NAME subrecord."""
import sqlite3
import struct
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "database"))
import catalog  # noqa: E402

SCHEMA = ROOT / "database" / "schema.sql"


def sub(tag, data):
    return tag + struct.pack("<I", len(data)) + data


def record(tag, *subs):
    body = b"".join(subs)
    return tag + struct.pack("<III", len(body), 0, 0) + body


def plugin(path, *records):
    path.write_bytes(record(b"TES3", sub(b"HEDR", bytes(300))) + b"".join(records))


def info(inam, text):
    return record(b"INFO", sub(b"INAM", inam + b"\0"), sub(b"NAME", text))


def exterior(x, y, name=b"Ascadian Isles Region"):
    return record(b"CELL", sub(b"NAME", name + b"\0"), sub(b"DATA", struct.pack("<Iii", 2, x, y)))


def land(x, y, height):
    return record(b"LAND", sub(b"INTV", struct.pack("<ii", x, y)), sub(b"VHGT", bytes([height])))


class CatalogIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def conflicts(self, db):
        return {(tag, key): (label, count) for tag, key, label, count in db.execute(
            "SELECT record_tag, editor_id_norm, max(editor_id), COUNT(DISTINCT source_file) FROM esm_record "
            "WHERE editor_id_norm IS NOT NULL GROUP BY record_tag, editor_id_norm "
            "HAVING COUNT(DISTINCT source_file) > 1")}

    def test_info_cell_and_land_are_paired_by_their_real_identity(self):
        plugin(self.dir / "A.esp", info(b"111", b"Hello."), exterior(1, 2), land(1, 2, 1))
        plugin(self.dir / "B.esp", info(b"222", b"Hello."), info(b"111", b"Changed."),
               exterior(3, 4), land(1, 2, 9))
        with closing(sqlite3.connect(self.dir / "c.db")) as db:
            catalog.create_schema(db, SCHEMA)
            catalog.index_esm(db, self.dir / "A.esp", 0)
            catalog.index_esm(db, self.dir / "B.esp", 1)
            found = self.conflicts(db)
        # The shared text "Hello." pairs nothing, and same-name cells at other grids do not either.
        self.assertEqual(set(found), {("INFO", "111"), ("LAND", "0100000002000000")})
        self.assertEqual(found[("LAND", "0100000002000000")][0], "(1, 2)")

    def test_an_old_catalog_is_rekeyed_in_place(self):
        plugin(self.dir / "A.esp", exterior(5, 6))
        with closing(sqlite3.connect(self.dir / "c.db")) as db:
            catalog.create_schema(db, SCHEMA)
            catalog.index_esm(db, self.dir / "A.esp", 0)
            db.execute("UPDATE esm_record SET editor_id_norm = 'ascadian isles region'")
            db.execute("PRAGMA user_version = 0")
            catalog.create_schema(db, SCHEMA)
            self.assertEqual(db.execute("SELECT editor_id_norm, editor_id FROM esm_record WHERE record_tag='CELL'")
                             .fetchone(), ("e|5|6", "Ascadian Isles Region (5, 6)"))


if __name__ == "__main__":
    unittest.main()
