import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from mwmanager_record_report import load_conflicts, render


class RecordReportTests(unittest.TestCase):
    def test_distinguishes_identical_and_conflicting_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "catalog.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE esm_record (record_tag TEXT, editor_id_norm TEXT, "
                                   "editor_id TEXT, source_file TEXT, body_sha256 BLOB, load_order INTEGER)")
                rows = [
                    ("NPC_", "npc", "npc", "A.esp", hashlib.sha256(b"a").digest(), 1),
                    ("NPC_", "npc", "npc", "B.esp", hashlib.sha256(b"b").digest(), 2),
                    ("MISC", "coin", "coin", "A.esp", hashlib.sha256(b"same").digest(), 1),
                    ("MISC", "coin", "coin", "B.esp", hashlib.sha256(b"same").digest(), 2),
                ]
                connection.executemany("INSERT INTO esm_record VALUES (?, ?, ?, ?, ?, ?)", rows)
                connection.commit()
            report = load_conflicts(database)
            self.assertEqual(report["records_indexed"], 4)
            self.assertEqual(report["total_conflicts"], 1)
            self.assertEqual(report["identical"], 1)
            conflict = report["conflicts"][0]
            self.assertEqual((conflict["tag"], conflict["winner"]), ("NPC_", "B.esp"))
            self.assertIn("winner: B.esp", "\n".join(render(report)))
            active = load_conflicts(database, active_plugins=["A.esp"])
            self.assertEqual(active["total_conflicts"], 0)
            self.assertEqual(active["records_active"], 2)
            reordered = load_conflicts(database, active_plugins=["B.esp", "A.esp"])
            self.assertEqual(reordered["conflicts"][0]["winner"], "A.esp")

    def test_missing_database_is_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = load_conflicts(Path(tmp) / "missing.sqlite3")
            self.assertIn("missing", report["note"])


if __name__ == "__main__":
    unittest.main()
