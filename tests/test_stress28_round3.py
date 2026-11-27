"""CR-28 stress run, round 3."""
import unittest

import mwmanager_gui


class ChosenRows(unittest.TestCase):
    KNOWN = {"a": 1, "b": 2, "c": 3}

    def test_ticked_rows_win(self):
        self.assertEqual(mwmanager_gui.chosen_rows({"c", "a"}, ("b",), self.KNOWN), ["a", "c"])

    def test_highlighted_rows_count_when_nothing_is_ticked(self):
        self.assertEqual(mwmanager_gui.chosen_rows(set(), ("b",), self.KNOWN), ["b"])

    def test_nothing_chosen(self):
        self.assertEqual(mwmanager_gui.chosen_rows(set(), (), self.KNOWN), [])
        self.assertEqual(mwmanager_gui.chosen_rows(set(), ("zz",), self.KNOWN), [])

    def test_both_dialogs_use_it(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "mwmanager_gui.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("chosen_rows(selected, tree.selection(), by_id)"), 3)

class TidyJournalLine(unittest.TestCase):
    def test_inline_journal_path_and_undo_hint_are_detail_only(self):
        from mwmanager_workflow import UserOutput
        out = UserOutput()
        lines = []
        for raw in ("Tidy and options for Earthquake.rar: Applying the reviewed tidy plan",
                    "Tidy changes: 1 of 1 applied. Journal: G:\\m\\outputs\\mwmanager_vortex_tidy_journal.jsonl",
                    "  (--undo --only <package> puts back that package's changes from every run, --undo alone the "
                    "whole last run).",
                    "Tidy and options for Earthquake.rar: Checking assets in Earthquake"):
            lines += out.feed(raw)
        self.assertEqual(lines, ["Tidy and options for Earthquake.rar: Applying the reviewed tidy plan",
                                 "Tidy changes: 1 of 1 applied.",
                                 "Tidy and options for Earthquake.rar: Checking assets in Earthquake"])

class RefusedApplyIsRetriedQuietly(unittest.TestCase):
    def test_refused_block_and_clean_issue_count_are_detail_only(self):
        from mwmanager_workflow import UserOutput
        pkg = "Tidy and options for GH.7z: "
        raw = [pkg + "Checking assets in GH-46599",
               "  0 asset issue(s), 0 repairable",
               "REFUSED: the plan vortex_tidy.py --apply works out now is not the one in the plan file you reviewed. "
               "Nothing was",
               "  changed.",
               "  1 action(s) added, 1 removed since the plan you reviewed",
               "    added:",
               "      GH | lua graphicHerbalism/main.lua: F1 high line 309: mwse.log",
               "The plan you reviewed was saved as",
               "Review G:\\m\\outputs\\mwmanager_vortex_tidy_plan.txt, then run vortex_tidy.py --apply again.",
               pkg + "Tidy found more to do on that pass; starting again (attempt 2 of 3).",
               "  (--undo --only <package> puts back that package's changes from every run).",
               "  2 asset issue(s), 2 repairable"]
        out = UserOutput()
        shown = [line for r in raw for line in out.feed(r)]
        self.assertEqual(shown, [pkg + "Checking assets in GH-46599",
                                 pkg + "Tidy found more to do on that pass; starting again (attempt 2 of 3).",
                                 "  2 asset issue(s), 2 repairable"])

class RemovalWithCatalog(unittest.TestCase):
    def test_removal_with_a_catalog_finishes_and_leaves_no_rollback_copy(self):
        import gc
        import json
        import os
        import sqlite3
        import tempfile
        from pathlib import Path
        import plugin_install
        gc.disable()                 # a leaked connection must not be rescued by the collector
        self.addCleanup(gc.enable)
        with tempfile.TemporaryDirectory() as tmp:
            manager = Path(tmp) / "manager"
            data = Path(tmp) / "game" / "Data Files"
            package = manager / "staging" / "plugins" / "Mod"
            package.mkdir(parents=True)
            data.mkdir(parents=True)
            source = package / "Mod.esp"
            source.write_bytes(b"payload")
            os.link(source, data / "Mod.esp")
            import sys
            root = Path(__file__).resolve().parents[1]
            sys.path.insert(0, str(root / "database"))
            import catalog as catalog_module
            connection = sqlite3.connect(manager / "game_catalog.sqlite3")
            catalog_module.create_schema(connection, root / "database" / "schema.sql")
            connection.commit()
            connection.close()
            state = {"version": 1, "deployments": [{
                "id": "mod", "label": "Mod", "plugins": ["Mod.esp"], "archives": [], "registered_archives": [],
                "staging_package": str(package),
                "operations": [{"source": str(source), "target": str(data / "Mod.esp"), "backup": None,
                                "size": 7, "sha256": None}]}]}
            (manager / "plugin_deployments.json").write_text(json.dumps(state), encoding="utf-8")
            plugin_install.remove_deployments(manager, data, {"mod"})
            self.assertFalse((data / "Mod.esp").exists())
            self.assertEqual(list(manager.glob(".game_catalog.sqlite3.*.rollback")), [])
            # Removing every deployment also deletes the catalog cache; a leftover
            # rollback copy or an open handle would still be a failure.
            self.assertFalse((manager / "game_catalog.sqlite3").exists())
            self.assertEqual(list(manager.glob("game_catalog.sqlite3*")), [])

    def test_partial_removal_leaves_the_catalog_and_holds_no_handle(self):
        import gc
        import json
        import os
        import sqlite3
        import tempfile
        from pathlib import Path
        import plugin_install
        gc.disable()
        self.addCleanup(gc.enable)
        with tempfile.TemporaryDirectory() as tmp:
            manager = Path(tmp) / "manager"
            data = Path(tmp) / "game" / "Data Files"
            pkg_a = manager / "staging" / "plugins" / "A"
            pkg_b = manager / "staging" / "plugins" / "B"
            pkg_a.mkdir(parents=True)
            pkg_b.mkdir(parents=True)
            data.mkdir(parents=True)
            (pkg_a / "A.esp").write_bytes(b"A")
            (pkg_b / "B.esp").write_bytes(b"B")
            os.link(pkg_a / "A.esp", data / "A.esp")
            os.link(pkg_b / "B.esp", data / "B.esp")
            import sys
            root = Path(__file__).resolve().parents[1]
            sys.path.insert(0, str(root / "database"))
            import catalog as catalog_module
            connection = sqlite3.connect(manager / "game_catalog.sqlite3")
            catalog_module.create_schema(connection, root / "database" / "schema.sql")
            connection.commit()
            connection.close()
            def ops(package, name):
                return [{"source": str(package / name), "target": str(data / name), "backup": None,
                         "size": 1, "sha256": None}]
            state = {"version": 1, "deployments": [
                {"id": "a", "label": "A", "plugins": ["A.esp"], "archives": [], "registered_archives": [],
                 "staging_package": str(pkg_a), "operations": ops(pkg_a, "A.esp")},
                {"id": "b", "label": "B", "plugins": ["B.esp"], "archives": [], "registered_archives": [],
                 "staging_package": str(pkg_b), "operations": ops(pkg_b, "B.esp")},
            ]}
            (manager / "plugin_deployments.json").write_text(json.dumps(state), encoding="utf-8")
            plugin_install.remove_deployments(manager, data, {"a"})
            self.assertFalse((data / "A.esp").exists())
            self.assertTrue((data / "B.esp").is_file())
            self.assertEqual(list(manager.glob(".game_catalog.sqlite3.*.rollback")), [])
            os.replace(manager / "game_catalog.sqlite3", manager / "moved.sqlite3")   # nothing holds it open
            self.assertTrue((manager / "moved.sqlite3").is_file())

    def test_catalog_context_closes_the_connection(self):
        import os
        import tempfile
        from pathlib import Path
        import plugin_install
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.sqlite3"
            with plugin_install._catalog(path) as db:
                db.execute("CREATE TABLE t (x)")
            os.unlink(path)          # WinError 32 while the connection is open
            self.assertFalse(path.exists())

class PlayWhileUnavailable(unittest.TestCase):
    def test_one_message_with_the_core_file_hint_first_and_no_play_will_continue(self):
        from types import SimpleNamespace
        from unittest import mock
        shown = []
        fake = SimpleNamespace(
            _resolve_pending_load_order=lambda _why: True, _update_action_buttons=lambda: None,
            play_button=SimpleNamespace(instate=lambda states: states == ["disabled"]),
            settings={"game_dir": "G:\\Games\\Morrowind", "init_complete": 1, "first_run": 0},
            status=SimpleNamespace(set=lambda text: None))
        play_game = next(getattr(c, "play_game") for c in vars(mwmanager_gui).values()
                         if isinstance(c, type) and "play_game" in vars(c))
        with mock.patch.object(mwmanager_gui, "core_file_hint", return_value="Tribunal.esm is altered.\n\n"), \
                mock.patch.object(mwmanager_gui.messagebox, "showinfo",
                                  side_effect=lambda title, text, **kw: shown.append((title, text))), \
                mock.patch("mwmanager_core_game_files.ensure_core_game_files_for_play") as core_dialog:
            play_game(fake)
        core_dialog.assert_not_called()
        self.assertEqual(len(shown), 1)
        title, text = shown[0]
        self.assertEqual(title, "Generate Patches first")
        self.assertTrue(text.startswith("Tribunal.esm is altered."))
        self.assertNotIn("Play will continue", text)

class GenerateWhenUpToDate(unittest.TestCase):
    def test_click_says_patches_are_up_to_date(self):
        from types import SimpleNamespace
        from unittest import mock
        shown, status = [], []
        fake = SimpleNamespace(
            _active_job=None, generate_button=SimpleNamespace(instate=lambda states: True),
            _current_generation_signature=lambda: "sig", new_plugins=lambda: [],
            gui_state={"generated_signature": "sig"}, status=SimpleNamespace(set=status.append),
            save=mock.Mock(side_effect=AssertionError("nothing should run")))
        with mock.patch.object(mwmanager_gui.messagebox, "showinfo",
                               side_effect=lambda title, text, **kw: shown.append(title)):
            mwmanager_gui.ManagerGUI.generate_patches(fake)
        self.assertEqual(shown, ["Patches are up to date"])
        self.assertIn("Ctrl-click Generate Patches", status[0])


if __name__ == "__main__":
    unittest.main()
