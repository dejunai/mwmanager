"""The major-master review must make an outdated row's Update button usable straight away."""
import sys
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core_addons  # noqa: E402


def _widgets(widget):
    for child in widget.winfo_children():
        yield child
        yield from _widgets(child)


class OutdatedRowStartsSelected(unittest.TestCase):
    def test_update_button_is_live_when_the_window_opens(self):
        rows = [{"name": "Tamriel_Data.esm", "deps": [], "error": None, "installed": True, "selected": False,
                 "path": "x", "nexus": {"status": "current"}, "page": "https://www.nexusmods.com/a"},
                {"name": "TR_Mainland.esm", "deps": [], "error": None, "installed": False, "selected": True,
                 "path": "tr.7z", "nexus": {"status": "outdated"}, "page": "https://www.nexusmods.com/b"}]

        def readiness():
            for row in rows:
                row["reasons"], row["ready"] = [], True

        prepared = {"rows": rows, "index_by_name": {r["name"].casefold(): i for i, r in enumerate(rows)},
                    "readiness": readiness, "select_dependencies": lambda i: (True, None),
                    "required_by_selection": set}
        seen = {}

        def inspect(window):
            tree = next(w for w in _widgets(window) if isinstance(w, ttk.Treeview))
            button = next(w for w in _widgets(window) if isinstance(w, ttk.Button) and "Update" in w.cget("text"))
            seen["focus"] = tree.item(tree.focus(), "text")
            seen["enabled"] = not button.instate(["disabled"])
            window.destroy()

        try:
            with mock.patch.object(tk.Tk, "mainloop", inspect):
                core_addons.review_core_master_install("m", "g", {}, prepared=prepared)
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.assertEqual(seen, {"focus": "TR_Mainland.esm", "enabled": True})


    def test_swapping_tamriel_data_variant_saves_the_choice_and_rescans(self):
        import tempfile
        rows = [{"name": "Tamriel_Data.esm", "deps": [], "error": None, "installed": False, "selected": True,
                 "path": "hd.7z", "nexus": {"status": "current"}, "page": "https://www.nexusmods.com/a",
                 "reasons": [], "ready": True}]
        prepared = {"rows": rows, "index_by_name": {"tamriel_data.esm": 0}, "readiness": lambda: None,
                    "select_dependencies": lambda i: (True, None), "required_by_selection": set}
        plan = {"master_variants": {"tamriel_data.esm": {"available": ["HD", "SD"], "using": "HD"}}}

        def click(window):
            tree = next(w for w in _widgets(window) if isinstance(w, ttk.Treeview))
            tree.focus(tree.get_children()[0])
            tree.event_generate("<<TreeviewSelect>>")
            window.update()
            next(w for w in _widgets(window) if isinstance(w, ttk.Button) and w.cget("text") == "Use SD").invoke()

        with tempfile.TemporaryDirectory() as manager:
            try:
                with mock.patch.object(tk.Tk, "mainloop", click):
                    result = core_addons.review_core_master_install(manager, "g", plan, prepared=prepared)
            except tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
            self.assertEqual(result, "rescan")
            self.assertEqual(core_addons.master_variant_choices(manager), {"tamriel_data.esm": "SD"})




class MasterScanSkipsAssets(unittest.TestCase):
    def test_staged_masters_never_walks_into_asset_folders(self):
        import os
        import tempfile
        import major_master_deploy
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp, "TR")
            (package / "Sound" / "Vo" / "AIV").mkdir(parents=True)
            (package / "Data Files").mkdir()
            (package / "Data Files" / "TR_Mainland.esm").write_bytes(b"TES3")
            visited = []
            real_walk = os.walk

            def walk(top, *a, **k):
                for base, dirs, files in real_walk(top, *a, **k):
                    visited.append(Path(base).name.casefold())
                    yield base, dirs, files

            with mock.patch.object(major_master_deploy.os, "walk", walk):
                found = major_master_deploy.staged_masters(Path(tmp), ["TR_Mainland.esm"])
            self.assertEqual(list(found), ["tr_mainland.esm"])
            self.assertNotIn("sound", visited)

def _run_until_closed(window, limit=400):
    for _ in range(limit):
        try:
            if not window.winfo_exists():
                return
            window.update()
        except tk.TclError:          # a destroyed root Tk refuses even winfo_exists
            return
        import time
        time.sleep(0.01)


def _prepared(rows):
    for row in rows:
        row.setdefault("reasons", [])
        row.setdefault("ready", True)
    return {"rows": rows, "index_by_name": {r["name"].casefold(): i for i, r in enumerate(rows)},
            "readiness": lambda: None, "select_dependencies": lambda i: (True, None),
            "required_by_selection": set}


class BackgroundDownloads(unittest.TestCase):
    def test_update_downloads_the_newest_file_without_a_dialog_then_rescans(self):
        import mwnexus_browser
        rows = [{"name": "TR_Mainland.esm", "deps": [], "error": None, "installed": False, "selected": True,
                 "path": "tr.7z", "page": "https://www.nexusmods.com/b",
                 "nexus": {"status": "outdated", "mod_id": 42145, "file_id": 1000074409}}]

        def click(window):
            next(w for w in _widgets(window) if isinstance(w, ttk.Button) and "Update" in w.cget("text")).invoke()
            _run_until_closed(window)

        with mock.patch.object(tk.Tk, "mainloop", click), \
                mock.patch.object(mwnexus_browser, "download_file", return_value=Path("tr.7z")) as download, \
                mock.patch.object(mwnexus_browser, "close_browser"), \
                mock.patch("mwmanager_dialogs._prompt") as prompt:
            try:
                result = core_addons.review_core_master_install("m", "g", {"downloads_dir": "dl"},
                                                                prepared=_prepared(rows))
            except tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
        self.assertEqual(result, "rescan")
        self.assertEqual(download.call_args.args[:2],
                         ("https://www.nexusmods.com/morrowind/mods/42145?tab=files&file_id=1000074409", "dl"))
        prompt.assert_not_called()


class OtherTamrielDataVariant(unittest.TestCase):
    def review(self, manager, answer):
        import mwnexus_browser
        rows = [{"name": "Tamriel_Data.esm", "deps": [], "error": None, "installed": False, "selected": True,
                 "path": "hd.7z", "page": "https://www.nexusmods.com/a", "nexus": {"status": "current"}}]
        plan = {"downloads_dir": "dl", "master_variants": {"tamriel_data.esm": {"available": ["HD"], "using": "HD"}}}

        def wait(window):
            _run_until_closed(window, 150)
            try:
                window.destroy()
            except tk.TclError:
                pass

        with mock.patch.object(tk.Tk, "mainloop", wait), \
                mock.patch("mwmanager_dialogs._prompt", return_value=answer) as prompt, \
                mock.patch.object(core_addons, "nexus_main_file", return_value={"fileId": 1000068848}), \
                mock.patch.object(mwnexus_browser, "download_file", return_value=Path("sd.7z")) as download, \
                mock.patch.object(mwnexus_browser, "close_browser"):
            try:
                result = core_addons.review_core_master_install(manager, "g", plan, prepared=_prepared(rows))
            except tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
        return result, prompt, download

    def test_keep_and_dont_ask_again_is_remembered(self):
        import tempfile
        with tempfile.TemporaryDirectory() as manager:
            _result, prompt, download = self.review(manager, "remember")
            self.assertIn("SD version", prompt.call_args.args[1])
            self.assertIn("vanilla-resolution", prompt.call_args.args[1])
            self.assertEqual(core_addons.master_variant_choices(manager), {"tamriel_data.esm": "HD"})
            download.assert_not_called()
            _result, prompt, _download = self.review(manager, "keep")
            prompt.assert_not_called()                 # remembered: the other variant is ignored

    def test_download_fetches_the_other_variant_and_uses_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as manager:
            result, _prompt, download = self.review(manager, "download")
            self.assertEqual(result, "rescan")
            self.assertEqual(download.call_args.args[0],
                             "https://www.nexusmods.com/morrowind/mods/44537?tab=files&file_id=1000068848")
            self.assertEqual(core_addons.master_variant_choices(manager), {"tamriel_data.esm": "SD"})


if __name__ == "__main__":
    unittest.main()
