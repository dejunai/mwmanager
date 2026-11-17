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
            self.assertIn("needs Epic Plants.esp; installing Epic Plants-1-0.zip first", text)
            self.assertIn("needs the major master Tamriel_Data.esm; install it from Major masters", text)
            self.assertNotIn("Other.esp", text)                                  # not chosen: not fetched
            queued = {str((dl / "Epic Plants-1-0.zip").resolve()).casefold()}
            with mock.patch.object(needed_mods, "search_on_nexus"):
                self.assertEqual(mwmanager_gui.plan_needed_archives("OAAB_Data", out, dl, Path(tmp), queued,
                                                                    ["Tamriel_Data.esm"], lines.append), [])


if __name__ == "__main__":
    unittest.main()
