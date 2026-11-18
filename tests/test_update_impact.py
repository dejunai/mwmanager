import tempfile
import unittest
from pathlib import Path

from mwmanager_update_impact import compare, risk_score


class UpdateImpactTests(unittest.TestCase):
    def test_reports_file_churn_and_removed_plugin_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp, "old"), Path(tmp, "new")
            old.mkdir()
            new.mkdir()
            (old / "Textures").mkdir()
            (new / "Textures").mkdir()
            (old / "Textures" / "same.dds").write_bytes(b"same")
            (new / "Textures" / "same.dds").write_bytes(b"same")
            (old / "Textures" / "changed.dds").write_bytes(b"old")
            (new / "Textures" / "changed.dds").write_bytes(b"new")
            (old / "Textures" / "removed.dds").write_bytes(b"gone")
            (new / "Textures" / "added.dds").write_bytes(b"fresh")
            (old / "Old.esp").write_bytes(b"TES3" + b"\0" * 12)
            (new / "New.esp").write_bytes(b"TES3" + b"\0" * 12)
            before_old = {p: p.read_bytes() for p in old.rglob("*") if p.is_file()}
            before_new = {p: p.read_bytes() for p in new.rglob("*") if p.is_file()}
            report = compare(old, new)
            self.assertEqual(report["files"]["added"],
                             ["New.esp", "Textures/added.dds"])
            self.assertEqual(report["files"]["removed"],
                             ["Old.esp", "Textures/removed.dds"])
            self.assertEqual(report["files"]["changed"], ["Textures/changed.dds"])
            self.assertEqual(risk_score(report), 27)
            self.assertEqual({p: p.read_bytes() for p in old.rglob("*") if p.is_file()}, before_old)
            self.assertEqual({p: p.read_bytes() for p in new.rglob("*") if p.is_file()}, before_new)


if __name__ == "__main__":
    unittest.main()
