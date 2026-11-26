"""Update rules: fetch only if newer, then prune in place against the new files, then save.

Temporary folders only; the network is mocked. No absolute paths.
"""
import json
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mwmanager_mlox as mlox  # noqa: E402


DISABLE_RE = re.compile(
    r"^;; MWManager: disabled - (.+) \((\d{4}-\d{2}-\d{2})\)$"
)


def _rule_text(*blocks, marker=""):
    parts = [f";; {marker}"] if marker else []
    for kind, entries in blocks:
        parts.append(f"[{kind}]")
        parts.extend(entries)
        parts.append("")
    return "\n".join(parts) + "\n"


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if str(k).lower() == str(key).lower():
                return v
        return default


class FakeResponse:
    def __init__(self, data=b"", status=200, headers=None):
        self._data = data
        self.status = status
        self.headers = FakeHeaders(headers or {})

    def read(self, size=-1):
        if size is None or size < 0:
            return self._data
        return self._data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeProgress:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(str(message))

    def shown(self):
        return [line for line in self.lines if not line.startswith("MWMANAGER-DETAIL:")]

    def details(self):
        return [line for line in self.lines if line.startswith("MWMANAGER-DETAIL:")]


class UpdateRulesOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = self.root / "MWManager"
        self.game = self.root / "Morrowind"
        self.folder = self.manager / "mlox"
        self.folder.mkdir(parents=True)
        (self.game / "Data Files").mkdir(parents=True)
        for name in ("Mine.esp", "Other.esp", "Alpha.esp"):
            (self.game / "Data Files" / name).write_bytes(b"TES3")
        self.old_base = _rule_text(("Order", ["Alpha.esp", "Mine.esp"]), marker="OLD-BASE-MARKER")
        self.new_base = _rule_text(("Order", ["Other.esp", "Mine.esp"]), marker="NEW-BASE-MARKER")
        self.user = _rule_text(("Order", ["Mine.esp", "Other.esp"]), marker="USER-RULE")
        (self.folder / "mlox_base.txt").write_text(self.old_base, encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text(self.user, encoding="utf-8")
        self.plugins = ["Mine.esp", "Other.esp", "Alpha.esp"]

    def opener(self, mapping, status=200, headers=None, errors=None):
        errors = errors or {}
        headers = headers or {"ETag": '"new-etag"'}

        def _open(request, timeout=None):
            url = mlox._url_of(request)
            for name, content in mapping.items():
                if url == mlox.MLOX_RULE_URLS[name] or str(url).endswith(name):
                    if name in errors:
                        raise errors[name]
                    data = content if isinstance(content, bytes) else content.encode("utf-8")
                    return FakeResponse(data, status=status, headers=headers)
            raise FileNotFoundError(url)

        return _open

    def _assert_rule_disabled(self, text, reason_part, *entries):
        lines = text.splitlines()
        markers = [line for line in lines if line.startswith(";; MWManager: disabled")]
        self.assertTrue(markers, text)
        match = DISABLE_RE.match(markers[0])
        self.assertIsNotNone(match, markers[0])
        self.assertIn(reason_part, match.group(1))
        # The rule block immediately below the marker is commented out.
        idx = lines.index(markers[0])
        self.assertTrue(any(lines[i].lstrip().startswith(";; [") for i in range(idx + 1, idx + 3)), text)
        for entry in entries:
            self.assertTrue(any(line.lstrip().startswith(";; " + entry) for line in lines), text)
            self.assertFalse(any(line.strip() == entry for line in lines), text)

    def test_fetch_then_prune_against_new_base_then_save(self):
        # A newly fetched base rule reverses an existing user [Order] pair.
        # Cleanup must see the new base, never the old one, and the live user
        # file must have that rule commented out in place.
        seen_bases = []
        real_prune = mlox.prune_mlox_rules

        def wrapped_prune(folder, plugins=None, **kwargs):
            text = Path(folder, "mlox_base.txt").read_text(encoding="utf-8")
            seen_bases.append(text)
            return real_prune(folder, plugins, **kwargs)

        bodies = {
            "mlox_base.txt": self.new_base,
            "mlox_user.txt": self.user,
        }
        progress = FakeProgress()
        with mock.patch.object(mlox, "prune_mlox_rules", wrapped_prune):
            ok = mlox.update_rules(progress, self.manager, plugins=self.plugins,
                                   opener=self.opener(bodies))
        self.assertTrue(ok)
        self.assertEqual(len(seen_bases), 1)
        self.assertIn("NEW-BASE-MARKER", seen_bases[0])
        self.assertIn("Other.esp", seen_bases[0])
        self.assertNotIn("OLD-BASE-MARKER", seen_bases[0])
        self.assertEqual(seen_bases[0], (self.folder / "mlox_base.txt").read_text(encoding="utf-8"))

        user = (self.folder / "mlox_user.txt").read_text(encoding="utf-8")
        self.assertIn("USER-RULE", user)
        self._assert_rule_disabled(user, "reverses [Order] in mlox_base.txt", "Mine.esp", "Other.esp")
        self.assertFalse((self.folder / "mlox_user.effective.txt").exists())
        self.assertFalse((self.folder / "mlox_base.effective.txt").exists())
        self.assertTrue((self.folder / "mlox_rules_meta.json").is_file())
        meta = json.loads((self.folder / "mlox_rules_meta.json").read_text(encoding="utf-8"))
        self.assertIn("mlox_base.txt", meta)
        shown = "\n".join(progress.shown())
        self.assertIn("Downloaded the latest mlox rules.", shown)
        self.assertIn("Skipped", shown)
        self.assertTrue(any("reverses [Order] in mlox_base.txt" in line for line in progress.details()))
        # Community downloads and prunes leave no backup of GitHub-sourced files.
        self.assertFalse((self.folder / "mlox_user.txt.bak").exists())
        self.assertFalse((self.folder / "mlox_base.txt.bak").exists())
        self.assertFalse((self.manager / "backups" / "rules").exists())

    def test_cleanup_still_runs_when_nothing_new_is_fetched(self):
        personal = _rule_text(("Order", ["Mine.esp", "Other.esp"]), marker="PERSONAL")
        (self.folder / "mlox_base.txt").write_text(self.new_base, encoding="utf-8")
        (self.folder / mlox.MLOX_MY_RULES).write_text(personal, encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text(_rule_text(("Order", ["Alpha.esp", "Mine.esp"])), encoding="utf-8")

        def _open(request, timeout=None):
            raise urllib.error.HTTPError(mlox._url_of(request), 304, "Not Modified", hdrs=None, fp=None)

        progress = FakeProgress()
        ok = mlox.update_rules(progress, self.manager, plugins=self.plugins, opener=_open)
        self.assertTrue(ok)
        self.assertIn("mlox rules are already up to date.", "\n".join(progress.shown()))
        live = (self.folder / mlox.MLOX_MY_RULES).read_text(encoding="utf-8")
        self.assertIn("PERSONAL", live)
        self._assert_rule_disabled(live, "reverses [Order] in mlox_base.txt", "Mine.esp", "Other.esp")
        self.assertFalse((self.folder / "mlox_my_rules.effective.txt").exists())
        self.assertNotIn("OLD-BASE-MARKER", (self.folder / "mlox_base.txt").read_text(encoding="utf-8"))

    def test_prune_twice_does_not_change_the_result(self):
        (self.folder / "mlox_base.txt").write_text(self.new_base, encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text(self.user, encoding="utf-8")
        first = mlox.prune_mlox_rules(self.folder, self.plugins)
        self.assertTrue(first)
        after_first = (self.folder / "mlox_user.txt").read_bytes()
        self._assert_rule_disabled(after_first.decode("utf-8"), "reverses [Order]", "Mine.esp", "Other.esp")
        marker_count = after_first.decode("utf-8").count(";; MWManager: disabled")
        self.assertEqual(marker_count, 1)
        second = mlox.prune_mlox_rules(self.folder, self.plugins)
        self.assertEqual(second, [])
        after_second = (self.folder / "mlox_user.txt").read_bytes()
        self.assertEqual(after_first, after_second)
        self.assertEqual(after_second.decode("utf-8").count(";; MWManager: disabled"), 1)
        # Already-commented lines were not re-prefixed.
        self.assertNotIn(";; ;;", after_second.decode("utf-8"))

    def test_personal_rules_keep_one_rolling_backup_when_changed(self):
        personal = _rule_text(("Order", ["Mine.esp", "Other.esp"]), marker="PERSONAL")
        (self.folder / "mlox_base.txt").write_text(self.new_base, encoding="utf-8")
        live = self.folder / mlox.MLOX_MY_RULES
        live.write_text(personal, encoding="utf-8")
        original = live.read_bytes()
        mlox.prune_mlox_rules(self.folder, self.plugins)
        bak = Path(mlox.personal_rules_backup_path(self.folder))
        self.assertTrue(bak.is_file())
        self.assertEqual(bak.read_bytes(), original)
        self.assertEqual(bak.name, "mlox_my_rules.txt.bak")
        self.assertEqual(list(self.folder.glob("mlox_my_rules.txt.bak*")), [bak])
        self.assertFalse(list((self.manager / "backups").glob("**/*")) if (self.manager / "backups").exists() else [])
        first_pruned = live.read_bytes()
        # A second run does not rewrite the backup: the live file did not change.
        mlox.prune_mlox_rules(self.folder, self.plugins)
        self.assertEqual(bak.read_bytes(), original)
        self.assertEqual(live.read_bytes(), first_pruned)
        # A later real change overwrites that single backup; no second file appears.
        extra = first_pruned.decode("utf-8") + _rule_text(("Order", ["Mine.esp", "Other.esp"]), marker="SECOND")
        live.write_text(extra, encoding="utf-8")
        before_second_change = live.read_bytes()
        mlox.prune_mlox_rules(self.folder, self.plugins)
        self.assertTrue(bak.is_file())
        self.assertEqual(bak.read_bytes(), before_second_change)
        self.assertEqual(list(self.folder.glob("mlox_my_rules.txt.bak*")), [bak])
        # The new conflict is now disabled in the live file, and the backup is the previous live text.
        self.assertIn("SECOND", live.read_text(encoding="utf-8"))
        self.assertGreater(live.read_text(encoding="utf-8").count(";; MWManager: disabled"), 1)
        self.assertIn("SECOND", bak.read_text(encoding="utf-8"))
        self.assertNotIn(";; MWManager: disabled - reverses [Order] in mlox_base.txt",
                         bak.read_text(encoding="utf-8").split(";; SECOND", 1)[-1] if ";; SECOND" in bak.read_text(encoding="utf-8") else "")

    def test_personal_rules_are_not_overwritten_by_download(self):
        personal = _rule_text(("Order", ["Alpha.esp", "Mine.esp"]), marker="KEEP-ME")
        (self.folder / mlox.MLOX_MY_RULES).write_text(personal, encoding="utf-8")
        bodies = {"mlox_base.txt": self.new_base, "mlox_user.txt": self.user}
        mlox.download_mlox_rules(self.folder, opener=self.opener(bodies))
        self.assertEqual((self.folder / mlox.MLOX_MY_RULES).read_text(encoding="utf-8"), personal)
        self.assertFalse((self.folder / "mlox_base.txt.bak").exists())
        self.assertFalse((self.manager / "backups" / "rules").exists())

    def test_import_personal_rules_once_from_the_game_folder(self):
        game_rules = self.game / "mlox"
        game_rules.mkdir()
        text = _rule_text(("Order", ["Alpha.esp", "Mine.esp"]), marker="FROM-GAME")
        (game_rules / mlox.MLOX_MY_RULES).write_text(text, encoding="utf-8")
        copied = mlox.import_personal_rules(self.game, self.folder)
        self.assertEqual(copied, [mlox.MLOX_MY_RULES])
        self.assertEqual((self.folder / mlox.MLOX_MY_RULES).read_text(encoding="utf-8"), text)
        (game_rules / mlox.MLOX_MY_RULES).write_text("later change", encoding="utf-8")
        self.assertEqual(mlox.import_personal_rules(self.game, self.folder), [])
        self.assertEqual((self.folder / mlox.MLOX_MY_RULES).read_text(encoding="utf-8"), text)

    def test_sort_paths_read_live_files_and_ignore_stale_effective_copies(self):
        (self.folder / mlox.MLOX_MY_RULES).write_text("[Order]\nA.esp\nB.esp\n", encoding="utf-8")
        stale = self.folder / "mlox_my_rules.effective.txt"
        stale.write_text("[Order]\nStale.esp\nOther.esp\n", encoding="utf-8")
        paths = [Path(p).name for p in mlox.mlox_sort_paths(self.manager)]
        self.assertEqual(paths[0], mlox.MLOX_MY_RULES)
        self.assertEqual(paths[-1], "mlox_base.txt")
        self.assertNotIn("mlox_my_rules.effective.txt", paths)
        # Prune deletes the leftover effective copy.
        mlox.prune_mlox_rules(self.folder, self.plugins)
        self.assertFalse(stale.exists())
        paths = [Path(p).name for p in mlox.mlox_sort_paths(self.manager)]
        self.assertEqual(paths[0], mlox.MLOX_MY_RULES)

    def test_uninstalled_and_duplicate_rules_are_pruned(self):
        base = _rule_text(("Order", ["Mine.esp", "Other.esp"]), marker="BASE")
        user = _rule_text(("Order", ["Mine.esp", "Other.esp"]), ("Order", ["Ghost.esp", "Missing.esp"]))
        (self.folder / "mlox_base.txt").write_text(base, encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text(user, encoding="utf-8")
        dropped = mlox.prune_mlox_rules(self.folder, ["Mine.esp", "Other.esp"])
        reasons = " ".join(reason for _src, reason in dropped)
        self.assertIn("duplicates", reasons)
        self.assertIn("not installed", reasons)
        live = (self.folder / "mlox_user.txt").read_text(encoding="utf-8")
        self._assert_rule_disabled(live, "duplicates", "Mine.esp", "Other.esp")
        self.assertIn(";; Ghost.esp", live)
        self.assertIn(";; Missing.esp", live)
        self.assertFalse(any(line.strip() == "Ghost.esp" for line in live.splitlines()))
        self.assertFalse((self.folder / "mlox_user.effective.txt").exists())

    def test_offline_update_is_plain_and_does_not_stop(self):
        def _open(request, timeout=None):
            raise OSError("offline")

        progress = FakeProgress()
        ok = mlox.update_rules(progress, self.manager, plugins=self.plugins, opener=_open)
        self.assertTrue(ok)
        shown = "\n".join(progress.shown())
        self.assertIn("offline", shown)
        self.assertNotIn("OSError", shown)
        self.assertTrue(progress.details())
        self.assertIn("OLD-BASE-MARKER", (self.folder / "mlox_base.txt").read_text(encoding="utf-8"))

    def test_sources_have_no_absolute_paths(self):
        drive = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\(?![dDsSnNtTwWbBaAzZrfvxuk0-9])")
        hits = []
        for path in (ROOT / "mwmanager_mlox.py", Path(__file__),
                     ROOT / "tests" / "test_mlox_rules_download.py"):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if drive.search(line):
                    hits.append(f"{path.name}:{i}:{line.strip()}")
        self.assertEqual(hits, [], "drive-letter literals:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
