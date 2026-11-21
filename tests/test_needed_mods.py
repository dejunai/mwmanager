"""Batch step 3: the download archive that holds a needed plugin, from a cached index of archive contents."""
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import needed_mods  # noqa: E402


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name in members:
            z.writestr(name, b"TES3")


class NeededModsTests(unittest.TestCase):
    def test_the_needed_plugin_is_found_in_its_own_mod_rather_than_a_patch_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp) / "downloads"
            dl.mkdir()
            make_zip(dl / "Epic Plants-12345-1-0.zip", ["Data Files/Epic Plants.esp", "Textures/a.dds"])
            make_zip(dl / "Patch Bundle-999-2-0.zip", ["Patches/Epic Plants.esp", "Other.esp"])
            make_zip(dl / "Unrelated-1-0.zip", ["Meshes/x.nif"])
            index = Path(tmp) / "index.json"
            found = needed_mods.find_needed(["epic plants.esp", "Glow.esp"], dl, index, Path(tmp))
            self.assertEqual(found, {"epic plants.esp": dl / "Epic Plants-12345-1-0.zip", "Glow.esp": None})

    def test_unchanged_archives_are_not_opened_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp) / "downloads"
            dl.mkdir()
            make_zip(dl / "A-1.zip", ["A.esp"])
            index = Path(tmp) / "index.json"
            needed_mods.archive_index(dl, index, None)
            with mock.patch.object(needed_mods.core_addons, "_members", side_effect=AssertionError("opened")):
                self.assertEqual(needed_mods.archive_index(dl, index, None), {"A-1.zip": ["A.esp"]})
            make_zip(dl / "B-1.zip", ["B.esm"])                                # a new archive is listed
            self.assertEqual(needed_mods.archive_index(dl, index, None)["B-1.zip"], ["B.esm"])
            self.assertEqual(json.loads(index.read_text())["version"], needed_mods.INDEX_VERSION)


class ModNameMatchTests(unittest.TestCase):
    """OAAB_Data's asset-only options name mods; the real archives (these are real names) rarely match exactly."""

    def test_option_names_find_misleadingly_named_archives(self):
        cases = {
            "glass glowset (mod)": "Definative Glass Glowset-42762-2-0-1758844090",
            "sm bitter coast tree replacer (mod)": "SM_Bitter Coast Tree-49883-1-0-1622322145",
            "improved kwama eggs and egg sacs (mod)": "Kwama Egg sacs and Eggs Replacer-43555-1-1",
            "better telvanni crystals (mod)": "Better Telvanni Crystal V2 4k-59196-1-0-1780326665",
            "glow in the dahrk (mod)": "Glow in the Dahrk-45886-3-3-0-1765851739",
        }
        with mock.patch.object(needed_mods, "vortex_mod_titles", return_value={}):
            for need, name in cases.items():
                self.assertTrue(needed_mods.names_mod(need, name), need)

    def test_vortex_titles_name_what_the_archive_name_hides(self):
        titles = {"all-in-one-46180-16-03-26-1773677704": "Epic Plants"}      # as Vortex records it
        with mock.patch.object(needed_mods, "vortex_mod_titles", return_value=titles):
            self.assertTrue(needed_mods.names_mod("epic plants (mod)", "All-in-one-46180-16-03-26-1773677704"))
        with mock.patch.object(needed_mods, "vortex_mod_titles", return_value={}):          # no Vortex
            self.assertFalse(needed_mods.names_mod("epic plants (mod)", "All-in-one-46180-16-03-26-1773677704"))

    def test_titles_are_read_from_the_vortex_hourly_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "Vortex" / "temp" / "state_backups_full" / "hourly.json"
            backup.parent.mkdir(parents=True)
            backup.write_text(json.dumps({"persistent": {"mods": {"morrowind": {
                "All-in-one-46180-1": {"attributes": {"modName": "Epic Plants", "fileName": "All-in-one-46180-1.rar"}},
                "No title": {"attributes": {}}}}}}), encoding="utf-8")
            needed_mods._VORTEX_TITLES.clear()
            with mock.patch.dict(os.environ, {"APPDATA": tmp}):
                titles = needed_mods.vortex_mod_titles()
            needed_mods._VORTEX_TITLES.clear()
        self.assertEqual(titles, {"all-in-one-46180-1": "Epic Plants"})

    def test_patches_and_other_mods_do_not_stand_in(self):
        with mock.patch.object(needed_mods, "vortex_mod_titles", return_value={}):
            self.assertFalse(needed_mods.names_mod("glass glowset (mod)", "Glass Glowset - Weapon Sheathing Patch-48602"))
            self.assertFalse(needed_mods.names_mod("rr - better crystals (mod)", "Better Telvanni Crystal V2 4k-59196"))
            self.assertFalse(needed_mods.names_mod("sm bitter coast tree replacer (mod)", "Glowing Bitter Coast-47946"))

    def test_the_closest_archive_is_chosen_for_a_mod_need(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp) / "downloads"
            dl.mkdir()
            for name in ("MET Glass Glowset Textures-50170-1-0.7z", "Definative Glass Glowset-42762-2-0.rar"):
                (dl / name).write_bytes(b"x")
            with mock.patch.object(needed_mods, "archive_index", return_value={}):
                found = needed_mods.find_needed(["glass glowset (mod)"], dl, Path(tmp) / "i.json", Path(tmp))
        self.assertEqual(found["glass glowset (mod)"].name, "Definative Glass Glowset-42762-2-0.rar")

    def test_animated_containers_prefers_the_full_mod_over_mesh_addon(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp) / "downloads"
            dl.mkdir()
            proper = dl / "MW Containers Animated-42238-2-10-1706266667.7z"
            optional = dl / "Some Improved Animated Containers-44321-0-2-1707072703.zip"
            optional.write_bytes(b"optional")
            proper.write_bytes(b"full")
            with mock.patch.object(needed_mods, "archive_index", return_value={}):
                found = needed_mods.find_needed(
                    ["animated containers (mod)"], dl, Path(tmp) / "i.json", Path(tmp))
        self.assertEqual(found["animated containers (mod)"], proper)


class NexusFallbackTests(unittest.TestCase):
    def test_a_missing_mod_opens_a_nexus_search_that_downloads_to_the_archive_folder(self):
        self.assertEqual(needed_mods.nexus_search_url("Knife_Ears for Bretons.esp"),
                         "https://www.nexusmods.com/games/morrowind/mods?keyword=Knife%20Ears%20for%20Bretons")
        with mock.patch("mwnexus_browser.open_page") as opened:
            needed_mods.search_on_nexus("Epic Plants.esp", Path("K:/dl"))
        self.assertEqual(opened.call_args.kwargs["download_dir"], Path("K:/dl"))
        self.assertTrue(opened.call_args.args[0].startswith("https://www.nexusmods.com/"))


class BatchPlanTests(unittest.TestCase):
    def test_held_needs_become_batch_items_ahead_of_the_package(self):
        import mwmanager_gui
        import mock_fomod
        with tempfile.TemporaryDirectory() as tmp:
            out, dl = Path(tmp) / "outputs", Path(tmp) / "downloads"
            out.mkdir()
            dl.mkdir()
            make_zip(dl / "Epic Plants-1-0.zip", ["Epic Plants.esp"])
            (out / mock_fomod.PENDING_FILE).write_text(json.dumps({"OAAB_Data": {
                "01 Epic": {"label": "Epic Plants", "needs": ["Epic Plants.esp"], "status": "waiting"},
                "02 Ears": {"label": "Knife Ears", "needs": ["Knife Ears.esp"], "status": "waiting"},
                "03 TD": {"label": "TD Patch", "needs": ["Tamriel_Data.esm"], "status": "waiting"},
                "04 Skip": {"label": "Unchosen", "needs": ["Other.esp"], "status": "not chosen"}}}), encoding="utf-8")
            lines = []
            with mock.patch.object(needed_mods, "search_on_nexus") as searched:
                items = mwmanager_gui.plan_needed_archives("OAAB_Data", out, dl, Path(tmp), set(),
                                                           ["Tamriel_Data.esm"], lines.append)
            self.assertEqual([(Path(i["archive"]).name, i["plugins"], i["needed_by"]) for i in items],
                             [("Epic Plants-1-0.zip", ["Epic Plants.esp"], "OAAB_Data")])
            self.assertEqual([c.args[0] for c in searched.call_args_list], ["Knife Ears.esp"])
            text = " ".join(lines)
            self.assertIn("needs Epic Plants.esp; staging Epic Plants-1-0.zip first", text)
            self.assertIn("needs the major master Tamriel_Data.esm; stage it from Major masters", text)
            self.assertNotIn("Other.esp", text)                                  # not chosen: not fetched
            queued = {str((dl / "Epic Plants-1-0.zip").resolve()).casefold()}
            with mock.patch.object(needed_mods, "search_on_nexus"):
                self.assertEqual(mwmanager_gui.plan_needed_archives("OAAB_Data", out, dl, Path(tmp), queued,
                                                                    ["Tamriel_Data.esm"], lines.append), [])


class UnreadableArchiveTests(unittest.TestCase):
    def test_an_archive_that_could_not_be_listed_is_tried_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp) / "downloads"
            dl.mkdir()
            (dl / "Epic Plants-1.7z").write_bytes(b"7z")
            index = Path(tmp) / "index.json"
            with mock.patch.object(needed_mods.core_addons, "_members", return_value=([], "7-Zip is required")):
                self.assertEqual(needed_mods.archive_index(dl, index, None), {})
            with mock.patch.object(needed_mods.core_addons, "_members", return_value=(["Data/Epic Plants.esp"], None)):
                self.assertEqual(needed_mods.archive_index(dl, index, "7z.exe"), {"Epic Plants-1.7z": ["Epic Plants.esp"]})


if __name__ == "__main__":
    unittest.main()
