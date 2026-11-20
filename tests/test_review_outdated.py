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


if __name__ == "__main__":
    unittest.main()
