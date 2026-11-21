"""An option that needs another mod starts its description with "Requires: <plugin>, Nexus <id>": from its plugins'
masters (and the mods its notes name), the Nexus id from the user's pick, the shipped table, the cache, else the
archive index."""
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mock_fomod
import needed_mods


def plugin(path: Path, *masters: str) -> Path:
    subs = b"".join(b"MAST" + struct.pack("<I", len(m) + 1) + m.encode() + b"\0" for m in masters)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"TES3" + struct.pack("<I", len(subs)) + b"\0" * 8 + subs)
    return path


class OptionRequiresTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.manager = self.root / "manager"
        (self.manager / "outputs").mkdir(parents=True)
        (self.manager / "outputs" / "mwmanager_archive_plugins.json").write_text(json.dumps({"version": 1, "archives": {
            "Big Mod-12345-1-0-1700000000.7z": {"stamp": "1|1", "plugins": ["Big Mod.esm"]},
            "Patch Bundle-999-1-0-1700000000.7z": {"stamp": "1|1", "plugins": ["Big Mod.esm", "Other.esp"]},
        }}), encoding="utf-8")
        self.shipped = self.root / "shipped.json"
        self.shipped.write_text(json.dumps({"version": 1, "plugins": {
            "shipped.esm": {"name": "Shipped.esm", "nexus_id": 777, "archive": ""}}}), encoding="utf-8")
        patcher = mock.patch.object(needed_mods, "REQUIRES_FILE", self.shipped)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cache(self):
        return json.loads((self.manager / "outputs" / needed_mods.REQUIRES_CACHE).read_text(encoding="utf-8"))

    def test_sources_come_from_shipped_table_then_index_and_are_cached(self):
        found = needed_mods.requirement_sources(["shipped.esm", "big mod.esm", "Missing.esp"], self.manager)
        self.assertEqual([(f["name"], f["nexus_id"]) for f in found],
                         [("Shipped.esm", 777), ("Big Mod.esm", 12345), ("Missing.esp", None)])
        self.assertEqual(set(self.cache()["plugins"]), {"big mod.esm"})     # unknowns are worked out again later

    def test_the_users_pick_wins_and_finds_that_archive(self):
        downloads = self.root / "downloads"
        downloads.mkdir()
        (downloads / "Patch Bundle-999-1-0-1700000000.7z").write_bytes(b"")
        needed_mods.set_requirement_source("Big Mod.esm", "Patch Bundle-999-1-0-1700000000.7z", 999, self.manager)
        self.assertEqual(needed_mods.requirement_sources(["Big Mod.esm"], self.manager)[0]["nexus_id"], 999)
        found = needed_mods.find_needed(["Big Mod.esm"], downloads, self.manager / "outputs" / "none.json",
                                        self.manager)
        self.assertEqual(found["Big Mod.esm"].name, "Patch Bundle-999-1-0-1700000000.7z")

    def test_requirements_are_masters_and_noted_mods_not_base_game_or_own_plugins(self):
        mod = self.root / "Some Mod-555"
        plugin(mod / "Main.esp", "Morrowind.esm")
        patch = plugin(mod / "02 Patch" / "Patch.esp", "Morrowind.esm", "Main.esp", "Big Mod.esm")
        record = {"key": "02 Patch", "label": "Patch", "path": patch.parent, "mod_dir": mod,
                  "payload_files": {"Patch.esp": patch}, "targets": ["main.esp", "big mod.esm"]}
        with mock.patch.object(mock_fomod, "_maintainer_needs", return_value=["Dark Molag Mar (mod)"]):
            mock_fomod._attach_requires([record], mod, self.manager)
        self.assertEqual([mock_fomod._requires_line(r) for r in record["requires"]],
                         ["Requires: Big Mod.esm, Nexus 12345", "Requires: Dark Molag Mar (mod), Nexus 32101"])


if __name__ == "__main__":
    unittest.main()
