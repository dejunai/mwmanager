import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import mwdialog_luafix as luafix


class LuaBackupPathTests(unittest.TestCase):
    def test_package_backup_uses_a_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Plugin"
            source = root / "MWSE" / "mods" / "example" / "main.lua"
            source.parent.mkdir(parents=True)
            original = b"return true\n"
            source.write_bytes(original)
            report = SimpleNamespace(files={str(source): {
                "mod": "example", "rel": "main.lua", "sha": luafix.sha(original),
                "patch": {"ok": True, "why": "", "text": "return false\n", "rules": ["test"]},
            }})
            backup_root = root / "Off" / "example" / "LuaRepairs"

            written, refused = luafix.apply_report(
                report, journal=str(root / "journal.jsonl"), backup_root=str(backup_root),
                backup_relative_to=str(root), log=lambda _message: None)

            self.assertEqual((written, refused), (1, []))
            backups = list(backup_root.rglob("main.lua"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].relative_to(backup_root).parts[1:],
                             ("MWSE", "mods", "example", "main.lua"))
            self.assertNotIn(source.drive.rstrip(":"), backups[0].relative_to(backup_root).parts)


if __name__ == "__main__":
    unittest.main()
