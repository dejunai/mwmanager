"""CR-26 stress run, loop 3: saved major-master record-conflict choices reach the ESM fixes patch the way the
review shows them. Exterior cells ('Name (x, y)'), LAND, PGRD, SCPT and SKIL/MGEF were looked up by NAME and
silently dropped; an interior CELL was copied with all its references, so the patch placed every object in it
again as its own new reference. Every path here is a temporary folder."""
import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402
import py_compat  # noqa: E402


def sub(tag, data):
    return struct.pack("<4sI", tag.encode("ascii"), len(data)) + data


def z(text):
    return text.encode("latin-1") + b"\0"


def record(tag, *subs):
    body = b"".join(subs)
    return struct.pack("<4sIII", tag.encode("ascii"), len(body), 0, 0) + body


def cell(name, x, y, interior, refs, region=""):
    head = [sub("NAME", z(name)), sub("DATA", struct.pack("<Iii", 1 if interior else 0, x, y))]
    if region:
        head.append(sub("RGNN", z(region)))
    placed = []
    for index, obj in refs:
        placed += [sub("FRMR", struct.pack("<I", index)), sub("NAME", z(obj)),
                   sub("DATA", struct.pack("<6f", 0, 0, 0, 0, 0, 0))]
    return record("CELL", *head, *placed)


def plugin(path, *records):
    header = record("TES3", sub("HEDR", struct.pack("<fI32s256sI", 1.3, 1, b"", b"", len(records))))
    path.write_bytes(header + b"".join(records))


class ConflictOverrides(unittest.TestCase):
    def setUp(self):
        self.m = mwmanager_upstream.compose()
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.master = self.root / "Morrowind.esm"
        plugin(self.master,
               cell("Ebonheart", 2, -13, False, [(1, "ex_door"), (2, "ex_wall")], "Ascadian Isles Region"),
               cell("T_Test_PC", 0, 0, True, [(i, f"obj_{i}") for i in range(1, 6)]),
               record("LAND", sub("INTV", struct.pack("<ii", 2, -13)), sub("DATA", struct.pack("<I", 1))),
               record("PGRD", sub("DATA", struct.pack("<iiHH", 2, -13, 0, 0)), sub("NAME", z("Ebonheart"))),
               record("SCPT", sub("SCHD", b"MainScript".ljust(32, b"\0") + bytes(20)), sub("SCTX", b"begin")),
               record("SKIL", sub("INDX", struct.pack("<i", 3)), sub("SKDT", bytes(24))),
               record("NPC_", sub("NAME", z("jiub")), sub("FNAM", z("Jiub"))))
        self.paths = {"morrowind.esm": str(self.master)}

    def overrides(self, *choices):
        saved = self.root / "mwmanager_major_master_conflicts.json"
        saved.write_text(json.dumps({"version": 1, "resolutions": [
            {"record_tag": t, "editor_id": e, "winner": w} for t, e, w in choices]}), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(self.m.major_master_conflict_overrides.__globals__,
                             {"manager_file": lambda name: str(self.root / name)}), contextlib.redirect_stdout(out):
            records, winners = self.m.major_master_conflict_overrides(self.paths)
        return records, winners, out.getvalue()

    def tags_and_subs(self, raw):
        tag = raw[:4].decode("ascii")
        body = raw[16:16 + struct.unpack_from("<I", raw, 4)[0]]
        return tag, [name for name, _data in self.m.iter_subs(body)], body

    def test_every_label_the_review_shows_is_found(self):
        records, winners, printed = self.overrides(
            ("CELL", "Ebonheart (2, -13)", "Morrowind.esm"), ("CELL", "T_Test_PC", "Morrowind.esm"),
            ("LAND", "(2, -13)", "Morrowind.esm"), ("PGRD", "Ebonheart (2, -13)", "Morrowind.esm"),
            ("SCPT", "MainScript", "Morrowind.esm"), ("SKIL", "3", "Morrowind.esm"),
            ("NPC_", "Jiub", "Morrowind.esm"))
        self.assertEqual(sorted(self.tags_and_subs(r)[0] for r in records),
                         ["CELL", "CELL", "LAND", "NPC_", "PGRD", "SCPT", "SKIL"])
        self.assertEqual(winners, ["Morrowind.esm"] * 7)
        self.assertNotIn("not applied", printed)

    def test_a_cell_is_copied_without_its_references(self):
        records, _w, _p = self.overrides(("CELL", "Ebonheart (2, -13)", "Morrowind.esm"),
                                         ("CELL", "T_Test_PC", "Morrowind.esm"))
        for raw in records:
            tag, names, body = self.tags_and_subs(raw)
            self.assertEqual(tag, "CELL")
            self.assertNotIn("FRMR", names)
            self.assertEqual(struct.unpack_from("<I", raw, 4)[0], len(body))
        ext = self.tags_and_subs(records[0])
        self.assertEqual(ext[1], ["NAME", "DATA", "RGNN"])

    def test_choices_that_cannot_be_applied_are_reported(self):
        records, _w, printed = self.overrides(("CELL", "Nowhere (9, 9)", "Morrowind.esm"),
                                              ("INFO", "123456789", "Morrowind.esm"),
                                              ("NPC_", "jiub", "Missing.esm"))
        self.assertEqual(records, [])
        self.assertIn("CELL Nowhere (9, 9) - not found in Morrowind.esm", printed)
        self.assertIn("INFO 123456789", printed)
        self.assertIn("Missing.esm is not in the load order", printed)

    def test_no_saved_choices_means_no_records(self):
        with mock.patch.dict(self.m.major_master_conflict_overrides.__globals__,
                             {"manager_file": lambda name: str(self.root / name)}):
            self.assertEqual(self.m.major_master_conflict_overrides(self.paths), ([], []))


if __name__ == "__main__":
    unittest.main()
