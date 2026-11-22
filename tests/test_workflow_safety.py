"""Storage, process ownership, and transaction invariants on temporary files."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import time
import signal
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflow_safety import DurableTransaction, mutation_lock, require_space
import managed_processes
import major_master_deploy as deploy
import mge_ghosts
from mwmanager_journal import Journal


class SafetyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def put(self, relative, data=b"original"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_unrelated_process_cannot_take_lock_but_managed_child_can(self):
        script = ("import sys; from workflow_safety import mutation_lock; "
                  "lock=mutation_lock(sys.argv[1]); lock.__enter__(); print('locked'); lock.__exit__(None,None,None)")
        with mutation_lock(self.root):
            plain = subprocess.run([sys.executable, "-c", script, str(self.root)],
                                   capture_output=True, text=True)
            self.assertNotEqual(plain.returncode, 0)
            child = managed_processes.run([sys.executable, "-c", script, str(self.root)], text=True)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn("locked", child.stdout)

    def test_managed_steam_tool_inherits_the_game_lock(self):
        script = ("import sys; sys.path.insert(0, sys.argv[2]); import mwdialog_paths as paths; "
                  "print(paths.acquire_game_lock(sys.argv[1]))")
        game = self.root / "game"
        game.mkdir()
        with mutation_lock(self.root, game):
            child = managed_processes.run(
                [sys.executable, "-c", script, str(game), str(Path(__file__).resolve().parents[1])], text=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "True")

    @unittest.skipIf(os.name == "nt", "POSIX inherited-flock behavior")
    def test_orphaned_tool_keeps_lock_after_parent_dies(self):
        child = ("import sys,time; from pathlib import Path; "
                 "root=Path(sys.argv[1]); "
                 "exec('while not (root/\"release\").exists(): time.sleep(0.01)')")
        parent = (
            "import os,sys,subprocess; from pathlib import Path; "
            "from workflow_safety import mutation_lock; import managed_processes; "
            "root=Path(sys.argv[1]); "
            "lock=mutation_lock(root); lock.__enter__(); "
            f"p=managed_processes.popen([sys.executable,'-c',{child!r},str(root)],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "(root/'child.pid').write_text(str(p.pid)); os._exit(86)"
        )
        result = subprocess.run([sys.executable, "-c", parent, str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, 86, result.stderr)
        pid = int((self.root / "child.pid").read_text())
        try:
            with self.assertRaisesRegex(RuntimeError, "instance"):
                with mutation_lock(self.root):
                    self.fail("An orphan tool released the parent's lock.")
            (self.root / "release").touch()
            deadline = time.monotonic() + 5
            while True:
                try:
                    with mutation_lock(self.root):
                        break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_link_intent_survives_failure_before_link(self):
        source = self.put("stage/A", b"new")
        target = self.put("game/A", b"old")
        tx = DurableTransaction(self.root / "tx", [self.root]).begin()
        with mock.patch("workflow_safety.os.link", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                tx.link(source, target, self.root / "backup/A")
        self.assertFalse(target.exists())
        self.assertTrue(DurableTransaction.recover(self.root / "tx", [self.root]))
        self.assertEqual(target.read_bytes(), b"old")

    def test_recovery_is_idempotent_after_restoring_one_link(self):
        tx = DurableTransaction(self.root / "tx", [self.root]).begin()
        source = self.put("stage/A", b"new")
        target = self.put("game/A", b"old")
        backup = self.root / "backup/A"
        tx.link(source, target, backup)
        target.unlink()
        os.replace(backup, target)
        DurableTransaction.recover(self.root / "tx", [self.root])
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(DurableTransaction.recover(self.root / "tx", [self.root]))

    def test_recovery_preserves_user_edits_and_keeps_intent(self):
        tx = DurableTransaction(self.root / "tx", [self.root]).begin()
        source = self.put("stage/A", b"new")
        target = self.put("game/A", b"old")
        tx.link(source, target, self.root / "backup/A")
        target.write_bytes(b"user edited linked bytes")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            DurableTransaction.recover(self.root / "tx", [self.root])
        self.assertEqual(target.read_bytes(), b"user edited linked bytes")
        self.assertTrue((self.root / "tx/state.json").exists())

    def test_corrupt_backup_never_causes_live_target_deletion(self):
        tx = DurableTransaction(self.root / "tx", [self.root]).begin()
        target = self.put("game/A", b"old")
        backup = self.root / "backup/A"
        tx.link(self.put("stage/A", b"new"), target, backup)
        backup.write_bytes(b"damaged")
        with self.assertRaisesRegex(RuntimeError, "backup changed"):
            DurableTransaction.recover(self.root / "tx", [self.root])
        self.assertEqual(target.read_bytes(), b"new")

    def test_existing_backup_is_never_overwritten(self):
        tx = DurableTransaction(self.root / "tx", [self.root]).begin()
        target = self.put("game/A", b"old")
        backup = self.put("backup/A", b"precious backup")
        with self.assertRaises(FileExistsError):
            tx.link(self.put("stage/A", b"new"), target, backup)
        self.assertEqual(backup.read_bytes(), b"precious backup")
        self.assertEqual(target.read_bytes(), b"old")
        tx.rollback()

    def test_committed_recovery_never_reverts_game_files(self):
        tx = DurableTransaction(self.root / "tx", [self.root]).begin()
        target = self.root / "game/A"
        tx.link(self.put("stage/A", b"new"), target, self.root / "backup/A")
        with mock.patch("workflow_safety.shutil.rmtree"):
            tx.commit()
        DurableTransaction.recover(self.root / "tx", [self.root])
        self.assertEqual(target.read_bytes(), b"new")

    def test_metadata_and_partial_journal_are_restored(self):
        ledger = self.put("journal/install.jsonl", b"old ledger\n")
        tx = DurableTransaction(self.root / "tx", [self.root]).begin()
        tx.snapshot(ledger)
        ledger.write_bytes(b"old ledger\npartial new entry\n")
        tx.rollback()
        self.assertEqual(ledger.read_bytes(), b"old ledger\n")

    @unittest.skipIf(os.name == "nt", "fcntl validates Windows-compatible descriptor access on POSIX")
    def test_snapshots_flush_write_capable_descriptors(self):
        import fcntl
        import stat
        real_fsync = os.fsync

        def windows_compatible_fsync(fd):
            if (stat.S_ISREG(os.fstat(fd).st_mode)
                    and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY):
                raise OSError(9, "Windows cannot commit a read-only descriptor")
            return real_fsync(fd)

        tx = DurableTransaction(self.root / "tx", [self.root]).begin()
        with mock.patch("workflow_safety.os.fsync", side_effect=windows_compatible_fsync):
            tx.snapshot(self.put("metadata.json"))
        tx.rollback()

    def test_archives_cannot_import_manager_recovery_records(self):
        from core_addons import _member_preflight
        unsafe, collisions = _member_preflight(
            [".mwmanager-stage.json", ".mwmanager-pack/state.json", "Data Files/A.esm"])
        self.assertEqual(len(unsafe), 2)
        self.assertFalse(collisions)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object lifetime")
    def test_windows_parent_death_ends_owned_tools(self):
        parent = (
            "import os,sys,subprocess; from pathlib import Path; import managed_processes; "
            "p=managed_processes.popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "(Path(sys.argv[1])/'owned.pid').write_text(str(p.pid)); os._exit(86)"
        )
        result = subprocess.run([sys.executable, "-c", parent, str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, 86, result.stderr)
        from mwnexus_browser import _pid_running, _terminate_pid
        pid = int((self.root / "owned.pid").read_text())
        try:
            deadline = time.monotonic() + 5
            while _pid_running(pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(_pid_running(pid), "Owned tool survived its Windows parent's death")
        finally:
            if _pid_running(pid):
                _terminate_pid(pid)

    def test_disk_full_is_reported_before_mutation(self):
        with mock.patch("workflow_safety.shutil.disk_usage", return_value=mock.Mock(free=0)):
            with self.assertRaisesRegex(OSError, "free space"):
                require_space(self.root, 100)

    def test_transaction_rejects_outside_target(self):
        tx = DurableTransaction(self.root / "tx", [self.root]).begin()
        with self.assertRaises(ValueError):
            tx.link(self.put("stage/A"), self.root.parent / "not-ours", self.root / "backup")
        tx.rollback()

    def test_packing_failure_after_publication_restores_all_loose_assets(self):
        package = self.root / "package"
        texture = self.put("package/Textures/a.dds", b"texture")

        def pack(command, cwd, **kwargs):
            (Path(cwd) / command[2]).write_bytes(b"archive")
            process = mock.Mock(stdout=io.StringIO("packed\n"))
            process.wait.return_value = 0
            return process

        move_many = DurableTransaction.move_many

        def fail_texture(tx, moves, **kwargs):
            if any(Path(item[0]) == texture for item in moves):
                raise OSError("publication interrupted")
            return move_many(tx, moves, **kwargs)

        with mock.patch.object(deploy.subprocess, "Popen", side_effect=pack), \
                mock.patch.object(DurableTransaction, "move_many", fail_texture):
            with self.assertRaisesRegex(OSError, "interrupted"):
                deploy._pack_assets(package, "A.esm", Path("bsapack"))
        self.assertEqual(texture.read_bytes(), b"texture")
        self.assertFalse((package / "A_assets.bsa").exists())
        self.assertFalse((package / ".mwmanager-pack").exists())

    def test_generated_bsa_can_be_reopened_without_stale_packed_options(self):
        package = self.root / "package"
        source = self.put("package/Data Files/Textures/a.dds", b"texture")

        def pack(command, cwd, **kwargs):
            (Path(cwd) / command[2]).write_bytes(b"archive")
            process = mock.Mock(stdout=io.StringIO(""))
            process.wait.return_value = 0
            return process

        with mock.patch.object(deploy.subprocess, "Popen", side_effect=pack):
            archive = deploy._pack_assets(package, "A.esm", Path("bsapack"))[0]
        self.assertFalse(source.exists())
        self.assertTrue(deploy.restore_packed_assets(package))
        self.assertFalse(archive.exists())
        self.assertEqual(source.read_bytes(), b"texture")
        with mock.patch.object(deploy.subprocess, "Popen", side_effect=pack):
            self.assertTrue(deploy._pack_assets(package, "A.esm", Path("bsapack"))[0].is_file())

    def test_malformed_ghost_config_is_safe(self):
        data = self.root / "Data Files"
        self.put("Data Files/MWSE/config/" + mge_ghosts.COLLIDE_DATA,
                 json.dumps({"cells": {"#1,2": [None, {}, {"id": 3}, {"id": "okay"}]}}).encode())
        self.assertEqual(mge_ghosts.hidden_exteriors(data), {"okay": 1})

    def test_ghost_overrides_are_journalled_and_require_regeneration(self):
        manager = self.root / "manager"
        game = self.root / "game"
        data = game / "Data Files"
        data.mkdir(parents=True)
        (game / "mge3").mkdir()
        original = self.put("game/mge3/" + mge_ghosts.NAME + ".ovr", b"pre-existing overrides")
        ledger = Journal(manager / "journal")
        ledger.capture_baseline(game)
        with mock.patch.object(mge_ghosts, "plan", return_value=(["door"], ["door.nif"], [])):
            mge_ghosts.write(game, data, [], log=lambda _: None, manager_dir=manager)
        entries, errors = ledger.entries()
        self.assertFalse(errors)
        self.assertEqual(entries[-1]["tag"], "ghost-statics")
        self.assertNotEqual(original.read_bytes(), b"pre-existing overrides")
        self.assertTrue(json.loads((manager / "outputs/distant_land_status.json").read_text())["regeneration_required"])
        self.assertTrue((manager / "journal/originals" / entries[-1]["original"]["sha256"]).is_file())

    def test_ghost_journal_failure_restores_override(self):
        manager, game = self.root / "manager", self.root / "game"
        (game / "Data Files").mkdir(parents=True)
        self.put("game/mge3/" + mge_ghosts.NAME + ".ovr", b"old")
        Journal(manager / "journal").capture_baseline(game)
        with mock.patch.object(mge_ghosts, "plan", return_value=(["door"], ["door.nif"], [])), \
                mock.patch.object(Journal, "record", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                mge_ghosts.write(game, game / "Data Files", [], manager_dir=manager)
        self.assertEqual((game / "mge3" / (mge_ghosts.NAME + ".ovr")).read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
