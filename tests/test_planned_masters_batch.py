"""Masters 'later in this install batch' come from the running major-master batch, not from the whole downloads
scan: outputs\\mwmanager_core_addon_plan.json lists every master found in the downloads, including ones unticked in
Review major masters. CR-27 loop 1 (seed 2107305417): Wares-base deployed without OAAB_Data / Cyr_Main, but the
plan still listed both, so 'OAAB to Wares' and the PC options were not held and --full failed on their plugins."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod  # noqa: E402

PLAN = ["Patch for Purists.esm", "Tamriel_Data.esm", "OAAB_Data.esm", "Wares-base.esm", "TR_Mainland.esm",
        "Cyr_Main.esm"]


def batch(out, masters, kind="major_masters"):
    (out / mock_fomod.ACTIVE_BATCH_FILE).write_text(json.dumps({
        "version": 1, "kind": kind, "packages": {
            f"p{i}": {"id": f"p{i}", "selected_masters": [m]} for i, m in enumerate(masters)}}), encoding="utf-8")


class PlannedMastersTests(unittest.TestCase):
    def run_staging(self, setup, chosen=frozenset()):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "stage" / "Wares"
            option = package / "Extras" / "04 OAAB to Wares"
            option.mkdir(parents=True)
            (option / "Wares_lists_OAAB.ESP").write_bytes(b"TES3")
            (package / "Wares-base.esm").write_bytes(b"TES3")
            data = root / "game" / "Data Files"
            data.mkdir(parents=True)
            out = root / "outputs"
            out.mkdir()
            (out / "mwmanager_core_addon_plan.json").write_text(json.dumps({"ordered_install_masters": PLAN}),
                                                                 encoding="utf-8")
            setup(out)
            seen = []

            def discover(mod_dir, installed):
                seen.append(set(installed))
                return [{"key": "option:4", "path": option, "label": "OAAB to Wares", "targets": [], "number": 4,
                         "missing_masters": ["OAAB_Data.esm"]}]

            with mock.patch("nexus_updates.check_and_offer_update", return_value=False), \
                    mock.patch.object(mock_fomod, "_has_fomod", return_value=False), \
                    mock.patch.object(mock_fomod, "_discover", side_effect=discover), \
                    mock.patch.object(mock_fomod, "_discover_plugin_choices", return_value=[]), \
                    mock.patch.object(mock_fomod, "_show_selector", return_value=set(chosen)):
                mock_fomod.process_staging(package.parent, data, out)
            journal = json.loads((out / "mwmanager_compatibility_journal.json").read_text(encoding="utf-8"))
            held = [h for entry in journal["mods"].values() for h in entry.get("held", [])]
            return seen[0], held

    def test_unticked_masters_in_the_plan_do_not_count_while_a_batch_runs(self):
        seen, _held = self.run_staging(lambda out: batch(out, ["Wares-base.esm", "Tamriel_Data.esm"]))
        self.assertIn("tamriel_data.esm", seen)
        self.assertNotIn("oaab_data.esm", seen)
        self.assertNotIn("cyr_main.esm", seen)
        self.assertNotIn("tr_mainland.esm", seen)

    def test_option_needing_an_unticked_master_is_held(self):
        _seen, held = self.run_staging(lambda out: batch(out, ["Wares-base.esm", "Tamriel_Data.esm"]),
                                       chosen={"option:4"})
        self.assertEqual(held, ["option:4"])

    def test_masters_the_batch_stages_later_still_count(self):
        seen, held = self.run_staging(lambda out: batch(out, ["Wares-base.esm", "OAAB_Data.esm"]),
                                      chosen={"option:4"})
        self.assertIn("oaab_data.esm", seen)
        self.assertEqual(held, [])

    def test_without_a_major_master_batch_the_plan_still_counts(self):
        seen, _held = self.run_staging(lambda out: None)
        self.assertIn("tr_mainland.esm", seen)
        seen, _held = self.run_staging(lambda out: batch(out, ["Some.esp"], kind="plugins"))
        self.assertIn("tr_mainland.esm", seen)

    def test_batch_reader_ignores_missing_or_broken_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertIsNone(mock_fomod._batch_selected_masters(out))
            (out / mock_fomod.ACTIVE_BATCH_FILE).write_text("{not json", encoding="utf-8")
            self.assertIsNone(mock_fomod._batch_selected_masters(out))
            (out / mock_fomod.ACTIVE_BATCH_FILE).write_text(json.dumps({"kind": "major_masters", "packages": {
                "a": {"selected_masters": []}}}), encoding="utf-8")
            self.assertIsNone(mock_fomod._batch_selected_masters(out))
            batch(out, ["Wares-base.esm", "Tamriel_Data.esm"])
            self.assertEqual(mock_fomod._batch_selected_masters(out), {"wares-base.esm", "tamriel_data.esm"})


if __name__ == "__main__":
    unittest.main()
