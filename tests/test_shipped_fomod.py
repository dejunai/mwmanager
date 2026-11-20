"""MWManager ships its own installer (fomods\\<Nexus id>.xml) for a mod that comes as loose BAIN folders whose valid
combinations only its readme explains: Pherim's Kwama Eggs and Egg Sacs Replacer (43555). Bump mapping is all or
nothing, so every valid combination is one choice, and within a choice a later folder overrides an earlier one
(03's pulsing bump-mapped sac goes in over 01's sac)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod  # noqa: E402

FOLDERS = {
    "01-bump-maps": ["meshes/f/food_kwama_egg_01.nif", "meshes/o/contain_egg_kwama00.nif"],
    "02-Pulsing Animation": ["meshes/o/contain_egg_kwama00.nif"],
    "03-Pulsing Animation Bump-mapped": ["meshes/o/contain_egg_kwama00.nif", "textures/tx_egg_kwama01_nm.dds"],
    "04-Graphic herbalism": ["meshes/gherb/egg_kwama00_P.nif"],
    "05-Graphic herbalism bump-mapped": ["meshes/gherb/egg_kwama00_P.nif"],
}


class KwamaEggInstaller(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = Path(tmp.name) / "staging" / "plugins" / "Kwama Egg sacs and Eggs Replacer-43555-1-1 [6]"
        extras = self.mod / "Extras" / self.mod.name          # where Tidy put the numbered folders
        for folder, files in FOLDERS.items():
            for rel in files:
                (extras / folder / rel).parent.mkdir(parents=True, exist_ok=True)
                (extras / folder / rel).write_bytes(folder.encode())

    def options(self):
        return {r["label"]: r for r in mock_fomod._discover_fomod(self.mod, set())}

    def winner(self, record, rel):
        return Path(record["payload_files"][str(Path(rel))]).read_bytes().decode()

    def test_every_valid_combination_is_one_exclusive_choice(self):
        self.assertTrue(mock_fomod._has_fomod(self.mod))
        options = self.options()
        self.assertEqual(len(options), 8)
        self.assertEqual({r["selection_type"] for r in options.values()}, {"SelectExactlyOne"})
        self.assertEqual(len({r["group_key"] for r in options.values()}), 1)
        self.assertEqual(options["Plain"]["payload_files"], {})
        self.assertEqual(options["Plain"]["option_type"], "Recommended")

    def test_folders_go_in_in_the_readmes_order(self):
        options = self.options()
        both = options["Bump-mapped, pulsing egg sacs, for Graphic Herbalism"]
        self.assertEqual(self.winner(both, "meshes/o/contain_egg_kwama00.nif"), "03-Pulsing Animation Bump-mapped")
        self.assertEqual(self.winner(both, "meshes/f/food_kwama_egg_01.nif"), "01-bump-maps")
        self.assertEqual(self.winner(both, "meshes/gherb/egg_kwama00_P.nif"), "05-Graphic herbalism bump-mapped")
        plain = options["Plain, pulsing egg sacs, for Graphic Herbalism"]
        self.assertEqual(self.winner(plain, "meshes/o/contain_egg_kwama00.nif"), "02-Pulsing Animation")
        self.assertEqual(self.winner(plain, "meshes/gherb/egg_kwama00_P.nif"), "04-Graphic herbalism")
        self.assertNotIn(str(Path("meshes/f/food_kwama_egg_01.nif")), plain["payload_files"])    # no bump maps

    def test_the_graphic_herbalism_choices_wait_for_graphic_herbalism(self):
        """Its emptied egg sac mesh is only used by Graphic Herbalism: chosen without it, the option waits and the
        batch queues that mod's archive."""
        for label, record in self.options().items():
            self.assertEqual(record["missing_masters"],
                             ["graphic herbalism (mod)"] if "Graphic Herbalism" in label else [], label)

    def test_a_packages_own_fomod_wins(self):
        config = self.mod / "fomod" / "ModuleConfig.xml"
        config.parent.mkdir(parents=True)
        config.write_text('<config><installSteps><installStep name="S"><optionalFileGroups><group name="G" '
                          'type="SelectAny"><plugins><plugin name="Own"><files><folder source="x" destination=""/>'
                          '</files><typeDescriptor><type name="Optional"/></typeDescriptor></plugin></plugins>'
                          '</group></optionalFileGroups></installStep></installSteps></config>', encoding="utf-8")
        self.assertEqual(list(self.options()), ["Own"])


if __name__ == "__main__":
    unittest.main()
