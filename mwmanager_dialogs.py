"""Themed prompts for MWManager: the same calls as tkinter.messagebox and simpledialog.askstring.

The system's own message boxes ignore the theme and place themselves. These are Tk windows in the shared
"ash and brass" theme, centred on the window that asked (or on the desktop when there is none on screen).
Import it under the old name and nothing else changes:

    import mwmanager_dialogs as messagebox
    messagebox.showerror("Could not save", str(error), parent=self)
    if messagebox.askyesno("Remove?", "Remove the selected plugins?", parent=dialog): ...

Return values match tkinter: show* return "ok", askyesno a bool, askyesnocancel True / False / None,
askstring the text or None. Folder and file pickers stay the system's (tkinter.filedialog).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk

from mwmanager_theme import COLORS, SPACE, apply_theme, center

_TONES = {"info": "gold", "question": "gold", "warning": "amber", "error": "red"}
WRAP = 460                      # px: message text wraps here, so a prompt is never wider than a small dialog


def _owner(parent):
    """(the window to belong to, a temporary root to destroy afterwards or None)."""
    if parent is not None:
        return parent, None
    root = getattr(tk, "_default_root", None)
    if root is not None:
        return root, None
    root = tk.Tk()
    root.withdraw()
    return root, root


def _themed(widget):
    """Apply the theme to a root that never had it (a helper script's hidden root)."""
    root = widget._root()
    if ttk.Style(root).lookup("TFrame", "background") != COLORS["canvas"]:
        apply_theme(root)


def _prompt(title, message, tone, buttons, default, cancel, parent=None, entry=None, **_ignored):
    """buttons: [(label, value)]. Returns the chosen value (cancel's value when the window is closed);
    with entry={"show": ..., "initialvalue": ...} the first button returns the text typed."""
    owner, temporary = _owner(parent)
    try:
        _themed(owner)
        window = tk.Toplevel(owner)
        window.withdraw()                                   # placed before it is shown: no jump
        window.title(title or "MWManager")
        window.resizable(True, True)
        result = {"value": cancel}

        frame = ttk.Frame(window, padding=SPACE["xl"])
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title or "", style=f"{_TONES.get(tone, 'gold').title()}.TLabel",
                  font="TkHeadingFont", wraplength=WRAP, justify="left").pack(anchor="w")
        # Keep decisions and their buttons reachable on small displays. A label
        # grows without a bound when preflight lists many blockers or file paths.
        if len(message or "") > 650 or len((message or "").splitlines()) > 10:
            details_box = ttk.Frame(frame)
            details_box.pack(fill="both", expand=True, pady=(SPACE["sm"], SPACE["lg"]))
            details = tk.Text(details_box, wrap="word", width=68, relief="flat", padx=8, pady=8)
            font = tkfont.Font(font=details.cget("font"))
            room = font.measure("0" * 68)
            # tall enough for the whole message (a word-wrap estimate plus one line), scrolling only past the screen
            needed = sum(max(1, -(-font.measure(line) // room)) for line in (message or "").splitlines()) + 1
            fits = max(7, (window.winfo_screenheight() - 260) // font.metrics("linespace"))
            details.configure(height=min(needed, fits))
            details.insert("1.0", message or "")
            details.configure(state="disabled")
            scroll = ttk.Scrollbar(details_box, command=details.yview)
            details.configure(yscrollcommand=scroll.set)
            details.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")
        else:
            ttk.Label(frame, text=message or "", wraplength=WRAP, justify="left").pack(
                anchor="w", pady=(SPACE["sm"], SPACE["lg"]))
        field = None
        if entry is not None:
            text = tk.StringVar(value=entry.get("initialvalue") or "")
            field = ttk.Entry(frame, textvariable=text, show=entry.get("show") or "", width=48)
            field.pack(fill="x", pady=(0, SPACE["lg"]))

        def choose(value):
            result["value"] = text.get() if (entry is not None and value is True) else value
            window.destroy()

        row = ttk.Frame(frame)
        row.pack(fill="x")
        focus = None
        for index, (label, value) in enumerate(buttons):
            button = ttk.Button(row, text=label, command=lambda v=value: choose(v),
                                style="Primary.TButton" if value == default else "TButton")
            # Conflict choices can have long plugin names. Stack them instead of
            # forcing an enormous, off-screen single button row.
            if len(buttons) > 3 or any(len(label) > 35 for label, _value in buttons):
                button.grid(row=index, column=0, sticky="ew", pady=3)
                row.columnconfigure(0, weight=1)
            else:
                button.pack(side="left", padx=(0, SPACE["sm"]))
            if value == default:
                focus = button
        window.protocol("WM_DELETE_WINDOW", lambda: choose(cancel))
        window.bind("<Escape>", lambda _event: choose(cancel))
        window.bind("<Return>", lambda _event: choose(default))

        visible = None
        if parent is not None:
            try:
                top = parent.winfo_toplevel()
                visible = top if top.winfo_viewable() else None
            except tk.TclError:
                visible = None
        if visible is not None:
            window.transient(visible)
        center(window, visible)
        window.deiconify()
        window.lift()
        (field or focus or window).focus_set()
        try:
            window.grab_set()
        except tk.TclError:
            pass                                            # another grab is active: still shown, just not modal
        window.wait_window()
        return result["value"]
    finally:
        if temporary is not None:
            temporary.destroy()


def showinfo(title=None, message=None, **options):
    return _prompt(title, message, "info", [("OK", "ok")], "ok", "ok", **options)


def showwarning(title=None, message=None, **options):
    return _prompt(title, message, "warning", [("OK", "ok")], "ok", "ok", **options)


def ask_continue_dont_warn(title=None, message=None, **options):
    """Non-blocking warning: Continue anyway, or Don't warn again for this exact version.

    Returns 'continue' or 'mute'. Closing the window continues; Play is never blocked.
    """
    return _prompt(
        title, message, "warning",
        [("Continue anyway", "continue"), ("Don't warn again", "mute")],
        "continue", "continue", **options)


def showerror(title=None, message=None, **options):
    return _prompt(title, message, "error", [("OK", "ok")], "ok", "ok", **options)


def _tone(options, fallback="question"):
    return options.pop("icon", None) or fallback


def _default(options, yes=True):
    """tkinter's default="no" / "cancel" picks the button Return presses."""
    return {"yes": True, "no": False, "cancel": None}.get(options.pop("default", None), yes)


def askyesno(title=None, message=None, **options):
    tone, default = _tone(options), _default(options)
    return bool(_prompt(title, message, tone, [("Yes", True), ("No", False)], default, False, **options))


def askyesnocancel(title=None, message=None, **options):
    tone, default = _tone(options), _default(options)
    return _prompt(title, message, tone, [("Yes", True), ("No", False), ("Cancel", None)], default, None, **options)


def askstring(title=None, prompt=None, **options):
    entry = {"show": options.pop("show", None), "initialvalue": options.pop("initialvalue", None)}
    return _prompt(title, prompt, "question", [("OK", True), ("Cancel", None)], True, None, entry=entry, **options)
