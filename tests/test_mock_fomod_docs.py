"""Readme text is read whole and decoded the way authors actually saved it; lines are normalized only for matching."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod


class ReadDocsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_encodings_are_decoded_not_replaced(self):
        self.assertEqual(mock_fomod._decode_doc("Für".encode("cp1252")), "Für")
        self.assertEqual(mock_fomod._decode_doc("﻿01 Option".encode("utf-8")), "01 Option")
        self.assertEqual(mock_fomod._decode_doc("Option".encode("utf-16")), "Option")

    def test_long_docs_keep_their_later_sections(self):
        (self.root / "readme.txt").write_text("x\n" * 20000 + "07 Late Option - the text\n", encoding="utf-8")
        (self.root / "__MACOSX").mkdir()
        (self.root / "__MACOSX" / "readme.txt").write_text("resource fork", encoding="utf-8")
        text = mock_fomod._read_docs([self.root])
        self.assertIn("07 Late Option - the text", text)
        self.assertNotIn("resource fork", text)

    def test_word_97_text_runs_are_read(self):
        (self.root / "readme.doc").write_bytes(b"\xd0\xcf\x11\xe0\x00\x0001 Core - always install\x00\x01")
        self.assertIn("01 Core - always install", mock_fomod._read_docs([self.root]))

    def test_lines_are_normalized_for_matching_only(self):
        self.assertEqual(mock_fomod._normalize_line("## **01 Core – “Main”**  [b]files[/b]"),
                         '01 core - "main" files')
        excerpt = mock_fomod._relevant_doc_excerpt("intro line here\n[b]01 Core – always install[/b]\n", "Core")
        self.assertIn("[b]01 Core – always install[/b]", excerpt)       # shown as the author wrote it


if __name__ == "__main__":
    unittest.main()
