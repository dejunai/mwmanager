#!/usr/bin/env python3
"""Tk desktop front end for MWManager's plugin load order and settings."""

from __future__ import annotations

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

from mwmanager_theme import COLORS, FONTS, SPACE, apply_theme, center, tree_tags

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
    "mwmanager PlaceholderCell ESP Patch.esp",
    "mwmanager Cell Fixes.esp",
    "mwmanager Performers Patch.esp",
    "mwmanager Multipatch.esp",
)
LOCKED_PATCHES = {ESM_FIX_PATCH.casefold(), *(name.casefold() for name in END_PATCHES)}
MERGE_RECORD = APP_DIR / "mwmanager_merged.txt"
MAJOR_MASTER_CONFIG = APP_DIR / "mwmanager_core_addons.json"
GUI_STATE_FILE = APP_DIR / "mwmanager_gui_state.json"
DEPLOYMENT_JOURNAL = APP_DIR / "major_master_deployments.json"
PLUGIN_STAGE_MANIFEST = APP_DIR / "staging" / "plugins" / ".mwmanager-plugin-stages.json"
DISABLED_RECORD = APP_DIR / "mwmanager_disabled.txt"


def _display_line(raw: str, limit: int = 600) -> str:
    """A tool output line for the progress pane: keep its indent (wrapped continuations, list dashes)."""
    line = raw.rstrip().expandtabs(4)
    return line if len(line) <= limit else line[:limit - 1] + "…"


TIDY_SCRIPT = APP_DIR / "mwmanager_install.py"
TIDY_REFUSED = 2                                      # mwmanager_install.py --apply: plan missing or changed


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_tidy_steps(py, staging: Path, data_files: Path, progress, label: str, *, only: str | None = None,
                   reopen: bool = False, env=None, quiet=None, on_line=None, log=None) -> int | None:
    """Run GUI Tidy options, build its plan, then apply it without prompting.

    Returns the number of mods covered; raises RuntimeError when a step fails or --apply refuses because the plan
    changed after it was built.
    """
    base = [str(py), "-u", str(TIDY_SCRIPT), "--staging", str(staging), "--game-data-files", str(data_files)]
    if only:
        base += ["--only", only]
    env = dict(os.environ if env is None else env, MWMANAGER_GUI="1")
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
        recent, mods, plan = [], None, (None, None)
        for line in process.stdout:
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
        if log:
            log(f"{'DONE' if code == 0 else 'FAIL'} {label}: {what}: exit code {code}")
        return code, recent, mods, plan

    code, recent, mods, plan = step(["--options-only" if reopen else "--options"], "options and plan")
    if code:
        raise RuntimeError(f"{label}: the option step failed (exit code {code}).\n\n" + "\n".join(recent)[-4000:])
    checkpoint(progress)
    state, plan_path = plan
    if state == "empty":
        progress(f"{label}: nothing to tidy.")
        return mods
    if state != "pending" or plan_path is None:
        raise RuntimeError(f"{label}: Tidy did not report a valid plan state. Deployment is halted.")
    progress(f"{label}: applying the validated Tidy plan automatically.")
    code, recent, _mods, _plan = step(["--apply", "--yes"], "apply")
    if code == TIDY_REFUSED:
        raise RuntimeError(f"{label}: Tidy refused to apply the plan - it changed since it was reviewed, or there "
                           f"was none. Nothing was changed; run it again to review the new plan.\n\n"
                           + "\n".join(recent)[-4000:])
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


_CHILDREN = set()                 # the processes of running steps: ended when the window is closed


def _popen(*args, **kwargs):
    """subprocess.Popen for a step MWManager waits on: remembered, so closing the window ends it too."""
    process = subprocess.Popen(*args, **kwargs)
    for old in [p for p in _CHILDREN if _ended(p)]:       # forget the ones that have finished
        _CHILDREN.discard(old)
    _CHILDREN.add(process)
    return process


def _ended(process) -> bool:
    poll = getattr(process, "poll", None)
    return poll is None or poll() is not None


def _end_children() -> None:
    """End every step still running (and what it started). A step left alive would keep changing Data Files
    while the next MWManager resumes the same step."""
    for process in list(_CHILDREN):
        if not _ended(process):
            try:
                setup_state.end_process(process.pid)
                process.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        _CHILDREN.discard(process)


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


def vortex_install_path() -> Path | None:
    """Return Vortex's executable path from its Windows registration, if present."""
    if os.name != "nt":
        return None
    try:                        # uninstall entry (InstallLocation or DisplayIcon), App Paths or the nxm:// handler
        import mwmanager_detect
        exe = mwmanager_detect.detect_vortex(mwmanager_detect.default_registry(), read_settings=False).get("exe")
        if exe:
            return Path(exe)
    except Exception:
        pass
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
                            if "vortex" not in display_name.casefold():
                                continue
                            try:
                                install_dir = Path(str(winreg.QueryValueEx(entry, "InstallLocation")[0]))
                                exe = install_dir / "Vortex.exe"
                                if exe.is_file():
                                    return exe
                            except OSError:
                                pass
                    except OSError:
                        continue
        except OSError:
            continue
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\Vortex.exe") as key:
                raw = winreg.QueryValueEx(key, None)[0]
                exe = Path(str(raw).strip('"'))
                if exe.is_file():
                    return exe
        except OSError:
            pass
    return None


def detect_vortex_downloads() -> Path | None:
    # Vortex's own settings first (its download folder for Morrowind), then its default locations.
    try:
        import mwdialog_paths
        mwdialog_paths.clear_vortex_settings_cache()                # Vortex may have changed since the GUI started
        mastercheck = sys.modules.get("mwdialog_mastercheck")       # its staging index / master profiles too
        if mastercheck is not None and hasattr(mastercheck, "clear_caches"):
            mastercheck.clear_caches()
        found = mwdialog_paths.resolve_downloads(required=False)
        if found:
            return Path(found)
    except Exception:
        pass
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "Vortex" / "downloads" / "morrowind",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Vortex" / "downloads" / "morrowind",
    ]
    for path in candidates:
        if str(path) and path.is_dir():
            return path
    # Vortex state files sometimes record a custom download directory.
    appdata = Path(os.environ.get("APPDATA", "")) / "Vortex" / "state.v2" / "persistent.json"
    if appdata.is_file():
        try:
            data = json.loads(appdata.read_text(encoding="utf-8"))
            text = json.dumps(data)
            marker = "morrowind"
            pos = text.lower().find(marker)
            if pos >= 0:
                # Use only this as a candidate; avoid guessing arbitrary config schemas.
                for candidate in candidates:
                    if candidate.is_dir():
                        return candidate
        except (OSError, ValueError):
            pass
    return None


def vortex_registered() -> bool:
    """Return whether Windows has a Vortex install registration."""
    if os.name != "nt":
        return False
    try:
        import winreg
    except ImportError:
        return False

    uninstall_roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, root_path in uninstall_roots:
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
                    except OSError:
                        continue
                    if "vortex" in display_name.casefold():
                        return True
        except OSError:
            continue

    app_paths = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\Vortex.exe"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths\Vortex.exe"),
    )
    for hive, path in app_paths:
        try:
            with winreg.OpenKey(hive, path):
                return True
        except OSError:
            pass
    return False


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
    with MWMANAGER_INI.open("w", encoding="utf-8", newline="\n") as stream:
        ini.write(stream)

    preferences = {key: value for key, value in settings.items()
                   if key not in PATH_SETTING_KEYS and key not in RUNTIME_KEYS and key not in LEGACY_PREFERENCE_KEYS}
    SETTINGS_FILE.write_text(json.dumps(preferences, indent=2) + "\n", encoding="utf-8")


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
                               "MWManager must run at the same elevation level to manage this game install "
                               "reliably. Without elevation, deployment and Morrowind.ini changes may fail.\n\n"
                               "Relaunch MWManager as administrator now?"),
                  justify="left", wraplength=440).pack(anchor="w", pady=(0, 12))
        always_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Always run MWManager as administrator for this install",
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
    try:
        journal = json.loads(DEPLOYMENT_JOURNAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {name.casefold() for deployment in journal.get("deployments", [])
            if isinstance(deployment, dict)
            for name in deployment.get("masters", []) if isinstance(name, str)}


def ordered_signature(plugin_names: list[str], order: list[str], active_names: set[str], game_dir: str) -> str:
    payload = {
        "game_dir": str(Path(game_dir).resolve()).casefold(),
        "plugins": sorted(name.casefold() for name in plugin_names),
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
    def __init__(self):
        super().__init__()
        apply_theme(self)
        self.title("MWManager")
        self._active_job = None
        self.status = tk.StringVar(value="Core ESMs stay enabled and fixed at the top.")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        center(self, None, 900, 620)                     # the main window: middle of the desktop
        self.minsize(760, 500)
        self.settings = read_settings()
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
        if self.settings.get("paths_confirmed"):
            self._setup_reconcile()
        first_run = self.settings.get("first_run", 0) == 1
        paths_confirmed = bool(self.settings.get("paths_confirmed"))
        if first_run and paths_confirmed and not self._setup_done("paths_confirmed"):
            self._setup_complete("paths_confirmed")  # migrate an interrupted run created before checkpoints
        if not first_run or paths_confirmed:
            offer_elevation(Path(self.settings["game_dir"]))
        if not first_run:
            self._initialize_plugin_baseline()
        self._ensure_core()
        self._build_menu()
        self._build_ui()
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
        if (not first_run and self._setup_done("initial_full_complete")
                and not self._setup_done("catalog_complete")):
            # setup stopped (or was made) before the catalog step: build it now, then carry on
            resume = self._begin_init if self.settings.get("init_complete", 0) != 1 else (lambda: None)
            self.after(150, lambda: self._run_initial_catalog(resume))
        elif not first_run and self.settings.get("init_complete", 0) != 1:
            self.after(150, self._begin_init)
        elif not first_run and (self.settings.get("archive_source") is None
                                or not self.settings.get("archive_downloads_dir")):
            self.after(150, self._ensure_archive_downloads)

    def _ensure_core(self):
        order_by_name = {name.casefold(): name for name in self.order}
        core = [order_by_name.get(name.casefold(), name) for name in CORE]
        rest = [n for n in self.order if n.casefold() not in {c.casefold() for c in CORE}]
        self.order = core + rest

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
            if migrated:
                self.status.set(f"Migrated {migrated} legacy asset backup files to Off/Original Install.")
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

    def _initialize_plugin_baseline(self):
        game_dir = str(Path(self.settings["game_dir"]).resolve())
        if (isinstance(self.gui_state.get("known_plugins"), list)
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
        file_menu.add_command(label="Detected installs…", command=lambda: self._open_detected_paths(first_run=False))
        file_menu.add_separator()
        file_menu.add_command(label="Resume setup", command=self._resume_setup)
        file_menu.add_command(label="Exit", command=self._on_close)
        menu.add_cascade(label="File", menu=file_menu)
        self.file_menu = file_menu
        self.config(menu=menu)

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
                self._preflight_signature = preflight_signature(self.settings["game_dir"])
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
                    mode="full", force=True, asset_repair=True, on_success=full_completed, skip_first_pass=skip,
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
        self.save()
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
                for label, command in commands:
                    checkpoint(report)
                    recent = []
                    report(label)
                    log.write(f"\n=== {label.rstrip('…')} ===\n")
                    process = _popen(command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1, **quiet)
                    setup_state.begin(state_path, game_dir, "catalog_complete", getattr(process, "pid", None))
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

    def _open_first_run_options(self):
        self.open_options(first_run=True)

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
        ttk.Label(form, text="Morrowind install").grid(row=1, column=0, columnspan=2, sticky="w")
        row = 2
        if found["games"]:
            for game in found["games"]:
                ttk.Radiobutton(form, text=f"{game['store']}: {game['path']}", value=game["path"],
                                variable=game_var).grid(row=row, column=0, columnspan=2, sticky="w")
                row += 1
        else:
            ttk.Label(form, text="No Morrowind install was found in the registry; choose its folder below.",
                      wraplength=600).grid(row=row, column=0, columnspan=2, sticky="w")
            row += 1
        ttk.Entry(form, textvariable=game_var, width=70).grid(row=row, column=0, sticky="ew", pady=(3, 12))
        ttk.Button(form, text="Browse…", command=lambda: game_var.set(filedialog.askdirectory(
            parent=dialog, title="Select Morrowind install", initialdir=game_var.get() or str(Path.home()))
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

        def approve():
            game = mwmanager_detect.valid_install(game_var.get())
            if game is None:
                messagebox.showerror("Not a Morrowind install",
                                     "The selected folder must contain Morrowind.exe and a Data Files folder.",
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
                                  "archive_source": source, "first_run": 1, "init_complete": 0})
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
            self.status.set("Paths saved. Starting preflight for the selected install…")
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

        actions = ttk.Frame(self, padding=(pad["xl"], pad["md"], pad["xl"], pad["md"]))
        actions.pack(fill="x", side="bottom")
        self.install_plugins_button = ttk.Button(actions, text="Install Plugins", command=self.install_plugins)
        self.install_plugins_button.pack(side="left")
        self.enable_new_button = ttk.Button(actions, text="Enable all new plugins", command=self.enable_all_new_plugins)
        self.enable_new_button.pack(side="left", padx=(pad["sm"], 0))
        self.remove_plugins_button = ttk.Button(actions, text="Remove Installed Plugins…", style="Danger.TButton",
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
        self.merge_status = ttk.Label(right, text="Merge exclusion controls are planned.", style="Card.Muted.TLabel",
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
        self.tree.heading("active", text="On")
        self.tree.heading("state", text="Status")
        self.tree.heading("type", text="Kind")
        self.tree.column("#0", width=360, stretch=True)
        self.tree.column("active", width=52, anchor="center", stretch=False)
        self.tree.column("state", width=120, anchor="w", stretch=False)
        self.tree.column("type", width=110, anchor="w", stretch=False)
        tree_scroll = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        tree_tags(self.tree)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())
        self.tree.bind("<Button-1>", self._click_active_checkbox)

        buttons = ttk.Frame(left, style="Header.TFrame")
        buttons.pack(fill="x", pady=(pad["md"], 0))
        self.up_btn = ttk.Button(buttons, text="▲  Move up", style="Toolbar.TButton",
                                 command=lambda: self.move_selected(-1))
        self.up_btn.pack(side="left")
        self.down_btn = ttk.Button(buttons, text="▼  Move down", style="Toolbar.TButton",
                                   command=lambda: self.move_selected(1))
        self.down_btn.pack(side="left", padx=(pad["xs"], 0))
        ttk.Button(buttons, text="Modify Sort Rules", style="Toolbar.TButton",
                   command=self.open_sort_rules).pack(side="left", padx=(pad["lg"], 0))
        ttk.Button(buttons, text="Show Disabled", style="Toolbar.TButton",
                   command=self.show_disabled_plugins).pack(side="left", padx=(pad["xs"], 0))
        ttk.Button(buttons, text="Save load order", command=self.save).pack(side="right")

    def refresh(self):
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
        installed_major = [disk[n.casefold()] for n in self.major_masters if n.casefold() in disk]
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
        self.save()
        self.status.set(f"Enabled {len(new_plugins)} newly detected plugin(s). Generate Patches to run the checks.")

    def _show_blocked_archive_warning(self, items):
        """Show unsafe archive contents as a readable, formatted list."""
        dialog = tk.Toplevel(self)
        dialog.title("Archives blocked from installation")
        dialog.transient(self)
        center_window(dialog, self, 820, 600)
        dialog.minsize(620, 360)
        ttk.Label(
            dialog,
            text=(f"{len(items)} archive(s) contain executable, library, or script files and are blocked. "
                  "They remain marked BLOCKED in the archive list."),
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
        for index, item in enumerate(items):
            if index:
                text.insert("end", "\n")
            text.insert("end", "— " + Path(item["archive"]).name + "\n", "archive")
            payloads = item.get("blocked_payloads", [])
            for payload in payloads[:4]:
                text.insert("end", "    " + str(payload) + "\n", "payload")
            if len(payloads) > 4:
                text.insert("end", f"    … {len(payloads) - 4} additional files\n", "payload")
        text.configure(state="disabled")
        ttk.Button(dialog, text="OK", command=dialog.destroy).pack(anchor="e", padx=12, pady=12)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        dialog.focus_set()
        self.wait_window(dialog)

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
            "Install Plugins", "From Archive Downloads?\n\nYes: browse the Archive Downloads folder\n"
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
                self._show_blocked_archive_warning(unsafe_items)
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
        dialog.title("Install Plugins")
        dialog.transient(self)
        center_window(dialog, self, 920, 520)
        dialog.minsize(760, 400)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        header = ttk.Frame(frame)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Select archives, then choose an install action. Auto Install prioritizes MWSE-only tools, then larger plugin mods and recent downloads, with replacers and pluginless mods last.",
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
        tree.heading("selected", text="Install")
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
        ttk.Label(footer, text="Auto Install uses the detected order; estimates are content-based.").pack(side="left")

        def close_then_install(chosen):
            if not chosen:
                messagebox.showinfo("Nothing selected", "Select one or more archives first.", parent=dialog)
                return
            blocked = [item for item in chosen if item["blocked"]]
            if blocked:
                messagebox.showerror("Archive needs review",
                                     "These archives cannot be installed until their paths are reviewed:\n\n" +
                                     "\n".join(Path(item["archive"]).name + ": " + str(item["problem"])
                                              for item in blocked[:8]), parent=dialog)
                return
            dialog.destroy()
            self._install_plugin_archives(chosen)

        ttk.Button(footer, text="Install Selected",
                   command=lambda: close_then_install([by_id[i] for i in sorted(selected, key=lambda x: list(by_id).index(x))])).pack(side="right")
        ttk.Button(footer, text="Install All Except Selected",
                   command=lambda: close_then_install([item for iid, item in by_id.items()
                                                        if iid not in selected and not item["blocked"]])).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Auto Install",
                   command=lambda: close_then_install([item for item in items if not item["blocked"]])).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Skip Install", command=dialog.destroy).pack(side="right", padx=(0, 6))

    def _install_plugin_archives(self, items, *, accept_staged=False):
        from plugin_install import (apply_plugin_compatibility, check_deploy_path_lengths, count_bsa_assets,
                                    deploy_package, extract_archive,
                                    pack_package_assets, pull_updated_steam_luas,
                                    index_deployment, record_deployment, remove_superseded_plugins)
        from major_master_deploy import register_archives

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
            run_logged(command, progress, "Plugin checks and patch generation")
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
                conflict = _popen(command, cwd=str(APP_DIR), creationflags=
                                            getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
                if conflict.wait():
                    raise RuntimeError("The interactive cell-conflict review did not complete.")
                run_logged([str(py), "-u", str(manager_script), str(data_dir), "--sort", "--all", "--yes"],
                           progress, "Applying conflict choices and regenerating patches")

        def worker(progress):
            staging_root.mkdir(parents=True, exist_ok=True)
            from preinstall import run_preinstall
            preflight_result = run_preinstall(game_dir, APP_DIR, progress,
                                              archive_dir=self.settings.get("archive_downloads_dir"))
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
                        f"The next batch of {len(pending)} mod(s) is staged and ready to deploy.\n\n"
                        f"Staging folder: {staging_root}\n\n"
                        "Review the opened staging folder. Choose Yes to deploy this batch and continue, or No "
                        "to stop here and leave this batch staged without deploying it.")
                    if not proceed:
                        return False
                # Preflight the entire batch before the first package changes Data Files.
                for package, label in pending:
                    check_deploy_path_lengths(package, data_dir, label)
                for package, label in pending:
                    progress(f"Deploying {label}…")
                    item = pending_items[str(package)]
                    refreshed = pull_updated_steam_luas(package)
                    if refreshed:
                        progress(f"Refreshed {len(refreshed)} MWSE Lua file(s) from the Steam install for {label}.")
                    linked, _skipped, operations = deploy_package(
                        package, data_dir, label, APP_DIR / "protected_replacer_textures.json")
                    archives = sorted(package.glob("*.bsa"), key=lambda path: path.name.casefold())
                    registrations = register_archives(data_dir.parent / "Morrowind.ini", archives, APP_DIR)
                    plugins = item.get("selected_plugins", item.get("plugins", []))
                    record_deployment(APP_DIR, item, package, operations, archives, registrations, data_dir)
                    index_deployment(APP_DIR, data_dir, plugins, archives)
                    _remove_ready_plugin_stage(package)
                    progress(f"Deployed {linked:,} files from {label}.")
                progress("Running --all checks and resolving newly surfaced cell conflicts…")
                run_batch_check(progress)
                pending = []
                pending_items.clear()
                return True

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
                else:
                    archive = Path(item["archive"])
                    safe_stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", archive.stem).strip(" .") or f"plugin-{index}"
                    destination = staging_root / f"{safe_stem} [{index}]"
                    if not destination.is_dir():
                        matching_stages = [path for path in staging_root.iterdir()
                                           if path.is_dir() and path.name.casefold().startswith(
                                               (safe_stem + " [").casefold())]
                        if len(matching_stages) == 1:
                            destination = matching_stages[0]
                    completed_stage = next((entry for entry in _read_ready_plugin_stages()
                                            if str(Path(entry.get("archive", "")).resolve()).casefold()
                                            == str(archive.resolve()).casefold()), None)
                    if completed_stage and Path(completed_stage.get("staging_package", "")).is_dir():
                        package = Path(completed_stage["staging_package"]).resolve()
                        item["selected_plugins"] = completed_stage.get("selected_plugins", item.get("plugins", []))
                        install_plugins = item["selected_plugins"]
                        prepared = True
                        progress(f"Using completed staged files for {archive.name}; skipping Tidy and FOMOD.")
                    elif destination.exists():
                        action = progress.ask_choice(
                            "Staged folder already exists",
                            f"A staging folder exists for {archive.name}:\n\n{destination}\n\n"
                            "Choose how to continue.",
                            [("Reprocess", "reprocess"), ("Accept staged files as ready", "ready"),
                             ("Cancel", "cancel")], "cancel")
                        if action == "cancel":
                            return {"processed": processed, "stopped": True, "cancelled": True,
                                    "staging": str(destination)}
                        package = destination
                        if action == "ready":
                            install_plugins = item.get("plugins", [])
                            item["selected_plugins"] = install_plugins
                            mark_ready(item, package, install_plugins)
                            prepared = True
                            progress(f"Accepted staged files for {archive.name} without reprocessing.")
                        else:
                            progress(f"Reprocessing existing staged files for {archive.name}.")
                    else:
                        progress(f"Extracting {index} of {len(items)}: {archive.name}")
                        package = extract_archive(item, destination, APP_DIR)
                    if not prepared:
                        progress(f"Running Tidy and mock FOMOD/BAIN options for {archive.name}…")
                        log_install_phase(f"BEGIN package {archive.name}")
                        tidy_env = os.environ.copy()
                        tidy_env["MWMANAGER_SOURCE_ARCHIVE"] = str(archive.resolve())
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
                        if eligible > 10000 and len(install_plugins) == 1:
                            plugin_label = install_plugins[0]
                            make_bsa = progress.ask_yes_no(
                                "Build a BSA?",
                                f"{plugin_label} has {eligible:,} BSA-compatible assets after Tidy.\n\n"
                                "Build and register a BSA before deployment? BSAPack may split it into multiple archives.")
                            if make_bsa:
                                packed = pack_package_assets(package, plugin_label, APP_DIR)
                                progress(f"Built {len(packed)} BSA archive(s) for {plugin_label}.")
                        elif eligible > 10000:
                            progress(f"BSA packing skipped for {archive.name}: its assets cannot be attributed to exactly one plugin.")
                        item["selected_plugins"] = install_plugins
                        mark_ready(item, package, install_plugins)
                        log_install_phase(f"READY {archive.name}: {len(install_plugins)} plugin(s), "
                                          f"{eligible} BSA-compatible assets")
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
                self.status.set("Plugin installation stopped; completed batches remain installed.")
                messagebox.showerror("Plugin installation failed", str(error), parent=self)
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
                            "warnings, then retry the install.", parent=self)
                        return
                    self.status.set("Plugin staging was left untouched; no files were deployed from the pending batch.")
                    return
                self.status.set("Plugin installation paused before deployment; the staged batch is ready for inspection.")
                messagebox.showinfo(
                    "Batch left staged",
                    f"Stopped before deploying the next batch. The staged files are here:\n\n{result['staging']}\n\n"
                    "No files from this batch were deployed. Inspect the staging folder before starting another install.",
                    parent=self)
                return
            self.refresh()
            self.gui_state["generated_signature"] = None
            write_gui_state(self.gui_state)
            self._update_action_buttons()
            self.status.set(f"Installed {result['processed']} archive(s). Generate Patches reflects the updated plugin set.")
            messagebox.showinfo("Plugins installed", f"Processed {result['processed']} archive(s) in batches of 15 mods.", parent=self)

        job_title = "Accept staged files as ready" if accept_staged else "Install Plugins"
        self._run_progress_job(job_title, "Preparing staged plugin deployment…", worker, completed)

    def remove_installed_plugins(self):
        from plugin_install import list_deployments
        deployments = list_deployments(APP_DIR)
        if not deployments:
            messagebox.showinfo("No tracked plugin deployments",
                                "MWManager has no regular plugin deployments recorded to remove.", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("Remove Installed Plugins")
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
                    "unregister their BSAs, and roll back their staged Tidy changes? "
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
                result = remove_deployments(APP_DIR, data_dir, ids)
                tidy_failures = []
                staging_root = (APP_DIR / "staging" / "plugins").resolve()
                for package_text in result.get("tidy_packages", []):
                    package = Path(package_text).resolve()
                    if not package.is_relative_to(staging_root):
                        tidy_failures.append(f"{package}: outside plugin staging")
                        continue
                    progress(f"Rolling back Tidy changes for {package.name}…")
                    tidy_command = [str(self._console_python()), "-u", str(APP_DIR / "mwmanager_install.py"),
                                    "--staging", str(staging_root), "--undo", "--only", package.name]
                    tidy_process = subprocess.run(
                        tidy_command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=7200, **quiet)
                    if tidy_process.returncode:
                        tidy_failures.append(f"{package.name}: "
                                             + (tidy_process.stderr or tidy_process.stdout or "rollback failed")[-500:])
                result["tidy_rollback_failures"] = tidy_failures
                progress("Updating the plugin catalog and load order…")
                command = [str(self._console_python()), "-u", str(APP_DIR / "mwmanager.py"),
                           str(data_dir), "--sort", "--all", "--yes"]
                process = subprocess.run(command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, capture_output=True,
                                         text=True, encoding="utf-8", errors="replace",
                                         timeout=7200, **quiet)
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
                                f"{len(result.get('tidy_rollback_failures', [])):,} Tidy rollback(s) need attention.")
                tidy_note = ""
                if result.get("tidy_rollback_failures"):
                    tidy_note = "\n\nTidy rollback needs attention:\n" + "\n".join(
                        result["tidy_rollback_failures"][:5])
                messagebox.showinfo("Plugin deployments removed",
                                    f"Removed files: {result['removed']:,}\nRestored files: {result['restored']:,}\n"
                                    f"Still tracked for review: {result['pending']:,}{tidy_note}", parent=self)

            self._run_progress_job("Remove Installed Plugins", "Removing selected tracked deployments…",
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

    def generate_patches(self, mode="all", force=False, on_success=None, asset_repair=False, *,
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
        self.save()
        self.generate_button.state(["disabled"])
        self.enable_new_button.state(["disabled"])
        self.play_button.state(["disabled"])
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        script = APP_DIR / "mwmanager.py"
        mode = "full" if mode == "full" else "all"
        pass_flag = "--full" if mode == "full" else "--all"
        command = [str(self._console_python()), "-u", str(script), str(data_dir), "--sort", pass_flag, "--yes"]
        if asset_repair:
            command.append("--asset-repair")
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
            recent = []

            def run_full_pass(label):
                checkpoint(report)
                report(label)
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
            return True

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
            if on_success:
                self.after(100, on_success)

        self._run_progress_job("Generate Patches", "Checking plugins and generating patches…", worker, completed)

    def play_game(self):
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
                                           errors="replace", bufsize=1, **quiet)
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
        core = name.casefold() in {n.casefold() for n in CORE}
        merged = name.casefold() in {n.casefold() for n in self.merge_inputs}
        is_major = name.casefold() in {n.casefold() for n in self.major_masters}
        is_fixed = name.casefold() in LOCKED_PATCHES or name.casefold() == XE_SKY_PLUGIN.casefold()
        installed = (Path(self.settings["game_dir"]) / "Data Files" / name).is_file()
        if core or merged or is_major or is_fixed or not installed:
            return "break"
        key = name.casefold()
        if key in self.active_names:
            self.active_names.discard(key)
        else:
            self.active_names.add(key)
        self.refresh()
        self.tree.selection_set(name)
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
            return
        moved = []
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
                os.replace(source, target)
                moved.append((source, target))
        except OSError as exc:
            for source, target in reversed(moved):
                try:
                    if target.is_file() and not source.exists():
                        os.replace(target, source)
                except OSError:
                    pass
            messagebox.showerror("Could not disable plugin", str(exc), parent=self)
            return
        disabled = read_disabled_plugins()
        for name in disabled_now:
            disabled[name] = "disabled in MWManager load-order panel"
        try:
            write_disabled_plugins(disabled)
            write_order(names)
            self._write_manual_order_rules()
        except OSError as exc:
            for source, target in reversed(moved):
                try:
                    if target.is_file() and not source.exists():
                        os.replace(target, source)
                except OSError:
                    pass
            for path, content in previous_state.items():
                try:
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_write(path, content)
                except OSError:
                    pass
            messagebox.showerror("Could not save load order", str(exc), parent=self)
            return
        self.order = names
        self.active_names = {n.casefold() for n in names}
        self.refresh()
        self.status.set(f"Saved {len(names)} active plugins; moved {len(moved)} disabled plugin(s) to MWManager's disabled_plugins folder.")

    def show_disabled_plugins(self):
        """List disabled plugin records and restore selected files into the active load order."""
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        off_dirs = (disabled_plugins_dir(), data_dir / "Off" / "Disabled Plugins")   # new place, then the old one
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
                result = subprocess.run(command, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, capture_output=True, text=True,
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
            current = ("; MWManager load-order rules\n\n"
                       "[NearStart]\n" + ESM_FIX_PATCH + "\n\n[NearEnd]\n" +
                       "\n".join((END_PATCHES[2], END_PATCHES[0], END_PATCHES[3])) + "\n")
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
            default = ("; MWManager load-order rules\n\n"
                       "[NearStart]\n" + ESM_FIX_PATCH + "\n\n[NearEnd]\n" +
                       "\n".join((END_PATCHES[2], END_PATCHES[0], END_PATCHES[3])) + "\n")
            try:
                rules_path.write_text(default, encoding="cp1252", newline="\r\n")
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
        dialog.transient(self)
        center_window(dialog, self, 780, 680)
        dialog.minsize(680, 600)
        masters = list(self.major_masters)
        reorder_warning_shown = False
        try:
            saved_master_data = json.loads(MAJOR_MASTER_CONFIG.read_text(encoding="utf-8"))
            keywords = {name: saved_master_data.get(name, []) for name in masters}
        except (OSError, ValueError):
            keywords = {name: [] for name in masters}
        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Optional community masters", font=FONTS["h2"]).pack(anchor="w")
        ttk.Label(frame, text="Installed masters can be enabled and prioritized here. Missing masters stay in this catalog only.", wraplength=660).pack(anchor="w", pady=(3, 8))
        tree_box = ttk.Frame(frame)
        tree_box.pack(fill="both", expand=True)
        tree = ttk.Treeview(tree_box, columns=("active", "installed"), show="tree headings", selectmode="browse")
        tree.heading("#0", text="Priority / Master")
        tree.heading("active", text="On")
        tree.heading("installed", text="Status")
        tree.column("#0", width=390, stretch=True)
        tree.column("active", width=55, anchor="center", stretch=False)
        tree.column("installed", width=100, anchor="center")
        tree.grid(row=0, column=0, sticky="nsew")
        master_scroll = ttk.Scrollbar(tree_box, orient="vertical", command=tree.yview)
        master_scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=master_scroll.set)
        tree_box.columnconfigure(0, weight=1)
        tree_box.rowconfigure(0, weight=1)
        tree.tag_configure("installed", foreground=COLORS["green"])
        tree.tag_configure("ready", foreground=COLORS["blue"])
        tree.tag_configure("absent", foreground=COLORS["amber"])
        tree.tag_configure("required", foreground=COLORS["red"])

        def refresh_masters():
            if not dialog.winfo_exists():
                return
            from major_master_deploy import staged_masters
            staged_map = staged_masters(APP_DIR / "staging" / "major_masters", masters)
            tree.delete(*tree.get_children())
            required_names = self._required_major_master_names()
            unfinished = {name.casefold() for name in self._unfinished_master_stages(masters)}
            for i, name in enumerate(masters, 1):
                exists = (data_dir / name).is_file()
                key = name.casefold()
                enabled = key in self.selected_major_masters
                ready = key in staged_map and not exists
                required = name.casefold() in required_names
                if required and (exists or ready):
                    self.selected_major_masters.add(key)
                    if exists:
                        self.active_names.add(name.casefold())
                check = "☑" if (exists or ready) and (enabled or required) else "☐"
                state = "Installed" if exists else "Ready for install" if ready else "Not staged"
                if ready and key in unfinished:
                    state = "Needs processing"
                tag = "required" if required else "installed" if exists else "ready" if ready else "absent"
                tree.insert("", "end", iid=name, text=f"{i:02d}  {name}", values=(check, state), tags=(tag,))
            self.deploy_master_button.state(["!disabled"] if any(
                name.casefold() in self.selected_major_masters and name.casefold() in staged_map
                and name.casefold() not in unfinished
                and not (data_dir / name).is_file() for name in masters) else ["disabled"])
            from major_master_deploy import tracked_deployment_masters
            tracked = tracked_deployment_masters(APP_DIR)
            self.remove_master_button.state(["!disabled"] if any((data_dir / name).is_file() for name in tracked) else ["disabled"])
            update_master_buttons()

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
        for label, color in (("Green — installed", COLORS["green"]), ("Blue — ready for install", COLORS["blue"]), ("Red — required by another master; cannot be disabled", COLORS["red"]), ("Amber — not staged", COLORS["amber"])):
            ttk.Label(legend, text=label, foreground=color, wraplength=660, justify="left").pack(anchor="w")
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(10, 0))
        footer_top = ttk.Frame(footer)
        footer_top.pack(fill="x")
        footer_bottom = ttk.Frame(footer)
        footer_bottom.pack(fill="x", pady=(SPACE["sm"], 0))

        def update_master_buttons(_event=None):
            selection = tree.selection()
            if not selection:
                up.state(["disabled"])
                down.state(["disabled"])
                if hasattr(self, "unstage_master_button"):
                    self.unstage_master_button.state(["disabled"])
                return
            idx = masters.index(selection[0])
            up.state(["disabled"] if idx == 0 else ["!disabled"])
            down.state(["disabled"] if idx == len(masters) - 1 else ["!disabled"])
            if hasattr(self, "unstage_master_button"):
                staged = self._staged_major_master_names()
                ready = (selection[0].casefold() in staged and not (data_dir / selection[0]).is_file()
                         and selection[0].casefold() not in self._required_major_master_names())
                self.unstage_master_button.state(["!disabled"] if ready else ["disabled"])

        def unstage_selected():
            import mwmanager_dialogs as messagebox
            import shutil
            from major_master_deploy import staged_masters
            selection = tree.selection()
            if not selection:
                return
            name = selection[0]
            staging_root = (APP_DIR / "staging" / "major_masters").resolve()
            staged_map = staged_masters(staging_root, masters)
            esm = staged_map.get(name.casefold())
            if esm is None or (data_dir / name).is_file():
                return
            package = next((parent for parent in esm.parents if parent.parent == staging_root), None)
            if (package is None or package.resolve() == staging_root
                    or not package.resolve().is_relative_to(staging_root)):
                messagebox.showerror("Cannot unstage master",
                                     "The staged package path is outside major-master staging.", parent=dialog)
                return
            package_masters = [key for key, path in staged_map.items()
                               if package.resolve() in path.resolve().parents]
            display = [next((master for master in masters if master.casefold() == key), key)
                       for key in package_masters]
            if not messagebox.askyesno(
                    "Unstage major-master package?",
                    f"Remove the staged package containing {name}?\n\n"
                    f"This package also contains: {', '.join(display)}\n\n"
                    "The live Data Files folder will not be changed.", parent=dialog):
                return
            def remove_package(progress):
                progress(f"Removing staged package: {package.name}")
                shutil.rmtree(package)
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

            self._run_progress_job("Unstage major master", f"Removing staged package {package.name}…",
                                   remove_package, package_removed)

        def click_master(event):
            if getattr(self, "_active_job", None):
                return "break"
            if tree.identify("region", event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
                return
            name = tree.identify_row(event.y)
            staged = self._staged_major_master_names()
            if (not name or (not (data_dir / name).is_file() and name.casefold() not in staged)
                    or name.casefold() in self._required_major_master_names()):
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
            refresh_masters()
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
            self.save()
            if close and not getattr(self, "_initializing_manager", False):
                dialog.destroy()
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
            footer_top, text="Reopen staged options…", command=lambda: saved_action(self.stage_major_masters))
        self.stage_master_button.pack(side="left")
        self.stage_more_master_button = ttk.Button(
            footer_top, text="Find / stage downloads…",
            command=lambda: saved_action(lambda: self.stage_major_masters(force_scan=True)))
        self.stage_more_master_button.pack(side="left", padx=(6, 0))
        self.deploy_master_button = ttk.Button(
            footer_top, text="Deploy Major Masters", command=lambda: saved_action(self.deploy_major_masters))
        self.deploy_master_button.pack(side="left", padx=(6, 0))
        self.remove_master_button = ttk.Button(footer_bottom, text="Remove deployed masters", command=self.remove_deployed_major_masters)
        self.remove_master_button.pack(side="left", padx=(6, 0))
        self.unstage_master_button = ttk.Button(footer_bottom, text="Unstage selected", command=unstage_selected)
        self.unstage_master_button.pack(side="left", padx=(6, 0))
        ttk.Button(footer_bottom, text="Save master order", command=save_masters, width=22, style="MasterSave.TButton").pack(side="right")
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
        refresh_masters()

    def _staged_major_master_names(self):
        """Return configured major-master ESMs present in current staged packages."""
        staging = APP_DIR / "staging" / "major_masters"
        if not staging.is_dir():
            return set()
        wanted = {name.casefold() for name in self.major_masters}
        found = set()
        for root, dirs, files in os.walk(staging):
            dirs[:] = [name for name in dirs if name.casefold() not in {"off", "bugreports", "extras"}]
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

    def _unfinished_master_stages(self, names):
        try:
            state = json.loads(self._master_stage_record().read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        return [name for name in names if state.get(name.casefold()) is not True]

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
        configured = {name.casefold() for name in self.major_masters}
        roots = [Path(self.settings["game_dir"]) / "Data Files", APP_DIR / "staging" / "major_masters"]
        required = set()
        for root in roots:
            if not root.is_dir():
                continue
            for base, dirs, files in os.walk(root):
                dirs[:] = [name for name in dirs if name.casefold() not in {"off", "bugreports", "extras"}]
                for filename in files:
                    if Path(filename).suffix.casefold() != ".esm" or filename.casefold() not in configured:
                        continue
                    required.update(self._esm_master_names(Path(base) / filename) & configured)
        # A master cannot be disabled when another configured master names it as a dependency.
        dependents = set()
        for root in roots:
            if not root.is_dir():
                continue
            for base, dirs, files in os.walk(root):
                dirs[:] = [name for name in dirs if name.casefold() not in {"off", "bugreports", "extras"}]
                for filename in files:
                    if Path(filename).suffix.casefold() == ".esm" and filename.casefold() in configured:
                        dependents.update(self._esm_master_names(Path(base) / filename) & configured)
        return dependents

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
            messagebox.showinfo("No masters selected", "Select at least one processed, staged master in the On column.", parent=self)
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
        accept_assets = _accept_assets
        if accept_assets is None:
            accept_assets = messagebox.askyesnocancel(
                "Deploy selected major masters?",
                "Selected masters, in priority order:\n\n" + "\n".join(ready) +
                "\n\nYes: deploy plugins, archives and loose assets.\n"
                "No: deploy plugins and archives only.\nCancel: leave the game unchanged.\n\n"
                "Replaced loose assets are preserved under the staged package's Off folder.",
                parent=self)
            if accept_assets is None:
                return

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
                self._preflight_signature = preflight_signature(self.settings["game_dir"])
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
                self.status.set("Major-master deployment stopped with an error.")
                messagebox.showerror("Could not deploy major masters", str(error), parent=self)
                return
            self.active_names.update(name.casefold() for name in result["ready"])
            self.selected_major_masters.update(name.casefold() for name in result["ready"])
            major_keys = {name.casefold() for name in self.major_masters}
            other = [name for name in self.order
                     if name.casefold() not in major_keys and name.casefold() not in {c.casefold() for c in CORE}]
            installed_active = [name for name in self.major_masters
                                if (data_dir / name).is_file() and name.casefold() in self.selected_major_masters]
            self.order = list(CORE) + installed_active + other
            self.gui_state["selected_major_masters"] = [n for n in self.major_masters
                                                        if n.casefold() in self.selected_major_masters]
            write_gui_state(self.gui_state)
            self.save()
            if hasattr(self, "_major_master_refresh"):
                self._major_master_refresh()
            self.refresh()
            self.status.set(f"Deployed {len(result['ready'])} major master(s); linked {len(result['linked']):,} files.")
            self._setup_flow_active = False
            detail = [f"Masters: {', '.join(result['ready'])}",
                      f"Hardlinked files: {len(result['linked']):,}",
                      f"Existing files preserved under Off: {len(result['backed_up']):,}",
                      f"Identical files already present: {len(result['skipped_identical']):,}"]
            if result["archives"]:
                detail.append("Registered BSA archives: " + ", ".join(result["archives"]))
            messagebox.showinfo("Major masters deployed", "\n".join(detail), parent=self)
            if result.get("conflicts"):
                self._resolve_major_master_conflicts(result["conflicts"])

        self._run_progress_job("Deploy Major Masters", "Hardlinking selected masters and their assets…", worker, completed)

    def remove_deployed_major_masters(self):
        """Remove only masters deployed by MWManager and restore replaced files."""
        import mwmanager_dialogs as messagebox
        from major_master_deploy import tracked_deployment_masters

        data_dir = Path(self.settings["game_dir"]) / "Data Files"
        staging = APP_DIR / "staging" / "major_masters"
        tracked = [name for name in tracked_deployment_masters(APP_DIR) if (data_dir / name).is_file()]
        if not tracked:
            messagebox.showinfo("No deployed masters to remove",
                                "There are no MWManager-tracked major masters installed in Data Files.", parent=self)
            return
        if not messagebox.askyesno(
                "Remove deployed masters?",
                f"Remove {len(tracked)} MWManager-deployed master(s)?\n\n"
                "MWManager will remove only files it hardlinked, restore files it preserved during deployment, "
                "unregister archives it added to Morrowind.ini, and clear these masters from its catalog. "
                "Staged masters and their BSAs will remain available for redeployment.", parent=self):
            return
        self.remove_master_button.state(["disabled"])

        def worker(progress):
            from major_master_deploy import remove_deployed
            return remove_deployed(staging, data_dir, APP_DIR, progress=progress)

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
        ttk.Button(footer, text="Use load order; close", command=dialog.destroy).pack(side="right")
        ttk.Button(footer, text="Save batch / next", command=save_batch).pack(side="right", padx=(0, 8))
        render()

    def _run_progress_job(self, title, initial_text, worker, on_complete):
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
        elif title == "Install Plugins":
            description = ("Each archive is extracted to its own staging folder, tidied, and passed through its FOMOD or mock BAIN options. "
                           "Before each 15-mod deployment batch, MWManager opens staging for review and waits for approval. "
                           "After deployment it runs --all and opens conflict review when new cell choices are needed.")
        elif title == "Scan plugin archives":
            description = "MWManager is inspecting archive contents to identify plugins, MWSE files, installers, asset counts, and download dates."
        else:
            description = {
                "First-run preflight": "Check the game and prerequisites. Only installation and configuration choices need your input.",
                "Initial setup": "Sort and tidy the shipped files. Safe fixes apply automatically; output is shown below.",
                "Catalog": "Build the plugin and archive catalog. Completed setup checkpoints will be kept.",
                "Pre-deploy checks": "Recheck prerequisites before changing the live game.",
                "Scanning major-master downloads": "Inspect downloaded archives and check versions before choosing what to stage.",
            }.get(title, "Process the staged files. Tidy applies automatically; installer choices remain interactive.")
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
            answered.wait()
            return bool(response.get("answer"))

        def ask_choice(question_title, question_text, choices, cancel_value=None):
            control.check()
            response = {"choice": cancel_value}
            answered = threading.Event()
            events.put(("choice", question_title, question_text, choices, response, answered))
            answered.wait()
            return response.get("choice")

        def launch_vortex():
            response = {"opened": False}
            answered = threading.Event()
            events.put(("launch_vortex", response, answered))
            answered.wait()
            return bool(response.get("opened"))

        def wait_for_vortex_purge():
            response = {"choice": "cancel"}
            answered = threading.Event()
            events.put(("wait_vortex_purge", response, answered))
            answered.wait()
            return response.get("choice")

        def run_program_with_reference(path, reference_title, reference_text):
            response = {"returncode": None, "error": None}
            answered = threading.Event()
            events.put(("run_program", str(path), reference_title, reference_text, response, answered))
            answered.wait()
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
                events.put(("done", worker(report), None))
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
                    reference.attributes("-topmost", True)
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
                    center(reference, self, min(680, reference.winfo_screenwidth() - 48),
                           min(720, reference.winfo_screenheight() - 80))
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
        view.stage.set(self.status.get())
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
            for index in range((self.file_menu.index("end") or 0) + 1):
                if self.file_menu.type(index) == "command":
                    self._busy_menu_states.append((index, self.file_menu.entrycget(index, "state")))
                    self.file_menu.entryconfigure(index, state="disabled")
        else:
            for widget, previous in getattr(self, "_busy_widget_states", []):
                try:
                    widget.state(["!disabled"])
                    widget.state(previous)
                except tk.TclError:
                    pass
            for index, state in getattr(self, "_busy_menu_states", []):
                self.file_menu.entryconfigure(index, state=state)
            self._busy_widget_states = []

    def _pause_setup(self, reason):
        step, _entry = setup_state.interrupted(self._setup_path(), self.settings["game_dir"])
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
        _end_children()
        self.destroy()

    def stage_major_masters(self, *, force_scan=False):
        """Review downloaded major masters and unpack selected archives into MWManager staging."""
        initializing = bool(getattr(self, "_initializing_manager", False))
        staging = APP_DIR / "staging" / "major_masters"
        if staging.is_dir() and any(staging.iterdir()) and not force_scan:
            game_data_files = APP_DIR / "staging" / "Data Files"
            py = Path(sys.executable)
            if py.name.casefold() == "pythonw.exe":
                console_py = py.with_name("python.exe")
                if console_py.is_file():
                    py = console_py
            if py.name.casefold() not in {"python.exe", "python3.exe", "python"}:
                messagebox.showerror("Cannot reopen staged options",
                                     "The mock FOMOD needs the Python console executable beside pythonw.exe.", parent=self)
                return

            def reopen_options(progress):
                checkpoint(progress)
                names = self._staged_major_master_names()
                self._set_master_stage_ready(names, False)
                quiet = {}
                if os.name == "nt":
                    startup = subprocess.STARTUPINFO()
                    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startup.wShowWindow = subprocess.SW_HIDE
                    quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}
                run_tidy_steps(py, staging, game_data_files, progress, "Saved mock FOMOD choices",
                               reopen=True, quiet=quiet)
                self._set_master_stage_ready(names, True)
                return True

            def options_reopened(_result, error):
                if error:
                    self._initializing_manager = False
                    self.status.set("Staged mock FOMOD stopped with an error.")
                    messagebox.showerror("Could not reopen staged options", str(error), parent=self)
                    return
                staged = self._staged_major_master_names()
                self.selected_major_masters.update(staged)
                self.gui_state["selected_major_masters"] = [name for name in self.major_masters
                                                            if name.casefold() in self.selected_major_masters]
                write_gui_state(self.gui_state)
                self.status.set("Saved mock FOMOD choices applied to the existing major-master stage.")
                if hasattr(self, "_major_master_refresh"):
                    self._major_master_refresh()
                if initializing:
                    self._initializing_manager = False
                    self._complete_init()
                if staged and messagebox.askyesno(
                        "Staged options updated",
                        "The existing stage is processed and ready. No archives were extracted.\n\n"
                        "Deploy the selected staged masters now? Choose No to keep them staged for later.",
                        parent=self):
                    self.after(100, self.deploy_major_masters)
                elif initializing:
                    self._setup_flow_active = False
                    self.after(100, self.open_major_masters)

            self.status.set("Reopening mock FOMOD for the existing major-master stage…")
            self._run_progress_job(
                "Reopen major-master options",
                f"Applying saved mock FOMOD choices to the existing stage:\n{staging}\n"
                "The archive extraction step will be skipped.",
                reopen_options, options_reopened)
            return
        downloads_setting = str(self.settings.get("archive_downloads_dir") or "").strip()
        downloads = Path(downloads_setting).expanduser()
        game_dir = Path(self.settings["game_dir"])
        if not downloads_setting or not downloads.is_dir():
            messagebox.showwarning("Archive Downloads not set",
                                   "Choose the Archive Downloads folder in Options first.", parent=self)
            self.open_options()
            return
        try:
            from core_addons import (prepare_core_master_review, review_core_master_install, scan_downloads,
                                     stage_core_master_archives, verify_nexus_versions)

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
                selected = review_core_master_install(APP_DIR, game_dir, plan, parent=self,
                                                       prepared=plan.pop("_prepared_review", None))
                if selected == "rescan":
                    self.status.set("Rescanning major-master downloads after the Nexus download…")
                    self.after(100, lambda: self.stage_major_masters(force_scan=True))
                    return
                if selected is None:
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
                selected_keys = {name.casefold() for name in selected}
                selected_archives = {str(item["archive"]).casefold(): item for item in plan.get("archives", [])
                                     if any(name.casefold() in selected_keys for name in item.get("core_masters", []))}
                total_bytes = sum(int(item.get("size_bytes") or 0) for item in selected_archives.values())
                size_text = f"{total_bytes / (1024 ** 3):.2f} GB" if total_bytes else "unknown size"
                staging = APP_DIR / "staging" / "major_masters"

                def staged(result, stage_error):
                    if stage_error:
                        self._initializing_manager = False
                        self.status.set("Major-master staging and tidy stopped with an error.")
                        messagebox.showerror("Could not process major masters", str(stage_error), parent=self)
                        return
                    destinations = result
                    self.selected_major_masters.update(name.casefold() for name in selected)
                    self.gui_state["selected_major_masters"] = [name for name in self.major_masters
                                                                if name.casefold() in self.selected_major_masters]
                    write_gui_state(self.gui_state)
                    if hasattr(self, "_major_master_refresh"):
                        self._major_master_refresh()
                    self.status.set(f"Staged and tidied {len(destinations)} archive(s). No live game files were changed.")
                    if initializing:
                        self._initializing_manager = False
                        self._complete_init()
                    if messagebox.askyesno(
                            "Major masters ready",
                            f"Processed {len(destinations)} archive(s). Live game files have not changed.\n\n"
                            "Deploy the selected masters now? Choose No to keep them staged for later.",
                            parent=self):
                        self.after(100, self.deploy_major_masters)
                    elif initializing:
                        self._setup_flow_active = False
                        self.open_major_masters()

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

                    staged_paths = []
                    staging_root = APP_DIR / "staging" / "major_masters"
                    game_data_files = APP_DIR / "staging" / "Data Files"

                    py = Path(sys.executable)
                    if py.name.casefold() == "pythonw.exe":
                        console_py = py.with_name("python.exe")
                        if console_py.is_file():
                            py = console_py
                    if py.name.casefold() not in {"python.exe", "python3.exe", "python"}:
                        raise RuntimeError("The tidy utility needs the Python console executable beside pythonw.exe.")
                    quiet = {}
                    if os.name == "nt":
                        startup = subprocess.STARTUPINFO()
                        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        startup.wShowWindow = subprocess.SW_HIDE
                        quiet = {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}

                    for index, (item, group_masters) in enumerate(groups, 1):
                        checkpoint(progress)
                        self._set_master_stage_ready(group_masters, False)
                        # Every Stage action extracts fresh archives and reruns the full pipeline.
                        progress(f"Staging master {index} of {len(groups)}: {', '.join(group_masters)}")
                        paths = stage_core_master_archives(plan, group_masters, APP_DIR, game_dir,
                                                           progress_callback=progress,
                                                           clear_staging=(index == 1 and not any(staging.iterdir())))
                        for mod_dir in paths:
                            staged_paths.append(mod_dir)
                            progress(f"Running tidy and option installer for {', '.join(group_masters)}. "
                                     "Choose any optional packages in the installer window that opens.")
                            def tidy_line(clean_line, name=mod_dir.name):
                                if clean_line.startswith("ASSET CHECK"):
                                    progress(f"Tidy {name}: checking assets")

                            run_tidy_steps(py, staging_root, game_data_files, progress, f"Tidy {mod_dir.name}",
                                           only=mod_dir.name, quiet=quiet, on_line=tidy_line)
                            progress(f"Checking BSA eligibility for {', '.join(group_masters)} after final Tidy pass…")
                            from major_master_deploy import count_bsa_eligible
                            eligible_assets = count_bsa_eligible(staging_root, group_masters)
                            if eligible_assets > 10000:
                                plugin_label = group_masters[0]
                                if progress.ask_yes_no(
                                        f"Build a BSA for {plugin_label}?",
                                        f"{plugin_label} has {eligible_assets:,} BSA-compatible assets after Tidy and FOMOD selection.\n\n"
                                        "Build its BSA archive(s) now? BSAPack will split archives before they reach 2 GB. "
                                        "The resulting archives will be registered in Morrowind.ini when deployed."):
                                    from plugin_install import pack_package_assets
                                    packed = pack_package_assets(mod_dir, plugin_label, APP_DIR)
                                    progress(f"Built {len(packed)} BSA archive(s) for {plugin_label}.")
                            progress(f"Finished tidy and installer for {', '.join(group_masters)}.")
                        self._set_master_stage_ready(group_masters, True)
                        checkpoint(progress)
                    return staged_paths

                self.status.set("Unpacking major masters into staging…")
                self._run_progress_job(
                    "Staging major masters",
                    f"Starting {len(selected_archives)} archive(s) ({size_text}) for {len(selected)} selected master(s).\n"
                    f"Destination: {staging}\nPreparing archive extraction, tidy, and installer steps…",
                    stage_and_process, staged)

            self.status.set("Scanning Archive Downloads for major masters…")
            def scan_worker(progress):
                def checked(message):
                    checkpoint(progress)
                    progress(message)
                plan = verify_nexus_versions(
                    scan_downloads(downloads, APP_DIR, APP_DIR / "outputs", progress_callback=checked),
                    progress_callback=checked)
                checkpoint(progress)
                if not plan.get("error") and not plan.get("missing_directory"):
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
        ttk.Label(form, text="Morrowind install folder").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Entry(form, textvariable=game_var, width=62).grid(row=1, column=0, sticky="ew", pady=(3, 12))
        ttk.Button(form, text="Browse…", command=lambda: game_var.set(filedialog.askdirectory(title="Select Morrowind install", initialdir=game_var.get()) or game_var.get())).grid(row=1, column=1, padx=(8, 0), pady=(3, 12))
        source = self.settings.get("archive_source")
        source_name = {"vortex": "Vortex", "mo2": "Mod Organizer 2", "unmanaged": "a folder you chose"}.get(
            source, source or "not chosen yet")
        ttk.Label(form, text=f"Archive Downloads (source: {source_name})").grid(row=2, column=0, columnspan=2,
                                                                                 sticky="w")
        ttk.Entry(form, textvariable=downloads_var, width=62).grid(row=3, column=0, sticky="ew", pady=(3, 5))
        ttk.Button(form, text="Browse…", command=lambda: downloads_var.set(filedialog.askdirectory(title="Select the Archive Downloads folder", initialdir=downloads_var.get() or str(Path.home())) or downloads_var.get())).grid(row=3, column=1, padx=(8, 0), pady=(3, 5))
        ttk.Label(form, text="Used to find downloaded mods. Install staging is configured separately.", wraplength=520).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 12))

        def save_close():
            game, downloads = Path(game_var.get()).expanduser(), Path(downloads_var.get()).expanduser()
            if not (game / "Data Files").is_dir():
                messagebox.showerror("Invalid game folder", "The selected folder must contain a Data Files folder.", parent=dialog)
                return
            if downloads_var.get().strip() and not downloads.is_dir():
                if not messagebox.askyesno("Downloads folder not found", "Save this path anyway? It will be checked again when Archive Downloads are scanned.", parent=dialog):
                    return
            old_game = Path(self.settings["game_dir"]).expanduser()
            game_changed = str(old_game.resolve()).casefold() != str(game.resolve()).casefold()
            old_downloads = str(self.settings.get("archive_downloads_dir", ""))
            downloads_changed = old_downloads.casefold() != str(downloads).casefold()
            self.settings["game_dir"] = str(game)
            self.settings["archive_downloads_dir"] = str(downloads) if downloads_var.get().strip() else ""
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
                self.status.set("Paths saved. Starting preflight for the selected install…")
                self.after(150, self._begin_first_run)
                return
            if game_changed:
                self.settings["first_run"] = 1
                self.settings["init_complete"] = 0
                self._setup_start()
            write_settings(self.settings)
            dialog.destroy()
            if game_changed:
                choice = messagebox.askyesnocancel(
                    "Morrowind Install Path has changed",
                    "MWManager must restart to reinitialize preflight against the selected install.\n\n"
                    "Yes restarts MWManager, sets first_run to 1, and uses the saved INI paths.\n"
                    "No continues in this session without restarting.\n"
                    "Cancel disables MWManager controls until you close it.",
                    parent=self,
                )
                if choice is None:
                    self._cancel_until_restart()
                    return
                if choice:
                    self.settings["first_run"] = 1
                    write_settings(self.settings)
                    self._restart_manager()
                    return
                self.settings["first_run"] = 1
                write_settings(self.settings)
                ini_order = read_game_ini_order(game)
                self.order = ini_order or read_order()
                self.active_names = {name.casefold() for name in ini_order} if ini_order else set()
                self.gui_state["game_dir"] = str(game.resolve())
                self.gui_state["known_plugins"] = plugins_in_data(game / "Data Files")
                write_gui_state(self.gui_state)
            self.refresh()
            self._update_action_buttons()
            if game_changed:
                self.status.set("Game path changed; refreshing plugins and rerunning preflight for this install…")
                self.after(150, self._begin_first_run)
            elif downloads_changed and self.settings.get("init_complete", 0) != 1:
                self.status.set("Downloads path changed; resuming initialization.")
                self.after(150, self._begin_init)
            else:
                self.status.set("Options saved.")

        buttons = ttk.Frame(form)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")
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
