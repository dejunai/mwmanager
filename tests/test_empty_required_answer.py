"""A saved answer that leaves a must-choose group (SelectExactlyOne / SelectAtLeastOne) empty, e.g. after an update
renamed the option it named, is not replayed as is: the window pre-fills that group's defaults so Apply goes
through, and the no-window reuse path opens the window instead of installing the package with the group empty."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mock_fomod
import mwmanager_dialogs


def option(mod, key, label, group, rule, number=0, compatible=(), required=False):
    return {"key": key, "label": label, "path": mod, "mod_dir": mod, "number": number, "required": required,
            "targets": list(compatible), "compatible": list(compatible), "missing_masters": [], "docs": "",
            "group_key": group, "group_label": group, "selection_type": rule}


class EmptyRequiredAnswerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = Path(tmp.name) / "Snow Prince"
        self.mod.mkdir()
        self.records = [
            option(self.mod, "fomod:0:0:0", "GitD Integration", "fomod:0:0", "SelectExactlyOne"),
            option(self.mod, "fomod:0:0:1", "No GitD", "fomod:0:0", "SelectExactlyOne"),
            option(self.mod, "fomod:1:0:0", "Extra A", "fomod:1:0", "SelectAny", number=1),
            option(self.mod, "fomod:1:0:1", "Extra B", "fomod:1:0", "SelectAny", number=1),
        ]

    def test_empty_required_groups(self):
        empty = mock_fomod._empty_required_groups
        self.assertEqual(empty(self.records, {"fomod:1:0:0"}), {"fomod:0:0"})
        self.assertEqual(empty(self.records, {"fomod:0:0:1"}), set())
        self.assertEqual(empty(self.records, set()), {"fomod:0:0"})          # an empty SelectAny group is fine
        at_least = [dict(r, selection_type="SelectAtLeastOne") for r in self.records[:2]]
        self.assertEqual(empty(at_least, set()), {"fomod:0:0"})
        at_most = [dict(r, selection_type="SelectAtMostOne") for r in self.records[:2]]
        self.assertEqual(empty(at_most, set()), set())
        locked = [dict(self.records[0], required=True), self.records[1]]
        self.assertEqual(empty(locked, set()), set())                       # a required option picks itself

    def run_selector(self, records, previous, answered):
        import tkinter as tk
        warned = []

        def apply(root):
            for widget in self.walk(root):
                if widget.winfo_class() == "TButton" and widget.cget("text") == "Apply selected options":
                    widget.invoke()
                    return

        with mock.patch.object(tk.Tk, "mainloop", apply), \
                mock.patch.object(mwmanager_dialogs, "showwarning", side_effect=lambda *a, **k: warned.append(a)), \
                mock.patch.object(mock_fomod, "_nexus_mod_id", return_value=""), \
                mock.patch.object(mock_fomod, "_nexus_page_url", return_value=""), \
                mock.patch.object(mock_fomod, "_dev_requirement_editor_enabled", return_value=False), \
                mock.patch.object(mock_fomod, "_dev_insight_editor_enabled", return_value=False):
            try:
                chosen = mock_fomod._show_selector("Snow Prince", records, set(previous), set(), set(answered))
            except tk.TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
        return chosen, warned

    def walk(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.walk(child)

    def test_window_prefills_a_group_its_saved_answer_left_empty(self):
        answered = {"fomod:0:0", "fomod:1:0"}
        chosen, warned = self.run_selector(self.records, {"fomod:1:0:1"}, answered)
        self.assertEqual(warned, [])                                        # Apply went through
        self.assertIsNotNone(chosen)
        self.assertIn("fomod:0:0:0", chosen)                                # the first choice stands in
        self.assertIn("fomod:1:0:1", chosen)                                # other answers kept
        self.assertNotIn("fomod:1:0:0", chosen)

    def test_stale_group_prefers_its_compatible_default(self):
        records = [option(self.mod, "fomod:0:0:0", "GitD Integration", "fomod:0:0", "SelectExactlyOne"),
                   option(self.mod, "fomod:0:0:1", "TD Integration", "fomod:0:0", "SelectExactlyOne",
                          compatible=["tamriel_data.esm"])]
        chosen, warned = self.run_selector(records, set(), {"fomod:0:0"})
        self.assertEqual(warned, [])
        self.assertEqual(set(chosen) & {"fomod:0:0:0", "fomod:0:0:1"}, {"fomod:0:0:1"})

    def test_an_empty_optional_answer_still_wins(self):
        chosen, warned = self.run_selector(self.records, {"fomod:0:0:1"}, {"fomod:0:0", "fomod:1:0"})
        self.assertEqual(warned, [])
        self.assertIn("fomod:0:0:1", chosen)
        self.assertFalse({"fomod:1:0:0", "fomod:1:0:1"} & set(chosen))

    def test_reuse_path_does_not_replay_an_empty_required_group(self):
        source = Path(mock_fomod.__file__).read_text(encoding="utf-8")
        reuse_checks = [line for line in source.splitlines() if "_reuse_saved_answers() and" in line]
        self.assertEqual(len(reuse_checks), 2)
        follow = source.split("_reuse_saved_answers() and")
        for part in follow[1:]:
            self.assertIn("not _empty_required_groups(records, previous)", part[:200])


if __name__ == "__main__":
    unittest.main()