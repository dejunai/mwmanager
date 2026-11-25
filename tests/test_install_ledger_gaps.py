"""Install-ledger gaps found by the CR-25 stress run: files a deployment leaves that verify must explain."""
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_journal as mj  # noqa: E402
from test_journal import Fixture, sha, write  # noqa: E402


class LedgerGaps(Fixture):
    def test_suite_runtime_data_is_not_an_unexplained_extra(self):
        self.journal.capture_baseline(self.game)
        write(self.game / "Data Files/MWSE/config/mwfloatfix_data.json", b"{}")
        write(self.game / "Data Files/MWSE/config/mwfloatfix_rescue.json", b"{}")
        report = self.journal.verify(self.game, against="deployed")
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["unexplained"], [])

    def test_an_original_moved_back_from_off_is_journaled(self):
        import mwmanager as m
        j = self.journal
        j.capture_baseline(self.game)
        source = self.game / "Data Files/Textures/a.dds"
        off = self.game / "Data Files/Off/Found at Setup/Unused assets/Textures/a.dds"
        run = j.start_run("test", {})
        j.move_original(self.game, source, off, run["run"], tag="unused-asset")
        j.finish_run(run)
        self.assertTrue(j.verify(self.game, against="deployed")["ok"])
        os.replace(off, source)                                   # what write_generated does for a needed asset
        with mock.patch.object(m, "MANAGER_DIR", str(self.manager)):
            m._journal_moves_back(self.game, [(str(off), str(source))])
        report = j.verify(self.game, against="deployed", full_hash=True)
        self.assertTrue(report["ok"], report)
        plan = j.plan_rollback(self.game)
        self.assertEqual(plan["refuse"], [])
        j.execute_rollback(self.game, plan)
        self.assertEqual(source.read_bytes(), b"original texture")
        self.assertFalse(off.exists())

    def test_mwse_core_lua_patched_in_place_is_journaled_and_rolled_back(self):
        import mwmanager_install as tidy
        j = self.journal
        j.capture_baseline(self.game)
        core = self.game / "Data Files/MWSE/core/initialize.lua"
        with mock.patch.object(mj, "DEFAULT_ROOT", j.root):
            held = tidy._core_lua_ledger(mock.Mock(files={str(core): {"patch": {"ok": True}}}))
            write(core, b"--[[localpatch 5]] -- core\n")         # luafix's in-place write
            tidy._record_core_lua(held)
        report = j.verify(self.game, against="deployed", full_hash=True)
        self.assertTrue(report["ok"], report)
        plan = j.plan_rollback(self.game)
        self.assertEqual(plan["refuse"], [])
        j.execute_rollback(self.game, plan)
        self.assertEqual(core.read_bytes(), b"-- core\n")

    def test_core_lua_ledger_without_a_baseline_does_nothing(self):
        import mwmanager_install as tidy
        with mock.patch.object(mj, "DEFAULT_ROOT", self.journal.root):
            core = str(self.game / "Data Files/MWSE/core/initialize.lua")
            self.assertIsNone(tidy._core_lua_ledger(mock.Mock(files={core: {"patch": {"ok": True}}})))
            self.assertIsNone(tidy._core_lua_ledger(mock.Mock()))          # a report without a file table

    def test_the_real_journal_folder_is_redirected_during_tests(self):
        target = self.tmp / "redirected"
        with mock.patch.dict(os.environ, {"MWMANAGER_JOURNAL_REDIRECT": str(target)}):
            self.assertEqual(mj.Journal(mj._REAL_ROOT).root, target)
            self.assertEqual(mj.Journal(self.manager / "journal").root, self.manager / "journal")
        self.assertIn("MWMANAGER_JOURNAL_REDIRECT", os.environ)       # the root conftest set it for this run
