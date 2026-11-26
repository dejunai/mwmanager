"""atomic_bytes retries a replace that Windows refuses while another reader has the file open (the 3.9 suite's
test_blocked_rescan_does_not_stop_the_tidy_of_other_packages failed now and then with WinError 5 on the batch
manifest). Temporary folders only."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workflow_safety as WS  # noqa: E402


class ReplaceRetry(unittest.TestCase):
    def test_a_refused_replace_is_retried_until_it_goes_through(self):
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")
            return real(src, dst)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "manifest.json"
            target.write_text("old")
            with mock.patch.object(WS.os, "replace", flaky), mock.patch.object(WS.os, "name", "nt"):
                WS.atomic_json(target, {"a": 1})
            self.assertEqual(target.read_text(encoding="utf-8").strip(), '{\n  "a": 1\n}')
            self.assertEqual(len(calls), 3)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["manifest.json"])   # no .tmp left

    def test_a_replace_that_keeps_failing_still_raises_and_leaves_no_temporary(self):
        def refuse(_src, _dst):
            raise PermissionError(5, "Access is denied")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "manifest.json"
            with mock.patch.object(WS.os, "replace", refuse), mock.patch.object(WS.os, "name", "nt"), \
                    mock.patch("time.sleep"):
                with self.assertRaises(PermissionError):
                    WS.atomic_bytes(target, b"x")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_elsewhere_a_refused_replace_is_not_retried(self):
        calls = []

        def refuse(_src, _dst):
            calls.append(1)
            raise PermissionError(13, "Permission denied")
        with mock.patch.object(WS.os, "replace", refuse), mock.patch.object(WS.os, "name", "posix"):
            with self.assertRaises(PermissionError):
                WS._replace("a.tmp", "a")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
