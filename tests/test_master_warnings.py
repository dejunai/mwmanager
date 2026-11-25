"""Warnings when Solstheim Tomb of the Snow Prince is chosen. Temporary folders only."""
import json
import sqlite3
import struct
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "database"))
import catalog  # noqa: E402
import core_addons  # noqa: E402
import master_warnings  # noqa: E402

SCHEMA = ROOT / "database" / "schema.sql"
STOSP = "Solstheim Tomb of the Snow Prince.esm"
STOSP_KEY = STOSP.casefold()
WARNING_PHRASE = "major landmass revision of Bloodmoon's Solstheim"
IMPOSSIBLE_PHRASE = "makes a number of mods impossible to use"


def sub(tag, data):
    return tag + struct.pack("<I", len(data)) + data


def record(tag, *subs):
    body = b"".join(subs)
    return tag + struct.pack("<III", len(body), 0, 0) + body


def plugin(path, *records):
    path.write_bytes(record(b"TES3", sub(b"HEDR", bytes(300))) + b"".join(records))


def exterior(x, y, name=b"Felsaad Coast Region"):
    return record(b"CELL", sub(b"NAME", name + b"\0"), sub(b"DATA", struct.pack("<Iii", 2, x, y)))


def interior(name):
    return record(b"CELL", sub(b"NAME", name + b"\0"), sub(b"DATA", struct.pack("<Iii", 1, 0, 0)))


def land(x, y, height=1):
    return record(b"LAND", sub(b"INTV", struct.pack("<ii", x, y)), sub(b"VHGT", bytes([height])))


def catalog_with(*entries):
    """A temporary game_catalog.sqlite3 filled with (filename, records...)."""
    tmp = tempfile.TemporaryDirectory()
    folder = Path(tmp.name)
    db_path = folder / "game_catalog.sqlite3"
    with closing(sqlite3.connect(db_path)) as db:
        catalog.create_schema(db, SCHEMA)
        for index, (name, recs) in enumerate(entries):
            plugin(folder / name, *recs)
            catalog.index_esm(db, folder / name, index)
    return tmp, db_path


class MasterWarningText(unittest.TestCase):
    def test_shipped_warning_covers_snow_prince_and_not_other_masters(self):
        text = master_warnings.warning_text(STOSP)
        self.assertIsNotNone(text)
        self.assertIn(WARNING_PHRASE, text)
        self.assertIn(IMPOSSIBLE_PHRASE, text)
        self.assertIn("edit Bloodmoon's Solstheim land or cells", text)
        self.assertIn("vanilla Solstheim layout", text)
        self.assertIsNone(master_warnings.warning_text("Tamriel_Data.esm"))
        self.assertIsNone(master_warnings.warning_text("Bloodmoon.esm"))
        record = master_warnings.master_warning(STOSP)
        self.assertTrue(record["summary"].startswith("Warning:"))
        # The shipped file is looked up by the module, not by an absolute path in code.
        self.assertEqual(master_warnings.DEFAULT_WARNINGS_PATH.name, "mwmanager_master_warnings.json")
        self.assertTrue(master_warnings.DEFAULT_WARNINGS_PATH.is_file())

    def test_warning_can_be_loaded_from_a_fixture_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warnings.json"
            path.write_text(json.dumps({
                "example.esm": {"text": "Example warning.", "summary": "Example summary."},
            }), encoding="utf-8")
            config = master_warnings.warnings_config(path)
            self.assertEqual(master_warnings.warning_text("Example.esm", config=config), "Example warning.")
            self.assertIsNone(master_warnings.warning_text(STOSP, config=config))

    def test_missing_or_invalid_config_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            self.assertEqual(master_warnings.warnings_config(missing), {})
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertEqual(master_warnings.warnings_config(bad), {})
            bad.write_text("[]", encoding="utf-8")
            self.assertEqual(master_warnings.warnings_config(bad), {})


class CatalogOverlap(unittest.TestCase):
    def test_lists_plugins_that_share_bloodmoon_cells_and_skips_patches(self):
        tmp, db_path = catalog_with(
            ("Bloodmoon.esm", (exterior(20, 20), land(20, 20), interior(b"Fort Frostmoth"))),
            ("Solstheim Village.esp", (exterior(20, 20), land(20, 20))),
            ("Fort Frostmoth Overhaul.esp", (interior(b"Fort Frostmoth"),)),
            ("Somewhere Else.esp", (exterior(1, 1, b"Ascadian Isles Region"),)),
            ("Patch for Purists.esm", (exterior(20, 20),)),
            ("TOTSP_Patch_for_Purists_5.0.6.esp", (exterior(20, 20), land(20, 20))),
            ("Rem-TOTSP.esp", (land(20, 20),)),
            ("Solstheim Tomb of the Snow Prince.esm", (exterior(20, 20), land(20, 20))),
        )
        self.addCleanup(tmp.cleanup)
        found = master_warnings.likely_incompatible_plugins(STOSP, catalog_db=db_path)
        self.assertEqual(found, ["Fort Frostmoth Overhaul.esp", "Solstheim Village.esp"])
        text = master_warnings.warning_text(STOSP, catalog_db=db_path)
        self.assertIn(WARNING_PHRASE, text)
        self.assertIn("These installed plugins already touch Bloodmoon's Solstheim cells", text)
        self.assertIn("- Fort Frostmoth Overhaul.esp", text)
        self.assertIn("- Solstheim Village.esp", text)
        self.assertNotIn("Patch for Purists.esm", text)
        self.assertNotIn("TOTSP_Patch", text)
        self.assertNotIn("Rem-TOTSP", text)
        self.assertNotIn("Somewhere Else.esp", text)

    def test_missing_or_unreadable_catalog_still_returns_the_static_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "game_catalog.sqlite3"
            self.assertEqual(master_warnings.likely_incompatible_plugins(STOSP, catalog_db=missing), [])
            self.assertEqual(master_warnings.warning_text(STOSP, catalog_db=missing),
                             master_warnings.warning_text(STOSP))
            broken = Path(tmp) / "broken.sqlite3"
            broken.write_bytes(b"not a database")
            self.assertEqual(master_warnings.overlapping_plugins(broken, "Bloodmoon.esm", ["CELL"]), [])
            self.assertIn(WARNING_PHRASE, master_warnings.warning_text(STOSP, catalog_db=broken))

    def test_long_incompatible_lists_are_capped(self):
        config = {
            STOSP_KEY: {
                "text": "Base warning.",
                "incompatible_heading": "Likely incompatible:",
                "likely_incompatible": {"list_limit": 2},
            }
        }
        plugins = [f"Mod{i}.esp" for i in range(1, 6)]
        text = master_warnings.warning_text(STOSP, config=config, plugins=plugins)
        self.assertIn("- Mod1.esp", text)
        self.assertIn("- Mod2.esp", text)
        self.assertNotIn("Mod3.esp", text)
        self.assertIn("- and 3 more", text)


class AttachAndReview(unittest.TestCase):
    def test_prepare_review_attaches_the_snow_prince_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = Path(tmp)
            game = manager / "game"
            (game / "Data Files").mkdir(parents=True)
            prepared = core_addons.prepare_core_master_review(manager, game, {"archives": []})
            row = next(r for r in prepared["rows"] if r["name"].casefold() == STOSP_KEY)
            self.assertIn(WARNING_PHRASE, row["warning"])
            self.assertIn("Warning:", row["warning_summary"])
            other = next(r for r in prepared["rows"] if r["name"].casefold() == "tamriel_data.esm")
            self.assertNotIn("warning", other)

    def test_review_window_shows_the_warning_for_a_selected_master(self):
        import tkinter as tk
        from tkinter import ttk

        rows = [{"name": STOSP, "deps": ["Bloodmoon.esm"], "error": None, "installed": False,
                 "selected": True, "path": "x", "nexus": {}, "page": None}]

        def readiness():
            for row in rows:
                row["reasons"], row["ready"] = [], True

        master_warnings.attach_master_warnings(rows)
        prepared = {"rows": rows, "index_by_name": {STOSP_KEY: 0},
                    "readiness": readiness, "select_dependencies": lambda i: (True, None),
                    "required_by_selection": set}
        seen = {}

        def inspect(window):
            def widgets(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from widgets(child)

            tree = next(w for w in widgets(window) if isinstance(w, ttk.Treeview))
            labels = [w.cget("text") for w in widgets(window) if isinstance(w, ttk.Label)]
            values = tree.item(tree.get_children()[0], "values")
            seen["summary"] = values[2]
            seen["label"] = next((text for text in labels if WARNING_PHRASE in str(text)), "")
            window.destroy()

        try:
            with mock.patch.object(tk.Tk, "mainloop", inspect):
                core_addons.review_core_master_install("m", "g", {}, prepared=prepared)
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.assertIn("Warning:", seen["summary"])
        self.assertIn(WARNING_PHRASE, seen["label"])
        self.assertIn(IMPOSSIBLE_PHRASE, seen["label"])


if __name__ == "__main__":
    unittest.main()
