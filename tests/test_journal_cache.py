"""The install ledger grows to hundreds of MB: recording a change must not read and re-check all of it each time
(asset repairs took ~38 s per moved file on a 670,000-line ledger)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_journal as J  # noqa: E402


class LedgerCache(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.journal = J.Journal(Path(tmp.name) / "journal")
        J._LEDGER_CACHE.clear()

    def record(self, n, start=0):
        for i in range(start, start + n):
            self.journal.record("run1", f"Data Files/t{i}.dds", "create", "ab" * 32, {"kind": "absent"})

    def test_many_records_parse_the_ledger_once(self):
        self.record(3)
        J._LEDGER_CACHE.clear()
        real = Path.read_text
        reads = []
        with mock.patch.object(Path, "read_text", lambda self, *a, **k: reads.append(self.name) or real(self, *a, **k)):
            self.record(20, start=3)
        self.assertEqual(reads.count(self.journal.ledger.name), 1)

    def test_the_cache_matches_a_fresh_read_and_an_outside_change_is_seen(self):
        self.record(5)
        cached, problems = self.journal.entries()
        J._LEDGER_CACHE.clear()
        fresh, fresh_problems = self.journal.entries()
        self.assertEqual((cached, problems), (fresh, fresh_problems))
        self.assertEqual(problems, [])
        self.assertEqual([e["seq"] for e in fresh], [1, 2, 3, 4, 5])
        with open(self.journal.ledger, "a", encoding="utf-8") as f:     # another writer
            f.write('{"broken": true}\n')
        _entries, problems = self.journal.entries()
        self.assertTrue(problems)                                      # read again, not the cached copy

    def test_only_the_first_original_of_a_path_counts_with_the_cache(self):
        orig = {"kind": "gog", "location": "game:Data Files/a.esp", "sha256": "cd" * 32}
        self.journal.record("r", "Data Files/a.esp", "modify", "ef" * 32, orig)
        second = self.journal.record("r", "Data Files/a.esp", "modify", "01" * 32, orig)
        self.assertIsNone(second["original"])


if __name__ == "__main__":
    unittest.main()
