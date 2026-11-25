r"""Staging error found by the CR-25 stress run (round 3): a lone wrapper folder with the game folders straight
inside it (Better Telvanni Crystal V2 4k\Meshes, \Textures; no Data Files) went to Extras, so the package
deployed 0 files."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_install as ext  # noqa: E402
import mwmanager_upstream  # noqa: E402


def _build(mod, files):
    for rel, data in files.items():
        (mod / rel).parent.mkdir(parents=True, exist_ok=True)
        (mod / rel).write_bytes(data)


class StagingRound3(unittest.TestCase):
    def _tidy(self, files, name):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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

    def test_lone_wrapper_around_game_folders_is_flattened(self):
        mod, failed = self._tidy({
            "Better Telvanni Crystal V2 4k/Meshes/oaab/i/t_crystal_00.nif": b"nif",
            "Better Telvanni Crystal V2 4k/Textures/Better Telvanni Crystal V2 4k/tx_t_crystal_01.dds": b"dds",
            "Better Telvanni Crystal V2 4k/readme.txt": b"readme",
        }, "Better Telvanni Crystal V2 4k-59196")
        self.assertEqual(failed, [])
        self.assertTrue((mod / "Meshes" / "oaab" / "i" / "t_crystal_00.nif").is_file())
        self.assertTrue((mod / "Textures" / "Better Telvanni Crystal V2 4k" / "tx_t_crystal_01.dds").is_file())
        self.assertFalse((mod / "Better Telvanni Crystal V2 4k").exists())
        self.assertFalse((mod / "Extras").exists() and any((mod / "Extras").rglob("*.nif")))
        self.assertTrue(any(p.name == "readme.txt" or "readme" in p.name.casefold()
                            for p in (mod / "Docs").rglob("*") if p.is_file()))

    def test_lone_wrapper_plugin_moves_to_the_root(self):
        mod, failed = self._tidy({
            "Wrapped Mod/Wrapped.esp": b"TES3",
            "Wrapped Mod/Meshes/w/a.nif": b"nif",
        }, "Wrapped Mod-1")
        self.assertEqual(failed, [])
        self.assertTrue((mod / "Wrapped.esp").is_file())
        self.assertTrue((mod / "Meshes" / "w" / "a.nif").is_file())

    def test_two_wrapped_variants_with_game_folders_stay_options(self):
        mod, failed = self._tidy({
            "Variant A/Meshes/a.nif": b"a",
            "Variant B/Meshes/a.nif": b"b",
        }, "Two Variants-2")
        self.assertEqual(failed, [])
        self.assertFalse((mod / "Meshes" / "a.nif").exists())

    def test_lone_folder_without_game_folders_is_not_flattened(self):
        mod, failed = self._tidy({
            "Source Art/bark.psd": b"psd",
        }, "Art Only-3")
        self.assertEqual(failed, [])
        self.assertFalse((mod / "bark.psd").exists())


if __name__ == "__main__":
    unittest.main()