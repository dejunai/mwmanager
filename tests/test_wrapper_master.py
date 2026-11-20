"""A master inside a bare "Data Files" wrapper is bad packaging, not an optional file."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod


class WrapperMasterTests(unittest.TestCase):
    def test_master_in_data_files_wrapper_is_not_an_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / "Sound_Data"
            (mod / "Data Files").mkdir(parents=True)
            (mod / "Data Files" / "Sound_Data.esm").write_bytes(b"")
            (mod / "Optional").mkdir()
            (mod / "Optional" / "Extra.esm").write_bytes(b"")
            labels = [r["label"] for r in mock_fomod._discover_plugin_choices(mod, set())]
            self.assertNotIn(str(Path("Data Files") / "Sound_Data.esm"), labels)
            self.assertIn("Optional", labels)


if __name__ == "__main__":
    unittest.main()
