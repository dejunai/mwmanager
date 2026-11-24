#!/usr/bin/env python3
"""Tk desktop front end for MWManager's plugin load order and settings."""

from __future__ import annotations

import io
import json
import configparser
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

import mwmanager_dialogs as messagebox  # themed prompts
import mwmanager_setup_state as setup_state
from mwmanager_workflow import JobCancelled, JobControl, JobView, checkpoint, preflight_signature

from mwmanager_theme import COLORS, FONTS, SPACE, apply_theme, center, master_status_legend, master_status_tags, tree_tags
from bsa_pack import BSA_OFFER_SETTING, DEFAULT_THRESHOLD as DEFAULT_BSA_OFFER, MIN_THRESHOLD as MIN_BSA_OFFER
ALWAYS_BSA_SETTING = "always_build_bsa"      # mwmanager.ini [Staging]; default off: ask per package
# mwmanager.ini [Staging], default on: Tidy/--asset-check reduce textures / splash screens wider than 2048
ASSET_LIMIT_SETTINGS = {"reduce_textures_2048": "Reduce textures larger than 2048 to 2048",
                        "reduce_splash_2048": "Reduce splash screens wider than 2048 to 2048"}

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "mwmanager_gui_settings.json"
MWMANAGER_INI = APP_DIR / "mwmanager.ini"
PATH_SETTING_KEYS = {"game_dir", "archive_downloads_dir", "always_elevate_game_dir"}
# Older mwmanager.ini files called the archive folder downloads_dir; it is still read, never written.
LEGACY_PATH_KEYS = {"archive_downloads_dir": ("downloads_dir",)}
# Where archives come from: a detected mod manager ("vortex", later "mo2", ...) or "unmanaged" (a folder the user
# chose). Older settings stored uses_vortex (true/false); it is read once and replaced.
RUNTIME_KEYS = {"vortex_detected", "vortex_exe", "paths_confirmed"}
LEGACY_PREFERENCE_KEYS = {"uses_vortex"}
SORT_FILE = APP_DIR / "mwmanager_sort_order.txt"
CORE = ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm")
ESM_FIX_PATCH = "mwmanager - ESM fixes.esp"
XE_SKY_PLUGIN = "XE Sky Variations.esp"
OFFICIAL_SHIPPED_ESPS = (
    "adamantiumarmor.esp", "AreaEffectArrows.esp", "bcsounds.esp", "EBQ_Artifact.esp",
    "entertainers.esp", "LeFemmArmor.esp", "master_index.esp", "Siege at Firemoth.esp",
)
END_PATCHES = (
    "mwdialog_check AutoMerge.esp",
    "mwdialog_check Cell Fixes.esp",
    "mwdialog_check PlaceholderCell ESP Patch.esp",
    "mwdialog_check Multipatch.esp",
)
LOCKED_PATCHES = {ESM_FIX_PATCH.casefold(), *(name.casefold() for name in END_PATCHES)}
USER_RULES_TEMPLATE = ("; MWManager load-order rules\n"
                       "; Add [Order], [NearStart], [NearEnd], or [Exact] rules as needed.\n"
                       "; Without custom rules, installed order is preserved wherever masters and mlox allow it.\n")
MERGE_RECORD = APP_DIR / "mwmanager_merged.txt"
MAJOR_MASTER_CONFIG = APP_DIR / "mwmanager_core_addons.json"
GUI_STATE_FILE = APP_DIR / "mwmanager_gui_state.json"
DEPLOYMENT_JOURNAL = APP_DIR / "major_master_deployments.json"
PLUGIN_STAGE_MANIFEST = APP_DIR / "staging" / "plugins" / ".mwmanager-plugin-stages.json"
DISABLED_RECORD = APP_DIR / "mwmanager_disabled.txt"
# Siege at Firemoth set aside for Tamriel Rebuilt. The winning plugin ends the reason: every run turns a plugin back on
# when the one after "conflicts with" is gone, and "Tamriel Rebuilt (deployed ...)" named no plugin, so it always was.
FIREMOTH_DISABLED_REASON = "Tamriel Rebuilt deployed without Remove Firemoth: conflicts with TR_Mainland.esm"


def _display_line(raw: str, limit: int = 600) -> str:
    """A tool output line for the progress pane: keep its indent (wrapped continuations, list dashes)."""
    line = raw.rstrip().expandtabs(4)
    return line if len(line) <= limit else line[:limit - 1] + "…"


TIDY_SCRIPT = APP_DIR / "mwmanager_install.py"
TIDY_REFUSED = 2                                      # mwmanager_install.py --apply: plan missing or changed


class TidyPlanPaused(RuntimeError):
    """The user kept a repeatedly changing Tidy package staged for a later retry."""


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def waiting_option_archives(staging: Path, outputs: Path, downloads, manager_dir: Path, major_masters,
                            tried: set[str], progress=print) -> tuple[list[dict], list[str]]:
    """(archives to install, masters to reprocess) for the options of staged major masters that wait on another
    mod. An archive tried before in this session is not queued again, so a need that can't be met ends the
    loop; the mods those archives need in turn are queued by the plugin batch itself (plan_needed_archives)."""
    import mock_fomod
    try:
        pending = json.loads((Path(outputs) / mock_fomod.PENDING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    majors = {m.casefold(): m for m in major_masters}
    items, masters = [], []
    for package in sorted((p for p in Path(staging).iterdir() if p.is_dir()), key=lambda p: p.name.casefold()) \
            if Path(staging).is_dir() else []:
        entries = pending.get(package.name) if isinstance(pending, dict) else None
        if not any(isinstance(v, dict) and v.get("status") == "waiting" for v in (entries or {}).values()):
            continue
        masters += [majors[p.name.casefold()] for p in package.iterdir() if p.name.casefold() in majors]
        items += plan_needed_archives(package.name, outputs, downloads, manager_dir, tried, major_masters, progress)
    return items, masters


def plan_needed_archives(package_name: str, outputs: Path, downloads, manager_dir: Path, queued: set[str],
                         major_masters=(), progress=print) -> list[dict]:
    """Batch step 3: batch items for the download archives that hold what this package's held options need
    (the "waiting" entries of mock_fomod's pending file), not already queued. A need that isn't in the
    downloads opens a Nexus search; one that is a major master is left to the major-master install."""
    import mock_fomod
    import needed_mods
    try:
        pending = json.loads((Path(outputs) / mock_fomod.PENDING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    held = {k: v for k, v in (pending.get(package_name) or {}).items()
            if isinstance(v, dict) and v.get("status") == "waiting"}
    needs = sorted({n for v in held.values() for n in v.get("needs", [])})
    if not needs or not downloads:
        return []
    index_path = Path(outputs) / "mwmanager_archive_plugins.json"
    found = needed_mods.find_needed(needs, Path(downloads), index_path, Path(manager_dir), progress)
    index = needed_mods.archive_index(Path(downloads), index_path, None)
    majors = {m.casefold() for m in major_masters}
    items = []
    for need in needs:
        users = ", ".join(f"'{v['label']}'" for v in held.values() if need in v.get("needs", []))
        archive = found.get(need)
        if need.casefold() in majors:
            progress(f"{package_name}: option {users} needs the major master {need}; stage it from Major masters")
            continue
        if archive is None:
            progress(f"{package_name}: option {users} needs {need}, which isn't in your downloads; "
                     f"opening a Nexus search for it")
            try:
                needed_mods.search_on_nexus(need, Path(downloads))
            except Exception as exc:              # the batch goes on; the option stays held
                progress(f"  (could not open the Nexus search: {exc})")
            continue
        key = str(Path(archive).resolve()).casefold()
        if key in queued:
            continue
        queued.add(key)
        progress(f"{package_name}: option {users} needs {need}; staging {Path(archive).name} first")
        items.append({"archive": str(archive), "plugins": index.get(Path(archive).name, []), "needed_by": package_name})
    return items


SHORT_NAME_LIMIT = 40          # a staged package's folder and its Extras folder repeat this name in every path


def short_package_name(archive: Path) -> str:
    """A download's mod name without Vortex's Nexus id, version and stamp ("Better Telvanni Crystal V2 4k", not
    "...-59196-1-0-1780326665"), cut to SHORT_NAME_LIMIT: the full name, twice in a staged path (package folder and
    Extras), pushed textures past Windows' 260 characters."""
    from vortex_tidy import mod_name
    name = mod_name(Path(archive).stem)
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name[:SHORT_NAME_LIMIT].rstrip(" .") or "plugin"


def staging_folder_name(item: dict) -> str:
    """staging/plugins/<short mod name>-<Nexus id>: unique per mod (a new version replaces the old one), short enough
    that the textures below stay inside Windows' 260 characters."""
    archive = Path(item["archive"])
    mod_id = str(item.get("nexus_mod_id") or "").strip()
    if not mod_id.isdigit():
        from plugin_install import _nexus_ids_from_archive_name
        mod_id = _nexus_ids_from_archive_name(archive)[0] or ""
    if not mod_id:
        match = re.search(r"-(\d{3,7})-", archive.stem)
        mod_id = match.group(1) if match else ""
    name = short_package_name(archive)
    return f"{name}-{mod_id}" if mod_id else name


def run_tidy_steps(py, staging: Path, data_files: Path, progress, label: str, *, only: str | None = None,
                   reopen: bool = False, env=None, quiet=None, on_line=None, log=None,
                   ask_only: bool = False) -> int | None:
    """Run GUI Tidy options, build its plan, then apply it without prompting.

    A plan that changes while being validated is rebuilt automatically. The user is asked what to do only after
    three consecutive refusals.
    """
    base = [str(py), "-u", str(TIDY_SCRIPT), "--staging", str(staging), "--game-data-files", str(data_files)]
    if only:
        base += ["--only", only]
    env = dict(os.environ if env is None else env, MWMANAGER_GUI="1")
    if ask_only:                       # a batch's first pass: the options are asked and saved, nothing is staged
        env["MWMANAGER_OPTIONS_ASK_ONLY"] = "1"
    if quiet is None and os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}

    def step(extra, what):
        checkpoint(progress)
        if log:
            log(f"START {label}: {what}")
        process = _popen(base + extra, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                   bufsize=1, env=env, **(quiet or {}))
        output = queue.Queue()

        def read_output():
            try:
                for line in process.stdout:
                    output.put(line)
            finally:
                output.put(None)

        threading.Thread(target=read_output, name=f"MWManager {label} output", daemon=True).start()
        recent, mods, plan = [], None, (None, None)
        try:
            while True:
                checkpoint(progress)
                try:
                    line = output.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                clean = " ".join(line.strip().split())
                if not clean:
                    continue
                recent.append(clean)
                del recent[:-80]
                match = re.search(r"vortex_tidy plan,.*:\s*(\d+)\s+mod\(s\)", clean, re.I)
                if match:
                    mods = int(match.group(1))
                if clean.startswith("MWMANAGER-PLAN:"):
                    state, _, path = clean.split(":", 1)[1].strip().partition(" ")
                    plan = (state, Path(path) if path else None)
                elif clean.startswith("MWMANAGER-PROGRESS:"):
                    progress(f"{label}: " + clean.split(":", 1)[1].strip())
                else:
                    progress(_display_line(line))
                    if on_line:
                        on_line(clean)
            code = process.wait()
        except BaseException:
            managed_processes.terminate(process)
            raise
        if log:
            log(f"{'DONE' if code == 0 else 'FAIL'} {label}: {what}: exit code {code}")
        return code, recent, mods, plan

    code, recent, mods, plan = step(["--options-only" if reopen else "--options"],
                                    "option questions" if ask_only else "options and plan")
    if code == 3:
        raise TidyPlanPaused(f"{label}: option selection was cancelled. Staged files were preserved.")
    if code:
        raise RuntimeError(f"{label}: the option step failed (exit code {code}).\n\n" + "\n".join(recent)[-4000:])
    if ask_only:
        return mods
    refusals, refusal_series = 0, 1
    while True:
        checkpoint(progress)
        state, plan_path = plan
        if state == "empty":
            progress(f"{label}: nothing to tidy.")
            return mods
        if state != "pending" or plan_path is None:
            raise RuntimeError(f"{label}: Tidy did not report a valid plan state. Deployment is halted.")
        progress(f"{label}: applying the validated Tidy plan automatically.")
        code, recent, _mods, _plan = step(["--apply", "--yes"], "apply")
        if code == 0:
            break
        if code != TIDY_REFUSED:
            raise RuntimeError(f"{label}: Tidy failed (exit code {code}); automatic retry was stopped.\n\n"
                               + "\n".join(recent)[-4000:])
        # Only the documented plan-mismatch refusal is retryable. Exit 1 and
        # unknown failures retain their diagnostics and never become success.
        refusals += 1
        if refusals == 3:
            stop = ("Stop staging", "stop")
            first = refusal_series == 1
            action = progress.ask_choice(
                "Tidy keeps finding new work",
                f"{label}: Tidy found a different plan on three passes in a row, so it has not finished. "
                "Earlier passes may already have changed staged files; their records were preserved.\n\n" +
                ("Try again runs three more passes. Stop staging keeps the staged files as they are."
                 if first else
                 "Apply Anyway applies the plan Tidy calculates now, even though it keeps changing. Stop "
                 "staging keeps the staged files as they are."),
                [("Try Again", "retry"), stop] if first else [("Apply Anyway", "apply_anyway"), stop], "stop")
            if action == "apply_anyway":
                progress(f"{label}: applying the plan Tidy calculates now, as asked.")
                code, recent, _mods, _plan = step(
                    ["--apply", "--yes", "--force-plan"], "apply changing plan by user request")
                break
            if action != "retry":
                raise TidyPlanPaused(f"{label}: staging was stopped. The staged files and Tidy's latest plan "
                                     f"are kept:\n{plan_path}")
            refusals = 0
            refusal_series += 1
        progress(f"{label}: Tidy found more to do on that pass; starting again (attempt {refusals + 1} of 3).")
        code, recent, new_mods, plan = step([], "rebuild changed plan")
        if code:
            raise RuntimeError(f"{label}: Tidy could not rebuild its changed plan (exit code {code}).\n\n"
                               + "\n".join(recent)[-4000:])
        if new_mods is not None:
            mods = new_mods
    if code:
        raise RuntimeError(f"{label}: Tidy could not apply every action (exit code {code}). The remaining actions "
                           f"stay in {plan_path}; deployment is halted.\n\n" + "\n".join(recent)[-4000:])
    checkpoint(progress)
    return mods


def read_disabled_plugins() -> dict[str, str]:
    result = {}
    try:
        for line in DISABLED_RECORD.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith(";"):
                continue
            name, _sep, reason = value.partition("\t")
            if name.strip():
                result[name.strip()] = reason.strip()
    except OSError:
        pass
    return result


def write_disabled_plugins(entries: dict[str, str]) -> None:
    if not entries:
        try:
            DISABLED_RECORD.unlink()
        except FileNotFoundError:
            pass
        return
    lines = ["; Plugins disabled by MWManager are kept in MWManager\\disabled_plugins.",
             "; --reenable restores and enables them.", ""]
    lines.extend(f"{name}\t{reason}" for name, reason in entries.items())
    _atomic_write(DISABLED_RECORD, ("\n".join(lines) + "\n").encode("utf-8"))


def _read_ready_plugin_stages() -> list[dict]:
    try:
        value = json.loads(PLUGIN_STAGE_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    try:
        deployments = json.loads((APP_DIR / "plugin_deployments.json").read_text(encoding="utf-8"))
        deployed = {str(Path(entry.get("staging_package", "")).resolve()).casefold()
                    for entry in deployments.get("deployments", []) if entry.get("staging_package")}
    except (OSError, ValueError, TypeError, AttributeError):
        deployed = set()
    ready = []
    for item in value.get("ready", []) if isinstance(value, dict) else []:
        if not isinstance(item, dict):
            continue
        package = Path(item.get("staging_package", ""))
        try:
            package.resolve().relative_to((APP_DIR / "staging" / "plugins").resolve())
        except (OSError, ValueError):
            continue
        if (package.is_dir() and str(package.resolve()).casefold() not in deployed and item.get("archive")
                and item.get("selected_plugins") is not None):
            ready.append(item)
    return ready


def _write_ready_plugin_stages(items: list[dict]) -> None:
    PLUGIN_STAGE_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    temporary = PLUGIN_STAGE_MANIFEST.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"version": 1, "ready": items}, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, PLUGIN_STAGE_MANIFEST)


def _remove_ready_plugin_stage(package: Path) -> None:
    package_key = str(package.resolve()).casefold()
    ready = [item for item in _read_ready_plugin_stages()
             if str(Path(item["staging_package"]).resolve()).casefold() != package_key]
    _write_ready_plugin_stages(ready)


import managed_processes
_CHILDREN = managed_processes.CHILDREN


def _popen(*args, **kwargs):
    """subprocess.Popen for a step MWManager waits on: remembered, so closing the window ends it too."""
    return managed_processes.popen(*args, **kwargs)


def _ended(process) -> bool:
    poll = getattr(process, "poll", None)
    return poll is None or poll() is not None


def _end_children() -> None:
    """End every step still running (and what it started). A step left alive would keep changing Data Files
    while the next MWManager resumes the same step."""
    managed_processes.end_all()


def _single_instance(wait_seconds: float = 6.0):
    """Hold outputs\\mwmanager.lock while this process lives. None when another MWManager has it (a restart
    starts the new one just before the old one goes, so it waits a few seconds first)."""
    import time
    path = APP_DIR / "outputs" / "mwmanager.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.25)


class _ShellProgram:
    """A program started the way Explorer starts it, in its own folder, that can be waited on.

    The Morrowind Code Patch (and MGEXEgui) read their files relative to the working directory, and Windows
    asks for administrator rights before starting the Code Patch, which a plain CreateProcess refuses
    (error 740). ShellExecuteEx does both: it shows the consent prompt, and lpDirectory sets the folder the
    program starts in even when it is elevated. poll() is None while it runs, then its exit code."""

    def __init__(self, path, arguments=""):
        import ctypes
        from ctypes import wintypes

        path = Path(path)
        self._process = None
        if not arguments:
            command = ([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(path)]
                       if path.suffix.casefold() in {".cmd", ".bat"} else [str(path)])
            try:
                self._process = _popen(command, cwd=str(path.parent), close_fds=True)
                self._handle = None
                self._code = None
                return
            except OSError as exc:
                if getattr(exc, "winerror", None) != 740:
                    raise

        class Info(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                        ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                        ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                        ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                        ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

        info = Info()
        info.cbSize = ctypes.sizeof(Info)
        # NOCLOSEPROCESS gives us a wait handle. NOASYNC can deadlock this worker thread while the shell waits
        # for DDE/COM completion, which left prerequisite setup locked before MCP appeared.
        info.fMask = 0x00000040                        # SEE_MASK_NOCLOSEPROCESS
        info.lpFile = str(path)
        info.lpParameters = arguments or None
        info.lpDirectory = str(path.parent)
        info.nShow = 1                                # SW_SHOWNORMAL
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(Info)]
        shell32.ShellExecuteExW.restype = wintypes.BOOL
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            error = ctypes.get_last_error()
            if error == 1223:                         # ERROR_CANCELLED: the consent prompt was declined
                raise OSError(f"{path.name} was not started: the administrator prompt was declined")
            raise OSError(error, ctypes.FormatError(error).strip() or f"could not start {path.name}")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._handle = info.hProcess                  # None when Windows handed the file to a running program
        self._code = None

    @property
    def pid(self) -> int | None:
        if self._process is not None:
            return self._process.pid
        return int(self._kernel32.GetProcessId(self._handle)) or None if self._handle else None

    @property
    def waitable(self) -> bool:
        return self._process is not None or bool(self._handle) or self._code is not None

    def poll(self):
        if self._process is not None:
            return self._process.poll()
        if self._code is not None or not self._handle:
            return self._code
        if self._kernel32.WaitForSingleObject(self._handle, 0) == 0x102:      # WAIT_TIMEOUT: still running
            return None
        from ctypes import byref, wintypes
        code = wintypes.DWORD(0)
        ok = self._kernel32.GetExitCodeProcess(self._handle, byref(code))
        self._code = int(code.value) if ok else 0     # an elevated program may not give its code: it ended
        self._kernel32.CloseHandle(self._handle)
        self._handle = None
        return self._code


def _move_left_of(pid: int, beside) -> bool:
    """Move a program's main window to the left of the Tk window beside; False until that window exists.
    Both rectangles come from Windows in this process, so DPI scaling cannot skew them."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _param):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # GW_OWNER
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        return False
    program, reference = wintypes.RECT(), wintypes.RECT()
    user32.GetWindowRect(found[0], ctypes.byref(program))
    user32.GetWindowRect(user32.GetParent(beside.winfo_id()), ctypes.byref(reference))
    x = max(0, reference.left - (program.right - program.left) - 8)
    user32.SetWindowPos(found[0], None, x, max(0, reference.top), 0, 0, 0x0001 | 0x0004 | 0x0010)
    return True


def center_window(window, parent, width: int, height: int) -> None:
    """Center a Tk window over its parent and keep it within the screen."""
    center(window, parent, width, height)


def disabled_plugins_dir() -> Path:
    """Disabled ESPs/ESMs live in MWManager's own folder, never in the game install."""
    return APP_DIR / "disabled_plugins"


def default_game_dir() -> Path:
    registered = detect_morrowind_install()
    if registered:
        return registered
    candidates = []
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if root:
            candidates += [Path(root) / "Steam" / "steamapps" / "common" / "Morrowind"]
    for path in candidates:
        if (path / "Data Files").is_dir():
            return path
    return candidates[0] if candidates else APP_DIR


def _valid_morrowind_install(path: Path | str | None) -> Path | None:
    if not path:
        return None
    candidate = Path(str(path).strip().strip('"'))
    if candidate.name.casefold() in {"morrowind.exe", "morrowind"} and candidate.suffix.casefold() == ".exe":
        candidate = candidate.parent
    return candidate if (candidate / "Morrowind.exe").is_file() and (candidate / "Data Files").is_dir() else None


def detect_morrowind_install() -> Path | None:
    """Find an installed Morrowind game path from uninstall and Steam registry data."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None

    roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, root_path in roots:
        try:
            with winreg.OpenKey(hive, root_path) as root:
                index = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(root, subkey_name) as entry:
                            display_name = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                            if "morrowind" not in display_name.casefold():
                                continue
                            for value_name in ("InstallLocation", "InstallDir", "DisplayIcon"):
                                try:
                                    raw = str(winreg.QueryValueEx(entry, value_name)[0]).split(",", 1)[0]
                                except OSError:
                                    continue
                                found = _valid_morrowind_install(raw)
                                if found:
                                    return found
                    except OSError:
                        continue
        except OSError:
            continue

    gog_roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\GOG.com\Games"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\GOG.com\Games"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\GOG.com\Games"),
    )
    for hive, root_path in gog_roots:
        try:
            with winreg.OpenKey(hive, root_path) as root:
                index = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(root, subkey_name) as entry:
                            for value_name in ("path", "workingDir", "InstallLocation"):
                                try:
                                    found = _valid_morrowind_install(winreg.QueryValueEx(entry, value_name)[0])
                                except OSError:
                                    continue
                                if found:
                                    return found
                    except OSError:
                        continue
        except OSError:
            continue

    steam_roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
    )
    steam_paths = []
    for hive, key_path in steam_roots:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                for value_name in ("SteamPath", "InstallPath"):
                    try:
                        steam_paths.append(Path(str(winreg.QueryValueEx(key, value_name)[0])))
                    except OSError:
                        pass
        except OSError:
            pass
    libraries = []
    for steam_path in steam_paths:
        libraries.append(steam_path)
        library_file = steam_path / "steamapps" / "libraryfolders.vdf"
        try:
            content = library_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        library_values = re.findall(r'"path"\s+"([^"]+)"', content, flags=re.I)
        if not library_values:
            library_values = re.findall(r'"\d+"\s+"([A-Za-z]:\\\\[^"]+)"', content)
        libraries.extend(Path(value.replace("\\\\", "\\")) for value in library_values)
    for library in libraries:
        found = _valid_morrowind_install(library / "steamapps" / "common" / "Morrowind")
        if found:
            return found
    return None


from mwmanager_detect import (  # noqa: E402  (moved there so preinstall needn't load the GUI)
    detect_vortex_downloads, vortex_install_path, vortex_registered)


def read_settings() -> dict:
    data = {"archive_source": None, "first_run": 1, "init_complete": 0}
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            data.update({key: value for key, value in saved.items()
                         if key not in PATH_SETTING_KEYS and key not in RUNTIME_KEYS})
    except (OSError, ValueError):
        pass
    legacy_vortex = data.pop("uses_vortex", None)
    if data.get("archive_source") is None and legacy_vortex is not None:
        data["archive_source"] = "vortex" if legacy_vortex else "unmanaged"
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read(MWMANAGER_INI, encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    paths = ini["Paths"] if ini.has_section("Paths") else {}
    configured_game = str(paths.get("game_dir", "")).strip()
    confirmed_game = _valid_morrowind_install(configured_game)
    data["paths_confirmed"] = bool(confirmed_game)
    data["game_dir"] = str(confirmed_game or default_game_dir())
    data["archive_downloads_dir"] = next((str(paths.get(key, "")).strip()
                                          for key in ("archive_downloads_dir", *LEGACY_PATH_KEYS["archive_downloads_dir"])
                                          if str(paths.get(key, "")).strip()), "")
    data["always_elevate_game_dir"] = str(paths.get("always_elevate_game_dir", "")).strip()
    try:
        threshold = ini.getint("Staging", BSA_OFFER_SETTING, fallback=DEFAULT_BSA_OFFER)
    except ValueError:
        threshold = DEFAULT_BSA_OFFER
    data[BSA_OFFER_SETTING] = threshold if threshold >= MIN_BSA_OFFER else DEFAULT_BSA_OFFER
    try:        # true: every package over the threshold gets its BSA without being asked
        data[ALWAYS_BSA_SETTING] = ini.getboolean("Staging", ALWAYS_BSA_SETTING, fallback=False)
    except ValueError:
        data[ALWAYS_BSA_SETTING] = False
    for key in ASSET_LIMIT_SETTINGS:
        try:
            data[key] = ini.getboolean("Staging", key, fallback=True)
        except ValueError:
            data[key] = True
    # REMOVE BEFORE SHIPPING: developer machine only, set by hand: diff staged mod Lua against the Steam copies
    try:
        data["compare_steam_lua"] = ini.getboolean("Dev", "compare_steam_lua", fallback=False)
    except ValueError:
        data["compare_steam_lua"] = False
    try:        # --remove before shipping: [Dev] dev_options turns on developer-only steps
        data["dev_options"] = ini.getboolean("Dev", "dev_options", fallback=False)
    except ValueError:
        data["dev_options"] = False
    detected_exe = vortex_install_path()
    data["vortex_detected"] = bool(detected_exe or vortex_registered())
    data["vortex_exe"] = str(detected_exe) if detected_exe else ""
    # Registry detection establishes a remembered Yes; otherwise let the user
    # answer once instead of guessing from a directory that may be stale.
    if data.get("archive_source") is None and data["vortex_detected"]:
        data["archive_source"] = "vortex"
    if data.get("archive_source") == "vortex" and not data.get("archive_downloads_dir"):
        found = detect_vortex_downloads()
        if found:
            data["archive_downloads_dir"] = str(found)
    return data


def write_settings(settings: dict) -> None:
    """Keep path configuration in mwmanager.ini and app preferences in JSON."""
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read(MWMANAGER_INI, encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    if not ini.has_section("Paths"):
        ini.add_section("Paths")
    for key in PATH_SETTING_KEYS:
        # A caller updating preferences must not erase path choices it did not load.
        if key in settings:
            ini.set("Paths", key, str(settings.get(key) or ""))
    for legacy in LEGACY_PATH_KEYS.values():
        for key in legacy:
            ini.remove_option("Paths", key)
    if not ini.has_section("Staging"):
        ini.add_section("Staging")
    try:
        threshold = int(settings.get(BSA_OFFER_SETTING, DEFAULT_BSA_OFFER))
        if threshold < MIN_BSA_OFFER:
            raise ValueError
    except (TypeError, ValueError):
        threshold = DEFAULT_BSA_OFFER
    ini.set("Staging", BSA_OFFER_SETTING, str(threshold))
    if ALWAYS_BSA_SETTING in settings:
        ini.set("Staging", ALWAYS_BSA_SETTING, "true" if settings[ALWAYS_BSA_SETTING] else "false")
    for key in ASSET_LIMIT_SETTINGS:
        if key in settings:
            ini.set("Staging", key, "true" if settings[key] else "false")
    stream = io.StringIO()
    ini.write(stream)
    _atomic_write(MWMANAGER_INI, stream.getvalue().encode("utf-8"))   # a crash mid-write never empties it

    preferences = {key: value for key, value in settings.items()
                   if key not in PATH_SETTING_KEYS and key not in (BSA_OFFER_SETTING, ALWAYS_BSA_SETTING)
                   and key not in ASSET_LIMIT_SETTINGS
                   and key not in RUNTIME_KEYS and key not in LEGACY_PREFERENCE_KEYS}
    _atomic_write(SETTINGS_FILE, (json.dumps(preferences, indent=2) + "\n").encode("utf-8"))


def _windows_user_is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _manifest_requires_admin(executable: Path) -> bool:
    """Read the PE RT_MANIFEST resource and check its requested execution level."""
    if os.name != "nt" or not executable.is_file():
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryExW.argtypes = (wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD)
    kernel32.LoadLibraryExW.restype = wintypes.HMODULE
    kernel32.FindResourceW.argtypes = (wintypes.HMODULE, wintypes.LPCWSTR, wintypes.LPCWSTR)
    kernel32.FindResourceW.restype = wintypes.HANDLE
    kernel32.LoadResource.argtypes = (wintypes.HMODULE, wintypes.HANDLE)
    kernel32.LoadResource.restype = wintypes.HANDLE
    kernel32.LockResource.argtypes = (wintypes.HANDLE,)
    kernel32.LockResource.restype = ctypes.c_void_p
    kernel32.SizeofResource.argtypes = (wintypes.HMODULE, wintypes.HANDLE)
    kernel32.SizeofResource.restype = wintypes.DWORD
    kernel32.FreeLibrary.argtypes = (wintypes.HMODULE,)
    module = kernel32.LoadLibraryExW(str(executable), None, 0x00000002)  # LOAD_LIBRARY_AS_DATAFILE
    if not module:
        return False
    try:
        resource = kernel32.FindResourceW(module, ctypes.cast(ctypes.c_void_p(1), wintypes.LPCWSTR),
                                          ctypes.cast(ctypes.c_void_p(24), wintypes.LPCWSTR))
        if not resource:
            return False
        size = kernel32.SizeofResource(module, resource)
        loaded = kernel32.LoadResource(module, resource)
        pointer = kernel32.LockResource(loaded) if loaded else None
        if not size or not pointer:
            return False
        raw = ctypes.string_at(pointer, size)
        for encoding in ("utf-8", "utf-16-le"):
            try:
                manifest = raw.decode(encoding, errors="ignore").casefold()
                if "requestedexecutionlevel" in manifest and 'level="requireadministrator"' in manifest:
                    return True
                if "requestedexecutionlevel" in manifest and 'level="highestavailable"' in manifest:
                    return True
            except UnicodeError:
                continue
        return False
    finally:
        kernel32.FreeLibrary(module)


def _compatibility_layer_requires_admin(executable: Path) -> bool:
    if os.name != "nt":
        return False
    import winreg

    key_path = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
    value_name = str(executable.resolve())
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, value_name)
            if "runasadmin" in str(value).casefold():
                return True
        except (OSError, FileNotFoundError):
            continue
    return False


def game_requires_admin(game_dir: Path) -> bool:
    executable = game_dir / "Morrowind.exe"
    return (_compatibility_layer_requires_admin(executable)
            or _manifest_requires_admin(executable))


def offer_elevation(game_dir: Path) -> None:
    """Offer a UAC relaunch when the configured game is set to require admin."""
    if os.name != "nt" or _windows_user_is_admin() or not game_requires_admin(game_dir):
        return
    game_key = str(game_dir.resolve()).casefold()
    settings = read_settings()
    always_elevate = str(settings.get("always_elevate_game_dir", "")).casefold() == game_key
    if not always_elevate:
        root = tk.Tk()
        apply_theme(root)
        dialog = root
        dialog.title("Administrator access")
        dialog.resizable(False, False)
        dialog.configure(background=COLORS["canvas"])
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=("Morrowind.exe is configured to run with administrator privileges.\n\n"
                               "MWManager must run at the same elevation level to manage this game folder "
                               "reliably. Without elevation, deployment and Morrowind.ini changes may fail.\n\n"
                               "Relaunch MWManager as administrator now?"),
                  justify="left", wraplength=440).pack(anchor="w", pady=(0, 12))
        always_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Always run MWManager as administrator for this game folder",
                        variable=always_var).pack(anchor="w", pady=(0, 14))
        result = {"elevate": False, "always": False}

        def close_prompt(elevate: bool) -> None:
            result["elevate"] = elevate
            result["always"] = always_var.get()
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Continue without elevation",
                   command=lambda: close_prompt(False)).pack(side="right")
        ttk.Button(buttons, text="Run as administrator",
                   command=lambda: close_prompt(True)).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", lambda: close_prompt(False))
        dialog.bind("<Return>", lambda _event: close_prompt(True))
        dialog.bind("<Escape>", lambda _event: close_prompt(False))
        dialog.update_idletasks()
        width, height = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
        x = max(0, (dialog.winfo_screenwidth() - width) // 2)
        y = max(0, (dialog.winfo_screenheight() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.grab_set()
        dialog.focus_force()
        root.mainloop()
        if not result["elevate"]:
            return
        always_elevate = result["always"]
    import ctypes

    script = str(Path(__file__).resolve())
    parameters = subprocess.list2cmdline([script, *sys.argv[1:]])
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                                  parameters, str(APP_DIR), 1)
    if result > 32:
        if always_elevate:
            settings["always_elevate_game_dir"] = str(game_dir.resolve())
            try:
                write_settings(settings)
            except OSError:
                pass
        raise SystemExit(0)
    messagebox.showerror("Elevation failed", "Windows could not start MWManager with administrator access.")


def read_order() -> list[str]:
    try:
        return [line.strip() for line in SORT_FILE.read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith(";")]
    except OSError:
        return []


def read_game_ini_order(game_dir: Path) -> list[str]:
    """Read the selected game's enabled plugin order from its Morrowind.ini."""
    try:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string((game_dir / "Morrowind.ini").read_text(encoding="cp1252"))
        values = parser["Game Files"] if parser.has_section("Game Files") else {}
        entries = []
        for key, value in values.items():
            match = re.fullmatch(r"gamefile(\d+)", key.strip(), flags=re.I)
            plugin = value.strip()
            if match and plugin.casefold().endswith((".esp", ".esm")):
                entries.append((int(match.group(1)), plugin))
        return [plugin for _index, plugin in sorted(entries)]
    except (OSError, configparser.Error, UnicodeError):
        return []


def write_order(order: list[str]) -> None:
    lines = ["; MWManager active load order. Core masters are fixed at the top.", "", *order, ""]
    _atomic_write(SORT_FILE, "\n".join(lines).encode("utf-8"))


def plugins_in_data(data_dir: Path) -> list[str]:
    try:
        return sorted((p.name for p in data_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in (".esm", ".esp")), key=str.casefold)
    except OSError:
        return []


def configured_major_masters() -> list[str]:
    """Read the editable optional community-master list in its configured priority order."""
    try:
        data = json.loads(MAJOR_MASTER_CONFIG.read_text(encoding="utf-8"))
        return [name for name in data if isinstance(name, str) and name.lower().endswith(".esm")]
    except (OSError, ValueError):
        return []


def read_merge_inputs() -> tuple[list[str], dict[str, list[str]]]:
    inputs: list[str] = []
    groups: dict[str, list[str]] = {}
    try:
        merged = None
        for line in MERGE_RECORD.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith(";"):
                continue
            if value.startswith("[") and value.endswith("]"):
                merged = value[1:-1]
                groups[merged] = []
            elif merged:
                groups[merged].append(value)
                inputs.append(value)
    except OSError:
        pass
    return inputs, groups


def read_gui_state() -> dict:
    try:
        value = json.loads(GUI_STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_gui_state(state: dict) -> None:
    temporary = GUI_STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, GUI_STATE_FILE)


def deployed_master_names() -> set[str]:
    from major_master_deploy import deployment_statuses, tracked_deployment_masters
    data_dir = Path(read_settings().get("game_dir", "")) / "Data Files"
    tracked = tracked_deployment_masters(APP_DIR)
    statuses = deployment_statuses(tracked, data_dir, APP_DIR)
    return {name.casefold() for name in tracked
            if statuses[name.casefold()]["state"] == "deployed"}


def waiting_option_count() -> int:
    try:
        import mock_fomod
        pending = json.loads(
            (APP_DIR / "outputs" / mock_fomod.PENDING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return 0
    return sum(
        1 for entries in pending.values() if isinstance(entries, dict)
        for entry in entries.values()
        if isinstance(entry, dict) and entry.get("status") == "waiting"
    ) if isinstance(pending, dict) else 0


def serializable_preflight_signature(signature: tuple) -> list:
    return [list(item) if isinstance(item, tuple) else item for item in signature]


def completed_preflight_signature(manager_settings: dict) -> tuple | None:
    """Return the persisted signature of a completed preflight, or None if it was never recorded."""
    raw = manager_settings.get("last_preflight_signature")
    if not isinstance(raw, list):
        return None

    def thaw(value):
        if isinstance(value, list):
            return tuple(thaw(item) for item in value)
        return value

    return thaw(raw)

def ordered_signature(plugin_names: list[str], order: list[str], active_names: set[str], game_dir: str) -> str:
    data_dir = Path(game_dir) / "Data Files"

    def stamp(name):                             # a plugin replaced in place changes the signature too
        try:
            info = (data_dir / name).stat()
            return [info.st_size, info.st_mtime_ns]
        except OSError:
            return None

    payload = {
        "game_dir": str(Path(game_dir).resolve()).casefold(),
        "plugins": sorted([name.casefold(), stamp(name)] for name in plugin_names),
        "order": [(name.casefold(), name.casefold() in active_names) for name in order],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class ToolTip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.window = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def show(self, _event=None):
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        ttk.Label(self.window, text=self.text, style="Tooltip.TLabel", wraplength=380, justify="left").pack()

    def hide(self, _event=None):
        if self.window:
            self.window.destroy()
            self.window = None


class ManagerGUI(tk.Tk):
    def _remember_preflight(self, signature: tuple) -> None:
        self._preflight_signature = signature
        self.settings["last_preflight_signature"] = serializable_preflight_signature(signature)
        write_settings(self.settings)

    def _release_grab_when_minimized(self, event):
        if event.widget is self:
            holder = self.grab_current()
            if holder is not None:
                self._grab_while_minimized = holder
                holder.grab_release()

    def _regrab_when_restored(self, event):
        if event.widget is not self or self._grab_while_minimized is None:
            return
        holder, self._grab_while_minimized = self._grab_while_minimized, None
        if holder.winfo_exists():
            holder.grab_set()

    def __init__(self):
        super().__init__()
        apply_theme(self)
        self.title("MWManager")
        self._active_job = None
        self.status = tk.StringVar(value="Core ESMs stay enabled and fixed at the top.")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        center(self, None, 900, 620)                     # the main window: middle of the desktop
        self.minsize(760, 500)
        # Tk drops the taskbar's restore while a dialog holds the grab: hand the grab back only once restored
        self._grab_while_minimized = None
        self.bind("<Unmap>", self._release_grab_when_minimized, add="+")
        self.bind("<Map>", self._regrab_when_restored, add="+")
        self.settings = read_settings()
        if self.settings.get("paths_confirmed"):     # another game's answers and rules are set aside first
            import mwmanager_gamestate
            switched = mwmanager_gamestate.use_game(APP_DIR, self.settings["game_dir"])
            if switched:
                print(switched, flush=True)
        persisted_preflight = completed_preflight_signature(self.settings)
        if (persisted_preflight is not None
                and self._setup_done("preflight_complete")
                and persisted_preflight == preflight_signature(self.settings["game_dir"])):
            self._preflight_signature = persisted_preflight
        elif self._setup_done("preflight_complete"):
            # Legacy state predates the persisted signature. The completed setup
            # checkpoint is the durable proof that prerequisites were accepted.
            self._remember_preflight(preflight_signature(self.settings["game_dir"]))
        self.gui_state = read_gui_state()
        self.order = read_order()
        self.active_names = {n.casefold() for n in self.order}
        self.merge_inputs, self.merge_groups = read_merge_inputs()
        self.major_masters = configured_major_masters()
        saved_selection = self.gui_state.get("selected_major_masters")
        self.selected_major_masters = ({str(name).casefold() for name in saved_selection}
                                        if isinstance(saved_selection, list)
                                        else ({name.casefold() for name in self.major_masters
                                               if name.casefold() in self.active_names}
                                              | self._staged_major_master_names()))
        self.pending_order_rules = []
        self.load_order_dirty = False
        if self.settings.get("paths_confirmed"):
            self._setup_reconcile()
        first_run = self.settings.get("first_run", 0) == 1
        paths_confirmed = bool(self.settings.get("paths_confirmed"))
        if first_run and paths_confirmed and not self._setup_done("paths_confirmed"):
            self._setup_complete("paths_confirmed")  # migrate an interrupted run created before checkpoints
        if not first_run or paths_confirmed:
            offer_elevation(Path(self.settings["game_dir"]))
        from major_master_deploy import recovery_pending
        interrupted_setup, _ = setup_state.interrupted(
            self._setup_path(), self.settings["game_dir"])
        if (not first_run and not recovery_pending(APP_DIR)
                and interrupted_setup != "major_masters_deployed"):
            self._initialize_plugin_baseline()
        self._ensure_core()
        self._build_menu()
        self._build_ui()
        self._clear_major_master_scan_cache()
        self.status.set("MWManager loaded; reconciling the current game state…")
        self.update_idletasks()
        if first_run:
            if paths_confirmed:
                self.game_label.config(text="Saved paths loaded. Resuming the incomplete preflight…")
                self.refresh()
                self.after(150, self._begin_first_run)
            else:
                self.game_label.config(text="Confirm the Morrowind and downloads paths before MWManager scans plugins.")
                self.after(150, self._open_detected_paths)
        else:
            self.refresh()
        from major_master_deploy import recovery_pending
        if not first_run and recovery_pending(APP_DIR):
            self.after(150, self._resume_interrupted_major_masters)
        elif not first_run and (APP_DIR / "transactions" / "plugin-deploy").is_dir():
            self.after(150, self._resume_interrupted_plugin_deployment)
        elif (not first_run and self._setup_done("initial_full_complete")
                and not self._setup_done("catalog_complete")):
            # setup stopped (or was made) before the catalog step: build it now, then carry on
            resume = self._begin_init if self.settings.get("init_complete", 0) != 1 else (lambda: None)
            self.after(150, lambda: self._run_initial_catalog(resume))
        elif not first_run and self.settings.get("init_complete", 0) != 1:
            self.after(150, self._begin_init)
        elif not first_run and setup_state.interrupted(self._setup_path(), self.settings["game_dir"])[0] in (
                "major_masters_staged", "major_masters_deployed"):
            self.after(150, self._resume_interrupted_major_masters)
        elif not first_run and (self.settings.get("archive_source") is None
                                or not self.settings.get("archive_downloads_dir")):
            self.after(150, self._ensure_archive_downloads)
        if not first_run and self.settings.get("paths_confirmed"):
            self.after(30000, self._start_background_integrity)
        if (not first_run and self.settings.get("init_complete", 0) == 1
                and not recovery_pending(APP_DIR)
                and waiting_option_count()):
            self.after(1500, self._resume_waiting_options_after_startup)

    def _ensure_core(self):
        order_by_name = {name.casefold(): name for name in self.order}
        core = [order_by_name.get(name.casefold(), name) for name in CORE]
        rest = [n for n in self.order if n.casefold() not in {c.casefold() for c in CORE}]
        self.order = core + rest

    def _start_background_integrity(self):
        """Check tracked deployed files off the Tk thread and surface drift once."""
        if getattr(self, "_active_job", None):
            self.after(10000, self._start_background_integrity)
            return
        if getattr(self, "_integrity_running", False):
            return
        self._integrity_running = True

        def work():
            import mwmanager_integrity
            try:
                result = mwmanager_integrity.inspect(
                    APP_DIR, Path(self.settings["game_dir"]) / "Data Files")
            except (OSError, ValueError):
                self.after(0, lambda: setattr(self, "_integrity_running", False))
                return

            def finish():
                self._integrity_running = False
                counts = result.get("counts") or {}
                drift = sum(count for status, count in counts.items() if status != "ok")
                if drift:
                    self.status.set(
                        f"Integrity warning: {drift} tracked deployment file(s) are missing, "
                        "modified, unreadable, or unverifiable. Open Tools > Integrity report.")

            self.after(0, finish)

        threading.Thread(target=work, name="MWManager integrity check", daemon=True).start()

    def _resume_waiting_options_after_startup(self):
        if getattr(self, "_active_job", None):
            self.after(3000, self._resume_waiting_options_after_startup)
            return
        count = waiting_option_count()
        if not count:
            return
        self.status.set(
            f"Resuming {count} chosen option dependency "
            f"{'queue' if count == 1 else 'queues'} after major-master staging…")
        self._resolve_waiting_options(lambda: self.status.set(
            "Required-plugin option queue checked; remaining missing downloads stay remembered."))

    def _setup_path(self):
        return APP_DIR / "mwmanager_setup_state.json"

    def _setup_done(self, step):
        return setup_state.done(self._setup_path(), self.settings["game_dir"], step)

    def _setup_complete(self, step):
        return setup_state.complete(self._setup_path(), self.settings["game_dir"], step)

    def _setup_start(self):
        self.settings["paths_confirmed"] = True
        setup_state.start(self._setup_path(), self.settings["game_dir"])
        self._setup_complete("paths_confirmed")

    def _setup_begin(self, step, pid=None):
        return setup_state.begin(self._setup_path(), self.settings["game_dir"], step, pid)

    def _setup_fail(self, step, error):
        return setup_state.fail(self._setup_path(), self.settings["game_dir"], step, error)

    def _setup_reconcile(self):
        """The step file is the record of setup; the two flags in the settings file follow it."""
        try:
            import mwmanager_journal
            journal = mwmanager_journal.Journal(APP_DIR / "journal")
            migrated = journal.migrate_unknown_asset_backups(self.settings["game_dir"])
            migrated += journal.migrate_official_asset_backups(self.settings["game_dir"])
            migrated += journal.migrate_original_install(
                self.settings["game_dir"], sorted((APP_DIR / "outputs").glob("*unused_assets.txt")))
            if migrated:
                self.status.set(f"Moved {migrated} kept file(s) from Off/Original Install: a package's own to its "
                                "Off folder, the rest to Off/Found at Setup.")
        except (OSError, ValueError, RuntimeError) as exc:
            self.status.set(f"Legacy asset-backup migration needs attention: {exc}")
        changed = False
        # Builds with the old placeholder marked initialization complete without ever opening major masters.
        if (self._setup_done("initialization_complete")
                and not self._setup_done("major_masters_started")):
            setup_state.reset(self._setup_path(), self.settings["game_dir"], "initialization_complete")
            self.settings["init_complete"] = 0
            changed = True
        if self._setup_done("initial_full_complete") and self.settings.get("first_run", 0) == 1:
            self.settings["first_run"] = 0
            changed = True
        if self._setup_done("initialization_complete") and self.settings.get("init_complete", 0) != 1:
            self.settings["first_run"], self.settings["init_complete"] = 0, 1
            changed = True
        if changed:
            write_settings(self.settings)

    def _setup_resume_check(self) -> bool:
        """Say which step was interrupted. If the process that was doing it is still alive (MWManager was
        ended without closing its window), offer to end it; False when the user leaves it running."""
        step, entry = setup_state.interrupted(self._setup_path(), self.settings["game_dir"])
        if not step:
            return True
        label = setup_state.LABELS.get(step, step)
        live_step, live = setup_state.running(self._setup_path(), self.settings["game_dir"])
        pid = live.get("pid") if live_step else None
        if pid and pid != os.getpid():
            label = setup_state.LABELS.get(live_step, live_step)
            if not messagebox.askyesno(
                    "A setup step is still running",
                    f"MWManager stopped during {label}, but the process doing it (PID {pid}) is still running "
                    "and may still be changing Data Files.\n\nEnd it and run the step again?\n\n"
                    "Choose No to leave it running; start MWManager again when it has finished.",
                    icon="warning", default="no", parent=self):
                self.status.set(f"Setup is waiting: {label} is still running (PID {pid}).")
                return False
            setup_state.end_process(pid)
        note = f"Resuming setup: {label} was interrupted (attempt {int(entry.get('attempts') or 0) + 1})."
        if entry.get("last_error"):
            note += " Last error: " + str(entry["last_error"]).splitlines()[0][:160]
        self.status.set(note)
        return True

    def _initialize_plugin_baseline(self, *, force=False):
        game_dir = str(Path(self.settings["game_dir"]).resolve())
        if (not force and isinstance(self.gui_state.get("known_plugins"), list)
                and str(self.gui_state.get("game_dir", "")).casefold() == game_dir.casefold()):
            return
        deployed = deployed_master_names()
        self.gui_state["game_dir"] = game_dir
        self.gui_state["known_plugins"] = [name for name in plugins_in_data(Path(game_dir) / "Data Files")
                                            if name.casefold() not in deployed]
        write_gui_state(self.gui_state)

    def _build_menu(self):
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Options…", command=self.open_options)
        file_menu.add_command(label="Detected game folders…", command=lambda: self._open_detected_paths(first_run=False))
        file_menu.add_separator()
        file_menu.add_command(label="Resume setup", command=self._resume_setup)
        file_menu.add_command(label="Exit", command=self._on_close)
        menu.add_cascade(label="File", menu=file_menu)
        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="Recovery center…", command=self.show_recovery_center)
        tools_menu.add_separator()
        tools_menu.add_command(label="Integrity report…", command=self.show_integrity_report)
        tools_menu.add_command(label="Capture integrity snapshot…", command=self.capture_integrity_snapshot)
        tools_menu.add_command(label="Compare integrity snapshot…", command=self.compare_integrity_snapshot)
        tools_menu.add_command(label="Record conflict report…", command=self.show_record_conflicts)
        tools_menu.add_command(label="Merge recommendations…", command=self.show_merge_plan)
        tools_menu.add_command(label="Auto-merge plugins…", command=self.run_auto_merge)
        tools_menu.add_command(label="Save setup profile…", command=self.save_install_profile)
        tools_menu.add_command(label="Apply setup profile…", command=self.apply_install_profile)
        tools_menu.add_separator()
        tools_menu.add_command(label="Install mods your options need…", command=self.install_waiting_option_mods)
        tools_menu.add_command(label="Preview staged files…", command=self.preview_staged_install)
        tools_menu.add_command(label="Compare staged update…", command=self.compare_staged_update)
        menu.add_cascade(label="Tools", menu=tools_menu)
        self.file_menu = file_menu
        self.tools_menu = tools_menu
        self.config(menu=menu)

    def _show_tool_report(self, title, lines):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.minsize(620, 360)
        center(dialog, self, 760, 520)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        box = tk.Text(frame, wrap="word", font=FONTS["mono"], relief="flat", padx=10, pady=8)
        scroll = ttk.Scrollbar(frame, command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        box.insert("1.0", "\n".join(str(line) for line in lines) + "\n")
        box.configure(state="disabled")
        box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Button(frame, text="Close", command=dialog.destroy).pack(side="bottom", anchor="e", pady=(10, 0))

    def show_integrity_report(self):
        import mwmanager_integrity
        try:
            report = mwmanager_integrity.inspect(
                APP_DIR, Path(self.settings["game_dir"]) / "Data Files", include_untracked=False)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Integrity report failed", str(exc), parent=self)
            return
        self._show_tool_report("MWManager integrity report", mwmanager_integrity.render(report))

    def show_record_conflicts(self):
        import mwmanager_record_report
        report = mwmanager_record_report.load_conflicts(
            APP_DIR / "game_catalog.sqlite3", active_plugins=read_order())
        self._show_tool_report("Catalog record conflicts", mwmanager_record_report.render(report))

    def show_merge_plan(self):
        """Read-only: which active plugins could be merged together (mwdialog_mergeplan), shown as a report."""
        if getattr(self, "_merge_plan_running", False):
            return
        self._merge_plan_running = True
        game_dir = Path(self.settings["game_dir"])
        data_dir = game_dir / "Data Files"
        names = read_order() or read_game_ini_order(game_dir)
        self.status.set("Working out merge recommendations…")

        def work():
            import mwdialog_mergeplan
            try:
                order = [(n, str(data_dir / n)) for n in names if (data_dir / n).is_file()]
                (APP_DIR / "outputs").mkdir(parents=True, exist_ok=True)
                plan = mwdialog_mergeplan.recommend(order, str(data_dir), str(game_dir))
                path = Path(mwdialog_mergeplan.write_plan(plan, str(APP_DIR / "outputs"), len(order)))
                lines = path.read_text(encoding="utf-8").splitlines()
                error = None
            except (OSError, ValueError) as exc:
                lines, error = [], exc

            def finish():
                self._merge_plan_running = False
                if error:
                    self.status.set("Merge recommendations failed.")
                    messagebox.showerror("Merge recommendations failed", str(error), parent=self)
                    return
                self.status.set(f"Merge recommendations saved to {path}")
                self._show_tool_report("Merge recommendations", lines)

            self.after(0, finish)

        threading.Thread(target=work, name="MWManager merge plan", daemon=True).start()

    def run_auto_merge(self):
        """--auto-merge on its own (normally part of --full); its follow-up --fix --multipatch rebuilds the patches."""
        if getattr(self, "_active_job", None):
            return
        if not messagebox.askyesno(
                "Auto-merge plugins",
                "Merge the eligible plugins into the auto-merge plugin and rebuild the patches?\n\n"
                "This changes the load order; start a new game if merged plugins place objects in cells.",
                parent=self):
            return
        if not self.save():
            return
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        command = [str(self._console_python()), "-u", str(APP_DIR / "mwmanager.py"), str(data_dir),
                   "--auto-merge", "--yes"]

        def worker(report):
            quiet = {}
            if os.name == "nt":
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
            env = dict(os.environ, MWMANAGER_GUI="1", MWMANAGER_CONFLICT_GUI="1")
            process = _popen(command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                             bufsize=1, env=env, **quiet)
            recent = []
            for line in process.stdout:
                message = " ".join(line.strip().split())
                if message:
                    recent.append(message)
                    del recent[:-80]
                    report(_display_line(line))
            result = process.wait()
            if result:
                raise RuntimeError("Auto-merge did not complete.\n\n" +
                                   ("\n".join(recent)[-5000:] or f"mwmanager.py exited with code {result}"))
            return recent

        def completed(result, error):
            self.order = read_order()
            self.active_names = {name.casefold() for name in self.order}
            self.refresh()
            self._update_action_buttons()
            if error:
                self.status.set("Auto-merge stopped with an error.")
                messagebox.showerror("Auto-merge failed", str(error), parent=self)
                return
            self.status.set("Auto-merge finished.")
            self._show_tool_report("Auto-merge", result or [])

        self._run_progress_job("Auto-merge", "Merging eligible plugins…", worker, completed)

    def capture_integrity_snapshot(self):
        import mwmanager_integrity
        name = messagebox.askstring("Integrity snapshot", "Snapshot name:", parent=self)
        if not name:
            return
        try:
            path = mwmanager_integrity.capture_snapshot(
                APP_DIR, Path(self.settings["game_dir"]) / "Data Files", name)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Snapshot failed", str(exc), parent=self)
            return
        messagebox.showinfo("Snapshot saved", f"Integrity snapshot saved to:\n\n{path}", parent=self)

    def compare_integrity_snapshot(self):
        import mwmanager_integrity
        folder = APP_DIR / "snapshots"
        names = sorted(path.stem for path in folder.glob("*.json")) if folder.is_dir() else []
        if not names:
            messagebox.showinfo("Integrity snapshots", "No snapshots are available.", parent=self)
            return
        name = messagebox.askstring(
            "Compare integrity snapshot",
            "Snapshot name:\n\n" + "\n".join(names[:20]),
            initialvalue=names[0], parent=self)
        if not name:
            return
        try:
            report = mwmanager_integrity.compare_snapshot(
                APP_DIR, Path(self.settings["game_dir"]) / "Data Files", name)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Snapshot comparison failed", str(exc), parent=self)
            return
        self._show_tool_report("Integrity snapshot comparison", [
            f"Snapshot: {report['snapshot']}",
            f"Added: {len(report['added'])}",
            *(f"  + {path}" for path in report["added"]),
            f"Removed: {len(report['removed'])}",
            *(f"  - {path}" for path in report["removed"]),
            f"Changed: {len(report['changed'])}",
            *(f"  ~ {path}" for path in report["changed"]),
            f"Unchanged: {report['unchanged']}",
        ])

    def show_recovery_center(self):
        import mwmanager_recovery
        try:
            inventory = mwmanager_recovery.build_recovery_inventory(
                APP_DIR, self.settings["game_dir"],
                APP_DIR / "staging" / "major_masters")
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("Recovery scan failed", str(exc), parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("MWManager recovery center")
        dialog.transient(self)
        dialog.minsize(760, 420)
        center(dialog, self, 900, 560)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Interrupted operations and deployment ownership",
                  style="H2.TLabel").pack(anchor="w")
        summary = inventory["counts"]
        ttk.Label(frame, text=(f"{summary['items']} item(s), {summary['recoverable']} recoverable, "
                               f"{summary['needs_review']} need review"),
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 10))
        columns = ("kind", "status", "label", "id")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for name, title, width in (("kind", "Kind", 145), ("status", "Status", 105),
                                   ("label", "Item", 450), ("id", "ID", 160)):
            tree.heading(name, text=title)
            tree.column(name, width=width, stretch=name == "label")
        scroll = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        by_id = {}
        for item in inventory["items"]:
            iid = tree.insert("", "end", values=(
                item["kind"], item["status"], item["label"], item["id"]))
            by_id[iid] = item
        footer = ttk.Frame(dialog, padding=(16, 0, 16, 16))
        footer.pack(fill="x")

        def recover_selected():
            selection = tree.selection()
            if not selection:
                return
            item = by_id[selection[0]]
            if not item.get("recoverable"):
                self._show_tool_report("Recovery needs review", [
                    item["label"], "", item.get("details", {}).get("problem")
                    or "This item is not safe to recover automatically.",
                ])
                return
            if not messagebox.askyesno(
                    "Recover selected item",
                    f"Recover this item?\n\n{item['label']}\n\n"
                    "MWManager will retain any modified file that cannot be safely restored.",
                    parent=dialog):
                return
            try:
                result = mwmanager_recovery.recover_item(
                    APP_DIR, self.settings["game_dir"], item["id"],
                    APP_DIR / "staging" / "major_masters")
            except (OSError, KeyError, RuntimeError, ValueError) as exc:
                messagebox.showerror("Recovery failed", str(exc), parent=dialog)
                return
            dialog.destroy()
            self.settings = read_settings()
            self.order = read_order()
            self.active_names = {name.casefold() for name in self.order}
            self.major_masters = set(configured_major_masters())
            self.refresh()
            self._show_tool_report("Recovery complete", [
                f"Action: {result.get('action')}",
                f"Removed: {result.get('removed', 0)}",
                f"Restored: {result.get('restored', 0)}",
                f"Pending: {result.get('pending', 0)}",
            ])

        ttk.Button(footer, text="Recover selected", command=recover_selected).pack(side="right")
        ttk.Button(footer, text="Close", command=dialog.destroy).pack(side="right", padx=(0, 8))

    def save_install_profile(self):
        import mwmanager_profiles
        name = messagebox.askstring("Save setup profile", "Profile name:", parent=self)
        if not name:
            return
        note = messagebox.askstring("Save setup profile", "Optional note:", parent=self) or ""
        try:
            path = mwmanager_profiles.save(APP_DIR, name, note=note)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not save profile", str(exc), parent=self)
            return
        messagebox.showinfo("Profile saved", f"Saved {name} to:\n\n{path}", parent=self)

    def apply_install_profile(self):
        import mwmanager_profiles
        profiles = mwmanager_profiles.list_profiles(APP_DIR)
        if not profiles:
            messagebox.showinfo("Setup profiles", "No saved profiles are available.", parent=self)
            return
        choices = [(f"{item['name']} — {item.get('note') or item.get('saved_at')}", item["name"])
                   for item in profiles]
        dialog = tk.Toplevel(self)
        dialog.title("Apply setup profile")
        dialog.transient(self)
        dialog.grab_set()
        center(dialog, self, 620, 360)
        selection = tk.StringVar(value=choices[0][1])
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Choose the manager choices to restore:",
                  style="H2.TLabel").pack(anchor="w", pady=(0, 10))
        for label, value in choices:
            ttk.Radiobutton(frame, text=label, variable=selection, value=value).pack(anchor="w", pady=3)

        def apply_selected():
            name = selection.get()
            delta = mwmanager_profiles.compare(APP_DIR, name)
            changed = delta["changed"] + delta["missing"] + delta["extra"]
            if not messagebox.askyesno(
                    "Apply setup profile",
                    f"Apply {name}?\n\n{len(changed)} managed state file(s) differ. "
                    "This restores manager choices only; it does not redeploy or remove game files.",
                    parent=dialog):
                return
            try:
                result = mwmanager_profiles.apply(APP_DIR, name)
            except (OSError, ValueError) as exc:
                messagebox.showerror("Could not apply profile", str(exc), parent=dialog)
                return
            dialog.destroy()
            self.settings = read_settings()
            self.order = read_order()
            self.active_names = {name.casefold() for name in self.order}
            self.major_masters = set(configured_major_masters())
            self.refresh()
            messagebox.showinfo(
                "Profile applied",
                f"Restored {len(result['restored'])} state file(s).\n\nBackup: {result['backup_dir']}",
                parent=self)

        ttk.Button(frame, text="Apply profile", command=apply_selected).pack(anchor="e", pady=(14, 0))

    def preview_staged_install(self):
        import mwmanager_install_plan
        initial = APP_DIR / "staging" / "plugins"
        package = filedialog.askdirectory(parent=self, title="Choose a staged plugin package",
                                          initialdir=str(initial))
        if not package:
            return
        package_path = Path(package).resolve()
        plugins = sorted(path.name for path in package_path.rglob("*")
                         if path.suffix.casefold() in {".esp", ".esm"})
        try:
            plan = mwmanager_install_plan.build(
                {"selected_plugins": plugins}, package_path,
                Path(self.settings["game_dir"]) / "Data Files", APP_DIR)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Preview failed", str(exc), parent=self)
            return
        lines = mwmanager_install_plan.render(plan)
        lines.extend([
            "",
            "Operations:",
            *[f"  {op['action']}: {op['relative_target']} ({op['size']:,} bytes)"
              for op in plan["operations"][:500]],
        ])
        self._show_tool_report("Staged files preview", lines)

    def compare_staged_update(self):
        import mwmanager_update_impact
        initial = APP_DIR / "staging" / "plugins"
        old = filedialog.askdirectory(parent=self, title="Choose the current/deployed staged package",
                                      initialdir=str(initial))
        if not old:
            return
        new = filedialog.askdirectory(parent=self, title="Choose the replacement staged package",
                                      initialdir=str(old))
        if not new:
            return
        try:
            report = mwmanager_update_impact.compare(Path(old), Path(new))
        except (OSError, ValueError) as exc:
            messagebox.showerror("Update comparison failed", str(exc), parent=self)
            return
        files = report["files"]
        lines = [
            f"Risk score: {mwmanager_update_impact.risk_score(report)}",
            f"Files added: {len(files['added'])}",
            *(f"  + {path}" for path in files["added"]),
            f"Files removed: {len(files['removed'])}",
            *(f"  - {path}" for path in files["removed"]),
            f"Files changed: {len(files['changed'])}",
            *(f"  ~ {path}" for path in files["changed"]),
            "",
            "Plugins:",
            *[f"  {item['plugin']}: {item['status']}, +{item.get('records_added', 0)} "
              f"-{item.get('records_removed', 0)} ~{item.get('records_changed', 0)} records"
              for item in report["plugins"]],
        ]
        self._show_tool_report("Staged update impact", lines)

    def _ensure_archive_downloads(self):
        """Make sure an Archive Downloads folder is set: a detected mod manager's folder, or one the user chose."""
        source = self.settings.get("archive_source")
        folder = self.settings.get("archive_downloads_dir") or ""
        if not folder or not Path(folder).is_dir():
            self.settings["archive_downloads_dir"] = ""
            try:
                import mwmanager_detect
                managers = mwmanager_detect.detect_managers()
            except Exception:
                managers = []
            found = next((m for m in managers if m.get("supported") and m.get("downloads")
                          and source in (None, m["key"])), None)
            if found:
                self.settings["archive_source"] = found["key"]
                self.settings["archive_downloads_dir"] = found["downloads"]
            else:
                selected = filedialog.askdirectory(parent=self, title="Select your Archive Downloads folder",
                                                   initialdir=str(Path.home()))
                if selected:
                    self.settings["archive_downloads_dir"] = selected
                    if source is None:
                        self.settings["archive_source"] = "unmanaged"
        write_settings(self.settings)
        self.game_label.config(
            text=f"Game: {self.settings['game_dir']}   |   Archive Downloads: "
                 f"{self.settings.get('archive_downloads_dir') or 'Not set'}"
        )
        return bool(self.settings.get("archive_downloads_dir") and Path(self.settings["archive_downloads_dir"]).is_dir())

    def _begin_first_run(self):
        if not self._ensure_archive_downloads():
            self.status.set("First-run setup is waiting for an archive download folder.")
            return
        if not self._setup_resume_check():
            return

        def worker(progress):
            from preinstall import run_preinstall
            result = run_preinstall(Path(self.settings["game_dir"]), APP_DIR, progress, first_run=True,
                                  archive_source=self.settings.get("archive_source"),
                                  archive_dir=self.settings.get("archive_downloads_dir"))
            if result is True:
                self._remember_preflight(preflight_signature(self.settings["game_dir"]))
            checkpoint(progress)
            return result

        def continue_after_preflight():
            self.order = read_order()
            existing = {name.casefold() for name in plugins_in_data(Path(self.settings["game_dir"]) / "Data Files")}
            self.active_names.intersection_update(existing)
            self.refresh()
            self._update_action_buttons()
            self.status.set("Preflight complete. Sorting plugins before Tidy and the initial --full cycle…")

            def full_completed():
                self._setup_complete("initial_full_complete")
                self.settings["first_run"] = 0
                write_settings(self.settings)
                self._run_initial_catalog(self._offer_major_master_processing)

            def run_full():
                if self._setup_done("initial_full_complete"):
                    full_completed()
                    return
                data_dir = Path(self.settings["game_dir"]) / "Data Files"
                if (data_dir / XE_SKY_PLUGIN).is_file():
                    self.active_names.add(XE_SKY_PLUGIN.casefold())
                self.refresh()
                game = self.settings["game_dir"]
                state_path = self._setup_path()
                # the --full pass itself is a checkpoint: closing during the conflict scan that follows does not
                # repeat it. A conflict review takes it back, because the rebuild after it must run the pass.
                skip = self._setup_done("initial_full_pass_complete")
                self._setup_begin("initial_full_complete")
                if not skip:
                    self._setup_begin("initial_full_pass_complete")
                self.generate_patches(
                    mode="full", force=True, on_success=full_completed, skip_first_pass=skip,
                    on_first_pass=lambda: setup_state.complete(state_path, game, "initial_full_pass_complete"),
                    on_review=lambda: setup_state.reset(state_path, game, "initial_full_pass_complete"),
                    on_spawn=lambda pid: setup_state.set_pid(state_path, game, "initial_full_complete", pid),
                    on_failure=lambda error: setup_state.fail(state_path, game, "initial_full_complete", error))

            self.after(200, lambda: self._run_initial_tidy(run_full))

        def completed(result, error):
            if error:
                self._setup_fail("preflight_complete", error)
                self.status.set("First-run preflight stopped with an error.")
                messagebox.showerror("First-run preflight failed", str(error), parent=self)
                return
            if result == "restart":
                self._restart_manager()
                return
            if result == "cancelled":
                self._cancel_until_restart()
                return
            if not result:
                self._setup_fail("preflight_complete", "stopped at a prompt before it finished")
                self.status.set("First-run setup remains incomplete; retry it after resolving the prompts.")
                return
            self._setup_complete("preflight_complete")
            continue_after_preflight()

        if self._setup_done("preflight_complete"):
            continue_after_preflight()
        else:
            self._setup_begin("preflight_complete")
            self._run_progress_job("First-run preflight", "Checking prerequisites and initial setup…",
                                   worker, completed)

    def _run_initial_tidy(self, on_success):
        """Apply the live Data Files tidy and sort every shipped plugin before the first full pass."""
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        present = {name.casefold() for name in plugins_in_data(data_dir)}
        self.active_names.update(name.casefold() for name in OFFICIAL_SHIPPED_ESPS
                                 if name.casefold() in present)
        if (data_dir / XE_SKY_PLUGIN).is_file():
            self.active_names.add(XE_SKY_PLUGIN.casefold())
        self.refresh()
        self.load_order_dirty = True
        if not self.save():
            self._setup_fail("preflight_complete", "Could not save the initial load order.")
            return
        python = str(self._console_python())
        commands = []
        if not self._setup_done("sort_complete"):
            commands.append(("sort_complete", "Sorting the shipped plugins before --full…",
                             [python, "-u", str(APP_DIR / "mwmanager.py"), str(data_dir), "--sort", "--yes",
                              "--no-follow-up"]))
        if not self._setup_done("tidy_complete"):
            commands.append(("tidy_complete", "Tidying Data Files and applying safe fixes…",
                             [python, "-u", str(TIDY_SCRIPT), "--init", "--game-data-files", str(data_dir),
                              "--apply", "--yes"]))
        log_path = APP_DIR / "outputs" / "mwmanager_initial_setup.log"

        if not commands:
            self.after(100, on_success)
            return

        def worker(report):
            quiet = {}
            if os.name == "nt":
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
            env = os.environ.copy()
            env["MWMANAGER_GUI"] = "1"
            env["MWMANAGER_CONFLICT_GUI"] = "1"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # a resume adds to the log of the steps already done; a fresh setup starts it over
            mode = "a" if len(commands) < 2 else "w"
            game = self.settings["game_dir"]
            with log_path.open(mode, encoding="utf-8", errors="replace") as log:
                for step, label, command in commands:
                    checkpoint(report)
                    recent = []
                    report(label)
                    log.write(f"\n=== {label.rstrip('…')} ===\n")
                    process = _popen(command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL,
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                               encoding="utf-8", errors="replace", bufsize=1, env=env, **quiet)
                    setup_state.begin(self._setup_path(), game, step, getattr(process, "pid", None))
                    for line in process.stdout:
                        log.write(line)
                        message = " ".join(line.strip().split())
                        if message:
                            recent.append(message)
                            del recent[:-12]
                            report(_display_line(line))
                    result = process.wait()
                    if result:
                        log.write(f"\nExited with code {result}.\n")
                        problem = (f"{label.rstrip('…')} failed (exit {result}).\n\n" +
                                   ("\n".join(recent) or "No diagnostic output was produced."))
                        setup_state.fail(self._setup_path(), game, step, problem)
                        raise RuntimeError(problem)
                    self._setup_complete(step)
                    checkpoint(report)
            return True

        def completed(_result, error):
            if error:
                self.status.set("Initial Tidy stopped; --full was not started.")
                self._show_report("Initial setup failed", log_path)
                return
            # The CLI sort may drop plugins whose masters are not installed yet. Do not let the
            # pre-sort in-memory order add them back when generate_patches() saves the next pass.
            self.order = read_order()
            self.active_names = {name.casefold() for name in self.order}
            self.status.set("Shipped-plugin sort and Initial Tidy complete. Running the initial --full cycle…")
            self.after(100, on_success)

        self._run_progress_job("Initial setup", "Sorting shipped plugins before Tidy and --full…",
                               worker, completed)

    def _run_initial_catalog(self, on_success):
        """Build game_catalog.sqlite3 once the initial --full has finished: the core masters and archives first,
        then every plugin in the load order. (The scanner's own --init does more: it resets the working load order
        and runs --full in stages, which setup has already done.)"""
        if self._setup_done("catalog_complete"):
            self.after(100, on_success)
            return
        game = Path(self.settings["game_dir"])
        python = str(self._console_python())
        catalog = str(APP_DIR / "database" / "catalog.py")
        base = [python, "-u", catalog, "--game-dir", str(game), "--ini", str(game / "Morrowind.ini")]
        plugins = [name for name in read_order() if name.casefold() not in {c.casefold() for c in CORE}]
        commands = [("Cataloguing the core masters and archives…", base + ["--canonical-core"])]
        if plugins:
            commands.append((f"Cataloguing {len(plugins)} plugin(s)…", base + ["--plugin-files", *plugins, "--append"]))
        log_path = APP_DIR / "outputs" / "mwmanager_initial_setup.log"
        state_path, game_dir = self._setup_path(), self.settings["game_dir"]

        def worker(report):
            quiet = {}
            if os.name == "nt":
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", errors="replace") as log:
                setup_state.begin(state_path, game_dir, "catalog_complete")
                for label, command in commands:
                    checkpoint(report)
                    recent = []
                    report(label)
                    log.write(f"\n=== {label.rstrip('…')} ===\n")
                    process = _popen(command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1, **quiet)
                    setup_state.set_pid(state_path, game_dir, "catalog_complete", getattr(process, "pid", None))
                    for line in process.stdout:
                        log.write(line)
                        message = " ".join(line.strip().split())
                        if message:
                            recent.append(message)
                            del recent[:-12]
                            report(_display_line(line))
                    result = process.wait()
                    if result:
                        problem = (f"{label.rstrip('…')} failed (exit {result}).\n\n" +
                                   ("\n".join(recent) or "No diagnostic output was produced."))
                        setup_state.fail(state_path, game_dir, "catalog_complete", problem)
                        raise RuntimeError(problem)
            setup_state.complete(state_path, game_dir, "catalog_complete")
            checkpoint(report)
            return True

        def completed(_result, error):
            if error:
                self.status.set("The catalog database was not built; it is tried again at the next start.")
                self._show_report("Catalog failed", log_path)
                return
            self.status.set("Catalog database built.")
            self.after(100, on_success)

        self._run_progress_job("Catalog", "Building the catalog database…", worker, completed)

    def _complete_init(self):
        self._setup_complete("initialization_complete")
        self.settings["init_complete"] = 1
        write_settings(self.settings)
        self.status.set("MWManager initialization complete.")
        if hasattr(self, "generate_button"):
            self._update_action_buttons()
        view = getattr(self, "_setup_view", None)
        if view is not None and view.window.winfo_exists():
            view.refresh_phases()
            view.stage.set("Setup complete. Staged masters can be deployed now or later.")
            view.resume.pack_forget()

    def _begin_init(self):
        """Resume the post-preflight major-master choice until it is committed."""
        if self.settings.get("init_complete", 0) == 1:
            return
        self._offer_major_master_processing()

    def _open_detected_paths(self, *, first_run=True):
        """Pre-preflight: show the installs and Archive Downloads folder found in the registry for approval.

        Approving opens the options window with these paths filled in; preflight starts only when that window
        is saved. Detection is read-only."""
        import mwmanager_detect
        try:
            found = mwmanager_detect.detect_all()
        except Exception as exc:                                    # detection must never block setup
            found = {"games": [], "managers": []}
            self.status.set(f"Registry detection failed: {exc}")
        dialog = tk.Toplevel(self)
        dialog.title("MWManager: confirm detected paths")
        dialog.transient(self)
        dialog.after_idle(lambda: center(dialog, self))    # sized by its contents: placed once they are built
        dialog.grab_set()
        dialog.resizable(False, False)
        form = ttk.Frame(dialog, padding=14)
        form.pack(fill="both", expand=True)
        current = "" if first_run else self.settings.get("game_dir", "")
        game_var = tk.StringVar(value=mwmanager_detect.preferred_game(found["games"], current)
                                or self.settings.get("game_dir", ""))
        downloads_var = tk.StringVar(value=mwmanager_detect.preferred_downloads(
            found["managers"], self.settings.get("archive_downloads_dir", "")))
        ttk.Label(form, text="MWManager found these paths. Approve them or change them before setup continues.",
                  wraplength=600).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(form, text="Morrowind folder").grid(row=1, column=0, columnspan=2, sticky="w")
        row = 2
        if found["games"]:
            for game in found["games"]:
                ttk.Radiobutton(form, text=f"{game['store']}: {game['path']}", value=game["path"],
                                variable=game_var).grid(row=row, column=0, columnspan=2, sticky="w")
                row += 1
        else:
            ttk.Label(form, text="No Morrowind folder was found in the registry; choose its folder below.",
                      wraplength=600).grid(row=row, column=0, columnspan=2, sticky="w")
            row += 1
        ttk.Entry(form, textvariable=game_var, width=70).grid(row=row, column=0, sticky="ew", pady=(3, 12))
        ttk.Button(form, text="Browse…", command=lambda: game_var.set(filedialog.askdirectory(
            parent=dialog, title="Select Morrowind folder", initialdir=game_var.get() or str(Path.home()))
            or game_var.get())).grid(row=row, column=1, padx=(8, 0), pady=(3, 12))
        row += 1
        ttk.Label(form, text="Archive Downloads").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        managers = found["managers"]
        source_var = tk.StringVar(value=mwmanager_detect.preferred_source(
            managers, self.settings.get("archive_source"), downloads_var.get()))
        by_key = {m["key"]: m for m in managers}

        def pick_source():
            manager = by_key.get(source_var.get())
            if manager and manager.get("downloads"):
                downloads_var.set(manager["downloads"])

        for manager in managers:
            if not manager.get("detected"):
                continue
            usable = manager.get("supported") and manager.get("downloads")
            text = f"{manager['name']}: " + (manager["downloads"] if manager.get("downloads") else
                                            "download folder not found")
            if not manager.get("supported"):
                text = f"{manager['name']}: detected, not supported yet"
            radio = ttk.Radiobutton(form, text=text, value=manager["key"], variable=source_var, command=pick_source)
            radio.grid(row=row, column=0, columnspan=2, sticky="w")
            if not usable:
                radio.state(["disabled"])
            row += 1
            for note in manager.get("notes", []):
                ttk.Label(form, text=note, wraplength=600).grid(row=row, column=0, columnspan=2, sticky="w",
                                                                padx=(22, 0))
                row += 1
        ttk.Radiobutton(form, text="None / unmanaged: a folder you choose", value="unmanaged",
                        variable=source_var).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Entry(form, textvariable=downloads_var, width=70).grid(row=row, column=0, sticky="ew", pady=(3, 12))

        def browse_downloads():
            chosen = filedialog.askdirectory(parent=dialog, title="Select the Archive Downloads folder",
                                             initialdir=downloads_var.get() or str(Path.home()))
            if chosen:
                downloads_var.set(chosen)
                source_var.set(mwmanager_detect.source_for_folder(managers, chosen))

        ttk.Button(form, text="Browse…", command=browse_downloads).grid(row=row, column=1, padx=(8, 0), pady=(3, 12))
        row += 1
        bsa_offer_var = tk.StringVar(value=str(self.settings.get(BSA_OFFER_SETTING, DEFAULT_BSA_OFFER)))
        if first_run:
            ttk.Label(form, text="Offer BSA when this many assets are found").grid(
                row=row, column=0, columnspan=2, sticky="w")
            row += 1
            ttk.Entry(form, textvariable=bsa_offer_var, width=12).grid(
                row=row, column=0, sticky="w", pady=(3, 4))
            row += 1
        always_bsa_var = tk.BooleanVar(value=bool(self.settings.get(ALWAYS_BSA_SETTING)))
        if first_run:
            ttk.Checkbutton(form, text="Always Pack BSA", variable=always_bsa_var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
            row += 1
        limit_vars = {key: tk.BooleanVar(value=self.settings.get(key, True) is not False)
                      for key in ASSET_LIMIT_SETTINGS}
        if first_run:
            for key, text in ASSET_LIMIT_SETTINGS.items():
                ttk.Checkbutton(form, text=text, variable=limit_vars[key]).grid(
                    row=row, column=0, columnspan=2, sticky="w")
                row += 1
            ttk.Frame(form, height=12).grid(row=row, column=0)
            row += 1

        def approve():
            game = mwmanager_detect.valid_install(game_var.get())
            if game is None:
                messagebox.showerror("Not a Morrowind folder",
                                     "The selected folder must contain Morrowind.exe and a Data Files folder.",
                                     parent=dialog)
                return
            drive = lambda p: os.path.splitdrive(str(Path(p).resolve()))[0].casefold()
            if drive(APP_DIR) != drive(game):             # staging hardlinks into Data Files need one drive
                messagebox.showerror("MWManager is on another drive",
                                     f"MWManager must be on the same drive as Morrowind ({drive(game).upper()}). "
                                     f"Move the MWManager folder to {drive(game).upper()}\\ and start it again.",
                                     parent=dialog)
                return
            if first_run:
                try:
                    bsa_offer = int(bsa_offer_var.get())
                    if bsa_offer < MIN_BSA_OFFER:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("Invalid BSA threshold",
                                         f"The BSA offer threshold must be an integer of at least {MIN_BSA_OFFER}.",
                                         parent=dialog)
                    return
            folder = downloads_var.get().strip()
            source = source_var.get() or "unmanaged"
            if source != "unmanaged" and folder and mwmanager_detect.source_for_folder(managers, folder) != source:
                source = "unmanaged"                 # edited by hand: it is the user's own folder now
            dialog.destroy()
            if not first_run:
                self.open_options(first_run=False, game=str(game), downloads=folder, archive_source=source)
                return
            self.settings.update({"game_dir": str(game), "archive_downloads_dir": folder,
                                  "archive_source": source, BSA_OFFER_SETTING: bsa_offer,
                                  ALWAYS_BSA_SETTING: always_bsa_var.get(),
                                  **{key: var.get() for key, var in limit_vars.items()},
                                  "first_run": 1, "init_complete": 0})
            write_settings(self.settings)
            self._setup_start()
            offer_elevation(game)
            ini_order = read_game_ini_order(game)
            self.order = ini_order or read_order()
            self.active_names = {name.casefold() for name in ini_order} if ini_order else set()
            self.gui_state["game_dir"] = str(game.resolve())
            self.gui_state["known_plugins"] = plugins_in_data(game / "Data Files")
            write_gui_state(self.gui_state)
            self._ensure_core()
            self.refresh()
            self._update_action_buttons()
            self.status.set("Paths saved. Starting preflight for the selected game folder…")
            self.after(150, self._begin_first_run)

        def cancel():
            dialog.destroy()
            if first_run:
                self._cancel_until_restart()

        buttons = ttk.Frame(form)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right")
        ttk.Button(buttons, text="Approve", command=approve).pack(side="right", padx=(0, 6))
        dialog.bind("<Return>", lambda _e: approve())
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.detected = found                                     # for tests
        return dialog

    def _offer_major_master_processing(self):
        """Resume setup at the major-master catalog until staging is accepted or skipped."""
        if self._setup_done("initialization_complete"):
            self._complete_init()                      # already done: only the settings flag was behind
            return
        self._setup_complete("major_masters_started")
        self._setup_begin("initialization_complete")
        self._initializing_manager = True
        self.status.set("Initial setup is ready for major-master selection and staging.")
        self._resume_interrupted_major_masters(open_dialog=False)
        self.open_major_masters()

    def _launch_vortex(self) -> bool:
        exe = Path(self.settings.get("vortex_exe") or "")
        if not exe.is_file():
            exe = vortex_install_path() or Path()
        if not exe.is_file():
            return False
        try:
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
            return True
        except OSError:
            return False

    def _restart_manager(self):
        try:
            self.settings["first_run"] = 1
            write_settings(self.settings)
            quiet = {}
            if os.name == "nt":
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
            subprocess.Popen([sys.executable, str(Path(__file__).resolve())], cwd=str(APP_DIR), **quiet)
        except OSError as exc:
            messagebox.showerror("Could not restart MWManager",
                                 f"Close MWManager and launch it again to continue first-run setup.\n\n{exc}",
                                 parent=self)
            self._cancel_until_restart()
            return
        self.destroy()

    def _cancel_until_restart(self):
        self.status.set("Setup paused. Resume setup from File, or restart MWManager.")
        if messagebox.askyesno(
                "Setup paused", "Completed setup steps have been kept. Restart MWManager now to resume?",
                default="no", parent=self):
            self._restart_manager()
            return

        # Keep Options, Resume setup and Exit available. A cancelled prompt is not
        # a reason to permanently disable the application for the whole session.

    def _resume_setup(self):
        if getattr(self, "_active_job", None):
            return
        if self.settings.get("first_run", 0) == 1:
            if not self.settings.get("paths_confirmed") and not self._setup_done("paths_confirmed"):
                self._open_detected_paths()
            else:
                self._begin_first_run()
        elif self.settings.get("init_complete", 0) != 1:
            if not self._setup_done("catalog_complete"):
                self._run_initial_catalog(self._begin_init)
            else:
                self._begin_init()
        else:
            self.open_major_masters()

    def _build_ui(self):
        pad = SPACE
        # Header: product name and install paths on the left, global tools on the right.
        header = ttk.Frame(self, style="Header.TFrame", padding=(pad["xl"], pad["lg"], pad["xl"], pad["lg"]))
        header.pack(fill="x", side="top")
        title_box = ttk.Frame(header, style="Header.TFrame")
        title_box.pack(side="left", fill="x", expand=True)
        ttk.Label(title_box, text="MWManager", style="Header.H1.TLabel").pack(anchor="w")
        self.game_label = ttk.Label(title_box, text="", style="Header.Muted.TLabel")
        self.game_label.pack(anchor="w", pady=(2, 0))
        header_tools = ttk.Frame(header, style="Header.TFrame")
        header_tools.pack(side="right")
        ttk.Button(header_tools, text="Major masters…", command=self.open_major_masters).pack(side="left")
        ttk.Button(header_tools, text="Options…", command=self.open_options).pack(side="left", padx=(pad["sm"], 0))
        ttk.Separator(self, orient="horizontal").pack(fill="x", side="top")

        # Status bar and primary actions are packed first so they keep their space when the window shrinks.
        status_bar = ttk.Frame(self, style="Statusbar.TFrame")
        status_bar.pack(fill="x", side="bottom")
        ttk.Separator(status_bar, orient="horizontal").pack(fill="x", side="top")
        ttk.Label(status_bar, textvariable=self.status, style="Status.TLabel", anchor="w").pack(fill="x")
        self.distant_land_notice = ttk.Button(
            status_bar, text="Distant land needs regeneration in MGE XE. Mark regenerated…",
            command=self._acknowledge_distant_land)
        self._refresh_distant_land_notice()

        actions = ttk.Frame(self, padding=(pad["xl"], pad["md"], pad["xl"], pad["md"]))
        actions.pack(fill="x", side="bottom")
        self.install_plugins_button = ttk.Button(actions, text="Deploy Plugins", command=self.install_plugins)
        self.install_plugins_button.pack(side="left")
        self.enable_new_button = ttk.Button(actions, text="Enable all new plugins", command=self.enable_all_new_plugins)
        self.enable_new_button.pack(side="left", padx=(pad["sm"], 0))
        self.remove_plugins_button = ttk.Button(actions, text="Remove Deployed Plugins…", style="Danger.TButton",
                                                command=self.remove_installed_plugins)
        self.remove_plugins_button.pack(side="left", padx=(pad["lg"], 0))
        self.play_button = ttk.Button(actions, text="▶  Play", style="Primary.TButton", command=self.play_game)
        self.play_button.pack(side="right")
        self.generate_button = ttk.Button(actions, text="Generate Patches", style="Primary.TButton",
                                          command=self.generate_patches)
        self.generate_button.pack(side="right", padx=(0, pad["sm"]))
        self.generate_button.bind("<Control-Button-1>", self._force_generate_patches, add="+")
        self.generate_button.bind("<Shift-Button-1>", self._force_full_generate_patches, add="+")
        self.generate_tooltip = ToolTip(self.generate_button, "No changes detected.")

        body = ttk.Frame(self, padding=(pad["xl"], pad["lg"], pad["xl"], 0))
        body.pack(fill="both", expand=True)

        # Right column: merge record.
        right = ttk.Frame(body, style="Card.TFrame", width=290, padding=pad["lg"])
        right.pack(side="right", fill="y", padx=(pad["lg"], 0))
        right.pack_propagate(False)
        ttk.Label(right, text="Previously merged", style="Card.H2.TLabel").pack(anchor="w")
        ttk.Label(right, text="Inputs remain in Data Files for now. This view reflects MWManager's merge record.",
                  style="Card.Muted.TLabel", wraplength=258, justify="left").pack(anchor="w", pady=(4, pad["md"]))
        merged_box = tk.Frame(right, background=COLORS["field"], highlightthickness=1,
                              highlightbackground=COLORS["border_soft"])
        merged_box.pack(fill="both", expand=True)
        self.merged_list = tk.Listbox(merged_box, height=12, exportselection=False, background=COLORS["field"],
                                      font=FONTS["body"])
        self.merged_list.pack(fill="both", expand=True, padx=6, pady=6)
        self.merge_status = ttk.Label(right, text="", style="Card.Muted.TLabel",
                                      wraplength=258, justify="left")
        self.merge_status.pack(anchor="w", pady=(pad["sm"], 0))

        # Left column: the load order.
        left = ttk.Frame(body, style="Card.TFrame", padding=pad["lg"])
        left.pack(side="left", fill="both", expand=True)
        title_row = ttk.Frame(left, style="Header.TFrame")
        title_row.pack(fill="x", pady=(0, pad["md"]))
        ttk.Label(title_row, text="Active load order", style="Card.H2.TLabel").pack(side="left")
        for text, tone in (("New", "Amber"), ("MWManager patch", "Blue"), ("Core", "Faint")):
            ttk.Label(title_row, text=f"●  {text}", style=f"Chip.{tone}.TLabel").pack(side="right", padx=(pad["xs"], 0))
        tree_box = ttk.Frame(left, style="Header.TFrame")
        tree_box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_box, columns=("active", "state", "type"), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="#   Plugin", anchor="w")
        self.tree.heading("active", text="Enabled")
        self.tree.heading("state", text="Status")
        self.tree.heading("type", text="Kind")
        self.tree.column("#0", width=360, stretch=True)
        self.tree.column("active", width=68, anchor="center", stretch=False)
        self.tree.column("state", width=120, anchor="w", stretch=False)
        self.tree.column("type", width=110, anchor="w", stretch=False)
        tree_scroll = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        tree_tags(self.tree)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())
        self.tree.bind("<Button-1>", self._click_active_checkbox)
        self.tree.bind("<space>", self._toggle_active_key)

        buttons = ttk.Frame(left, style="Header.TFrame")
        buttons.pack(fill="x", pady=(pad["md"], 0))
        self.up_btn = ttk.Button(buttons, text="▲  Move up", style="Toolbar.TButton",
                                 command=lambda: self.move_selected(-1))
        self.up_btn.pack(side="left")
        self.down_btn = ttk.Button(buttons, text="▼  Move down", style="Toolbar.TButton",
                                   command=lambda: self.move_selected(1))
        self.down_btn.pack(side="left", padx=(pad["xs"], 0))
        ttk.Button(buttons, text="Edit sort rules", style="Toolbar.TButton",
                   command=self.open_sort_rules).pack(side="left", padx=(pad["lg"], 0))
        ttk.Button(buttons, text="Show Disabled", style="Toolbar.TButton",
                   command=self.show_disabled_plugins).pack(side="left", padx=(pad["xs"], 0))
        ttk.Button(buttons, text="Save load order", command=self.save).pack(side="right")

    def _refresh_distant_land_notice(self):
        if not hasattr(self, "distant_land_notice"):
            return
        try:
            state = json.loads((APP_DIR / "outputs" / "distant_land_status.json").read_text(encoding="utf-8"))
            pending = (state.get("regeneration_required") and
                       state.get("game_dir") == str(Path(self.settings["game_dir"]).resolve()))
        except (OSError, ValueError, AttributeError):
            pending = False
        if pending:
            self.distant_land_notice.pack(fill="x")
        else:
            self.distant_land_notice.pack_forget()

    def _acknowledge_distant_land(self):
        if not messagebox.askyesno("Distant land regeneration",
                                   "Have you finished regenerating distant land in MGE XE (with the MWManager "
                                   "override loaded)?", default="no", parent=self):
            mge = Path(self.settings["game_dir"]) / "MGEXEgui.exe"
            if not mge.is_file():
                messagebox.showwarning("Distant land regeneration",
                                       f"MGEXEgui.exe was not found in {mge.parent}. Install MGE XE, then "
                                       "regenerate distant land from its Distant Land tab.", parent=self)
            elif messagebox.askyesno("Distant land regeneration",
                                     "Open MGE XE now? In its Distant Land tab, load the MWManager override and "
                                     "generate distant land. Click this notice again when it has finished.",
                                     parent=self):
                try:
                    _ShellProgram(mge)          # its own folder, elevated if Windows asks; MWManager keeps running
                except OSError as exc:
                    messagebox.showerror("Could not open MGE XE", str(exc), parent=self)
            return
        from workflow_safety import atomic_json, mutation_lock
        with mutation_lock(APP_DIR, self.settings["game_dir"]):
            path = APP_DIR / "outputs" / "distant_land_status.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state["regeneration_required"] = False
            atomic_json(path, state)
        self._refresh_distant_land_notice()

    def refresh(self):
        self._refresh_distant_land_notice()
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        self.game_label.config(text=f"Game: {self.settings['game_dir']}   |   Archive Downloads: {self.settings.get('archive_downloads_dir') or 'Not set'}")
        disk = {n.casefold(): n for n in plugins_in_data(data_dir)}
        order = []
        seen = set()
        for name in self.order:
            actual = disk.get(name.casefold())
            if actual is None:
                continue
            if actual.casefold() not in seen:
                order.append(actual)
                seen.add(actual.casefold())
        for name in sorted(disk.values(), key=str.casefold):
            if name.casefold() not in seen:
                order.append(name)
                seen.add(name.casefold())
        deployed_major = deployed_master_names()
        installed_major = [disk[n.casefold()] for n in self.major_masters
                           if n.casefold() in disk and n.casefold() in deployed_major]
        self.installed_major_names = {n.casefold() for n in installed_major}
        core_names = [disk.get(n.casefold(), n) for n in CORE]
        core_keys = {n.casefold() for n in CORE}
        major_keys = self.installed_major_names
        fixed_keys = LOCKED_PATCHES | {XE_SKY_PLUGIN.casefold()}
        esm_fix = [disk[ESM_FIX_PATCH.casefold()]] if ESM_FIX_PATCH.casefold() in disk else []
        other_esms = [n for n in order if n.casefold() not in core_keys | major_keys | fixed_keys
                      and Path(n).suffix.lower() == ".esm"]
        regular_esps = [n for n in order if n.casefold() not in core_keys | major_keys | fixed_keys
                        and Path(n).suffix.lower() != ".esm"]
        xe_sky = [disk[XE_SKY_PLUGIN.casefold()]] if XE_SKY_PLUGIN.casefold() in disk else []
        end_patches = [disk[n.casefold()] for n in END_PATCHES if n.casefold() in disk]
        order = core_names + installed_major + other_esms + esm_fix + regular_esps + xe_sky + end_patches
        self.order = order
        # Generated manager patches, when present, are fixed and active.
        self.active_names.update(n.casefold() for n in order if n.casefold() in LOCKED_PATCHES)
        # XE Sky Variations is part of the required MGE XE setup: enable it by default and lock its position.
        if xe_sky:
            self.active_names.add(XE_SKY_PLUGIN.casefold())
        merged = {n.casefold() for n in self.merge_inputs}
        new_keys = {name.casefold() for name in self.new_plugins()}
        self.tree.delete(*self.tree.get_children())
        for index, name in enumerate(self.order, 1):
            key = name.casefold()
            core = name.casefold() in {x.casefold() for x in CORE}
            typ = "Core master" if core else ("Merged input" if name.casefold() in merged else Path(name).suffix.upper()[1:])
            is_merged = name.casefold() in merged
            locked_patch = name.casefold() in LOCKED_PATCHES
            state = ("Core" if core else "Fixed patch" if locked_patch else "Merged" if is_merged else
                     "New plugin" if key in new_keys else "Enabled" if key in self.active_names else "Disabled")
            tag = ("core" if core else "fixed_patch" if locked_patch else "new_plugin" if key in new_keys
                   else "disabled_row" if state in ("Disabled", "Merged") else "")
            check = "☑" if core or locked_patch or (not is_merged and name.casefold() in self.active_names) else "☐"
            tags = tuple(t for t in (tag, "odd" if index % 2 == 0 else "") if t)
            self.tree.insert("", "end", iid=name, text=f"{index:03d}   {name}", values=(check, state, typ), tags=tags)
        self.merged_list.delete(0, "end")
        if self.merge_groups:
            for target, sources in self.merge_groups.items():
                self.merged_list.insert("end", f"→ {target}")
                self.merged_list.itemconfig("end", foreground=COLORS["gold"])
                for source in sources:
                    self.merged_list.insert("end", f"   {source}")
        else:
            self.merged_list.insert("end", "No merge record yet")
        self._update_buttons()
        self._update_action_buttons()

    def new_plugins(self) -> list[str]:
        """Plugins installed since the last successful patch generation."""
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        disk = plugins_in_data(data_dir)
        known = self.gui_state.get("known_plugins")
        baseline = {str(name).casefold() for name in known} if isinstance(known, list) else set()
        detected = {name.casefold() for name in disk} - baseline
        # Masters from the current deployment were just introduced even though deployment
        # has already inserted them into the record catalog.
        detected |= deployed_master_names() - baseline
        return sorted((name for name in disk if name.casefold() in detected), key=str.casefold)

    def _current_generation_signature(self) -> str:
        disk = plugins_in_data(Path(self.settings["game_dir"]) / "Data Files")
        return ordered_signature(disk, self.order, self.active_names, self.settings["game_dir"])

    def _update_action_buttons(self):
        if getattr(self, "_active_job", None):
            return
        new_plugins = self.new_plugins()
        unselected_new = [name for name in new_plugins if name.casefold() not in self.active_names]
        self.enable_new_button.state(["!disabled"] if unselected_new else ["disabled"])
        signature = self._current_generation_signature()
        has_changes = bool(new_plugins) or signature != self.gui_state.get("generated_signature")
        # Keep it clickable while idle so modifier-clicks work; the idle style shows no changes.
        self.generate_button.state(["!disabled"])
        self.generate_button.configure(style="Primary.TButton" if has_changes else "GenerateIdle.TButton")
        self.generate_tooltip.text = (
            "Generate patches for newly detected plugins and load-order changes. Ctrl-click forces --all; "
            "Shift-click forces --full."
            if has_changes else "No changes detected. Ctrl-click forces --all; Shift-click forces --full.")
        self.play_button.state(["!disabled"] if not has_changes and self.gui_state.get("generated_signature") else ["disabled"])
        pending_setup = (self.settings.get("first_run", 0) == 1 or self.settings.get("init_complete", 0) != 1)
        for name in ("install_plugins_button", "remove_plugins_button"):
            getattr(self, name).state(["disabled"] if pending_setup else ["!disabled"])
        if pending_setup:
            for name in ("enable_new_button", "generate_button", "play_button"):
                getattr(self, name).state(["disabled"])
    def enable_all_new_plugins(self):
        new_plugins = self.new_plugins()
        if not new_plugins:
            return
        self.active_names.update(name.casefold() for name in new_plugins)
        self.load_order_dirty = True
        if not self.save():
            return
        self.status.set(f"Enabled {len(new_plugins)} newly detected plugin(s). Generate Patches to run the checks.")

    def _show_blocked_archive_warning(self, items):
        """Show unsafe archive contents as a readable list; returns archives approved for quarantine.
        Blocked by default: binaries and scripts usually mean an engine injector or something for the game folder,
        but now and then a normal mod ships a modder's utility."""
        dialog = tk.Toplevel(self)
        dialog.title("Archives blocked from deployment")
        dialog.transient(self)
        center_window(dialog, self, 820, 600)
        dialog.minsize(620, 360)
        ttk.Label(
            dialog,
            text=(f"{len(items)} archive(s) contain executable, library, or script files and are blocked: these "
                  "are never deployed into Data Files. Review the listed files. Approve only archives whose "
                  "utilities you recognize; approved files are extracted directly into Off/<plugin name>/"
                  "Executables/<original path> for inspection or manual use. The rest stay marked BLOCKED."),
            wraplength=780,
        ).pack(anchor="w", padx=12, pady=(12, 8))
        body = ttk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=12, pady=4)
        text = tk.Text(body, wrap="word", height=24, state="normal")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        text.tag_configure("archive", font=FONTS["bold"])
        text.tag_configure("payload", font=(FONTS["body"][0], 10, "italic"))
        allow = []
        for index, item in enumerate(items):
            if index:
                text.insert("end", "\n")
            allow.append(tk.BooleanVar(dialog, value=False))
            text.window_create("end", window=ttk.Checkbutton(text, text="Approve + quarantine", variable=allow[-1]))
            text.insert("end", "  " + Path(item["archive"]).name + "\n", "archive")
            payloads = item.get("blocked_payloads", [])
            for payload in payloads[:4]:
                text.insert("end", "    " + str(payload) + "\n", "payload")
            if len(payloads) > 4:
                text.insert("end", f"    … {len(payloads) - 4} additional files\n", "payload")
        text.configure(state="disabled")
        chosen = []

        def allow_ticked():
            chosen.extend(item for item, var in zip(items, allow) if var.get())
            dialog.destroy()
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=12)
        ttk.Button(buttons, text="Approve and quarantine ticked", command=allow_ticked).pack(side="right")
        keep = ttk.Button(buttons, text="Keep all blocked", command=dialog.destroy)
        keep.pack(side="right", padx=(0, 6))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        keep.focus_set()
        self.wait_window(dialog)
        return chosen

    def _show_report(self, title: str, path: Path) -> None:
        """Display a generated text report inside MWManager."""
        try:
            contents = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            messagebox.showerror(title, f"Could not read {path}: {exc}", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        center_window(dialog, self, 900, 620)
        dialog.minsize(680, 420)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=str(path), style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=(0, 8))
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, wrap="word", font=FONTS["mono"], state="normal")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        text.insert("1.0", contents)
        text.configure(state="disabled")
        ttk.Button(frame, text="Close", command=dialog.destroy).pack(anchor="e", pady=(10, 0))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.lift()
        dialog.focus_force()

    def install_plugins(self):
        """Choose regular plugin archives from Vortex or a manually selected archive."""
        source = messagebox.askyesnocancel(
            "Deploy Plugins", "From Archive Downloads?\n\nYes: browse the Archive Downloads folder\n"
            "No: choose one plugin archive",
            parent=self)
        if source is None:
            return
        downloads = Path(self.settings.get("archive_downloads_dir") or "")
        if source:
            if not downloads.is_dir():
                messagebox.showwarning("Archive Downloads unavailable",
                                       "Choose the Archive Downloads folder in Options first.", parent=self)
                self.open_options()
                return
            manual = None
        else:
            manual = filedialog.askopenfilename(
                title="Select plugin archive", initialdir=str(downloads if downloads.is_dir() else Path.home()),
                filetypes=[("Plugin archives", "*.zip *.7z *.rar"), ("All files", "*.*")])
            if not manual:
                return
        from plugin_install import scan_archives
        installed = {name.casefold() for name in plugins_in_data(Path(self.settings["game_dir"]) / "Data Files")}

        def scanned(items, error):
            if error:
                self.status.set("Plugin archive scan stopped with an error.")
                messagebox.showerror("Could not scan plugin archives", str(error), parent=self)
                return
            if manual:
                chosen_path = str(Path(manual).resolve()).casefold()
                items = [item for item in items if str(Path(item["archive"]).resolve()).casefold() == chosen_path]
            major_keys = {name.casefold() for name in self.major_masters}
            items = [item for item in items
                     if not major_keys.intersection(name.casefold() for name in item.get("plugins", []))]
            installed_now = {name.casefold() for name in plugins_in_data(
                Path(self.settings["game_dir"]) / "Data Files")}
            for item in items:
                for plugin, masters in item.get("plugin_masters", {}).items():
                    missing_major = [master for master in masters
                                     if master.casefold() in major_keys and master.casefold() not in installed_now]
                    if missing_major:
                        item["blocked"] = True
                        item["problem"] = "; ".join(filter(None, (item.get("problem"),
                            "Required major master(s) must be deployed first: " + ", ".join(missing_major))))
            unsafe_items = [item for item in items if item.get("blocked_payloads")]
            if unsafe_items:
                allowed = self._show_blocked_archive_warning(unsafe_items)
                if allowed:
                    from plugin_install import allow_payloads
                    for item in allowed:
                        allow_payloads(APP_DIR, item["archive"], item["blocked_payloads"])
                    run_scan()                  # rescans only the allowed archives; the rest come from the cache
                    return
            if not items:
                messagebox.showinfo("No plugin archives", "No supported archives were found.", parent=self)
                return
            self._show_plugin_archive_picker(items, refresh_callback=lambda: run_scan(force_refresh=True))

        def run_scan(force_refresh=False):
            self._run_progress_job(
                "Scan plugin archives", "Inspecting Vortex archives and their plugin/assets…",
                lambda progress: scan_archives(downloads if source else Path(manual).parent, APP_DIR, progress,
                                               installed_plugins=installed, refresh_cache=force_refresh),
                scanned)

        run_scan()

    def _show_plugin_archive_picker(self, items, refresh_callback=None):
        dialog = tk.Toplevel(self)
        dialog.title("Deploy Plugins")
        dialog.transient(self)
        center_window(dialog, self, 920, 520)
        dialog.minsize(760, 400)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        header = ttk.Frame(frame)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Select archives, then choose a deploy action. Auto Deploy prioritizes MWSE-only tools, then larger plugin mods and recent downloads, with replacers and pluginless mods last.",
                  wraplength=760, justify="left").pack(side="left", fill="x", expand=True, anchor="w")
        if refresh_callback:
            def refresh_scan():
                dialog.destroy()
                refresh_callback()
            ttk.Button(header, text="Refresh scan", command=refresh_scan).pack(side="right", padx=(12, 0))
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True)
        columns = ("selected", "plugins", "assets", "features", "download")
        tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="browse")
        tree.heading("#0", text="Archive")
        tree.heading("selected", text="Deploy")
        tree.heading("plugins", text="Plugins")
        tree.heading("assets", text="Assets")
        tree.heading("features", text="Detected contents")
        tree.heading("download", text="Download date")
        tree.column("#0", width=260, stretch=True)
        tree.column("selected", width=52, anchor="center", stretch=False)
        tree.column("plugins", width=160, stretch=False)
        tree.column("assets", width=60, anchor="e", stretch=False)
        tree.column("features", width=160, stretch=False)
        tree.column("download", width=115, stretch=False)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.columnconfigure(1, minsize=18)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview, takefocus=True)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        details = tk.StringVar(value="Select an archive to see scan notes.")
        ttk.Label(frame, textvariable=details, wraplength=880, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        by_id = {}
        selected = set()
        for index, item in enumerate(items):
            iid = f"archive-{index}"
            by_id[iid] = item
            features = [label for key, label in (("has_mwse", "MWSE"), ("has_fomod", "FOMOD"),
                                                   ("has_bain", "BAIN"), ("replacer", "replacer")) if item[key]]
            if item.get("utility_route"):
                features.append("UTILITY — " + item["utility_route"].upper())
            if item.get("has_duplicate_plugin_payload"):
                features.append("older plugin copy omitted")
            if item["blocked"]:
                features.append("BLOCKED")
            date = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["download_time"])) if item["download_time"] else "unknown"
            tree.insert("", "end", iid=iid, text=Path(item["archive"]).name,
                        values=("☐", ", ".join(item["plugins"]) or "(no ESP/ESM)",
                                f"{item['asset_count']:,}", ", ".join(features), date),
                        tags=("blocked",) if item["blocked"] else ())
        tree.tag_configure("blocked", foreground=COLORS["red"])

        def show_archive_details(_event=None):
            selection = tree.selection()
            item = by_id.get(selection[0]) if selection else None
            notes = item.get("problem") or ("Newer archive selected for duplicate plugin names: " +
                    ", ".join(item.get("superseded_plugins", [])) if item.get("superseded_plugins") else
                    "Ready. Required masters are ordered before their dependents.")
            details.set(str(notes)
                        if item else "Select an archive to see scan notes.")
        tree.bind("<<TreeviewSelect>>", show_archive_details)

        def toggle(event):
            iid = tree.identify_row(event.y)
            if iid not in by_id or tree.identify_column(event.x) != "#1":
                return
            if iid in selected:
                selected.remove(iid)
            else:
                selected.add(iid)
            values = list(tree.item(iid, "values"))
            values[0] = "☑" if iid in selected else "☐"
            tree.item(iid, values=values)
        tree.bind("<Button-1>", toggle)

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(10, 0))
        footer_top = ttk.Frame(footer)
        footer_top.pack(fill="x")
        footer_bottom = ttk.Frame(footer)
        footer_bottom.pack(fill="x", pady=(5, 0))
        ttk.Label(footer, text="Auto Deploy uses the detected order; estimates are content-based.").pack(side="left")

        def close_then_install(chosen):
            if not chosen:
                messagebox.showinfo("Nothing selected", "Select one or more archives first.", parent=dialog)
                return
            blocked = [item for item in chosen if item["blocked"]]
            if blocked:
                messagebox.showerror("Archive needs review",
                                     "These archives cannot be deployed until their paths are reviewed:\n\n" +
                                     "\n".join(Path(item["archive"]).name + ": " + str(item["problem"])
                                              for item in blocked[:8]), parent=dialog)
                return
            dialog.destroy()
            self._install_plugin_archives(chosen)

        ttk.Button(footer, text="Deploy Selected",
                   command=lambda: close_then_install([by_id[i] for i in sorted(selected, key=lambda x: list(by_id).index(x))])).pack(side="right")
        ttk.Button(footer, text="Deploy All Except Selected",
                   command=lambda: close_then_install([item for iid, item in by_id.items()
                                                        if iid not in selected and not item["blocked"]])).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Auto Deploy",
                   command=lambda: close_then_install([item for item in items if not item["blocked"]])).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Skip", command=dialog.destroy).pack(side="right", padx=(0, 6))

    def _install_plugin_archives(self, items, *, accept_staged=False, then=None, title=None, intro=None):
        from plugin_install import (apply_plugin_compatibility, check_deploy_path_lengths, count_bsa_assets,
                                    deploy_package, extract_archive, deployment_label, has_altered_assets,
                                    altered_assets, settle_altered_assets,
                                    pack_package_assets, pull_updated_steam_luas,
                                    index_deployment, record_deployment, remove_superseded_plugins)
        from major_master_deploy import register_archives
        from core_addons import extraction_status

        staging_root = APP_DIR / "staging" / "plugins"
        game_dir = Path(self.settings["game_dir"])
        data_dir = game_dir / "Data Files"
        manager_script = APP_DIR / "mwmanager.py"
        py = self._console_python()
        quiet = {}
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}

        install_progress_log = APP_DIR / "outputs" / "mwmanager_plugin_install_progress.log"

        def log_install_phase(message):
            try:
                install_progress_log.parent.mkdir(parents=True, exist_ok=True)
                with install_progress_log.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
            except OSError:
                pass

        def run_logged(command, progress, label, env=None):
            log_install_phase(f"START {label}: {command[0]} {Path(command[1]).name if len(command) > 1 else ''}")
            process = _popen(command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                       errors="replace", bufsize=1, env=env, **quiet)
            recent = []
            for line in process.stdout:
                clean = " ".join(line.strip().split())
                if clean:
                    recent.append(clean)
                    del recent[:-80]
                    if clean.startswith("MWMANAGER-PROGRESS:"):
                        phase = clean.split(":", 1)[1].strip()
                        progress(phase)
                        log_install_phase(f"{label}: {phase}")
            code = process.wait()
            if code:
                log_install_phase(f"FAIL {label}: exit code {code}")
                raise RuntimeError(f"{label} failed.\n\n" + "\n".join(recent)[-4000:])
            log_install_phase(f"DONE {label}: exit code 0")

        def run_batch_check(progress):
            command = [str(py), "-u", str(manager_script), str(data_dir), "--sort", "--all", "--yes"]
            conflict_env = dict(os.environ, MWMANAGER_GUI="1", MWMANAGER_CONFLICT_GUI="1")
            run_logged(command, progress, "Plugin checks and patch generation", env=conflict_env)
            report_file = APP_DIR / "outputs" / "mwmanager_new_conflicts.txt"
            scan = [str(py), "-u", str(manager_script), str(data_dir), "--cell-conflicts", "--no-interactive"]
            run_logged(scan, progress, "Cell-conflict scan")
            try:
                unresolved = [line for line in report_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            except OSError:
                unresolved = []
            if unresolved:
                progress(f"Reviewing {len(unresolved):,} unresolved cell conflict pair(s)…")
                command = [str(py), "-u", str(manager_script), str(data_dir),
                           "--cell-conflicts", "--only-new", "--defer-follow-up"]
                conflict = _popen(command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                  env=dict(os.environ, MWMANAGER_CONFLICT_GUI="1"),
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for line in conflict.stdout:
                    progress(_display_line(line))
                if conflict.wait():
                    raise RuntimeError("The interactive cell-conflict review did not complete.")
                run_logged([str(py), "-u", str(manager_script), str(data_dir), "--sort", "--all", "--yes"],
                           progress, "Applying conflict choices and regenerating patches", env=conflict_env)

        altered_choices = []

        def worker(progress):
            staging_root.mkdir(parents=True, exist_ok=True)
            from preinstall import run_preinstall
            cached_preflight = getattr(self, "_preflight_signature", None)
            if cached_preflight is not None and cached_preflight == preflight_signature(game_dir):
                progress("Reusing this session's successful preflight; prerequisite files are unchanged.")
                preflight_result = True
            else:
                preflight_result = run_preinstall(game_dir, APP_DIR, progress,
                                                  archive_dir=self.settings.get("archive_downloads_dir"))
                if preflight_result is True:
                    self._remember_preflight(preflight_signature(game_dir))
            if preflight_result == "restart":
                return {"processed": 0, "stopped": True, "restart": True,
                        "staging": str(staging_root)}
            if not preflight_result:
                return {"processed": 0, "stopped": True, "cancelled": True,
                        "preflight_blocked": True,
                        "preflight_cancelled": preflight_result == "cancelled",
                        "staging": str(staging_root)}
            pending = []
            pending_items = {}
            processed = 0

            def mark_ready(item, package, selected_plugins):
                ready_item = {key: item.get(key) for key in (
                    "archive", "plugins", "archive_rank", "nexus_mod_id", "nexus_file_id")}
                ready_item["selected_plugins"] = selected_plugins
                ready_item["staging_package"] = str(package.resolve())
                ready_item["ready_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                ready_items = _read_ready_plugin_stages()
                package_key = str(package.resolve()).casefold()
                ready_items = [entry for entry in ready_items
                               if str(Path(entry["staging_package"]).resolve()).casefold() != package_key]
                ready_items.append(ready_item)
                _write_ready_plugin_stages(ready_items)

            def deploy_batch(checkpoint=False):
                nonlocal pending
                if not pending:
                    return True
                if checkpoint:
                    try:
                        os.startfile(str(staging_root))
                    except (AttributeError, OSError):
                        pass
                    proceed = progress.ask_yes_no(
                        "Review staged mods before deployment",
                        f"The next batch of {len(pending)} mod(s) is staged and ready to deploy:\n\n"
                        + "".join(f"  • {label}\n" for _package, label in pending) + "\n"
                        f"Staging folder: {staging_root}\n\n"
                        "Review the opened staging folder. Choose Yes to deploy this batch and continue, or No "
                        "to stop here and leave this batch staged without deploying it.")
                    if not proceed:
                        return False
                # Preflight the entire batch before the first package changes Data Files.
                for package, label in pending:
                    check_deploy_path_lengths(package, data_dir, label)
                from workflow_safety import DurableTransaction
                transaction_root = APP_DIR / "transactions" / "plugin-deploy"
                DurableTransaction.recover(
                    transaction_root, [APP_DIR, data_dir.parent, staging_root])
                transaction = DurableTransaction(
                    transaction_root, [APP_DIR, data_dir.parent, staging_root]).begin()
                try:
                    for path in (
                            data_dir.parent / "Morrowind.ini",
                            APP_DIR / "plugin_deployments.json",
                            APP_DIR / "protected_replacer_textures.json",
                            APP_DIR / "journal" / "install.jsonl",
                            APP_DIR / "journal" / "runs.jsonl",
                            APP_DIR / "game_catalog.sqlite3",
                            PLUGIN_STAGE_MANIFEST):
                        transaction.snapshot(path, database=path.name == "game_catalog.sqlite3")
                    for package, label in pending:
                        progress(f"Deploying {label}…")
                        item = pending_items[str(package)]
                        deployed_label = deployment_label(item, package)
                        if has_altered_assets(data_dir, deployed_label):
                            altered_choices.append((deployed_label, progress.ask_choice(
                                "Altered assets found",
                                f"{label} was rolled back earlier with files that had changed after it was "
                                f"deployed. They are kept in:\n\n"
                                f"{altered_assets(data_dir, deployed_label)}\n\nWhat should happen to them?",
                                [("Restore them over the fresh files", "restore"), ("Delete them", "delete"),
                                 ("Skip (leave them in Off)", "skip")], "skip")))
                        # REMOVE BEFORE SHIPPING: [Dev] compare_steam_lua, developer machine only
                        if self.settings.get("compare_steam_lua"):
                            imported = pull_updated_steam_luas(package, progress.ask_yes_no)
                            if imported:
                                progress(f"Imported {len(imported)} Lua file(s) from the Steam game for {label}.")
                        linked, _skipped, operations = deploy_package(
                            package, data_dir, label, APP_DIR / "protected_replacer_textures.json",
                            transaction=transaction)
                        archives = sorted(package.glob("*.bsa"), key=lambda path: path.name.casefold())
                        registrations = register_archives(data_dir.parent / "Morrowind.ini", archives, APP_DIR)
                        plugins = item.get("selected_plugins", item.get("plugins", []))
                        record_deployment(APP_DIR, item, package, operations, archives, registrations, data_dir)
                        index_deployment(APP_DIR, data_dir, plugins, archives)
                        _remove_ready_plugin_stage(package)
                        progress(f"Deployed {linked:,} files from {label}.")
                    transaction.commit()
                except BaseException:
                    transaction.rollback()
                    raise
                for deployed_label, choice in altered_choices:
                    count = settle_altered_assets(data_dir, deployed_label, choice)
                    if count:
                        progress(f"{'Restored' if choice == 'restore' else 'Deleted'} {count:,} altered "
                                 f"asset(s) of {deployed_label}.")
                altered_choices.clear()
                progress("Running --all checks and resolving newly surfaced cell conflicts…")
                run_batch_check(progress)
                pending = []
                pending_items.clear()
                return True

            batch_names = []                 # packages Tidy staged so far in this install: they ship missing assets

            unpacking = {}                   # archive -> (thread, result, folder): the next one, unpacked early

            def destination_for(index, item):
                """staging/plugins/<short mod name>-<Nexus id>. A folder an earlier run named after the whole
                download ("<download name> [n]") is still found."""
                archive = Path(item["archive"])
                destination = staging_root / staging_folder_name(item)
                if not destination.is_dir():
                    old_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", archive.stem).strip(" .")
                    matching_stages = [path for path in staging_root.iterdir()
                                       if path.is_dir() and path.name.casefold().startswith((old_name + " [").casefold())]
                    if len(matching_stages) == 1:
                        destination = matching_stages[0]
                completed_stage = next((entry for entry in _read_ready_plugin_stages()
                                        if str(Path(entry.get("archive", "")).resolve()).casefold()
                                        == str(archive.resolve()).casefold()), None)
                return destination, completed_stage

            def unpack_ahead(index, item):
                """Unpack the next archive while this one's options are answered (only a fresh one: a folder
                from before still gets its question)."""
                key = str(Path(item["archive"]).resolve()).casefold()
                destination, completed_stage = destination_for(index, item)
                if key in unpacking or destination.exists() or (
                        completed_stage and Path(completed_stage.get("staging_package", "")).is_dir()):
                    return
                result = {}

                def run():
                    try:
                        result["package"] = extract_archive(item, destination, APP_DIR)
                    except BaseException as exc:          # reported when its turn comes
                        result["error"] = exc
                thread = threading.Thread(target=run, name="MWManager unpack ahead", daemon=True)
                thread.start()
                unpacking[key] = (thread, result, destination)

            def locate(index, item):
                """Phase A for one archive: its staging folder (unpacked now, or one from before), then its
                options asked and saved. Returns (package, prepared, install plugins), or a result to stop with."""
                prepared, install_plugins = False, None
                archive = Path(item["archive"])
                destination, completed_stage = destination_for(index, item)
                early = unpacking.pop(str(archive.resolve()).casefold(), None)
                if early:
                    thread, result, destination = early
                    if thread.is_alive():
                        progress(f"Waiting for {archive.name} to finish unpacking…")
                    while thread.is_alive():
                        checkpoint(progress)
                        thread.join(0.2)
                    if "error" in result:
                        raise result["error"]
                    completed_stage = None
                if completed_stage and Path(completed_stage.get("staging_package", "")).is_dir():
                    package = Path(completed_stage["staging_package"]).resolve()
                    item["selected_plugins"] = completed_stage.get("selected_plugins", item.get("plugins", []))
                    progress(f"Using completed staged files for {archive.name}; skipping Tidy and FOMOD.")
                    return package, True, item["selected_plugins"]
                if early:
                    package = result["package"]
                elif destination.exists():
                    action = progress.ask_choice(
                        "Staged folder already exists",
                        f"A staging folder exists for {archive.name}:\n\n{destination}\n\n"
                        "Choose how to continue.",
                        [("Reprocess", "reprocess"), ("Accept staged files as ready", "ready"),
                         ("Cancel", "cancel")], "cancel")
                    if action == "cancel":
                        return {"processed": 0, "stopped": True, "cancelled": True, "staging": str(destination)}
                    package = destination
                    if action == "ready":
                        install_plugins = item.get("plugins", [])
                        item["selected_plugins"] = install_plugins
                        mark_ready(item, package, install_plugins)
                        progress(f"Accepted staged files for {archive.name} without reprocessing.")
                        return package, True, install_plugins
                    progress(f"Reprocessing existing staged files for {archive.name}.")
                else:
                    progress(extraction_status(archive, index, len(items)))
                    package = extract_archive(item, destination, APP_DIR)
                progress(f"Options for {archive.name} ({index} of {len(items)}); staging waits until every "
                         "archive in the batch has its options…")
                if index < len(items):
                    unpack_ahead(index + 1, items[index])
                ask_env = os.environ.copy()
                ask_env["MWMANAGER_SOURCE_ARCHIVE"] = str(archive.resolve())
                ask_env["MWMANAGER_PLUGIN_NAME"] = short_package_name(archive)
                run_tidy_steps(py, staging_root, data_dir, progress, f"Options for {archive.name}",
                               only=package.name, env=ask_env, quiet=quiet, log=log_install_phase, ask_only=True)
                return package, prepared, install_plugins

            if not accept_staged:
                # Phase A: every archive's options first (answers saved); the long staging work comes after, in
                # one stretch that needs no answers
                index = 0
                while index < len(items):
                    item = items[index]
                    index += 1
                    if "_stage" not in item:
                        found = locate(index, item)
                        if isinstance(found, dict):
                            return found
                        item["_stage"] = found
                    if not item.get("needs_checked"):
                        # options held for a mod that isn't installed: that mod's archive goes first
                        item["needs_checked"] = True
                        queued = {str(Path(i["archive"]).resolve()).casefold() for i in items}
                        first = plan_needed_archives(item["_stage"][0].name, APP_DIR / "outputs",
                                                     self.settings.get("archive_downloads_dir"), APP_DIR, queued,
                                                     self.major_masters, progress)
                        if first:
                            items[index - 1:index - 1] = first
                            index -= 1
                progress(f"Options chosen for {len(items):,} archive(s); staging them now.")

            # Phase B: stage each archive with the answers saved above (no option windows)
            for index, item in enumerate(items, 1):
                prepared = False
                if accept_staged:
                    archive = Path(item["archive"])
                    package = Path(item["staging_package"]).resolve()
                    try:
                        package.relative_to(staging_root.resolve())
                    except ValueError:
                        raise RuntimeError(f"Staged package is outside the plugin staging folder: {package}")
                    if not package.is_dir():
                        raise RuntimeError(f"Completed staging folder is missing: {package}")
                    install_plugins = item.get("selected_plugins", item.get("plugins", []))
                    item["selected_plugins"] = install_plugins
                    prepared = True
                    progress(f"Accepting completed staged files for {archive.name} without reprocessing.")
                else:                       # located, and its options asked, in phase A
                    archive = Path(item["archive"])
                    package, prepared, staged_plugins = item.pop("_stage")
                    if prepared:
                        install_plugins = staged_plugins
                    if not prepared:
                        progress(f"Staging {archive.name} with the options chosen ({index} of {len(items)})…")
                        log_install_phase(f"BEGIN package {archive.name}")
                        tidy_env = os.environ.copy()
                        tidy_env["MWMANAGER_SOURCE_ARCHIVE"] = str(archive.resolve())
                        tidy_env["MWMANAGER_REUSE_FOMOD"] = "1"        # the answers given in phase A
                        tidy_env["MWMANAGER_PLUGIN_NAME"] = short_package_name(archive)
                        batch_names.append(package.name)
                        tidy_env["MWMANAGER_INSTALL_BATCH"] = os.pathsep.join(batch_names)
                        tidy_mod_count = run_tidy_steps(py, staging_root, data_dir, progress,
                                                        f"Tidy and options for {archive.name}",
                                                        only=package.name, env=tidy_env, quiet=quiet,
                                                        log=log_install_phase)
                        if tidy_mod_count != 1:
                            raise RuntimeError(f"Tidy did not process the staged package for {archive.name} "
                                               f"(reported {tidy_mod_count!r} mod(s)); deployment is halted.")
                        progress(f"Tidy and options finished for {archive.name}; applying plugin compatibility updates…")
                        log_install_phase(f"TIDY-RETURNED {archive.name}: {tidy_mod_count} staged mod")
                        remastered = apply_plugin_compatibility(package)
                        if remastered:
                            progress("Applied plugin compatibility updates: " + "; ".join(remastered))
                        install_plugins = item.get("selected_plugins", item.get("plugins", []))
                        omitted = remove_superseded_plugins(package, item)
                        if omitted:
                            progress(f"Omitted older duplicate plugin file(s): {', '.join(omitted)}")
                        progress(f"Checking BSA eligibility and final staged contents for {archive.name}…")
                        eligible = count_bsa_assets(package)
                        bsa_offer = int(self.settings.get(BSA_OFFER_SETTING, DEFAULT_BSA_OFFER))
                        if eligible >= bsa_offer and len(install_plugins) == 1:
                            plugin_label = install_plugins[0]
                            make_bsa = bool(self.settings.get(ALWAYS_BSA_SETTING)) or progress.ask_yes_no(
                                "Build a BSA?",
                                f"{plugin_label} has {eligible:,} BSA-compatible assets after Tidy.\n\n"
                                "Build and register a BSA before deployment? BSAPack may split it into multiple archives.")
                            if make_bsa:
                                packed = pack_package_assets(package, plugin_label, APP_DIR, progress)
                                progress(f"Built {len(packed)} BSA archive(s) for {plugin_label}.")
                        elif eligible >= bsa_offer:
                            progress(f"BSA packing skipped for {archive.name}: its assets cannot be attributed to exactly one plugin.")
                        item["selected_plugins"] = install_plugins
                        mark_ready(item, package, install_plugins)
                        log_install_phase(f"READY {archive.name}: {len(install_plugins)} plugin(s), "
                                          f"{eligible} BSA-compatible assets")
                if not item.get("needs_checked"):
                    # options held for a mod that isn't installed: that mod's archive goes first, then this
                    # package again (already staged; its held options go in once the mod is staged)
                    item["needs_checked"] = True
                    queued = {str(Path(i["archive"]).resolve()).casefold() for i in items}
                    first = plan_needed_archives(package.name, APP_DIR / "outputs",
                                                 self.settings.get("archive_downloads_dir"), APP_DIR, queued,
                                                 self.major_masters, progress)
                    if first:
                        items[index:index] = first + [item]
                        continue
                install_label = (Path(install_plugins[0]).stem if install_plugins else archive.stem)
                pending.append((package, install_label))
                pending_items[str(package)] = item
                processed += 1
                if len(pending) >= 15:
                    if not deploy_batch(checkpoint=True):
                        return {"processed": processed, "stopped": True, "staging": str(staging_root)}
            deploy_batch()
            return {"processed": processed, "stopped": False, "staging": str(staging_root)}

        def completed(result, error):
            if error:
                log_install_phase(f"ERROR: {error}")
                if isinstance(error, TidyPlanPaused):
                    self.status.set("Plugin Tidy paused; staged files were preserved for a later retry.")
                    messagebox.showinfo("Plugin Tidy paused", str(error), parent=self)
                    self.refresh()
                    return
                self.status.set("Plugin deployment stopped; completed batches remain deployed.")
                messagebox.showerror("Plugin deployment failed", str(error), parent=self)
                self.refresh()
                return
            if result.get("stopped"):
                self.refresh()
                if result.get("restart"):
                    self._restart_manager()
                    return
                if result.get("cancelled"):
                    if result.get("preflight_cancelled"):
                        self._cancel_until_restart()
                        return
                    if result.get("preflight_blocked"):
                        self.status.set("Pre-deploy checks were not accepted; no staged files were deployed.")
                        messagebox.showinfo(
                            "Deployment paused by preflight",
                            "No files were deployed. Resolve any missing prerequisites or review the declined "
                            "warnings, then retry the deployment.", parent=self)
                        return
                    self.status.set("Plugin staging was left untouched; no files were deployed from the pending batch.")
                    return
                self.status.set("Plugin deployment paused before copying to Data Files; the staged batch is ready for inspection.")
                messagebox.showinfo(
                    "Batch left staged",
                    f"Stopped before deploying the next batch. The staged files are here:\n\n{result['staging']}\n\n"
                    "No files from this batch were deployed. Inspect the staging folder before starting another deployment.",
                    parent=self)
                return
            self.refresh()
            self.gui_state["generated_signature"] = None
            write_gui_state(self.gui_state)
            self._update_action_buttons()
            self.status.set(f"Deployed {result['processed']} archive(s). Generate Patches reflects the updated plugin set.")
            messagebox.showinfo("Plugins deployed", f"Processed {result['processed']} archive(s) in batches of 15 mods.", parent=self)
            if then:                                   # e.g. put in the master options these plugins were for
                self.after(100, then)

        job_title = title or ("Accept staged files as ready" if accept_staged else "Deploy Plugins")
        self._run_progress_job(job_title, "Preparing staged plugin deployment…", worker, completed,
                               description_override=intro)

    def remove_installed_plugins(self):
        from plugin_install import list_deployments
        deployments = list_deployments(APP_DIR)
        if not deployments:
            messagebox.showinfo("No tracked plugin deployments",
                                "MWManager has no regular plugin deployments recorded to remove.", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("Remove Deployed Plugins")
        dialog.transient(self)
        center_window(dialog, self, 760, 420)
        dialog.minsize(620, 320)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Select tracked deployments to remove. Modified or uncertain files are preserved.",
                  wraplength=720).pack(anchor="w", pady=(0, 8))
        tree = ttk.Treeview(frame, columns=("selected", "plugins"), show="tree headings", selectmode="browse")
        tree.heading("#0", text="Archive / deployment")
        tree.heading("selected", text="Remove")
        tree.heading("plugins", text="Plugins")
        tree.column("#0", width=320, stretch=True)
        tree.column("selected", width=65, anchor="center", stretch=False)
        tree.column("plugins", width=280)
        tree.pack(fill="both", expand=True)
        by_id = {}
        selected = set()
        for index, deployment in enumerate(deployments):
            iid = f"deployment-{index}"
            by_id[iid] = deployment
            tree.insert("", "end", iid=iid, text=deployment.get("label", "Unknown archive"),
                        values=("☐", ", ".join(deployment.get("plugins", [])) or "(no plugin file)"))

        def toggle(event):
            iid = tree.identify_row(event.y)
            if iid not in by_id or tree.identify_column(event.x) != "#1":
                return
            if iid in selected:
                selected.remove(iid)
            else:
                selected.add(iid)
            values = list(tree.item(iid, "values"))
            values[0] = "☑" if iid in selected else "☐"
            tree.item(iid, values=values)
        tree.bind("<Button-1>", toggle)

        def remove_selected():
            chosen = [by_id[iid] for iid in by_id if iid in selected]
            if not chosen:
                messagebox.showinfo("Nothing selected", "Select one or more tracked deployments.", parent=dialog)
                return
            if not messagebox.askyesno(
                    "Remove tracked deployments",
                    f"Remove {len(chosen)} selected deployment(s), restore their saved overwritten files, "
                    "unregister their BSAs, and unpack their staged folders again from the archives? "
                    "Any file changed since deployment will be left in place.",
                    parent=dialog):
                return
            ids = {entry["id"] for entry in chosen}
            dialog.destroy()
            data_dir = Path(self.settings["game_dir"]) / "Data Files"

            def worker(progress):
                from plugin_install import remove_deployments
                quiet = {}
                if os.name == "nt":
                    startup = subprocess.STARTUPINFO()
                    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startup.wShowWindow = subprocess.SW_HIDE
                    quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
                from plugin_install import unpack_again
                result = remove_deployments(APP_DIR, data_dir, ids)
                unpack_failures = []
                for item in result.get("unpack", []):
                    package = Path(item["package"])
                    progress(f"Unpacking {item.get('label') or package.name} again from its archive…")
                    try:
                        unpack_again(APP_DIR, package, item.get("archive"))
                        _remove_ready_plugin_stage(package)
                    except (OSError, ValueError, RuntimeError) as exc:
                        unpack_failures.append(str(exc))
                result["unpack_failures"] = unpack_failures
                progress("Updating the plugin catalog and load order…")
                command = [str(self._console_python()), "-u", str(APP_DIR / "mwmanager.py"),
                           str(data_dir), "--sort", "--all", "--yes"]
                process = managed_processes.run(
                    command, cwd=str(APP_DIR), text=True, encoding="utf-8", errors="replace", timeout=7200,
                    env=dict(os.environ, MWMANAGER_GUI="1", MWMANAGER_CONFLICT_GUI="1"), **quiet)
                if process.returncode:
                    raise RuntimeError((process.stderr or process.stdout or "MWManager checks failed")[-4000:])
                return result

            def completed(result, error):
                if error:
                    messagebox.showerror("Plugin removal failed", str(error), parent=self)
                    self.status.set("Plugin removal stopped; tracked files remain available for a safe retry.")
                    return
                self.refresh()
                self.gui_state["generated_signature"] = None
                write_gui_state(self.gui_state)
                self._update_action_buttons()
                self.status.set(f"Removed {result['removed']:,} files; restored {result['restored']:,}. "
                                f"{result['pending']:,} changed file(s) remain tracked; "
                                f"{len(result.get('altered', [])):,} altered asset(s) kept in Off; "
                                f"{len(result.get('unpack_failures', [])):,} staged folder(s) not unpacked again.")
                tidy_note = ""
                if result.get("unpack_failures"):
                    tidy_note = "\n\nNot unpacked again from the archive:\n" + "\n".join(
                        result["unpack_failures"][:5])
                messagebox.showinfo("Plugin deployments removed",
                                    f"Removed files: {result['removed']:,}\nRestored files: {result['restored']:,}\n"
                                    f"Altered assets kept in Data Files\\Off\\<plugin>\\Altered Assets: "
                                    f"{len(result.get('altered', [])):,}\n"
                                    f"Still tracked for review: {result['pending']:,}{tidy_note}", parent=self)

            self._run_progress_job("Remove Deployed Plugins", "Removing selected tracked deployments…",
                                   worker, completed)

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Button(footer, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(footer, text="Remove Selected", command=remove_selected).pack(side="right", padx=(0, 6))

    def _console_python(self) -> Path:
        executable = Path(sys.executable)
        if executable.name.casefold() == "pythonw.exe":
            console = executable.with_name("python.exe")
            if console.is_file():
                return console
        return executable

    def _force_generate_patches(self, _event=None):
        if self._active_job or self.settings.get("init_complete", 0) != 1:
            return "break"
        self.generate_patches(mode="all", force=True)
        return "break"

    def _force_full_generate_patches(self, _event=None):
        if self._active_job or self.settings.get("init_complete", 0) != 1:
            return "break"
        self.generate_patches(mode="full", force=True)
        return "break"

    def generate_patches(self, mode="all", force=False, on_success=None, *,
                         skip_first_pass=False, on_first_pass=None, on_review=None, on_spawn=None,
                         on_failure=None):
        """skip_first_pass / on_first_pass / on_review / on_spawn / on_failure: setup's checkpoints (called from
        the worker, so they must not touch Tk). on_review: a conflict review is starting, the pass runs again."""
        if self._active_job:
            return
        if not force and not self.generate_button.instate(["!disabled"]):
            return
        if not force:
            signature = self._current_generation_signature()
            if (not self.new_plugins()
                    and signature == self.gui_state.get("generated_signature")):
                return
        if not self.save():
            return
        self.generate_button.state(["disabled"])
        self.enable_new_button.state(["disabled"])
        self.play_button.state(["disabled"])
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        script = APP_DIR / "mwmanager.py"
        mode = "full" if mode == "full" else "all"
        pass_flag = "--full" if mode == "full" else "--all"
        command = [str(self._console_python()), "-u", str(script), str(data_dir), "--sort", pass_flag, "--yes"]
        report_path = APP_DIR / "outputs" / "mwmanager_report.txt"
        try:
            report_before = report_path.stat().st_mtime_ns
        except OSError:
            report_before = None

        def worker(report):
            quiet = {}
            if os.name == "nt":
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
            env = os.environ.copy()
            env["MWMANAGER_GUI"] = "1"
            env["MWMANAGER_CONFLICT_GUI"] = "1"
            recent = []
            summary = []            # the run's own end-of-run summary, shown when the job window has closed

            def run_full_pass(label):
                checkpoint(report)
                report(label)
                summary.clear()
                process = _popen(command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                           errors="replace", bufsize=1, env=env, **quiet)
                if on_spawn:
                    on_spawn(getattr(process, "pid", None))
                for line in process.stdout:
                    message = " ".join(line.strip().split())
                    if message:
                        recent.append(message)
                        del recent[:-80]
                        if message == "--full summary":
                            summary[:] = [message]                      # --full: its closing summary block
                        elif summary[:1] == ["--full summary"]:
                            summary.append(line.strip())                # raw: the step columns are space-aligned
                            del summary[40:]
                        elif re.match(r"Found \d+ ERROR", message):     # --all: its last report count
                            summary[:] = [message]
                        report(_display_line(line))
                result = process.wait()
                if result:
                    raise RuntimeError("MWManager's full plugin checks did not complete.\n\n" +
                                       ("\n".join(recent)[-5000:] or f"mwmanager.py exited with code {result}"))
                checkpoint(report)

            if skip_first_pass:
                report(f"MWMANAGER-STAGE: 1 of 4 — The --{mode} pass finished before the interruption; not repeated")
            else:
                report(f"MWMANAGER-STAGE: 1 of 4 — Sorting plugins and running --{mode} checks")
                run_full_pass(f"Running --{mode} checks and generating patches…")
                if on_first_pass:
                    on_first_pass()

            # Find unanswered cell conflicts without blocking the GUI process. The existing
            # resolver is opened only when there are decisions to make.
            report("MWMANAGER-STAGE: 2 of 4 — Scanning for unresolved cell conflicts")
            checkpoint(report)
            unresolved_path = APP_DIR / "outputs" / "mwmanager_new_conflicts.txt"
            scan_command = [str(self._console_python()), "-u", str(script), str(data_dir),
                            "--cell-conflicts", "--no-interactive"]
            scan = _popen(scan_command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1, env=env, **quiet)
            scan_output = []
            for line in scan.stdout:
                message = " ".join(line.strip().split())
                if message:
                    scan_output.append(message)
                    del scan_output[:-40]
                    report(_display_line(line))
            scan_result = scan.wait()
            if scan_result:
                raise RuntimeError("The cell-conflict scan did not complete.\n\n" +
                                   ("\n".join(scan_output)[-4000:] or
                                    f"mwmanager.py exited with code {scan_result}"))
            checkpoint(report)
            try:
                unresolved = [line.strip() for line in unresolved_path.read_text(encoding="utf-8").splitlines()
                              if line.strip()]
            except OSError:
                unresolved = []
            if unresolved:
                if on_review:
                    on_review()
                report(f"MWMANAGER-STAGE: 3 of 4 — Reviewing {len(unresolved):,} cell-conflict pair(s)")
                report(f"Reviewing {len(unresolved):,} unresolved cell conflict pair(s)…")
                conflict_command = [str(self._console_python()), "-u", str(script), str(data_dir),
                                    "--cell-conflicts", "--only-new", "--defer-follow-up"]
                report("Opening the native cell-conflict picker. Finish review to keep decisions made so far.")
                conflict_env = dict(env, MWMANAGER_CONFLICT_GUI="1")
                conflict_process = _popen(
                    conflict_command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", env=conflict_env, **quiet)
                conflict_output = []
                for line in conflict_process.stdout:
                    conflict_output.append(line.rstrip())
                    del conflict_output[:-40]
                    report(_display_line(line))
                conflict_result = conflict_process.wait()
                if conflict_result:
                    raise RuntimeError("The cell-conflict review did not complete.\n\n" +
                                       "\n".join(conflict_output))
                checkpoint(report)
                report("MWMANAGER-STAGE: 4 of 4 — Applying choices and rebuilding patches")
                run_full_pass(f"Applying saved choices and rebuilding patches with --{mode}…")
            else:
                report("MWMANAGER-STAGE: 3 of 4 — No new cell conflicts; finishing")
            return {"summary": list(summary)}

        def completed(_result, error):
            if error:
                if on_failure:
                    on_failure(error)
                self.status.set("Patch generation stopped; changes are still pending.")
                try:
                    report_changed = report_path.stat().st_mtime_ns != report_before
                except OSError:
                    report_changed = False
                if report_changed:
                    self._show_report("MWManager report", report_path)
                else:
                    messagebox.showerror("Generate Patches failed", str(error), parent=self)
                log = APP_DIR / "outputs" / "mwmanager_status.log"
                if log.is_file() and messagebox.askyesno(
                        "Generate Patches failed", "Open the full log of this run to see where it stopped?",
                        parent=self):
                    self._show_report("MWManager status log", log)
                self._update_action_buttons()
                return
            self.order = read_order()
            self.active_names = {name.casefold() for name in self.order}
            self.refresh()
            self.gui_state["game_dir"] = str(Path(self.settings["game_dir"]).resolve())
            self.gui_state["known_plugins"] = plugins_in_data(data_dir)
            self.gui_state["generated_signature"] = self._current_generation_signature()
            write_gui_state(self.gui_state)
            self.refresh()
            self.status.set("Plugin checks and patch generation completed. Play is ready.")
            if not on_success:                  # setup runs report through the setup window instead
                self._report_generation(_result, report_path)
            if on_success:
                self.after(100, on_success)

        self._run_progress_job("Generate Patches", "Checking plugins and generating patches…", worker, completed)

    def _report_generation(self, result, report_path: Path) -> None:
        """After the job window closes: did it finish cleanly, which steps reported issues, and the report."""
        lines = (result or {}).get("summary", []) if isinstance(result, dict) else []
        issues = [re.sub(r"\s{2,}.*$", "", line) for line in lines
                  if re.search(r"\s{2,}(issues reported|failed|stopped|skipped)\b", line)]
        active = next((line for line in lines if line.startswith("Active plugins now")), "")
        clean = any("finished with no errors" in line or re.match(r"Found 0 ERROR", line) for line in lines)
        text = ("Finished with no errors." if clean else "Finished.") + (f" {active}." if active else "")
        if issues:
            text += "\n\nSteps that reported issues (nothing was left broken):\n" + "".join(f"  • {s}\n" for s in issues)
            if report_path.is_file() and messagebox.askyesno("Generate Patches finished",
                                                             text + "\nOpen the report?", parent=self):
                self._show_report("MWManager report", report_path)
            return
        messagebox.showinfo("Generate Patches finished", text, parent=self)

    def play_game(self):
        if not self._resolve_pending_load_order("playing"):
            return
        self._update_action_buttons()
        if self.play_button.instate(["disabled"]):
            return
        run_full = messagebox.askyesno(
            "Full check before playing",
            "Run --full before launching? This rebuilds the auto-merge patch and may take a while.\n\n"
            "Choose No to launch now without a full rebuild.", parent=self)
        command = [str(self._console_python()), "-u", str(APP_DIR / "mwmanager.py"),
                   str(Path(self.settings["game_dir"]) / "Data Files"),
                   "--full", "--play"] if run_full else [
                       str(self._console_python()), str(APP_DIR / "mwmanager.py"),
                       str(Path(self.settings["game_dir"]) / "Data Files"), "--play"]
        if run_full:
            def worker(report):
                quiet = {}
                if os.name == "nt":
                    startup = subprocess.STARTUPINFO()
                    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startup.wShowWindow = subprocess.SW_HIDE
                    quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
                process = _popen(command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                           errors="replace", bufsize=1,
                                           env=dict(os.environ, MWMANAGER_GUI="1", MWMANAGER_CONFLICT_GUI="1"),
                                           **quiet)
                recent = []
                for line in process.stdout:
                    message = " ".join(line.strip().split())
                    if message:
                        recent.append(message)
                        del recent[:-80]
                        report(_display_line(line))
                result = process.wait()
                if result:
                    raise RuntimeError("The full checks or game launch did not complete.\n\n" +
                                       ("\n".join(recent)[-5000:] or
                                        f"mwmanager.py exited with code {result}"))
                return True

            def completed(_result, error):
                if error:
                    self.status.set("Full checks or game launch stopped with an error.")
                    messagebox.showerror("Full check failed", str(error), parent=self)
                else:
                    self.status.set("Full checks completed; Morrowind is starting.")

            self._run_progress_job("Full Check and Play", "Running full checks before launch…", worker, completed)
            return
        quiet = {}
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
        try:
            subprocess.Popen(command, cwd=str(APP_DIR), **quiet)
            self.status.set("Starting Morrowind…")
        except OSError as exc:
            messagebox.showerror("Could not start Morrowind", str(exc), parent=self)

    def selected_name(self):
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _update_buttons(self):
        if getattr(self, "_active_job", None):
            return
        name = self.selected_name()
        if not name:
            self.up_btn.state(["disabled"])
            self.down_btn.state(["disabled"])
            return
        core = name.casefold() in {x.casefold() for x in CORE}
        is_merged = name.casefold() in {n.casefold() for n in self.merge_inputs}
        installed = (Path(self.settings["game_dir"]) / "Data Files" / name).is_file()
        is_major = name.casefold() in {n.casefold() for n in self.major_masters}
        is_fixed = name.casefold() in LOCKED_PATCHES or name.casefold() == XE_SKY_PLUGIN.casefold()
        locked = core or is_merged or is_major or is_fixed or not installed
        self.up_btn.state(["disabled"] if locked or not self._can_move(name, -1) else ["!disabled"])
        self.down_btn.state(["disabled"] if locked or not self._can_move(name, 1) else ["!disabled"])

    def _can_move(self, name, step):
        if name not in self.order:
            return False
        index = self.order.index(name)
        neighbor = index + step
        if not 0 <= neighbor < len(self.order):
            return False
        other = self.order[neighbor]
        barriers = {n.casefold() for n in CORE} | {n.casefold() for n in self.major_masters} | LOCKED_PATCHES | {XE_SKY_PLUGIN.casefold()}
        if other.casefold() in barriers or name.casefold() in barriers:
            return False
        if name.casefold() in {n.casefold() for n in self.merge_inputs} or other.casefold() in {n.casefold() for n in self.merge_inputs}:
            return False
        return Path(name).suffix.casefold() == Path(other).suffix.casefold()

    def _click_active_checkbox(self, event):
        if getattr(self, "_active_job", None):
            return "break"
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        name = self.tree.identify_row(event.y)
        if not name:
            return
        return self._toggle_active([name])

    def _toggle_active_key(self, _event=None):
        """Space: toggle the selected plugins, as clicking their checkboxes would."""
        if getattr(self, "_active_job", None):
            return "break"
        return self._toggle_active(list(self.tree.selection()))

    def _toggle_active(self, names):
        toggled = []
        for name in names:
            core = name.casefold() in {n.casefold() for n in CORE}
            merged = name.casefold() in {n.casefold() for n in self.merge_inputs}
            is_major = name.casefold() in {n.casefold() for n in self.major_masters}
            is_fixed = name.casefold() in LOCKED_PATCHES or name.casefold() == XE_SKY_PLUGIN.casefold()
            installed = (Path(self.settings["game_dir"]) / "Data Files" / name).is_file()
            if core or merged or is_major or is_fixed or not installed:
                continue
            key = name.casefold()
            if key in self.active_names:
                self.active_names.discard(key)
            else:
                self.active_names.add(key)
            toggled.append(name)
        if not toggled:
            return "break"
        self.load_order_dirty = True
        self.refresh()
        self.tree.selection_set(names)
        self.status.set("Plugin checkbox change is pending. Save load order to apply it.")
        return "break"

    def move_selected(self, step):
        if getattr(self, "_active_job", None):
            return
        name = self.selected_name()
        if not name or not self._can_move(name, step):
            return
        old = self.order.index(name)
        new = old + step
        self.order[old], self.order[new] = self.order[new], self.order[old]
        if step > 0:
            # Moving down: the plugin above it must load before the moved plugin.
            pair = (self.order[new - 1], name)
        else:
            # Moving up: the moved plugin must load before the plugin now below it.
            pair = (name, self.order[new + 1])
        self.pending_order_rules.append(pair)
        self.load_order_dirty = True
        self.refresh()
        self.tree.selection_set(name)
        self.tree.see(name)
        self.status.set("Load order change is pending. Save load order to apply it.")

    def save(self):
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        merged = {n.casefold() for n in self.merge_inputs}
        disk_names = plugins_in_data(data_dir)
        disk = {n.casefold() for n in disk_names}
        names = [n for n in self.order if n.casefold() not in merged and
                 n.casefold() in disk and
                 (n.casefold() in self.active_names or n.casefold() in {x.casefold() for x in CORE})]
        for core in CORE:
            if core.casefold() not in {n.casefold() for n in names}:
                names.insert(CORE.index(core), core)
        enabled = {name.casefold() for name in names}
        protected = {name.casefold() for name in CORE} | merged | LOCKED_PATCHES
        disabled_now = [name for name in disk_names
                        if name.casefold() not in enabled and name.casefold() not in protected]
        state_paths = (DISABLED_RECORD, SORT_FILE, APP_DIR / "mwmanager_rules.txt")
        try:
            previous_state = {path: path.read_bytes() if path.is_file() else None for path in state_paths}
        except OSError as exc:
            messagebox.showerror("Could not save load order", str(exc), parent=self)
            return False
        moved = []
        journal = run = None
        if disabled_now:
            import mwmanager_journal
            journal = mwmanager_journal.Journal(APP_DIR / "journal")
            if journal.has_baseline():                  # rollback can then explain and undo the move
                run = journal.start_run("mwmanager_gui.py", {"operation": "disable plugins"})
        try:
            for name in disabled_now:
                source = data_dir / name
                target = disabled_plugins_dir() / name
                earlier = next((p for p in (target, data_dir / "Off" / "Disabled Plugins" / name) if p.exists()), None)
                if earlier:
                    raise FileExistsError(f"Cannot disable {name}; a preserved copy already exists at {earlier}")
                if target.exists():
                    raise FileExistsError(f"Cannot disable {name}; a preserved copy already exists at {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if run:
                    journal.move_original(data_dir.parent, source, target, run["run"], tag="disabled-plugin")
                else:
                    os.replace(source, target)
                moved.append((source, target))
        except OSError as exc:
            if run:
                journal.finish_run(run)
            rollback_errors = []
            for source, target in reversed(moved):
                try:
                    if target.is_file() and not source.exists():
                        os.replace(target, source)
                except OSError as rollback_error:
                    rollback_errors.append(f"{target} -> {source}: {rollback_error}")
            message = str(exc)
            if rollback_errors:
                message += "\n\nRollback incomplete:\n" + "\n".join(rollback_errors)
            messagebox.showerror("Could not disable plugin", message, parent=self)
            return False
        if run:
            journal.finish_run(run)
        disabled = read_disabled_plugins()
        for name in disabled_now:
            disabled[name] = "disabled in MWManager load-order panel"
        try:
            write_disabled_plugins(disabled)
            write_order(names)
            self._write_manual_order_rules()
        except OSError as exc:
            rollback_errors = []
            for source, target in reversed(moved):
                try:
                    if target.is_file() and not source.exists():
                        os.replace(target, source)
                except OSError as rollback_error:
                    rollback_errors.append(f"{target} -> {source}: {rollback_error}")
            for path, content in previous_state.items():
                try:
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_write(path, content)
                except OSError as rollback_error:
                    rollback_errors.append(f"{path}: {rollback_error}")
            message = str(exc)
            if rollback_errors:
                message += "\n\nRollback incomplete:\n" + "\n".join(rollback_errors)
            messagebox.showerror("Could not save load order", message, parent=self)
            return False
        self.order = names
        self.active_names = {n.casefold() for n in names}
        self.load_order_dirty = False
        self.refresh()
        total_disabled = len(read_disabled_plugins())
        self.status.set(f"Saved {len(names)} active plugins; {total_disabled} plugin(s) disabled"
                        + (" (Show Disabled lists them)." if total_disabled else "."))
        return True

    def show_disabled_plugins(self):
        """List disabled plugin records and restore selected files into the active load order."""
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        off_dirs = (disabled_plugins_dir(), data_dir / "Off" / "Disabled Plugins",   # new place, then the old one
                    data_dir / "Off" / "Official Add-Ons")       # an add-on a master replaces (Siege at Firemoth)
        entries = read_disabled_plugins()
        for off_dir in off_dirs:
            if off_dir.is_dir():
                for path in off_dir.iterdir():
                    if path.is_file() and path.suffix.casefold() in {".esp", ".esm"}:
                        entries.setdefault(path.name, "found among the disabled plugins")

        dialog = tk.Toplevel(self)
        dialog.title("Disabled plugins")
        dialog.transient(self)
        center_window(dialog, self, 680, 440)
        dialog.minsize(560, 320)
        ttk.Label(dialog, text="Disabled ESPs and ESMs are kept in MWManager's disabled_plugins folder. "
                  "Select plugins to restore and enable them.", wraplength=640).pack(anchor="w", padx=12, pady=(12, 8))
        body = ttk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=12, pady=4)
        tree = ttk.Treeview(body, columns=("reason", "location"), show="tree headings", selectmode="extended")
        tree.heading("#0", text="Plugin")
        tree.heading("reason", text="Why disabled")
        tree.heading("location", text="File")
        tree.column("#0", width=235, stretch=True)
        tree.column("reason", width=235, stretch=True)
        tree.column("location", width=120, anchor="center", stretch=False)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def refresh_rows():
            tree.delete(*tree.get_children())
            for name, reason in sorted(entries.items(), key=lambda item: item[0].casefold()):
                in_off = any((off_dir / name).is_file() for off_dir in off_dirs)
                in_data = (data_dir / name).is_file()
                location = "Off" if in_off else "Data Files" if in_data else "Missing"
                tree.insert("", "end", iid=name, text=name,
                            values=(reason, location))

        def reenable_selected():
            selected = list(tree.selection())
            if not selected:
                return
            for name in selected:
                entries.setdefault(name, "re-enabled from Show Disabled")
            write_disabled_plugins(entries)
            command = [sys.executable, str(APP_DIR / "mwmanager.py"), str(data_dir), "--reenable", *selected]
            try:
                result = managed_processes.run(command, cwd=str(APP_DIR), text=True,
                                               errors="replace", timeout=180)
            except (OSError, subprocess.TimeoutExpired) as exc:
                messagebox.showerror("Could not re-enable plugins", str(exc), parent=dialog)
                return
            if result.returncode:
                messagebox.showerror("Could not re-enable plugins", (result.stderr or result.stdout).strip(),
                                     parent=dialog)
                return
            for name in selected:
                entries.pop(name, None)
            write_disabled_plugins(entries)
            self.order = read_order()
            self.active_names = {name.casefold() for name in self.order}
            self.refresh()
            self.status.set("Re-enabled: " + ", ".join(selected))
            refresh_rows()

        refresh_rows()
        footer = ttk.Frame(dialog, padding=(12, 8, 12, 12))
        footer.pack(fill="x")
        ttk.Button(footer, text="Re-enable selected", command=reenable_selected).pack(side="right")
        ttk.Button(footer, text="Close", command=dialog.destroy).pack(side="right", padx=(0, 7))

    def _write_manual_order_rules(self):
        if not self.pending_order_rules:
            return
        rules_path = APP_DIR / "mwmanager_rules.txt"
        current = rules_path.read_text(encoding="cp1252") if rules_path.is_file() else ""
        if not current.strip():
            current = USER_RULES_TEMPLATE
        fresh = []
        seen = set()
        for first, second in reversed(self.pending_order_rules):
            key = (first.casefold(), second.casefold())
            if key not in seen:
                seen.add(key)
                fresh.append((first, second))
        block = "[Order]\n" + "\n".join(name for pair in fresh for name in pair) + "\n\n"
        text = (block + current.lstrip("\r\n")).replace("\r\n", "\n").replace("\n", "\r\n")
        _atomic_write(rules_path, text.encode("cp1252"))
        self.pending_order_rules.clear()

    def open_sort_rules(self):
        """Open the exact rules file used by mwmanager.py --sort in the user's editor."""
        rules_path = APP_DIR / "mwmanager_rules.txt"
        if not rules_path.exists():
            try:
                rules_path.write_text(USER_RULES_TEMPLATE, encoding="cp1252", newline="\r\n")
            except OSError as exc:
                messagebox.showerror("Could not create sort rules", str(exc), parent=self)
                return
        try:
            if os.name == "nt":
                os.startfile(str(rules_path))
            else:
                import webbrowser
                webbrowser.open(rules_path.as_uri())
        except (OSError, AttributeError) as exc:
            messagebox.showerror("Could not open sort rules", str(exc), parent=self)

    def open_major_masters(self):
        existing = getattr(self, "_major_master_dialog", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            return
        dialog = tk.Toplevel(self)
        self._major_master_dialog = dialog
        dialog.title("Major Masters")
        setup = getattr(getattr(self, "_setup_view", None), "window", None)
        over = setup if setup is not None and setup.winfo_exists() and setup.winfo_viewable() else self
        dialog.transient(over)                          # during setup: on top of the setup window, not hidden behind it
        center_window(dialog, over, 780, 680)
        dialog.minsize(680, 600)
        masters = list(self.major_masters)
        reorder_warning_shown = False

        def master_state():
            return tuple(masters), frozenset(n.casefold() for n in masters
                                             if n.casefold() in self.selected_major_masters)
        saved_state = [master_state()]
        save_button = {}

        def update_save_label():
            if "button" in save_button:
                save_button["button"].configure(
                    text="Save master order" if master_state() != saved_state[0] else "Done")
        try:
            saved_master_data = json.loads(MAJOR_MASTER_CONFIG.read_text(encoding="utf-8"))
            keywords = {name: saved_master_data.get(name, []) for name in masters}
        except (OSError, ValueError):
            keywords = {name: [] for name in masters}
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Optional community masters", font=FONTS["h2"]).pack(anchor="w")
        ttk.Label(frame, text="Staged and deployed masters can be enabled and prioritized here. Missing masters stay in this catalog only.", wraplength=660).pack(anchor="w", pady=(3, 8))
        tree_box = ttk.Frame(frame)
        tree_box.pack(fill="both", expand=True)
        tree = ttk.Treeview(tree_box, columns=("active", "installed"), show="tree headings", selectmode="extended")
        tree.heading("#0", text="Priority / Master")
        tree.heading("active", text="Enable")
        tree.heading("installed", text="Status")
        tree.column("#0", width=390, stretch=True)
        tree.column("active", width=70, anchor="center", stretch=False)
        tree.column("installed", width=100, anchor="center")
        tree.grid(row=0, column=0, sticky="nsew")
        master_scroll = ttk.Scrollbar(tree_box, orient="vertical", command=tree.yview)
        master_scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=master_scroll.set)
        tree_box.columnconfigure(0, weight=1)
        tree_box.rowconfigure(0, weight=1)
        master_status_tags(tree)
        toggleable, last_scan = set(), {}        # rows the Enable column may flip, from the last refresh; a rescan per click froze the dialog

        def update_deploy_button(staged_keys, unfinished):
            self.deploy_master_button.state(["!disabled"] if any(
                name.casefold() in self.selected_major_masters and name.casefold() in staged_keys
                and name.casefold() not in unfinished
                and not (data_dir / name).is_file() for name in masters) else ["disabled"])

        def scan_masters():
            from major_master_deploy import deployment_statuses, staged_masters
            staged_map = staged_masters(APP_DIR / "staging" / "major_masters", masters)
            statuses = deployment_statuses(masters, data_dir, APP_DIR, staged=staged_map)
            required_names = self._scan_required_major_master_names()
            unfinished = {name.casefold()
                          for name in self._unfinished_master_stages(masters, staged=staged_map)}
            from major_master_deploy import tracked_deployment_masters
            return {
                "staged": staged_map, "statuses": statuses,
                "required": required_names, "unfinished": unfinished,
                "tracked": tracked_deployment_masters(APP_DIR),
            }

        def render_master_scan(scan):
            if not dialog.winfo_exists():
                return
            staged_map = scan["staged"]
            statuses = scan["statuses"]
            required_names = scan["required"]
            unfinished = scan["unfinished"]
            tree.delete(*tree.get_children())
            toggleable.clear()
            for i, name in enumerate(masters, 1):
                key = name.casefold()
                status_info = statuses[key]
                exists = status_info["state"] == "deployed"
                enabled = key in self.selected_major_masters
                ready = status_info["state"] == "ready"
                required = name.casefold() in required_names
                if (exists or ready) and not required:
                    toggleable.add(key)
                if required and (exists or ready):
                    self.selected_major_masters.add(key)
                    if exists:
                        self.active_names.add(name.casefold())
                check = "☑" if (exists or ready) and (enabled or required) else "☐"
                state = status_info["label"]
                if ready and key in unfinished:
                    state = "Needs processing"
                if status_info["state"] in {"recovery", "missing", "untracked"}:
                    tag = "problem"
                else:
                    tag = "locked" if required else "deployed" if exists else "staged" if ready else "not_staged"
                tree.insert("", "end", iid=name, text=f"{i:02d}  {name}", values=(check, state), tags=(tag,))
            last_scan.update(staged=staged_map, unfinished=unfinished, required=required_names,
                             tracked=scan["tracked"])
            update_master_buttons()

        scan_generation = {"value": 0}

        def refresh_masters(background=False):
            if not background:
                render_master_scan(scan_masters())
                return
            scan_generation["value"] += 1
            generation = scan_generation["value"]
            results = queue.Queue()
            tree.delete(*tree.get_children())
            tree.insert("", "end", iid="__scanning__", text="Scanning staged and deployed masters…",
                        values=("", "Please wait"), tags=("not_staged",))
            dialog.update_idletasks()
            wait = tk.Toplevel(dialog)
            wait.title("Scanning Major Masters")
            wait.transient(dialog)
            center_window(wait, dialog, 440, 150)
            wait.resizable(False, False)
            body = ttk.Frame(wait, padding=18)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="Scanning staged and deployed masters…",
                      font=FONTS["h2"]).pack(anchor="w")
            ttk.Label(body, text="The window is painted and the scan is running in the background. "
                                  "Large staged packages can take a while; MWManager is not hung.",
                      wraplength=400).pack(anchor="w", pady=(6, 12))
            bar = ttk.Progressbar(body, mode="indeterminate")
            bar.pack(fill="x")
            bar.start(40)
            wait.grab_set()
            wait.protocol("WM_DELETE_WINDOW", lambda: None)

            def worker():
                try:
                    scan = scan_masters()
                except Exception as exc:
                    scan = {"error": str(exc)}
                results.put(scan)

            def finish_when_ready():
                if not dialog.winfo_exists() or generation != scan_generation["value"]:
                    if wait.winfo_exists():
                        wait.destroy()
                    return
                try:
                    scan = results.get_nowait()
                except queue.Empty:
                    dialog.after(25, finish_when_ready)
                    return
                bar.stop()
                if wait.winfo_exists():
                    wait.grab_release()
                    wait.destroy()
                if "error" not in scan:
                    render_master_scan(scan)
                else:
                    tree.delete(*tree.get_children())
                    tree.insert("", "end", iid="__error__", text="Major-master scan failed",
                                values=("", scan["error"][:180]), tags=("problem",))

            threading.Thread(target=worker, name="MWManager major-master scan",
                             daemon=True).start()
            dialog.after(25, finish_when_ready)

        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(8, 0))
        up = ttk.Button(controls, text="Move up")
        down = ttk.Button(controls, text="Move down")
        add_button = ttk.Button(controls, text="Add master…")
        up.pack(side="left")
        down.pack(side="left", padx=(5, 0))
        add_button.pack(side="left", padx=(12, 0))
        legend = ttk.Frame(frame)
        legend.pack(fill="x", pady=(7, 0))
        master_status_legend(legend, ("deployed", "staged", "not_staged", "locked", "problem"))
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(10, 0))
        footer_top = ttk.Frame(footer)
        footer_top.pack(fill="x")
        footer_bottom = ttk.Frame(footer)
        footer_bottom.pack(fill="x", pady=(SPACE["sm"], 0))

        def update_master_buttons(_event=None):
            """Every Major Masters button's enabled state, from the last refresh's scan."""
            update_save_label()
            selection = tree.selection()
            update_deploy_button(last_scan.get("staged", {}), last_scan.get("unfinished", set()))
            self.remove_master_button.state(["!disabled"] if any(
                (data_dir / name).is_file() for name in last_scan.get("tracked", [])) else ["disabled"])
            # nothing selected reopens every staged master
            self.stage_master_button.state(["!disabled"] if any(
                name.casefold() in last_scan.get("staged", {}) for name in selection or masters) else ["disabled"])
            if not selection:
                up.state(["disabled"])
                down.state(["disabled"])
                if hasattr(self, "unstage_master_button"):
                    self.unstage_master_button.state(["disabled"])
                return
            idx = masters.index(selection[0])
            single = len(selection) == 1                        # reordering moves one row at a time
            up.state(["!disabled"] if single and idx > 0 else ["disabled"])
            down.state(["!disabled"] if single and idx < len(masters) - 1 else ["disabled"])
            if hasattr(self, "unstage_master_button"):
                mapping, required = last_scan.get("staged", {}), last_scan.get("required", set())
                ready = any(name.casefold() in mapping and not (data_dir / name).is_file()
                            and name.casefold() not in required for name in selection)
                if ready:
                    from major_master_deploy import assert_package_disposable
                    try:
                        for name in selection:
                            source = mapping.get(name.casefold())
                            if source:
                                package = next(p for p in source.parents
                                               if p.parent == APP_DIR / "staging" / "major_masters")
                                assert_package_disposable(package, package.parent, data_dir, APP_DIR, masters)
                    except (ValueError, RuntimeError, StopIteration):
                        ready = False
                self.unstage_master_button.state(["!disabled"] if ready else ["disabled"])

        def unstage_selected():
            import mwmanager_dialogs as messagebox
            import shutil
            from core_addons import _rmtree_clear_readonly
            from major_master_deploy import staged_masters
            selection = tree.selection()
            if not selection:
                return
            staging_root = (APP_DIR / "staging" / "major_masters").resolve()
            staged_map = staged_masters(staging_root, masters)
            required = self._required_major_master_names()
            packages = []
            for name in selection:
                esm = staged_map.get(name.casefold())
                if esm is None or (data_dir / name).is_file() or name.casefold() in required:
                    continue
                package = next((parent for parent in esm.parents if parent.parent == staging_root), None)
                if (package is None or package.resolve() == staging_root
                        or not package.resolve().is_relative_to(staging_root)):
                    messagebox.showerror("Cannot unstage master",
                                         "The staged package path is outside major-master staging.", parent=dialog)
                    return
                if package not in packages:
                    packages.append(package)
            if not packages:
                return
            resolved = [package.resolve() for package in packages]
            package_masters = [key for key, path in staged_map.items()
                               if any(root in path.resolve().parents for root in resolved)]
            display = [next((master for master in masters if master.casefold() == key), key)
                       for key in package_masters]
            if not messagebox.askyesno(
                    "Unstage major-master packages?",
                    f"Remove {len(packages)} staged package(s): {', '.join(p.name for p in packages)}?\n\n"
                    f"Masters in them: {', '.join(display)}\n\n"
                    "The live Data Files folder will not be changed.", parent=dialog):
                return
            def remove_package(progress):
                from major_master_deploy import unstage_packages
                unstage_packages(packages, staging_root, data_dir, APP_DIR, masters, progress)
                return display

            def package_removed(result, error):
                if error:
                    messagebox.showerror("Could not unstage package", str(error), parent=dialog)
                    return
                self.selected_major_masters.difference_update(package_masters)
                self.active_names.difference_update(package_masters)
                self.gui_state["selected_major_masters"] = [master for master in masters
                                                            if master.casefold() in self.selected_major_masters]
                write_gui_state(self.gui_state)
                refresh_masters()
                self.status.set(f"Unstaged {', '.join(result)}. Live game files were not changed.")

            self._run_progress_job("Unstage major master", f"Removing {len(packages)} staged package(s)…",
                                   remove_package, package_removed)

        def click_master(event):
            if getattr(self, "_active_job", None):
                return "break"
            if tree.identify("region", event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
                return
            name = tree.identify_row(event.y)
            if not name or name.casefold() not in toggleable:
                return "break"
            if name.casefold() in self.selected_major_masters:
                self.selected_major_masters.discard(name.casefold())
                self.active_names.discard(name.casefold())
            else:
                self.selected_major_masters.add(name.casefold())
                self.active_names.add(name.casefold())
            self.gui_state["selected_major_masters"] = [n for n in masters
                                                        if n.casefold() in self.selected_major_masters]
            write_gui_state(self.gui_state)
            values = list(tree.item(name, "values"))
            values[0] = "☑" if name.casefold() in self.selected_major_masters else "☐"
            tree.item(name, values=values)
            update_master_buttons()
            tree.selection_set(name)
            return "break"

        def move_master(step):
            nonlocal reorder_warning_shown
            selection = tree.selection()
            if not selection:
                return
            name = selection[0]
            idx = masters.index(name)
            new_idx = idx + step
            if not 0 <= new_idx < len(masters):
                return
            if not reorder_warning_shown:
                if not messagebox.askyesno(
                        "Change the recommended master order?",
                        "MWManager's supplied order is the recommended load order. Changing it can cause "
                        "undesirable overrides or dependency problems.\n\nContinue with this change?",
                        icon="warning", default="no", parent=dialog):
                    return
                reorder_warning_shown = True
            masters[idx], masters[new_idx] = masters[new_idx], masters[idx]
            from major_master_deploy import staged_masters
            paths = {name.casefold(): data_dir / name for name in masters if (data_dir / name).is_file()}
            paths.update(staged_masters(APP_DIR / "staging" / "major_masters", masters))
            positions = {name.casefold(): i for i, name in enumerate(masters)}
            for consumer, path in paths.items():
                for required in self._esm_master_names(path):
                    if required in positions and positions[required] > positions[consumer]:
                        required_name = next((item for item in masters if item.casefold() == required), required)
                        consumer_name = next((item for item in masters if item.casefold() == consumer), consumer)
                        masters[idx], masters[new_idx] = masters[new_idx], masters[idx]
                        messagebox.showwarning(
                            "Master dependency order required",
                            f"{consumer_name} depends on {required_name}. "
                            "A master must remain before every plugin that consumes it.", parent=dialog)
                        refresh_masters()
                        tree.selection_set(name)
                        return
            refresh_masters()
            tree.selection_set(name)
            tree.see(name)

        def add_master():
            name = messagebox.askstring("Add major master", "Master filename (must end in .esm):", parent=dialog)
            if name is None:
                return
            name = name.strip()
            if not name:
                return
            if Path(name).name != name or Path(name).suffix.casefold() != ".esm":
                messagebox.showerror("Invalid master filename", "Enter an ESM filename only, such as example_data.esm.", parent=dialog)
                return
            if any(existing.casefold() == name.casefold() for existing in masters):
                messagebox.showinfo("Already listed", f"{name} is already in the major master list.", parent=dialog)
                return
            masters.append(name)
            keywords[name] = [Path(name).stem.replace("_", " ").casefold()]
            refresh_masters()
            tree.selection_set(name)
            tree.see(name)

        def save_masters(close=True):
            try:
                raw = json.loads(MAJOR_MASTER_CONFIG.read_text(encoding="utf-8"))
                config = {name: keywords.get(name, raw.get(name, [])) for name in masters}
                MAJOR_MASTER_CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            except (OSError, ValueError) as exc:
                messagebox.showerror("Could not save master list", str(exc), parent=dialog)
                return False
            self.major_masters = list(masters)
            self.gui_state["selected_major_masters"] = [n for n in masters
                                                        if n.casefold() in self.selected_major_masters]
            write_gui_state(self.gui_state)
            # Put active community masters immediately after the three fixed game masters,
            # in the chosen priority order, ahead of their dependent plugins.
            major_keys = {n.casefold() for n in masters}
            other = [n for n in self.order if n.casefold() not in major_keys and n.casefold() not in {c.casefold() for c in CORE}]
            installed_active = [name for name in masters if (data_dir / name).is_file()
                               and name.casefold() in self.selected_major_masters]
            self.order = list(CORE) + installed_active + other
            self.load_order_dirty = True
            if not self.save():
                return False
            saved_state[0] = master_state()
            if close and not getattr(self, "_initializing_manager", False):
                dialog.destroy()
            else:
                update_save_label()
            return True

        def saved_action(action):
            if save_masters(close=False):
                action()

        tree.bind("<<TreeviewSelect>>", update_master_buttons)
        tree.bind("<Button-1>", click_master)
        up.configure(command=lambda: move_master(-1))
        down.configure(command=lambda: move_master(1))
        add_button.configure(command=add_master)
        self.stage_master_button = ttk.Button(
            footer_top, text="Reopen options for selected…",
            command=lambda: saved_action(lambda: self.stage_major_masters(option_masters=list(tree.selection()))))
        self.stage_master_button.pack(side="left")
        self.stage_more_master_button = ttk.Button(
            footer_top, text="Find and stage archives…",
            command=lambda: saved_action(lambda: self.stage_major_masters(force_scan=True)))
        self.stage_more_master_button.pack(side="left", padx=(6, 0))
        self.deploy_master_button = ttk.Button(
            footer_top, text="Deploy Major Masters", command=lambda: saved_action(self.deploy_major_masters))
        self.deploy_master_button.pack(side="left", padx=(6, 0))
        self.remove_master_button = ttk.Button(footer_bottom, text="Remove deployed masters", command=self.remove_deployed_major_masters)
        self.remove_master_button.pack(side="left", padx=(6, 0))
        self.unstage_master_button = ttk.Button(footer_bottom, text="Unstage selected", command=unstage_selected)
        self.unstage_master_button.pack(side="left", padx=(6, 0))
        def finish_masters():
            try:
                if not save_masters():
                    return
            except Exception as exc:
                messagebox.showerror(
                    "Could not finish Major Masters",
                    f"MWManager could not save the current master state.\n\n{exc}", parent=dialog)
                return
            if waiting_option_count():
                self._resume_waiting_options_after_startup()
            else:
                self.status.set("Major masters are current; no selected options are waiting on other mods.")

        save_button["button"] = ttk.Button(footer_bottom, text="Done", command=finish_masters, width=22,
                                           style="MasterSave.TButton")
        save_button["button"].pack(side="right")
        update_save_label()
        def close_masters():
            if getattr(self, "_active_job", None):
                return
            if getattr(self, "_initializing_manager", False):
                self._initializing_manager = False
                self._complete_init()
                self.status.set("Setup complete. Optional major masters were skipped; add them later from Major masters.")
                self._setup_flow_active = False
            dialog.destroy()
        ttk.Button(footer_bottom, text="Skip for now" if getattr(self, "_initializing_manager", False) else "Close",
                   command=close_masters, width=14).pack(side="right", padx=(0, 8))
        def pause_masters():
            if getattr(self, "_active_job", None):
                return
            if getattr(self, "_initializing_manager", False):
                self._initializing_manager = False
                self.status.set("Setup paused at optional major masters. Use File > Resume setup to continue.")
            dialog.destroy()
        dialog.protocol("WM_DELETE_WINDOW", pause_masters)
        def forget_dialog(event):
            if event.widget == dialog and getattr(self, "_major_master_dialog", None) == dialog:
                for name in ("stage_master_button", "stage_more_master_button", "deploy_master_button",
                             "remove_master_button", "unstage_master_button", "_major_master_refresh",
                             "_major_master_dialog"):
                    if hasattr(self, name):
                        delattr(self, name)
        dialog.bind("<Destroy>", forget_dialog, add="+")
        self._major_master_refresh = refresh_masters
        refresh_masters(background=True)

    def _master_scan_cached(self, kind, compute):
        """Walking staging and Data Files takes seconds once the big masters are staged, and the master list asks
        on every click. Reuse the last answer until a package folder, the staging folder or Data Files changes
        (their modification times move when a master is extracted, tidied to the package root, deployed or removed)."""
        staging = APP_DIR / "staging" / "major_masters"
        stamps = [tuple(self.major_masters), str(staging.resolve()), str(Path(self.settings["game_dir"]).resolve())]
        for folder in (staging, Path(self.settings["game_dir"]) / "Data Files"):
            try:
                stamps.append(folder.stat().st_mtime_ns)
            except OSError:
                stamps.append(None)
        from major_master_deploy import ASSET_FOLDERS, SKIP_FOLDERS
        for root in (staging, Path(self.settings["game_dir"]) / "Data Files"):
            for base, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if d.casefold() not in SKIP_FOLDERS | ASSET_FOLDERS
                                 and not d.startswith("."))
                for name in sorted(files):
                    if Path(name).suffix.casefold() not in {".esm", ".esp"}:
                        continue
                    path = Path(base) / name
                    try:
                        stat = path.stat()
                        stamps.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
                    except OSError:
                        return set(compute())  # Unreadable files never produce a trusted cache hit.
        cache = self.__dict__.setdefault("_master_scan_cache", {})
        if cache.get(kind, (None,))[0] != stamps:
            cache[kind] = (stamps, compute())
        return set(cache[kind][1])

    def _clear_major_master_scan_cache(self):
        self.__dict__.pop("_master_scan_cache", None)

    def _staged_major_master_names(self):
        """Return configured major-master ESMs present in current staged packages."""
        return self._master_scan_cached("staged", self._scan_staged_major_master_names)

    def _scan_staged_major_master_names(self):
        staging = APP_DIR / "staging" / "major_masters"
        if not staging.is_dir():
            return set()
        from major_master_deploy import ASSET_FOLDERS
        wanted = {name.casefold() for name in self.major_masters}
        found = set()
        for root, dirs, files in os.walk(staging):
            dirs[:] = [name for name in dirs if name.casefold() not in {"off", "bugreports", "extras"} | ASSET_FOLDERS
                       and not name.startswith(".")]
            found.update(filename.casefold() for filename in files if filename.casefold() in wanted)
        return found

    def _master_stage_record(self):
        return APP_DIR / "outputs" / "mwmanager_major_stage_state.json"

    def _set_master_stage_ready(self, names, ready):
        path = self._master_stage_record()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        for name in names:
            state[name.casefold()] = bool(ready)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, (json.dumps(state, indent=2) + "\n").encode("utf-8"))

    def _unfinished_master_stages(self, names, staged=None):
        from major_master_deploy import staged_masters
        from core_addons import package_steps, PACKAGE_STEPS
        staging = APP_DIR / "staging" / "major_masters"
        if staged is None:
            staged = staged_masters(staging, list(names))
        try:
            state = json.loads(self._master_stage_record().read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        unfinished = []
        for name in names:
            source = staged.get(name.casefold())
            package = next((p for p in source.parents if p.parent == staging), None) if source else None
            steps = package_steps(package) if package else None
            if steps is not None:
                if any(not steps[step]["completed_at"] for step in PACKAGE_STEPS):
                    unfinished.append(name)
            elif state.get(name.casefold()) is not True:
                unfinished.append(name)
        return unfinished

    @staticmethod
    def _esm_master_names(path):
        """Read TES3 MAST names from an ESM header."""
        import struct
        try:
            with path.open("rb") as stream:
                header = stream.read(16)
                if len(header) != 16 or header[:4] != b"TES3":
                    return set()
                size = struct.unpack_from("<I", header, 4)[0]
                if size > 16 * 1024 * 1024:
                    return set()
                body = stream.read(size)
            if len(body) != size:
                return set()
            masters, offset = set(), 0
            while offset + 8 <= len(body):
                tag = body[offset:offset + 4]
                length = struct.unpack_from("<I", body, offset + 4)[0]
                offset += 8
                if offset + length > len(body):
                    return set()
                if tag == b"MAST":
                    masters.add(body[offset:offset + length].split(b"\0", 1)[0].decode("cp1252", "replace").casefold())
                offset += length
            return masters
        except (OSError, ValueError):
            return set()

    def _required_major_master_names(self):
        """Return configured masters referenced by another installed or staged major master."""
        return self._master_scan_cached("required", self._scan_required_major_master_names)

    def _scan_required_major_master_names(self):
        from major_master_deploy import ASSET_FOLDERS
        configured = {name.casefold() for name in self.major_masters}
        roots = [Path(self.settings["game_dir"]) / "Data Files", APP_DIR / "staging" / "major_masters"]
        required = set()
        for root in roots:
            if not root.is_dir():
                continue
            for base, dirs, files in os.walk(root):
                dirs[:] = [name for name in dirs if name.casefold() not in {"off", "bugreports", "extras"} | ASSET_FOLDERS
                           and not name.startswith(".")]
                for filename in files:
                    if Path(filename).suffix.casefold() != ".esm" or filename.casefold() not in configured:
                        continue
                    required.update(self._esm_master_names(Path(base) / filename) & configured)
        return required

    def deploy_major_masters(self, *, _preflight_done=False, _accept_assets=None):
        """Confirm deployment policy, then hardlink staged major masters into Data Files."""
        import mwmanager_dialogs as messagebox
        from major_master_deploy import staged_masters

        if getattr(self, "_active_job", None):
            return
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        staging = APP_DIR / "staging" / "major_masters"
        available = staged_masters(staging, self.major_masters)
        ready = [name for name in self.major_masters
                 if name.casefold() in available and not (data_dir / name).is_file()
                 and name.casefold() in self.selected_major_masters]
        if not ready:
            messagebox.showinfo("No masters selected", "Select at least one processed, staged master in the Enable column.", parent=self)
            return
        unfinished = self._unfinished_master_stages(ready)
        if unfinished:
            messagebox.showwarning(
                "Finish staging first", "These masters have not finished Tidy and option processing:\n\n" +
                "\n".join(unfinished) + "\n\nChoose Reopen staged options to finish processing before deployment.",
                parent=self)
            return
        # Ask before hashing or network work. Keep this policy local to this
        # invocation so a cancelled preflight cannot leak it into a later click.
        # A master always deploys whole (plugins, archives and loose assets); only ask when nothing confirmed it yet.
        accept_assets = _accept_assets
        if accept_assets is None:
            if not messagebox.askyesno(
                    "Deploy selected major masters?",
                    "Selected masters, in load order:\n\n" + "\n".join(ready) +
                    "\n\nReplaced loose assets are preserved under the staged package's Off folder.",
                    parent=self):
                return
            accept_assets = True

        if not _preflight_done:
            cached_preflight = getattr(self, "_preflight_signature", None)
            if cached_preflight is not None and cached_preflight == preflight_signature(self.settings["game_dir"]):
                self.status.set("Reusing this session's successful preflight; prerequisite files are unchanged.")
                self.deploy_major_masters(_preflight_done=True, _accept_assets=accept_assets)
                return
            if hasattr(self, "deploy_master_button"):
                self.deploy_master_button.state(["disabled"])

            def preflight_worker(progress):
                from preinstall import run_preinstall
                return run_preinstall(Path(self.settings["game_dir"]), APP_DIR, progress,
                                      archive_dir=self.settings.get("archive_downloads_dir"))

            def preflight_completed(result, error):
                if hasattr(self, "deploy_master_button"):
                    self.deploy_master_button.state(["!disabled"])
                if error:
                    self.status.set("Pre-deploy checks stopped with an error.")
                    messagebox.showerror("Pre-deploy checks failed", str(error), parent=self)
                    return
                if result == "restart":
                    self._restart_manager()
                    return
                if result == "cancelled":
                    self._cancel_until_restart()
                    return
                if not result:
                    self.status.set("Pre-deploy checks were not accepted; no major masters were deployed.")
                    messagebox.showinfo("Deployment paused by preflight",
                                        "No files were deployed. Resolve any missing prerequisites or review the "
                                        "declined warnings, then retry deployment.", parent=self)
                    return
                self._remember_preflight(preflight_signature(self.settings["game_dir"]))
                self.deploy_major_masters(_preflight_done=True, _accept_assets=accept_assets)

            self._run_progress_job("Pre-deploy checks", "Checking game files and required utilities…",
                                   preflight_worker, preflight_completed)
            return

        if hasattr(self, "deploy_master_button"):
            self.deploy_master_button.state(["disabled"])

        def worker(progress):
            checkpoint(progress)
            from plugin_install import check_deploy_path_lengths
            checked_packages = set()
            for plugin in ready:
                esm = available[plugin.casefold()]
                package = next((parent for parent in esm.parents if parent.parent == staging), esm.parent)
                key = str(package.resolve()).casefold()
                if key not in checked_packages:
                    check_deploy_path_lengths(package, data_dir, plugin)
                    checked_packages.add(key)
            from major_master_deploy import deploy
            # Keep catalog priorities, but deploy only the explicit selection.
            return deploy(staging, data_dir, APP_DIR, self.major_masters, selected=ready,
                          include_assets=bool(accept_assets), progress=progress)

        def completed(result, error):
            if hasattr(self, "deploy_master_button"):
                self.deploy_master_button.state(["!disabled"])
            if error:
                self._setup_fail("major_masters_deployed", error)
                self.status.set("Major-master deployment stopped with an error.")
                messagebox.showerror("Could not deploy major masters", str(error), parent=self)
                return
            self._setup_complete("major_masters_deployed")
            if getattr(self, "_initializing_manager", False):
                self._initializing_manager = False
                self._complete_init()                # setup's major-master step ends with the deployment
            self.active_names.update(name.casefold() for name in result["ready"])
            self.selected_major_masters.update(name.casefold() for name in result["ready"])
            firemoth = data_dir / "Off" / "Official Add-Ons" / "Siege at Firemoth.esp"
            if firemoth.is_file() and not (data_dir / firemoth.name).exists():      # set aside by the deployment
                disabled = read_disabled_plugins()
                disabled[firemoth.name] = FIREMOTH_DISABLED_REASON
                write_disabled_plugins(disabled)
                self.active_names.discard(firemoth.name.casefold())
            major_keys = {name.casefold() for name in self.major_masters}
            # plugins deployed with the masters were chosen in their installer (TR_Factions.esp): keep them
            # active, or save() below moves them straight back out of Data Files
            companions = [Path(rel).name for rel in result.get("linked", [])
                          if len(Path(rel).parts) == 1 and Path(rel).suffix.lower() in (".esp", ".esm")
                          and Path(rel).name.casefold() not in major_keys]
            self.active_names.update(name.casefold() for name in companions)
            known = {name.casefold() for name in self.order}
            self.order += [name for name in companions if name.casefold() not in known]
            other = [name for name in self.order
                     if name.casefold() not in major_keys and name.casefold() not in {c.casefold() for c in CORE}]
            installed_active = [name for name in self.major_masters
                                if (data_dir / name).is_file() and name.casefold() in self.selected_major_masters]
            self.order = list(CORE) + installed_active + other
            self.gui_state["selected_major_masters"] = [n for n in self.major_masters
                                                        if n.casefold() in self.selected_major_masters]
            write_gui_state(self.gui_state)
            self.load_order_dirty = True
            if not self.save():
                self.status.set("Major masters are deployed, but the load order was not saved. Retry Save load order.")
                return
            if hasattr(self, "_major_master_refresh"):
                self._major_master_refresh()
            self.refresh()
            self.status.set(f"Deployed {len(result['ready'])} major master(s); linked {len(result['linked']):,} files.")
            self._setup_flow_active = False
            proper = {name.casefold(): name for name in self.major_masters}
            detail = (["Masters:"] + [f"  • {proper.get(n.casefold(), n)}" for n in result["ready"]]
                      + ["", f"Hardlinked files: {len(result['linked']):,}",
                         f"Existing files preserved under Off: {len(result['backed_up']):,}",
                         f"Identical files already present: {len(result['skipped_identical']):,}"])
            if result["archives"]:
                detail += ["", "Registered BSA archives:"] + [f"  • {name}" for name in result["archives"]]
            masters_window = getattr(self, "_major_master_dialog", None)
            if masters_window is not None and masters_window.winfo_exists():
                masters_window.destroy()       # its job is done; on top, it hid this summary behind it
            messagebox.showinfo("Major masters deployed", "\n".join(detail), parent=self)
            view = getattr(self, "_job_view", None)
            if view is not None and getattr(view, "setup", False) and view.window.winfo_exists():
                view.window.withdraw()          # deployment is done: the conflict review and --full follow alone
            if result.get("conflicts"):
                self._resolve_major_master_conflicts(result["conflicts"])
            def offer_full():
                if messagebox.askyesno("Run --full?", "The load order changed. Run --full against the new load "
                                       "order now? It rebuilds the patches and may take a while.", parent=self):
                    self.generate_patches(mode="full", force=True)

            def next_step():                    # options waiting on another mod first, then --full
                self._resolve_waiting_options(offer_full)

            if view is not None and getattr(view, "setup", False):
                view.then = next_step           # offered once this job has fully settled
            else:
                next_step()

        self._setup_begin("major_masters_deployed")
        self._run_progress_job("Deploy Major Masters", "Hardlinking selected masters and their assets…", worker, completed)

    def install_waiting_option_mods(self):
        """Tools menu: the queue of mods that chosen major-master options wait on (outputs\\
        mwmanager_pending_options.json), started by hand - it otherwise runs only right after a major-master
        deployment, so a batch that stopped could not be picked up again."""
        self.status.set("Looking for the mods your chosen options need…")
        self._resolve_waiting_options(lambda: None)

    def _resolve_waiting_options(self, after):
        """The masters are deployed with every option that needs nothing more. Options chosen that wait on
        another mod: find those mods in the downloads, install them (Deploy Plugins; the mods they in turn need
        are queued there), then put the waiting options in and deploy the masters again. When nothing waits,
        or nothing new can be found, `after` runs (the offer to run --full)."""
        downloads = self._archive_downloads_folder()
        tried = self.__dict__.setdefault("_waiting_option_archives_tried", set())

        def worker(progress):
            return waiting_option_archives(APP_DIR / "staging" / "major_masters", APP_DIR / "outputs", downloads,
                                           APP_DIR, self.major_masters, tried, progress)

        def completed(result, error):
            if error:
                self.status.set(f"Options waiting on other mods were not followed up: {error}")
                after()
                return
            items, masters = result
            if not items:
                if masters:
                    self.status.set(f"{', '.join(masters)}: some chosen options still wait on mods that are not in "
                                    "your downloads (a Nexus search was opened for each).")
                after()
                return

            def put_options_in():
                self.stage_major_masters(option_masters=masters, reuse_answers=True)

            names = [Path(item["archive"]).name for item in items]
            self._install_plugin_archives(
                items, then=put_options_in, title="Install mods your options need",
                intro=(f"Options you chose for {', '.join(masters)} need other mods, so these {len(names)} are installed "
                       f"now: {', '.join(names[:6])}{' …' if len(names) > 6 else ''}. Their own options are asked "
                       "first, then they are staged and deployed; after that the waiting options go into the masters."))

        if downloads is None:
            after()
            return
        self._run_progress_job("Options waiting on other mods", "Looking for the mods your chosen options need…",
                               worker, completed)

    def remove_deployed_major_masters(self):
        """Remove only masters deployed by MWManager and restore replaced files."""
        import mwmanager_dialogs as messagebox
        from major_master_deploy import tracked_deployment_masters

        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        staging = APP_DIR / "staging" / "major_masters"
        tracked = [name for name in tracked_deployment_masters(APP_DIR) if (data_dir / name).is_file()]
        if not tracked:
            messagebox.showinfo("No deployed masters to remove",
                                "There are no MWManager-tracked major masters deployed in Data Files.", parent=self)
            return
        if not messagebox.askyesno(
                "Remove deployed masters?",
                f"Remove {len(tracked)} MWManager-deployed master(s)?\n\n"
                "MWManager will remove only files it hardlinked, restore files it preserved during deployment, "
                "unregister archives it added to Morrowind.ini, and clear these masters from its catalog. "
                "Staged masters and their BSAs will remain available for redeployment.", parent=self):
            return
        self._remove_masters_job(staging, data_dir)

    def _remove_masters_job(self, staging, data_dir, remove_edited=False):
        import mwmanager_dialogs as messagebox
        self.remove_master_button.state(["disabled"])

        def worker(progress):
            from major_master_deploy import remove_deployed
            return remove_deployed(staging, data_dir, APP_DIR, progress=progress, remove_edited=remove_edited)

        def completed(result, error):
            if error:
                if hasattr(self, "_major_master_refresh"):
                    self._major_master_refresh()
                self.status.set("Major-master removal stopped with an error.")
                messagebox.showerror("Could not remove deployed masters", str(error), parent=self)
                return
            if hasattr(self, "_major_master_refresh"):
                self._major_master_refresh()
            self.refresh()
            self.status.set(f"Removed {len(result['masters'])} deployed major master(s); restored {result['restored']:,} files.")
            detail = (
                f"Masters: {', '.join(result['masters'])}\n"
                f"Deployed hardlinks removed: {result['removed']:,}\n"
                f"Preserved files restored: {result['restored']:,}\n"
                f"Archive registrations removed: {result['registrations']:,}\n\n"
                "Staged masters and BSAs remain ready for deployment.")
            edited = result.get("edited") or []
            if edited:
                shown = "\n".join(edited[:10]) + (f"\n… and {len(edited) - 10:,} more" if len(edited) > 10 else "")
                if messagebox.askyesno(
                        "Save Altered Files?",
                        detail + f"\n\n{len(edited):,} deployed file(s) no longer match the originals. It is "
                        "assumed you edited them after deployment, perhaps long ago:\n\n" + shown +
                        "\n\nSave copies of them? They go to MWManager's altered_files folder, which removal "
                        "and rollback never touch, so your edits are kept.", parent=self):
                    from major_master_deploy import save_altered
                    try:
                        folder = save_altered(edited, data_dir.parent, APP_DIR)
                        self.status.set(f"Saved {len(edited):,} altered file(s) to {folder}")
                    except OSError as exc:
                        messagebox.showerror("Could not save altered files", str(exc), parent=self)
                        return
                if messagebox.askyesno(
                        "Delete edited files?",
                        f"Delete the {len(edited):,} altered file(s) from the game too? "
                        "No leaves them in place.", parent=self):
                    self._remove_masters_job(staging, data_dir, remove_edited=True)
                    return
            if result.get("pending"):
                messagebox.showwarning(
                    "Some files need review",
                    detail + f"\n\n{result['pending']:,} files were changed after deployment. MWManager left them and their backups untouched; the Remove deployed masters button will stay available for another attempt.", parent=self)
            else:
                messagebox.showinfo("Major masters removed", detail, parent=self)

        self._run_progress_job("Remove Major Masters", "Removing tracked major-master files…", worker, completed)

    def _resolve_major_master_conflicts(self, conflicts):
        """Summarize DB conflicts and offer optional, severity-filtered batches."""
        from tkinter import ttk
        import mwmanager_dialogs as messagebox
        if not conflicts:
            return

        severity_order = ("Extreme", "Substantial", "Major", "Minor")
        severity_tags = {
            "Extreme": {"CELL", "LAND", "PGRD", "WRLD"},
            "Substantial": {"NPC_", "CREA", "DIAL", "INFO", "LEVC", "LEVI", "CONT",
                            "DOOR", "STAT", "ACTI", "MISC", "WEAP", "ARMO", "CLOT"},
            "Major": {"RACE", "CLAS", "FACT", "SKIL", "SPEL", "ENCH", "ALCH", "BSGN"},
        }
        def grade(tag):
            return next((level for level, tags in severity_tags.items() if str(tag).upper() in tags), "Minor")

        grouped = {level: [] for level in severity_order}
        for conflict in conflicts:
            grouped[grade(conflict[0])].append(conflict)
        counts = {level: len(grouped[level]) for level in severity_order}
        examples = []
        for level in severity_order:
            examples.extend((level, f"{tag} ({editor_id or 'no editor ID'})")
                            for tag, editor_id, _sources, _versions in grouped[level][:3])
        by_degree = "\n".join(f"  {level}: {counts[level]:,}" for level in severity_order if counts[level])
        if not messagebox.askyesno(
                "Major-master conflicts",
                "Major masters are expected to conflict with each other. The agreed community load order is the "
                f"resolution order.\n\nThere are {len(conflicts):,} conflicts:\n{by_degree}\n\n"
                "It is strongly recommended to let the load order resolve them. You may edit the resolution if you "
                "choose.\n\nReview conflicts?", default="no", parent=self):
            return

        dialog = tk.Toplevel(self)
        dialog.title("Major-master record conflicts")
        dialog.transient(self)
        center_window(dialog, self, 900, 620)
        dialog.minsize(760, 500)
        outer = ttk.Frame(dialog, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"{len(conflicts):,} conflicting ESM records detected",
                  font=FONTS["h2"]).pack(anchor="w")
        ttk.Label(outer, text="Major masters follow the configured load order by default. Source ESM files stay unchanged; optional overrides are saved for the MWManager ESM patch.",
                  wraplength=860).pack(anchor="w", pady=(3, 8))
        summary = ttk.Frame(outer)
        summary.pack(fill="x", pady=(0, 6))
        for level in severity_order:
            ttk.Label(summary, text=f"{level}: {counts[level]:,}").pack(side="left", padx=(0, 18))
        guide = ttk.LabelFrame(outer, text="Severity guide", padding=(8, 4))
        guide.pack(fill="x", pady=(0, 8))
        ttk.Label(guide, text="Extreme: cells, land, path grids, world data   •   Substantial: NPCs, creatures, dialogue, placed objects\nMajor: gameplay definitions   •   Minor: other record types. These are screening estimates; inspect examples before saving overrides.",
                  wraplength=850, justify="left").pack(anchor="w")
        example_text = "Worst examples by severity: " + "; ".join(f"{level}: {name}" for level, name in examples[:12])
        ttk.Label(outer, text=example_text, wraplength=860, justify="left").pack(anchor="w", pady=(0, 8))

        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        ttk.Label(controls, text="Review severity:").pack(side="left")
        available = [level for level in severity_order if counts[level]]
        level_var = tk.StringVar(value=available[0] if available else "Minor")
        selector = ttk.Combobox(controls, textvariable=level_var, values=available, state="readonly", width=18)
        selector.pack(side="left", padx=(6, 16))
        page_label = ttk.Label(controls, text="")
        page_label.pack(side="left")

        canvas = tk.Canvas(outer, highlightthickness=0, height=310)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, pady=(6, 0))
        scrollbar.pack(side="right", fill="y", pady=(6, 0))

        page_size = 40
        page = 0
        row_vars = []
        choices_path = APP_DIR / "mwmanager_major_master_conflicts.json"
        saved = {}
        try:
            prior = json.loads(choices_path.read_text(encoding="utf-8"))
            for item in prior.get("resolutions", []):
                saved[(item.get("record_tag"), item.get("editor_id"))] = item.get("winner")
        except (OSError, ValueError, TypeError):
            pass

        def render(_event=None):
            nonlocal page, row_vars
            for child in content.winfo_children():
                child.destroy()
            row_vars = []
            records = grouped.get(level_var.get(), [])
            pages = max(1, (len(records) + page_size - 1) // page_size)
            page = min(max(page, 0), pages - 1)
            page_label.configure(text=f"{counts.get(level_var.get(), 0):,} records • batch {page + 1} of {pages}")
            data_dir = Path(self.settings["game_dir"]) / "Data Files"
            priority = {name.casefold(): i for i, name in enumerate(self.major_masters)}
            content.columnconfigure(0, weight=1)
            for row, (tag, editor_id, sources_text, versions) in enumerate(
                    records[page * page_size:(page + 1) * page_size]):
                sources = [name for name in set(str(sources_text).split(",")) if (data_dir / name).is_file()]
                sources.sort(key=lambda n: priority.get(n.casefold(), -1))
                if len(sources) < 2:
                    continue
                key = (tag, editor_id)
                default = saved.get(key, sources[-1])
                if default not in sources:
                    default = sources[-1]
                variable = tk.StringVar(value=default)
                ttk.Label(content, text=f"{tag} ({editor_id or 'no editor ID'}) — {versions} versions",
                          width=58).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
                chooser = ttk.Combobox(content, textvariable=variable, values=sources, state="readonly", width=38)
                chooser.grid(row=row, column=1, sticky="ew", pady=2)
                row_vars.append({"record_tag": tag, "editor_id": editor_id, "winner": variable})
            content.columnconfigure(1, weight=1)
            canvas.yview_moveto(0)

        selector.bind("<<ComboboxSelected>>", lambda _e: (setattr_page_zero(), render()))
        def setattr_page_zero():
            nonlocal page
            page = 0

        def save_batch():
            for item in row_vars:
                saved[(item["record_tag"], item["editor_id"])] = item["winner"].get()
            records = grouped.get(level_var.get(), [])
            pages = max(1, (len(records) + page_size - 1) // page_size)
            if page + 1 < pages:
                nonlocal_page_next()
                render()
                return
            data = {"version": 1, "resolutions": [
                {"record_tag": tag, "editor_id": editor_id, "winner": winner}
                for (tag, editor_id), winner in saved.items()]}
            try:
                choices_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Could not save conflict choices", str(exc), parent=dialog)
                return
            dialog.destroy()
            messagebox.showinfo("Conflict choices saved",
                                "Reviewed overrides are queued for the next MWManager ESM patch rebuild. Unreviewed records continue to follow load order.", parent=self)

        def nonlocal_page_next():
            nonlocal page
            page += 1

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Button(footer, text="Use this order and close", command=dialog.destroy).pack(side="right")
        ttk.Button(footer, text="Save and next batch", command=save_batch).pack(side="right", padx=(0, 8))
        render()
        dialog.wait_window()        # the caller's next prompt (run --full) waits until the review is closed

    def _run_progress_job(self, title, initial_text, worker, on_complete, description_override=None):
        """Run slow archive work away from Tk and keep the user informed while it runs."""
        running = getattr(self, "_active_job", None)
        if running:
            messagebox.showinfo("MWManager is busy",
                                f"\u201c{running}\u201d is still running. Start this again when it finishes.",
                                parent=self)
            return
        self._active_job = title
        in_setup = (self.settings.get("first_run", 0) == 1 or
                    self.settings.get("init_complete", 0) != 1 or
                    bool(getattr(self, "_setup_flow_active", False)))
        if in_setup:
            self._setup_flow_active = True
        view = getattr(self, "_setup_view", None) if in_setup else None
        if view is None or not view.window.winfo_exists():
            view = JobView(self, setup=in_setup)
            if in_setup:
                self._setup_view = view
        self._job_view = view
        view.status_log = APP_DIR / "outputs" / "mwmanager_status.log"
        dialog = view.window
        if title == "Deploy Major Masters":
            description = ("MWManager will hardlink the selected staged files into Data Files, register any BSA archives, "
                           "and update its plugin database. Existing conflicting files are preserved in the staged Off folder.")
        elif title == "Remove Major Masters":
            description = ("MWManager will remove only its tracked hardlinks, restore replaced files, unregister archives it added, "
                           "and clear the deployed masters from its catalog. Staged masters and BSAs remain available.")
        elif title == "Generate Patches":
            description = ("MWManager will check the active plugins, sort the load order, and rebuild its repair patches. "
                           "If new cell conflicts need decisions, an interactive review opens before the final rebuild.")
        elif title == "Accept staged files as ready":
            description = ("MWManager will deploy the selected staging folders directly without running Tidy, the mock FOMOD, "
                           "or BSA packing again. It will then run --all and open conflict review when needed.")
        elif title == "Deploy Plugins":
            description = ("Each archive is extracted to its own staging folder, tidied, and passed through its FOMOD or mock BAIN options. "
                           "Before each 15-mod deployment batch, MWManager opens staging for review and waits for approval. "
                           "After deployment it runs --all and opens conflict review when new cell choices are needed.")
        elif title == "Scan plugin archives":
            description = "MWManager is inspecting archive contents to identify plugins, MWSE files, FOMOD options, asset counts, and download dates."
        else:
            description = {
                "First-run preflight": "Check the game and prerequisites. Only setup and configuration choices need your input.",
                "Initial setup": "Sort and tidy the shipped files. Safe fixes apply automatically; output is shown below.",
                "Catalog": "Build the plugin and archive catalog. Completed setup checkpoints will be kept.",
                "Pre-deploy checks": "Recheck prerequisites before changing the live game.",
                "Scanning major-master downloads": "Inspect downloaded archives and check versions before choosing what to stage.",
            }.get(title, "Process the staged files. Tidy applies automatically; option choices remain interactive.")
        if description_override:
            description = description_override
        control = JobControl()
        self._job_control = control
        view.begin(title, description, control)
        view.refresh_phases()
        append_status = view.append
        progress = view.progress
        append_status(initial_text)
        self._set_job_controls(True)

        events = queue.Queue()

        def report(message):
            events.put(("progress", message))

        def ask_yes_no(question_title, question_text):
            control.check()
            response = {}
            answered = threading.Event()
            events.put(("question", question_title, question_text, response, answered))
            while not answered.wait(0.2):
                control.check()
            control.check()
            return bool(response.get("answer"))

        def ask_choice(question_title, question_text, choices, cancel_value=None):
            control.check()
            response = {"choice": cancel_value}
            answered = threading.Event()
            events.put(("choice", question_title, question_text, choices, response, answered))
            while not answered.wait(0.2):
                control.check()
            control.check()
            return response.get("choice")

        def launch_vortex():
            response = {"opened": False}
            answered = threading.Event()
            events.put(("launch_vortex", response, answered))
            while not answered.wait(0.2):
                control.check()
            control.check()
            return bool(response.get("opened"))

        def wait_for_vortex_purge():
            response = {"choice": "cancel"}
            answered = threading.Event()
            events.put(("wait_vortex_purge", response, answered))
            while not answered.wait(0.2):
                control.check()
            control.check()
            return response.get("choice")

        def run_program_with_reference(path, reference_title, reference_text):
            response = {"returncode": None, "error": None}
            answered = threading.Event()
            events.put(("run_program", str(path), reference_title, reference_text, response, answered))
            while not answered.wait(0.2):
                control.check()
            control.check()
            return response["returncode"], response["error"]

        report.ask_yes_no = ask_yes_no
        report.ask_choice = ask_choice
        report.launch_vortex = launch_vortex
        report.wait_for_vortex_purge = wait_for_vortex_purge
        report.run_program_with_reference = run_program_with_reference
        report.check_cancelled = control.check

        def run():
            try:
                control.check()
                from workflow_safety import mutation_lock
                with mutation_lock(APP_DIR, self.settings["game_dir"]):
                    from workflow_safety import DurableTransaction
                    DurableTransaction.recover(
                        APP_DIR / "transactions" / "plugin-deploy",
                        [APP_DIR, Path(self.settings["game_dir"]), APP_DIR / "staging" / "plugins"])
                    from major_master_deploy import recovery_pending, recover_deployment
                    if recovery_pending(APP_DIR):
                        report("Recovering interrupted deployment changes before starting this job.")
                        recover_deployment(APP_DIR, self.settings["game_dir"])
                    result = worker(report)
                events.put(("done", result, None))
            except Exception as exc:
                events.put(("done", None, exc))

        threading.Thread(target=run, name="MWManager archive task", daemon=True).start()

        def poll():
            finished = None
            # Yield to Tk even when a subprocess emits thousands of lines.
            for _ in range(150):
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                if event[0] == "progress":
                    append_status(event[1])
                elif event[0] == "question":
                    _kind, question_title, question_text, response, answered = event
                    response["answer"] = messagebox.askyesno(question_title, question_text, parent=dialog)
                    answered.set()
                elif event[0] == "choice":
                    _kind, question_title, question_text, choices, response, answered = event
                    choice_dialog = tk.Toplevel(dialog)
                    choice_dialog.title(question_title)
                    choice_dialog.transient(dialog)
                    choice_dialog.resizable(True, True)
                    choice_dialog.minsize(540, 340)
                    body = ttk.Frame(choice_dialog, padding=12)
                    body.pack(fill="both", expand=True)
                    details = tk.Text(body, wrap="word", height=18, width=96, font=FONTS["mono"],
                                      relief="flat", borderwidth=0, padx=10, pady=8)
                    details.insert("1.0", question_text)
                    details.configure(state="disabled")
                    details.grid(row=0, column=0, sticky="nsew")
                    scrollbar = ttk.Scrollbar(body, orient="vertical", command=details.yview)
                    scrollbar.grid(row=0, column=1, sticky="ns")
                    details.configure(yscrollcommand=scrollbar.set)
                    body.columnconfigure(0, weight=1)
                    body.rowconfigure(0, weight=1)
                    row = ttk.Frame(choice_dialog, padding=(12, 0, 12, 12))
                    row.pack(fill="x")
                    cancel_choice = response.get("choice")      # ask_choice pre-fills its cancel value

                    def choose(value, response=response, choice_dialog=choice_dialog):
                        response["choice"] = value
                        choice_dialog.destroy()
                    stacked = len(choices) > 3 or any(len(label) > 32 for label, _value in choices)
                    for index, (label, value) in enumerate(choices):
                        button = ttk.Button(row, text=label, command=lambda value=value, choose=choose: choose(value))
                        if stacked:
                            button.grid(row=index, column=0, sticky="ew", pady=3)
                            row.columnconfigure(0, weight=1)
                        else:
                            button.pack(side="left", padx=(0, 6))
                    choice_dialog.protocol("WM_DELETE_WINDOW", lambda choose=choose, cancel=cancel_choice: choose(cancel))
                    center_window(choice_dialog, dialog, min(860, dialog.winfo_screenwidth() - 48),
                                  min(620 if stacked else 520, dialog.winfo_screenheight() - 80))
                    choice_dialog.grab_set()
                    dialog.wait_window(choice_dialog)
                    answered.set()
                elif event[0] == "launch_vortex":
                    _kind, response, answered = event
                    response["opened"] = self._launch_vortex()
                    answered.set()
                elif event[0] == "wait_vortex_purge":
                    _kind, response, answered = event
                    purge_dialog = tk.Toplevel(dialog)
                    purge_dialog.title("Complete Vortex purge")
                    purge_dialog.transient(self)
                    purge_dialog.resizable(False, False)
                    ttk.Label(
                        purge_dialog,
                        text=("In Vortex, select Morrowind and click Purge Mods. When Vortex finishes, "
                              "use the button below to restart MWManager and rerun preflight.\n\n"
                              "This window stays open while you work in Vortex."),
                        wraplength=430, justify="left", padding=14,
                    ).pack(fill="x")
                    actions = ttk.Frame(purge_dialog, padding=(10, 0, 10, 10))
                    actions.pack(fill="x")

                    def finish_purge(choice, response=response, purge_dialog=purge_dialog, answered=answered):
                        response["choice"] = choice
                        purge_dialog.destroy()
                        answered.set()

                    ttk.Button(actions, text="Purge complete — restart MWManager",
                               command=lambda finish=finish_purge: finish("restart")).pack(side="right")
                    ttk.Button(actions, text="Cancel", command=lambda finish=finish_purge: finish("cancel")).pack(
                        side="right", padx=(0, 8))
                    purge_dialog.protocol("WM_DELETE_WINDOW", lambda finish=finish_purge: finish("cancel"))
                    center_window(purge_dialog, self, max(460, purge_dialog.winfo_reqwidth()),
                                  purge_dialog.winfo_reqheight())
                elif event[0] == "run_program":
                    _kind, path, reference_title, reference_text, response, answered = event
                    reference = tk.Toplevel(dialog)
                    reference.title(reference_title)
                    reference.transient(self)
                    reference.resizable(True, True)
                    body = ttk.Frame(reference, padding=12)
                    body.pack(fill="both", expand=True)
                    ttk.Label(body, text="Keep this reference open while configuring the program.",
                              style="H3.TLabel", wraplength=560).pack(fill="x", pady=(0, 8))
                    reference_body = ttk.Frame(body)
                    reference_body.pack(fill="both", expand=True)
                    details = tk.Text(reference_body, wrap="word", width=76, height=24, font=FONTS["mono"],
                                      relief="flat", borderwidth=0, padx=10, pady=8)
                    details.insert("1.0", reference_text)
                    details.configure(state="disabled")
                    reference_scroll = ttk.Scrollbar(reference_body, command=details.yview)
                    details.configure(yscrollcommand=reference_scroll.set)
                    details.pack(side="left", fill="both", expand=True)
                    reference_scroll.pack(side="right", fill="y")
                    instruction = "MWManager continues automatically when the program closes."
                    ttk.Label(body, text=instruction, wraplength=560).pack(fill="x", pady=(8, 0))
                    reference.protocol("WM_DELETE_WINDOW", reference.iconify)
                    # Docked at the right edge, the program is moved to its left: side by side, never stacked.
                    ref_width = min(560, reference.winfo_screenwidth() // 2 - 24)
                    ref_height = min(720, reference.winfo_screenheight() - 80)
                    reference.geometry(f"{ref_width}x{ref_height}+"
                                       f"{reference.winfo_screenwidth() - ref_width - 16}+40")
                    reference.lift()
                    try:
                        if os.name == "nt":
                            # in its own folder, through the shell (see _ShellProgram): the Code Patch finds
                            # nothing and closes at once when it starts anywhere else
                            process = _ShellProgram(path)
                            if not process.waitable:
                                process = None
                        else:
                            process = subprocess.Popen([path], cwd=str(Path(path).parent), close_fds=True)
                        append_status(f"Opened {Path(path).name}; waiting for it to close…")
                        program_pid = getattr(process, "pid", None) if process is not None else None

                        def place_program(pid=program_pid, reference=reference, tries=[25]):
                            tries[0] -= 1
                            try:
                                placed = reference.winfo_exists() and _move_left_of(pid, reference)
                            except (OSError, AttributeError, tk.TclError):
                                return                      # best effort: an elevated program can refuse the move
                            if not placed and tries[0] > 0 and reference.winfo_exists():
                                dialog.after(400, place_program)

                        if program_pid and os.name == "nt":
                            dialog.after(400, place_program)
                    except OSError as exc:
                        response["error"] = str(exc)
                        reference.destroy()
                        answered.set()
                        continue

                    def program_finished(reference=reference, response=response, answered=answered):
                        response["returncode"] = 0
                        if reference.winfo_exists():
                            reference.destroy()
                        answered.set()

                    if process is None:
                        ttk.Button(body, text="Program closed — continue", command=program_finished).pack(
                            anchor="e", pady=(12, 0))
                        continue

                    def wait_for_program(process=process, reference=reference,
                                         response=response, answered=answered):
                        code = process.poll()
                        if code is None:
                            dialog.after(250, wait_for_program)
                            return
                        response["returncode"] = code
                        if reference.winfo_exists():
                            reference.destroy()
                        answered.set()

                    dialog.after(250, wait_for_program)
                else:
                    finished = event[1:]
            if finished is None:
                # Keep the job window modal: a nested choice dialog's grab ends when it closes, so take it back.
                try:
                    if dialog.grab_current() is None and dialog.winfo_viewable():
                        dialog.grab_set()
                except tk.TclError:
                    pass
                dialog.after(100, poll)
                return
            progress.stop()
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            view.finish(finished[1])
            if not in_setup:
                dialog.destroy()
            self._active_job = None
            self._job_control = None
            self._enable_job_buttons()
            self._refresh_distant_land_notice()
            if isinstance(finished[1], JobCancelled):
                self._pause_setup(finished[1])
                if in_setup:
                    view.stage.set(str(finished[1]))
                return
            try:
                on_complete(*finished)
            except Exception as exc:
                self.status.set(f"{title} stopped with an error.")
                messagebox.showerror(f"{title} failed", str(exc), parent=self)
            if in_setup:
                view.refresh_phases()
                self.after(350, lambda: self._settle_setup_view(view))

        dialog.after(100, poll)

    def _settle_setup_view(self, view):
        if self._active_job or not view.window.winfo_exists():
            return
        view.refresh_phases()
        final = getattr(view, "final_stage", None)       # a job's own last word wins over the status bar
        view.final_stage = None
        view.stage.set(final or self.status.get())
        then, view.then = getattr(view, "then", None), None
        if then:                                         # the next job starts once this one has fully settled
            self.after_idle(then)
        if self.settings.get("init_complete", 0) != 1:
            view.resume.pack(side="right", padx=6)
        else:
            view.resume.pack_forget()

    def _enable_job_buttons(self):
        self._set_job_controls(False)
        self._update_action_buttons()

    def _set_job_controls(self, busy):
        """Restore actual previous states, not an unconditional enable-all."""
        if busy:
            saved = []
            view = getattr(self, "_job_view", None)

            def walk(parent):
                for child in parent.winfo_children():
                    if view is not None and child == view.window:
                        continue
                    if isinstance(child, (ttk.Button, ttk.Treeview, ttk.Entry, ttk.Combobox)):
                        saved.append((child, child.state()))
                        child.state(["disabled"])
                    walk(child)
            walk(self)
            self._busy_widget_states = saved
            self._busy_menu_states = []
            for menu in (self.file_menu, self.tools_menu):
                for index in range((menu.index("end") or 0) + 1):
                    if menu.type(index) == "command":
                        self._busy_menu_states.append(
                            (menu, index, menu.entrycget(index, "state")))
                        menu.entryconfigure(index, state="disabled")
            # An owned window stays above its owner on Windows, so the Major Masters window would cover the job
            # window; hide it while the job runs (its buttons are disabled anyway) and bring the job forward.
            masters = getattr(self, "_major_master_dialog", None)
            self._masters_hidden_for_job = bool(masters is not None and masters.winfo_exists()
                                                and masters.winfo_viewable())
            if self._masters_hidden_for_job:
                masters.withdraw()
            if view is not None and view.window.winfo_exists():
                view.window.deiconify()
                view.window.lift()
        else:
            masters = getattr(self, "_major_master_dialog", None)
            if getattr(self, "_masters_hidden_for_job", False) and masters is not None and masters.winfo_exists():
                masters.deiconify()
                masters.lift()
            self._masters_hidden_for_job = False
            for widget, previous in getattr(self, "_busy_widget_states", []):
                try:
                    widget.state(["!disabled"])
                    widget.state(previous)
                except tk.TclError:
                    pass
            for menu, index, state in getattr(self, "_busy_menu_states", []):
                menu.entryconfigure(index, state=state)
            self._busy_widget_states = []

    def _pause_setup(self, reason):
        step, _entry = setup_state.interrupted(self._setup_path(), self.settings["game_dir"])
        from major_master_deploy import recovery_pending
        if recovery_pending(APP_DIR):
            step = "major_masters_deployed"
        if step:
            self._setup_fail(step, reason)
        self._initializing_manager = False
        self.status.set(str(reason))

    def _on_close(self):
        running = getattr(self, "_active_job", None)
        if running and not messagebox.askyesno(
                "Quit while a job is running?",
                f"\u201c{running}\u201d is still running. Quitting now can leave it half-finished.\n\nQuit anyway?",
                icon="warning", default="no", parent=self):
            return
        if not self._resolve_pending_load_order("closing"):
            return
        _end_children()
        self.destroy()

    def _resolve_pending_load_order(self, action):
        if not getattr(self, "load_order_dirty", False) and not self.pending_order_rules:
            return True
        answer = messagebox.askyesnocancel(
            "Unsaved load-order changes",
            f"Save the pending load-order changes before {action}?\n\n"
            "Choose No to discard them" + (" and play with the saved load order." if action == "playing" else "."),
            parent=self,
        )
        if answer is None:
            return False
        if answer:
            return bool(self.save())
        self.order = read_order()
        self.active_names = {name.casefold() for name in self.order}
        self.pending_order_rules.clear()
        self.load_order_dirty = False
        self.refresh()
        return True

    def _archive_downloads_folder(self):
        """The Archive Downloads folder, or None. If the value held in memory is empty or gone, the saved one
        (mwmanager.ini) is read again before giving up."""
        held = str(self.settings.get("archive_downloads_dir") or "").strip()
        if held and Path(held).is_dir():
            return Path(held)
        saved = str(read_settings().get("archive_downloads_dir") or "").strip()
        if saved and Path(saved).is_dir():
            self.settings["archive_downloads_dir"] = saved
            return Path(saved)
        return None

    def _after_major_master_staging(self, initializing, prompt):
        """Staging is finished: record it and offer deployment. Initialization is complete once the masters are
        deployed, or once deployment is declined here - not before."""
        self._set_staging_run(None)
        self._setup_complete("major_masters_staged")
        from mwmanager_batch import BatchCoordinator
        from core_addons import unfinished_packages
        active_batch = BatchCoordinator.resume(APP_DIR)
        if active_batch is not None and not unfinished_packages(
                APP_DIR / "staging" / "major_masters"):
            active_batch.finish()
        if self._staged_major_master_names() and messagebox.askyesno("Deploy staged major masters?", prompt, parent=self):
            self.after(100, lambda: self.deploy_major_masters(_accept_assets=True))    # already confirmed
            return
        if initializing:
            self._initializing_manager = False
            self._complete_init()
            self.after(100, self.open_major_masters)

    def _resume_interrupted_major_masters(self, *, open_dialog=True) -> bool:
        """At start: if staging or deploying the major masters was interrupted, say so and pick it up."""
        step, _entry = setup_state.interrupted(self._setup_path(), self.settings["game_dir"])
        from major_master_deploy import recovery_pending
        if recovery_pending(APP_DIR):
            step = "major_masters_deployed"
        if step not in ("major_masters_staged", "major_masters_deployed"):
            return False
        if not self._setup_resume_check():
            return True
        if step == "major_masters_staged":
            from core_addons import unfinished_packages
            if unfinished_packages(APP_DIR / "staging" / "major_masters") or self._pending_stage_masters():
                self.after(300, self.stage_major_masters)   # resumes from each package's own step record
            else:
                # every package is finished and none is owed: only the deploy question was left unanswered.
                # Nothing is re-run; the option installers are not opened again.
                self.after(300, lambda: self._after_major_master_staging(
                    bool(getattr(self, "_initializing_manager", False)),
                    "Major-master staging had finished when MWManager was closed.\n\n"
                    "Deploy the staged masters to the game now?"))
        elif step == "major_masters_deployed":
            from major_master_deploy import (
                recover_deployment, recovery_pending, tracked_deployment_masters,
            )
            if recovery_pending(APP_DIR):
                def recovered(_result, error):
                    if error:
                        self._setup_fail("major_masters_deployed", error)
                        messagebox.showerror("Deployment recovery needs review", str(error), parent=self)
                    else:
                        self._clear_major_master_scan_cache()
                        self._initialize_plugin_baseline(force=True)
                        self.status.set("Interrupted deployment recovered. Review the selected masters before deploying.")
                        self.refresh()
                        if hasattr(self, "_major_master_refresh"):
                            self._major_master_refresh()
                self.after(300, lambda: self._run_progress_job(
                    "Recover interrupted deployment", "Reconciling the interrupted deployment before continuing.",
                    lambda progress: recover_deployment(APP_DIR, self.settings["game_dir"]), recovered))
            else:
                data_dir = Path(self.settings["game_dir"]) / "Data Files"
                tracked = {name.casefold() for name in tracked_deployment_masters(APP_DIR)}
                live = {name.casefold() for name in self.major_masters
                        if (data_dir / name).is_file()}
                staged = self._staged_major_master_names()
                untracked = live - tracked
                if tracked and not untracked and all(name in live for name in tracked):
                    self._setup_complete("major_masters_deployed")
                    self._clear_major_master_scan_cache()
                    self.status.set("Deployment state is complete; the deployed masters are now tracked.")
                    self._initialize_plugin_baseline(force=True)
                    self.refresh()
                elif untracked:
                    self._setup_fail(
                        "major_masters_deployed",
                        "Live major-master files exist without a committed deployment record; recovery/review is required.")
                    self.status.set(
                        "Deployment recovery required: major-master files are untracked. "
                        "Open Tools > Recovery center; nothing was automatically redeployed.")
                elif tracked:
                    self._setup_fail(
                        "major_masters_deployed",
                        "Committed deployment state references major-master files that are missing or incomplete.")
                    self.status.set(
                        "Deployment recovery required: committed major-master files are missing. "
                        "Use Remove deployed masters or the Recovery center.")
                elif staged:
                    self._setup_fail(
                        "major_masters_deployed",
                        "The previous deployment was not committed. Staged masters remain ready for review.")
                    self._clear_major_master_scan_cache()
                    self.status.set(
                        "Interrupted deployment was rolled back. Review the staged major masters and choose Deploy when ready.")
                    self.refresh()
                else:
                    self._setup_complete("major_masters_deployed")
                    self._clear_major_master_scan_cache()
                    self.status.set(
                        "No major-master deployment was committed; staging is not present, "
                        "so the main view was reconciled from live state.")
                    self._initialize_plugin_baseline(force=True)
                    self.refresh()
        if open_dialog:
            self.open_major_masters()
        return True

    def _resume_interrupted_plugin_deployment(self):
        from workflow_safety import DurableTransaction
        root = APP_DIR / "transactions" / "plugin-deploy"
        if not root.is_dir():
            return

        def recovered(_result, error):
            if error:
                messagebox.showerror("Plugin deployment recovery needs review", str(error), parent=self)
            else:
                self.status.set("Interrupted plugin deployment recovered; no incomplete files were left active.")
                self.refresh()

        self._run_progress_job(
            "Recover interrupted plugin deployment",
            "Reconciling the interrupted regular-plugin deployment before continuing.",
            lambda _progress: DurableTransaction.recover(
                root, [APP_DIR, Path(self.settings["game_dir"]),
                       APP_DIR / "staging" / "plugins"]),
            recovered)

    def _set_staging_run(self, masters):
        """Remember (or, with None, forget) the masters chosen for the staging run in progress."""
        if masters:
            self.gui_state["staging_run"] = {"masters": list(masters),
                                             "started_at": setup_state._now()}
        else:
            self.gui_state.pop("staging_run", None)
        write_gui_state(self.gui_state)

    def _pending_stage_masters(self) -> list[str]:
        """Masters of an interrupted staging run that are neither staged nor deployed yet, in priority order."""
        run = self.gui_state.get("staging_run")
        wanted = {str(name).casefold() for name in (run or {}).get("masters", [])} if isinstance(run, dict) else set()
        have = self._staged_major_master_names() | deployed_master_names()
        return [name for name in self.major_masters if name.casefold() in wanted and name.casefold() not in have]

    def stage_major_masters(self, *, force_scan=False, option_masters=None, reuse_answers=False):
        """Review downloaded major masters and unpack selected archives into MWManager staging."""
        initializing = bool(getattr(self, "_initializing_manager", False))
        staging = APP_DIR / "staging" / "major_masters"
        game_data_files = Path(self.settings["game_dir"]) / "Data Files"
        from core_addons import (PACKAGE_STEP_LABELS, begin_package_step, complete_package_step, fail_package_step,
                                 package_steps, reset_partial_package, unfinished_packages)

        def console_python():
            py = Path(sys.executable)
            if py.name.casefold() == "pythonw.exe":
                console_py = py.with_name("python.exe")
                if console_py.is_file():
                    py = console_py
            return py if py.name.casefold() in {"python.exe", "python3.exe", "python"} else None

        def quiet_options():
            if os.name != "nt":
                return {}
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}

        def process_package(progress, py, quiet, mod_dir, group_masters, *, reopen=False,
                            phase="all"):
            """Tidy with the option installer, then the BSA check. Each step is recorded in the package's marker
            as it starts and completes; a step already completed is not run again."""
            steps = package_steps(mod_dir) or {}
            from major_master_deploy import assert_package_disposable, restore_packed_assets, staged_masters
            if group_masters and not {m.casefold() for m in group_masters} <= set(staged_masters(staging, list(group_masters))):
                steps = {}      # marked done, but its master never reached the package: run the steps again
            assert_package_disposable(mod_dir, staging, game_data_files, APP_DIR, self.major_masters,
                                      check_dependencies=False)
            if not (steps.get("tidied") or {}).get("completed_at"):
                restore_packed_assets(mod_dir)
            from core_addons import _read_staged_archive_marker
            marker = _read_staged_archive_marker(mod_dir)
            primary_master = Path(group_masters[0]) if group_masters else next(mod_dir.glob("*.esm"), None)
            PLUGIN_NAME = marker.get("PLUGIN_NAME") or (primary_master.stem if primary_master else mod_dir.name)
            label = ", ".join(group_masters) or mod_dir.name

            def run(step, work, *, force=False):
                if not force and (steps.get(step) or {}).get("completed_at"):
                    return
                begin_package_step(mod_dir, step)
                try:
                    outcome = work()
                except Exception as exc:
                    fail_package_step(mod_dir, step, exc)
                    raise
                complete_package_step(mod_dir, step, outcome=outcome or "completed")

            def options():
                progress(f"Collecting options for {label}. Other archive work will continue while the installer is open.")

                def tidy_line(clean_line):
                    if clean_line.startswith("ASSET CHECK"):
                        progress(f"Tidy {mod_dir.name}: checking assets")

                env = os.environ.copy()          # the option window's "Open source archive" button reads this
                archive = _read_staged_archive_marker(mod_dir).get("archive")
                archive_path = _read_staged_archive_marker(mod_dir).get("archive_path")
                if archive_path:
                    source_archive = Path(archive_path)
                    env["MWMANAGER_SOURCE_ARCHIVE"] = str(source_archive)
                elif archive:
                    source_archive = Path(archive)
                    if not source_archive.is_absolute():
                        source_archive = Path(self.settings.get("archive_downloads_dir", "")) / source_archive
                    env["MWMANAGER_SOURCE_ARCHIVE"] = str(source_archive)
                env["MWMANAGER_PLUGIN_NAME"] = PLUGIN_NAME
                run_tidy_steps(py, staging, game_data_files, progress, f"Options {mod_dir.name}",
                               only=mod_dir.name, reopen=reopen, env=env, quiet=quiet, on_line=tidy_line,
                               ask_only=True)

            def tidy():
                progress(f"Running Tidy for {label}; saved option answers will be reused.")
                env = os.environ.copy()
                archive = _read_staged_archive_marker(mod_dir).get("archive")
                archive_path = _read_staged_archive_marker(mod_dir).get("archive_path")
                if archive_path:
                    env["MWMANAGER_SOURCE_ARCHIVE"] = str(Path(archive_path))
                elif archive:
                    source_archive = Path(archive)
                    if not source_archive.is_absolute():
                        source_archive = Path(self.settings.get("archive_downloads_dir", "")) / source_archive
                    env["MWMANAGER_SOURCE_ARCHIVE"] = str(source_archive)
                env["MWMANAGER_PLUGIN_NAME"] = PLUGIN_NAME
                env["MWMANAGER_REUSE_FOMOD"] = "1"
                run_tidy_steps(py, staging, game_data_files, progress, f"Tidy {mod_dir.name}",
                               only=mod_dir.name, env=env, quiet=quiet)

            def bsa_check():
                if not group_masters:
                    return "not_eligible"
                progress(f"Checking BSA eligibility for {label} after final Tidy pass…")
                from major_master_deploy import count_bsa_eligible
                eligible_assets = count_bsa_eligible(staging, group_masters)
                if eligible_assets >= int(self.settings.get(BSA_OFFER_SETTING, DEFAULT_BSA_OFFER)):
                    if self.settings.get(ALWAYS_BSA_SETTING) or progress.ask_yes_no(
                            f"Build a BSA for {PLUGIN_NAME}?",
                            f"{PLUGIN_NAME} has {eligible_assets:,} BSA-compatible assets after Tidy and FOMOD selection.\n\n"
                            "Build its BSA archive(s) now? BSAPack will split archives before they reach 2 GB. "
                            "The resulting archives will be registered in Morrowind.ini when deployed."):
                        from plugin_install import pack_package_assets
                        packed = pack_package_assets(mod_dir, PLUGIN_NAME, APP_DIR, progress)
                        progress(f"Built {len(packed)} BSA archive(s) for {PLUGIN_NAME}.")
                        return "packed"
                    return "skipped_by_user"
                return "below_threshold"

            if phase in {"all", "options"}:
                run("options_answered", options, force=reopen)
            if phase == "options":
                progress(f"Saved options for {label}.")
                return
            if not (package_steps(mod_dir) or {}).get("options_answered", {}).get("completed_at"):
                raise RuntimeError(f"{label}: options have not been completed; resume the option queue first.")
            run("tidied", tidy)
            run("bsa_checked", bsa_check)
            progress(f"Finished tidy and options for {label}.")

        def unstaged_masters():
            have = self._staged_major_master_names() | deployed_master_names()
            pending = self._pending_stage_masters()
            return [name for name in pending if name.casefold() not in have]

        has_packages = staging.is_dir() and any(p.is_dir() and not p.name.startswith(".")
                                                for p in staging.iterdir())
        if has_packages and not force_scan:
            py = console_python()
            if py is None:
                messagebox.showerror("Cannot reopen staged options",
                                     "The mock FOMOD needs the Python console executable beside pythonw.exe.", parent=self)
                return

            unfinished = unfinished_packages(staging)
            if unfinished or self._pending_stage_masters():
                # an earlier Stage run stopped part-way: finish each package from the step its marker stops at
                def resume(progress):
                    from core_addons import _read_staged_archive_marker
                    restage = 0
                    for number, (package, missing) in enumerate(unfinished, 1):
                        if "extracted" in missing:
                            # a folder with no step record at all may have been put there by hand: ask first
                            if package_steps(package) is None and not progress.ask_yes_no(
                                    "Staged folder without a record",
                                    f"{package.name} has no staging record. Its extraction was interrupted, or the "
                                    "folder was put into staging by hand.\n\nRemove it and extract its archive again?"):
                                progress(f"{package.name}: left as it is.")
                                continue
                            progress(f"{package.name}: the archive was never fully extracted; it will be staged again.")
                            reset_partial_package(package, manager_dir=APP_DIR,
                                                  game_dir=game_data_files.parent, configured=self.major_masters)
                            restage += 1
                            continue
                        progress(f"Resuming package {number} of {len(unfinished)}: {package.name} - "
                                 + ", ".join(PACKAGE_STEP_LABELS[step] for step in missing))
                        masters = [str(name) for name in
                                   _read_staged_archive_marker(package).get("selected_masters", []) if name]
                        process_package(progress, py, quiet_options(), package, masters, reopen=True)
                    return restage

                def resumed(restage, error):
                    if error:
                        if isinstance(error, TidyPlanPaused):
                            self._initializing_manager = False
                            self.status.set("Major-master Tidy paused; staged files were preserved.")
                            messagebox.showinfo("Major-master Tidy paused", str(error), parent=self)
                            return
                        self._setup_fail("major_masters_staged", error)
                        self._initializing_manager = False
                        self.status.set("Resuming major-master staging stopped with an error.")
                        messagebox.showerror("Could not resume major-master staging", str(error), parent=self)
                        return
                    if hasattr(self, "_major_master_refresh"):
                        self._major_master_refresh()
                    staged_names = self._staged_major_master_names()
                    unfinished_names = {name.casefold() for name in self._unfinished_master_stages(staged_names)}
                    self._set_master_stage_ready(staged_names - unfinished_names, True)
                    if unfinished_packages(staging) and not restage:
                        self._setup_fail("major_masters_staged", "Some staged packages were left unprocessed.")
                        self.status.set("Staging paused: some packages were left unprocessed.")
                        return
                    if restage or unstaged_masters():
                        # masters are still missing from staging: open the selection window for them
                        self.status.set("Interrupted packages are finished. Choose the masters still to stage.")
                        self.after(100, lambda: self.stage_major_masters(force_scan=True))
                        return
                    self.status.set("The interrupted major-master staging is finished.")
                    self._after_major_master_staging(
                        initializing, "The interrupted major-master staging is finished and every staged package "
                                      "is processed.\n\nDeploy the staged masters to the game now?")

                self._setup_begin("major_masters_staged")
                self.status.set("Resuming the interrupted major-master staging…")
                self._run_progress_job(
                    "Resume major-master staging",
                    f"Finishing {len(unfinished)} package(s) from the step each one stopped at:\n{staging}\n"
                    "Completed steps are not repeated.",
                    resume, resumed)
                return

            def reopen_options(progress):
                checkpoint(progress)
                names = self._staged_major_master_names()
                if option_masters:
                    names &= {name.casefold() for name in option_masters}
                if not names:
                    raise RuntimeError("None of the selected masters is staged yet, so there are no options to "
                                       "reopen. Select them and use Find and stage archives instead.")
                self._set_master_stage_ready(names, False)
                from major_master_deploy import _package_roots, assert_package_disposable, restore_packed_assets
                # options waiting on a mod that is now staged: the saved choices go in without the window
                previous_reuse = os.environ.get("MWMANAGER_REUSE_FOMOD")
                if reuse_answers:
                    os.environ["MWMANAGER_REUSE_FOMOD"] = "1"
                try:
                    for package, masters in _package_roots(staging, list(names)):
                        assert_package_disposable(package, staging, game_data_files, APP_DIR, self.major_masters,
                                                  check_dependencies=False)
                        begin_package_step(package, "tidied")
                        begin_package_step(package, "bsa_checked")
                        restore_packed_assets(package)
                        process_package(progress, py, quiet_options(), package, masters, reopen=True)
                finally:
                    if reuse_answers:
                        if previous_reuse is None:
                            os.environ.pop("MWMANAGER_REUSE_FOMOD", None)
                        else:
                            os.environ["MWMANAGER_REUSE_FOMOD"] = previous_reuse
                self._set_master_stage_ready(names, True)
                return names

            def options_reopened(_result, error):
                if error:
                    if isinstance(error, TidyPlanPaused):
                        self._initializing_manager = False
                        self.status.set("Staged mock FOMOD Tidy paused; staged files were preserved.")
                        messagebox.showinfo("Staged Tidy paused", str(error), parent=self)
                        return
                    self._setup_fail("major_masters_staged", error)
                    self._initializing_manager = False
                    self.status.set("Staged mock FOMOD stopped with an error.")
                    messagebox.showerror("Could not reopen staged options", str(error), parent=self)
                    return
                staged = (set(_result) if isinstance(_result, (set, list, tuple))
                          else self._staged_major_master_names())
                self.selected_major_masters.update(staged)
                self.gui_state["selected_major_masters"] = [name for name in self.major_masters
                                                            if name.casefold() in self.selected_major_masters]
                write_gui_state(self.gui_state)
                self.status.set("Saved mock FOMOD choices applied to the existing major-master stage.")
                if hasattr(self, "_major_master_refresh"):
                    self._major_master_refresh()
                self._after_major_master_staging(
                    initializing, "The existing major-master stage is processed and ready. No archives were "
                                  "re-extracted.\n\nDeploy the staged masters to the game now?")

            self._setup_begin("major_masters_staged")
            self.status.set("Reopening mock FOMOD for the existing major-master stage…")
            self._run_progress_job(
                "Reopen major-master options",
                f"Applying saved mock FOMOD choices to the existing stage:\n{staging}\n"
                "The archive extraction step will be skipped.",
                reopen_options, options_reopened)
            return
        downloads = self._archive_downloads_folder()
        game_dir = Path(self.settings["game_dir"])
        if downloads is None:
            held = self.settings.get("archive_downloads_dir") or ""
            messagebox.showwarning(
                "Archive Downloads not set",
                (f"The Archive Downloads folder cannot be opened from this session:\n{held}\n\n" if held
                 else "No Archive Downloads folder is saved.\n\n")
                + "Choose the Archive Downloads folder in Options first.", parent=self)
            self.open_options()
            return
        try:
            from core_addons import (edit_master_config, find_companions, prepare_core_master_review,
                                     review_core_master_install, scan_downloads, stage_core_master_archives,
                                     suggest_masters, verify_nexus_versions)

            def scanned(plan, error):
                if error:
                    self._initializing_manager = False
                    self.status.set("Major-master scan stopped with an error.")
                    messagebox.showerror("Could not scan downloads", str(error), parent=self)
                    return
                if plan.get("error") or plan.get("missing_directory"):
                    self._initializing_manager = False
                    messagebox.showerror("Could not scan downloads",
                                         plan.get("error") or "The downloads folder could not be scanned.", parent=self)
                    return
                # --remove before shipping: dev-only master suggestions; the user confirms each, nothing is removed
                declined = set(self.gui_state.get("declined_master_suggestions", []))
                added = []
                for suggestion in plan.pop("_master_suggestions", []):
                    if suggestion["tier"] != "major" or str(suggestion["mod_id"]) in declined:
                        continue       # common masters go in the normal batches; nothing to add here
                    if messagebox.askyesno(
                            "Add a major master?",
                            f"{suggestion['master']} ({suggestion['category']}) is required by "
                            f"{suggestion['required_by']} mods on Nexus, which makes it a major master.\n\n"
                            "Add it to the major masters? No is remembered.", parent=self):
                        stem = Path(suggestion["master"]).stem.casefold()
                        edit_master_config(APP_DIR, add=suggestion["master"],
                                           keywords=sorted({stem, stem.replace("_", " ")}))
                        added.append(suggestion["master"])
                    else:
                        declined.add(str(suggestion["mod_id"]))
                self.gui_state["declined_master_suggestions"] = sorted(declined)
                write_gui_state(self.gui_state)
                if added:
                    self.major_masters = configured_major_masters()
                    self._clear_major_master_scan_cache()
                    self.status.set(f"Added {', '.join(added)}; rescanning downloads…")
                    self.after(100, lambda: self.stage_major_masters(force_scan=True))
                    return
                selected = review_core_master_install(APP_DIR, game_dir, plan, parent=self,
                                                       prepared=plan.pop("_prepared_review", None))
                if selected == "rescan":
                    self.status.set("Rescanning major-master downloads after the Nexus download…")
                    self.after(100, lambda: self.stage_major_masters(force_scan=True))
                    return
                if selected is None:
                    self._set_staging_run(None)                 # cancelled: nothing is left to resume
                    self._initializing_manager = False
                    if initializing:
                        self.status.set("Initialization paused at major-master selection; it will resume next launch.")
                    return
                already_staged = self._staged_major_master_names()
                selected = [name for name in selected if name.casefold() not in already_staged]
                if not selected:
                    if initializing:
                        self._initializing_manager = False
                        self._complete_init()
                        self.open_major_masters()
                    else:
                        messagebox.showinfo("No masters selected", "No new major masters were selected for staging.", parent=self)
                    return
                # the masters of this run are written down before anything is extracted, so a run that is
                # interrupted knows which ones it still owes
                self._set_staging_run(selected)
                selected_keys = {name.casefold() for name in selected}
                selected_archives ={str(item["archive"]).casefold(): item for item in plan.get("archives", [])
                                     if any(name.casefold() in selected_keys for name in item.get("core_masters", []))}
                total_bytes = sum(int(item.get("size_bytes") or 0) for item in selected_archives.values())
                size_text = f"{total_bytes / (1024 ** 3):.2f} GB" if total_bytes else "unknown size"
                staging = APP_DIR / "staging" / "major_masters"

                def staged(result, stage_error):
                    if stage_error:
                        if isinstance(stage_error, TidyPlanPaused):
                            self._initializing_manager = False
                            self.status.set("Major-master Tidy paused; staged files were preserved.")
                            messagebox.showinfo("Major-master Tidy paused", str(stage_error), parent=self)
                            return
                        self._setup_fail("major_masters_staged", stage_error)
                        self._initializing_manager = False
                        self.status.set("Major-master staging and tidy stopped with an error.")
                        messagebox.showerror("Could not process major masters", str(stage_error), parent=self)
                        return
                    destinations = result
                    self._set_staging_run(None)                 # every master of the run is staged
                    self.selected_major_masters.update(name.casefold() for name in selected)
                    self.gui_state["selected_major_masters"] = [name for name in self.major_masters
                                                                if name.casefold() in self.selected_major_masters]
                    write_gui_state(self.gui_state)
                    if hasattr(self, "_major_master_refresh"):
                        self._major_master_refresh()
                    self.status.set(f"Staged and tidied {len(destinations)} archive(s). No live game files were changed.")
                    self._after_major_master_staging(
                        initializing, f"Processed {len(destinations)} archive(s). Live game files have not changed.\n\n"
                                      "Deploy the selected staged masters now?")

                def stage_and_process(progress):
                    # Preserve configured master priority, grouping masters that share one archive.
                    staging.mkdir(parents=True, exist_ok=True)
                    by_master = {name.casefold(): item for item in plan.get("archives", [])
                                 for name in item.get("core_masters", [])}
                    groups, seen_archives = [], set()
                    for master in selected:
                        item = by_master.get(master.casefold())
                        if not item:
                            raise RuntimeError(f"No selected archive remains for {master}; rescan downloads.")
                        key = str(Path(item["archive"]).resolve()).casefold()
                        if key in seen_archives:
                            continue
                        seen_archives.add(key)
                        masters_in_archive = [name for name in selected
                                              if any(name.casefold() == found.casefold()
                                                     for found in item.get("core_masters", []))]
                        groups.append((item, masters_in_archive or [master]))

                    py = console_python()
                    if py is None:
                        raise RuntimeError("The tidy utility needs the Python console executable beside pythonw.exe.")
                    quiet = quiet_options()
                    from mwmanager_batch import (
                        BatchCoordinator, BatchUnit, file_sha256, option_answer_fingerprint, package_id_for,
                        run_threaded_batch,
                    )

                    package_specs = [{
                        "id": package_id_for(item["archive"]),
                        "name": Path(item["archive"]).name,
                        "archive": str(Path(item["archive"]).resolve()),
                        "selected_masters": list(masters),
                    } for item, masters in groups]
                    coordinator = BatchCoordinator.start(APP_DIR, game_dir, package_specs)
                    processing_lock = threading.Lock()
                    stage_ready_lock = threading.Lock()

                    def set_stage_ready(masters, ready):
                        with stage_ready_lock:
                            self._set_master_stage_ready(masters, ready)

                    def config_hash(mod_dir):
                        from core_addons import _read_staged_archive_marker
                        marker_hash = str(_read_staged_archive_marker(mod_dir).get("sha256") or "")
                        candidates = []
                        try:
                            candidates = sorted(
                                (path for path in mod_dir.rglob("*") if path.is_file()
                                 and path.name.casefold() == "moduleconfig.xml"),
                                key=lambda path: str(path).casefold())
                        except OSError:
                            pass
                        return marker_hash + ":" + ":".join(filter(None, (file_sha256(path) for path in candidates)))

                    def answer_hash(mod_dir):
                        import mock_fomod
                        identity = mock_fomod._selection_identity(Path(mod_dir))
                        answer = mock_fomod._answer_file_path(APP_DIR / "outputs", identity)
                        return option_answer_fingerprint(answer)

                    def extract_batch(item, index):
                        group_masters = item["selected_masters"]
                        set_stage_ready(group_masters, False)
                        progress(f"Staging master {index} of {len(groups)}: {', '.join(group_masters)}")
                        paths = stage_core_master_archives(
                            plan, group_masters, APP_DIR, game_dir,
                            progress_callback=progress,
                            clear_staging=(index == 1 and not any(staging.iterdir())),
                            lock_held=True)
                        if len(paths) != 1:
                            raise RuntimeError(
                                f"Expected one staged package for {group_masters}; found {len(paths)}.")
                        return BatchUnit(item["id"], Path(paths[0]), {
                            "masters": list(group_masters),
                        })

                    def prepare_options(unit, reopen):
                        # Options and Tidy share answer, pending-option and compatibility journals.
                        with processing_lock:
                            checkpoint(progress)
                            process_package(
                                progress, py, quiet, unit.path, unit.context["masters"],
                                reopen=reopen, phase="options")

                    def apply_package(unit):
                        with processing_lock:
                            checkpoint(progress)
                            process_package(
                                progress, py, quiet, unit.path, unit.context["masters"], phase="apply")

                    def reset_after_option_change(unit):
                        from core_addons import reset_package_step
                        reset_package_step(unit.path, "tidied")
                        reset_package_step(unit.path, "bsa_checked")

                    units = run_threaded_batch(
                        coordinator,
                        package_specs,
                        extract=extract_batch,
                        prepare_options=prepare_options,
                        apply=apply_package,
                        fingerprint=lambda unit: answer_hash(unit.path),
                        config_fingerprint=lambda unit: config_hash(unit.path),
                        reset_after_option_change=reset_after_option_change,
                        on_ready=lambda unit: set_stage_ready(
                            unit.context["masters"], True),
                        progress=progress,
                    )
                    progress("All selected major-master FOMODs and nested installer rescans are complete.")
                    return [unit.path for unit in units]

                self._setup_begin("major_masters_staged")
                self.status.set("Unpacking major masters into staging…")
                self._run_progress_job(
                    "Staging major masters",
                    f"Starting {len(selected_archives)} archive(s) ({size_text}) for {len(selected)} selected master(s).\n"
                    f"Destination: {staging}\nPreparing archive extraction, tidy, and option steps…",
                    stage_and_process, staged)

            self.status.set("Scanning Archive Downloads for major masters…")
            def scan_worker(progress):
                def checked(message):
                    checkpoint(progress)
                    progress(message)
                checked.ask_yes_no = progress.ask_yes_no
                plan = verify_nexus_versions(
                    scan_downloads(downloads, APP_DIR, APP_DIR / "outputs", progress_callback=checked),
                    progress_callback=checked)
                checkpoint(progress)
                if not plan.get("error") and not plan.get("missing_directory"):
                    checked("Looking for HD and voice packs that go with the masters…")
                    find_companions(plan, downloads, APP_DIR)
                    if self.settings.get("dev_options"):        # --remove before shipping
                        checked("Checking Nexus for other masters many mods require…")
                        plan["_master_suggestions"] = suggest_masters(plan, downloads, APP_DIR,
                                                                      data_dir=Path(game_dir) / "Data Files")
                    plan["_prepared_review"] = prepare_core_master_review(APP_DIR, game_dir, plan, progress=checked)
                return plan
            self._run_progress_job(
                "Scanning major-master downloads",
                "Checking archive contents and hashes. Large archives can take a while…",
                scan_worker, scanned)
        except Exception as exc:
            self._initializing_manager = False
            self.status.set("Major-master staging stopped with an error.")
            messagebox.showerror("Could not stage major masters", str(exc), parent=self)

    def open_options(self, *, first_run=False, game=None, downloads=None, archive_source=None):
        dialog = tk.Toplevel(self)
        dialog.title("MWManager Options")
        dialog.transient(self)
        dialog.after_idle(lambda: center(dialog, self))    # sized by its contents: placed once they are built
        dialog.grab_set()
        dialog.resizable(False, False)
        form = ttk.Frame(dialog, padding=14)
        form.pack(fill="both", expand=True)
        game_var = tk.StringVar(value=game if game is not None else self.settings["game_dir"])
        downloads_var = tk.StringVar(value=downloads if downloads is not None
                                     else self.settings.get("archive_downloads_dir", ""))
        bsa_offer_var = tk.StringVar(value=str(self.settings.get(BSA_OFFER_SETTING, DEFAULT_BSA_OFFER)))
        ttk.Label(form, text="Morrowind folder").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Entry(form, textvariable=game_var, width=62).grid(row=1, column=0, sticky="ew", pady=(3, 12))
        ttk.Button(form, text="Browse…", command=lambda: game_var.set(filedialog.askdirectory(title="Select Morrowind folder", initialdir=game_var.get()) or game_var.get())).grid(row=1, column=1, padx=(8, 0), pady=(3, 12))
        source = self.settings.get("archive_source")
        source_name = {"vortex": "Vortex", "mo2": "Mod Organizer 2", "unmanaged": "a folder you chose"}.get(
            source, source or "not chosen yet")
        ttk.Label(form, text=f"Archive Downloads (source: {source_name})").grid(row=2, column=0, columnspan=2,
                                                                                 sticky="w")
        ttk.Entry(form, textvariable=downloads_var, width=62).grid(row=3, column=0, sticky="ew", pady=(3, 5))
        ttk.Button(form, text="Browse…", command=lambda: downloads_var.set(filedialog.askdirectory(title="Select the Archive Downloads folder", initialdir=downloads_var.get() or str(Path.home())) or downloads_var.get())).grid(row=3, column=1, padx=(8, 0), pady=(3, 5))
        ttk.Label(form, text="Used to find downloaded mods. Staging is configured separately.", wraplength=520).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(form, text="Offer BSA when this many assets are found").grid(
            row=5, column=0, columnspan=2, sticky="w")
        ttk.Entry(form, textvariable=bsa_offer_var, width=12).grid(row=6, column=0, sticky="w", pady=(3, 4))
        always_bsa_var = tk.BooleanVar(value=bool(self.settings.get(ALWAYS_BSA_SETTING)))
        ttk.Checkbutton(form, text="Always Pack BSA", variable=always_bsa_var).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(0, 4))
        limit_vars = {key: tk.BooleanVar(value=self.settings.get(key, True) is not False)
                      for key in ASSET_LIMIT_SETTINGS}
        for offset, (key, text) in enumerate(ASSET_LIMIT_SETTINGS.items()):
            ttk.Checkbutton(form, text=text, variable=limit_vars[key]).grid(
                row=8 + offset, column=0, columnspan=2, sticky="w",
                pady=(0, 12 if offset == len(ASSET_LIMIT_SETTINGS) - 1 else 0))

        def save_close():
            game, downloads = Path(game_var.get()).expanduser(), Path(downloads_var.get()).expanduser()
            try:
                bsa_offer = int(bsa_offer_var.get())
                if bsa_offer < MIN_BSA_OFFER:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid BSA threshold",
                                     f"The BSA offer threshold must be an integer of at least {MIN_BSA_OFFER}.",
                                     parent=dialog)
                return
            import mwmanager_detect
            if mwmanager_detect.valid_install(str(game)) is None:      # the same check as the first window
                messagebox.showerror("Invalid game folder",
                                     "The selected folder must contain Morrowind.exe and a Data Files folder.",
                                     parent=dialog)
                return
            if downloads_var.get().strip() and not downloads.is_dir():
                if not messagebox.askyesno("Downloads folder not found", "Save this path anyway? It will be checked again when Archive Downloads are scanned.", parent=dialog):
                    return
            old_game = Path(self.settings["game_dir"]).expanduser()
            game_changed = str(old_game.resolve()).casefold() != str(game.resolve()).casefold()
            old_downloads = str(self.settings.get("archive_downloads_dir", ""))
            downloads_changed = old_downloads.casefold() != str(downloads).casefold()
            choice = False
            if game_changed and not first_run:      # ask before saving, so Cancel changes nothing
                choice = messagebox.askyesnocancel(
                    "Morrowind folder has changed",
                    "MWManager needs to check the new game folder before it can use it.\n\n"
                    "Yes: save and restart MWManager now.\n"
                    "No: save and check the new folder in this session.\n"
                    "Cancel: go back to Options without saving.",
                    parent=dialog,
                )
                if choice is None:
                    return
            self.settings["game_dir"] = str(game)
            self.settings["archive_downloads_dir"] = str(downloads) if downloads_var.get().strip() else ""
            self.settings[BSA_OFFER_SETTING] = bsa_offer
            self.settings[ALWAYS_BSA_SETTING] = always_bsa_var.get()
            self.settings.update({key: var.get() for key, var in limit_vars.items()})
            if archive_source is not None:
                self.settings["archive_source"] = archive_source
            if first_run:
                self.settings["first_run"] = 1
                self.settings["init_complete"] = 0
                write_settings(self.settings)
                self._setup_start()
                dialog.destroy()
                offer_elevation(game)
                ini_order = read_game_ini_order(game)
                self.order = ini_order or read_order()
                self.active_names = {name.casefold() for name in ini_order} if ini_order else set()
                self.gui_state["game_dir"] = str(game.resolve())
                self.gui_state["known_plugins"] = plugins_in_data(game / "Data Files")
                write_gui_state(self.gui_state)
                self._ensure_core()
                self.refresh()
                self._update_action_buttons()
                self.status.set("Paths saved. Starting preflight for the selected game folder…")
                self.after(150, self._begin_first_run)
                return
            if game_changed:
                self.settings["first_run"] = 1
                self.settings["init_complete"] = 0
                self._setup_start()
            write_settings(self.settings)
            dialog.destroy()
            if game_changed:
                if choice:
                    self._restart_manager()
                    return
                ini_order = read_game_ini_order(game)
                self.order = ini_order or read_order()
                self.active_names = {name.casefold() for name in ini_order} if ini_order else set()
                self.gui_state["game_dir"] = str(game.resolve())
                self.gui_state["known_plugins"] = plugins_in_data(game / "Data Files")
                write_gui_state(self.gui_state)
            self.refresh()
            self._update_action_buttons()
            if game_changed:
                self.status.set("Game path changed; refreshing plugins and rerunning preflight for this game folder…")
                self.after(150, self._begin_first_run)
            elif downloads_changed and self.settings.get("init_complete", 0) != 1:
                self.status.set("Downloads path changed; resuming initialization.")
                self.after(150, self._begin_init)
            else:
                self.status.set("Options saved.")

        buttons = ttk.Frame(form)
        buttons.grid(row=8 + len(ASSET_LIMIT_SETTINGS), column=0, columnspan=2, sticky="e")
        def cancel_options():
            dialog.destroy()
            if first_run:
                self._cancel_until_restart()
        ttk.Button(buttons, text="Cancel", command=cancel_options).pack(side="right")
        ttk.Button(buttons, text="Save", command=save_close).pack(side="right", padx=(0, 6))
        dialog.bind("<Return>", lambda _e: save_close())
        if first_run:
            dialog.protocol("WM_DELETE_WINDOW", cancel_options)


def main():
    lock = _single_instance()
    if lock is None:
        messagebox.showerror("MWManager is already running",
                             "Another MWManager window is open for this folder. Close it first: two of them "
                             "would run the same setup step at the same time.")
        return
    app = ManagerGUI()
    try:
        app.mainloop()
    finally:
        _end_children()
        lock.close()


if __name__ == "__main__":
    main()
