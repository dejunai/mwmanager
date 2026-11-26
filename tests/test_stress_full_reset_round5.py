"""CR-25 stress run, round 5 (error K): --full's first step ('turn back on what earlier runs turned off',
full_reset -> --reenable and --remove-generated) moved plugins back and deleted the asset check's stand-ins
without journal entries, so verify against the deployed state reported 3 extra and 7 missing files; and it
turned Siege at Firemoth.esp back on although the major-master deployment had set it aside for Tamriel Rebuilt.
Every path here is a temporary folder."""
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402
import py_compat  # noqa: E402


class FullResetJournal(unittest.TestCase):
    def setUp(self):
        import mwmanager as m
        import mwmanager_journal
        self.m, self.mj = m, mwmanager_journal
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.manager, self.game = base / "MWManager", base / "Game"
        self.data = self.game / "Data Files"
        (self.data / "Sound").mkdir(parents=True)
        self.manager.mkdir()
        (self.data / "Morrowind.esm").write_bytes(b"TES3 base")
        patch = mwmanager_upstream.use_manager_dir(self.manager)
        patch.__enter__()
        self.addCleanup(patch.__exit__, None, None, None)
        owner = mock.patch.object(m, "asset_owner_folder", return_value=(None, None))
        owner.start()
        self.addCleanup(owner.stop)
        self.journal = mwmanager_journal.Journal(self.manager / "journal")
        self.journal.capture_baseline(self.game)

    def verify(self):
        report = self.journal.verify(self.game, against="deployed", full_hash=True)
        return report["changed"], report["missing"], report["extra"]

    def disable_like_the_gui(self, name, data=b"TES3 plugin"):
        """A deployed plugin, then the load-order panel's save moving it to MWManager\\disabled_plugins."""
        source = self.data / name
        run = self.journal.start_run("test", {"operation": "deploy"})
        source.write_bytes(data)
        self.journal.record(run["run"], f"Data Files/{name}", "create", self.mj.sha256_file(source),
                            {"kind": "absent"}, tag="plugin-deploy")
        self.journal.finish_run(run)
        target = Path(self.m.DISABLED_PLUGINS_DIR) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        run = self.journal.start_run("mwmanager_gui.py", {"operation": "disable plugins"})
        self.journal.move_original(self.game, source, target, run["run"], tag="disabled-plugin")
        self.journal.finish_run(run)
        return source, target

    def test_reenable_restore_is_journaled(self):
        source, target = self.disable_like_the_gui("Glowing_Mines.ESP")
        self.assertEqual(self.verify(), ([], [], []))
        restored = self.m.restore_reenabled_plugins(str(self.data), ["Glowing_Mines.ESP"])
        self.assertEqual(len(restored), 1)
        self.assertTrue(source.is_file())
        self.assertFalse(target.exists())
        self.assertEqual(self.verify(), ([], [], []))           # was: extra Data Files/Glowing_Mines.ESP
        last = self.journal.entries()[0][-1]
        self.assertEqual((last["path"], last["action"], last["tag"]),
                         ("Data Files/Glowing_Mines.ESP", "create", "reenabled-plugin"))

    def test_undoing_a_reenable_is_journaled_too(self):
        source, target = self.disable_like_the_gui("NOD - Core.esm")
        restored = self.m.restore_reenabled_plugins(str(self.data), ["NOD - Core.esm"])
        self.m.rollback_reenabled_moves(restored)
        self.assertFalse(source.exists())
        self.assertTrue(target.is_file())
        self.assertEqual(self.verify(), ([], [], []))

    def test_rollback_after_reenable_returns_to_baseline(self):
        self.disable_like_the_gui("MW Containers Animated.esp")
        self.m.restore_reenabled_plugins(str(self.data), ["MW Containers Animated.esp"])
        plan = self.journal.plan_rollback(self.game)
        self.assertIn(("delete", "Data Files/MW Containers Animated.esp"),
                      {(s["op"], s["path"]) for s in plan["steps"]})
        self.journal.execute_rollback(self.game, plan)
        self.assertEqual(self.journal.verify(self.game, against="baseline", full_hash=True)["ok"], True)

    def test_run_reenable_uses_the_journaling_restore(self):
        self.assertIs(self.m.run_reenable.__globals__["restore_reenabled_plugins"], self.m.restore_reenabled_plugins)
        self.assertIs(self.m.run_reenable.__globals__["rollback_reenabled_moves"], self.m.rollback_reenabled_moves)

    def test_remove_generated_journals_each_deleted_stand_in(self):
        rel = os.path.join("Sound", "vo", "n", "f", "bIdl_NF021.mp3")
        made = self.m.write_generated(str(self.data), str(self.game), [(rel, "missing voice", None, "Test.esp")], [])
        self.assertEqual(made, [rel])
        self.assertEqual(self.verify(), ([], [], []))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = self.m.run_remove_generated(types.SimpleNamespace(data_dir=str(self.data), dry_run=False))
        self.assertEqual(rc, 0)
        self.assertFalse((self.data / rel).exists())
        self.assertEqual(self.verify(), ([], [], []))           # was: missing Sound/vo/n/f/bIdl_NF021.mp3
        removed = [e for e in self.journal.entries()[0] if e["action"] == "remove"]
        self.assertEqual([(e["path"], e["tag"], e.get("mod")) for e in removed],
                         [("Data Files/Sound/vo/n/f/bIdl_NF021.mp3", "generated-asset", "Test.esp")])

    def test_remove_generated_dry_run_records_nothing(self):
        rel = os.path.join("Sound", "missing.wav")
        self.m.write_generated(str(self.data), str(self.game), [(rel, "missing", None, "Test.esp")], [])
        before = len(self.journal.entries()[0])
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.run_remove_generated(types.SimpleNamespace(data_dir=str(self.data), dry_run=True))
        self.assertTrue((self.data / rel).is_file())
        self.assertEqual(len(self.journal.entries()[0]), before)

    def _siege_set_aside_by_master_deploy(self):
        siege = self.data / "Siege at Firemoth.esp"
        siege.write_bytes(b"TES3 official add-on")
        journal = self.mj.Journal(self.manager / "journal")
        journal.capture_baseline(self.game, force=True)
        self.journal = journal
        off = self.data / "Off" / "Official Add-Ons" / siege.name
        off.parent.mkdir(parents=True)
        os.replace(siege, off)
        journal.record_deployment(self.game, [], tool="major_master_deploy.py", tag="major-master-deployment",
                                  moves=[{"source": str(siege), "target": str(off), "sha256": ""}])
        return siege, off

    def _full_reset(self):
        calls = []
        args = types.SimpleNamespace(data_dir=str(self.data), dry_run=False, ini=None, use_answers=False)
        out = io.StringIO()
        with mock.patch.object(self.m, "main", side_effect=lambda argv: calls.append(argv) or 0), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.m.full_reset(args, str(self.game)), 0)
        return calls, out.getvalue()

    def test_full_reset_keeps_a_master_deploy_conflict_turnoff_off(self):
        siege, off = self._siege_set_aside_by_master_deploy()
        self.disable_like_the_gui("Glowing_Mines.ESP")
        self.m.write_disabled(str(self.game), {
            "Siege at Firemoth.esp": "Tamriel Rebuilt deployed without Remove Firemoth: conflicts with TR_Mainland.esm",
            "Glowing_Mines.ESP": "disabled in MWManager load-order panel"})
        calls, out = self._full_reset()
        self.assertEqual(calls, [[str(self.data), "--reenable", "Glowing_Mines.ESP"]])
        self.assertIn("kept off (a major-master deployment set them aside for a conflict): Siege at Firemoth.esp", out)
        self.assertTrue(off.is_file())
        self.assertFalse(siege.exists())
        self.assertIn("Siege at Firemoth.esp", self.m.read_disabled(str(self.game)))

    def test_full_reset_with_only_the_conflict_turnoff_reenables_nothing(self):
        self._siege_set_aside_by_master_deploy()
        self.m.write_disabled(str(self.game), {"Siege at Firemoth.esp": "conflicts with TR_Mainland.esm"})
        calls, out = self._full_reset()
        self.assertEqual(calls, [])
        self.assertIn("back on: none", out)

    def test_full_reset_still_reenables_a_plugin_the_user_turned_off(self):
        self.disable_like_the_gui("Siege at Firemoth.esp")      # turned off in the panel, not by a deployment
        self.m.write_disabled(str(self.game), {"Siege at Firemoth.esp": "disabled in MWManager load-order panel"})
        calls, _out = self._full_reset()
        self.assertEqual(calls, [[str(self.data), "--reenable", "Siege at Firemoth.esp"]])

    def test_without_a_baseline_nothing_is_journaled_and_nothing_kept(self):
        import shutil
        shutil.rmtree(self.manager / "journal")
        self.assertEqual(self.m.major_master_turnoffs(str(self.data), ["Siege at Firemoth.esp"]), [])
        self.m.journal_plugin_moves(str(self.data), [(str(self.data / "x.esp"), str(self.data / "y.esp"))], "t")
        self.assertFalse((self.manager / "journal").exists())


if __name__ == "__main__":
    unittest.main()