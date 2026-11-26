"""A staged package's folder and its Extras folder repeat the package name in every path below them. The full
download name, twice ("Better Telvanni Crystal V2 4k-59196-1-0-1780326665 [2]\\Extras\\<the same>\\..."), pushed a
texture to 261 characters and stopped the deployment: both use the short mod name now, at most 40 characters."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwmanager_upstream  # noqa: E402


class ShortPackageNames(unittest.TestCase):
    def test_the_staging_folder_uses_the_short_mod_name(self):
        import mwmanager_gui as g
        self.assertEqual(g.short_package_name(Path("Better Telvanni Crystal V2 4k-59196-1-0-1780326665.rar")),
                         "Better Telvanni Crystal V2 4k")
        self.assertEqual(g.short_package_name(Path("Kwama Egg sacs and Eggs Replacer-43555-1-1.7z")),
                         "Kwama Egg sacs and Eggs Replacer")
        long = g.short_package_name(Path("A Very Long Mod Name That Goes On And On Forever-12345-1-0-1700000000.zip"))
        self.assertLessEqual(len(long), g.SHORT_NAME_LIMIT)
        self.assertEqual(long, "A Very Long Mod Name That Goes On And On")

    def test_the_staging_folder_is_the_short_name_and_the_nexus_id(self):
        import mwmanager_gui as g
        self.assertEqual(g.staging_folder_name({"archive": "Better Telvanni Crystal V2 4k-59196-1-0-1780326665.rar"}),
                         "Better Telvanni Crystal V2 4k-59196")
        self.assertEqual(g.staging_folder_name({"archive": "Kwama Egg sacs and Eggs Replacer-43555-1-1.7z"}),
                         "Kwama Egg sacs and Eggs Replacer-43555")
        self.assertEqual(g.staging_folder_name({"archive": "Odd.7z", "nexus_mod_id": "777"}), "Odd-777")
        self.assertEqual(g.staging_folder_name({"archive": "Loose files.zip"}), "Loose files")

    def test_tidys_extras_name_drops_the_staging_suffix_and_is_capped(self):
        tidy = mwmanager_upstream.compose_tidy()
        self.assertEqual(tidy.staging_package_name(r"G:\s\Better Telvanni Crystal V2 4k-59196-1-0-1780326665 [2]"),
                         "Better Telvanni Crystal V2 4k")
        self.assertLessEqual(len(tidy.staging_package_name("G:\\s\\" + "x" * 80 + " [3]")), tidy.PACKAGE_NAME_LIMIT)
        self.assertEqual(tidy.staging_package_name(r"G:\s\Better Telvanni Crystal V2 4k-59196"),
                         "Better Telvanni Crystal V2 4k")



    def test_compose_leaves_upstreams_openmw_package_name_alone(self):
        """vortex_tidy.package_name is omw2mwse's Off\\<plugin>\\OpenMW helper; our staging extras
        name is staging_package_name. They must not shadow each other after compose_tidy()."""
        import tempfile
        tidy = mwmanager_upstream.compose_tidy()
        self.assertEqual(
            tidy.staging_package_name(r"G:\s\Better Telvanni Crystal V2 4k-59196-1-0-1780326665 [2]"),
            "Better Telvanni Crystal V2 4k")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "My OpenMW Pack [2]"
            folder.mkdir()
            (folder / "My OpenMW Pack.omwscripts").write_text("x", encoding="utf-8")
            self.assertEqual(tidy.package_name(str(folder)), "MyOpenMWPack")
            empty = Path(tmp) / "Some Mod v1.0"
            empty.mkdir()
            self.assertEqual(tidy.package_name(str(empty)), "SomeModv10")

if __name__ == "__main__":
    unittest.main()
