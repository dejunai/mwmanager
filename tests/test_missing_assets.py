"""Tidy resolves assets a staged plugin names but its package doesn't ship: Data Files, the install batch,
Data Files\\Off, else a flagged stand-in in the package. Every path is a temporary folder."""
import contextlib
import hashlib
import io
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402


def sub(name, text):
    data = text.encode("ascii") + b"\0"
    return name.encode("ascii") + struct.pack("<I", len(data)) + data


def record(tag, *subs):
    body = b"".join(subs)
    return tag.encode("ascii") + struct.pack("<III", len(body), 0, 0) + body


def plugin(path, *records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(record("TES3", sub("HEDR", "x")) + b"".join(records))


class MissingAssetsAtStaging(unittest.TestCase):
    def setUp(self):
        import mwdialog_assets
        import mwmanager_install
        self.t = mwmanager_install
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.manager = base / "MWManager"
        self.game = base / "Game"
        self.data = self.game / "Data Files"
        self.data.mkdir(parents=True)
        (self.manager / "outputs").mkdir(parents=True)
        self.staging = self.manager / "staging"
        self.patch = mwmanager_upstream.use_manager_dir(self.manager)
        self.patch.__enter__()
        self.cache = mwdialog_assets.AssetCache(str(self.manager))
        self.env = mock.patch.dict(os.environ, {"MWMANAGER_INSTALL_BATCH": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.cache.close()
        self.patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def book(self, package, icon, name="Cyr.esm", staging=None):
        plugin((staging or self.staging) / package / name,
               record("BOOK", sub("NAME", "bk_test"), sub("ITEX", icon)))

    def run_tidy(self, mods, staging=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ops = self.t.resolve_missing_assets(str(staging or self.staging), mods, str(self.data), self.cache)
        return ops, [ln for ln in out.getvalue().splitlines() if ln]

    def test_asset_in_data_files_needs_nothing(self):
        (self.data / "Icons" / "m").mkdir(parents=True)
        (self.data / "Icons" / "m" / "coat.dds").write_bytes(b"DDS real")
        self.book("Cyr", "m\\coat.tga")                     # the game loads the .dds for a .tga
        ops, lines = self.run_tidy(["Cyr"])
        self.assertEqual(ops, {})
        self.assertFalse((self.staging / "Cyr" / "Icons").exists())
        self.assertEqual(len(lines), 1)
        self.assertIn("1 already in Data Files", lines[0])

    def test_asset_shipped_by_another_package_of_the_batch(self):
        self.book("Cyr", "m\\coat.tga")
        td = self.staging / "Tamriel_Data" / "Icons" / "m" / "coat.dds"
        td.parent.mkdir(parents=True)
        td.write_bytes(b"DDS real")
        os.environ["MWMANAGER_INSTALL_BATCH"] = os.pathsep.join(["Tamriel_Data", "Cyr"])
        ops, lines = self.run_tidy(["Cyr"])
        self.assertEqual(ops, {})
        self.assertEqual(len(lines), 1)
        self.assertIn("1 shipped by another package", lines[0])

    def test_staged_major_masters_are_one_batch(self):
        majors = self.staging / "major_masters"
        self.book("Cyr", "m\\coat.dds", staging=majors)
        td = majors / "TD" / "Icons" / "m" / "coat.dds"
        td.parent.mkdir(parents=True)
        td.write_bytes(b"DDS real")
        ops, lines = self.run_tidy(["Cyr"], staging=majors)
        self.assertEqual(ops, {})
        self.assertIn("1 shipped by another package", lines[0])

    def test_unused_original_in_off_is_moved_back(self):
        import mwmanager_journal
        original = self.data / "Sound" / "Cr" / "kgout" / "moan2.wav"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"RIFF vanilla")
        mwmanager_journal.Journal(self.manager / "journal").capture_baseline(self.game)
        kept = self.data / "Off" / "Original Install" / "Unused assets" / "Sound" / "Cr" / "kgout" / "moan2.wav"
        kept.parent.mkdir(parents=True)
        os.replace(original, kept)
        plugin(self.staging / "TR" / "TR_Mainland.esm", record("SOUN", sub("NAME", "moan"), sub("FNAM", "Cr\\kgout\\moan2.wav")))
        ops, lines = self.run_tidy(["TR"])
        self.assertEqual(original.read_bytes(), b"RIFF vanilla")
        self.assertFalse(kept.exists())
        self.assertEqual(ops, {})
        self.assertIn("1 restored from Data Files\\Off", lines[0])

    def test_replaced_original_in_off_is_copied_back_and_journaled(self):
        import mwmanager_journal
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.game)
        kept = self.data / "Off" / "bsa packed" / "Meshes" / "x" / "door.nif"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"NIF real")
        plugin(self.staging / "TR" / "TR.esm", record("DOOR", sub("NAME", "door"), sub("MODL", "x\\door.nif")))
        ops, lines = self.run_tidy(["TR"])
        placed = self.data / "Meshes" / "x" / "door.nif"
        self.assertEqual(placed.read_bytes(), b"NIF real")
        self.assertIn("1 restored from Data Files\\Off", lines[0])
        plan = journal.plan_rollback(self.game)
        self.assertIn(("delete", "Data Files/Meshes/x/door.nif"), {(s["op"], s["path"]) for s in plan["steps"]})

    def test_otherwise_a_flagged_stand_in_is_built_in_the_package(self):
        self.book("Cyr", "m\\misc_com_coatofarms_01.tga")
        ops, lines = self.run_tidy(["Cyr"])
        built = self.staging / "Cyr" / "Icons" / "m" / "misc_com_coatofarms_01.dds"
        self.assertEqual(built.read_bytes(), self.t._scanner.stand_in_bytes(str(built.relative_to(self.staging / "Cyr"))))
        sha = hashlib.sha256(built.read_bytes()).hexdigest()
        self.assertEqual(ops, {"Cyr": [{"generated": str(built), "sha256": sha}]})
        flag = self.cache.get(self.t.GENERATED_KEY + str(built), sha)
        self.assertEqual((flag["package"], flag["rel"].lower()), ("Cyr", "icons\\m\\misc_com_coatofarms_01.dds"))
        self.assertEqual(len(lines), 1)
        self.assertIn("1 stand-in(s) built, flagged", lines[0])
        _ops, again = self.run_tidy(["Cyr"])               # a second run: the package ships it now
        self.assertEqual(again, [])

    def test_real_file_from_a_later_package_replaces_the_stand_in(self):
        self.book("Cyr", "m\\coat.dds")
        self.run_tidy(["Cyr"])
        built = self.staging / "Cyr" / "Icons" / "m" / "coat.dds"
        self.assertTrue(built.exists())
        real = self.staging / "TD" / "Icons" / "m" / "coat.dds"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"DDS real")
        _ops, lines = self.run_tidy(["TD"])
        self.assertFalse(built.exists())
        self.assertFalse(any(k.startswith(self.t.GENERATED_KEY) for k in self.cache.d))
        self.assertIn("earlier stand-in(s) removed", lines[0])

    def test_one_status_line_per_resolution_class(self):
        (self.data / "Icons").mkdir()
        (self.data / "Icons" / "a.dds").write_bytes(b"x")
        (self.data / "Icons" / "b.dds").write_bytes(b"x")
        plugin(self.staging / "Cyr" / "Cyr.esm",
               record("BOOK", sub("NAME", "a"), sub("ITEX", "a.dds")),
               record("BOOK", sub("NAME", "b"), sub("ITEX", "b.dds")),
               record("BOOK", sub("NAME", "c"), sub("ITEX", "c.dds")),
               record("BOOK", sub("NAME", "d"), sub("ITEX", "d.dds")))
        _ops, lines = self.run_tidy(["Cyr"])
        self.assertEqual(len(lines), 2)
        self.assertIn("2 already in Data Files", lines[0])
        self.assertIn("2 stand-in(s) built", lines[1])


if __name__ == "__main__":
    unittest.main()
