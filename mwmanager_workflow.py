"""Native Tk workflow presentation and cooperative cancellation.

Cancellation is checked at explicit safe boundaries, never by killing a thread
in the middle of a journaled file operation. No game files are written here.
"""
from __future__ import annotations

import os
import re
import time
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from mwmanager_theme import COLORS, FONTS, center

FORCE_STOP_DELAY_MS = 30000      # a stop request still unanswered after this offers Force stop


class JobCancelled(Exception):
    """The current operation reached a safe stopping point."""


class JobControl:
    def __init__(self):
        self.cancelled = threading.Event()

    def check(self):
        if self.cancelled.is_set():
            raise JobCancelled("Stopped at a safe boundary. Completed work has been kept.")


def checkpoint(progress):
    """Legacy/test progress callbacks need not implement cancellation."""
    check = getattr(progress, "check_cancelled", None)
    if check:
        check()


def preflight_signature(game):
    """Cheap same-session invalidation, not a replacement for first-run hashing."""
    game = Path(game).resolve()
    paths = [game / name for name in (
        "Morrowind.exe", "MWSE.dll", "mwse-version.txt", "Morrowind Code Patch.exe",
        "d3d8.dll", "MGEXEgui.exe", "mgeXE.toml", "mcpatch/describe.json",
    )]
    paths += [game / "Data Files" / f"{name}.{ext}"
              for name in ("Morrowind", "Tribunal", "Bloodmoon") for ext in ("esm", "bsa")]
    result = [str(game)]
    for path in paths:
        try:
            stat = path.stat()
            result.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            result.append((str(path), None))
        except OSError:
            return None                       # unreadable: never reuse the cache
    return tuple(result)


def log_tone(message):
    """Only diagnostic prefixes carry severity; mod/file names are not errors."""
    text = str(message).lstrip()
    if re.match(r"(?:error|failed|failure|fatal|traceback|refused)(?:\b|:)", text, re.I):
        return "error"
    if re.match(r"(?:warning|warn|waiting|opening)(?:\b|:)", text, re.I):
        return "attention"
    if re.match(r"(?:complete|completed|finished|ready|done)(?:\b|:)", text, re.I):
        return "complete"
    return "progress"


MIN_COUNTER = 20          # smaller counters ("1 of 1", "3 of 10") are left to the moving bar


def progress_fraction(message):
    """Only explicit counters are determinate; never invent time estimates."""
    match = re.search(r"(?:^|\b)(\d[\d,]*)(?:\s+of\s+|\s*/\s*)(\d[\d,]*)\b", message, re.I)
    if not match:
        return None
    current, total = (int(value.replace(",", "")) for value in match.groups())
    return (current, total) if total > 0 and 0 <= current <= total else None


def is_action_line(message):
    """A top-level line that says what is happening now: not detail, separators, headings or bare paths."""
    text = str(message)
    line = text.strip()
    return (bool(line) and text[:1] not in (" ", "\t") and re.match(r"[A-Za-z\[]", line) is not None
            and not line.split("(")[0].strip().isupper() and not re.match(r"[A-Za-z]:\\", line))


_INDENT_RE = re.compile(r"[ \t]*")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:\\")
_PACKAGE_RE = re.compile(r"^(?:Tidy|Options) (?!changes:)(.+?): ")
_LUA_PATCHED_RE = re.compile(r"^\s+patched (.+?): (\S.*?) \(([A-Z]{1,3}\d+(?:, [A-Z]{1,3}\d+)*)\)\s*$")
_LUA_REPORT_RE = re.compile(r"^\s+lua \S.*\.lua\s*$", re.I)
_LUA_END_RE = re.compile(r"^\s+=> (?:NOT )?PATCHED\b")
_LUA_SUMMARY_RE = re.compile(r"^\s*patched (\d+) Lua file\(s\)")
_TIDY_CONTEXT_RE = re.compile(r"^(?:Tidy |Options |Running Tidy|vortex_tidy plan|ASSET CHECK|Asset |Tidy changes)")
_LEAVE_TIDY_RE = re.compile(r"^(?:# --full step|--full|==== |Preflight:|Cataloguing|Sorting)")
# top-level lines MWManager itself prints: one of them always ends a Lua report a "=>" line did not close
_TOP_LEVEL_RE = re.compile(r"^(?:(?:Tidy|Options) [^:]+: |Tidy changes: |Running Tidy |Finished |Checking |Saved |Collecting |Preparing |Staging "
                           r"|Extracting |Staged |Asset |Missing assets |Applying |Deploy|Cataloguing|# --full|--full"
                           r"|==== |MWMANAGER-STAGE:)")
_DROP_TOP_RE = re.compile(r"^(?:={10,}\s*$|ASSET CHECK\b|vortex_tidy plan,|Full plan:|Plan\s*$|Lua check:"
                          r"|Nothing changed\. Purge in Vortex|The plan matches the one you reviewed)")
_DROP_DETAIL_RE = re.compile(r"^\s+(?:ERROR|WARNING|WARN|INFO|NOTE|FIXED|REPAIRED|PATCHED|NOT PATCHED|SKIPPED)\b"
                             r"|^\s+->|^\s+\d+ texture\(s\) without mipmaps")


def _short_package(name):
    """'Patch for Purists 45096 5.0.7 2026-09-30T01-36Z Le0Pe4QA' -> 'Patch for Purists' (Nexus id onward cut)."""
    short = re.split(r"[ _-]\d{3,6}(?=[ _-])", name, maxsplit=1)[0].strip(" _-")
    return short or name


class UserOutput:
    """What the job window shows of a step's output. Tidy and luafix print their full CLI report (the replacement
    Lua of each patch, every asset repaired, the plan and journal paths, --apply hints); that is kept in the detail
    log and the Tidy journal, and the window gets at most one plain line per package for it instead."""

    def __init__(self):
        self.package = ""
        self.in_tidy = False
        self.in_lua = False
        self.skip_children_of = None       # indent of a dropped line: its wrapped continuation lines go too
        self.lua_files, self.lua_codes = set(), set()

    def _lua_summary(self, files=None):
        count = len(self.lua_codes)
        scripts = files if files is not None else len(self.lua_files)
        where = f" for {_short_package(self.package)}" if self.package else ""
        self.lua_files, self.lua_codes = set(), set()
        if not count and not scripts:
            return []
        issues = f"{count} known issue(s)" if count else "known issues"
        return [f"Fixed {issues} in {scripts} Lua script(s){where} (details in the Tidy journal)"]

    def feed(self, message):
        """The lines to show for one line of output (none, the line, or a shorter version of it)."""
        text = str(message).rstrip("\r\n")
        if text.startswith("MWMANAGER-STAGE:"):
            return [text]
        if text.startswith("MWMANAGER-DETAIL:"):
            return []                      # detail-log only (append() has already recorded it there)
        indent = len(_INDENT_RE.match(text).group(0))
        stripped = text.strip()
        package = _PACKAGE_RE.match(text)
        if package:
            self.package = package.group(1)
        if _LEAVE_TIDY_RE.match(text):
            self.in_tidy = False
        elif _TIDY_CONTEXT_RE.match(text):
            self.in_tidy = True
        patched = _LUA_PATCHED_RE.match(text)
        if patched:
            self.in_lua, self.skip_children_of = True, None
            self.lua_files.add(patched.group(2).casefold())
            self.lua_codes.update((patched.group(2).casefold(), c) for c in patched.group(3).split(", "))
            return []
        if _LUA_REPORT_RE.match(text):
            self.in_lua, self.skip_children_of = True, None
            return []
        if self.in_lua:
            if _LUA_END_RE.match(text):
                self.in_lua = False
                return []
            if not (indent == 0 and _TOP_LEVEL_RE.match(text)) and not _LUA_SUMMARY_RE.match(text):
                return []                  # replacement Lua and its findings
            self.in_lua = False
        if self.skip_children_of is not None:
            if stripped and indent > self.skip_children_of:
                return []
            self.skip_children_of = None
        summary = _LUA_SUMMARY_RE.match(text)
        if summary:
            self.skip_children_of = indent
            return self._lua_summary(int(summary.group(1)))
        if not self.in_tidy:
            return [text]
        if _DROP_TOP_RE.match(stripped) or _DRIVE_PATH_RE.match(stripped) or _DROP_DETAIL_RE.match(text) \
                or (self.package and stripped == self.package):
            self.skip_children_of = indent
            return []
        if stripped.endswith("Journal:"):
            self.skip_children_of = indent
            return [text[:text.rfind("Journal:")].rstrip()]
        text = re.sub(r"\s*\((?:details in|run with --apply)[^)]*\)", "", text)
        text = re.sub(r";\s*each is listed in \S.*$", "", text)
        text = re.sub(r"\s*\(originals (?:in|kept)[^)]*\)?\s*$", "", text)
        return [text] if text.strip() else []


PHASES = (
    ("Paths", ("paths_confirmed",)),
    ("Prerequisites", ("preflight_complete",)),
    ("Sort and Tidy", ("sort_complete", "tidy_complete")),
    ("Checks and patches", ("initial_full_complete",)),
    ("Catalog", ("catalog_complete",)),
    ("Stage major masters", ("initialization_complete",)),
)


class JobView:
    """A reusable setup shell; ordinary jobs also use the same presentation."""
    def __init__(self, owner, *, setup=False):
        self.owner, self.setup = owner, setup
        self.window = tk.Toplevel(owner)
        self.window.title("MWManager setup" if setup else "MWManager")
        self.window.transient(owner)
        self.window.resizable(True, True)
        width = min(1100 if setup else 940, self.window.winfo_screenwidth() - 48)
        height = min(650, self.window.winfo_screenheight() - 80)
        self.window.minsize(min(680, width), min(420, height))
        center(self.window, owner, width, height)
        outer = ttk.Frame(self.window, padding=16)
        outer.pack(fill="both", expand=True)
        self.phases = {}
        if setup:
            sidebar = ttk.Frame(outer, padding=(0, 0, 16, 0))
            sidebar.pack(side="left", fill="y")
            ttk.Label(sidebar, text="SETUP", style="H2.TLabel").pack(anchor="w", pady=(0, 12))
            for label, _steps in PHASES:
                var = tk.StringVar(value=f"Pending  {label}")
                ttk.Label(sidebar, textvariable=var, wraplength=180).pack(anchor="w", pady=7)
                self.phases[label] = var
        frame = ttk.Frame(outer)
        frame.pack(side="left", fill="both", expand=True)
        self.title = tk.StringVar()
        self.stage = tk.StringVar(value="Starting...")
        self.description = tk.StringVar()
        self.activity = tk.StringVar()      # the latest top-level action, under the coarser stage
        ttk.Label(frame, textvariable=self.title, style="H2.TLabel").pack(anchor="w")
        ttk.Label(frame, textvariable=self.description, style="Muted.TLabel",
                  wraplength=650, justify="left").pack(fill="x", pady=(6, 12))
        ttk.Label(frame, textvariable=self.stage, style="H3.TLabel", wraplength=650).pack(fill="x", pady=(0, 2))
        ttk.Label(frame, textvariable=self.activity, style="Muted.TLabel", wraplength=650).pack(fill="x", pady=(0, 8))
        logs = ttk.Frame(frame)
        logs.pack(fill="both", expand=True)
        self.log = tk.Text(logs, height=14, width=70, wrap="word", state="disabled",
                           background=COLORS["field"], foreground=COLORS["text"],
                           font=FONTS["mono"], relief="flat", padx=10, pady=8)
        scroll = ttk.Scrollbar(logs, command=self.log.yview, style="Job.Vertical.TScrollbar")
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for name, color in (("progress", "text"), ("complete", "green"),
                            ("attention", "gold"), ("error", "red")):
            self.log.tag_configure(name, foreground=COLORS[color], lmargin2=16)
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(12, 8))
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text="Follow output", variable=self.follow,
                        command=self._follow_changed).pack(side="left")
        self.cancel = ttk.Button(actions, text="Stop after current operation")
        self.cancel.pack(side="right")
        self.resume = ttk.Button(actions, text="Resume setup", command=owner._resume_setup)
        self.close = ttk.Button(actions, text="Close", command=self.window.withdraw)
        self.force = ttk.Button(actions, text="Force stop", command=self.force_stop)
        self.control = None

    def _follow_changed(self):
        if self.follow.get():
            self.log.see("end")

    status_log = None           # the GUI sets outputs\mwmanager_status.log: everything the window shows, kept
    _fresh_logs = set()         # logs already started by this MWManager session

    @staticmethod
    def detail_log_path(status_log):
        """outputs\\mwmanager_status_detail.log: every line of output, the full Tidy/luafix reports included."""
        log = Path(status_log)
        return log.with_name(log.stem + "_detail" + log.suffix)

    def _record(self, text, detail=False):
        if not self.status_log:
            return
        try:
            log = self.detail_log_path(self.status_log) if detail else Path(self.status_log)
            log.parent.mkdir(parents=True, exist_ok=True)
            key = os.path.normcase(str(log.resolve()))
            if key not in JobView._fresh_logs:                       # a new session: a new log, the last one kept
                JobView._fresh_logs.add(key)
                if log.is_file():
                    os.replace(log, log.with_suffix(".prev.log"))
            with open(log, "a", encoding="utf-8") as f:
                f.write(text.rstrip() + "\n")
        except OSError:
            pass                                                     # the window still shows it

    def begin(self, title, description, control):
        header = f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')}  {title} ===="
        self._record(header)
        self._record(header, detail=True)
        self.user_output = UserOutput()
        self.control = control
        self.title.set(title)
        self.description.set(description)
        self.stage.set("Starting...")
        self.activity.set("")
        self.resume.pack_forget()
        self.close.pack_forget()
        self.force.pack_forget()
        self.force.configure(state="normal")
        self.cancel.configure(state="normal", command=self.request_cancel)
        self.cancel.pack(side="right")
        self.window.protocol("WM_DELETE_WINDOW", self.request_cancel)
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(50)
        self.window.deiconify()
        self.window.lift()
        self.window.grab_set()

    def request_cancel(self):
        if self.control:
            self.control.cancelled.set()
            self.stage.set("Stopping after the current operation; finishing safe writes first.")
            self.cancel.configure(state="disabled")
            control = self.control
            self.window.after(FORCE_STOP_DELAY_MS, lambda: self._offer_force_stop(control))

    def _offer_force_stop(self, control):
        if control is not None and self.control is control:      # the same job is still running
            self.force.pack(side="right", padx=6)

    def force_stop(self):
        """A step that hangs after a stop request: end its processes and let the window go. The step is left
        unfinished, so Resume runs it again."""
        import managed_processes
        managed_processes.end_all()
        self.force.configure(state="disabled")
        self.stage.set("Force stopped. The step did not finish; Resume setup runs it again.")
        self.window.grab_release()
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.close.pack(side="right")

    user_output = None

    def append(self, message):
        self._record(str(message), detail=True)
        if self.user_output is None:
            self.user_output = UserOutput()
        for text in self.user_output.feed(message):
            self._show(text)

    def _show(self, text):
        self._record(text)
        stopping = self.control is not None and self.control.cancelled.is_set()
        if text.startswith("MWMANAGER-STAGE:"):
            if not stopping:
                self.stage.set(text.split(":", 1)[1].strip())
        else:
            if not stopping and text.startswith("# --full step"):
                # a new --full step names itself; the title still said the last step that set one
                self.stage.set(text.lstrip("# ").strip()[:180])
            elif not stopping and text.startswith(("Preflight:", "Staging master ", "Cataloguing", "Tidying", "Sorting",
                                                 "Opening", "Checking BSA", "Running tidy")):
                self.stage.set(text.strip()[:180])
            elif not stopping and is_action_line(text):
                self.activity.set(text.strip()[:180])
            self.log.configure(state="normal")
            self.log.insert("end", text.rstrip() + "\n", log_tone(text))
            excess = int(self.log.index("end-1c").split(".")[0]) - 4000
            if excess > 0:
                self.log.delete("1.0", f"{excess + 1}.0")
            if self.follow.get():
                self.log.see("end")
            self.log.configure(state="disabled")
        fraction = progress_fraction(text)
        # a counter only fills the bar when it counts enough to show progress; "0 of 1" then "1 of 1" (one per
        # package) flipped it empty to full over and over. Once a counter is done, the bar moves on its own again.
        if fraction and fraction[1] >= MIN_COUNTER and (text.startswith("MWMANAGER-STAGE:") or is_action_line(text)):
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=fraction[1], value=fraction[0])
        elif (str(self.progress.cget("mode")) == "determinate" and is_action_line(text)
              and float(self.progress.cget("value")) >= float(self.progress.cget("maximum"))):
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(50)

    def refresh_phases(self):
        if not self.setup:
            return
        import mwmanager_setup_state as state
        saved = state.load(self.owner._setup_path(), self.owner.settings["game_dir"])["steps"]
        for label, steps in PHASES:
            entries = [saved[step] for step in steps]
            prefix = ("Done" if all(e.get("completed_at") for e in entries)
                      else "Paused" if any(e.get("last_error") for e in entries)
                      else "Current" if any(e.get("started_at") for e in entries) else "Pending")
            if label == "Stage major masters" and prefix == "Pending" and saved["major_masters_started"].get("started_at"):
                prefix = "Current"
            self.phases[label].set(f"{prefix}  {label}")

    def finish(self, error=None):
        self.progress.stop()
        self.cancel.pack_forget()
        self.force.pack_forget()
        self.control = None
        self.window.grab_release()      # the job is over: begin()'s input grab must not outlive it
        self.refresh_phases()
        self.stage.set("Paused. Resume setup when ready." if error else "Step complete. Continuing setup...")
        self.activity.set("")
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.close.pack(side="right")
        if self.setup and error is not None:
            self.resume.pack(side="right", padx=6)


def conflict_answer(title, details, choices, default="s"):
    """A native conflict picker in the scanner subprocess; no console required."""
    root = tk.Tk()
    root.withdraw()
    from mwmanager_theme import apply_theme
    try:
        apply_theme(root)
        window = tk.Toplevel(root)
        window.title(title)
        window.minsize(560, 400)
        width = min(820, window.winfo_screenwidth() - 48)
        height = min(720, window.winfo_screenheight() - 80)
        center(window, None, width, height)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, style="H2.TLabel", wraplength=700).pack(anchor="w", pady=(0, 10))
        box = ttk.Frame(frame)
        box.pack(fill="both", expand=True)
        text = tk.Text(box, wrap="word", width=70, height=10, relief="flat", padx=10, pady=8)
        text.insert("1.0", details)
        text.configure(state="disabled")
        scrollbar = ttk.Scrollbar(box, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        selection = tk.StringVar(value=default)
        for label, value in choices:
            # A wrapping native radio button avoids off-screen action buttons
            # when filenames are long. The single Apply button stays predictable.
            tk.Radiobutton(frame, text=label, variable=selection, value=value,
                           wraplength=max(400, width - 80), anchor="w", justify="left",
                           background=COLORS["canvas"], foreground=COLORS["text"],
                           selectcolor=COLORS["field"], activebackground=COLORS["surface"],
                           activeforeground=COLORS["text"]).pack(fill="x", pady=2)
        result = {"value": "q"}

        def choose(value):
            result["value"] = value
            window.destroy()

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Apply choice", style="Primary.TButton",
                   command=lambda: choose(selection.get())).pack(side="right")
        ttk.Button(actions, text="Finish review", command=lambda: choose("q")).pack(side="right", padx=8)
        window.protocol("WM_DELETE_WINDOW", lambda: choose("q"))
        window.bind("<Escape>", lambda _event: choose("q"))
        window.bind("<Return>", lambda _event: choose(selection.get()))
        window.lift()
        window.attributes("-topmost", True)
        window.after(500, lambda: window.attributes("-topmost", False))
        window.focus_force()
        window.grab_set()
        window.wait_window()
        return result["value"]
    finally:
        root.destroy()
