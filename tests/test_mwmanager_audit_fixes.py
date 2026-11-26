"""MWManager's own versions of the 2026-11-22 audit's fixes: no repair, rename, clean-up or restore loses a file."""
import argparse
import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402


def sub(name, data):
    return name + struct.pack("<I", len(data)) + data


def rec(tag, body):
    return tag + struct.pack("<III", len(body), 0, 0) + body


def plugin(path, *models):
    hedr = struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", len(models))
    data = rec(b"TES3", sub(b"HEDR", hedr)) + b"".join(
        rec(b"STAT", sub(b"NAME", b"s%d\0" % i) + sub(b"MODL", name.encode("cp1252") + b"\0"))
        for i, name in enumerate(models))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class AuditFixes(unittest.TestCase):
    def setUp(self):
        import mwmanager as m
        self.m = m
        mwmanager_upstream.compose()
        import mwdialog_assets
        self.ah = mwdialog_assets
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.data = self.base / "Game" / "Data Files"
        self.data.mkdir(parents=True)
        self.manager = self.base / "MWManager"
        self.manager.mkdir()
        state = mwmanager_upstream.use_manager_dir(self.manager)
        state.__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        owner = mock.patch.object(m, "asset_owner_folder", return_value=(None, None))
        owner.start()
        self.addCleanup(owner.stop)

    def kept(self, data):
        return [p for p in self.base.rglob("*") if p.is_file() and p.read_bytes() == data]

    def test_tidy_backup_keeps_an_updated_original(self):
        root = self.base / "staging" / "Mod"
        put(root / "Off" / "Mod" / "converted sounds" / "a.wav", b"first")
        put(root / "a.wav", b"updated")
        bak = self.ah._backup_in_mod(str(root), "a.wav", str(root / "a.wav"), "Mod", self.m)
        self.assertEqual(Path(bak).read_bytes(), b"updated")
        self.assertEqual((root / "Off" / "Mod" / "converted sounds" / "a.wav").read_bytes(), b"first")

    def test_tidy_backup_that_cannot_be_made_stops_the_repair(self):
        root = self.base / "staging" / "Mod"
        put(root / "Off" / "Mod" / "converted sounds" / "a.wav", b"first")
        put(root / "a.wav", b"updated")
        with mock.patch("shutil.copy2", side_effect=OSError("no space")), self.assertRaises(OSError):
            self.ah._backup_in_mod(str(root), "a.wav", str(root / "a.wav"), "Mod", self.m)

    def test_fix_backup_keeps_an_updated_original(self):
        put(self.data / "textures" / "a.dds", b"first")
        rel = os.path.join("textures", "a.dds")
        self.m.backup_asset_original(str(self.data), rel)
        put(self.data / "textures" / "a.dds", b"updated")
        bak = self.m.backup_asset_original(str(self.data), rel)
        self.assertEqual(Path(bak).read_bytes(), b"updated")
        self.assertTrue(self.kept(b"first"))

    def test_rename_keeps_an_updated_file(self):
        root = self.base / "staging" / "Mod"
        plugin(root / "Mod.esp")
        old, new = os.path.join("Textures", "café.dds"), os.path.join("Textures", "cafe.dds")
        put(Path(self.m.off_dest(str(root), "non-ascii", None, old)), b"first")
        put(root / old, b"update")
        put(root / new, b"fixed")
        self.ah.rename_ascii(str(root), str(root / old), old, new, lambda _t: None)
        self.assertTrue(self.kept(b"update") and self.kept(b"first"))

    def rename_all(self, root, names):
        for n in names:
            old = os.path.join("Meshes", n)
            self.ah.rename_ascii(str(root), str(root / old), old, old.replace("é", "e"), lambda _t: None)

    def test_rename_keeps_an_updated_plugin_once(self):
        root = self.base / "staging" / "Mod"
        names = [f"café{i}.nif" for i in range(3)]
        for n in names:
            put(root / "Meshes" / n, b"nif")
        home = root / "Off" / "Mod" / "ascii"
        plugin(home / "Mod.esp.bak_ascii", *names)
        update = plugin(root / "Mod.esp", *names, "only_in_update.nif")
        self.rename_all(root, names)
        self.assertTrue(self.kept(update))
        self.assertEqual(len([p for p in home.iterdir() if p.suffix != ".sha256"]), 2)
        with mock.patch.object(self.m, "backup_root_for", return_value=str(root)):
            found = self.m.backup_candidates(str(root / "Mod.esp"))
        self.assertFalse([p for p in found if p.endswith(".repaired.sha256")])

    def test_restore_unused_never_replaces_a_newer_file(self):
        root = self.base / "staging" / "Mod"
        rel = os.path.join("Textures", "x.dds")
        moved = root / "Off" / "Mod" / "Unused assets" / rel
        put(moved, b"old")
        put(root / rel, b"newer")
        record = Path(self.ah.OUTPUT_DIR, self.ah.UNUSED_RECORD)
        put(record, f"2026-01-01 00:00:00\t{rel}\t{moved}\tunused\n".encode())
        self.assertEqual(self.ah.restore_unused(str(self.data.parent), str(self.data)), 1)
        self.assertEqual(((root / rel).read_bytes(), moved.read_bytes()), (b"newer", b"old"))
        self.assertIn("x.dds", record.read_text())

    def backed_up_versions(self, count):
        path, made = self.data / "Mod.esp", []
        for i in range(count):
            made.append(plugin(path, f"v{i}.nif"))
            bak = self.m.backup_path(str(path))
            shutil.copy2(path, bak)
        return path, made

    def clean_up(self):
        self.m.run_clean_up(argparse.Namespace(data_dir=str(self.data), dry_run=False, yes=True,
                                               fix_log=str(self.base / "log")))

    def test_clean_up_keeps_every_backup_of_an_undeployed_plugin(self):
        bak = Path(self.m.BACKUP_DIR, "plugins", "Mod.esp.bak")
        original = plugin(bak, "original.nif")
        self.clean_up()
        self.assertEqual(bak.read_bytes(), original)

    def test_a_gui_job_and_the_steam_tools_share_the_game_lock(self):
        import subprocess
        import workflow_safety
        game = str(self.data.parent)
        code = "import sys, mwdialog_paths as P; print(P.acquire_game_lock(sys.argv[1]), flush=True); sys.stdin.read()"

        def tool():
            child = subprocess.Popen([sys.executable, "-B", "-c", code, game], cwd=ROOT, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
            self.addCleanup(lambda: child.poll() is None and child.kill())
            return child, child.stdout.readline().strip()

        with workflow_safety.mutation_lock(self.manager, game):
            child, got = tool()
            child.communicate("")
            self.assertEqual(got, "False")
        child, got = tool()
        self.assertEqual(got, "True")
        try:
            with self.assertRaises(RuntimeError), workflow_safety.mutation_lock(self.manager, game):
                pass
        finally:
            child.communicate("")
        with workflow_safety.mutation_lock(self.manager, game):      # free again when the tool ends
            pass

    def test_clean_up_keeps_the_original_and_the_newest(self):
        path, made = self.backed_up_versions(4)
        self.clean_up()
        self.assertEqual({Path(p).read_bytes() for p in self.m.backup_candidates(str(path))}, {made[0], made[3]})

    def test_restore_all_keeps_what_it_replaces(self):
        path, made = self.backed_up_versions(1)
        update = plugin(path, "version_two.nif")
        self.m.restore_all(str(self.data), True, str(self.base / "log"))
        self.assertEqual(path.read_bytes(), made[0])
        self.assertTrue(self.kept(update))


if __name__ == "__main__":
    unittest.main()
