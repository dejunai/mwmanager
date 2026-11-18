import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mwmanager_profiles import apply, compare, export, list_profiles, save


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = Path(self.temporary.name)
        (self.manager / "outputs").mkdir()
        (self.manager / "staging" / "plugins").mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def test_save_compare_and_apply_managed_choices(self):
        order = self.manager / "mwmanager_sort_order.txt"
        order.write_text("Morrowind.esm\nA.esp\n", encoding="utf-8")
        settings = self.manager / "mwmanager_gui_settings.json"
        settings.write_text(json.dumps({"theme": "dark", "nexus_api_key": "never-save-this"}),
                           encoding="utf-8")
        saved = save(self.manager, "Balanced", note="test profile")
        data = json.loads(saved.read_text("utf-8"))
        self.assertNotIn("never-save-this", saved.read_text("utf-8"))
        self.assertEqual(data["files"]["mwmanager_sort_order.txt"]["data"].splitlines(),
                         ["Morrowind.esm", "A.esp"])
        self.assertEqual(list_profiles(self.manager)[0]["name"], "Balanced")

        order.write_text("Morrowind.esm\nB.esp\n", encoding="utf-8")
        delta = compare(self.manager, "Balanced")
        self.assertIn("mwmanager_sort_order.txt", delta["changed"])
        restored = apply(self.manager, "Balanced")
        self.assertEqual(order.read_text("utf-8").splitlines(), ["Morrowind.esm", "A.esp"])
        self.assertTrue(Path(restored["backup_dir"]).is_dir())
        self.assertEqual(json.loads(settings.read_text("utf-8"))["nexus_api_key"],
                         "never-save-this")

    def test_apply_rejects_tampered_profile_content(self):
        order = self.manager / "mwmanager_sort_order.txt"
        order.write_text("A.esp\n", encoding="utf-8")
        path = save(self.manager, "A")
        value = json.loads(path.read_text("utf-8"))
        value["files"]["mwmanager_sort_order.txt"]["data"] = "Tampered.esp\n"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash"):
            apply(self.manager, "A", backup=False)
        self.assertEqual(order.read_text("utf-8"), "A.esp\n")

    def test_export_preserves_profile(self):
        (self.manager / "mwmanager_rules.txt").write_text("[Order]\nA.esp\n", encoding="utf-8")
        save(self.manager, "Export")
        destination = self.manager / "copy.json"
        export(self.manager, "Export", destination)
        self.assertEqual(json.loads(destination.read_text("utf-8"))["name"], "Export")

    def test_apply_rolls_back_if_a_later_write_fails(self):
        order = self.manager / "mwmanager_sort_order.txt"
        rules = self.manager / "mwmanager_rules.txt"
        order.write_text("A.esp\n", encoding="utf-8")
        rules.write_text("[Order]\nA.esp\n", encoding="utf-8")
        save(self.manager, "Atomic")
        order.write_text("B.esp\n", encoding="utf-8")
        rules.write_text("[Order]\nB.esp\n", encoding="utf-8")
        original = order.read_bytes(), rules.read_bytes()
        real_write = __import__("mwmanager_profiles")._atomic_write

        calls = 0

        def fail_second(path, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            real_write(path, data)

        with mock.patch("mwmanager_profiles._atomic_write", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "disk full"):
                apply(self.manager, "Atomic", backup=False)
        self.assertEqual((order.read_bytes(), rules.read_bytes()), original)


if __name__ == "__main__":
    unittest.main()
