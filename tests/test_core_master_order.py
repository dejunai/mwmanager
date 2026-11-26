"""Patch for Purists is a major master in MWManager's priority list. Temporary folders only."""
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

    def test_current_stage_marker_keeps_a_newer_file_version_than_the_mod_page(self):
        # Wares Ultimate: the Nexus mod page says 2.1.1, the downloaded file is 2.1.3 (status current by date)
        item = {"archive": "Wares Ultimate-52013-2-1-3-1775985220.7z", "filename_version": "2.1.3",
                "nexus": {"wares-base.esm": {"status": "current", "latest": "2.1.1"}}}
        identity = core_addons._stage_plugin_identity(item, ["Wares-base.esm"])
        self.assertEqual((identity["PLUGIN_VERSION"], identity["NEXUS_VERSION"]), ("2.1.3", "2.1.1"))

    def test_current_stage_marker_keeps_the_nexus_spelling_of_the_same_number(self):
        item = {"archive": "Tamriel Data (HD) 59927 26.08 2026-08-23T04-15Z 4vY6vjul.7z", "filename_version": "26.8",
                "nexus": {"tamriel_data.esm": {"status": "current", "latest": "26.08"}}}
        identity = core_addons._stage_plugin_identity(item, ["Tamriel_Data.esm"])
        self.assertEqual(identity["PLUGIN_VERSION"], "26.08")


class NewestMainFile(unittest.TestCase):
    """Nexus can leave an archived file marked primary; Update must still use the newest MAIN file."""

    # GraphQL shape for OAAB_Data (mod 49042) after the 2.7.6 MAIN upload:
    # archived 2.7.1 is still primary, 2.7.6 is the current MAIN file.
    OAAB_FILES = [
        {"fileId": 1000071398, "name": "OAAB Data", "version": "2.7.1",
         "date": 1700000000, "primary": True, "category": "ARCHIVED"},
        {"fileId": 1000074664, "name": "OAAB Data", "version": "2.7.6",
         "date": 1764082320, "primary": False, "category": "MAIN"},
        {"fileId": 1000070001, "name": "OAAB Data", "version": "2.7.5",
         "date": 1760000000, "primary": False, "category": "OLD_VERSION"},
        {"fileId": 1000000001, "name": "removed", "version": "9.9.9",
         "date": 1999999999, "primary": False, "category": "REMOVED"},
        {"fileId": 1000000002, "name": "optional", "version": "8.0.0",
         "date": 1888888888, "primary": False, "category": "OPTIONAL"},
    ]

    def test_chooser_skips_archived_primary_and_retired_categories(self):
        chosen = core_addons._choose_newest_main_file(self.OAAB_FILES)
        self.assertEqual(chosen["fileId"], 1000074664)
        self.assertEqual(chosen["version"], "2.7.6")

    def test_chooser_orders_by_date_then_version_then_primary(self):
        files = [
            {"fileId": 1, "version": "2.0.0", "date": 100, "primary": True, "category": "MAIN"},
            {"fileId": 2, "version": "1.0.0", "date": 200, "primary": False, "category": "MAIN"},
        ]
        self.assertEqual(core_addons._choose_newest_main_file(files)["fileId"], 2)
        files = [
            {"fileId": 1, "version": "2.0.0", "date": 200, "primary": True, "category": "MAIN"},
            {"fileId": 2, "version": "3.0.0", "date": 200, "primary": False, "category": "MAIN"},
        ]
        self.assertEqual(core_addons._choose_newest_main_file(files)["fileId"], 2)
        files = [
            {"fileId": 1, "version": "3.0.0", "date": 200, "primary": False, "category": "MAIN"},
            {"fileId": 2, "version": "3.0.0", "date": 200, "primary": True, "category": "MAIN"},
        ]
        self.assertEqual(core_addons._choose_newest_main_file(files)["fileId"], 2)
        self.assertIsNone(core_addons._choose_newest_main_file([
            {"fileId": 1, "version": "9.0.0", "date": 999, "primary": True, "category": "ARCHIVED"},
        ]))

    def test_nexus_main_file_ignores_archived_primary(self):
        responses = [{"game": {"id": "100"}}, {"f": self.OAAB_FILES}]
        with mock.patch.object(core_addons, "_nexus_query", side_effect=responses) as query, \
                mock.patch.object(core_addons.urllib.request, "urlopen") as urlopen:
            chosen = core_addons.nexus_main_file(49042)
        self.assertEqual((chosen["fileId"], chosen["version"]), (1000074664, "2.7.6"))
        urlopen.assert_not_called()
        self.assertEqual(query.call_count, 2)

    def test_update_target_is_newest_main_when_primary_is_archived(self):
        plan = {"archives": [{"archive": "OAAB_Data 49042 2.7.5.7z",
                              "core_masters": ["OAAB_Data.esm"]}]}
        responses = [{"game": {"id": "100"}},
                     {"v0": {"version": "2.7.6"}, "m0": self.OAAB_FILES}]
        with mock.patch.object(core_addons, "_scrape_nexus_versions") as scrape, \
                mock.patch.object(core_addons, "_nexus_query", side_effect=responses), \
                mock.patch.object(core_addons.urllib.request, "urlopen") as urlopen:
            core_addons.verify_nexus_versions(plan)
        scrape.assert_not_called()
        urlopen.assert_not_called()
        status = plan["archives"][0]["nexus"]["oaab_data.esm"]
        self.assertEqual(status["status"], "outdated")
        self.assertEqual(status["latest"], "2.7.6")
        self.assertEqual(status["file_id"], 1000074664)
        self.assertIn("file_id=1000074664", core_addons.nexus_file_page(status["mod_id"], status["file_id"]))

    def test_file_version_marks_outdated_when_mod_header_lags(self):
        plan = {"archives": [{"archive": "Wares-Ultimate-52013-2-1-1.7z",
                              "core_masters": ["Wares-base.esm"]}]}
        responses = [{"game": {"id": "100"}},
                     {"v0": {"version": "2.1.1"},
                      "m0": [{"fileId": 99, "name": "Wares Ultimate", "version": "2.1.3",
                              "date": 1760000100, "primary": False, "category": "MAIN"}]}]
        with mock.patch.object(core_addons, "_scrape_nexus_versions") as scrape, \
                mock.patch.object(core_addons, "_nexus_query", side_effect=responses):
            core_addons.verify_nexus_versions(plan)
        scrape.assert_not_called()
        status = plan["archives"][0]["nexus"]["wares-base.esm"]
        self.assertEqual((status["status"], status["latest"], status["file_id"]),
                         ("outdated", "2.1.3", 99))


class SortPlacesIt(unittest.TestCase):
    def test_shipped_rules_do_not_contain_install_specific_master_order(self):
        # no rules are shipped: the file is each install's own, and a rollback sets it aside
        self.assertIn("mwmanager_rules.txt", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())


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
