"""Patch for Purists is a major master: first in MWManager's priority list (right after Bloodmoon.esm), and
mwmanager_rules.txt has the sort rule that puts it there. Temporary folders only; nothing real is touched."""
import json
import struct
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import core_addons  # noqa: E402
import mwmanager_upstream  # noqa: E402

PFP = "Patch for Purists.esm"


def plugin(path, masters):
    def sub(n, d):
        return n.encode() + struct.pack("<I", len(d)) + d
    body = sub("HEDR", struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", 0))
    for m in masters:
        body += sub("MAST", m.encode() + b"\0") + sub("DATA", struct.pack("<Q", 1))
    Path(path).write_bytes(b"TES3" + struct.pack("<III", len(body), 0, 0) + body)


class PriorityList(unittest.TestCase):
    def test_large_archive_extraction_status_warns_that_it_will_take_time(self):
        archive = mock.Mock()
        archive.name = "Tamriel Data HD.7z"
        archive.stat.return_value.st_size = 6 * 1024 ** 3
        status = core_addons.extraction_status(archive, 2, 4)
        self.assertIn("6.0 GB", status)
        self.assertIn("please be patient", status)
        self.assertIn("still working", status)

    def test_patch_for_purists_is_the_first_major_master(self):
        self.assertEqual(next(iter(core_addons.BUILTIN_MASTERS)), PFP.lower())
        shipped = json.loads((ROOT / "mwmanager_core_addons.json").read_text(encoding="utf-8"))
        self.assertEqual(next(iter(shipped)), PFP.lower())
        self.assertEqual(set(shipped), set(core_addons.BUILTIN_MASTERS) | {"cyr_main.esm", "sky_main.esm"})
        order = list(shipped)
        self.assertLess(order.index("tamriel_data.esm"), order.index("tr_mainland.esm"))
        self.assertNotIn("sound_data.esm", order)              # users add it themselves if they want it
        self.assertLess(order.index("dr_data.esm"), order.index("wares-base.esm"))

    def test_which_mods_are_major_or_common_masters(self):
        tier = core_addons.master_tier
        self.assertEqual(tier("Modders Resources and Tutorials", 548, True), "major")
        self.assertEqual(tier("Overhauls", 17, True), "major")                        # Wares
        self.assertEqual(tier("New Lands", 12, True), "common")
        self.assertIsNone(tier("Patches", 10, True))                                  # 10 or fewer: neither
        self.assertIsNone(tier("Dungeons and Locations", 99, True))                   # category disqualifies
        self.assertIsNone(tier("Modders Resources and Tutorials", 99, False))         # no ESM: not a master

    def test_tamriel_data_hd_and_sd_are_separate_nexus_mods(self):
        self.assertEqual(core_addons.master_mod_id("Tamriel_Data.esm", "Tamriel Data (HD) 59927 26.08.7z"), 59927)
        self.assertEqual(core_addons.master_mod_id("Tamriel_Data.esm", "Tamriel_Data 44537 26.08.7z"), 44537)
        self.assertEqual(core_addons.master_mod_id("OAAB_Data.esm", "OAAB_Data (HD) 49042.7z"), 49042)
        self.assertIn("/mods/59927?", core_addons.nexus_page("tamriel_data.esm", "Tamriel Data (HD).7z"))
        with tempfile.TemporaryDirectory() as tmp:            # no JSON yet: the built-in list
            self.assertEqual(core_addons.configured_master_names(tmp)[0], PFP.lower())

    def test_downloaded_archive_is_checked_against_nexus(self):
        plan = {"archives": [{"archive": "Patch-for-Purists-45096-5-0-6-1760000000.7z",
                              "core_masters": [PFP]}]}
        responses = [{"game": {"id": "100"}},
                     {"v0": {"version": "5.0.7"},
                      "m0": [{"name": "Patch for Purists", "version": "5.0.6",
                               "date": 1760000100, "primary": True, "category": "MAIN"}]}]
        with mock.patch.object(core_addons, "_scrape_nexus_versions", return_value={}) as scrape, \
                mock.patch.object(core_addons, "_nexus_query", side_effect=responses):
            core_addons.verify_nexus_versions(plan)
        scrape.assert_not_called()
        status = plan["archives"][0]["nexus"][PFP.casefold()]
        self.assertEqual(status["status"], "outdated")
        self.assertEqual(status["latest"], "5.0.7")
        self.assertEqual(status["page"], "https://www.nexusmods.com/morrowind/mods/45096?tab=files")

    def test_files_page_supplies_version_missing_from_api(self):
        plan = {"archives": [{"archive": "Patch-for-Purists-45096-5-0-6-1760000000.7z",
                              "core_masters": [PFP]}]}
        responses = [{"game": {"id": "100"}}, {"m0": []}]
        with mock.patch.object(core_addons, "_scrape_nexus_versions",
                               return_value={PFP.casefold(): "5.0.7"}), \
                mock.patch.object(core_addons, "_nexus_query", side_effect=responses):
            core_addons.verify_nexus_versions(plan)
        status = plan["archives"][0]["nexus"][PFP.casefold()]
        self.assertEqual((status["status"], status["latest"]), ("outdated", "5.0.7"))

    def test_mod_header_version_overrides_stale_file_version(self):
        plan = {"archives": [{"archive": "OAAB_Data-49042-2-7-5-1760000000.7z",
                              "core_masters": ["OAAB_Data.esm"]}]}
        responses = [{"game": {"id": "100"}},
                     {"v0": {"version": "2.7.5"},
                      "m0": [{"name": "OAAB Data", "version": "2.7.1", "date": 1760000000,
                               "primary": True, "category": "MAIN"}]}]
        with mock.patch.object(core_addons, "_scrape_nexus_versions") as scrape, \
                mock.patch.object(core_addons, "_nexus_query", side_effect=responses):
            core_addons.verify_nexus_versions(plan)
        scrape.assert_not_called()
        status = plan["archives"][0]["nexus"]["oaab_data.esm"]
        self.assertEqual((status["status"], status["latest"]), ("current", "2.7.5"))

    def test_files_page_version_parser_uses_header(self):
        page = "Patch for Purists\nVersion\n5.0.7\nMain files\nPatch archive\nVersion 5.0.6\n"
        self.assertEqual(core_addons._files_page_version(page), "5.0.7")

    def test_browser_fallback_reports_success_and_each_page(self):
        import mwnexus_browser

        class Progress(list):
            def __call__(self, message):
                self.append(message)

            def ask_yes_no(self, title, message):
                self.prompt = (title, message)
                return True

        progress = Progress()
        page = {"text": "Patch for Purists\nVersion\n5.0.7\nMain files\nPatch archive"}
        with mock.patch.object(core_addons.urllib.request, "urlopen", side_effect=OSError("blocked")), \
                mock.patch.object(mwnexus_browser, "open_page"), \
                mock.patch.object(mwnexus_browser, "capture_page", return_value=page), \
                mock.patch.object(mwnexus_browser, "close_browser"):
            versions = core_addons._scrape_nexus_versions({PFP.casefold()}, progress, 1)
        self.assertEqual(versions[PFP.casefold()], "5.0.7")
        self.assertIn("opened successfully", progress.prompt[1])
        self.assertTrue(any("Reading Nexus Files page 1 of 1" in line for line in progress))

    def test_nexus_upload_timestamp_beats_broken_vortex_version(self):
        plan = {"archives": [{"archive": "Patch-for-Purists-45096-1-0-1760000100 (2).7z",
                              "core_masters": [PFP]}]}
        responses = [{"game": {"id": "100"}},
                     {"m0": [{"name": "Patch for Purists", "version": "5.0.7",
                               "date": 1760000100, "primary": True, "category": "MAIN"}]}]
        with mock.patch.object(core_addons, "_scrape_nexus_versions",
                               return_value={PFP.casefold(): "5.0.7"}), \
                mock.patch.object(core_addons, "_nexus_query", side_effect=responses):
            core_addons.verify_nexus_versions(plan)
        self.assertEqual(plan["archives"][0]["nexus"][PFP.casefold()]["status"], "current")

    def test_staged_master_keeps_the_verified_archive_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = Path(tmp)
            staged = manager / "staging" / "Patch for Purists" / PFP
            staged.parent.mkdir(parents=True)
            plugin(staged, ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"])
            plan = {"archives": [{"archive": str(manager / "Patch-for-Purists-45096-5-0-7.7z"),
                                   "core_masters": [PFP],
                                   "nexus": {PFP.casefold(): {"status": "current", "latest": "5.0.7"}}}]}

            prepared = core_addons.prepare_core_master_review(manager, manager / "game", plan)
            row = next(row for row in prepared["rows"] if row["name"].casefold() == PFP.casefold())

            self.assertEqual(row["nexus"], {"status": "current", "latest": "5.0.7"})

    def test_stage_marker_identity_uses_the_verified_current_version(self):
        item = {"archive": "Patch-for-Purists-45096-5-0-7.7z", "filename_version": "5.0.7",
                "nexus": {PFP.casefold(): {"status": "current", "latest": "5.0.7",
                                            "verified_at": "2026-11-16T04:00:00-08:00",
                                            "page": "https://www.nexusmods.com/morrowind/mods/45096?tab=files"}}}
        identity = core_addons._stage_plugin_identity(item, [PFP])
        self.assertEqual(list(identity),
                         ["PLUGIN_NAME", "PLUGIN_VERSION", "NEXUS_VERSION", "NEXUS_VERSION_LAST_VERIFIED",
                          "NEXUS_MOD_PAGE"])
        self.assertEqual(identity, {
            "PLUGIN_NAME": "Patch for Purists",
            "PLUGIN_VERSION": "5.0.7",
            "NEXUS_VERSION": "5.0.7",
            "NEXUS_VERSION_LAST_VERIFIED": "2026-11-16T04:00:00-08:00",
            "NEXUS_MOD_PAGE": "https://www.nexusmods.com/morrowind/mods/45096?tab=files",
        })

    def test_outdated_stage_marker_keeps_its_local_version(self):
        item = {"archive": "Patch-for-Purists-45096-5-0-6.7z", "filename_version": "5.0.6",
                "nexus": {PFP.casefold(): {"status": "outdated", "latest": "5.0.7",
                                            "verified_at": "2026-11-16T04:00:00-08:00"}}}
        identity = core_addons._stage_plugin_identity(item, [PFP])
        self.assertEqual((identity["PLUGIN_VERSION"], identity["NEXUS_VERSION"]), ("5.0.6", "5.0.7"))


class SortPlacesIt(unittest.TestCase):
    def test_rules_file_puts_it_right_after_bloodmoon(self):
        import mwmanager as m
        with tempfile.TemporaryDirectory() as tmp, mwmanager_upstream.use_manager_dir(tmp):
            df = Path(tmp) / "game" / "Data Files"
            df.mkdir(parents=True)
            layout = [("Morrowind.esm", []), ("Tribunal.esm", ["Morrowind.esm"]), ("Bloodmoon.esm", ["Morrowind.esm"]),
                      ("AATL_Data.esm", ["Morrowind.esm"]), ("Tamriel_Data.esm", ["Morrowind.esm"]),
                      ("OAAB_Data.esm", ["Morrowind.esm"]), (PFP, ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"]),
                      ("A Mod.esp", ["Morrowind.esm"])]
            plugins = []
            for name, masters in layout:
                plugin(df / name, masters)
                plugins.append((name, str(df / name)))
            rules = m.parse_rules(str(ROOT / "mwmanager_rules.txt"))[0]          # the shipped rules file
            self.assertTrue(any(kind == "nearstart" and PFP in entries for kind, entries, _src in rules))
            order, _notes = m.compute_order(plugins, [rules], [n for n, _p in plugins])
        self.assertEqual(order[:4], ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm", PFP])
        self.assertLess(order.index("Tamriel_Data.esm"), order.index("OAAB_Data.esm"))


class LongRunningCaches(unittest.TestCase):
    def test_mastercheck_caches_can_be_cleared(self):
        import mwdialog_mastercheck as mc
        mc._STAGING_INDEX["x"] = {}
        mc.clear_caches()
        self.assertEqual(mc._STAGING_INDEX, {})


if __name__ == "__main__":
    unittest.main()


class TamrielDataVariantChoice(unittest.TestCase):
    """Both Tamriel_Data variants downloaded: the plan says so, and the user's pick survives a rescan."""

    def test_the_chosen_variant_wins_and_is_kept_across_rescans(self):
        import io
        import zipfile
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            downloads, manager = tmp / "dl", tmp / "mgr"
            downloads.mkdir(), manager.mkdir()
            esm = tmp / "Tamriel_Data.esm"
            plugin(esm, [])
            # SD has the higher version, so it wins by default
            for name in ("Tamriel Data (HD) 59927 26.08 2026-08-23T04-15Z a.zip",
                         "Tamriel_Data 44537 26.09 2026-09-23T04-15Z b.zip"):
                with zipfile.ZipFile(downloads / name, "w") as z:
                    z.write(esm, "Data Files/Tamriel_Data.esm")
            (manager / "mwmanager_core_addons.json").write_text(
                json.dumps({"tamriel_data.esm": ["tamriel data", "tamriel_data"]}), encoding="utf-8")

            def scan():
                with redirect_stdout(io.StringIO()):
                    plan = core_addons.scan_downloads(downloads, manager, tmp / "out", dry_run=True)
                return plan, Path(plan["archives"][0]["archive"]).name

            plan, used = scan()
            self.assertTrue(used.startswith("Tamriel_Data 44537"))
            self.assertEqual(plan["master_variants"], {"tamriel_data.esm": {"available": ["HD", "SD"],
                                                                           "using": "SD"}})
            core_addons.choose_master_variant(manager, "Tamriel_Data.esm", "HD")
            plan, used = scan()
            self.assertTrue(used.startswith("Tamriel Data (HD)"))
            self.assertEqual(plan["master_variants"]["tamriel_data.esm"]["using"], "HD")
            self.assertEqual(len(plan["superseded_archives"]), 1)
