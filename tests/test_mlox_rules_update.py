"""Update rules: fetch only if newer, then prune against the new files, then save.

Temporary folders only; the network is mocked. No absolute paths.
"""
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mwmanager_mlox as mlox  # noqa: E402


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

    def test_fetch_then_prune_against_new_base_then_save(self):
        # A newly fetched base rule reverses an existing user [Order] pair.
        # Cleanup must see the new base, never the old one, and the saved
        # effective copy must drop the conflicting user rule.
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

        # Original user file is unchanged; the effective copy dropped the conflict.
        self.assertIn("Mine.esp", (self.folder / "mlox_user.txt").read_text(encoding="utf-8"))
        self.assertIn("USER-RULE", (self.folder / "mlox_user.txt").read_text(encoding="utf-8"))
        effective = (self.folder / "mlox_user.effective.txt").read_text(encoding="utf-8")
        self.assertNotIn("Mine.esp", effective)
        self.assertTrue((self.folder / "mlox_rules_meta.json").is_file())
        meta = json.loads((self.folder / "mlox_rules_meta.json").read_text(encoding="utf-8"))
        self.assertIn("mlox_base.txt", meta)
        shown = "\n".join(progress.shown())
        self.assertIn("Downloaded the latest mlox rules.", shown)
        self.assertIn("Skipped", shown)
        self.assertTrue(any("conflicts with mlox_base.txt" in line for line in progress.details()))

    def test_cleanup_still_runs_when_nothing_new_is_fetched(self):
        personal = _rule_text(("Order", ["Mine.esp", "Other.esp"]), marker="PERSONAL")
        # Current base already conflicts with the personal rule.
        (self.folder / "mlox_base.txt").write_text(self.new_base, encoding="utf-8")
        (self.folder / mlox.MLOX_MY_RULES).write_text(personal, encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text(_rule_text(("Order", ["Alpha.esp", "Mine.esp"])), encoding="utf-8")

        def _open(request, timeout=None):
            raise urllib.error.HTTPError(mlox._url_of(request), 304, "Not Modified", hdrs=None, fp=None)

        progress = FakeProgress()
        ok = mlox.update_rules(progress, self.manager, plugins=self.plugins, opener=_open)
        self.assertTrue(ok)
        self.assertIn("mlox rules are already up to date.", "\n".join(progress.shown()))
        # Cleanup ran against the current (already new) base and pruned personal rules.
        self.assertIn("PERSONAL", (self.folder / mlox.MLOX_MY_RULES).read_text(encoding="utf-8"))
        effective = (self.folder / "mlox_my_rules.effective.txt").read_text(encoding="utf-8")
        self.assertNotIn("Mine.esp", effective)
        self.assertNotIn("OLD-BASE-MARKER", (self.folder / "mlox_base.txt").read_text(encoding="utf-8"))

    def test_personal_rules_are_not_overwritten_by_download(self):
        personal = _rule_text(("Order", ["Alpha.esp", "Mine.esp"]), marker="KEEP-ME")
        (self.folder / mlox.MLOX_MY_RULES).write_text(personal, encoding="utf-8")
        bodies = {"mlox_base.txt": self.new_base, "mlox_user.txt": self.user}
        mlox.download_mlox_rules(self.folder, opener=self.opener(bodies),
                                 backup_dir=self.manager / "backups" / "rules")
        self.assertEqual((self.folder / mlox.MLOX_MY_RULES).read_text(encoding="utf-8"), personal)

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

    def test_sort_paths_put_personal_rules_first(self):
        (self.folder / mlox.MLOX_MY_RULES).write_text("[Order]\nA.esp\nB.esp\n", encoding="utf-8")
        paths = [Path(p).name for p in mlox.mlox_sort_paths(self.manager)]
        self.assertEqual(paths[0], mlox.MLOX_MY_RULES)
        self.assertEqual(paths[-1], "mlox_base.txt")
        (self.folder / "mlox_my_rules.effective.txt").write_text("[Order]\nA.esp\nB.esp\n", encoding="utf-8")
        paths = [Path(p).name for p in mlox.mlox_sort_paths(self.manager)]
        self.assertEqual(paths[0], "mlox_my_rules.effective.txt")

    def test_uninstalled_and_duplicate_rules_are_pruned(self):
        base = _rule_text(("Order", ["Mine.esp", "Other.esp"]), marker="BASE")
        user = _rule_text(("Order", ["Mine.esp", "Other.esp"]), ("Order", ["Ghost.esp", "Missing.esp"]))
        (self.folder / "mlox_base.txt").write_text(base, encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text(user, encoding="utf-8")
        dropped = mlox.prune_mlox_rules(self.folder, ["Mine.esp", "Other.esp"])
        reasons = " ".join(reason for _src, reason in dropped)
        self.assertIn("duplicates", reasons)
        self.assertIn("not installed", reasons)
        effective = (self.folder / "mlox_user.effective.txt").read_text(encoding="utf-8")
        self.assertNotIn("Ghost.esp", effective)
        self.assertIn("Mine.esp", (self.folder / "mlox_user.txt").read_text(encoding="utf-8"))

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
        import re
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
