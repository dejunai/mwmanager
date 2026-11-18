"""Major masters deploy in the configured load order: a later master's loose file replaces an earlier one's."""
import struct
import tempfile
import unittest
from pathlib import Path

from major_master_deploy import _package_roots


class DeployOrder(unittest.TestCase):
    def test_packages_link_in_load_order(self):
        configured = ["Patch for Purists.esm", "Tamriel_Data.esm", "TR_Mainland.esm"]
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            for name in reversed(configured):
                package = staging / Path(name).stem
                package.mkdir()
                (package / name).write_bytes(struct.pack("<4sIII", b"TES3", 0, 0, 0))
            order = [names[0] for _package, names in _package_roots(staging, configured)]
        self.assertEqual(order, configured)      # Patch for Purists first, so it loses file conflicts


if __name__ == "__main__":
    unittest.main()
