"""Tidy write roots: staging, except the allowlisted --init Data Files context.

Tidy is confined to staging, except allow="init" (Tidy --init / first-run Initial Tidy).
A successful MWSE update re-applies core patches only; it does not tidy, staged or live.
A failed or cancelled update runs neither reapply nor tidy. Generated patches and the
--full asset-check still target Data Files (they are not Tidy). Every path here is a
temporary folder.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import workflow_safety


_REPO = Path(__file__).resolve().parents[1]
_LATEST = "1dce8de6b87c7b216c6809fbc261a176157764ce"
_OLD_BUILD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_TIDY_FUNCS = (
    "run_data_files_tidy",
    "run_initial_tidy",
    "run_live_data_files_tidy",
    "run_tidy_after_mwse_update",
    "maybe_tidy_after_mwse_update",
    "mwmanager_main",
)


class StagingWriteRoot(unittest.TestCase):
    def test_normal_tidy_is_refused_on_data_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp) / "Game"
            data = game / "Data Files"
            staging = Path(tmp) / "staging" / "plugins"
            data.mkdir(parents=True)
            staging.mkdir(parents=True)
            with self.assertRaises(ValueError):
                workflow_safety.require_staging_write_root(data, data_dir=data, game_dir=game)
            with self.assertRaises(ValueError):
                workflow_safety.require_staging_write_root(game, data_dir=data, game_dir=game)
            with self.assertRaises(ValueError):
                workflow_safety.require_staging_write_root(data / "Meshes", data_dir=data, game_dir=game)
            self.assertEqual(
                workflow_safety.require_staging_write_root(staging, data_dir=data, game_dir=game),
                staging.resolve(),
            )
            self.assertTrue(workflow_safety.is_data_files_tree(data))
            self.assertFalse(workflow_safety.is_data_files_tree(staging))

    def test_unknown_allow_context_is_not_a_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "Game" / "Data Files"
            data.mkdir(parents=True)
            for allow in ("anything", "mwse-update"):
                with self.assertRaises(ValueError) as raised:
                    workflow_safety.require_staging_write_root(data, data_dir=data, allow=allow)
                self.assertIn("unknown", str(raised.exception).casefold())

    def test_init_allow_is_accepted_on_data_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "Game" / "Data Files"
            data.mkdir(parents=True)
            self.assertEqual(
                workflow_safety.require_staging_write_root(
                    data, data_dir=data, allow="init", what="Tidy"),
                data.resolve(),
            )

    def test_run_tidy_steps_refuses_data_files_as_staging(self):
        import mwmanager_gui as g
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "Game" / "Data Files"
            data.mkdir(parents=True)
            with self.assertRaises(ValueError):
                g.run_tidy_steps("py", data, data, lambda _m: None, "Tidy")

    def test_init_is_allowed_on_data_files(self):
        """Tidy --init still runs document tidy and scanner --fix --asset-check on Data Files."""
        import mwmanager_upstream
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            manager, game = base / "MWManager", base / "Game"
            data = game / "Data Files"
            data.mkdir(parents=True)
            manager.mkdir()
            with mwmanager_upstream.use_manager_dir(manager):
                tidy = mwmanager_upstream.compose_tidy()
                assets = mock.Mock()
                with mock.patch.object(tidy, "run_data_files_tidy", return_value=0) as docs, \
                        mock.patch.object(tidy._scanner, "main", return_value=0) as scan, \
                        mock.patch.object(tidy._scanner, "asset_health_module", return_value=assets):
                    rc = tidy.run_initial_tidy(str(data), apply_changes=True)
                    rc2 = tidy.mwmanager_main(
                        ["--init", "--game-data-files", str(data), "--apply", "--yes"])
            self.assertEqual(rc, 0)
            self.assertEqual(rc2, 0)
            docs.assert_called()
            argv = scan.call_args.args[0]
            self.assertEqual(argv[0], str(data))
            self.assertIn("--fix", argv)
            self.assertIn("--asset-check", argv)


class MwseUpdateDoesNotTidy(unittest.TestCase):
    """After MWSE-Update.exe, only reapply_core_patches runs; tidy does not."""

    def test_update_paths_do_not_call_a_tidy_hook(self):
        sources = [
            _REPO / "mwmanager_mwse_core.py",
            _REPO / "mwmanager_init.py",
            _REPO / "mwmanager_gui.py",
            _REPO / "preinstall.py",
            _REPO / "mwmanager_tidy_ext.py",
        ]
        banned = (
            "maybe_tidy_after_mwse_update",
            "run_tidy_after_mwse_update",
            "--after-mwse-update",
            "tidy_after_mwse_update",
        )
        for path in sources:
            text = path.read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, text, "%s still mentions %s" % (path.name, token))

    def _game(self, tmp):
        base = Path(tmp)
        manager, game = base / "MWManager", base / "Game"
        (game / "Data Files").mkdir(parents=True)
        manager.mkdir()
        (game / "MWSE-Update.exe").write_bytes(b"fake-updater")
        (game / "MWSE.dll").write_bytes(b"fake-dll")
        (game / "mwse-version.txt").write_text(_OLD_BUILD, encoding="ascii")
        return game, manager

    def _succeeding_runner(self, game):
        def runner(cmd, cwd=None, check=False):
            Path(game, "mwse-version.txt").write_text(_LATEST, encoding="ascii")
            return mock.Mock(returncode=0)
        return runner

    def _watch(self, reapply=None):
        tidy = mock.Mock(side_effect=AssertionError("tidy must not run on an MWSE update"))
        if reapply is None:
            reapply = mock.Mock(return_value={"status": "applied", "written": 1, "detail": []})
        patches = [
            mock.patch("mwmanager_prereqs.latest_mwse_build", return_value=(_LATEST, None)),
            mock.patch("mwmanager_mwse_core.reapply_core_patches", reapply),
            mock.patch("mwmanager_mwse_core.morrowind_process_running", return_value=False),
            mock.patch("mwmanager_upstream.compose_tidy", side_effect=AssertionError(
                "tidy must not run on an MWSE update")),
        ]
        for mod in ("mwmanager_tidy_ext", "mwmanager_init", "vortex_tidy"):
            for name in _TIDY_FUNCS:
                try:
                    __import__(mod)
                except Exception:
                    continue
                target = "%s.%s" % (mod, name)
                try:
                    patches.append(mock.patch(target, tidy))
                except Exception:
                    continue
        return reapply, tidy, patches

    def _started(self, patches):
        entered = []
        for patch in patches:
            try:
                entered.append(patch.start())
            except (AttributeError, ImportError):
                continue
        return entered

    def test_successful_update_reapplies_core_patches_and_does_not_tidy(self):
        import mwmanager_init
        import mwmanager_mwse_core as mwse_core
        import preinstall

        with tempfile.TemporaryDirectory() as tmp:
            game, manager = self._game(tmp)
            reapply, _tidy, patches = self._watch()
            self._started(patches)
            try:
                result = mwse_core.offer_mwse_update(
                    game, yes=True, manager_dir=manager,
                    runner=self._succeeding_runner(game), reapply_after=True)
                self.assertTrue(result["updated"])
                self.assertEqual(result["state"], "updated")
                reapply.assert_called()
                reapply.reset_mock()

                Path(game, "mwse-version.txt").write_text(_OLD_BUILD, encoding="ascii")
                ok = mwmanager_init.offer_mwse_update(
                    game, yes=True, manager_dir=manager,
                    runner=self._succeeding_runner(game), reapply_after=True)
                self.assertTrue(ok)
                reapply.assert_called()
                reapply.reset_mock()

                Path(game, "mwse-version.txt").write_text(_OLD_BUILD, encoding="ascii")
                ok = preinstall.run_mwse_updater(
                    game, _LATEST, progress=lambda _m: None,
                    runner=self._succeeding_runner(game), manager_dir=manager)
                self.assertTrue(ok)
                reapply.assert_called()
            finally:
                mock.patch.stopall()

    def test_failed_or_cancelled_update_runs_neither_reapply_nor_tidy(self):
        import mwmanager_init
        import mwmanager_mwse_core as mwse_core
        import preinstall

        with tempfile.TemporaryDirectory() as tmp:
            game, manager = self._game(tmp)
            reapply, _tidy, patches = self._watch()
            self._started(patches)
            try:
                failed = mock.Mock(return_value=mock.Mock(returncode=1))
                result = mwse_core.offer_mwse_update(
                    game, yes=True, manager_dir=manager, runner=failed, reapply_after=True)
                self.assertFalse(result["updated"])
                reapply.assert_not_called()

                cancelled = mwse_core.offer_mwse_update(
                    game, yes=False, manager_dir=manager, runner=failed,
                    ask=lambda title, message: False, reapply_after=True)
                self.assertTrue(cancelled.get("skipped"))
                self.assertFalse(cancelled["updated"])
                reapply.assert_not_called()

                ok = mwmanager_init.offer_mwse_update(
                    game, yes=True, manager_dir=manager, runner=failed, reapply_after=True)
                self.assertFalse(ok)
                reapply.assert_not_called()

                ok = preinstall.run_mwse_updater(
                    game, _LATEST, progress=lambda _m: None,
                    runner=failed, manager_dir=manager)
                self.assertFalse(ok)
                reapply.assert_not_called()
            finally:
                mock.patch.stopall()


class PatchesAndFullAssetCheckStayOnDataFiles(unittest.TestCase):
    def test_full_asset_check_and_patches_still_target_data_files(self):
        source = (_REPO / "mwmanager_overrides.py").read_text(encoding="utf-8")
        self.assertIn('["--clean", "--fix", "--asset-check"]', source)
        self.assertIn('main([args.data_dir] + extra + common)', source)
        self.assertIn('argv2 = [args.data_dir, "--fix", "--multipatch"', source)
        self.assertIn('write_plugin_bytes', source)


if __name__ == "__main__":
    unittest.main()
