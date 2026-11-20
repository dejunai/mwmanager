"""Companion packs (HD and voice packs offered in a master's option window) and the Nexus "mods requiring this
file" count that suggests new masters. Vortex state and Nexus are mocked; temporary folders only."""
import io
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import core_addons  # noqa: E402
import mock_fomod  # noqa: E402
import plugin_install  # noqa: E402

MASTER = "OAAB_Data 49042 2.7.5 2026-11-05T04-50Z sZJEZCNJ.zip"
HD = "OAAB_Data (HD) 49042 2.7.0 2026-09-25T18-23Z AbOYby57.zip"
VOICE = "OAAB Data 60274 4 2026-11-02T08-23Z 8MahMSPa.zip"
OTHER_VOICE = "Morrowind Tribunal Bloodmoon 60274 10 2026-10-30T20-45Z uFetFX3x.zip"


def tes3(masters=()):
    def sub(n, d):
        return n.encode() + struct.pack("<I", len(d)) + d
    body = sub("HEDR", struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", 0))
    for m in masters:
        body += sub("MAST", m.encode() + b"\0") + sub("DATA", struct.pack("<Q", 1))
    return b"TES3" + struct.pack("<III", len(body), 0, 0) + body


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def vortex_entry(local, mod_id, category, name, version, category_id=17):
    return {"localPath": local, "game": ["morrowind"], "modInfo": {"nexus": {
        "ids": {"modId": mod_id}, "modInfo": {"category_id": category_id},
        "fileInfo": {"category_name": category, "name": name, "version": version}}}}


class VortexAndNames(unittest.TestCase):
    def test_vortex_downloads_reads_ids_and_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "hourly.json"
            state.write_text(json.dumps({"persistent": {"downloads": {"files": {
                "a": vortex_entry(VOICE, 60274, "Audio", "OAAB Data", "4", 25),
                "b": vortex_entry("Old Voice-1234-1-0.zip", 1234, "MAIN", "Old", "1", 25),   # file category
                "c": vortex_entry("skyrim.zip", 5, "Audio", "x", "1") | {"game": ["skyrimse"]}}}}}),
                encoding="utf-8")
            found = core_addons.vortex_downloads(state)
        self.assertEqual(found[VOICE.casefold()], {"mod_id": 60274, "category": "Audio", "name": "OAAB Data",
                                                   "version": "4"})
        self.assertEqual(found["old voice-1234-1-0.zip"]["category"], "Audio")    # by the category id
        self.assertNotIn("skyrim.zip", found)
        self.assertEqual(core_addons.vortex_downloads(Path(tmp) / "missing.json"), {})

    def test_a_master_the_user_added_takes_its_mod_id_from_its_archive(self):
        archive = "Solstheim Tomb of the Snow Prince-46810-2-3-0-1731263823.7z"
        self.assertEqual(core_addons.master_mod_id("Solstheim Tomb of The Snow Prince.esm", archive), 46810)
        self.assertIsNone(core_addons.master_mod_id("Unlisted Master.esm"))
        renamed = {"snow prince.7z": {"mod_id": 46810}}                     # no ID in the name: Vortex knows it
        self.assertEqual(core_addons.master_mod_id("Snow.esm", "K:/dl/Snow Prince.7z", renamed), 46810)
        self.assertIsNone(core_addons.master_mod_id("Snow.esm", "K:/dl/Other.7z", renamed))

    def test_mod_id_from_newer_nexus_archive_names(self):
        self.assertEqual(plugin_install._nexus_ids_from_archive_name(Path(HD)), ("49042", None))
        self.assertEqual(plugin_install._nexus_ids_from_archive_name(Path("Mod-12345-1-0-1700000000.zip")),
                         ("12345", "1700000000"))
        self.assertEqual(plugin_install._nexus_ids_from_archive_name(Path("plain.zip")), (None, None))


class Companions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        make_zip(self.downloads / MASTER, {"OAAB_Data.esm": tes3(), "textures/a.dds": b"new"})
        make_zip(self.downloads / HD, {"OAAB_Data (HD)/Textures/a.dds": b"hd"})
        make_zip(self.downloads / VOICE, {"Sound/Vo/oaab/hello.mp3": b"v"})
        make_zip(self.downloads / OTHER_VOICE, {"Sound/Vo/x.mp3": b"v"})
        make_zip(self.downloads / "OAAB_Data 49042 2.6.0 2026-01-01T00-00Z old.zip", {"OAAB_Data.esm": tes3()})
        self.plan = {"archives": [{"archive": str(self.downloads / MASTER), "plugins": ["OAAB_Data.esm"],
                                   "core_masters": ["oaab_data.esm"]}]}
        self.vortex = {VOICE.casefold(): {"mod_id": 60274, "category": "Audio", "name": "OAAB Data", "version": "4"},
                       OTHER_VOICE.casefold(): {"mod_id": 60274, "category": "Audio",
                                                "name": "Morrowind Tribunal Bloodmoon", "version": "10"}}

    def tearDown(self):
        self._tmp.cleanup()

    def companions(self, vortex):
        with mock.patch("needed_mods.vortex_mod_titles", return_value={}):
            core_addons.find_companions(self.plan, self.downloads, self.root, vortex=vortex)
        return {c["kind"]: c for c in self.plan["archives"][0].get("companions", [])}

    def test_hd_pack_and_voice_pack_are_found_and_an_older_hd_pack_warns(self):
        found = self.companions(self.vortex)
        self.assertEqual(set(found), {"hd", "voice"})       # the older full archive has an ESM: not a companion
        self.assertEqual(found["hd"]["label"], "HD pack available")
        self.assertIn("2.7.0 is older than oaab_data.esm 2.7.5", found["hd"]["warning"])
        self.assertEqual(found["voice"]["label"], "Voice pack available")
        self.assertNotIn("warning", found["voice"])         # a voice pack's own version is not comparable

    def test_another_mods_patch_naming_the_master_last_is_no_voice_pack(self):
        self.plan["archives"][0]["core_masters"] = ["Patch for Purists.esm"]
        self.vortex[VOICE.casefold()]["name"] = "Django's Dialogue Patch for Purists"
        self.vortex[OTHER_VOICE.casefold()]["name"] = "Patch for Purists - Voices"
        found = self.companions(self.vortex)
        self.assertEqual(found["voice"]["name"], "Patch for Purists - Voices")
        self.assertEqual([c["name"] for c in self.plan["archives"][0]["companions"] if c["kind"] == "voice"],
                         ["Patch for Purists - Voices"])

    def test_voice_pack_row_names_only_the_npcs_it_voices(self):
        self.vortex[VOICE.casefold()]["name"] = "OAAB Data - Dark Elves Female Part 2"
        self.assertEqual(self.companions(self.vortex)["voice"]["short"], "Dark Elves Female Part 2")
        self.assertEqual(core_addons._after_name("Tamriel Rebuilt - Argonians", "Tamriel Rebuilt"), "Argonians")

    def test_without_vortex_the_hd_pack_is_found_by_the_id_in_its_name(self):
        self.assertEqual(set(self.companions({})), {"hd"})

    def test_several_voice_packs_get_one_select_all_box(self):
        import tkinter as tk
        from tkinter import ttk
        package = self.root / "pkg"
        package.mkdir()
        records = [{"key": f"companion:v{i}.7z", "path": None, "number": 950 + i, "label": name,
                    "required": False, "core_bundle": False, "has_plugin_payload": False,
                    "has_master_payload": False, "informational_utility": False, "targets": [],
                    "compatible": [], "missing_masters": [], "docs": "", "mod_dir": package, "badges": [],
                    "outputs": set(), "group_key": "companion:voice", "group_label": "Voice packs",
                    "selection_type": "SelectAny"}
                   for i, name in enumerate(("Argonians", "Khajiit", "Imperials"))]

        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)

        def click_select_all(window):
            boxes = [w for w in walk(window) if isinstance(w, ttk.Checkbutton) and w.cget("text") == "Select all"]
            self.assertEqual(len(boxes), 1)
            boxes[0].invoke()
            next(w for w in walk(window) if isinstance(w, ttk.Button)
                 and w.cget("text") == "Apply selected options").invoke()

        # a saved empty answer: nothing starts ticked, so without the box every pack is a separate click
        with mock.patch.object(tk.Tk, "mainloop", click_select_all), \
                mock.patch("mwmanager_dialogs._prompt", return_value=True):
            try:
                chosen = mock_fomod._show_selector("TR", records, set(), set(), {"companion:voice"})
            except tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
        self.assertEqual(chosen, {r["key"] for r in records})

    def test_newer_voice_pack_wins_shared_lines_and_older_fills_the_rest(self):
        package = self.root / "staging" / "major_masters" / "Cyr_Main"
        package.mkdir(parents=True)
        downloads = self.root / "dl"
        downloads.mkdir()
        names = {"old": "Project Cyrodiil Fully Voiced Dialogue (MWSE)-57146-1-0-1752788019.zip",
                 "p1": "Project Cyrodiil Part 1 60274 2 2026-10-27T23-24Z x2p02ARr.7z",
                 "p2": "Project Cyrodiil Part 2 60274 2 2026-10-27T23-19Z 9AnoATdY.7z",
                 "music": "Project Cyrodiil Music-55779-1-1-1737192092.7z"}
        companions = [{"archive": str(downloads / names[k]), "kind": "voice", "label": "Voice pack available",
                       "name": f"Project Cyrodiil {short}", "short": short, "master": "Cyr_Main.ESM",
                       "size_bytes": 1}
                      for k, short in (("old", "Fully Voiced Dialogue (MWSE)"), ("music", "Music"),
                                       ("p1", "Part 1"), ("p2", "Part 2"))]
        (package / ".mwmanager-stage.json").write_text(json.dumps({"companions": companions}), encoding="utf-8")
        records = mock_fomod._companion_records(package)
        voice = [r for r in records if r.get("group_key") == "companion:voice"]
        self.assertEqual([r["label"] for r in voice],
                         ["Older · Fully Voiced Dialogue (MWSE)", "Newer · Part 1", "Newer · Part 2"])
        self.assertIn("newer wins", voice[0]["group_label"])
        self.assertIn("only adds the lines", voice[0]["docs"])
        music = next(r for r in records if "Music" in r["label"])
        self.assertEqual((music["group_key"], music["group_label"]), ("companion:music", "Music packs"))
        self.assertEqual([r["number"] for r in records], sorted(r["number"] for r in records))

        def pack(name, lines):
            files = {}
            for rel, text in lines.items():
                path = self.root / "src" / name / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                files[rel] = path
            return files

        payloads = {"Older · Fully Voiced Dialogue (MWSE)": pack("old", {"Sound/Vo/AIV/a.mp3": "old a",
                                                                           "Sound/Vo/AIV/b.mp3": "old b"}),
                    "Newer · Part 1": pack("p1", {"Sound/Vo/AIV/a.mp3": "new a"}),
                    "Newer · Part 2": pack("p2", {"Sound/Vo/AIV/c.mp3": "new c"})}
        for record in voice:
            record["payload_files"] = payloads[record["label"]]
        mock_fomod._activate(package, "Cyr_Main.ESM", records, {r["key"] for r in voice}, {}, self.root / "bk")
        read = lambda rel: (package / rel).read_text(encoding="utf-8")
        self.assertEqual(read("Sound/Vo/AIV/a.mp3"), "new a")                  # both voice it: the newer wins
        self.assertEqual(read("Sound/Vo/AIV/b.mp3"), "old b")                  # only the older has it: kept
        self.assertEqual(read("Sound/Vo/AIV/c.mp3"), "new c")

    def test_upload_times_compare_across_name_formats(self):
        epoch = core_addons.archive_upload_time(Path("X-57146-1-0-1752788019.zip"))
        iso = core_addons.archive_upload_time(Path("X 60274 2 2026-10-27T23-24Z x2p02ARr.7z"))
        self.assertEqual(epoch, 1752788019.0)
        self.assertGreater(iso, epoch)

    def test_exclusive_and_required_groups_get_no_select_all_box(self):
        import tkinter as tk
        from tkinter import ttk
        package = self.root / "pkg"
        package.mkdir()

        def group(kind, required=False):
            return [{"key": f"{kind}:{i}", "path": None, "number": 900, "label": f"{kind} {i}",
                     "required": required, "core_bundle": False, "has_plugin_payload": False,
                     "has_master_payload": False, "informational_utility": False, "targets": [],
                     "compatible": [], "missing_masters": [], "docs": "", "mod_dir": package, "badges": [],
                     "outputs": set(), "group_key": kind, "group_label": kind, "selection_type": kind}
                    for i in range(4)]

        records = group("SelectExactlyOne") + group("SelectAtMostOne") + group("SelectAll") + \
            [dict(r, group_key="req", selection_type="SelectAny") for r in group("SelectAny", required=True)]
        found = {}

        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)

        def look(window):
            found["radios"] = sum(isinstance(w, ttk.Radiobutton) for w in walk(window))
            found["boxes"] = [w for w in walk(window)
                              if isinstance(w, ttk.Checkbutton) and w.cget("text") == "Select all"]
            window.destroy()

        with mock.patch.object(tk.Tk, "mainloop", look), mock.patch("mwmanager_dialogs._prompt", return_value=True):
            try:
                mock_fomod._show_selector("X", records, set(), set())
            except tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
        self.assertEqual(found["radios"], 8)                  # both one-choice groups rendered as radios
        self.assertEqual(found["boxes"], [])

    def test_option_window_offers_companions_and_extracts_only_a_chosen_one(self):
        staging = self.root / "staging" / "major_masters"
        package = staging / "OAAB_Data"
        package.mkdir(parents=True)
        (package / "textures").mkdir()
        (package / "textures" / "a.dds").write_bytes(b"new")
        found = self.companions(self.vortex)
        (package / ".mwmanager-stage.json").write_text(json.dumps({"companions": list(found.values())}),
                                                       encoding="utf-8")
        records = mock_fomod._companion_records(package)
        hd = next(r for r in records if r["group_key"] == "companion:hd")
        voice = next(r for r in records if r["group_key"] == "companion:voice")
        self.assertEqual(hd["group_label"], "HD packs")              # the kind is the heading, not the row
        self.assertIn("HD pack available", hd["badges"])
        self.assertEqual(voice["label"], "OAAB Data")       # nothing follows the master's name: the whole title
        self.assertEqual((voice["group_key"], voice["selection_type"], voice["recommended"]),
                         ("companion:voice", "SelectAny", True))
        self.assertNotIn("recommended", hd)
        self.assertIn("Older than the master", hd["badges"])
        self.assertIn("may overwrite newer files", hd["docs"])
        self.assertFalse(hd["required"])

        with mock.patch("sys.stdout", new_callable=io.StringIO) as said:
            mock_fomod._materialize_companions(package, records, {hd["key"]})
        self.assertNotIn("large pack", said.getvalue())          # a small archive gets no large-pack note
        self.assertTrue((staging.parent / "companions").is_dir())
        self.assertNotIn("payload_files", voice)        # not chosen: never extracted
        self.assertEqual(set(hd["payload_files"]), {str(Path("Textures/a.dds"))})
        applied = mock_fomod._activate(package, "OAAB_Data.esm", records, {hd["key"]}, {}, self.root / "backup")
        self.assertEqual((package / "Textures" / "a.dds").read_bytes(), b"hd")
        self.assertEqual(applied["selected"], [hd["key"]])
        clock = iter(range(0, 10_000, 20))                       # 20 s pass between file copies
        with mock.patch.object(mock_fomod.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            mock_fomod._activate(package, "OAAB_Data.esm", records, {hd["key"]}, {}, self.root / "backup2")
        self.assertIn("adding option files 0 of 1", out.getvalue())


class RequiredBy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def api(self, counts):
        def query(document, variables, timeout=8):
            if "game(" in document:
                return {"game": {"id": "100"}}
            return {f"m{i}": {"modCategory": {"name": category},
                              "modRequirements": {"modsRequiringThisMod": {"totalCount": count}}}
                    for i, (category, count) in enumerate(counts)}
        return query

    def test_api_count_is_cached_per_mod_id_with_its_date(self):
        with mock.patch.object(core_addons, "_nexus_query", side_effect=self.api([("New Lands", 12)])):
            first = core_addons.nexus_required_by({51776: "Dr_Data.esm"}, self.root)
        self.assertEqual(first[51776]["required_by"], 12)
        self.assertEqual(first[51776]["source"], "nexus api")
        cache = json.loads((self.root / core_addons.REQUIRED_BY_CACHE).read_text(encoding="utf-8"))
        self.assertTrue(cache["51776"]["fetched"])
        with mock.patch.object(core_addons, "_nexus_query", side_effect=AssertionError("network")):
            again = core_addons.nexus_required_by({51776: "Dr_Data.esm"}, self.root)
        self.assertEqual(again[51776]["required_by"], 12)

    def test_mod_page_table_when_the_api_fails(self):
        page = (b"<h3>Mods requiring this file</h3><table><thead><tr><th>Mod</th></tr></thead><tbody>"
                b"<tr><td>a</td></tr><tr><td>b</td></tr><tr class='x'><td>c</td></tr></tbody></table>")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = page
        with mock.patch.object(core_addons, "_nexus_query", side_effect=OSError("down")), \
                mock.patch.object(core_addons.urllib.request, "urlopen", return_value=response):
            found = core_addons.nexus_required_by({1: "X.esm"}, self.root)
        self.assertEqual((found[1]["required_by"], found[1]["source"]), (3, "nexus page"))

    def test_offline_counts_installed_plugins_that_name_the_master(self):
        data = self.root / "Data Files"
        data.mkdir()
        (data / "a.esp").write_bytes(tes3(["Morrowind.esm", "Dr_Data.esm"]))
        (data / "b.esp").write_bytes(tes3(["Morrowind.esm"]))
        with mock.patch.object(core_addons, "_nexus_query", side_effect=OSError("offline")), \
                mock.patch.object(core_addons.urllib.request, "urlopen", side_effect=OSError("offline")):
            found = core_addons.nexus_required_by({51776: "Dr_Data.esm"}, self.root, data_dir=data)
        self.assertEqual((found[51776]["required_by"], found[51776]["source"]), (1, "local"))
        self.assertFalse((self.root / core_addons.REQUIRED_BY_CACHE).exists())

    def test_count_only_suggests_new_masters_and_never_a_shipped_one(self):
        downloads = self.root / "downloads"
        downloads.mkdir()
        make_zip(downloads / "Big-111-1-0-1700000000.zip", {"Big_Data.esm": tes3()})
        make_zip(downloads / "Mid-222-1-0-1700000000.zip", {"Mid.esm": tes3()})
        make_zip(downloads / "NoEsm-333-1-0-1700000000.zip", {"x.esp": tes3()})
        vortex = {"big-111-1-0-1700000000.zip": {"mod_id": 111, "category": "Modders Resources and Tutorials"},
                  "mid-222-1-0-1700000000.zip": {"mod_id": 222, "category": "Patches"},
                  "noesm-333-1-0-1700000000.zip": {"mod_id": 333, "category": "Overhauls"},
                  "quest-444-1-0-1700000000.zip": {"mod_id": 444, "category": "Quests"},
                  "oaab.zip": {"mod_id": 49042, "category": "Modders Resources and Tutorials"}}
        asked = []

        def query(document, variables, timeout=8):
            if "game(" in document:
                return {"game": {"id": "100"}}
            asked.append(document)
            return {"m0": {"modCategory": {"name": "Modders Resources and Tutorials"},
                           "modRequirements": {"modsRequiringThisMod": {"totalCount": 40}}},
                    "m1": {"modCategory": {"name": "Patches"},
                           "modRequirements": {"modsRequiringThisMod": {"totalCount": 12}}},
                    "m2": {"modCategory": {"name": "Overhauls"},
                           "modRequirements": {"modsRequiringThisMod": {"totalCount": 90}}}}
        with mock.patch.object(core_addons, "_nexus_query", side_effect=query):
            found = core_addons.suggest_masters({"archives": []}, downloads, self.root, vortex=vortex)
        self.assertEqual([(s["master"], s["tier"], s["required_by"]) for s in found],
                         [("Big_Data.esm", "major", 40), ("Mid.esm", "common", 12)])
        self.assertNotIn("49042", asked[0])                  # shipped masters are not even asked about
        self.assertNotIn("444", asked[0])                    # Quests never qualify


if __name__ == "__main__":
    unittest.main()
