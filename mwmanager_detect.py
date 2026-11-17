#!/usr/bin/env python3
"""Pre-preflight: find Morrowind installs and the Archive Downloads folder, read-only.

MWManager's first window shows what this module finds so the user can approve or change it before the
options window and preflight. Everything here only reads: the Windows registry (via winreg), Steam's
libraryfolders.vdf and the mod manager's own settings. Nothing is created or changed anywhere.

Archive Downloads is manager-agnostic: it is the folder MWManager reads mod archives from. It can come from a
detected mod manager or be any folder the user chooses ("unmanaged").

Mod managers are pluggable: each detector in MOD_MANAGER_DETECTORS returns one dict. Vortex is supported;
Mod Organizer 2 is detected but reported as not supported yet, so a later version only has to fill it in.
"""
from __future__ import annotations

import json
import os
import re
import sys
import textwrap
from pathlib import Path

MORROWIND_GOG_ID = "1435828767"
MORROWIND_STEAM_APP = "22320"
WIDTH = 115
UNINSTALL_ROOTS = (("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
                   ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
                   ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"))


class WinRegistry:
    """Read-only access to the Windows registry. Tests pass a fake with the same three methods."""

    def __init__(self):
        import winreg
        self._w = winreg
        self._hives = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}

    def _open(self, hive, path):
        return self._w.OpenKey(self._hives[hive], path, 0, self._w.KEY_READ)

    def subkeys(self, hive: str, path: str) -> list[str]:
        names = []
        try:
            with self._open(hive, path) as key:
                index = 0
                while True:
                    try:
                        names.append(self._w.EnumKey(key, index))
                    except OSError:
                        break
                    index += 1
        except OSError:
            pass
        return names

    def values(self, hive: str, path: str) -> dict:
        found = {}
        try:
            with self._open(hive, path) as key:
                index = 0
                while True:
                    try:
                        name, value, _kind = self._w.EnumValue(key, index)
                    except OSError:
                        break
                    found[name] = value
                    index += 1
        except OSError:
            return {}
        return found

    def exists(self, hive: str, path: str) -> bool:
        try:
            with self._open(hive, path):
                return True
        except OSError:
            return False


class NoRegistry:
    """Used off Windows: every lookup is empty."""

    def subkeys(self, hive, path):
        return []

    def values(self, hive, path):
        return {}

    def exists(self, hive, path):
        return False


def default_registry():
    if os.name != "nt":
        return NoRegistry()
    try:
        return WinRegistry()
    except ImportError:
        return NoRegistry()


def _clean_path(raw) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith('"'):
        text = text[1:].split('"', 1)[0]
    else:
        text = re.split(r",\s*-?\d+$", text)[0]          # DisplayIcon "C:\x\y.exe,0"
    return Path(text.replace("/", "\\") if os.name == "nt" else text) if text else None


def valid_install(path) -> Path | None:
    """The folder holding Morrowind.exe and Data Files, from a folder or an .exe path; else None."""
    candidate = _clean_path(path)
    if candidate is None:
        return None
    if candidate.suffix.casefold() == ".exe":
        candidate = candidate.parent
    try:
        return candidate if (candidate / "Morrowind.exe").is_file() and (candidate / "Data Files").is_dir() else None
    except OSError:
        return None


def _vdf_library_paths(steam_root: Path) -> list[Path]:
    try:
        content = (steam_root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    values = re.findall(r'"path"\s+"([^"]+)"', content, flags=re.I)
    return [Path(value.replace("\\\\", "\\")) for value in values]


def detect_game_installs(reg=None) -> list[dict]:
    """Every Morrowind install the registry points at: [{"store", "path", "source"}], unique by folder."""
    reg = reg or default_registry()
    found, seen = [], set()

    def add(store, raw, source):
        path = valid_install(raw)
        if path is None:
            return
        key = os.path.normcase(os.path.normpath(str(path))).casefold()
        if key in seen:
            return
        seen.add(key)
        found.append({"store": store, "path": str(path), "source": source})

    for hive, root in (("HKLM", r"Software\WOW6432Node\GOG.com\Games"), ("HKLM", r"Software\GOG.com\Games"),
                       ("HKCU", r"Software\GOG.com\Games")):
        for sub in reg.subkeys(hive, root):
            values = reg.values(hive, rf"{root}\{sub}")
            if sub == MORROWIND_GOG_ID or "morrowind" in str(values.get("gameName", "")).casefold():
                for name in ("path", "workingDir"):
                    add("GOG", values.get(name), rf"{hive}\{root}\{sub} ({name})")
    for hive, root in UNINSTALL_ROOTS:
        for sub in reg.subkeys(hive, root):
            values = reg.values(hive, rf"{root}\{sub}")
            display = str(values.get("DisplayName", ""))
            if "morrowind" not in display.casefold():
                continue
            if sub.casefold() == f"steam app {MORROWIND_STEAM_APP}":
                store = "Steam"
            elif sub.casefold().startswith(MORROWIND_GOG_ID) or str(values.get("Publisher", "")).casefold() == "gog.com":
                store = "GOG"
            elif "epic" in str(values.get("Publisher", "")).casefold():
                store = "Epic"
            else:
                store = "Other"
            for name in ("InstallLocation", "Inno Setup: App Path", "InstallDir"):
                add(store, values.get(name), rf"{hive}\{root}\{sub} ({name})")
    steam_roots = []
    for hive, key in (("HKCU", r"Software\Valve\Steam"), ("HKLM", r"Software\WOW6432Node\Valve\Steam"),
                      ("HKLM", r"Software\Valve\Steam")):
        values = reg.values(hive, key)
        for name in ("SteamPath", "InstallPath"):
            if values.get(name):
                steam_roots.append((_clean_path(values[name]), rf"{hive}\{key} ({name})"))
    for root, source in steam_roots:
        if root is None:
            continue
        for library in [root] + _vdf_library_paths(root):
            add("Steam", library / "steamapps" / "common" / "Morrowind", source + " + libraryfolders.vdf")
    for hive, key in (("HKLM", r"Software\WOW6432Node\Bethesda Softworks\Morrowind"),
                      ("HKLM", r"Software\Bethesda Softworks\Morrowind")):
        installed = reg.values(hive, key).get("Installed Path")
        if installed:
            path = valid_install(installed)
            already = path and os.path.normcase(os.path.normpath(str(path))).casefold() in seen
            if not already:
                add("Registry (last launched)", installed, rf"{hive}\{key} (Installed Path)")
    for manifest in _epic_manifests():
        add("Epic", manifest.get("InstallLocation"), manifest.get("_file", "Epic manifest"))
    return found


def _epic_manifests() -> list[dict]:
    """Epic Games Launcher manifests that name Morrowind (Epic has no registry entry per game)."""
    import json
    base = os.environ.get("ProgramData")
    if not base:
        return []
    folder = Path(base) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    found = []
    try:
        items = list(folder.glob("*.item"))
    except OSError:
        return []
    for item in items:
        try:
            data = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "morrowind" in str(data.get("DisplayName", "")).casefold():
            data["_file"] = str(item)
            found.append(data)
    return found


# ---------------------------------------------------------------- mod managers (pluggable)

def _basename(path) -> str:
    return re.split(r"[\\/]", str(path))[-1].casefold()


def _nxm_handler(reg) -> tuple[Path | None, str]:
    for hive, key in (("HKCU", r"Software\Classes\nxm\shell\open\command"),
                      ("HKLM", r"Software\Classes\nxm\shell\open\command")):
        command = reg.values(hive, key).get("")
        if command:
            return _clean_path(command), rf"{hive}\{key}"
    return None, ""


def _uninstall_entry(reg, needle: str) -> tuple[dict, str]:
    for hive, root in UNINSTALL_ROOTS:
        for sub in reg.subkeys(hive, root):
            values = reg.values(hive, rf"{root}\{sub}")
            if needle in str(values.get("DisplayName", "")).casefold():
                return values, rf"{hive}\{root}\{sub}"
    return {}, ""


def detect_vortex(reg, read_settings: bool = True) -> dict:
    """Vortex.exe from its uninstall entry, App Paths or the nxm:// handler; downloads from its own settings."""
    result = {"name": "Vortex", "key": "vortex", "supported": True, "detected": False, "exe": "",
              "downloads": "", "sources": [], "notes": []}
    values, source = _uninstall_entry(reg, "vortex")
    candidates = []
    if values:
        result["detected"] = True
        result["sources"].append(source)
        if values.get("InstallLocation"):
            candidates.append(_clean_path(values["InstallLocation"]) / "Vortex.exe")
        candidates.append(_clean_path(values.get("DisplayIcon")))
    for hive in ("HKCU", "HKLM"):
        app = reg.values(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\Vortex.exe").get("")
        if app:
            result["detected"] = True
            result["sources"].append(rf"{hive}\...\App Paths\Vortex.exe")
            candidates.append(_clean_path(app))
    handler, handler_source = _nxm_handler(reg)
    if handler and _basename(handler) == "vortex.exe":
        result["detected"] = True
        result["sources"].append(handler_source + " (nxm:// links open Vortex)")
        candidates.append(handler)
    exe = next((c for c in candidates if c and _basename(c) == "vortex.exe" and c.is_file()), None)
    result["exe"] = str(exe) if exe else ""
    if not read_settings:
        return result
    try:
        import mwdialog_paths
        info = mwdialog_paths.read_vortex_settings(refresh=True)     # read Vortex again: the GUI runs for long
        downloads = mwdialog_paths.resolve_downloads(required=False)
        if downloads:
            result["downloads"] = str(downloads)
            result["downloads_template"] = str(info.get("downloads_tmpl") or "")
            result["sources"].append("Vortex settings (%APPDATA%\\Vortex\\state.v2): download path "
                                     + str(info.get("downloads_tmpl") or "default"))
        elif result["detected"]:
            result["notes"].append(info.get("reason") or "Vortex's settings did not name a Morrowind download "
                                   "folder that exists.")
    except Exception as exc:                                     # never let detection stop the first window
        result["notes"].append(f"Could not read Vortex settings: {exc}")
    return result


def detect_mo2(reg) -> dict:
    """Stub: Mod Organizer 2 is recognised so the first window can say so, but it is not supported yet."""
    result = {"name": "Mod Organizer 2", "key": "mo2", "supported": False, "detected": False, "exe": "",
              "downloads": "", "sources": [], "notes": []}
    values, source = _uninstall_entry(reg, "mod organizer")
    handler, handler_source = _nxm_handler(reg)
    if values:
        result["detected"] = True
        result["sources"].append(source)
    if handler and _basename(handler) in {"nxmhandler.exe", "modorganizer.exe"}:
        result["detected"] = True
        result["sources"].append(handler_source)
    if result["detected"]:
        result["notes"].append("Mod Organizer 2 support is planned; choose its downloads folder by hand for now.")
    return result


MOD_MANAGER_DETECTORS = [detect_vortex, detect_mo2]


def detect_managers(reg=None) -> list[dict]:
    """One dict per detector: name, key, supported, detected, exe, downloads (Archive Downloads), sources, notes."""
    reg = reg or default_registry()
    return [detector(reg) for detector in MOD_MANAGER_DETECTORS]


def detect_all(reg=None) -> dict:
    reg = reg or default_registry()
    return {"games": detect_game_installs(reg), "managers": detect_managers(reg)}


def preferred_game(games: list[dict], current: str = "") -> str:
    """Keep the configured install when it is still valid; otherwise the first GOG, then the first found."""
    if current and valid_install(current):
        return str(valid_install(current))
    for store in ("GOG", "Steam"):
        for game in games:
            if game["store"] == store:
                return game["path"]
    return games[0]["path"] if games else ""


def preferred_downloads(managers: list[dict], current: str = "") -> str:
    if current and Path(current).is_dir():
        return current
    return next((m["downloads"] for m in managers if m.get("supported") and m.get("downloads")), "")


def _same_folder(a, b) -> bool:
    norm = lambda p: os.path.normcase(os.path.normpath(str(p))).rstrip("\\/").casefold()
    return bool(a) and bool(b) and norm(a) == norm(b)


def source_for_folder(managers: list[dict], folder: str) -> str:
    """The key of the supported manager whose Archive Downloads folder this is, else "unmanaged"."""
    return next((m["key"] for m in managers if m.get("supported") and _same_folder(m.get("downloads"), folder)),
                "unmanaged")


def preferred_source(managers: list[dict], saved: str | None, folder: str) -> str:
    """Archive source to preselect: the manager owning `folder`; else the user's own folder when one is set or
    they chose unmanaged before; else the first supported manager with a folder; else unmanaged."""
    owner = source_for_folder(managers, folder) if folder else "unmanaged"
    if owner != "unmanaged" or folder or saved == "unmanaged":
        return owner
    return next((m["key"] for m in managers if m.get("supported") and m.get("downloads")), "unmanaged")


def report_lines(found: dict) -> list[str]:
    lines = []
    if not found["games"]:
        lines.append("Morrowind installs: none found in the registry")
    for game in found["games"]:
        lines.append(f"Morrowind ({game['store']}): {game['path']} (from {game['source']})")
    for manager in found["managers"]:
        if not manager["detected"]:
            lines.append(f"{manager['name']}: not detected")
            continue
        bits = [f"exe {manager['exe']}" if manager["exe"] else "exe not found",
                f"Archive Downloads {manager['downloads']}" if manager["downloads"]
                else "Archive Downloads folder not found"]
        lines.append(f"{manager['name']}: " + ", ".join(bits) + ("" if manager["supported"] else " (not supported yet)"))
        lines.extend(f"{manager['name']} note: {note}" for note in manager["notes"])
        lines.extend(f"{manager['name']} found via {src}" for src in manager["sources"])
    return lines


def main(argv=None) -> int:
    found = detect_all()
    print("Detected (read-only):")
    for line in report_lines(found):
        print(textwrap.fill(line, WIDTH, initial_indent="- ", subsequent_indent="  "))
    return 0


if __name__ == "__main__":
    sys.exit(main())


def vortex_install_path() -> Path | None:
    """Return Vortex's executable path from its Windows registration, if present."""
    if os.name != "nt":
        return None
    try:                        # uninstall entry (InstallLocation or DisplayIcon), App Paths or the nxm:// handler
        exe = detect_vortex(default_registry(), read_settings=False).get("exe")
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
