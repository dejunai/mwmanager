"""Generated files are recorded and removed on rollback and when every mod is removed.

Install or generate into a temporary manager and game folder, then remove every
deployment or roll back: outputs, dummy stand-ins, caches, hash records, generated
patches, and recorded game-side files are gone, empty generated folders are gone,
and user files plus game originals survive. Nothing outside the generated roots
is deleted. Every path here is a temporary folder.
"""
import json
import os
import sqlite3
import struct
import tempfile
import py_compat
import unittest
from pathlib import Path

import mwmanager_generated
import mwmanager_journal
import mwmanager_upstream
import plugin_install


def _plant_generated(manager: Path, game: Path) -> dict:
    """Write typical generated artifacts and a few files that must survive cleanup."""
    data = game / "Data Files"
    outputs = manager / "outputs"
    dummy = outputs / "dummy" / "meshes"
    cache = outputs / "cache"
    dummy.mkdir(parents=True)
    cache.mkdir(parents=True)
    (dummy / "standin.nif").write_bytes(b"DUMMY")
    (cache / "assets.sqlite").write_bytes(b"CACHE")
    (outputs / "mwdialog_play_hashes.json").write_text("{}", encoding="utf-8")
    (outputs / "luafix_plan.json").write_text("{}", encoding="utf-8")
    (outputs / "report.txt").write_text("report", encoding="utf-8")

    user_ini = manager / "mwmanager.ini"
    user_ini.write_text("[settings]\nkeep=1\n", encoding="utf-8")
    staging = manager / "staging" / "plugins" / "UserMod" / "readme.txt"
    staging.parent.mkdir(parents=True)
    staging.write_text("user staging", encoding="utf-8")
    backup = manager / "backups" / "luafix" / "kept.lua"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"-- original lua")
    outside = manager.parent / "do-not-delete.txt"
    outside.write_text("outside the manager", encoding="utf-8")

    original = data / "Morrowind.esm"
    original.write_bytes(b"TES3 original")
    user_plugin = data / "My Notes.esp"
    user_plugin.write_bytes(b"TES3 user")
    user_doc = data / "Docs" / "my_readme.txt"
    user_doc.parent.mkdir(parents=True)
    user_doc.write_text("user file in Data Files", encoding="utf-8")

    stand_in = data / "Meshes" / "generated" / "missing.nif"
    stand_in.parent.mkdir(parents=True)
    stand_in.write_bytes(b"STANDIN")
    rec = outputs / mwmanager_generated.GENERATED_RECORD_NAME
    rec.write_text(
        "; generated-files record\n[earlier runs (plugin not recorded)]\n  Meshes/generated/missing.nif\t7\n",
        encoding="utf-8",
    )

    patch = data / "mwdialog_check Multipatch.esp"
    patch.write_bytes(b"TES3 patch")
    mwse = data / "MWSE" / "config" / "mwfloatfix_data.json"
    mwse.parent.mkdir(parents=True)
    mwse.write_text("{}", encoding="utf-8")
    catalog = manager / "game_catalog.sqlite3"
    schema = Path(__file__).resolve().parents[1] / "database" / "schema.sql"
    db = sqlite3.connect(catalog)
    try:
        db.executescript(schema.read_text(encoding="utf-8"))
    finally:
        db.close()
    mwmanager_generated.register_game_generated(manager, game, patch)
    mwmanager_generated.register_game_generated(manager, game, mwse)
    # A path-traversal entry must be ignored, not followed.
    secret = manager.parent / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    with open(mwmanager_generated.manifest_path(manager), "a", encoding="utf-8") as handle:
        handle.write("../secret.txt\n")
        handle.write(str(secret) + "\n")

    return {
        "user_ini": user_ini, "staging": staging, "backup": backup, "outside": outside,
        "original": original, "user_plugin": user_plugin, "user_doc": user_doc,
        "stand_in": stand_in, "patch": patch, "mwse": mwse, "catalog": catalog,
        "secret": secret, "dummy": dummy / "standin.nif",
        "hashes": outputs / "mwdialog_play_hashes.json",
    }


def _assert_generated_gone(test, manager: Path, game: Path, planted: dict):
    data = game / "Data Files"
    test.assertFalse((manager / "outputs").exists(), "outputs/ should be removed once empty")
    test.assertFalse(planted["stand_in"].exists())
    test.assertFalse(planted["patch"].exists())
    test.assertFalse(planted["mwse"].exists())
    test.assertFalse(planted["catalog"].exists())
    test.assertFalse(planted["dummy"].exists())
    test.assertFalse(planted["hashes"].exists())
    test.assertFalse((data / "Meshes" / "generated").exists(), "empty generated folder should be pruned")
    if (data / "MWSE").exists():
        test.assertFalse(any(path.is_file() for path in (data / "MWSE").rglob("*")),
                         "generated MWSE files should be gone")
    test.assertEqual(mwmanager_generated.leftovers(manager, game), [])
    # Survivors
    test.assertEqual(planted["user_ini"].read_text(encoding="utf-8"), "[settings]\nkeep=1\n")
    test.assertEqual(planted["staging"].read_text(encoding="utf-8"), "user staging")
    test.assertEqual(planted["backup"].read_bytes(), b"-- original lua")
    test.assertEqual(planted["outside"].read_text(encoding="utf-8"), "outside the manager")
    test.assertEqual(planted["original"].read_bytes(), b"TES3 original")
    test.assertEqual(planted["user_plugin"].read_bytes(), b"TES3 user")
    test.assertEqual(planted["user_doc"].read_text(encoding="utf-8"), "user file in Data Files")
    test.assertTrue(planted["secret"].is_file())
    test.assertTrue((data / "Docs").is_dir())
    test.assertTrue(data.is_dir())
    test.assertTrue(game.is_dir())


class GeneratedCleanup(unittest.TestCase):
    def test_cleanup_sweeps_roots_and_registered_game_files(self):
        with py_compat.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp).resolve()
            manager, game = base / "MWManager", base / "Game"
            (game / "Data Files").mkdir(parents=True)
            manager.mkdir()
            planted = _plant_generated(manager, game)
            report = mwmanager_generated.cleanup_generated(manager, game)
            self.assertTrue(report["removed"])
            _assert_generated_gone(self, manager, game, planted)

    def test_removing_every_mod_cleans_generated_files(self):
        with py_compat.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp).resolve()
            manager, game = base / "MWManager", base / "Game"
            data = game / "Data Files"
            package = manager / "staging" / "plugins" / "Mod"
            data.mkdir(parents=True)
            manager.mkdir()
            (package).mkdir(parents=True)
            (package / "Mod.esp").write_bytes(b"plugin")
            _linked, _skipped, operations = plugin_install.deploy_package(package, data, "Mod.esp")
            state = {"version": 1, "deployments": [{
                "id": "mod", "label": "Mod", "plugins": ["Mod.esp"], "archives": [],
                "registered_archives": [], "staging_package": str(package), "mod": "Mod",
                "operations": operations,
            }]}
            (manager / "plugin_deployments.json").write_text(json.dumps(state), encoding="utf-8")
            planted = _plant_generated(manager, game)
            self.assertTrue((data / "Mod.esp").is_file())
            plugin_install.remove_deployments(manager, data, {"mod"})
            self.assertFalse((data / "Mod.esp").exists())
            _assert_generated_gone(self, manager, game, planted)
            # Staging package (the mod original) and other user files remain.
            self.assertTrue((package / "Mod.esp").is_file())

    def test_removing_one_mod_leaves_generated_files(self):
        with py_compat.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp).resolve()
            manager, game = base / "MWManager", base / "Game"
            data = game / "Data Files"
            pkg_a = manager / "staging" / "plugins" / "A"
            pkg_b = manager / "staging" / "plugins" / "B"
            data.mkdir(parents=True)
            manager.mkdir()
            pkg_a.mkdir(parents=True)
            pkg_b.mkdir(parents=True)
            (pkg_a / "A.esp").write_bytes(b"A")
            (pkg_b / "B.esp").write_bytes(b"B")
            _l, _s, ops_a = plugin_install.deploy_package(pkg_a, data, "A.esp")
            _l, _s, ops_b = plugin_install.deploy_package(pkg_b, data, "B.esp")
            state = {"version": 1, "deployments": [
                {"id": "a", "label": "A", "plugins": ["A.esp"], "archives": [],
                 "registered_archives": [], "staging_package": str(pkg_a), "mod": "A",
                 "operations": ops_a},
                {"id": "b", "label": "B", "plugins": ["B.esp"], "archives": [],
                 "registered_archives": [], "staging_package": str(pkg_b), "mod": "B",
                 "operations": ops_b},
            ]}
            (manager / "plugin_deployments.json").write_text(json.dumps(state), encoding="utf-8")
            planted = _plant_generated(manager, game)
            plugin_install.remove_deployments(manager, data, {"a"})
            self.assertFalse((data / "A.esp").exists())
            self.assertTrue((data / "B.esp").is_file())
            self.assertTrue(planted["stand_in"].is_file())
            self.assertTrue((manager / "outputs").is_dir())
            self.assertTrue(planted["catalog"].is_file())

    def test_rollback_cleans_generated_files(self):
        with py_compat.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp).resolve()
            manager, game = base / "MWManager", base / "Game"
            data = game / "Data Files"
            data.mkdir(parents=True)
            manager.mkdir()
            (data / "Morrowind.esm").write_bytes(b"TES3 original")
            journal = mwmanager_journal.Journal(manager / "journal")
            journal.capture_baseline(game)
            planted = _plant_generated(manager, game)
            # A generated stand-in journaled as a create, so rollback deletes it too.
            run = journal.start_run("test", {"operation": "generate missing assets"})
            journal.record(
                run["run"], "Data Files/Meshes/generated/missing.nif", "create",
                mwmanager_journal.sha256_file(planted["stand_in"]),
                {"kind": "absent"}, tag="generated-asset",
            )
            journal.finish_run(run)
            plan = journal.plan_rollback(game)
            self.assertFalse(plan.get("refuse"), plan.get("refuse"))
            journal.execute_rollback(game, plan)
            _assert_generated_gone(self, manager, game, planted)
            self.assertEqual((data / "Morrowind.esm").read_bytes(), b"TES3 original")

    def test_register_rejects_paths_outside_the_game_folder(self):
        with py_compat.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp).resolve()
            manager, game = base / "MWManager", base / "Game"
            (game / "Data Files").mkdir(parents=True)
            manager.mkdir()
            outsider = base / "outside.bin"
            outsider.write_bytes(b"no")
            self.assertFalse(mwmanager_generated.register_game_generated(manager, game, outsider))
            self.assertFalse(mwmanager_generated.manifest_path(manager).is_file())

    def test_write_plugin_bytes_registers_a_new_generated_plugin(self):
        with py_compat.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp).resolve()
            manager, game = base / "MWManager", base / "Game"
            data = game / "Data Files"
            data.mkdir(parents=True)
            manager.mkdir()
            (data / "Morrowind.esm").write_bytes(b"TES3 original")
            with mwmanager_upstream.use_manager_dir(manager):
                import mwdialog_check
                journal = mwmanager_journal.Journal(manager / "journal")
                journal.capture_baseline(game)
                target = data / "mwmanager - ESM fixes.esp"
                plugin = b"TES3" + struct.pack("<III", 0, 0, 0)
                mwdialog_check.write_plugin_bytes(str(target), plugin)
                self.assertTrue(target.is_file())
                lines = mwmanager_generated.manifest_path(manager).read_text(encoding="utf-8")
                self.assertIn("Data Files/mwmanager - ESM fixes.esp", lines.replace("\\", "/"))
                mwmanager_generated.cleanup_generated(manager, game)
                self.assertFalse(target.exists())
                self.assertEqual((data / "Morrowind.esm").read_bytes(), b"TES3 original")


if __name__ == "__main__":
    unittest.main()
