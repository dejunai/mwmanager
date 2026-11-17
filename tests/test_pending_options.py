"""Batch step 1: chosen options that need a mod that isn't installed or staged are kept as waiting on it."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_fomod  # noqa: E402


def put(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class PendingOptionsTests(unittest.TestCase):
    def test_only_chosen_options_still_missing_a_master_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            records = [{"key": "01 Epic Plants", "label": "Epic Plants", "missing_masters": ["Epic Plants.esp"]},
                       {"key": "02 Glow", "label": "Glow", "missing_masters": ["Glow.esp"]},          # not chosen
                       {"key": "03 Now Here", "label": "Now Here", "missing_masters": ["Here.esp"]}]  # now installed
            waiting = mock_fomod.record_pending_options(out, Path("OAAB_Data"), records,
                                                        {"01 Epic Plants", "03 Now Here"}, {"here.ESP"})
            self.assertEqual(waiting, {"01 Epic Plants": {"label": "Epic Plants", "needs": ["Epic Plants.esp"],
                                                          "status": "waiting"},
                                       "02 Glow": {"label": "Glow", "needs": ["Glow.esp"], "status": "not chosen"}})
            saved = json.loads((out / mock_fomod.PENDING_FILE).read_text(encoding="utf-8"))
            self.assertEqual(list(saved), ["OAAB_Data"])
            mock_fomod.record_pending_options(out, Path("OAAB_Data"), records, {"01 Epic Plants"},
                                              {"Epic Plants.esp", "Glow.esp", "Here.esp"})   # all arrived: none left
            self.assertEqual(json.loads((out / mock_fomod.PENDING_FILE).read_text(encoding="utf-8")), {})

    def test_the_option_window_records_what_a_chosen_option_waits_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "stage" / "OAAB_Data"
            put(package / "Extras/01 Epic Plants/Textures/a.dds", b"optional")
            data = root / "game" / "Data Files"
            data.mkdir(parents=True)
            record = {"key": "option:1", "path": package / "Extras/01 Epic Plants", "label": "Epic Plants",
                      "targets": [], "number": 1, "missing_masters": ["Epic Plants.esp"]}
            with mock.patch("nexus_updates.check_and_offer_update", return_value=False), \
                    mock.patch.object(mock_fomod, "_has_fomod", return_value=False), \
                    mock.patch.object(mock_fomod, "_discover", return_value=[record]), \
                    mock.patch.object(mock_fomod, "_discover_plugin_choices", return_value=[]), \
                    mock.patch.object(mock_fomod, "_show_selector", return_value={"option:1"}):
                mock_fomod.process_staging(package.parent, data, root / "outputs")
            saved = json.loads((root / "outputs" / mock_fomod.PENDING_FILE).read_text(encoding="utf-8"))
            self.assertEqual(saved["OAAB_Data"]["option:1"]["needs"], ["Epic Plants.esp"])


class ArrivedModOfferTests(unittest.TestCase):
    def run_offer(self, answer):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "stage" / "OAAB_Data"
            put(package / "Extras/02 Knife Ears/OAAB Knife Ears.esp")
            data = root / "game" / "Data Files"
            put(data / "Knife Ears.esp")                                    # the mod just installed
            out = root / "outputs"
            out.mkdir()
            (out / "mwmanager_compatibility_journal.json").write_text(json.dumps({
                "version": 1, "known_installed_plugins": [], "mods": {"oaab": {
                    "mod_dir": str(package), "plugin": "OAAB_Data.esm", "targets": ["knife ears.esp"],
                    "selected": [], "reviewed_targets": []}}}), encoding="utf-8")
            record = {"key": "02 Knife Ears", "path": package / "Extras/02 Knife Ears", "label": "Knife Ears",
                      "targets": ["knife ears.esp"], "number": 2}
            with mock.patch("nexus_updates.check_and_offer_update", return_value=False), \
                    mock.patch.object(mock_fomod, "_has_fomod", return_value=False), \
                    mock.patch.object(mock_fomod, "_discover", return_value=[record]), \
                    mock.patch.object(mock_fomod, "_discover_plugin_choices", return_value=[]), \
                    mock.patch.object(mock_fomod, "ask_see_options", return_value=answer) as asked, \
                    mock.patch.object(mock_fomod, "_show_selector", return_value=None) as shown:
                try:
                    mock_fomod.process_staging(package.parent, data, out, only=["nothing-else"])
                except mock_fomod.OptionsCancelled:
                    pass
            journal = json.loads((out / "mwmanager_compatibility_journal.json").read_text(encoding="utf-8"))
            return asked.call_args.args, shown.called, journal["mods"]["oaab"]["reviewed_targets"]

    def test_the_user_is_asked_and_no_means_no_window_and_no_second_ask(self):
        args, shown, reviewed = self.run_offer(False)
        self.assertEqual(args, ("OAAB_Data.esm", ["Knife Ears.esp"], ["Knife Ears"]))
        self.assertFalse(shown)
        self.assertEqual(reviewed, ["knife ears.esp"])

    def test_yes_opens_the_option_window(self):
        _args, shown, _reviewed = self.run_offer(True)
        self.assertTrue(shown)


if __name__ == "__main__":
    unittest.main()
