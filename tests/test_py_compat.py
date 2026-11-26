"""py_compat: the Python 3.9 stand-ins behave like the 3.10+ standard library calls they replace."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import py_compat  # noqa: E402


class WriteText(unittest.TestCase):
    def test_newline_is_applied_and_data_round_trips(self):
        with py_compat.TemporaryDirectory() as tmp:
            path = Path(tmp, "rules.txt")
            written = py_compat.write_text(path, "a\nb\n", encoding="cp1252", newline="\r\n")
            self.assertEqual(written, 4)
            self.assertEqual(path.read_bytes(), b"a\r\nb\r\n")
            py_compat.write_text(path, "a\nb\n", encoding="utf-8", newline="\n")
            self.assertEqual(path.read_bytes(), b"a\nb\n")

    def test_errors_argument_is_passed_through(self):
        with py_compat.TemporaryDirectory() as tmp:
            path = Path(tmp, "x.txt")
            py_compat.write_text(path, "caf\udce9", encoding="utf-8", errors="surrogateescape", newline="")
            self.assertEqual(path.read_bytes(), b"caf\xe9")


class TemporaryDirectory(unittest.TestCase):
    def test_ignore_cleanup_errors_keyword_is_accepted_and_the_folder_goes_away(self):
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        Path(tmp.name, "f.txt").write_text("x")
        name = tmp.name
        tmp.cleanup()
        self.assertFalse(Path(name).exists())
        tmp.cleanup()                       # a second cleanup is harmless

    def test_context_manager_removes_the_folder(self):
        with py_compat.TemporaryDirectory(ignore_cleanup_errors=True) as name:
            self.assertTrue(Path(name).is_dir())
        self.assertFalse(Path(name).exists())


class Toml(unittest.TestCase):
    SAMPLE = '# mge\n[distant_land]\nper_pixel_lighting = true # on\nname = "a # b"\ncount = 1_000\nratio = 0.5\n[a.b]\nflag = false\n'
    EXPECTED = {"distant_land": {"per_pixel_lighting": True, "name": "a # b", "count": 1000, "ratio": 0.5},
                "a": {"b": {"flag": False}}}

    def test_toml_loads(self):
        self.assertEqual(py_compat.toml_loads(self.SAMPLE), self.EXPECTED)

    def test_fallback_reader_matches_for_flat_tables(self):
        self.assertEqual(py_compat._simple_toml(self.SAMPLE), self.EXPECTED)

    def test_fallback_reader_refuses_what_it_cannot_read(self):
        for text in ("a = [1, 2]\n", "a = {b = 1}\n", 'a = """x\ny"""\n', "a = nonsense\n"):
            with self.assertRaises(py_compat._FallbackTOMLError):
                py_compat._simple_toml(text)


if __name__ == "__main__":
    unittest.main()