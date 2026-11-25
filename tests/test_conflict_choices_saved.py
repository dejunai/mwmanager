"""Major-master record conflicts: every 'Save and next batch' writes the choices file, so closing the window after
any batch keeps what was reviewed (it used to be written only after a severity's last batch: closing earlier dropped
every pick). Only real overrides are stored; picking the load-order winner again removes an earlier override."""
import inspect
import json
import tempfile
import unittest
from pathlib import Path

import mwmanager_gui as g


class Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def row(tag, editor_id, winner, default):
    return {"record_tag": tag, "editor_id": editor_id, "winner": Var(winner), "default": default}


class ConflictChoicesSavedTests(unittest.TestCase):
    def test_merge_keeps_only_overrides(self):
        saved = {("CELL", "Old (1, 1)"): "Morrowind.esm"}
        g.merge_conflict_choices(saved, [
            row("CELL", "Ebonheart (2, -13)", "Morrowind.esm", "Cyr_Main.ESM"),       # override
            row("CELL", "Gnisis (-11, 11)", "tr_mainland.esm", "tr_mainland.esm"),    # load order: not stored
            row("CELL", "Old (1, 1)", "tamriel_data.esm", "tamriel_data.esm"),        # back to load order
        ])
        self.assertEqual(saved, {("CELL", "Ebonheart (2, -13)"): "Morrowind.esm"})

    def test_write_is_what_the_esm_patch_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mwmanager_major_master_conflicts.json"
            g.write_conflict_choices(path, {("CELL", "T_Test_PC"): "tamriel_data.esm"})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data, {"version": 1, "resolutions": [
                {"record_tag": "CELL", "editor_id": "T_Test_PC", "winner": "tamriel_data.esm"}]})
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_each_batch_is_written_before_the_next_one_shows(self):
        source = inspect.getsource(g.App._resolve_major_master_conflicts) \
            if hasattr(g, "App") else Path(g.__file__).read_text(encoding="utf-8")
        body = source[source.index("def save_batch"):source.index("def nonlocal_page_next")]
        self.assertLess(body.index("write_conflict_choices(choices_path, saved)"), body.index("nonlocal_page_next()"))
        self.assertIn('"default": sources[-1]', source)


if __name__ == "__main__":
    unittest.main()