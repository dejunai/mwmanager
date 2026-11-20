"""Prompts are MWManager's themed ones (not the system's), and windows open centred."""
import re
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWN = sorted(p for p in ROOT.glob("*.py") if not p.name.startswith("mwdialog_") and p.name != "vortex_tidy.py")


class NoSystemPromptsOrLooseColours(unittest.TestCase):
    def test_own_modules_do_not_use_the_systems_message_boxes(self):
        for path in OWN:
            if path.name == "mwmanager_dialogs.py":             # its docstring names what it replaces
                continue
            text = path.read_text("utf-8")
            self.assertIsNone(re.search(r"tkinter import [^\n]*\b(messagebox|simpledialog)\b|tkinter\.(messagebox|simpledialog)",
                                        text), path.name)

    def test_colours_come_from_the_theme(self):
        for path in OWN:
            if path.name == "mwmanager_theme.py":
                continue
            for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
                if re.search(r"(foreground|background|fg|bg)\s*=\s*[\"']#[0-9a-fA-F]{6}", line):
                    self.fail(f"{path.name}:{number}: a colour outside mwmanager_theme.COLORS")

    def test_every_toplevel_is_placed(self):
        """A dialog is centred by center()/center_window(), or sets its own position (WxH+X+Y)."""
        for path in OWN:
            lines = path.read_text("utf-8").splitlines()
            for i, line in enumerate(lines):
                m = re.search(r"(\w+(?:\.\w+)?)\s*=\s*tk\.Toplevel\(", line)
                if not m or "class tooltip" in "\n".join(lines[max(0, i - 40):i + 1]).casefold():
                    continue                                    # a tooltip follows the pointer
                name = re.escape(m.group(1))
                body = "\n".join(lines[i:i + 140])
                placed = re.search(rf"center(_window)?\(\s*{name}\b|{name}\.geometry\(f\"[^\"]*\+", body)
                self.assertTrue(placed, f"{path.name}:{i + 1}: {m.group(1)} is never placed")


class ThemedPrompts(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        from mwmanager_theme import apply_theme
        apply_theme(self.root)
        self.root.geometry("800x600+200+120")
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def _press(self, label, seen, typed=None):
        def act():
            top = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
            top.update_idletasks()
            seen.update(x=top.winfo_rootx(), y=top.winfo_rooty(), w=top.winfo_width(), h=top.winfo_height(),
                        bg=str(top.cget("background")))
            stack = [top]
            while stack:
                widget = stack.pop()
                stack.extend(widget.winfo_children())
                if typed is not None and widget.winfo_class() == "TEntry":
                    widget.insert(0, typed)
                if widget.winfo_class() == "TButton" and str(widget.cget("text")) == label:
                    button = widget
            button.invoke()
        self.root.after(250, act)

    def test_answers_match_tkinter_and_the_prompt_is_themed_and_centred(self):
        import mwmanager_dialogs as dialogs
        from mwmanager_theme import COLORS
        seen = {}
        self._press("Yes", seen)
        self.assertIs(dialogs.askyesno("Remove?", "Remove the selected plugins?", parent=self.root), True)
        self.assertEqual(seen["bg"], COLORS["canvas"])
        centre_x = self.root.winfo_rootx() + self.root.winfo_width() // 2
        centre_y = self.root.winfo_rooty() + self.root.winfo_height() // 2
        self.assertLess(abs(seen["x"] + seen["w"] // 2 - centre_x), 30)
        self.assertLess(abs(seen["y"] + seen["h"] // 2 - centre_y), 40)

        self._press("No", seen)
        self.assertIs(dialogs.askyesno("Remove?", "Sure?", parent=self.root), False)
        self._press("Cancel", seen)
        self.assertIsNone(dialogs.askyesnocancel("Stage?", "Stage these masters?", parent=self.root))
        self._press("OK", seen)
        self.assertEqual(dialogs.showerror("Failed", "It did not work.", parent=self.root), "ok")
        self._press("OK", seen, typed="example_data.esm")
        self.assertEqual(dialogs.askstring("Add major master", "Master filename:", parent=self.root), "example_data.esm")
        self._press("Cancel", seen)
        self.assertIsNone(dialogs.askstring("Add major master", "Master filename:", parent=self.root))

    def test_a_long_message_shows_whole_without_scrolling(self):
        import mwmanager_dialogs as dialogs
        from preinstall import MCP_PATCH_GUIDANCE
        seen = {}

        def look():
            top = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
            top.update()
            stack = [top]
            while stack:
                widget = stack.pop()
                stack.extend(widget.winfo_children())
                if widget.winfo_class() == "Text":
                    seen["view"] = widget.yview()
                if widget.winfo_class() == "TButton" and str(widget.cget("text")) == "No":
                    button = widget
            button.invoke()
        self.root.after(300, look)
        dialogs.askyesno("Configure and apply Morrowind Code Patch",
                         MCP_PATCH_GUIDANCE + "\n\nOpen the MCP interface now?", parent=self.root)
        self.assertEqual(seen["view"], (0.0, 1.0))


if __name__ == "__main__":
    unittest.main()
