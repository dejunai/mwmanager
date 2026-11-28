r"""Staging errors found by the opt16 stress batch: a lone wrapper folder around a mod's MWSE scripts or around a
lone ESM was not flattened (MWSE went to Extras, the ESM went to Extras: the mod would not run or not load), and
the option window offered the package's only plugin, still inside that wrapper, as an optional nested choice."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod  # noqa: E402
import mwmanager_install as ext  # noqa: E402
import mwmanager_upstream  # noqa: E402
import py_compat  # noqa: E402


def _build(mod, files):
    for rel, data in files.items():
        (mod / rel).parent.mkdir(parents=True, exist_ok=True)
        (mod / rel).write_bytes(data)


class LoneWrapperTidy(unittest.TestCase):
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

    def test_mwse_scripts_in_a_lone_wrapper_go_to_the_root(self):
        mod, failed = self._tidy({
            "Vamp PTR/Vamp PTR.ESP": b"TES3",
            "Vamp PTR/MWSE/mods/Vamp/main.lua": b"-- lua",
            "Vamp PTR/Sound/Vo/v/a.mp3": b"mp3",
        }, "Vamp-1")
        self.assertEqual(failed, [])
        self.assertTrue((mod / "Vamp PTR.ESP").is_file())
        self.assertTrue((mod / "MWSE" / "mods" / "Vamp" / "main.lua").is_file())
        self.assertTrue((mod / "Sound" / "Vo" / "v" / "a.mp3").is_file())
        self.assertFalse(any((mod / "Extras").rglob("main.lua")) if (mod / "Extras").exists() else False)

    def test_a_lone_esm_in_a_wrapper_goes_to_the_root(self):
        mod, failed = self._tidy({"Anthology Patched/AnthologyPatched.esm": b"TES3"}, "Anthology-2")
        self.assertEqual(failed, [])
        self.assertTrue((mod / "AnthologyPatched.esm").is_file())
        self.assertFalse((mod / "Anthology Patched").exists())

    def test_a_wrapper_of_loose_files_is_still_not_flattened(self):
        mod, failed = self._tidy({"Notes Only/thing.ini": b"x=1"}, "Notes-3")
        self.assertEqual(failed, [])
        self.assertFalse((mod / "thing.ini").exists())


class LoneWrapperOptions(unittest.TestCase):
    def _mod(self, files):
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        mod = Path(tmp.name) / "Mod-1"
        _build(mod, files)
        return mod

    def test_the_only_plugin_in_the_only_folder_is_not_an_optional_choice(self):
        mod = self._mod({"Vamp PTR/Vamp PTR.ESP": b"TES3", "Vamp PTR/MWSE/mods/v/main.lua": b"--"})
        self.assertEqual(mock_fomod._lone_plugin_wrapper(mod), "Vamp PTR")
        self.assertEqual(mock_fomod._discover_plugin_choices(mod, set()), [])

    def test_several_wrapped_plugins_stay_choices(self):
        mod = self._mod({"Pack/A.esp": b"TES3", "Pack/B.esp": b"TES3"})
        self.assertIsNone(mock_fomod._lone_plugin_wrapper(mod))
        labels = [r["key"] for r in mock_fomod._discover_plugin_choices(mod, set())]
        self.assertEqual(len(labels), 2)

    def test_a_plugin_in_one_of_two_folders_stays_a_choice(self):
        mod = self._mod({"Optional Patch/Patch.esp": b"TES3", "Meshes/a.nif": b"nif"})
        self.assertIsNone(mock_fomod._lone_plugin_wrapper(mod))
        self.assertEqual(len(mock_fomod._discover_plugin_choices(mod, set())), 1)


if __name__ == "__main__":
    unittest.main()
