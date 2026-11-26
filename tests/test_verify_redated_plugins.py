"""CR-25 stress run round 3: a sort after a major-master deployment sets the plugins' dates (the load order)
again; the deployment journaled the hardlinked masters by size and date, so verify --verify-against deployed
reported all of them as changed though not a byte differed."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_journal as J  # noqa: E402
import py_compat  # noqa: E402


class RedatedPlugin(unittest.TestCase):
    def setUp(self):
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _linked(self, name, data=b"TES3 plugin"):
        staged = self.dir / ("staged_" + name)
        staged.write_bytes(data)
        deployed = self.dir / name
        os.link(staged, deployed)
        return staged, deployed

    def test_new_date_on_a_linked_plugin_is_not_a_change(self):
        _staged, deployed = self._linked("Patch for Purists.esm")
        want = J.stat_marker(deployed.stat())
        os.utime(deployed, (946886400, 946886400))
        self.assertFalse(J.matches_after(deployed, want))
        self.assertTrue(J.redated_plugin(deployed, want))

    def test_a_different_size_is_a_change(self):
        _staged, deployed = self._linked("Wares-base.esm")
        want = J.stat_marker(deployed.stat())
        deployed.write_bytes(b"TES3 plugin, edited")
        self.assertFalse(J.redated_plugin(deployed, want))

    def test_a_plugin_no_longer_linked_is_a_change(self):
        staged, deployed = self._linked("Wares_Legacy.esp")
        want = J.stat_marker(deployed.stat())
        deployed.unlink()
        deployed.write_bytes(staged.read_bytes())
        os.utime(deployed, (946886400, 946886400))
        self.assertFalse(J.redated_plugin(deployed, want))

    def test_other_files_keep_the_strict_check(self):
        _staged, deployed = self._linked("tex.dds", b"DDS data")
        want = J.stat_marker(deployed.stat())
        os.utime(deployed, (946886400, 946886400))
        self.assertFalse(J.redated_plugin(deployed, want))

    def test_sha_markers_are_never_redated(self):
        _staged, deployed = self._linked("A.esp")
        self.assertFalse(J.redated_plugin(deployed, "0" * 64))


if __name__ == "__main__":
    unittest.main()