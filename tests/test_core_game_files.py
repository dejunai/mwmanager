"""Launch-time check of the six shipped ESM and BSA files. Temporary folders only."""
import hashlib
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

import mwmanager_core_game_files as core  # noqa: E402
import py_compat  # noqa: E402

ABS_PATH_RE = re.compile(r'(?i)\b[A-Z]:\\')


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        py_compat.write_text(path, data, encoding="utf-8", newline="\n")
    else:
        path.write_bytes(data)
    return path


def digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = self.root / "MWManager"
        self.game = self.root / "Morrowind"
        self.data = self.game / "Data Files"
        self.manager.mkdir()
        self.data.mkdir(parents=True)
        self.payloads = {name: f"{name}-clean".encode("ascii") for name in core.CORE_GAME_FILES}
        self.known = {name: (digest(payload),) for name, payload in self.payloads.items()}
        for name, payload in self.payloads.items():
            write(self.data / name, payload)
        self.hashes = []

    def md(self):
        return str(self.manager)

    def hasher(self, path):
        self.hashes.append(Path(path).name)
        return core.sha256_file(path)

    def check(self, **kwargs):
        kwargs.setdefault("manager_dir", self.md())
        kwargs.setdefault("known", self.known)
        kwargs.setdefault("hasher", self.hasher)
        kwargs.setdefault("cli", False)
        kwargs.setdefault("notify", None)
        return core.ensure_core_game_files_for_play(self.game, **kwargs)


class CoreGameFileCheck(Repo):
    def test_matching_files_stay_silent_and_record_a_baseline(self):
        result = self.check()
        self.assertFalse(result["block"])
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["message"])
        self.assertEqual(len(self.hashes), 6)
        state = json.loads(Path(core.state_path(self.md())).read_text(encoding="utf-8"))
        self.assertTrue(all(entry["baseline"] for entry in state["files"].values()))
        self.assertFalse(Path(core.detail_log_path(self.md())).exists())

    def test_unchanged_size_and_mtime_reuse_the_cached_hash(self):
        self.check()
        self.hashes.clear()
        result = self.check()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.hashes, [])

    def test_size_or_mtime_change_rehashes(self):
        self.check()
        self.hashes.clear()
        path = self.data / "Morrowind.esm"
        os.utime(path, (path.stat().st_mtime + 10, path.stat().st_mtime + 10))
        result = self.check()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.hashes, ["Morrowind.esm"])

    def test_altered_file_warns_with_expected_and_actual_in_the_log(self):
        write(self.data / "Tribunal.esm", b"tampered-tribunal")
        prints = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: prints.append(" ".join(map(str, a)))):
            result = self.check(cli=True)
        self.assertFalse(result["block"])
        self.assertEqual(result["status"], "issues")
        self.assertIn("Tribunal.esm", result["message"])
        self.assertIn("altered", result["message"])
        self.assertTrue(any("Tribunal.esm" in line for line in prints))
        log = Path(core.detail_log_path(self.md())).read_text(encoding="utf-8")
        self.assertIn("Tribunal.esm", log)
        self.assertIn(digest(b"tampered-tribunal"), log)
        self.assertIn(self.known["Tribunal.esm"][0], log)
        self.assertIn("ALTERED", log)

    def test_missing_file_warns_and_still_allows_play(self):
        (self.data / "Bloodmoon.bsa").unlink()
        result = self.check()
        self.assertFalse(result["block"])
        self.assertEqual(result["status"], "issues")
        self.assertIn("Bloodmoon.bsa", result["message"])
        self.assertIn("missing", result["message"])
        log = Path(core.detail_log_path(self.md())).read_text(encoding="utf-8")
        self.assertIn("MISSING", log)
        self.assertIn("Bloodmoon.bsa", log)

    def test_dont_warn_again_is_per_hash_not_global(self):
        write(self.data / "Morrowind.bsa", b"bad-bsa-v1")
        write(self.data / "Tribunal.bsa", b"bad-other")
        first = self.check(notify=lambda title, message: "mute")
        self.assertEqual(first["choice"], "mute")
        self.hashes.clear()
        silent = self.check(notify=lambda title, message: self.fail("should stay silent"))
        self.assertEqual(silent["status"], "muted")
        self.assertIsNone(silent["message"])

        write(self.data / "Morrowind.bsa", b"bad-bsa-v2")
        os.utime(self.data / "Morrowind.bsa", None)
        warned = []
        again = self.check(notify=lambda title, message: warned.append(message) or "continue")
        self.assertEqual(again["status"], "issues")
        self.assertEqual(len(again["visible"]), 1)
        self.assertEqual(again["visible"][0]["name"], "Morrowind.bsa")
        self.assertTrue(warned)
        # Tribunal.bsa is still the muted hash and must not appear in the new warning.
        self.assertNotIn("Tribunal.bsa", warned[0])

    def test_muted_missing_warns_again_when_a_different_state_appears(self):
        (self.data / "Morrowind.esm").unlink()
        self.check(notify=lambda title, message: "mute")
        silent = self.check(notify=lambda title, message: self.fail("missing is still muted"))
        self.assertEqual(silent["status"], "muted")

        write(self.data / "Morrowind.esm", b"still-wrong")
        warned = []
        result = self.check(notify=lambda title, message: warned.append(True) or "continue")
        self.assertEqual(result["status"], "issues")
        self.assertTrue(warned)

        write(self.data / "Morrowind.esm", self.payloads["Morrowind.esm"])
        ok = self.check(notify=lambda title, message: self.fail("restored file should be silent"))
        self.assertEqual(ok["status"], "ok")

    def test_ini_flag_skips_the_check(self):
        write(self.data / "Morrowind.esm", b"changed")
        core.write_ini_flags({"check_core_game_files": False}, self.md())
        result = self.check()
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["block"])
        self.assertEqual(self.hashes, [])

    def test_ini_flag_defaults_true(self):
        self.assertTrue(core.ini_flag("check_core_game_files", self.md()))

    def test_sha256sums_in_outputs_is_accepted_as_known_good(self):
        payload = b"user-baseline-esm"
        write(self.data / "Morrowind.esm", payload)
        write(self.manager / "outputs" / "SHA256SUMS.txt",
              f"{digest(payload)}  Data Files/Morrowind.esm\n")
        # Official known hashes still describe the original clean bytes; the sums file is the user baseline.
        result = self.check()
        self.assertEqual(result["status"], "ok", result)

    def test_sources_have_no_absolute_paths(self):
        text = (ROOT / "mwmanager_core_game_files.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, ABS_PATH_RE)
        self.assertNotIn("/" + "home/", text)
        self.assertNotIn("Program Files", text)
        self.assertNotIn("GOG" + " Galaxy", text)


if __name__ == "__main__":
    unittest.main()
