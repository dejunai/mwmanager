"""Nexus page sentences are scoped to the option: credits and installer-meta dropped, shared clauses trimmed."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod
import mwnexus_parse as P
import option_descriptions as od

# Modelled on Consistent Main Menus (45982) and Tamriel Rebuilt Soundtrack (47254),
# not a dump of the live pages.
CMM_PAGE = (
    "Consistent Main Menus\n"
    "If you use JiFFY's Morrowind UI Revamped (like I do) or another UI mod (like Monochrome) "
    "and/or a main menu image replacement (like Androl's Main Menu Replacer), the MWSE MCM and "
    "Petethegoat's Continue mod add menu textures that don't fit in with your UI. These texture "
    "replacers fix that.\n"
    "I've packed these in BAIN format for people not using the MWSE MCM and/or Continue. "
    "Choose the options that apply when installing\n"
    "The menus come in three flavors:\n"
    "Thanks and Credits\n"
    "Jiffy for Morrowind UI Revamped\n"
    "Androl for Main Menu Replacer\n"
    "NullCascade and the MWSE Team for MWSE and the MCM\n"
    "Petethegoat for his Continue mod.\n"
    "New high-quality replacer textures that homogenize main menus so that MWSE's MCM and "
    "Petethegoat's Continue Mod menu items don't look out of place.\n"
)

TR_PAGE = (
    "Adds whole new soundtrack for Tamriel Rebuilt areas, with region based music, dungeons, cities and other.\n"
    "Drop files from 00 core folder into Morrowind/data files.\n"
    "Optionally you can also drop files and replace from 01 dungeon music everywhere to have unique "
    "dungeon music plays in areas outside Tamriel Rebuilt.\n"
    "There's also 02 air and underwater music everywhere which enabled soundtracks air and underwater "
    "music outside TR areas.\n"
    "Can be also installed using mod manager like Wrye Mash.\n"
    "Permissions\n"
    "You are not allowed to use or redistribute the music files without permission.\n"
)

SIB_CMM = ["MWSE MCM", "Petetehgoat's Continue"]


def describe(label, page, siblings):
    record = {"label": label, "targets": [], "docs": "", "path": "01 " + label}
    return mock_fomod._option_description(record, Path("staging"), page, sibling_labels=siblings)


class CreditAndInstallerFilterTests(unittest.TestCase):
    def test_credit_and_name_for_lines_are_dropped(self):
        mcm = describe("MWSE MCM", CMM_PAGE, SIB_CMM)
        cont = describe("Petetehgoat's Continue", CMM_PAGE, SIB_CMM)
        for text in (mcm, cont):
            self.assertTrue(text, text)
            self.assertNotIn("NullCascade", text)
            self.assertNotIn("Jiffy for Morrowind", text)
            self.assertNotIn("for his Continue mod", text)
            self.assertNotIn("Thanks and Credits", text)

    def test_installer_meta_is_dropped_when_real_text_exists(self):
        mcm = describe("MWSE MCM", CMM_PAGE, SIB_CMM)
        cont = describe("Petetehgoat's Continue", CMM_PAGE, SIB_CMM)
        for text in (mcm, cont):
            self.assertNotIn("BAIN", text)
            self.assertNotIn("Choose the options that apply", text)
        dungeon = describe("dungeon music everywhere", TR_PAGE, ["air and underwater music everywhere"])
        self.assertNotIn("Drop files from 00 core", dungeon)
        self.assertNotIn("Wrye Mash", dungeon)
        self.assertNotIn("data files", dungeon.casefold())

    def test_installer_meta_is_kept_when_it_is_the_only_text(self):
        page = "I've packed these in BAIN format so you can choose the MWSE MCM option.\n"
        text = "\n".join(P.best_option_sentences(page, "MWSE MCM"))
        self.assertIn("BAIN", text)
        self.assertIn("MCM", text)


class SharedClauseTests(unittest.TestCase):
    def test_a_shared_sentence_is_trimmed_to_this_option(self):
        mcm = describe("MWSE MCM", CMM_PAGE, SIB_CMM)
        cont = describe("Petetehgoat's Continue", CMM_PAGE, SIB_CMM)
        self.assertIn("so that MWSE's MCM menu items don't look out of place", mcm)
        self.assertIn("so that Petethegoat's Continue Mod menu items don't look out of place", cont)
        self.assertNotIn("Continue", mcm)
        self.assertNotIn("MWSE's MCM and", cont)
        self.assertNotIn("BAIN", mcm + cont)
        self.assertNotIn("NullCascade", mcm + cont)

    def test_a_unique_sentence_is_preferred_over_a_shared_one(self):
        page = (
            "The Labelled Potions pack adds icons so you can tell poisons from potions.\n"
            "The exploit fix and the Labelled Potions pack can be used together.\n"
        )
        lines = P.best_option_sentences(page, "Labelled Potions", sibling_labels=["Exploit Fix"])
        self.assertTrue(lines)
        self.assertIn("icons", lines[0])
        self.assertNotIn("exploit fix", lines[0].casefold())


class FolderWordingTests(unittest.TestCase):
    def test_leading_folder_numbers_and_drop_files_from_are_cleaned(self):
        siblings = ["dungeon music everywhere", "air and underwater music everywhere"]
        dungeon = describe("dungeon music everywhere", TR_PAGE, siblings)
        air = describe("air and underwater music everywhere", TR_PAGE, siblings)
        self.assertIn("dungeon music everywhere", dungeon)
        self.assertNotIn("01 dungeon", dungeon)
        self.assertNotRegex(dungeon, r"\bdrop files and replace from\b")
        self.assertIn("outside Tamriel Rebuilt", dungeon)
        self.assertIn("air and underwater music everywhere", air)
        self.assertNotIn("02 air", air)

    def test_self_in_sibling_labels_does_not_cancel_a_definition_line(self):
        # Callers pass every option label, including this one.
        page = (
            "Replacer - a replacer for some of the worst offenders amongst original sounds. "
            "Aimed at providing a high quality alternative.\n"
            "There are also additional replacers for Animated Morrowind and Passage of Prayers.\n"
            "Technically compatible with existing sound mods. Replacer file will replace original sounds.\n"
        )
        labels = ["Replacer", "Prayer wheel sound replacer", "Animated Morrowind Music Replacer"]
        replacer = P.best_option_sentences(page, "Replacer", sibling_labels=labels)
        self.assertTrue(replacer)
        self.assertTrue(replacer[0].startswith("Replacer - a replacer"), replacer)
        self.assertFalse(any("additional replacers" in line for line in replacer), replacer)
        self.assertFalse(any("Technically compatible" in line for line in replacer), replacer)
        prayer = "\n".join(P.best_option_sentences(page, "Prayer wheel sound replacer", sibling_labels=labels))
        self.assertIn("Passage of Prayers", prayer)
        self.assertNotIn("Technically compatible", prayer)
        self.assertNotIn("worst offenders", prayer)

    def test_a_generic_music_stem_keeps_a_pack_summary_when_no_unique_line_exists(self):
        page = (
            "A MUSE-compatible music pack featuring Celtic compositions to Solstheim, Skyrim, and Lokken.\n"
            "Built directly on music from Celtic Music in Skyrim and Songbook of the North.\n"
            "You can also add your own music by simply placing tracks in the Music folder.\n"
            "Copy the \"Music\" folder and your chosen configuration file to the \"Data Files\" directory.\n"
            "Skyrim: Home of the Nords\n"
            "Solstheim - Tomb of the Snow Prince\n"
        )
        labels = ["Skyrim", "Solstheim", "Combat Music"]
        combat = "\n".join(P.best_option_sentences(page, "Combat Music", sibling_labels=labels))
        self.assertIn("MUSE-compatible", combat)
        self.assertNotIn("Data Files", combat)
        skyrim = "\n".join(P.best_option_sentences(page, "Skyrim", sibling_labels=labels))
        self.assertIn("Home of the Nords", skyrim)
        self.assertNotIn("MUSE-compatible", skyrim)

    def test_refine_drops_a_name_for_credit_but_keeps_designed_for(self):
        raw = (
            "NullCascade and the MWSE Team for MWSE and the MCM\n"
            "These VFX are designed for Nvidia graphics cards and have higher quality effects.\n"
        )
        out = od.refine(raw)
        self.assertNotIn("NullCascade", out)
        self.assertIn("designed for Nvidia", out)


# Modelled on Watch the Skies (48636): numbered definition lines under "Installation:".
WTS_PAGE = (
    "randomised cloud textures - no more exactly same clouds each time\n"
    "includes a texture pack (6.0.0 now includes a supported and recommended skies.iv pack)\n"
    "alternative clouds shader\n"
    "Also note that you can turn that off if you want your sky colour closer to your Weather Adjuster preset.\n"
    "Installation:\n"
    "01 Clouds textures - 1k texture pack for all weathers. Incompatible with skies.iv meshes.\n"
    "02 Weather Adjuster preset - Weather Adjuster presets covering all regions.\n"
    "04 Clouds shader - A new shader for the clouds, with crisp looking halo. Huge thanks to Dexter and Hrnchamd.\n"
)
WTS_LABELS = ["Clouds textures (vanilla)", "Clouds textures - (skies.iv)", "Weather Adjuster preset", "Clouds shader"]


class NumberedDefinitionTests(unittest.TestCase):
    def pick(self, label):
        return "\n".join(P.best_option_sentences(WTS_PAGE, label, sibling_labels=WTS_LABELS))

    def test_a_numbered_definition_line_wins_and_loses_its_number(self):
        self.assertEqual(self.pick("Weather Adjuster preset"),
                         "Weather Adjuster preset - Weather Adjuster presets covering all regions.")

    def test_a_trailing_thanks_sentence_is_dropped(self):
        text = self.pick("Clouds shader")
        self.assertTrue(text.startswith("Clouds shader - A new shader"), text)
        self.assertNotIn("thanks", text.casefold())
        self.assertNotIn("alternative clouds shader", text)

    def test_variant_qualifiers_pick_the_matching_line(self):
        vanilla = self.pick("Clouds textures (vanilla)")
        self.assertTrue(vanilla.startswith("Clouds textures - 1k texture pack"), vanilla)
        skies = self.pick("Clouds textures - (skies.iv)")
        self.assertIn("skies.iv pack", skies)
        self.assertNotIn("Incompatible with skies.iv", skies)


if __name__ == "__main__":
    unittest.main()
