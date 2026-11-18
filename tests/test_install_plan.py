import json
import tempfile
import unittest
from pathlib import Path

from mwmanager_install_plan import build, render


class InstallPlanTests(unittest.TestCase):
    def test_package_master_is_available_to_another_selected_plugin(self):
        import struct
        from mwdialog_tes3 import pack_sub
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager"
            package = manager / "staging" / "plugins" / "Example"
            data = root / "Data Files"
            package.mkdir(parents=True)
            data.mkdir(parents=True)
            (package / "Master.esm").write_bytes(b"TES3" + struct.pack("<III", 0, 0, 0))
            header = pack_sub("MAST", b"Master.esm\0") + pack_sub("DATA", struct.pack("<Q", 123))
            (package / "Dependent.esp").write_bytes(
                b"TES3" + struct.pack("<III", len(header), 0, 0) + header)
            plan = build({"selected_plugins": ["Master.esm", "Dependent.esp"]},
                         package, data, manager)
            self.assertEqual(plan["missing_masters"], [])
            self.assertFalse(plan["blocked"])

    def test_plan_reports_creates_replacements_and_duplicates_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager"
            package = manager / "staging" / "plugins" / "Example"
            data = root / "game" / "Data Files"
            package.mkdir(parents=True)
            data.mkdir(parents=True)
            (package / "Textures").mkdir()
            (package / "Textures" / "new.dds").write_bytes(b"new")
            (package / "Textures" / "replace.dds").write_bytes(b"newer")
            (package / "Textures" / "same.dds").write_bytes(b"same")
            (data / "Textures").mkdir()
            (data / "Textures" / "replace.dds").write_bytes(b"old")
            (data / "Textures" / "same.dds").write_bytes(b"same")
            before = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            plan = build({"archive": "example.zip", "selected_plugins": []}, package, data, manager)
            after = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            self.assertEqual(before, after)
            self.assertEqual(plan["operation_counts"],
                             {"create": 1, "replace": 1, "same-content": 1})
            self.assertFalse(plan["blocked"])
            self.assertIn("READY", "\n".join(render(plan)))

    def test_pending_options_block_the_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager"
            package = manager / "staging" / "plugins" / "Example"
            data = root / "Data Files"
            (manager / "outputs").mkdir(parents=True)
            package.mkdir(parents=True)
            data.mkdir(parents=True)
            (manager / "outputs" / "mwmanager_pending_options.json").write_text(json.dumps({
                "Example": {"patch": {"label": "Patch", "status": "waiting", "needs": ["TR_Data.esm"]}}
            }), encoding="utf-8")
            plan = build({"selected_plugins": ["Example.esp"]}, package, data, manager)
            self.assertTrue(plan["blocked"])
            self.assertTrue(plan["pending_options"])


if __name__ == "__main__":
    unittest.main()
