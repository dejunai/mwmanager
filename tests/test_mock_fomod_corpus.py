"""Hand-checked option texts (tests/mock_fomod_golden.json) that the matcher got right must stay right.

Needs the local corpus (python mock_fomod_corpus.py build); skipped without it, since the readmes are not committed."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod_corpus as corpus


@unittest.skipUnless((corpus.CORPUS / "index.json").is_file(), "corpus not built")
class GoldenOptionTextTests(unittest.TestCase):
    def test_entries_that_passed_still_pass(self):
        golden = json.loads(corpus.GOLDEN.read_text(encoding="utf-8"))["packages"]
        failed = set(corpus.measure()["golden_fail"])
        regressed = [f"{archive} :: {folder}" for archive, options in golden.items()
                     for folder, want in options.items()
                     if want.get("baseline") and f"{archive} :: {folder}" in failed]
        self.assertEqual(regressed, [])


if __name__ == "__main__":
    unittest.main()
