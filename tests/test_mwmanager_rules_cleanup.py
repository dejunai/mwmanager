"""Cleanup of mwmanager_rules.txt during --update-rules. Temp dirs; network mocked.

No absolute paths.
"""
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mwmanager_mlox as mlox  # noqa: E402


DISABLE_RE = re.compile(r"^;; MWManager: disabled - (.+) \((\d{4}-\d{2}-\d{2})\)$")

CONTEXT = (
    "[Context]\n"
    "; Written by --sort on 2026-11-24 19:09. For reading only: every line here is\n"
    "; a comment, and --sort rewrites this section each time. Edit the rules below it instead.\n"
    ";\n"
    "; 0 rule(s) found:\n"
    ";\n"
    ";   (none)\n"
    "; ---- end of [Context] ----\n"
    "\n"
)

HEADER = (
    "; mwmanager_rules.txt: your load order rules for mwmanager.py --sort\n"
    ";\n"
)

CONFLICT_BLOCK = (
    "; ---- added by --cell-conflicts 2026-11-25 12:00 ----\n"
    "\n"
    "[Order]\n"
    "Ghost.esp\n"
    "Missing.esp\n"
    "; keep Ghost.esp winning where they conflict\n"
)


def _rules_text(*, extra="", conflict=True):
    return CONTEXT + HEADER + extra + ("\n" + CONFLICT_BLOCK if conflict else "")


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


class ManagerRulesCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = self.root / "MWManager"
        self.folder = self.manager / "mlox"
        self.folder.mkdir(parents=True)
        self.plugins = ["Mine.esp", "Other.esp", "Alpha.esp"]
        self.base = "[Order]\nOther.esp\nMine.esp\n"
        (self.folder / "mlox_base.txt").write_text(self.base, encoding="utf-8")
        (self.folder / "mlox_user.txt").write_text("[Order]\nAlpha.esp\nMine.esp\n", encoding="utf-8")
        self.rules = self.manager / mlox.MANAGER_RULES_FILE
        # Override (Mine before Other) is the reverse of base and must stay.
        # Exact duplicate of base, a repeated self-duplicate, and an uninstalled-only rule
        # should be commented. The cell-conflicts Ghost/Missing pair must stay live.
        extra = (
            "[Order]\nMine.esp\nOther.esp\n\n"
            "[Order]\nOther.esp\nMine.esp\n\n"
            "[Order]\nAlpha.esp\nMine.esp\n\n"
            "[Order]\nAlpha.esp\nMine.esp\n\n"
            "[Order]\nVanished.esp\nGone.esp\n"
        )
        self.original = _rules_text(extra=extra, conflict=True)
        self.rules.write_text(self.original, encoding="utf-8")

    def opener(self, mapping, headers=None):
        headers = headers or {"ETag": '"etag"'}

        def _open(request, timeout=None):
            url = mlox._url_of(request)
            for name, content in mapping.items():
                if url == mlox.MLOX_RULE_URLS[name] or str(url).endswith(name):
                    data = content if isinstance(content, bytes) else content.encode("utf-8")
                    return FakeResponse(data, headers=headers)
            raise FileNotFoundError(url)

        return _open

    def _disable_comments(self, text):
        return [line for line in text.splitlines() if line.startswith(";; MWManager: disabled")]

    def test_does_not_create_the_file_when_missing(self):
        self.rules.unlink()
        dropped = mlox.prune_manager_rules(self.manager, self.plugins)
        self.assertEqual(dropped, [])
        self.assertFalse(self.rules.exists())
        self.assertFalse(Path(mlox.manager_rules_backup_path(self.manager)).exists())

    def test_comments_uninstalled_self_duplicates_and_base_duplicates_only(self):
        dropped = mlox.prune_manager_rules(self.manager, self.plugins)
        reasons = " ".join(reason for _src, reason in dropped)
        self.assertIn("not installed", reasons)
        self.assertIn("duplicate of an earlier rule", reasons)
        self.assertIn("duplicates a rule in mlox_base.txt", reasons)
        live = self.rules.read_text(encoding="utf-8")
        # Override of base remains active.
        self.assertRegex(live, r"(?m)^\[Order\]\r?\nMine\.esp\r?\nOther\.esp\r?\n")
        # Exact duplicate of base, second Alpha/Mine, and uninstalled pair are commented.
        comments = self._disable_comments(live)
        self.assertEqual(len(comments), 3)
        for line in comments:
            self.assertIsNotNone(DISABLE_RE.match(line), line)
        self.assertIn(";; Other.esp", live)
        self.assertIn(";; Vanished.esp", live)
        self.assertIn(";; Gone.esp", live)
        # One Alpha/Mine stays live (the first); the second is commented.
        live_alpha = len(re.findall(r"(?m)^Alpha\.esp$", live))
        commented_alpha = len(re.findall(r"(?m)^;; Alpha\.esp$", live))
        self.assertEqual(live_alpha, 1)
        self.assertEqual(commented_alpha, 1)
        # Context and cell-conflicts block are unchanged (Ghost/Missing still live).
        self.assertIn("; ---- end of [Context] ----", live)
        self.assertIn("; Written by --sort on 2026-11-24 19:09.", live)
        self.assertIn("; ---- added by --cell-conflicts 2026-11-25 12:00 ----", live)
        self.assertRegex(live, r"(?m)^\[Order\]\r?\nGhost\.esp\r?\nMissing\.esp\r?\n; keep Ghost\.esp winning")
        self.assertNotIn(";; Ghost.esp", live)

    def test_second_run_is_idempotent_and_does_not_rewrite_the_backup(self):
        mlox.prune_manager_rules(self.manager, self.plugins)
        bak = Path(mlox.manager_rules_backup_path(self.manager))
        self.assertTrue(bak.is_file())
        self.assertEqual(bak.read_text(encoding="utf-8"), self.original)
        after_first = self.rules.read_bytes()
        bak_bytes = bak.read_bytes()
        bak_mtime = bak.stat().st_mtime_ns
        dropped = mlox.prune_manager_rules(self.manager, self.plugins)
        self.assertEqual(dropped, [])
        self.assertEqual(self.rules.read_bytes(), after_first)
        self.assertEqual(bak.read_bytes(), bak_bytes)
        self.assertEqual(bak.stat().st_mtime_ns, bak_mtime)
        self.assertEqual(list(self.manager.glob("mwmanager_rules.txt.bak*")), [bak])
        self.assertNotIn(";; ;;", self.rules.read_text(encoding="utf-8"))

    def test_update_rules_prunes_existing_file_against_the_new_base(self):
        # Current base does not mention Alpha. After fetch, base gains Alpha-before-Mine,
        # which exactly matches a manager rule that must then be commented.
        (self.folder / "mlox_base.txt").write_text("[Order]\nOther.esp\nMine.esp\n", encoding="utf-8")
        extra = "[Order]\nMine.esp\nOther.esp\n\n[Order]\nAlpha.esp\nMine.esp\n"
        self.original = _rules_text(extra=extra, conflict=True)
        self.rules.write_text(self.original, encoding="utf-8")
        new_base = "[Order]\nOther.esp\nMine.esp\n\n[Order]\nAlpha.esp\nMine.esp\n"
        bodies = {"mlox_base.txt": new_base, "mlox_user.txt": "[Order]\nAlpha.esp\nMine.esp\n"}
        progress = FakeProgress()

        def _open(request, timeout=None):
            # 304 for unchanged user, 200 for new base
            url = mlox._url_of(request)
            if str(url).endswith("mlox_user.txt"):
                raise urllib.error.HTTPError(url, 304, "Not Modified", hdrs=None, fp=None)
            return self.opener({"mlox_base.txt": new_base})(request, timeout)

        ok = mlox.update_rules(progress, self.manager, plugins=self.plugins, opener=_open)
        self.assertTrue(ok)
        live = self.rules.read_text(encoding="utf-8")
        self.assertRegex(live, r"(?m)^\[Order\]\r?\nMine\.esp\r?\nOther\.esp\r?\n")
        self.assertIn(";; Alpha.esp", live)
        self.assertTrue(any("duplicates a rule in mlox_base.txt" in line for line in progress.details()))
        self.assertTrue(any("mwmanager_rules.txt" in line and "Skipped" in line for line in progress.shown()))
        # Still did not create a second rules file or touch the conflict block.
        self.assertRegex(live, r"(?m)^Ghost\.esp$")

    def test_update_rules_does_not_create_manager_rules(self):
        self.rules.unlink()
        def _open(request, timeout=None):
            raise urllib.error.HTTPError(mlox._url_of(request), 304, "Not Modified", hdrs=None, fp=None)
        progress = FakeProgress()
        self.assertTrue(mlox.update_rules(progress, self.manager, plugins=self.plugins, opener=_open))
        self.assertFalse(self.rules.exists())
        self.assertFalse(Path(mlox.manager_rules_backup_path(self.manager)).exists())

    def test_sources_have_no_absolute_paths(self):
        drive = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\(?![dDsSnNtTwWbBaAzZrfvxuk0-9])")
        hits = []
        for path in (ROOT / "mwmanager_mlox.py", Path(__file__)):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if drive.search(line):
                    hits.append(f"{path.name}:{i}:{line.strip()}")
        self.assertEqual(hits, [], "drive-letter literals:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
