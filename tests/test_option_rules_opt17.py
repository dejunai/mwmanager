r"""Rule-based option shaping (Dej, t87u/t88u): exclusive plugin variants in one folder, optional plugins, warnings
first and unticked, code lines out of descriptions, and the test guard around the real Nexus browser folder."""
import os
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod  # noqa: E402
import mwnexus_browser  # noqa: E402
import option_variants as V  # noqa: E402
import py_compat  # noqa: E402
from mwnexus_parse import strip_code_lines  # noqa: E402

BASE = ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"]


def _sub(tag, data):
    return tag + struct.pack("<I", len(data)) + data


def _record(rtype, body):
    return rtype + struct.pack("<I", len(body)) + b"\0" * 8 + body


def make_plugin(path, masters=BASE, records=()):
    header = _sub(b"HEDR", struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", len(records)))
    for master in masters:
        header += _sub(b"MAST", master.encode() + b"\0") + _sub(b"DATA", struct.pack("<Q", 0))
    data = _record(b"TES3", header)
    for rtype, ident in records:
        data += _record(rtype.encode(), _sub(b"NAME", ident.encode() + b"\0") + _sub(b"DATA", b"\0" * 8))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


LIGHTS = [("LIGH", f"light_{i:02d}") for i in range(20)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class VariantRule(Base):
    def test_same_name_variants_with_overlapping_records_are_one_choice(self):
        a = make_plugin(self.root / "Mod Full - Still - Necro Colors.esp", records=LIGHTS)
        b = make_plugin(self.root / "Mod Full - Still - Original Colors.esp", records=LIGHTS[:18])
        self.assertEqual(V.variant_groups([b, a]), [[a, b]])

    def test_patches_beside_a_main_plugin_are_not_variants(self):
        plugins = [make_plugin(self.root / n, masters=BASE + ["Fighters Guild Improved.esp"], records=LIGHTS)
                   for n in ("FGI_CaerisCamonnaTong_Patch.esp", "FGI_FightersGuildRedux_Patch.esp")]
        plugins.append(make_plugin(self.root / "Fighters Guild Improved.esp", records=LIGHTS))
        self.assertEqual(V.variant_groups(plugins, "Use only one of the patches you need."), [])

    def test_addons_next_to_the_main_plugin_are_not_variants(self):
        plugins = [make_plugin(self.root / n, records=LIGHTS) for n in (
            "BTB's Game Improvements (Necro Edit).esp", "BTBGI Creature Buffs.esp", "BTBGI Races and Birthsigns.esp")]
        self.assertEqual(V.variant_groups(plugins), [])

    def test_variant_names_without_shared_records_or_docs_load_together(self):
        a = make_plugin(self.root / "Great Towns - Balmora Part.esp", records=[("CELL", "balmora")])
        b = make_plugin(self.root / "Great Towns - Vivec Part.esp", records=[("CELL", "vivec")])
        self.assertEqual(V.variant_groups([a, b]), [])

    def test_docs_saying_use_one_make_variants_exclusive(self):
        a = make_plugin(self.root / "Great Towns - Balmora Part.esp", records=[("CELL", "balmora")])
        b = make_plugin(self.root / "Great Towns - Vivec Part.esp", records=[("CELL", "vivec")])
        self.assertEqual(V.variant_groups([a, b], "Choose one: Balmora Part or Vivec Part."), [[a, b]])

    def test_a_plugin_built_on_the_other_is_an_addon(self):
        a = make_plugin(self.root / "Mod Full - Base Lights.esp", records=LIGHTS)
        b = make_plugin(self.root / "Mod Full - Extra Lights.esp", masters=BASE + ["Mod Full - Base Lights.esp"],
                        records=LIGHTS)
        self.assertEqual(V.variant_groups([a, b]), [])

    def test_recommended_variant_wins_else_first_in_natural_order(self):
        a = make_plugin(self.root / "Mod Full - Still - Necro Colors.esp", records=LIGHTS)
        b = make_plugin(self.root / "Mod Full - Still - Original Colors.esp", records=LIGHTS)
        self.assertEqual(V.recommended_variant([a, b], ["I recommend the Original Colors version."]), b)
        self.assertEqual(V.recommended_variant([a, b], [""]), a)


class VariantsInTheOptionWindow(Base):
    NAMES = ("Mod Full - Still - Necro Colors.esp", "Mod Full - Still - Original Colors.esp")

    def _package(self, readme=""):
        """The layout after the pre-option tidy: the 00 folder kept under Extras, its files linked into the root."""
        mod = self.root / "Lights Mod-123"
        core = mod / "Extras" / "Lights Mod" / "00 Full Mod"
        for name in self.NAMES:
            make_plugin(core / name, records=LIGHTS)
            os.link(core / name, mod / name)
        make_plugin(mod / "Extras" / "Lights Mod" / "01 Daylight" / "Mod Daylight.esp", records=[("CELL", "x")])
        if readme:
            (mod / "readme.txt").write_text(readme, encoding="utf-8")
        return mod

    def _variants(self, records):
        return [r for r in records if str(r.get("group_key", "")).startswith("variants:")]

    def test_core_variants_become_one_exclusive_choice_and_one_is_deployed(self):
        mod = self._package()
        records = mock_fomod._discover(mod, set())
        variants = self._variants(records)
        self.assertEqual([r["label"] for r in variants], list(self.NAMES))
        self.assertTrue(all(r["selection_type"] == "SelectExactlyOne" for r in variants))
        self.assertEqual([r.get("recommended") for r in variants], [True, False])
        self.assertNotIn(set(r["key"] for r in variants), [mock_fomod._sole_data_files_choices(records)])
        chosen = {variants[1]["key"]}
        mock_fomod._activate(mod, "x", records, chosen, {}, self.root / "backup")
        mock_fomod._apply_plugin_choices(mod, records, chosen)
        self.assertFalse((mod / self.NAMES[0]).exists())
        self.assertTrue((mod / self.NAMES[1]).exists())
        self.assertTrue((mod / "Extras" / "Lights Mod" / "00 Full Mod" / self.NAMES[0]).exists())
        # Tidy runs again after the options: it must not link the left-out variant back from the 00 folder
        import mwmanager_install as ext
        import mwmanager_upstream
        with mwmanager_upstream.use_manager_dir(str(self.root)):
            (self.root / "outputs").mkdir(exist_ok=True)
            plan = ext.plan_mod(str(mod))
            failed = ext.apply(plan, str(self.root)) if not isinstance(plan, str) else []
        self.assertEqual(failed or [], [])
        self.assertFalse((mod / self.NAMES[0]).exists())
        self.assertTrue((mod / self.NAMES[1]).exists())
        # changing the answer later brings the other one back
        mock_fomod._apply_plugin_choices(mod, records, {variants[0]["key"]})
        self.assertTrue((mod / self.NAMES[0]).exists())
        self.assertFalse((mod / self.NAMES[1]).exists())

    def test_the_readme_recommendation_is_the_default(self):
        mod = self._package("Mod Full: I recommend the Original Colors version for most setups.")
        variants = self._variants(mock_fomod._discover(mod, set()))
        self.assertEqual([r.get("recommended") for r in variants], [False, True])

    def test_variants_in_an_optional_folder_allow_at_most_one(self):
        mod = self.root / "Lights Mod-124"
        folder = mod / "Extras" / "Lights Mod" / "01 Modular"
        for name in self.NAMES:
            make_plugin(folder / name, records=LIGHTS)
        make_plugin(folder / "Mod Ambient.esp", records=[("CELL", "y")])
        records = mock_fomod._discover(mod, set())
        variants = self._variants(records)
        self.assertEqual([r["selection_type"] for r in variants], ["SelectAtMostOne"] * 2)
        self.assertFalse(any(r.get("recommended") for r in variants))
        folder_record = next(r for r in records if r["key"].endswith("01 Modular"))
        self.assertEqual(sorted(Path(k).name for k in folder_record["payload_files"]), ["Mod Ambient.esp"])
        chosen = {folder_record["key"], variants[0]["key"]}
        mock_fomod._activate(mod, "x", records, chosen, {}, self.root / "backup")
        mock_fomod._apply_plugin_choices(mod, records, chosen)
        self.assertEqual(sorted(p.name for p in mod.glob("*.esp")), ["Mod Ambient.esp", self.NAMES[0]])


class OptionalPlugins(Base):
    def _repair(self, readme=""):
        mod = self.root / "Repair Mod-46"
        make_plugin(mod / "Data Files" / "Repair_Optional.ESP", records=[("CELL", "x")])
        main = mod / "Data Files" / "MWSE" / "mods" / "repair" / "main.lua"
        main.parent.mkdir(parents=True)
        main.write_text("-- lua", encoding="utf-8")
        if readme:
            (mod / "readme.txt").write_text(readme, encoding="utf-8")
        return mod

    def _optional(self, records):
        return [r for r in records if str(r.get("group_key", "")).startswith("optional-plugin:")]

    def _records(self, mod):
        records = mock_fomod._discover(mod, set())
        return records + mock_fomod._discover_plugin_choices(mod, set(), mock_fomod._represented_option_sources(records))

    def test_an_optional_plugin_in_the_base_payload_is_a_ticked_choice(self):
        mod = self._repair()
        records = self._records(mod)
        optional = self._optional(records)
        self.assertEqual([r["label"] for r in optional], ["Repair_Optional.ESP"])
        self.assertTrue(optional[0]["recommended"])
        base = [r for r in records if r is not optional[0]]
        self.assertTrue(all("Repair_Optional.ESP" not in r.get("payload_files", {}) for r in base))

    def test_a_warning_goes_first_and_leaves_it_unticked(self):
        warning = "However, I'm not very good at CS stuff so I highly recommend instead using Other Repair Add-on instead."
        mod = self._repair("There is also an optional ESP that adds additional forges and anvils. " + warning)
        optional = self._optional(self._records(mod))[0]
        self.assertFalse(optional["recommended"])
        self.assertEqual(optional["warning"], warning)
        text = mock_fomod._option_description(optional, mod)
        self.assertTrue(text.startswith(warning), text)
        self.assertIn("adds additional forges", text)

    def test_an_unticked_optional_plugin_is_not_left_for_tidy_to_deploy(self):
        mod = self._repair()
        records = self._records(mod)
        self.assertNotIn(self._optional(records)[0]["key"], mock_fomod._sole_data_files_choices(records))
        mock_fomod._activate(mod, "x", records, {r["key"] for r in records if r.get("required")}, {},
                             self.root / "backup")
        mock_fomod._apply_plugin_choices(mod, records, set())
        self.assertFalse((mod / "Repair_Optional.ESP").exists())
        self.assertFalse((mod / "Data Files" / "Repair_Optional.ESP").exists())
        self.assertTrue(any((mod / "Extras").rglob("Repair_Optional.ESP")))

    def test_a_sole_optional_root_plugin_is_a_choice(self):
        mod = self.root / "Thing-9"
        make_plugin(mod / "Thing_Optional.esp")
        records = mock_fomod._discover_plugin_choices(mod, set())
        self.assertEqual([(r["label"], r["recommended"]) for r in records], [("Thing_Optional.esp", True)])

    def test_a_plain_sole_plugin_is_not_a_choice(self):
        mod = self.root / "Thing-10"
        make_plugin(mod / "Thing.esp")
        self.assertEqual(mock_fomod._discover_plugin_choices(mod, set()), [])

    def test_a_numbered_option_the_readme_warns_against_starts_unticked(self):
        mod = self.root / "Tex-11"
        for name in ("01 Old Textures", "02 New Textures"):
            (mod / "Extras" / "Tex" / name / "Textures").mkdir(parents=True)
            (mod / "Extras" / "Tex" / name / "Textures" / "a.dds").write_bytes(b"dds")
        (mod / "readme.txt").write_text("01 Old Textures - the old textures are outdated, do not use them.\n"
                                        "02 New Textures - the new textures.", encoding="utf-8")
        records = {r["label"]: r for r in mock_fomod._discover(mod, set())}
        self.assertTrue(records["Old Textures"].get("warning"))
        self.assertFalse(records["Old Textures"].get("recommended"))
        self.assertFalse(records["New Textures"].get("warning"))


class CodeLines(unittest.TestCase):
    def test_config_and_lua_lines_are_dropped(self):
        text = ("01 folder is a tweaked replacement for Close Inspection.esp, install it only if you want to use it\n"
                "closeInspectionTweaks = true, -- true to tweak Close Inspection to avoid possible crashes\n"
                "local cfg = require(\"x.config\")\nend\nfov = 75")
        self.assertEqual(strip_code_lines(text),
                         "01 folder is a tweaked replacement for Close Inspection.esp, install it only if you want "
                         "to use it")

    def test_prose_with_equals_or_if_stays(self):
        for line in ("If you use X, then install Y.", "Speed = 2x faster than before",
                     "Set config.enabled = false in the MCM", "return to Balmora when done"):
            self.assertEqual(strip_code_lines(line), line)

    def test_option_description_strips_code(self):
        record = {"label": "Close Inspection", "docs": "Tweaked Close Inspection.\nzoomSpeed = 2, -- faster",
                  "targets": []}
        self.assertEqual(mock_fomod._option_description(record, Path(".")), "Tweaked Close Inspection.")


class AboutSentences(unittest.TestCase):
    def test_the_sentence_about_the_optional_plugin_beats_one_about_the_mod(self):
        docs = "# Lantern Works Refactoring Summary\nComplete refactoring of the Lantern Works mod.\n"
        page = "Lantern Works changes lanterns. There is also an optional ESP that adds lanterns to towns."
        about = V.about_sentences("Lantern_Works_Optional", [docs, page], optional=True)
        self.assertEqual(about, ["There is also an optional ESP that adds lanterns to towns."])

    def test_headings_and_repeats_are_left_out(self):
        docs = "## Lantern Works\nLantern Works adds lanterns.\nLantern Works adds lanterns to towns."
        about = V.about_sentences("Lantern Works", [docs], limit=3)
        self.assertEqual(about, ["Lantern Works adds lanterns."])


class RealBrowserFolderIsOutOfReach(unittest.TestCase):
    def test_the_browser_folder_is_private_during_tests(self):
        real = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "MWManager" / "NexusBrowser"
        root = mwnexus_browser._root()
        self.assertNotEqual(os.path.normcase(str(root.resolve())), os.path.normcase(str(real.resolve())))
        self.assertEqual(mwnexus_browser.close_browser(timeout=0.1), "not-running")


if __name__ == "__main__":
    unittest.main()
