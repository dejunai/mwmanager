"""An option that needs another mod starts its description with "Requires: <plugin>, Nexus <id>": from its plugins'
masters (and the mods its notes name), the Nexus id from the user's pick, the shipped table, the cache, else the
archive index."""
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mock_fomod
import needed_mods


def plugin(path: Path, *masters: str) -> Path:
    subs = b"".join(b"MAST" + struct.pack("<I", len(m) + 1) + m.encode() + b"\0" for m in masters)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"TES3" + struct.pack("<I", len(subs)) + b"\0" * 8 + subs)
    return path


class OptionRequiresTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.manager = self.root / "manager"
        (self.manager / "outputs").mkdir(parents=True)
        (self.manager / "outputs" / "mwmanager_archive_plugins.json").write_text(json.dumps({"version": 1, "archives": {
            "Big Mod-12345-1-0-1700000000.7z": {"stamp": "1|1", "plugins": ["Big Mod.esm"]},
            "Patch Bundle-999-1-0-1700000000.7z": {"stamp": "1|1", "plugins": ["Big Mod.esm", "Other.esp"]},
        }}), encoding="utf-8")
        self.shipped = self.root / "shipped.json"
        self.shipped.write_text(json.dumps({"version": 1, "plugins": {
            "shipped.esm": {"name": "Shipped.esm", "nexus_id": 777, "archive": ""}}}), encoding="utf-8")
        patcher = mock.patch.object(needed_mods, "REQUIRES_FILE", self.shipped)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cache(self):
        return json.loads((self.manager / "outputs" / needed_mods.REQUIRES_CACHE).read_text(encoding="utf-8"))

    def test_sources_come_from_shipped_table_then_index_and_are_cached(self):
        found = needed_mods.requirement_sources(["shipped.esm", "big mod.esm", "Missing.esp"], self.manager)
        self.assertEqual([(f["name"], f["nexus_id"]) for f in found],
                         [("Shipped.esm", 777), ("Big Mod.esm", 12345), ("Missing.esp", None)])
        self.assertEqual(set(self.cache()["plugins"]), {"big mod.esm"})     # unknowns are worked out again later

    def test_the_users_pick_wins_and_finds_that_archive(self):
        downloads = self.root / "downloads"
        downloads.mkdir()
        (downloads / "Patch Bundle-999-1-0-1700000000.7z").write_bytes(b"")
        needed_mods.set_requirement_source("Big Mod.esm", "Patch Bundle-999-1-0-1700000000.7z", 999, self.manager)
        self.assertEqual(needed_mods.requirement_sources(["Big Mod.esm"], self.manager)[0]["nexus_id"], 999)
        found = needed_mods.find_needed(["Big Mod.esm"], downloads, self.manager / "outputs" / "none.json",
                                        self.manager)
        self.assertEqual(found["Big Mod.esm"].name, "Patch Bundle-999-1-0-1700000000.7z")

    def test_requirements_are_masters_and_noted_mods_not_base_game_or_own_plugins(self):
        mod = self.root / "Some Mod-555"
        plugin(mod / "Main.esp", "Morrowind.esm")
        patch = plugin(mod / "02 Patch" / "Patch.esp", "Morrowind.esm", "Main.esp", "Big Mod.esm")
        record = {"key": "02 Patch", "label": "Patch", "path": patch.parent, "mod_dir": mod,
                  "payload_files": {"Patch.esp": patch}, "targets": ["main.esp", "big mod.esm"]}
        with mock.patch.object(mock_fomod, "_maintainer_needs", return_value=["Dark Molag Mar (mod)"]):
            mock_fomod._attach_requires([record], mod, self.manager)
        self.assertEqual([mock_fomod._requires_line(r) for r in record["requires"]],
                         ["Requires: Big Mod.esm, Nexus 12345", "Requires: Dark Molag Mar (mod), Nexus 32101"])

    def test_curated_asset_only_requirements_are_displayed_without_holding(self):
        mod = self.root / "Improved Weapon Mesh Compilation-50886"
        option = mod / "Extras" / "01 Weapon Sheathing Patch"
        option.mkdir(parents=True)
        record = {"key": "01 Weapon Sheathing Patch", "label": "Weapon Sheathing Patch", "path": option,
                  "mod_dir": mod, "targets": [], "missing_masters": []}
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="50886"):
            mock_fomod._attach_requires([record], mod, self.manager)
            mock_fomod._mark_maintainer_needs([record], set())
        self.assertEqual([mock_fomod._requires_line(r) for r in record["requires"]],
                         ["Requires: Weapon Sheathing (mod), Nexus 46069"])
        self.assertNotIn("needs_mods", record)
        self.assertEqual(record["missing_masters"], [])
        needed_mods.set_requirement_source("Weapon Sheathing (mod)", "chosen.7z", 98765, self.manager)
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="50886"):
            mock_fomod._attach_requires([record], mod, self.manager)
        self.assertEqual(record["requires"][0]["nexus_id"], 98765)

    def test_curated_combined_patch_lists_both_mods_without_duplicates(self):
        mod = self.root / "Better Morrowind Armor Fixes-47920"
        option = mod / "04 HiRez Armors - Native Styles V2 + Glass Glowset"
        option.mkdir(parents=True)
        record = {"label": option.name, "key": option.name, "path": option, "mod_dir": mod}
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="47920"):
            mock_fomod._attach_requires([record], mod, self.manager)
        self.assertEqual({(r["name"], r["nexus_id"]) for r in record["requires"]}, {
            ("Glass Glowset (mod)", 42762), ("HiRez Armors - Native Styles V2 (mod)", 41947)})
        self.assertEqual(len(record["requires"]), 2)

    def test_curated_package_patterns_do_not_match_other_same_id_downloads(self):
        mod = self.root / "Improved Pauldron Meshes-47920"
        option = mod / "03 Glass Glowset"
        option.mkdir(parents=True)
        record = {"label": "Glass Glowset", "key": "Glass Glowset", "path": option, "mod_dir": mod}
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="47920"):
            mock_fomod._attach_requires([record], mod, self.manager)
        self.assertEqual(record["requires"], [])

    def test_curated_requirement_matches_native_fomod_choice(self):
        mod = self.root / "Glowbugs 50538"
        fomod = mod / "fomod"
        fomod.mkdir(parents=True)
        (fomod / "ModuleConfig.xml").write_text(
            '<config><installSteps><installStep name="Engine"><optionalFileGroups>'
            '<group name="Engine" type="SelectExactlyOne"><plugins>'
            '<plugin name="MGE XE"><description>Use MWSE.</description></plugin>'
            '<plugin name="OpenMW"><description>Use OpenMW.</description></plugin>'
            '</plugins></group></optionalFileGroups></installStep></installSteps></config>',
            encoding="utf-8")
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="50538"):
            records = mock_fomod._discover_fomod(mod, set())
            mock_fomod._attach_requires(records, mod, self.manager)
        self.assertEqual([r["label"] for r in records], ["MGE XE", "OpenMW"])
        for record in records:
            self.assertEqual([mock_fomod._requires_line(r) for r in record["requires"]],
                             ["Requires: OAAB Data (mod), Nexus 49042"])

    def test_curated_patterns_match_variants_but_not_unrelated_choices(self):
        examples = [
            ("ST Alchemy-53097", "53097", "08 MWSE Dynamic Object Replacer (75%)", 52105),
            ("NPC Outfit Diversity-52091", "52091", "03 MWSE Fashionwind - Bosmer Addon", 50352),
            ("Kwama Egg sacs-43555", "43555", "05-Graphic herbalism bump-mapped", 46599),
            ("ST Alchemy-53097", "53097", "01 Base", None),
        ]
        for package, nexus_id, option, expected in examples:
            with self.subTest(option=option):
                mod = self.root / package
                path = mod / option
                path.mkdir(parents=True, exist_ok=True)
                record = {"label": option, "key": option, "path": path, "mod_dir": mod}
                with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value=nexus_id):
                    mock_fomod._attach_requires([record], mod, self.manager)
                self.assertEqual([r["nexus_id"] for r in record["requires"]],
                                 [expected] if expected else [])

    def test_curated_unknown_id_stays_unknown_until_user_picks_a_source(self):
        mod = self.root / "OAAB Saplings-50334"
        mod.mkdir()
        record = {"label": "Beautiful Cities of Morrowind", "key": "choice", "path": mod, "mod_dir": mod}
        with mock.patch.object(mock_fomod, "_nexus_mod_id", return_value="50334"):
            mock_fomod._attach_requires([record], mod, self.manager)
            self.assertEqual([mock_fomod._requires_line(r) for r in record["requires"]],
                             ["Requires: Beautiful Cities of Morrowind (mod), Nexus ID unknown"])
            needed_mods.set_requirement_source("Beautiful Cities of Morrowind (mod)", "chosen.7z", 98765,
                                               self.manager)
            mock_fomod._attach_requires([record], mod, self.manager)
        self.assertEqual(record["requires"][0]["nexus_id"], 98765)

    # REMOVE BEFORE SHIPPING: developer-only option requirement editor tests.
    def test_option_overrides_persist_without_affecting_other_options_or_packages(self):
        mod = self.root / "Pack-555"
        mod.mkdir()
        first = {"key": "01 Patch", "label": "Patch", "path": mod, "mod_dir": mod}
        second = {"key": "02 Other", "label": "Other", "path": mod, "mod_dir": mod}
        needed_mods.set_requirement_source("Big Mod.esm", "", 12345, self.manager)
        with mock.patch.object(mock_fomod, "_option_requirements", return_value=["Big Mod.esm"]):
            mock_fomod._set_option_requirement(first, mod, self.manager, "Big Mod.esm", 777)
            mock_fomod._set_option_requirement(first, mod, self.manager, "Missing Mod (mod)", 888)
            mock_fomod._attach_requires([first, second], mod, self.manager)
            self.assertEqual([(r["name"], r["nexus_id"]) for r in first["requires"]],
                             [("Big Mod.esm", 777), ("Missing Mod (mod)", 888)])
            self.assertEqual(second["requires"][0]["nexus_id"], 12345)
            other_mod = self.root / "Other Pack-555"
            other_mod.mkdir()
            other = {**first, "path": other_mod, "mod_dir": other_mod}
            mock_fomod._attach_requires([other], other_mod, self.manager)
            self.assertEqual(other["requires"][0]["nexus_id"], 12345)
            self.assertEqual(len(other["requires"]), 1)

    def test_numbered_and_unnumbered_options_accept_saved_requirements(self):
        mod = self.root / "Unpacked"
        for folder in ("01 BAIN patch", "Legacy wrapper/First", "Legacy wrapper/Second"):
            path = mod / "Extras" / folder / "Textures" / "asset.dds"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"texture")
        records = mock_fomod._discover(mod, set())
        self.assertEqual({r["label"] for r in records}, {"BAIN patch", "First", "Second"})
        for record in records:
            mock_fomod._set_option_requirement(record, mod, self.manager, "Example.esm", 12345)
        mock_fomod._attach_requires(records, mod, self.manager)
        for record in records:
            self.assertEqual(record["requires"][0]["nexus_id"], 12345)
            self.assertEqual(set(mock_fomod._payload_files(record)), {str(Path("Textures") / "asset.dds")})

    def test_native_fomod_option_override_survives_rediscovery(self):
        mod = self.root / "Native Package"
        fomod = mod / "fomod"
        fomod.mkdir(parents=True)
        (fomod / "ModuleConfig.xml").write_text(
            '<config><installSteps><installStep name="Step"><optionalFileGroups>'
            '<group name="Group"><plugins><plugin name="Patch"/></plugins></group>'
            '</optionalFileGroups></installStep></installSteps></config>', encoding="utf-8")
        record = mock_fomod._discover_fomod(mod, set())[0]
        mock_fomod._set_option_requirement(record, mod, self.manager, "Missing.esm", 45678)
        rediscovered = mock_fomod._discover_fomod(mod, set())
        mock_fomod._attach_requires(rediscovered, mod, self.manager)
        self.assertEqual(rediscovered[0]["requires"][0]["nexus_id"], 45678)
        self.assertEqual(rediscovered[0]["missing_masters"], [])

    def test_developer_editor_defaults_off_and_reads_its_own_setting(self):
        fake_module = self.root / "mock_fomod.py"
        ini = self.root / "mwmanager.ini"
        with mock.patch.object(mock_fomod, "__file__", str(fake_module)):
            self.assertFalse(mock_fomod._dev_requirement_editor_enabled())
            ini.write_text("[Dev]\nedit_option_requirements = true\n", encoding="utf-8")
            self.assertTrue(mock_fomod._dev_requirement_editor_enabled())

    def test_developer_buttons_add_and_refresh_in_selector(self):
        import tkinter as tk
        from tkinter import ttk

        mod = self.root / "GUI Pack"
        path = mod / "Extras" / "01 Patch"
        path.mkdir(parents=True)
        record = {"key": "01 Patch", "label": "Patch", "path": path, "mod_dir": mod,
                  "number": 1, "required": False, "targets": [], "compatible": [],
                  "missing_masters": [], "docs": ""}
        mock_fomod._attach_requires([record], mod, self.manager)

        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)

        def interact(root):
            def button(text):
                return next(w for w in walk(root) if isinstance(w, ttk.Button) and w.cget("text") == text)

            button("Add requirement…").invoke()
            dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
            entries = [w for w in walk(dialog) if isinstance(w, ttk.Entry)]
            self.assertEqual(len(entries), 3)
            entries[0].insert(0, "New Plugin.esm")
            entries[2].insert(0, "45678")
            button("Use this mod").invoke()
            texts = [w.get("1.0", "end") for w in walk(root) if isinstance(w, tk.Text)]
            self.assertTrue(any("Requires: New Plugin.esm, Nexus 45678" in text for text in texts))
            button("Change…").invoke()
            dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
            entries = [w for w in walk(dialog) if isinstance(w, ttk.Entry)]
            self.assertEqual(len(entries), 2)
            entries[1].insert(0, "56789")
            button("Use this mod").invoke()
            texts = [w.get("1.0", "end") for w in walk(root) if isinstance(w, tk.Text)]
            self.assertTrue(any("Requires: New Plugin.esm, Nexus 56789" in text for text in texts))
            self.assertFalse(any("Nexus 45678" in text for text in texts))
            button("Apply selected options").invoke()

        with mock.patch.object(mock_fomod, "_dev_requirement_editor_enabled", return_value=True), \
                mock.patch.object(tk.Tk, "mainloop", interact), \
                mock.patch.object(mock_fomod, "_nexus_mod_id", return_value=""), \
                mock.patch.object(mock_fomod, "_nexus_page_url", return_value=""):
            try:
                mock_fomod._show_selector("GUI Pack", [record], set(), set())
            except tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
        reopened = {**record, "requires": []}
        mock_fomod._attach_requires([reopened], mod, self.manager)
        self.assertEqual(reopened["requires"][0]["nexus_id"], 56789)

    def test_developer_buttons_are_hidden_when_disabled(self):
        import tkinter as tk
        from tkinter import ttk

        mod = self.root / "GUI Pack"
        mod.mkdir()
        record = {"key": "patch", "label": "Patch", "path": mod, "mod_dir": mod, "number": 1,
                  "required": False, "targets": [], "compatible": [], "missing_masters": [], "docs": ""}
        mock_fomod._set_option_requirement(record, mod, self.manager, "Dependency.esm", 12345)
        mock_fomod._attach_requires([record], mod, self.manager)

        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)

        def inspect(root):
            buttons = [w for w in walk(root) if isinstance(w, ttk.Button)]
            labels = {w.cget("text") for w in buttons}
            self.assertNotIn("Change…", labels)
            self.assertNotIn("Add requirement…", labels)
            self.assertNotIn("Insights…", labels)
            self.assertTrue(any("Requires: Dependency.esm, Nexus 12345" in w.get("1.0", "end")
                                for w in walk(root) if isinstance(w, tk.Text)))
            next(w for w in buttons if w.cget("text") == "Apply selected options").invoke()

        with mock.patch.object(mock_fomod, "_dev_requirement_editor_enabled", return_value=False), \
                mock.patch.object(mock_fomod, "_dev_insight_editor_enabled", return_value=False), \
                mock.patch.object(tk.Tk, "mainloop", inspect), \
                mock.patch.object(mock_fomod, "_nexus_mod_id", return_value=""), \
                mock.patch.object(mock_fomod, "_nexus_page_url", return_value=""):
            try:
                mock_fomod._show_selector("GUI Pack", [record], set(), set())
            except tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")


if __name__ == "__main__":
    unittest.main()
