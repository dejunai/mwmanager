"""MWManager's own option notes (mock_fomod_notes.json) are found by Nexus id or package name and option pattern."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod


class MaintainerNotesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = Path(tmp.name) / "Some Pack 123 1.0"
        self.plugin = self.mod / "Extras" / "Some Pack" / "02 Resource ESPs" / "Res.esp"
        self.notes = Path(tmp.name) / "notes.json"
        self.notes.write_text(json.dumps({"mods": [
            {"nexus_id": 123, "options": {"*Res.esp": "Modder's resource.", "*": "Whole package note."}},
            {"packages": ["Other *"], "options": {"*": "Not this one."}}]}), encoding="utf-8")
        patches = [mock.patch.object(mock_fomod, "MAINTAINER_NOTES", self.notes),
                   mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="123")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def item(self, path, label):
        return {"path": path, "label": label, "key": "nested-plugin:" + label.casefold(), "mod_dir": self.mod}

    def test_option_and_package_notes_are_found(self):
        self.assertEqual(mock_fomod._maintainer_notes(self.item(self.plugin, "02 Resource ESPs\\Res.esp")),
                         "Modder's resource.\n\nWhole package note.")
        other = self.item(self.mod / "Extras" / "Some Pack" / "01 HD", "01 HD")
        self.assertEqual(mock_fomod._maintainer_notes(other), "Whole package note.")

    def test_package_name_match_and_no_match(self):
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value=""):
            self.assertEqual(mock_fomod._maintainer_notes(self.item(self.plugin, "Res.esp")), "")
            renamed = Path(str(self.mod).replace("Some Pack", "Other Pack"))
            self.assertEqual(mock_fomod._maintainer_notes({"path": renamed / "x", "label": "x", "mod_dir": renamed}),
                             "Not this one.")

    # REMOVE BEFORE SHIPPING: developer insight authoring tests; keep reader coverage.
    def test_authored_insight_is_exact_editable_and_preserves_curated_notes(self):
        item = self.item(self.plugin, "02 Resource ESPs\\Res.esp")
        original = self.notes.read_text(encoding="utf-8")
        mock_fomod._save_insight(item, "First line.\nSecond line.")
        note = mock_fomod._maintainer_notes(item)
        self.assertIn("Modder's resource.", note)
        self.assertIn("Whole package note.", note)
        self.assertIn("First line.\nSecond line.", note)
        self.assertEqual(json.loads(self.notes.read_text(encoding="utf-8"))["mods"],
                         json.loads(original)["mods"])
        sibling = {**item, "key": "another-key"}
        self.assertNotIn("First line.", mock_fomod._maintainer_notes(sibling))
        other_pack = {**item, "mod_dir": self.mod.with_name("Some Pack 123 1.0 [2]")}
        self.assertNotIn("First line.", mock_fomod._maintainer_notes(other_pack))
        mock_fomod._save_insight(item, "Updated insight.")
        self.assertIn("Updated insight.", mock_fomod._maintainer_notes(item))
        self.assertNotIn("First line.", mock_fomod._maintainer_notes(item))
        self.assertEqual(len(json.loads(self.notes.read_text(encoding="utf-8"))["insights"]), 1)
        mock_fomod._save_insight(item, "")
        self.assertNotIn("Updated insight.", mock_fomod._maintainer_notes(item))

    def test_authored_insight_does_not_overwrite_invalid_notes(self):
        self.notes.write_text("{ broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            mock_fomod._save_insight(self.item(self.plugin, "Patch"), "Insight")
        self.assertEqual(self.notes.read_text(encoding="utf-8"), "{ broken")

    def test_authored_insight_updates_compact_json_without_rewriting_other_fields(self):
        self.notes.write_text('{"mods":[],"other":{"insights":"keep"},"insights":[]}', encoding="utf-8")
        item = self.item(self.plugin, "Patch")
        mock_fomod._save_insight(item, "New insight.")
        data = json.loads(self.notes.read_text(encoding="utf-8"))
        self.assertEqual(data["other"], {"insights": "keep"})
        self.assertEqual(data["insights"][0]["note"], "New insight.")
        mock_fomod._save_insight(item, "Edited insight.")
        self.assertEqual(mock_fomod._maintainer_notes(item), "Edited insight.")

    def test_insight_button_saves_on_window_close_and_refreshes(self):
        import tkinter as tk
        from tkinter import ttk

        self.mod.mkdir()
        item = {"key": "fomod:0:0:0", "label": "Patch", "path": self.mod, "mod_dir": self.mod,
                "number": 1, "required": False, "targets": [], "compatible": [],
                "missing_masters": [], "docs": "", "fomod_description": "Original description."}

        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)

        def interact(root):
            def button(text):
                return next(w for w in walk(root) if isinstance(w, ttk.Button) and w.cget("text") == text)

            button("Insights…").invoke()
            dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
            editor = next(w for w in walk(dialog) if isinstance(w, tk.Text))
            editor.insert("1.0", "An added insight.")
            dialog.tk.call(dialog.protocol("WM_DELETE_WINDOW"))
            details = [w.get("1.0", "end") for w in walk(root) if isinstance(w, tk.Text)]
            self.assertTrue(any("Original description." in text and "An added insight." in text for text in details))
            button("Insights…").invoke()
            dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
            editor = next(w for w in walk(dialog) if isinstance(w, tk.Text))
            self.assertEqual(editor.get("1.0", "end-1c"), "An added insight.")
            editor.delete("1.0", "end")
            editor.insert("1.0", "Edited insight.")
            button("Save and close").invoke()
            details = [w.get("1.0", "end") for w in walk(root) if isinstance(w, tk.Text)]
            self.assertTrue(any("Edited insight." in text and "An added insight." not in text for text in details))
            button("Apply selected options").invoke()

        with mock.patch.object(mock_fomod, "_dev_insight_editor_enabled", return_value=True), \
                mock.patch.object(mock_fomod, "_dev_requirement_editor_enabled", return_value=False), \
                mock.patch.object(tk.Tk, "mainloop", interact), \
                mock.patch.object(mock_fomod, "_nexus_page_url", return_value=""):
            try:
                mock_fomod._show_selector("Some Pack", [item], set(), set())
            except tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
        self.assertIn("Edited insight.", mock_fomod._maintainer_notes(item))

    def test_the_shipped_notes_file_is_valid(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "mock_fomod_notes.json").read_text(encoding="utf-8"))
        for mod in data["mods"]:
            self.assertTrue(mod.get("nexus_id") or mod.get("packages"))
            for value in mod["options"].values():          # a note, or {"note": ..., "needs": [plugin, ...]}
                if isinstance(value, dict):
                    self.assertTrue(isinstance(value.get("note"), str) and value["note"])
                    self.assertTrue(isinstance(value.get("needs", []), list))
                    for requirement in value.get("requires", []):
                        self.assertTrue(isinstance(requirement.get("name"), str) and requirement["name"])
                        nexus_id = requirement.get("nexus_id")
                        self.assertTrue(nexus_id is None or isinstance(nexus_id, int) and nexus_id > 0)
                else:
                    self.assertTrue(isinstance(value, str) and value)


class ShippedNotesTests(unittest.TestCase):
    def test_epic_plants_2d_moss_gets_its_note(self):
        """Neither the archive nor the Nexus page says what the 2D Moss option is for."""
        mod = Path("staging/plugins/All-in-one-46180-16-03-26-1773677704 [3]")
        item = {"path": mod / "Extras" / "01 BC - 2D Moss", "label": "BC \u2013 2D Moss",
                "key": "fomod:bc-2d-moss", "mod_dir": mod}
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="46180"):
            note = mock_fomod._maintainer_notes(item)
        self.assertIn("lower-end systems", note)
        self.assertIn("overwrites the larger 3D moss meshes", note)


    def test_glow_in_the_dahrk_windoors_and_dark_molag_mar_notes(self):
        mod = Path("staging/plugins/Glow in the Dahrk-45886-3-3-0-1765851739 [5]")
        extras = mod / "Extras" / mod.name
        windoors = {"path": extras / "06 Windoors Patch", "label": "Windoors Patch", "key": "fomod:windoors",
                    "mod_dir": mod}
        molag = {"path": extras / "05 Dark Molag Mar Patch", "label": "Dark Molag Mar", "key": "fomod:dmm",
                 "mod_dir": mod}
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="45886"):
            self.assertIn("very hard to find mod from a Russian author", mock_fomod._maintainer_notes(windoors))
            self.assertIn("separate mod Dark Molag Mar", mock_fomod._maintainer_notes(molag))


    def test_wares_nod_patch_names_npc_outfit_diversity(self):
        """Wares Ultimate's NOD Patch needs NOD - Core.esm, from a mod its package never names."""
        mod = Path("staging/major_masters/Wares Ultimate-52013-2-1-3-1775985220")
        item = {"path": mod / "Extras" / "wares-base" / "10 NOD Patch", "label": "NOD Patch",
                "key": "Extras\\wares-base\\10 NOD Patch", "mod_dir": mod}
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="52013"):
            self.assertIn("NPC Outfit Diversity (NOD - Core.esm, Nexus 52091)", mock_fomod._maintainer_notes(item))

class NeededModTests(unittest.TestCase):
    """A note can name a plugin an option needs that its files don't say (a meshes-only patch for another mod):
    chosen without it, the option waits like one with a missing master."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = Path(tmp.name) / "Glow 45886"
        notes = Path(tmp.name) / "notes.json"
        notes.write_text(json.dumps({"mods": [{"nexus_id": 45886, "options": {
            "Patch X": {"note": "For mod X.", "needs": ["X.esp"]}, "Plain": "Just a note."}}]}), encoding="utf-8")
        for patch in (mock.patch.object(mock_fomod, "MAINTAINER_NOTES", notes),
                      mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="45886")):
            patch.start()
            self.addCleanup(patch.stop)

    def records(self):
        return [{"key": "x", "label": "Patch X", "path": self.mod / "05 Patch X", "mod_dir": self.mod,
                 "targets": [], "missing_masters": []},
                {"key": "p", "label": "Plain", "path": self.mod / "01 Plain", "mod_dir": self.mod,
                 "targets": [], "missing_masters": []}]

    def test_a_needed_mod_that_is_missing_holds_the_option(self):
        records = self.records()
        mock_fomod._mark_maintainer_needs(records, {"morrowind.esm"})
        self.assertEqual((records[0]["needs_mods"], records[0]["missing_masters"]), (["X.esp"], ["X.esp"]))
        self.assertEqual(mock_fomod.held_options(records, {"x", "p"}, {"morrowind.esm"}), {"x"})
        self.assertEqual(mock_fomod._maintainer_notes(records[0]), "For mod X.")
        self.assertEqual(mock_fomod._maintainer_notes(records[1]), "Just a note.")

    def test_once_installed_nothing_waits(self):
        records = self.records()
        mock_fomod._mark_maintainer_needs(records, {"x.esp"})
        self.assertNotIn("needs_mods", records[0])
        self.assertEqual(records[0]["missing_masters"], [])
        self.assertEqual(records[0]["targets"], ["x.esp"])          # its arrival re-offers the option


if __name__ == "__main__":
    unittest.main()
