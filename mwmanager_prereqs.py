"""Detect the user-installed prerequisites MWManager relies on, and point to their official download pages.

MWSE, MGE XE and the Morrowind Code Patch are installed by the user, never by MWManager. Detection is
read-only. When something is missing, MWManager only opens the official page (in its embedded Nexus browser
when that is available, otherwise the system browser); it never downloads or installs anything. MWSE's own
updater (MWSE-Update.exe) is only ever started by preflight after the user clicks to run it.
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path

# Official pages (checked 2026-11-13). The embedded browser only opens nexusmods.com pages; anything else
# opens in the system browser.
PAGES = {
    "mwse": {"name": "MWSE (Morrowind Script Extender)",
             "page": "https://www.nexusmods.com/morrowind/mods/45468?tab=files",
             "also": "https://github.com/MWSE/MWSE/releases",
             "note": "Extract into the Morrowind folder and run MWSE-Update.exe. The MGE XE installer can also "
                     "install MWSE."},
    "mge_xe": {"name": "MGE XE",
               "page": "https://www.nexusmods.com/morrowind/mods/41102?tab=files",
               "also": "https://github.com/Greatness7/MGE-XE/releases",
               "note": "Any MGE XE build works; the Greatness7 build is recommended. Install the Code Patch "
                       "first."},
    "mge_g7": {"name": "MGE XE, Greatness7 build (recommended)",
               "page": "https://github.com/Greatness7/MGE-XE/releases",
               "note": "Extract over the Morrowind folder, then run MGEXEgui.exe once so it writes mgeXE.toml."},
    "mcp": {"name": "Morrowind Code Patch",
            "page": "https://www.nexusmods.com/morrowind/mods/19510?tab=files",
            "note": "Copy into the Morrowind folder, close the game, run 'Morrowind Code Patch.exe'."},
    "mcp_beta": {"name": "MCP Beta: Skunk Works",
                 "page": "https://www.nexusmods.com/morrowind/mods/26348?tab=files",
                 "note": "Required. Install over the base Code Patch, then run 'Morrowind Code Patch.exe' again."},
}
INSTALL_ORDER = ("mcp", "mcp_beta", "mge_xe", "mwse")
MWSE_LATEST_API = "https://api.github.com/repos/MWSE/MWSE/releases/tags/build-automatic"
MCP_DESCRIBE_NAMES = ("describe.json", "description.json")
# The three masters and three BSAs are identical across stores: these SHA-256 values were read from GOG's
# goggame-1435828767.hashdb-verified install and from the Steam install on 2026-11-13 (both matched). Anything
# else in the install, including Morrowind.exe (the Code Patch rewrites it), is never checked against a store.
CORE_FILE_HASHES = {
    "Morrowind.esm": ("5c3c8c2cbd20e25901b59b3ece33d36b7ef0e3d60ad8d11828bcc61a5ead1647",),
    "Tribunal.esm": ("2ace511f23cc2a9ddd5f3aa59c7919789b9378cf4b17c8ae3375dd6b782f3f2b",),
    "Bloodmoon.esm": ("bd27090d0e6ad4c1bf1abc83f1a2dac56fcc82cae7bfe8263c413fb301801357",),
    "Morrowind.bsa": ("3dcd5e6bfa08245521a53374bf733ee5c920df49103898a15aa31144ad64cf48",),
    "Tribunal.bsa": ("3901e7a146f7a64a4ae9534c80d5974f7ab15adf5c48c4561d9313c0f7831d69",),
    "Bloodmoon.bsa": ("7c20956791400d958cb407f0b7c1c19ceaf46719df7eb724d0b450299360bd7c",),
}
MGE_EXECUTABLES = ("MGEXEgui.exe", "MGEgui.exe", "MGE XE.exe", "MGE XE GUI.exe")
_VS_FIXED = b"\xbd\x04\xef\xfe"


def pe_version(path) -> str | None:
    """FileVersion from a Windows PE version resource (VS_FIXEDFILEINFO), or None."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    at = data.find(_VS_FIXED)
    if at < 0 or at + 24 > len(data):
        return None
    ms, ls = struct.unpack_from("<II", data, at + 8)
    if not (ms or ls):
        return None
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _mwse_log_value(game_dir: Path, pattern: str) -> str | None:
    try:
        text = (game_dir / "MWSE.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = re.findall(pattern, text, re.I)
    return found[-1].strip() if found else None


def _detect_mwse(g: Path) -> dict:
    """MWSE.dll is the requirement. MWSE-Update.exe is its updater and writes mwse-version.txt (the build's
    commit) each time it installs a build, so that file is how the installed build is identified."""
    dll, updater, marker = g / "MWSE.dll", g / "MWSE-Update.exe", g / "mwse-version.txt"
    evidence = [str(p.relative_to(g)) for p in (dll, updater, marker, g / "Data Files" / "MWSE" / "core")
                if p.exists()]
    build = None
    try:
        text = marker.read_text(encoding="ascii", errors="ignore").strip()
        build = text.lower() if re.fullmatch(r"[0-9a-fA-F]{7,40}", text) else None
    except OSError:
        pass
    present = dll.is_file()
    return {"present": present, "version": pe_version(dll) if present else None, "build": build,
            "updater": str(updater) if updater.is_file() else "", "evidence": evidence,
            "missing": [] if present else ["MWSE.dll"]}


def _detect_mge(g: Path) -> dict:
    """Any MGE XE build is accepted. The Greatness7 build keeps its settings in mgeXE.toml, which it writes the
    first time MGEXEgui.exe is run, so that file confirms G7."""
    exe = next((g / n for n in MGE_EXECUTABLES if (g / n).is_file()), None)
    d3d8 = g / "d3d8.dll"
    extras = [p for p in (g / "mge3", g / "mgeXE.toml", g / "MGE.ini") if p.exists()]
    evidence = [str(p.relative_to(g)) for p in ([exe] if exe else []) + ([d3d8] if d3d8.is_file() else []) + extras]
    present = exe is not None or (d3d8.is_file() and bool(extras))
    version = pe_version(exe) if exe else None
    toml = next((p for p in (g / "mgeXE.toml", g / "mge3" / "mgeXE.toml") if p.is_file()), None)
    try:
        g7_files = sorted(p.name for p in (g / "mge3").glob("*.toml"))
    except OSError:
        g7_files = []
    return {"present": present, "version": version, "g7": toml is not None,
            "g7_settings": str(toml.relative_to(g)) if toml else "", "g7_files_without_settings": g7_files
            if toml is None else [], "evidence": evidence,
            "missing": [] if present else ["MGEXEgui.exe (or d3d8.dll with MGE XE data)"]}


def mcp_describe_file(g: Path) -> Path | None:
    return next((g / "mcpatch" / n for n in MCP_DESCRIBE_NAMES if (g / "mcpatch" / n).is_file()), None)


def _detect_mcp(g: Path) -> dict:
    """The Code Patch is required with its Skunk Works beta. Skunk Works ships its own mcpatch\\describe.json
    whose first category is "Beta"; that is the check. Morrowind.exe itself is never compared."""
    describe = mcp_describe_file(g)
    original, patcher = g / "Morrowind.Original.exe", g / "Morrowind Code Patch.exe"
    evidence = [str(p.relative_to(g)) for p in (describe, original, patcher) if p and p.exists()]
    version = None
    for readme in ("MCP Readme.txt", "MCP History.txt"):
        try:
            match = re.search(r"version\s+(\d+(?:\.\d+)+)", (g / readme).read_text(encoding="cp1252", errors="replace"),
                              re.I)
        except OSError:
            continue
        if match:
            version = match.group(1)
            break
    first, beta_patches, error = None, 0, None
    if describe:
        try:
            data = json.loads(describe.read_text(encoding="utf-8-sig"))
            head = data[0] if isinstance(data, list) and data else None
            if isinstance(head, dict):
                first = str(head.get("category", ""))
                beta_patches = len(head.get("patches", []) or [])
        except (OSError, ValueError, TypeError) as exc:
            error = str(exc)
    present = bool(describe) or original.is_file() or patcher.is_file()
    beta = (first or "").strip().casefold() == "beta" if describe and error is None else (None if error else False)
    return {"present": present, "version": version, "skunk_works_beta": beta, "first_category": first,
            "beta_patches": beta_patches, "describe_file": str(describe.relative_to(g)) if describe else "",
            "error": error, "evidence": evidence,
            "missing": [] if present else ["mcpatch\\describe.json, Morrowind Code Patch.exe or Morrowind.Original.exe"]}


def detect(game_dir) -> dict:
    """Read-only: {"mwse": {...}, "mge_xe": {...}, "mcp": {...}}."""
    g = Path(game_dir)
    return {"mwse": _detect_mwse(g), "mge_xe": _detect_mge(g), "mcp": _detect_mcp(g)}


def missing_keys(found: dict) -> list[str]:
    """Pages to offer, in install order: base Code Patch, Skunk Works, MGE XE, MWSE."""
    keys = []
    mcp = found.get("mcp", {})
    if not mcp.get("present"):
        keys += ["mcp", "mcp_beta"]
    elif not mcp.get("skunk_works_beta"):
        keys.append("mcp_beta")
    keys += [k for k in ("mge_xe", "mwse") if not found.get(k, {}).get("present")]
    return keys


def latest_mwse_build(timeout: float = 8) -> tuple[str | None, str | None]:
    """(commit, None) of the build MWSE-Update.exe would install, or (None, reason) when it can't be checked.

    MWSE-Update.exe downloads the 'build-automatic' release; its target commit is what mwse-version.txt holds
    after an update. Network is optional: offline simply means the status is unknown."""
    from urllib.request import Request, urlopen
    try:
        request = Request(MWSE_LATEST_API, headers={"Accept": "application/vnd.github+json",
                                                    "User-Agent": "MWManager"})
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:                                     # offline, rate-limited, proxy, ...
        return None, f"GitHub could not be reached ({exc.__class__.__name__}: {exc})"
    sha = str(payload.get("target_commitish", "")) if isinstance(payload, dict) else ""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        return None, "GitHub did not return a build commit for MWSE's build-automatic release"
    return sha.lower(), None


def mwse_status(item: dict, latest: str | None) -> str:
    """"current", "outdated" or "unknown" (offline, or no mwse-version.txt to compare)."""
    if not latest:
        return "unknown"
    if not item.get("build"):
        return "outdated"          # never updated by MWSE-Update.exe (or the record was removed)
    return "current" if latest.startswith(item["build"]) or item["build"] == latest else "outdated"


_MCP_FEATURES = re.compile(r"(?:Morrowind )?Code Patch[^:\r\n]*:\s*([0-9][0-9,\s]*)", re.I)
_MGE_VERSION = re.compile(r"Found MGE XE\. Version:\s*([^\r\n]+)", re.I)


def _head(path: Path, lines: int) -> list[str] | None:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            return [stream.readline().rstrip("\r\n") for _ in range(lines)]
    except OSError:
        return None


def code_patch_log(game_dir) -> dict:
    """Informational: the Code Patch features the game saw at its last start, read from the logs (read-only).

    Install order matters for this: Code Patch first, then MGE XE, then MWSE. At startup the second line of
    mgeXE.log is checked first; MWSE also writes "Found MGE XE. Version: ..." and "Morrowind Code Patch installed
    features: ..." at the top of MWSE.log, which is where they appear with current builds. No log simply means
    the game has not been started with MGE XE yet."""
    g = Path(game_dir)
    result = {"found": False, "source": "", "line": "", "features": None, "mge_version": None,
              "skunk_works_ids_missing": None}
    head = _head(g / "mgeXE.log", 2)
    if head is not None:
        result["mge_log_line2"] = head[1]
        match = _MCP_FEATURES.search(head[1])
        if match:
            result.update(found=True, source="mgeXE.log line 2", line=head[1])
    if not result["found"]:
        head = _head(g / "MWSE.log", 12)
        for number, line in enumerate(head or [], 1):
            version = _MGE_VERSION.search(line)
            if version and not result["mge_version"]:
                result["mge_version"] = version.group(1).strip()
            match = _MCP_FEATURES.search(line)
            if match and not result["found"]:
                result.update(found=True, source=f"MWSE.log line {number}", line=line)
    if result["found"]:
        features = sorted({int(n) for n in re.findall(r"\d+", _MCP_FEATURES.search(result["line"]).group(1))})
        result["features"] = features
        result["skunk_works_ids_missing"] = sorted(set(range(180, 196)) - set(features))
    return result


def code_patch_log_line(info: dict) -> str:
    if not info.get("found"):
        return ("Code Patch features seen by the game: no log yet. Run the game once with MGE XE to confirm "
                "(install order: Code Patch, then MGE XE, then MWSE)")
    features, missing = info["features"], info["skunk_works_ids_missing"]
    beta = ("all 16 Skunk Works beta features (180-195) applied" if not missing else
            f"{16 - len(missing)} of 16 Skunk Works beta features applied")
    return (f"Code Patch features seen by the game ({info['source']}): {len(features)} applied, {beta}"
            + (f"; MGE XE {info['mge_version']}" if info.get("mge_version") else ""))


# Files whose change means the game has not yet been started with the current setup: the Code Patch rewrites
# Morrowind.exe, MGE XE ships d3d8.dll/MGEXEgui.exe, MWSE ships MWSE.dll and MWSE-Update.exe writes the record.
SETUP_FILES = ("Morrowind.exe", "mcpatch/describe.json", "mcpatch/description.json", "d3d8.dll", "MGEXEgui.exe",
               "MWSE.dll", "mwse-version.txt")
STARTUP_LOGS = ("mgeXE.log", "MWSE.log")


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def startup_logs(game_dir) -> dict:
    """Read-only: are mgeXE.log and MWSE.log present and newer than the installed Code Patch, MGE XE and MWSE?

    The logs only exist after Morrowind has been launched once, so "fresh" also confirms the setup starts."""
    g = Path(game_dir)
    newest, newest_name = None, ""
    for name in SETUP_FILES:
        stamp = _mtime(g / name)
        if stamp is not None and (newest is None or stamp > newest):
            newest, newest_name = stamp, name
    logs = {}
    for name in STARTUP_LOGS:
        stamp = _mtime(g / name)
        logs[name] = {"present": stamp is not None, "mtime": stamp,
                      "stale": stamp is not None and newest is not None and stamp < newest,
                      "head": _head(g / name, 3) if stamp is not None else None}
    present = all(item["present"] for item in logs.values())
    stale = [name for name, item in logs.items() if item["stale"]]
    return {"logs": logs, "present": present, "stale": stale, "fresh": present and not stale,
            "newest_setup_file": newest_name, "newest_setup_mtime": newest,
            "missing": [name for name, item in logs.items() if not item["present"]],
            "code_patch": code_patch_log(g)}


def startup_log_lines(info: dict) -> list[str]:
    lines = []
    mge = info["logs"]["mgeXE.log"]
    if mge["present"] and mge["head"]:
        lines.append(f"mgeXE.log: {mge['head'][0] or '(empty)'}" + (" (older than the current setup)"
                                                                    if mge["stale"] else ""))
    mwse = info["logs"]["MWSE.log"]
    if mwse["present"] and mwse["head"]:
        lines.append(f"MWSE.log: {mwse['head'][0] or '(empty)'}" + (" (older than the current setup)"
                                                                   if mwse["stale"] else ""))
    if info["code_patch"].get("mge_version"):
        lines.append(f"MWSE.log: Found MGE XE {info['code_patch']['mge_version']}")
    lines.append(code_patch_log_line(info["code_patch"]))
    return lines


def summary_lines(found: dict) -> list[str]:
    lines = []
    mcp = found.get("mcp", {})
    if mcp.get("present"):
        bits = [f"version {mcp['version']}" if mcp.get("version") else "version not detectable"]
        if mcp.get("describe_file"):
            bits.append(f"{mcp['describe_file']} first entry \"{mcp.get('first_category')}\"")
        bits.append("Skunk Works beta " + ("installed" if mcp.get("skunk_works_beta") else "NOT installed"))
        lines.append(f"{PAGES['mcp']['name']}: " + ", ".join(bits))
    else:
        lines.append(f"{PAGES['mcp']['name']}: NOT detected (missing {', '.join(mcp.get('missing', []))})")
    mge = found.get("mge_xe", {})
    if mge.get("present"):
        build = ("Greatness7 build confirmed by " + mge["g7_settings"] if mge.get("g7") else
                 "mgeXE.toml not found, so not confirmed as the Greatness7 build")
        lines.append(f"MGE XE: installed (version {mge.get('version') or 'not detectable'}, {build})")
    else:
        lines.append(f"MGE XE: NOT detected (missing {', '.join(mge.get('missing', []))})")
    mwse = found.get("mwse", {})
    if mwse.get("present"):
        bits = [f"version {mwse['version']}" if mwse.get("version") else "version not detectable",
                f"build {mwse['build'][:12]}" if mwse.get("build") else "no mwse-version.txt",
                "updater MWSE-Update.exe present" if mwse.get("updater") else "MWSE-Update.exe missing"]
        lines.append("MWSE: installed (" + ", ".join(bits) + ")")
    else:
        lines.append("MWSE: NOT detected (missing MWSE.dll)")
    return lines


def open_download_page(key: str) -> str:
    """Open the official page for `key`; never downloads anything. Returns "embedded" or "system"."""
    url = PAGES[key]["page"]
    try:
        import mwnexus_browser
        mwnexus_browser.open_page(url)
        return "embedded"
    except Exception:
        import webbrowser
        webbrowser.open(url)
        return "system"
