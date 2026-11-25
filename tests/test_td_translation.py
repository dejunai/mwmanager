"""Copy Tamriel Data's Translation.txt into tools/ when that master is staged.

Tests use a tiny made-up map only; never Tamriel Data's real file.
"""
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import core_addons as ca
import td_translation as td


def write_map(path: Path, lines=("old_crate:T_Crate", "tr_widget:T_Widget")):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="cp1252")
    return path


def marker(package: Path, **fields):
    data = {
        "archive": "Tamriel Data (HD) 59927 26.08.7z",
        "sha256": "a" * 64,
        "PLUGIN_VERSION": "26.08",
        "selected_masters": ["Tamriel_Data.esm"],
        **fields,
    }
    (package / td.STAGE_MARKER).write_text(json.dumps(data), encoding="utf-8")
    return data


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FindAndCopy(unittest.TestCase):
    def test_finds_docs_translation_and_prefers_exact_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "td"
            write_map(package / "00 Data Files" / "Docs" / "Translation.txt")
            write_map(package / "00 Data Files" / "Docs" / "Tamriel Data (HD) - Translation.txt",
                      ("older:T_Older",))
            found = td.find_translation(package)
            self.assertEqual(found.name, "Translation.txt")
            self.assertEqual(found.parent.name, "Docs")

    def test_missing_file_does_not_raise_or_create_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, tools, manager = root / "pkg", root / "tools", root / "manager"
            package.mkdir()
            (package / "Tamriel_Data.esm").write_bytes(b"esm")
            result = td.copy_from_package(package, tools, manager_dir=manager)
            self.assertEqual(result["status"], "missing")
            self.assertIn("ships with Tamriel Data", result["warning"])
            self.assertFalse(td.translation_path(tools).exists())
            self.assertFalse(td.rebuild_is_pending(manager))

    def test_garbage_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, tools = root / "pkg", root / "tools"
            (package / "Docs").mkdir(parents=True)
            (package / "Docs" / "Translation.txt").write_text("this is a readme\n", encoding="cp1252")
            result = td.copy_from_package(package, tools)
            self.assertEqual(result["status"], "skipped")
            self.assertIn("does not look like", result["warning"])
            self.assertFalse(td.translation_path(tools).exists())

    def test_first_stage_copies_the_file_and_records_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, tools, manager = root / "pkg", root / "tools", root / "manager"
            manager.mkdir()
            source = write_map(package / "Docs" / "Translation.txt")
            marker(package, sha256="b" * 64, PLUGIN_VERSION="26.08")
            result = td.copy_from_package(package, tools, manager_dir=manager)
            dest = td.translation_path(tools)
            self.assertEqual(result["status"], "copied")
            self.assertTrue(result["content_changed"])
            self.assertTrue(result["rebuild_queued"])
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_bytes(), source.read_bytes())
            meta = td.read_meta(tools)
            self.assertEqual(meta["source_version"], "26.08")
            self.assertEqual(meta["source_archive_sha256"], "b" * 64)
            self.assertEqual(meta["file_sha256"], sha(dest))
            self.assertTrue(td.rebuild_is_pending(manager))
            alert = td.pending_alert(manager)
            self.assertIn("Translation.txt has changed", alert)
            self.assertIn("Generate Patches", alert)
            self.assertIn("--full", alert)

    def test_same_file_is_not_copied_again_and_does_not_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, tools, manager = root / "pkg", root / "tools", root / "manager"
            manager.mkdir()
            write_map(package / "Docs" / "Translation.txt")
            marker(package)
            td.copy_from_package(package, tools, manager_dir=manager)
            td.clear_rebuild(manager)
            dest = td.translation_path(tools)
            before = dest.read_bytes()
            mtime = dest.stat().st_mtime_ns
            result = td.copy_from_package(package, tools, manager_dir=manager)
            self.assertEqual(result["status"], "unchanged")
            self.assertFalse(result["content_changed"])
            self.assertFalse(result["rebuild_queued"])
            self.assertFalse(td.rebuild_is_pending(manager))
            self.assertEqual(dest.read_bytes(), before)
            self.assertEqual(dest.stat().st_mtime_ns, mtime)

    def test_newer_archive_with_changed_file_refreshes_and_queues_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, tools, manager = root / "pkg", root / "tools", root / "manager"
            manager.mkdir()
            write_map(package / "Docs" / "Translation.txt")
            marker(package, sha256="a" * 64, PLUGIN_VERSION="26.08")
            first = td.copy_from_package(package, tools, manager_dir=manager)
            self.assertEqual(first["status"], "copied")
            td.clear_rebuild(manager)
            write_map(package / "Docs" / "Translation.txt",
                      ("old_crate:T_Crate", "tr_widget:T_Widget", "tr_new:T_New"))
            marker(package, sha256="c" * 64, PLUGIN_VERSION="26.11.19",
                   archive="Tamriel Data (HD) 59927 26.11.19.7z")
            result = td.copy_from_package(package, tools, manager_dir=manager)
            self.assertEqual(result["status"], "updated")
            self.assertTrue(result["content_changed"])
            self.assertTrue(result["rebuild_queued"])
            self.assertTrue(td.rebuild_is_pending(manager))
            meta = td.read_meta(tools)
            self.assertEqual(meta["source_version"], "26.11.19")
            self.assertEqual(meta["source_archive_sha256"], "c" * 64)
            self.assertIn("tr_new:T_New", td.translation_path(tools).read_text(encoding="cp1252"))
            alert = result["alert"]
            self.assertIn("has changed", alert)
            self.assertIn("26.11.19", alert)
            self.assertIn("--full", alert)

    def test_same_bytes_from_a_new_archive_update_the_record_but_do_not_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, tools, manager = root / "pkg", root / "tools", root / "manager"
            manager.mkdir()
            write_map(package / "Docs" / "Translation.txt")
            marker(package, sha256="a" * 64, PLUGIN_VERSION="26.08")
            td.copy_from_package(package, tools, manager_dir=manager)
            td.clear_rebuild(manager)
            marker(package, sha256="d" * 64, PLUGIN_VERSION="26.11.19",
                   archive="Tamriel Data (HD) 59927 26.11.19.7z")
            result = td.copy_from_package(package, tools, manager_dir=manager)
            self.assertEqual(result["status"], "unchanged")
            self.assertFalse(result["content_changed"])
            self.assertFalse(result["rebuild_queued"])
            self.assertFalse(td.rebuild_is_pending(manager))
            self.assertIsNone(result["alert"])
            self.assertEqual(td.read_meta(tools)["source_version"], "26.11.19")

    def test_tr_update_uses_sibling_tamriel_data_and_mentions_tr_in_the_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, tools, manager = root / "staging", root / "tools", root / "manager"
            manager.mkdir()
            td_pkg = staging / "Tamriel Data (HD)"
            tr_pkg = staging / "Tamriel Rebuilt"
            write_map(td_pkg / "00 Data Files" / "Docs" / "Translation.txt",
                      ("old_crate:T_Crate", "tr_widget:T_NewWidget"))
            marker(td_pkg, sha256="e" * 64, PLUGIN_VERSION="26.11.19",
                   archive="Tamriel Data (HD) 59927 26.11.19.7z")
            (tr_pkg / "TR_Mainland.esm").parent.mkdir(parents=True, exist_ok=True)
            (tr_pkg / "TR_Mainland.esm").write_bytes(b"esm")
            marker(tr_pkg, archive="Tamriel Rebuilt 26.11.19.7z", sha256="f" * 64,
                   PLUGIN_VERSION="26.11.19", selected_masters=["TR_Mainland.esm"])
            result = td.refresh_from_staging(
                staging, tools_dir=tools, manager_dir=manager, packages=[tr_pkg, td_pkg])
            self.assertEqual(result["status"], "copied")
            self.assertTrue(result["content_changed"])
            self.assertTrue(result["rebuild_queued"])
            self.assertTrue(td.translation_path(tools).is_file())
            self.assertIn("Tamriel Rebuilt was updated", result["alert"])
            self.assertIn("Translation.txt from the staged Tamriel Data has changed", result["alert"])
            self.assertTrue(td.rebuild_is_pending(manager))

    def test_tr_alone_without_the_file_warns_and_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, tools, manager = root / "staging", root / "tools", root / "manager"
            manager.mkdir()
            tr_pkg = staging / "Tamriel Rebuilt"
            (tr_pkg / "TR_Mainland.esm").parent.mkdir(parents=True, exist_ok=True)
            (tr_pkg / "TR_Mainland.esm").write_bytes(b"esm")
            marker(tr_pkg, archive="Tamriel Rebuilt 26.11.19.7z",
                   selected_masters=["TR_Mainland.esm"])
            result = td.refresh_from_staging(staging, tools_dir=tools, manager_dir=manager)
            self.assertEqual(result["status"], "missing")
            self.assertIn("ships with Tamriel Data", result["warning"])
            self.assertFalse(td.rebuild_is_pending(manager))
            self.assertFalse(td.translation_path(tools).exists())

    def test_clear_rebuild_drops_the_queued_full_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = Path(tmp)
            td._queue_rebuild(
                manager,
                info={"source_archive": "a.7z", "source_version": "1", "source_archive_sha256": "ab"},
                file_sha256="cd",
                alert="Translation.txt has changed. Run --full.",
            )
            self.assertTrue(td.rebuild_is_pending(manager))
            self.assertIn("has changed", td.pending_alert(manager))
            prompt = td.rebuild_offer_text(manager, "The load order changed. Run --full now?")
            self.assertIn("The load order changed", prompt)
            self.assertIn("Translation.txt has changed", prompt)
            td.clear_rebuild(manager)
            self.assertFalse(td.rebuild_is_pending(manager))
            self.assertIsNone(td.pending_alert(manager))
            self.assertEqual(td.rebuild_offer_text(manager, "The load order changed. Run --full now?"),
                             "The load order changed. Run --full now?")

    def test_unreadable_tools_dir_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "pkg"
            write_map(package / "Docs" / "Translation.txt")
            marker(package)
            blocked = root / "blocked"
            blocked.mkdir()
            with mock.patch("td_translation._atomic_copy", side_effect=OSError("access denied")):
                result = td.copy_from_package(package, blocked, manager_dir=root / "manager")
            self.assertEqual(result["status"], "error")
            self.assertIn("Could not copy", result["warning"])
            self.assertFalse(td.rebuild_is_pending(root / "manager"))


class StagingHook(unittest.TestCase):
    def test_stage_core_master_archives_copies_translation_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, game = root / "manager", root / "game"
            manager.mkdir()
            game.mkdir()
            archive = root / "Tamriel Data (HD) 26.08.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("Tamriel_Data.esm", b"esm-placeholder")
                stream.writestr("00 Data Files/Docs/Translation.txt",
                                "old_crate:T_Crate\ntr_widget:T_Widget\n")
            plan = {"archives": [{
                "archive": str(archive),
                "core_masters": ["Tamriel_Data.esm"],
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "filename_version": "26.08",
                "member_count": 2,
                "install_blocked": False,
            }]}
            staged = ca.stage_core_master_archives(
                plan, ["Tamriel_Data.esm"], manager, game, clear_staging=False)
            self.assertEqual(len(staged), 1)
            dest = manager / "tools" / "Translation.txt"
            self.assertTrue(dest.is_file(), "staging should copy Translation.txt into tools/")
            self.assertIn("old_crate:T_Crate", dest.read_text(encoding="cp1252"))
            meta = json.loads((manager / "tools" / "Translation.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["source_version"], "26.08")
            self.assertTrue(td.rebuild_is_pending(manager))
            self.assertIn("Translation.txt has changed", td.pending_alert(manager))

    def test_restaging_an_updated_map_queues_another_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, game = root / "manager", root / "game"
            manager.mkdir()
            game.mkdir()

            def stage(name, lines, version, digest_seed):
                archive = root / name
                with zipfile.ZipFile(archive, "w") as stream:
                    stream.writestr("Tamriel_Data.esm", b"esm-placeholder")
                    stream.writestr("00 Data Files/Docs/Translation.txt",
                                    "\n".join(lines) + "\n")
                plan = {"archives": [{
                    "archive": str(archive),
                    "core_masters": ["Tamriel_Data.esm"],
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "filename_version": version,
                    "member_count": 2,
                    "install_blocked": False,
                }]}
                return ca.stage_core_master_archives(
                    plan, ["Tamriel_Data.esm"], manager, game, clear_staging=False)

            stage("td-old.zip", ["old_crate:T_Crate"], "26.08", "1")
            td.clear_rebuild(manager)
            stage("td-new.zip", ["old_crate:T_Crate", "tr_new:T_New"], "26.11.19", "2")
            dest = manager / "tools" / "Translation.txt"
            self.assertIn("tr_new:T_New", dest.read_text(encoding="cp1252"))
            self.assertTrue(td.rebuild_is_pending(manager))
            self.assertEqual(td.read_meta(manager / "tools")["source_version"], "26.11.19")


class PathsAndHooks(unittest.TestCase):
    def test_default_tools_dir_matches_td_map_convention(self):
        tools = td.default_tools_dir(ROOT)
        self.assertEqual(tools, ROOT / "tools")
        self.assertEqual(td.translation_path(tools).name, "Translation.txt")
        # The reader still looks at tools/Translation.txt beside the scripts.
        text = (ROOT / "mwdialog_patchfile.py").read_text(encoding="utf-8")
        self.assertIn('os.path.dirname(os.path.abspath(__file__)), "tools", "Translation.txt")', text)

    def test_module_and_tests_have_no_absolute_paths(self):
        files = [ROOT / "td_translation.py", Path(__file__)]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r'(?i)\b[A-Z]:\\', msg=f"absolute path in {path.name}")

    def test_gui_offers_full_and_clears_the_queue_after_a_full_run(self):
        # The GUI hooks are source-checked so a typo would fail this without opening a window.
        gui = (ROOT / "mwmanager_gui.py").read_text(encoding="utf-8")
        self.assertIn("import td_translation", gui)
        self.assertIn("td_translation.rebuild_is_pending(APP_DIR)", gui)
        self.assertIn("td_translation.pending_alert(APP_DIR)", gui)
        self.assertIn("td_translation.rebuild_offer_text(APP_DIR", gui)
        self.assertIn("td_translation.clear_rebuild(APP_DIR)", gui)
        addons = (ROOT / "core_addons.py").read_text(encoding="utf-8")
        self.assertIn("import td_translation", addons)
        self.assertIn("td_translation.refresh_from_staging", addons)


class PlainStatus(unittest.TestCase):
    """88201bb's plain-status rules: the job window gets one plain line, the detail log gets the rest."""

    def _window_and_detail(self, lines):
        from mwmanager_workflow import UserOutput
        out = UserOutput()
        shown = [text for line in lines for text in out.feed(line)]
        return shown, list(lines)

    def test_copy_shows_one_plain_line_and_keeps_paths_for_the_detail_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, tools, manager = root / "staging", root / "tools", root / "manager"
            manager.mkdir()
            package = staging / "td"
            write_map(package / "Docs" / "Translation.txt")
            marker(package)
            lines = []
            td.refresh_from_staging(staging, tools_dir=tools, manager_dir=manager, progress_callback=lines.append)
            shown, detail = self._window_and_detail(lines)
            self.assertEqual(len(shown), 1, shown)
            self.assertIn("Updated Translation.txt", shown[0])
            self.assertNotIn(tmp, shown[0])
            self.assertNotIn("sha256", shown[0])
            self.assertTrue(any(line.startswith(td.DETAIL_PREFIX) and tmp in line for line in detail))

    def test_no_file_before_tamriel_data_is_staged_is_detail_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, tools = root / "staging", root / "tools"
            package = staging / "sky"
            package.mkdir(parents=True)
            (package / "Sky_Main.esm").write_bytes(b"esm")
            lines = []
            td.refresh_from_staging(staging, tools_dir=tools, progress_callback=lines.append)
            shown, detail = self._window_and_detail(lines)
            self.assertEqual(shown, [])
            self.assertTrue(detail and all(line.startswith(td.DETAIL_PREFIX) for line in detail))

    def test_copy_error_is_plain_in_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, tools = root / "staging", root / "tools"
            package = staging / "td"
            write_map(package / "Docs" / "Translation.txt")
            marker(package)
            lines = []
            with mock.patch.object(td, "_atomic_copy", side_effect=OSError(f"denied: {tmp}")):
                td.refresh_from_staging(staging, tools_dir=tools, progress_callback=lines.append)
            shown, detail = self._window_and_detail(lines)
            self.assertEqual(len(shown), 1, shown)
            self.assertIn("could not be updated", shown[0])
            self.assertNotIn(tmp, shown[0])
            self.assertTrue(any(tmp in line for line in detail if line.startswith(td.DETAIL_PREFIX)))


if __name__ == "__main__":
    unittest.main()
