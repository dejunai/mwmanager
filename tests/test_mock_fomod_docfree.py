"""Options without readme text: shared files make folders alternatives, and the plugin header or the folder's
contents describe them."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod


def plugin(path: Path, description: str):
    hedr = struct.pack("<fI32s256sI", 1.3, 0, b"author", description.encode("cp1252"), 0)
    body = b"HEDR" + struct.pack("<I", len(hedr)) + hedr
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"TES3" + struct.pack("<III", len(body), 0, 0) + body)


class DocFreeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = Path(tmp.name) / "Rocks"
        for folder in ("Rocks 512", "Rocks 1024"):
            texture = self.mod / "Extras" / folder / "textures" / "rock.dds"
            texture.parent.mkdir(parents=True)
            texture.write_bytes(b"dds")
        plugin(self.mod / "Extras" / "Rock Patch" / "RockPatch.esp", "Fixes the rocks near Seyda Neen.")

    def test_header_description_is_read(self):
        self.assertEqual(mock_fomod._plugin_description(self.mod / "Extras" / "Rock Patch" / "RockPatch.esp"),
                         "Fixes the rocks near Seyda Neen.")

    def test_folders_installing_the_same_files_are_alternatives_and_described(self):
        records = {r["path"].name: r for r in mock_fomod._discover(self.mod, set())}
        self.assertEqual(records["Rocks 512"].get("selection_type"), "SelectAtMostOne")
        self.assertEqual(records["Rocks 512"]["fallback_text"], "Contents: 1 texture")
        self.assertEqual(records["Rock Patch"]["fallback_text"],
                         "From the plugin header: Fixes the rocks near Seyda Neen.")


class SeveralCoreFoldersTests(unittest.TestCase):
    def discover(self, readme=""):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mod = Path(tmp.name) / "Pack"
        for folder in ("00 - Meshes", "00 - Textures"):
            (mod / "Extras" / folder / "meshes").mkdir(parents=True)
            (mod / "Extras" / folder / "meshes" / f"{folder[-4:]}.nif").write_bytes(b"nif")
        if readme:
            (mod / "Docs").mkdir()
            (mod / "Docs" / "readme.txt").write_text(readme, encoding="utf-8")
        return mock_fomod._discover(mod, set())

    def test_two_00_folders_are_both_core(self):
        self.assertTrue(all(mock_fomod._is_hidden_required_option(r) for r in self.discover()))

    def test_unless_the_readme_says_choose_one(self):
        records = self.discover("00 - Meshes: choose one of the two.\n00 - Textures: or this one instead of it.\n")
        self.assertFalse(any(mock_fomod._is_hidden_required_option(r) for r in records))


if __name__ == "__main__":
    unittest.main()
