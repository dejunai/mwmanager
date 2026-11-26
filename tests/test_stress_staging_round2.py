"""Staging errors found by the CR-25 stress run (round 2): a Data Files folder flattened onto an existing root
folder, a lone wrapper folder around Data Files sent to Extras, and --all failing after fixing everything."""
import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_install as ext  # noqa: E402
import mwmanager_upstream  # noqa: E402
import py_compat  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _build(mod, files):
    for rel, data in files.items():
        (mod / rel).parent.mkdir(parents=True, exist_ok=True)
        (mod / rel).write_bytes(data)


class StagingRound2(unittest.TestCase):
    def _tidy(self, files, name):
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        staging = Path(tmp.name) / "staging"
        mod = staging / name
        _build(mod, files)
        with mwmanager_upstream.use_manager_dir(tmp.name):
            Path(tmp.name, "outputs").mkdir(exist_ok=True)
            plan = ext.plan_mod(str(mod))
            self.assertNotIsInstance(plan, str)
            failed = ext.apply(plan, str(staging))
        return mod, failed

    def test_data_files_docs_merges_into_an_existing_root_docs(self):
        mod, failed = self._tidy({
            "Docs/Containers - readme.txt": b"root readme",
            "Data Files/Docs/Containers - changes.txt": b"changes",
            "Data Files/Meshes/ac/box.nif": b"nif",
            "Containers.esp": b"TES3",
        }, "MW Containers Animated-42238")
        self.assertEqual(failed, [])
        self.assertFalse((mod / "Data Files").exists())
        self.assertTrue((mod / "Meshes" / "ac" / "box.nif").is_file())
        docs = {p.name for p in (mod / "Docs").rglob("*") if p.is_file()}
        self.assertIn("Containers - readme.txt", docs)
        self.assertTrue(any("changes" in n for n in docs), docs)

    def test_lone_wrapper_around_data_files_is_flattened(self):
        mod, failed = self._tidy({
            "SM_BCTree_1_0/Data files/Meshes/f/flora_bc_tree_01.nif": b"nif",
            "SM_BCTree_1_0/Data files/Textures/sm_BCtree/tx_bc_bark_02.dds": b"dds",
            "SM_BCTree_1_0/read me.txt": b"readme",
        }, "SM_Bitter Coast Tree-49883")
        self.assertEqual(failed, [])
        self.assertTrue((mod / "Meshes" / "f" / "flora_bc_tree_01.nif").is_file())
        self.assertTrue((mod / "Textures" / "sm_BCtree" / "tx_bc_bark_02.dds").is_file())
        self.assertFalse((mod / "SM_BCTree_1_0").exists())
        self.assertFalse((mod / "Extras").exists() and any((mod / "Extras").rglob("*.nif")))

    def test_two_wrapped_variants_stay_options(self):
        mod, failed = self._tidy({
            "Variant A/Data Files/Meshes/a.nif": b"a",
            "Variant B/Data Files/Meshes/a.nif": b"b",
        }, "Two Variants-1")
        self.assertEqual(failed, [])
        self.assertFalse((mod / "Meshes" / "a.nif").exists())


class AllExitCode(unittest.TestCase):
    def test_check_exits_zero_after_its_fixes_were_applied(self):
        tree = ast.parse((ROOT / "mwmanager_overrides.py").read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and "run_fix" in ast.unparse(n) and "counts['ERROR']" in ast.unparse(n))
        src = ast.unparse(fn)
        self.assertIn("fixed = run_fix(", src)
        ret = [ast.unparse(n.value) for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
        self.assertIn("1 if counts['ERROR'] and (not fixed) else 0", ret)


if __name__ == "__main__":
    unittest.main()