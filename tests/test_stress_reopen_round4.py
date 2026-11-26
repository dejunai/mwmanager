"""CR-25 stress run, round 4: 'Reopen major-master options' after the plugin batch. The masters' deployment could
not come out cleanly (re-dated plugins counted as edited; master files covered by plugins deployed later), and
half of it was removed before the error. Now a read-only check comes first."""
import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import major_master_deploy as mmd  # noqa: E402
import py_compat  # noqa: E402


class RemovalBlockers(unittest.TestCase):
    def setUp(self):
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.manager, self.staging, self.data = base / "mgr", base / "staging", base / "game" / "Data Files"
        for d in (self.manager, self.staging / "Pkg", self.data):
            d.mkdir(parents=True)
        self.ops = []

    def _link(self, rel, data=b"x"):
        source = self.staging / "Pkg" / rel
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
        target = self.data / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)
        st = target.stat()
        self.ops.append({"source": str(source), "target": str(target), "size": st.st_size,
                         "mtime_ns": st.st_mtime_ns, "verification": "hardlink"})
        mmd._write_deployment_state(self.manager, {"version": 1, "deployments": [
            {"masters": ["Pkg.esm"], "operations": self.ops, "moves": []}]})
        return source, target

    def blockers(self):
        return mmd.removal_blockers(self.staging, self.data, self.manager)

    def test_untouched_deployment_has_no_blockers(self):
        self._link("Pkg.esm")
        self._link("meshes/a.nif")
        self.assertEqual(self.blockers(), [])

    def test_redated_plugin_is_not_a_blocker(self):
        _s, target = self._link("Pkg.esm")
        os.utime(target, ns=(946857600 * 10**9, 946857600 * 10**9))
        self.assertEqual(self.blockers(), [])

    def test_redated_mesh_and_resized_plugin_are_blockers(self):
        _s, mesh = self._link("meshes/a.nif")
        os.utime(mesh, ns=(946857600 * 10**9, 946857600 * 10**9))
        _s, plugin = self._link("Pkg.esm")
        with open(plugin, "ab") as f:
            f.write(b"more")
        self.assertEqual(sorted(self.blockers()), sorted([str(mesh), str(plugin)]))

    def test_file_replaced_by_a_later_plugin_deployment_is_a_blocker(self):
        _s, target = self._link("meshes/a.nif")
        target.unlink()                                  # a plugin moved the link into its own backup
        target.write_bytes(b"plugin's mesh")
        self.assertEqual(self.blockers(), [str(target)])

    def test_remove_deployed_takes_out_a_redated_plugin(self):
        _s, target = self._link("Pkg.esm")
        os.utime(target, ns=(946857600 * 10**9, 946857600 * 10**9))
        result = mmd.remove_deployed(self.staging, self.data, self.manager)
        self.assertEqual(result["edited"], [])
        self.assertEqual(result["pending"], 0)
        self.assertFalse(target.exists())


class ReopenChecksFirst(unittest.TestCase):
    def test_reopen_checks_blockers_before_removing(self):
        src = (ROOT / "mwmanager_gui.py").read_text(encoding="utf-8")
        start = src.index("def reopen_options(progress):")
        body = src[start:src.index("def options_reopened(", start)]
        self.assertLess(body.index("removal_blockers("), body.index("remove_deployed(staging"))
        self.assertIn('"kept_waiting": True', body)
        ast.parse(src)


if __name__ == "__main__":
    unittest.main()