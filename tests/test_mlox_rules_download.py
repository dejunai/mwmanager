"""Download or keep mlox rules during preflight. Temporary folders only; the network is mocked."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mwmanager_mlox as mlox  # noqa: E402
import preinstall  # noqa: E402


def _rule_bytes(name, body="[Order]\nAlpha.esp\nBeta.esp\n"):
    return f";; {name}\n{body}".encode("utf-8")


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def read(self, size=-1):
        if size is None or size < 0:
            return self._data
        return self._data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeProgress:
    def __init__(self, choice=mlox.CHOICE_OWN):
        self.choice = choice
        self.lines = []
        self.prompts = []

    def __call__(self, message):
        self.lines.append(str(message))

    def ask_choice(self, title, message, choices, cancel):
        self.prompts.append((title, message, list(choices), cancel))
        return self.choice

    def shown(self):
        return [line for line in self.lines if not line.startswith("MWMANAGER-DETAIL:")]

    def details(self):
        return [line for line in self.lines if line.startswith("MWMANAGER-DETAIL:")]


class MloxRulesDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = self.root / "MWManager"
        self.game = self.root / "Morrowind"
        self.folder = self.manager / "mlox"
        self.backup = self.manager / "backups" / "rules"
        self.manager.mkdir()
        self.game.mkdir()
        self.bodies = {name: _rule_bytes(name) for name in mlox.MLOX_RULE_NAMES}

    def opener(self, mapping=None, errors=None):
        mapping = mapping if mapping is not None else self.bodies
        errors = errors or {}

        def _open(request, timeout=None):
            url = mlox._url_of(request)
            for name, content in mapping.items():
                if url == mlox.MLOX_RULE_URLS[name] or url.endswith(name):
                    if name in errors:
                        raise errors[name]
                    return FakeResponse(content)
            raise FileNotFoundError(url)

        return _open

    def test_urls_point_at_the_community_rules_repo(self):
        self.assertEqual(mlox.MLOX_RULE_URLS["mlox_base.txt"],
                         "https://raw.githubusercontent.com/DanaePlays/mlox-rules/main/mlox_base.txt")
        self.assertEqual(mlox.MLOX_RULE_URLS["mlox_user.txt"],
                         "https://raw.githubusercontent.com/DanaePlays/mlox-rules/main/mlox_user.txt")
        self.assertEqual(mlox.MLOX_RULES_REPO, "https://github.com/DanaePlays/mlox-rules")

    def test_rules_folder_is_relative_to_the_configured_manager_directory(self):
        folder = mlox.mlox_rules_folder(self.manager)
        self.assertEqual(Path(folder), self.folder)
        self.assertEqual(Path(folder), Path(self.manager) / "mlox")

    def test_download_writes_both_files(self):
        result = mlox.download_mlox_rules(self.folder, opener=self.opener())
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(sorted(result["written"]), ["mlox_base.txt", "mlox_user.txt"])
        self.assertEqual((self.folder / "mlox_base.txt").read_bytes(), self.bodies["mlox_base.txt"])
        self.assertEqual((self.folder / "mlox_user.txt").read_bytes(), self.bodies["mlox_user.txt"])

    def test_download_replaces_community_files_without_a_backup(self):
        self.folder.mkdir()
        (self.folder / "mlox_base.txt").write_text("old base [Order]\nMine.esp\n", encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text("old user [Order]\nMine.esp\n", encoding="utf-8")
        result = mlox.download_mlox_rules(self.folder, opener=self.opener())
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual((self.folder / "mlox_base.txt").read_bytes(), self.bodies["mlox_base.txt"])
        self.assertEqual((self.folder / "mlox_user.txt").read_bytes(), self.bodies["mlox_user.txt"])
        self.assertFalse((self.folder / "mlox_base.txt.bak").exists())
        self.assertFalse((self.folder / "mlox_user.txt.bak").exists())
        self.assertFalse(self.backup.exists())
        self.assertNotIn("backed_up", result)

    def test_failed_download_leaves_existing_files_in_place(self):
        self.folder.mkdir()
        (self.folder / "mlox_base.txt").write_text("keep me [Order]\nMine.esp\n", encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text("keep me too [Order]\nMine.esp\n", encoding="utf-8")
        result = mlox.download_mlox_rules(
            self.folder, opener=self.opener(errors={"mlox_base.txt": OSError("offline")}))
        self.assertFalse(result["ok"])
        self.assertIn("offline", result["errors"][0])
        self.assertIn("mlox_base.txt", result["errors"][0])
        self.assertEqual((self.folder / "mlox_base.txt").read_text(encoding="utf-8"),
                         "keep me [Order]\nMine.esp\n")
        self.assertEqual((self.folder / "mlox_user.txt").read_text(encoding="utf-8"),
                         "keep me too [Order]\nMine.esp\n")
        self.assertFalse(list(self.folder.glob("*.download")))

    def test_invalid_content_is_rejected(self):
        bad = {"mlox_base.txt": b"not a rules file", "mlox_user.txt": _rule_bytes("mlox_user.txt")}
        result = mlox.download_mlox_rules(self.folder, opener=self.opener(mapping=bad))
        self.assertFalse(result["ok"])
        self.assertFalse(list(self.folder.glob("mlox_*.txt")))

    def test_offer_download_writes_rules_and_uses_plain_status(self):
        progress = FakeProgress(choice=mlox.CHOICE_DOWNLOAD)
        self.assertTrue(mlox.offer_mlox_rules(progress, self.game, self.manager,
                                              first_run=True, opener=self.opener()))
        self.assertEqual(progress.prompts[0][0], mlox.CHOICE_TITLE)
        labels = [label for label, _value in progress.prompts[0][2]]
        self.assertEqual(labels, [mlox.DOWNLOAD_LABEL, mlox.KEEP_OWN_LABEL])
        self.assertEqual(mlox.DOWNLOAD_LABEL,
                         "Download the latest mlox rules (from the mlox project on GitHub)")
        self.assertEqual(mlox.KEEP_OWN_LABEL, "Keep using my own mlox rules")
        self.assertIn("DanaePlays/mlox-rules", progress.prompts[0][1])
        self.assertIn("Preflight: downloaded the latest mlox rules.", progress.shown())
        self.assertTrue((self.folder / "mlox_base.txt").is_file())
        self.assertTrue((self.folder / "mlox_user.txt").is_file())
        self.assertFalse(progress.details())

    def test_offer_keep_own_imports_game_rules_and_does_not_hit_the_network(self):
        game_rules = self.game / "mlox"
        game_rules.mkdir()
        (game_rules / "mlox_base.txt").write_text("[Order]\nMine.esp\nOther.esp\n", encoding="utf-8")
        (game_rules / "mlox_user.txt").write_text("[Order]\nMine.esp\n", encoding="utf-8")
        progress = FakeProgress(choice=mlox.CHOICE_OWN)
        opener = mock.Mock(side_effect=AssertionError("network should not be used"))
        self.assertTrue(mlox.offer_mlox_rules(progress, self.game, self.manager,
                                              first_run=True, opener=opener))
        opener.assert_not_called()
        self.assertIn("Preflight: keeping your existing mlox rules.", progress.shown())
        self.assertEqual((self.folder / "mlox_base.txt").read_text(encoding="utf-8"),
                         "[Order]\nMine.esp\nOther.esp\n")

    def test_failed_offer_is_plain_and_details_go_to_the_log(self):
        self.folder.mkdir()
        (self.folder / "mlox_base.txt").write_text("keep [Order]\nMine.esp\n", encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text("keep [Order]\nMine.esp\n", encoding="utf-8")
        progress = FakeProgress(choice=mlox.CHOICE_DOWNLOAD)
        self.assertTrue(mlox.offer_mlox_rules(
            progress, self.game, self.manager, first_run=True,
            opener=self.opener(errors={"mlox_user.txt": OSError("timed out")})))
        shown = "\n".join(progress.shown())
        self.assertIn("could not download the latest mlox rules; existing rules were left as they are.", shown)
        self.assertNotIn("timed out", shown)
        self.assertTrue(progress.details())
        self.assertIn("timed out", progress.details()[0])
        self.assertEqual((self.folder / "mlox_base.txt").read_text(encoding="utf-8"),
                         "keep [Order]\nMine.esp\n")

    def test_later_preflight_does_not_reprompt_when_rules_already_exist(self):
        self.folder.mkdir()
        (self.folder / "mlox_base.txt").write_text("[Order]\nA.esp\n", encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text("[Order]\nA.esp\n", encoding="utf-8")
        progress = FakeProgress(choice=mlox.CHOICE_DOWNLOAD)
        opener = mock.Mock(side_effect=AssertionError("network should not be used"))
        self.assertTrue(mlox.offer_mlox_rules(progress, self.game, self.manager,
                                              first_run=False, opener=opener))
        self.assertEqual(progress.prompts, [])
        self.assertNotIn("Preflight: using the mlox rules already in MWManager.", progress.shown())
        self.assertIn("MWMANAGER-DETAIL: Preflight: using the mlox rules already in MWManager.", progress.details())
        opener.assert_not_called()

    def test_later_preflight_does_not_ask_when_rules_are_missing(self):
        progress = FakeProgress(choice=mlox.CHOICE_DOWNLOAD)
        opener = mock.Mock(side_effect=AssertionError("network should not be used"))
        self.assertTrue(mlox.offer_mlox_rules(progress, self.game, self.manager,
                                              first_run=False, opener=opener))
        self.assertEqual(progress.prompts, [])
        self.assertFalse((self.folder / "mlox_base.txt").exists())
        opener.assert_not_called()

    def test_preflight_calls_the_mlox_choice(self):
        source = Path(preinstall.__file__).read_text(encoding="utf-8")
        self.assertIs(preinstall.offer_mlox_rules, mlox.offer_mlox_rules)
        self.assertIn("offer_mlox_rules(progress, game_dir, manager_dir, first_run=first_run)", source)

    def test_sources_have_no_absolute_paths(self):
        # The same drive-letter lint as tools/steam-upstream/tests/test_paths.py.
        import re
        drive = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\(?![dDsSnNtTwWbBaAzZrfvxuk0-9])")
        hits = []
        for path in (ROOT / "mwmanager_mlox.py", Path(__file__)):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if drive.search(line):
                    hits.append(f"{path.name}:{i}:{line.strip()}")
        self.assertEqual(hits, [], "drive-letter literals:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
