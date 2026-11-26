"""MWSE updater offer, core KnownBug re-apply, and the --play stamp check.

Temporary folders only. The network is mocked. No absolute paths.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mwmanager_mwse_core as core  # noqa: E402
import mwdialog_luafix as luafix  # noqa: E402


LATEST = "1dce8de6b87c7b216c6809fbc261a176157764ce"
OLD_BUILD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ABS_PATH_RE = re.compile(r'(?i)\b[A-Z]:\\')


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="latin-1", newline="\n")
    else:
        path.write_bytes(data)
    return path


def core_parts(lf=luafix, rel=core.CORE_REL):
    return [(rule, old, new) for rule, _plugin, path, old, new, _what in lf.KNOWN_BUGS
            if path.replace("\\", "/").lower() == rel.lower()]


def stock_exclusionspage(lf=luafix):
    """A small, valid Lua file that still contains every core KnownBug fingerprint."""
    chunks = []
    seen = set()
    for rule, old, _new in core_parts(lf):
        if old in seen:
            continue
        seen.add(old)
        if rule == "KB3":
            chunks.append("function ExclusionsPage:create(parent)\n" + old + "\nend\n")
        elif old.lstrip().startswith("function "):
            chunks.append(old if old.endswith("\n") else old + "\n")
        elif "Constructor" in old or "placeholderText" in old:
            chunks.append(old if old.endswith("\n") else old + "\n")
        else:
            chunks.append("function ExclusionsPage:filter(button, items)\n" + old + "\nend\n")
    return "-- stock MWSE ExclusionsPage fixture\n\n" + "\n".join(chunks) + "\n"


def patched_exclusionspage(lf=luafix):
    """The same fixture after the exact KnownBug replacements, with the localpatch banner."""
    text = stock_exclusionspage(lf)
    rules = []
    for rule, old, new in core_parts(lf):
        if old in text:
            text = text.replace(old, new, 1)
            if rule not in rules:
                rules.append(rule)
    banner = f"--[[localpatch {getattr(lf, 'VERSION', 5)}: {' '.join(rules)}]] "
    return banner + text


class AcceptChecker:
    name = "test-accept"

    def check(self, text):
        return True, ""


class RejectChecker:
    name = "test-reject"

    def check(self, text):
        return False, "rejected in test"


class FakeProgress:
    def __init__(self, yes_no=None):
        self.lines = []
        self.answers = list(yes_no or [])

    def __call__(self, message):
        self.lines.append(str(message))

    def ask_yes_no(self, title, message):
        self.lines.append(f"ASK {title}: {message}")
        if not self.answers:
            return False
        return bool(self.answers.pop(0))

    def shown(self):
        return [line for line in self.lines if not line.startswith(core.DETAIL_PREFIX)]

    def details(self):
        return [line for line in self.lines if line.startswith(core.DETAIL_PREFIX)]


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = self.root / "MWManager"
        self.game = self.root / "Morrowind"
        self.manager.mkdir()
        # Unit tests must not depend on whether Morrowind.exe is running on the host.
        proc = mock.patch.object(core, "morrowind_process_running", return_value=False)
        proc.start()
        self.addCleanup(proc.stop)
        write(self.game / "Morrowind.ini", "[General]\n")
        write(self.game / "Morrowind.exe", b"MZ")
        write(self.game / "MWSE.dll", b"MZ")
        write(self.game / "MWSE-Update.exe", b"MZ")
        write(self.game / "mwse-version.txt", LATEST)
        self.core_lua = self.game / "Data Files" / "MWSE" / "core" / "mcm" / "components" / "pages" / "exclusionspage.lua"

    def write_stock(self):
        write(self.core_lua, stock_exclusionspage())
        return self.core_lua

    def write_patched(self):
        write(self.core_lua, patched_exclusionspage())
        return self.core_lua

    def md(self):
        return str(self.manager)


class FingerprintAndStamp(Repo):
    def test_stock_fixture_is_pending_patched_is_current(self):
        self.write_stock()
        scanned = core.pending_core_patches(self.game, manager_dir=self.md(), checker=AcceptChecker())
        self.assertEqual(scanned["status"], "pending")
        self.assertIn("KB3", scanned["pending_rules"])
        self.assertIn("KB4", scanned["pending_rules"])
        self.assertTrue(scanned["pending"])

        self.write_patched()
        scanned = core.pending_core_patches(self.game, manager_dir=self.md(), checker=AcceptChecker())
        self.assertEqual(scanned["status"], "current")
        self.assertFalse(scanned["pending"])
        self.assertFalse(scanned["partial"])
        text = self.core_lua.read_text(encoding="latin-1")
        self.assertIn("localpatch", text)
        self.assertNotIn("self.elements.outerContainer.parent:destroyChildren()", text)

    def test_partial_kb4_is_left_alone(self):
        # Only the first of KB4's interdependent parts, plus KB3, so KB4 is partial.
        kb3_old = next(old for rule, old, _new in core_parts() if rule == "KB3")
        kb4_olds = [old for rule, old, _new in core_parts() if rule == "KB4"]
        self.assertGreaterEqual(len(kb4_olds), 2)
        text = (
            "function ExclusionsPage:create(parent)\n" + kb3_old + "\nend\n\n"
            + kb4_olds[0] + "\n"
        )
        write(self.core_lua, text)
        scanned = core.pending_core_patches(self.game, manager_dir=self.md(), checker=AcceptChecker())
        self.assertEqual(scanned["status"], "partial")
        self.assertIn("KB4", scanned["partial_rules"])

    def test_stamp_matches_only_when_version_hashes_and_rules_agree(self):
        self.write_patched()
        core.write_stamp(self.game, self.md())
        self.assertTrue(core.stamp_matches(self.game, self.md()))

        write(self.game / "mwse-version.txt", OLD_BUILD)
        self.assertFalse(core.stamp_matches(self.game, self.md()))
        write(self.game / "mwse-version.txt", LATEST)
        self.assertTrue(core.stamp_matches(self.game, self.md()))

        # A different game folder must not reuse the stamp.
        other = self.root / "OtherGame"
        write(other / "mwse-version.txt", LATEST)
        write(other / "Data Files" / "MWSE" / "core" / "mcm" / "components" / "pages" / "exclusionspage.lua",
              patched_exclusionspage())
        self.assertFalse(core.stamp_matches(other, self.md()))

    def test_ini_flags_default_true_and_round_trip(self):
        self.assertTrue(core.ini_flag("check_on_play", self.md()))
        self.assertTrue(core.ini_flag("apply_core_patches", self.md()))
        self.assertTrue(core.ini_flag("offer_update_on_full", self.md()))
        core.write_ini_flags({"check_on_play": False, "apply_core_patches": False,
                              "offer_update_on_full": True}, self.md())
        self.assertFalse(core.ini_flag("check_on_play", self.md()))
        self.assertFalse(core.ini_flag("apply_core_patches", self.md()))
        self.assertTrue(core.ini_flag("offer_update_on_full", self.md()))
        text = Path(core.ini_path(self.md())).read_text(encoding="utf-8")
        self.assertIn("[MWSE]", text)
        self.assertNotRegex(text, ABS_PATH_RE)


class ApplyAndPlay(Repo):
    def test_apply_rewrites_stock_and_writes_stamp_and_journal(self):
        self.write_stock()
        result = core.reapply_core_patches(self.game, manager_dir=self.md(), checker=AcceptChecker(),
                                           require_closed=False)
        self.assertEqual(result["status"], "applied", result)
        self.assertGreater(result["written"], 0)
        text = self.core_lua.read_text(encoding="latin-1")
        self.assertIn("localpatch", text)
        self.assertIn("PAGE_SIZE", text)
        self.assertNotIn("self.elements.outerContainer.parent:destroyChildren()", text)
        self.assertTrue(core.stamp_matches(self.game, self.md()))
        journal = Path(core.luafix_journal_path(self.md()))
        self.assertTrue(journal.is_file())
        self.assertIn("exclusionspage.lua", journal.read_text(encoding="utf-8"))
        # A second run is a no-op.
        again = core.reapply_core_patches(self.game, manager_dir=self.md(), checker=AcceptChecker(),
                                          require_closed=False)
        self.assertEqual(again["status"], "unchanged", again)
        self.assertEqual(again["written"], 0)

    def test_play_skips_apply_when_stamp_matches(self):
        self.write_patched()
        core.write_stamp(self.game, self.md())
        with mock.patch.object(core, "pending_core_patches") as pending, \
                mock.patch.object(core, "reapply_core_patches") as apply:
            result = core.ensure_core_patches_for_play(self.game, manager_dir=self.md())
        self.assertFalse(result["block"])
        self.assertEqual(result["status"], "current")
        pending.assert_not_called()
        apply.assert_not_called()

    def test_live_morrowind_process_does_not_change_unit_tests(self):
        # The real guard stays in production code; tests inject the process check.
        self.write_stock()
        before = self.core_lua.read_bytes()
        runner = mock.Mock()
        write(self.game / "mwse-version.txt", OLD_BUILD)
        with mock.patch.object(core, "morrowind_process_running", return_value=True), \
                mock.patch("mwmanager_prereqs.latest_mwse_build", return_value=(LATEST, None)):
            result = core.reapply_core_patches(
                self.game, manager_dir=self.md(), checker=AcceptChecker())
            offer = core.offer_mwse_update(
                self.game, yes=True, manager_dir=self.md(), runner=runner,
                checker=AcceptChecker())
        self.assertEqual(result["status"], "refused", result)
        self.assertTrue(any("Morrowind.exe is running" in line for line in result["detail"]))
        self.assertEqual(self.core_lua.read_bytes(), before)
        self.assertFalse(offer["updated"])
        self.assertIn("Morrowind.exe is running", offer["message"])
        runner.assert_not_called()

    def test_play_applies_stock_core_then_allows_launch(self):
        self.write_stock()
        progress = FakeProgress()
        result = core.ensure_core_patches_for_play(
            self.game, manager_dir=self.md(), checker=AcceptChecker(), progress=progress)
        self.assertFalse(result["block"], result)
        self.assertEqual(result["status"], "applied")
        self.assertTrue(core.stamp_matches(self.game, self.md()))
        self.assertTrue(any("reapplied" in line for line in progress.shown()))

    def test_play_blocks_like_missing_masters_unless_play_anyway(self):
        self.write_stock()
        # Do not apply; fingerprints stay pending.
        result = core.ensure_core_patches_for_play(
            self.game, manager_dir=self.md(), apply=False, checker=AcceptChecker())
        self.assertTrue(result["block"])
        self.assertEqual(result["code"], core.PLAY_BLOCK_CODE)
        self.assertIn("need reapplying", result["message"])
        log = Path(core.detail_log_path(self.md())).read_text(encoding="utf-8")
        self.assertIn("apply_core_patches is false", log)
        # Short status is a sentence, not a dump of replacement Lua.
        self.assertNotIn("PAGE_SIZE", result["message"])

        anyway = core.ensure_core_patches_for_play(
            self.game, manager_dir=self.md(), apply=False, play_anyway=True, checker=AcceptChecker())
        self.assertFalse(anyway["block"])
        self.assertTrue(anyway.get("play_anyway"))
        # The stock file was still not written.
        self.assertIn("self.elements.outerContainer.parent:destroyChildren()",
                      self.core_lua.read_text(encoding="latin-1"))

    def test_play_blocks_on_partial_fingerprint(self):
        kb4_olds = [old for rule, old, _new in core_parts() if rule == "KB4"]
        write(self.core_lua, kb4_olds[0] + "\n")
        result = core.ensure_core_patches_for_play(
            self.game, manager_dir=self.md(), checker=AcceptChecker())
        self.assertTrue(result["block"])
        self.assertEqual(result["status"], "partial")
        self.assertIn("could not be reapplied", result["message"])
        self.assertIn("only partly", result["message"])
        anyway = core.ensure_core_patches_for_play(
            self.game, manager_dir=self.md(), checker=AcceptChecker(), play_anyway=True)
        self.assertFalse(anyway["block"])
        self.assertTrue(anyway.get("play_anyway"))
        self.assertFalse(Path(core.stamp_path(self.md())).is_file())

    def test_check_on_play_false_skips_the_check(self):
        self.write_stock()
        core.write_ini_flags({"check_on_play": False}, self.md())
        with mock.patch.object(core, "reapply_core_patches") as apply:
            result = core.ensure_core_patches_for_play(self.game, manager_dir=self.md())
        self.assertFalse(result["block"])
        self.assertEqual(result["status"], "skipped")
        apply.assert_not_called()

    def test_refused_apply_blocks_play(self):
        self.write_stock()
        result = core.ensure_core_patches_for_play(
            self.game, manager_dir=self.md(), checker=RejectChecker())
        self.assertTrue(result["block"])
        self.assertEqual(result["code"], core.PLAY_BLOCK_CODE)
        self.assertEqual(result["status"], "refused")
        self.assertIn("could not be reapplied", result["message"])
        self.assertFalse(Path(core.stamp_path(self.md())).is_file())

    def test_play_blocks_when_the_core_file_is_missing(self):
        result = core.ensure_core_patches_for_play(
            self.game, manager_dir=self.md(), checker=AcceptChecker())
        self.assertTrue(result["block"])
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["code"], core.PLAY_BLOCK_CODE)
        self.assertIn("could not be reapplied", result["message"])
        self.assertFalse(Path(core.stamp_path(self.md())).is_file())


class UpdaterOffer(Repo):
    def test_offer_uses_build_automatic_not_master(self):
        self.write_patched()
        with mock.patch("mwmanager_prereqs.latest_mwse_build", return_value=(LATEST, None)) as latest, \
                mock.patch("mwmanager_prereqs.MWSE_LATEST_API",
                           "https://api.github.com/repos/MWSE/MWSE/releases/tags/build-automatic"):
            result = core.offer_mwse_update(self.game, manager_dir=self.md(), reapply_after=False)
        latest.assert_called()
        self.assertEqual(result["state"], "current")
        self.assertFalse(result["updated"])

    def test_outdated_runs_updater_then_reapplies(self):
        self.write_stock()
        write(self.game / "mwse-version.txt", OLD_BUILD)
        progress = FakeProgress()

        def runner(cmd, cwd, check):
            self.assertEqual(Path(cmd[0]).name, "MWSE-Update.exe")
            # The official updater would unpack a stock core tree; leave the stock fixture
            # and only advance the version marker.
            write(self.game / "mwse-version.txt", LATEST)
            return mock.Mock(returncode=0)

        with mock.patch("mwmanager_prereqs.latest_mwse_build", return_value=(LATEST, None)):
            result = core.offer_mwse_update(
                self.game, yes=True, manager_dir=self.md(), runner=runner,
                checker=AcceptChecker(), progress=progress, reapply_after=True)
        self.assertTrue(result["updated"])
        self.assertEqual(result["state"], "updated")
        self.assertIsNotNone(result["patches"])
        self.assertEqual(result["patches"]["status"], "applied")
        self.assertIn("localpatch", self.core_lua.read_text(encoding="latin-1"))
        self.assertTrue(core.stamp_matches(self.game, self.md()))
        self.assertTrue(any("updater finished" in line.lower() or "reapplied" in line
                            for line in progress.shown()))

    def test_offer_update_on_full_false_skips_the_prompt(self):
        write(self.game / "mwse-version.txt", OLD_BUILD)
        core.write_ini_flags({"offer_update_on_full": False}, self.md())
        runner = mock.Mock()
        with mock.patch("mwmanager_prereqs.latest_mwse_build", return_value=(LATEST, None)):
            result = core.offer_mwse_update(self.game, yes=True, manager_dir=self.md(), runner=runner)
        self.assertEqual(result["state"], "skipped-offer")
        runner.assert_not_called()

        # Tools > Update MWSE uses force=True and still offers.
        with mock.patch("mwmanager_prereqs.latest_mwse_build", return_value=(LATEST, None)):
            result = core.offer_mwse_update(
                self.game, force=True, yes=False, manager_dir=self.md(), runner=runner,
                ask=lambda title, message: False, reapply_after=False)
        self.assertTrue(result.get("skipped"))
        runner.assert_not_called()

    def test_current_force_reapplies_without_running_the_updater(self):
        self.write_stock()
        runner = mock.Mock()
        with mock.patch("mwmanager_prereqs.latest_mwse_build", return_value=(LATEST, None)):
            result = core.offer_mwse_update(
                self.game, force=True, manager_dir=self.md(), runner=runner,
                checker=AcceptChecker(), reapply_after=True)
        runner.assert_not_called()
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["patches"]["status"], "applied")
        self.assertIn("localpatch", self.core_lua.read_text(encoding="latin-1"))

    def test_preflight_runner_reapplies_after_the_updater(self):
        self.write_stock()
        write(self.game / "mwse-version.txt", OLD_BUILD)
        progress = FakeProgress()

        def runner(cmd, cwd, check):
            write(self.game / "mwse-version.txt", LATEST)
            return mock.Mock(returncode=0)

        ok = core.run_updater_then_reapply(
            self.game, latest=LATEST, progress=progress, runner=runner,
            manager_dir=self.md(), checker=AcceptChecker())
        self.assertTrue(ok)
        self.assertIn("localpatch", self.core_lua.read_text(encoding="latin-1"))
        self.assertTrue(any("latest build" in line for line in progress.shown()))

    def test_preflight_reapply_does_not_fail_or_stamp_when_core_lua_is_missing(self):
        write(self.game / "mwse-version.txt", OLD_BUILD)
        progress = FakeProgress()

        def runner(cmd, cwd, check):
            write(self.game / "mwse-version.txt", LATEST)
            return mock.Mock(returncode=0)

        ok = core.run_updater_then_reapply(
            self.game, latest=LATEST, progress=progress, runner=runner,
            manager_dir=self.md(), checker=AcceptChecker())
        self.assertTrue(ok)
        self.assertFalse(Path(core.stamp_path(self.md())).is_file())
        self.assertTrue(any("latest build" in line for line in progress.shown()))
        # The helper refused the missing file; that is logged, not a preflight failure.
        log = Path(core.detail_log_path(self.md())).read_text(encoding="utf-8")
        self.assertIn("refused", log.lower())


class AdapterAndPaths(Repo):
    def test_adapter_calls_apply_core_known_bugs_dry_run_then_apply(self):
        self.write_stock()
        calls = []
        real = luafix.apply_core_known_bugs

        def wrapper(data_dir, dry_run=False, **kwargs):
            calls.append(bool(dry_run))
            return real(data_dir, dry_run, **kwargs)

        with mock.patch.object(luafix, "apply_core_known_bugs", side_effect=wrapper):
            pending = core.pending_core_patches(
                self.game, lf=luafix, checker=AcceptChecker(), manager_dir=self.md())
            result = core.reapply_core_patches(
                self.game, manager_dir=self.md(), lf=luafix, checker=AcceptChecker(),
                require_closed=False)
        self.assertEqual(pending["status"], "pending")
        self.assertTrue(pending["pending"])
        self.assertEqual(result["status"], "applied")
        self.assertGreater(result["written"], 0)
        self.assertTrue(calls, "the helper was not called")
        self.assertTrue(calls[0], "the first helper call must be a dry run")
        self.assertIn(False, calls)
        self.assertEqual(core.HELPER_APPLY, "apply_core_known_bugs")

    def test_sources_and_tests_have_no_absolute_paths(self):
        files = [
            ROOT / "mwmanager_mwse_core.py",
            Path(__file__),
        ]
        host = "G:" + "\\GOG"
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, ABS_PATH_RE, f"absolute path in {path.name}")
            if path.name == "mwmanager_mwse_core.py":
                self.assertNotIn("/" + "home/", text)
                self.assertNotIn(host, text)
                self.assertNotIn("Program Files", text)

    def test_stock_and_patched_fixtures_do_not_embed_host_paths(self):
        self.assertNotRegex(stock_exclusionspage(), ABS_PATH_RE)
        self.assertNotRegex(patched_exclusionspage(), ABS_PATH_RE)

    def test_detail_log_gets_the_long_story(self):
        self.write_stock()
        core.ensure_core_patches_for_play(
            self.game, manager_dir=self.md(), apply=False, checker=AcceptChecker())
        log = Path(core.detail_log_path(self.md())).read_text(encoding="utf-8")
        self.assertIn("stamp miss", log)
        self.assertIn("core-only", log)
        self.assertIn("exclusionspage.lua", log.lower())


if __name__ == "__main__":
    unittest.main()
