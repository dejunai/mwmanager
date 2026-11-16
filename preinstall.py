"""Preflight: validate, install and configure MWManager's prerequisites before mod deployment.

The whole preflight, in order (everything here only reads the game folder):
- Morrowind.exe exists. It is never hashed: every store ships a different one and the Code Patch rewrites it.
- Morrowind.esm, Tribunal.esm, Bloodmoon.esm and their three BSAs exist and match the built-in SHA-256 values,
  which are the same on every store. A mismatch is a warning the user can continue past.
- Morrowind.ini exists. Its contents are not checked.
- The Morrowind Code Patch with MCP Beta: Skunk Works: mcpatch\\describe.json (description.json is accepted too)
  must list "Beta" as its first category.
- MGE XE, any build. The Greatness7 build is recommended; it is confirmed by its mgeXE.toml settings file.
- MWSE.dll. Its build (mwse-version.txt, written by MWSE-Update.exe) is compared with the latest MWSE build when
  GitHub can be reached, and the user is offered MWSE-Update.exe when it is behind. It only runs on their click.
- Missing prerequisites are processed in the guided order MWSE, Code Patch, Skunk Works, then MGE XE. Existing
  archives or fresh downloads are kept under MWManager\\pre-reqs and portable packages are safely extracted.
- Informational: the Code Patch features the game logged at its last start (mgeXE.log line 2, else the top of
  MWSE.log), after all prerequisites have been configured.
- First launch: mgeXE.log and MWSE.log must exist and be newer than the installed Code Patch, MGE XE and MWSE,
  which proves the setup starts. If not, the user is asked to launch Morrowind once to the main menu and quit;
  a Launch Morrowind button starts the selected Morrowind.exe on their click (MWManager never waits on or
  closes the game), Re-check re-reads the logs, and Skip continues with a warning.
Nexus downloads remain interactive so the user can finish any security check. After a passing preflight, the
first run offers to record the install's starting state for rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import time
import shutil
import tempfile
import types
import urllib.parse
import urllib.request
from pathlib import Path

import mwmanager_prereqs
from core_addons import _quiet_subprocess_options, _seven_zip

CORE_FILES = ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm",
              "Morrowind.bsa", "Tribunal.bsa", "Bloodmoon.bsa")

GENERATED_NON_MOD_FILES = {
    "mwedit/customfunctions.dat",
    "mwse-update.exe.tmp",
    "newtonsoft.json.dll.tmp",
    "warnings.txt",
}

SEVEN_ZIP_DOWNLOAD_PAGE = "https://www.7-zip.org/download.html"
IS_WINDOWS = os.name == "nt"


def _seven_zip_installer_url(opener=urllib.request.urlopen) -> str:
    """Return the current official Windows installer URL advertised by 7-zip.org."""
    machine = platform.machine().casefold()
    suffix = "arm64" if "arm" in machine else "x64" if machine not in {"x86", "i386", "i686"} else ""
    with opener(SEVEN_ZIP_DOWNLOAD_PAGE, timeout=30) as response:
        page = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
    links = re.findall(r'href=["\']([^"\']+7z\d+(?:-(?:x64|arm64))?\.exe)["\']', page, re.I)
    for link in links:
        url = urllib.parse.urljoin(SEVEN_ZIP_DOWNLOAD_PAGE, link)
        name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1].casefold()
        if url.startswith("https://github.com/ip7z/7zip/releases/download/") and (
                (suffix and name.endswith(f"-{suffix}.exe")) or (not suffix and re.fullmatch(r"7z\d+\.exe", name))):
            return url
    raise RuntimeError("the official 7-Zip page did not list an installer for this Windows architecture")


def ensure_archive_tool(manager_dir: Path, progress, *, opener=urllib.request.urlopen,
                        runner=subprocess.run) -> bool:
    """Ensure full 7-Zip is available for ZIP, 7z and RAR packages."""
    manager_dir = Path(manager_dir)
    found = _seven_zip(manager_dir)
    if found:
        progress(f"Preflight: archive tool found at {found} (ZIP, 7z and RAR).")
        return True
    if not IS_WINDOWS:
        progress("Preflight: full 7-Zip is required for ZIP, 7z and RAR archives; install 7z and retry.")
        return False
    destination = manager_dir / "tools" / "7zip"
    if not progress.ask_yes_no(
            "Install 7-Zip archive support",
            "MWManager needs full 7-Zip to read ZIP, 7z and RAR mod archives. No suitable 7z.exe was found.\n\n"
            "Download the current official build and install it privately under "
            f"{destination}?\n\nSource: {SEVEN_ZIP_DOWNLOAD_PAGE}"):
        return False
    installer = manager_dir / "tools" / "7zip-installer.exe"
    partial = installer.with_suffix(".exe.part")
    try:
        url = _seven_zip_installer_url(opener)
        with opener(url, timeout=120) as response:
            payload = response.read(16 * 1024 * 1024 + 1)
        if len(payload) > 16 * 1024 * 1024 or not payload.startswith(b"MZ"):
            raise RuntimeError("download was not a valid 7-Zip Windows installer")
        installer.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(payload)
        os.replace(partial, installer)
        destination.mkdir(parents=True, exist_ok=True)
        result = runner([str(installer), "/S", f"/D={destination}"], check=False,
                        timeout=300, **_quiet_subprocess_options())
        if result.returncode or not (destination / "7z.exe").is_file():
            raise RuntimeError(f"7-Zip installer exited with code {result.returncode}")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        partial.unlink(missing_ok=True)
        progress(f"Preflight: 7-Zip could not be installed: {exc}")
        return False
    progress(f"Preflight: installed 7-Zip archive support in {destination}.")
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _find(folder: Path, name: str) -> Path | None:
    try:
        return next((p for p in folder.iterdir() if p.is_file() and p.name.casefold() == name.casefold()), None)
    except OSError:
        return None


OFFICIAL_ADDON_DOC_STEMS = (
    "adamantium armor", "area effect arrows", "bitter coast sounds", "entertainers",
    "helm of tohan", "lefemm armor", "master index", "siege at firemoth",
)
OFFICIAL_ADDON_PLUGINS = {name.casefold() for name in (
    "Adamantiumarmor.esp", "AreaEffectArrows.esp", "bcsounds.esp", "entertainers.esp",
    "EBQ_Artifact.esp", "LeFemmArmor.esp", "master_index.esp", "Siege at Firemoth.esp",
)}


def install_store(game_dir: Path) -> str:
    game_dir = Path(game_dir)
    folded = str(game_dir).replace("/", "\\").casefold()
    if list(game_dir.glob("goggame-*.hashdb")) or list(game_dir.glob("goggame-*.info")):
        return "GOG"
    if (game_dir / "steam_api.dll").is_file() or "\\steamapps\\common\\" in folded:
        return "Steam"
    if (game_dir / ".egstore").exists():
        return "Epic"
    return "disc or other"


def recovery_text(game_dir: Path) -> str:
    store = install_store(game_dir)
    if store == "Steam":
        action = "Use Steam > Morrowind > Properties > Installed Files > Verify integrity of game files."
    elif store == "GOG":
        action = "Use GOG Galaxy > Morrowind > Manage installation > Verify / Repair."
    elif store == "Epic":
        action = "Use Epic Games Launcher > Library > Morrowind > Manage > Verify."
    else:
        action = "Restore the files from the original GOTY media or reinstall Morrowind."
    return f"Detected install: {store}. {action} If verification cannot restore it, perform a clean reinstall."


def move_official_addon_docs(game_dir: Path, manager_dir: Path) -> list[str]:
    """Move only the GOTY official-add-on documents into Data Files/docs/Official Add-Ons."""
    import mwmanager_journal
    data_dir = Path(game_dir) / "Data Files"
    destination = data_dir / "docs" / "Official Add-Ons"
    journal = mwmanager_journal.Journal(Path(manager_dir) / "journal")
    run = None
    moved = []
    try:
        candidates = list(data_dir.iterdir())
    except OSError:
        return moved
    for source in candidates:
        if not source.is_file() or source.suffix.casefold() not in {".txt", ".rtf", ".htm", ".html", ".pdf"}:
            continue
        stem = source.stem.casefold()
        if not any(name in stem for name in OFFICIAL_ADDON_DOC_STEMS):
            continue
        target = destination / source.name
        if target.exists():
            if _sha256(target) == _sha256(source):
                moved.append(f"Official add-on document already organized; duplicate left in place: {source.name}")
            else:
                moved.append(f"Official add-on document conflict left untouched: {source.name}")
            continue
        if run is None:
            run = journal.start_run("preinstall.py", {"operation": "organize official add-on documents"})
        journal.move_original(game_dir, source, target, run["run"], tag="official-add-on-document")
        moved.append(f"Moved official add-on document: {source.name}")
    if run is not None:
        journal.finish_run(run)
    return moved


def detected_mod_files(game_dir: Path, manager_dir: Path | None = None) -> tuple[list[Path], bool]:
    """Return safely identifiable mod files and whether a GOG store manifest made the scan complete."""
    game_dir = Path(game_dir).resolve()
    import mwmanager_journal
    managed = {}
    if manager_dir:
        entries, problems = mwmanager_journal.Journal(Path(manager_dir) / "journal").entries()
        if not problems:
            for entry in entries:
                managed[mwmanager_journal.key(entry["path"])] = entry.get("after_sha256")

    def is_managed(path: Path, rel: str) -> bool:
        expected = managed.get(mwmanager_journal.key(rel))
        return bool(expected) and _sha256(path) == expected

    hashdb = mwmanager_journal.find_hashdb(game_dir)
    if hashdb:
        try:
            original = mwmanager_journal.read_hashdb(hashdb)
        except (OSError, ValueError):
            original = {}
        if original:
            extras = []
            for path in mwmanager_journal.walk_files(game_dir):
                rel = mwmanager_journal.rel_of(game_dir, path)
                if mwmanager_journal.key(rel) in GENERATED_NON_MOD_FILES:
                    continue
                if is_managed(path, rel):
                    continue
                if mwmanager_journal.key(rel).startswith("data files/docs/official add-ons/"):
                    continue
                if mwmanager_journal.key(rel) in original:
                    continue
                if mwmanager_journal.classify(rel) in {"prereq:mcp", "prereq:mge_xe", "prereq:mwse",
                                                       "user-data", "store-meta", "game-exe"}:
                    continue
                extras.append(path)
            return extras, True
    data = game_dir / "Data Files"
    core = {name.casefold() for name in CORE_FILES}
    plugins = [path for path in data.glob("*") if path.is_file() and path.suffix.casefold() in {".esm", ".esp", ".omwaddon"}
               and path.name.casefold() not in core and path.name.casefold() not in OFFICIAL_ADDON_PLUGINS]
    plugins = [path for path in plugins if not is_managed(path, mwmanager_journal.rel_of(game_dir, path))]
    return plugins, False


def quarantine_mod_files(game_dir: Path, manager_dir: Path, files: list[Path]) -> Path:
    """Recoverably purge known mod files by moving them outside the game install."""
    game_dir = Path(game_dir).resolve()
    root = Path(manager_dir) / "quarantine" / time.strftime("%Y%m%d-%H%M%S")
    manifest = []
    for source in files:
        relative = source.resolve().relative_to(game_dir)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        manifest.append(relative.as_posix())
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps({"game_dir": str(game_dir), "files": manifest}, indent=2) + "\n",
                                         encoding="utf-8")
    return root


def inspect_install(game_dir: Path, manager_dir: Path | None = None, progress=None, *,
                    check_latest=mwmanager_prereqs.latest_mwse_build) -> dict:
    """Read-only. Returns blockers (missing requirements), hash_mismatches, recommendations and status lines."""
    game_dir = Path(game_dir).resolve()
    data_dir = game_dir / "Data Files"
    report = {"game_dir": str(game_dir), "blockers": [], "missing_keys": [], "hash_mismatches": [],
              "recommendations": [], "status": [], "mwse": {}}
    if _find(game_dir, "Morrowind.exe") is None:
        report["blockers"].append(f"Morrowind.exe was not found in {game_dir}")
    else:
        report["status"].append("Morrowind.exe found (not hashed: it differs by store and the Code Patch "
                                "rewrites it)")
    if _find(game_dir, "Morrowind.ini") is None:
        report["blockers"].append(f"Morrowind.ini was not found in {game_dir}; start the game's launcher once "
                                  "so it is created")
    else:
        report["status"].append("Morrowind.ini found (contents are not checked)")
    if not data_dir.is_dir():
        report["blockers"].append(f"Data Files folder was not found in {game_dir}")
    else:
        for name in CORE_FILES:
            path = _find(data_dir, name)
            if path is None:
                report["blockers"].append(f"Missing core game file: Data Files\\{name}")
                continue
            if progress:
                progress(f"Preflight: hashing {name}…")
            actual = _sha256(path)
            known = mwmanager_prereqs.CORE_FILE_HASHES[name]
            if actual.casefold() in known:
                report["status"].append(f"SHA-256 matches the original: {name}")
            else:
                report["hash_mismatches"].append((name, known[0], actual))

    found = mwmanager_prereqs.detect(game_dir)
    report["prerequisites"] = found
    report["status"].extend(line for line in mwmanager_prereqs.summary_lines(found) if "NOT detected" not in line)
    mcp, mge, mwse = found["mcp"], found["mge_xe"], found["mwse"]
    if not mcp["present"]:
        report["blockers"].append("The Morrowind Code Patch was not found, and MCP Beta: Skunk Works is "
                                  "required with it")
    elif mcp.get("error"):
        report["blockers"].append(f"{mcp['describe_file']} could not be read ({mcp['error']}); reinstall MCP Beta: "
                                  "Skunk Works")
    elif not mcp["skunk_works_beta"]:
        detail = (f"the first entry of {mcp['describe_file']} is \"{mcp.get('first_category')}\", not \"Beta\""
                  if mcp.get("describe_file") else "mcpatch\\describe.json was not found")
        report["blockers"].append(f"MCP Beta: Skunk Works is required but not installed ({detail})")
    if not mge["present"]:
        report["blockers"].append("MGE XE was not found (any build works; the Greatness7 build is recommended)")
    elif not mge["g7"]:
        if mge.get("g7_files_without_settings"):
            text = ("MGE XE is installed but mgeXE.toml does not exist yet. Greatness7 files are present (mge3\\"
                    + mge["g7_files_without_settings"][0] + "), so run MGEXEgui.exe once to create its settings.")
        else:
            text = ("MGE XE is installed but no mgeXE.toml was found, so this is not the Greatness7 build. Any "
                    "build works; Greatness7 is recommended.")
        report["recommendations"].append(("mge_g7", text))
    if not mwse["present"]:
        report["blockers"].append("MWSE.dll was not found")
    else:
        latest, why = check_latest() if check_latest else (None, "not checked")
        state = mwmanager_prereqs.mwse_status(mwse, latest)
        report["mwse"] = {"installed": mwse.get("build"), "latest": latest, "state": state, "why": why,
                          "updater": mwse.get("updater")}
        if state == "current":
            report["status"].append(f"MWSE is the latest build ({latest[:12]})")
        elif state == "outdated":
            have = mwse["build"][:12] if mwse.get("build") else "no build recorded (no mwse-version.txt)"
            report["status"].append(f"MWSE is not the latest build: installed {have}, latest {latest[:12]}")
        else:
            report["status"].append(f"MWSE latest build unknown: {why}")
        if not mwse.get("updater"):
            report["recommendations"].append(("mwse", "MWSE-Update.exe was not found beside MWSE.dll. Reinstall "
                                              "MWSE so its updater is present; MWManager always recommends the "
                                              "latest MWSE."))
    report["code_patch_log"] = mwmanager_prereqs.code_patch_log(game_dir)        # informational only
    report["status"].append(mwmanager_prereqs.code_patch_log_line(report["code_patch_log"]))
    report["missing_keys"] = [k for k in mwmanager_prereqs.missing_keys(found)]
    return report


def _choose(progress, title, message, choices, cancel):
    if hasattr(progress, "ask_choice"):
        return progress.ask_choice(title, message, choices, cancel)
    return choices[0][1] if progress.ask_yes_no(title, message) else cancel


def run_mwse_updater(game_dir: Path, latest: str | None, progress, runner=subprocess.run) -> bool:
    """Start MWSE-Update.exe from the game folder (only ever after the user clicked to run it)."""
    updater = Path(game_dir) / "MWSE-Update.exe"
    progress(f"Running {updater} (the MWSE updater)…")
    try:
        result = runner([str(updater)], cwd=str(game_dir), check=False)
    except OSError as exc:
        hint = (" Windows says it needs administrator rights; run MWSE-Update.exe yourself as administrator."
                if getattr(exc, "winerror", None) == 740 else "")
        progress(f"Preflight: MWSE-Update.exe could not be started: {exc}.{hint}")
        return False
    if result.returncode:
        progress(f"Preflight: MWSE-Update.exe exited with code {result.returncode}.")
        return False
    build = mwmanager_prereqs.detect(game_dir)["mwse"].get("build")
    if latest and build and latest.startswith(build):
        progress(f"Preflight: MWSE is now the latest build ({latest[:12]}).")
        return True
    progress("Preflight: MWSE-Update.exe finished, but mwse-version.txt does not show the latest build yet.")
    return False


def launch_morrowind(game_dir: Path) -> str:
    """Start the selected install's Morrowind.exe and return at once (only ever after the user's click).

    MWManager never waits on, watches or closes the game; the user quits it from the main menu."""
    exe = _find(Path(game_dir), "Morrowind.exe")
    if exe is None:
        return f"Morrowind.exe was not found in {game_dir}."
    try:
        subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 740:
            return ("Windows says Morrowind.exe needs administrator rights. Start it yourself from the game "
                    "folder, quit at the main menu, then choose Re-check.")
        return f"Morrowind.exe could not be started: {exc}"
    return ""


FIRST_LAUNCH_TEXT = (
    "Confirm your setup works before MWManager does anything else: launch Morrowind once, wait for the main "
    "menu, then quit the game (MWManager does not close it for you). That first start also writes mgeXE.log "
    "and MWSE.log, which record the MGE XE and MWSE versions and the Code Patch features the game started with.")


def confirm_first_launch(game_dir: Path, progress, overrides: list, *, launcher=None) -> bool:
    """Require fresh startup logs (or an explicit skip) before the baseline and any deploy."""
    launcher = launcher or launch_morrowind
    info = mwmanager_prereqs.startup_logs(game_dir)
    if info["fresh"]:
        for line in mwmanager_prereqs.startup_log_lines(info):
            progress("Preflight: " + line)
        return True
    if not hasattr(progress, "ask_choice"):
        progress("Preflight: the game has not been started with this setup yet (no fresh mgeXE.log/MWSE.log); "
                 "launch Morrowind once to the main menu and quit to confirm it works.")
        overrides.append("first-launch-not-confirmed")
        return True
    note = ""
    while True:
        if info["missing"]:
            state = "Not found yet: " + ", ".join(info["missing"]) + "."
        else:
            state = (", ".join(info["stale"]) + " predate" + ("s" if len(info["stale"]) == 1 else "")
                     + f" the current setup ({info['newest_setup_file']} is newer), so they describe an earlier "
                     "setup.")
        choice = progress.ask_choice(
            "Confirm your setup works",
            FIRST_LAUNCH_TEXT + "\n\n" + state + (f"\n\n{note}" if note else "") +
            "\n\nLaunch Morrowind starts the selected install's Morrowind.exe. After you quit at the main menu, "
            "choose Re-check.",
            [("Launch Morrowind", "launch"), ("Re-check", "recheck"), ("Skip", "skip"), ("Stop", "stop")], "stop")
        if choice == "launch":
            problem = launcher(game_dir)
            note = problem or "Morrowind was started. Quit at the main menu, then choose Re-check."
            progress("Preflight: " + note)
            continue
        if choice == "recheck":
            info = mwmanager_prereqs.startup_logs(game_dir)
            for line in mwmanager_prereqs.startup_log_lines(info):
                progress("Preflight: " + line)
            if info["fresh"]:
                progress("Preflight: startup logs found; the setup started successfully.")
                return True
            note = "Re-checked: the logs are still missing or older than the current setup."
            continue
        if choice == "skip":
            if progress.ask_yes_no(
                    "Skip the first launch?",
                    "Without a first launch MWManager cannot confirm that the Code Patch, MGE XE and MWSE start "
                    "correctly, and problems may only show up after mods are installed. Skip anyway?"):
                progress("Preflight: first launch skipped by the user; the setup is not confirmed.")
                overrides.append("first-launch-skipped")
                return True
            continue
        progress("Preflight stopped before the first launch was confirmed.")
        return False


def _record_preflight(manager_dir: Path, game_dir: Path, overrides: list[str], statuses: list[str]) -> None:
    path = manager_dir / "outputs" / "mwmanager_preinstall_audit.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            history = {"version": 1, "runs": []}
        if not isinstance(history, dict) or not isinstance(history.get("runs"), list):
            history = {"version": 1, "runs": []}
        history["runs"].append({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "game_dir": str(game_dir), "overrides": overrides, "status": statuses})
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def _offer_baseline(game_dir: Path, manager_dir: Path, progress) -> bool:
    """After a passing preflight: snapshot the install as the user starts from it, so --rollback can return to it."""
    import mwmanager_journal
    journal = mwmanager_journal.Journal(manager_dir / "journal")
    if journal.has_baseline():
        matches, reason = journal.baseline_matches_install(game_dir)
        if matches:
            return True
        if not progress.ask_yes_no(
                "Replace the obsolete rollback baseline",
                "The existing rollback journal does not belong to the selected installation in its current "
                f"state: {reason}. This normally means the game was repaired or reinstalled.\n\n"
                "Archive the complete old journal under journal\\history and record a fresh baseline now? "
                "No old evidence will be deleted."):
            return False
        archived = journal.archive_current(reason)
        progress(f"Preflight: archived the obsolete rollback journal at {archived}.")
    capture = progress.ask_yes_no(
        "Record the starting state of this install",
        "MWManager has no snapshot of this Morrowind install yet. Recording one now lets a rollback return the "
        "game folder to exactly this state later, whether it is a fresh install or a playthrough that already "
        "has mods. Nothing in the game folder is changed; this reads every file once and can take a few "
        "minutes.\n\nRecord it now? (No stops here; deploying without a snapshot is not offered.)")
    if not capture:
        return False
    progress("Recording the starting state of the install (reading every file)…")
    record = journal.capture_baseline(game_dir)
    progress(f"Starting state recorded: {len(record['files'])} files, {journal.baseline_file}")
    for warning in record["warnings"]:
        progress("Preflight: " + warning)
    return True


def _install_mwse_lua_suite(game_dir: Path, progress) -> bool:
    """Install MWManager's runtime utilities after the clean baseline, before the first full scan."""
    import mwmanager
    progress("Preflight: installing MWManager's MWSE Lua utilities before the initial --full cycle…")
    if mwmanager.install_mwse_lua_guards(
            types.SimpleNamespace(data_dir=str(game_dir / "Data Files"), dry_run=False)):
        progress("Preflight: the MWSE Lua utilities could not be installed; initial --full was not started.")
        return False
    progress("Preflight: MWManager's MWSE Lua utilities are installed.")
    return True


MCP_PATCH_GUIDANCE = """MWManager recommends these Morrowind Code Patch selections:

- Beta: enable every patch.
- Game mechanics: enable every patch.
- Visuals: enable every patch. “Over the shoulder 3rd person camera” is a personal-preference recommendation.
- Mod specific: enable every patch except “NPC AI casts zero cost powers”; it makes the game more difficult and is not recommended for a first Morrowind experience.
- Interface changes: enable every patch except “Don't loot on dispose”. Leave “Level-up skills tooltip” off if you plan to use MULE, an auto-leveler.
- International: choose patches for the language and regional behavior you need.
- Bugs: strongly recommended—enable every patch.

When finished, click “Apply chosen patches” in the MCP interface."""

MGE_GUIDANCE = """MWManager will open MGEXEgui once so the Greatness7 settings TOML can be generated.

Recommended starting settings:

First tab:
- Leave VSync off.
- Enable shaders on a current system with a current GPU.
- For an above-average system and GPU, open Shader Setup, select “Very High”, set “Depth of Field” to “On”, and save.
- Set the frame limiter to 300 so Morrowind's internal limiter does not take over.

In Game tab:
- Enable “Pause rendering in menus”.
- Enable “Crosshair autohide”.
- Enable “Allow yes to all”.
- Enable “Allow screenshots”.
- Recommended Light Attenuation: Constant 0.382; Linear 0 when using per-pixel lighting; Quadratic 2.619.

Review the remaining settings for your system, save, and close MGEXEgui when finished."""


def _prereq_ready(key: str, game_dir: Path) -> bool:
    found = mwmanager_prereqs.detect(game_dir)
    if key == "mcp_beta":
        return bool(found["mcp"].get("skunk_works_beta"))
    return bool(found[{"mcp": "mcp", "mwse": "mwse", "mge_xe": "mge_xe"}[key]].get("present"))


def _run_prereq_program(path: Path, progress, runner=subprocess.run, reference: str | None = None) -> bool:
    progress(f"Launching {path.name} from {path.parent}…")
    if reference and hasattr(progress, "run_program_with_reference"):
        code, error = progress.run_program_with_reference(path, f"{path.name} reference", reference)
        if error:
            progress(f"Preflight: {path.name} could not be started: {error}")
            return False
        if code:
            progress(f"Preflight: {path.name} exited with code {code}.")
            return False
        return True
    try:
        result = runner([str(path)], cwd=str(path.parent), check=False)
    except OSError as exc:
        progress(f"Preflight: {path.name} could not be started: {exc}")
        return False
    if result.returncode:
        progress(f"Preflight: {path.name} exited with code {result.returncode}.")
        return False
    return True


def _payload_root(extracted: Path, key: str) -> Path | None:
    if key == "mcp_beta":
        describe = next((path for name in mwmanager_prereqs.MCP_DESCRIBE_NAMES
                         for path in extracted.rglob(name)
                         if path.parent.name.casefold() == "mcpatch"), None)
        return describe.parent.parent if describe else None
    expected = {"mwse": "MWSE-Update.exe", "mcp": "Morrowind Code Patch.exe",
                "mge_xe": "MGEXEgui.exe"}[key]
    found = next((path for path in extracted.rglob("*")
                  if path.is_file() and path.name.casefold() == expected.casefold()), None)
    return found.parent if found else None


def install_prerequisite_archive(key: str, archive: Path, game_dir: Path, manager_dir: Path,
                                 progress, runner=subprocess.run) -> bool:
    """Keep one prerequisite archive, safely extract portable packages, and install or launch it."""
    archive = Path(archive).resolve()
    store = Path(manager_dir) / "pre-reqs" / key
    store.mkdir(parents=True, exist_ok=True)
    kept = store / archive.name
    if archive != kept.resolve():
        if kept.exists() and _sha256(kept) != _sha256(archive):
            kept = store / f"{archive.stem}-{_sha256(archive)[:10]}{archive.suffix}"
        if not kept.exists():
            shutil.copy2(archive, kept)
    else:
        kept = archive
    progress(f"Preflight: using {kept}.")
    if kept.suffix.casefold() == ".exe":
        return _run_prereq_program(kept, progress, runner)

    from plugin_install import extract_archive
    with tempfile.TemporaryDirectory(prefix=f"{key}-", dir=store) as temporary:
        extracted = Path(temporary) / "files"
        extract_archive({"archive": str(kept)}, extracted, Path(manager_dir))
        payload = _payload_root(extracted, key)
        if payload is None:
            installer = next((path for path in extracted.rglob("*.exe")
                              if any(word in path.name.casefold() for word in ("setup", "install"))), None)
            if installer:
                return _run_prereq_program(installer, progress, runner)
            raise RuntimeError(f"{kept.name} does not contain the expected {mwmanager_prereqs.PAGES[key]['name']} files")
        for source in payload.iterdir():
            destination = Path(game_dir) / source.name
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True, copy_function=shutil.copy2)
            else:
                shutil.copy2(source, destination)
    progress(f"Preflight: installed the portable {mwmanager_prereqs.PAGES[key]['name']} files into {game_dir}.")
    return True


def _post_prerequisite(key: str, game_dir: Path, progress, latest: str | None,
                       runner=subprocess.run, state=None) -> bool:
    state = state if state is not None else {}
    if key == "mwse":
        state["mwse_update_offered"] = True
        updater = Path(game_dir) / "MWSE-Update.exe"
        if updater.is_file() and progress.ask_yes_no(
                "Update MWSE now (highly recommended)",
                "MWSE is installed. Running MWSE-Update.exe now is highly recommended so the first launch uses "
                "the current MWSE build. Close Morrowind before continuing.\n\nRun the updater now?"):
            run_mwse_updater(game_dir, latest, progress, runner=runner)
        return True
    if key == "mcp_beta":
        patcher = Path(game_dir) / "Morrowind Code Patch.exe"
        if patcher.is_file() and progress.ask_yes_no(
                "Configure and apply Morrowind Code Patch", MCP_PATCH_GUIDANCE + "\n\nOpen the MCP interface now?"):
            return _run_prereq_program(patcher, progress, runner, MCP_PATCH_GUIDANCE)
        return True
    if key == "mge_xe":
        exe = next((Path(game_dir) / name for name in mwmanager_prereqs.MGE_EXECUTABLES
                    if (Path(game_dir) / name).is_file()), None)
        if exe and progress.ask_yes_no("Configure MGE XE", MGE_GUIDANCE + "\n\nOpen MGEXEgui now?"):
            return _run_prereq_program(exe, progress, runner, MGE_GUIDANCE)
    return True


def _offer_missing(report: dict, progress, game_dir: Path, manager_dir: Path, archive_dir=None,
                   latest: str | None = None, runner=subprocess.run, state=None) -> bool:
    keys = [k for k in mwmanager_prereqs.INSTALL_ORDER if k in report["missing_keys"]]
    pages = "\n".join(f"- {mwmanager_prereqs.PAGES[k]['name']}: {mwmanager_prereqs.PAGES[k]['page']}\n"
                      f"  {mwmanager_prereqs.PAGES[k]['note']}" for k in keys)
    message = ("MWSE, the Morrowind Code Patch with MCP Beta: Skunk Works, and MGE XE are prerequisites. "
               "MWManager processes them in this order: MWSE, the Code Patch, Skunk Works, then MGE XE. "
               "For missing Morrowind files, use your store's "
               "repair or verify action.\n\n"
               + "\n".join("- " + item for item in report["blockers"]) + "\n\n" +
               recovery_text(Path(report.get("game_dir", "."))))
    if not keys:
        progress.ask_yes_no("Preflight stopped", message + "\n\nFix these, then run preflight again.")
        return False
    first = keys[0]
    prereq_root = Path(manager_dir) / "pre-reqs"
    cached = mwmanager_prereqs.prerequisite_archives(first, prereq_root)
    cache_found = bool(cached)
    if cached:
        cache_status, nexus_version = mwmanager_prereqs.cached_archive_status(first, cached[0])
        if cache_status == "outdated":
            progress(f"Preflight: Nexus has a newer {mwmanager_prereqs.PAGES[first]['name']} "
                     f"({nexus_version}); ignoring cached {cached[0].name}.")
            cached = []
        elif cache_status == "unknown":
            progress(f"Preflight: could not confirm whether cached {cached[0].name} is current; keeping it available.")
    archived = [] if cache_found else mwmanager_prereqs.prerequisite_archives(first, archive_dir)
    choices = []
    if cached:
        choices.append((f"Use MWManager cached version: {cached[0].name}", "cache"))
    if archived:
        choices.append((f"Use mod-manager archive: {archived[0].name}", "archive"))
    choices.append((f"Download fresh {mwmanager_prereqs.PAGES[first]['name']}", first))
    if first == "mge_xe":
        choices[-1] = ("Download fresh Greatness7 build", "mge_g7")
    choices.append(("Not now", "none"))
    choice = _choose(progress, "Required prerequisites missing",
                     message + "\n\nOfficial pages, in install order:\n" + pages +
                     "\n\nUse a matching archive already found, or download a fresh copy. Nexus downloads stay in "
                     "MWManager's visible browser so you can complete any security check.",
                     choices, "none")
    if not choice or choice == "none":
        return False
    if choice in {"cache", "archive"}:
        selected = cached[0] if choice == "cache" else archived[0]
        if not install_prerequisite_archive(first, selected, game_dir, manager_dir, progress, runner=runner):
            return False
        return _post_prerequisite(first, game_dir, progress, latest, runner=runner, state=state)
    download_root = prereq_root / first
    download_root.mkdir(parents=True, exist_ok=True)
    download_dir = Path(tempfile.mkdtemp(prefix="download-", dir=download_root))
    where = mwmanager_prereqs.open_download_page(choice, download_dir)
    progress(f"Opened {mwmanager_prereqs.PAGES[choice]['page']} in the "
             + ("MWManager browser." if where == "embedded" else "system browser."))
    ready = progress.ask_yes_no(
        "Download the prerequisite",
        "Complete any security check in the browser and save the downloaded installer/archive here:\n\n"
        f"{download_dir}\n\nClose the browser after the download finishes. MWManager will then process and launch it. Continue now?")
    if ready and where == "embedded":
        try:
            import mwnexus_browser
            mwnexus_browser.close_browser()
        except Exception:
            pass
    if not ready:
        return False
    if _prereq_ready(first, game_dir):
        return _post_prerequisite(first, game_dir, progress, latest, runner=runner, state=state)
    downloaded = mwmanager_prereqs.prerequisite_archives(first, download_dir)
    if not downloaded:
        progress(f"Preflight: no matching {mwmanager_prereqs.PAGES[first]['name']} archive was found in {download_dir}.")
        return False
    if not install_prerequisite_archive(first, downloaded[0], game_dir, manager_dir, progress, runner=runner):
        return False
    return _post_prerequisite(first, game_dir, progress, latest, runner=runner, state=state)


def _prepare_first_run(game_dir: Path, manager_dir: Path, progress, archive_source: str | None) -> bool:
    """Keep pre-existing mods out of the trusted baseline, then organize the GOTY add-on documents."""
    while True:
        files, complete = detected_mod_files(game_dir, manager_dir)
        if not files:
            break
        names = "\n".join(f"- {path.relative_to(game_dir)}" for path in files[:20])
        if len(files) > 20:
            names += f"\n- …and {len(files) - 20} more"
        choices = []
        if archive_source == "vortex":
            choices.append(("Open Vortex to purge", "vortex"))
        if complete:
            choices.append(("Quarantine mod files with MWManager", "quarantine"))
        choices += [("Keep existing mods", "keep"), ("Stop", "stop")]
        detail = ("The GOG file manifest identified every non-store file shown below. MWManager can move them "
                  "to a recoverable quarantine outside the game folder."
                  if complete else
                  "This install has no complete store file manifest, so MWManager will not guess which assets "
                  "are safe to remove. Use the store's verify/reinstall feature for a truly clean install.")
        choice = _choose(progress, "Existing mods detected",
                         f"MWManager is optimized for a fresh install. {detail}\n\n{names}", choices, "stop")
        if choice == "keep":
            progress("Preflight: existing mods kept by the user; the baseline will include them.")
            return True
        if choice == "quarantine":
            if not progress.ask_yes_no(
                    "Quarantine detected mod files?",
                    f"Move {len(files)} file(s) out of Morrowind into MWManager's quarantine? This is the "
                    "built-in purge and is recoverable from its manifest."):
                continue
            root = quarantine_mod_files(game_dir, manager_dir, files)
            progress(f"Preflight: quarantined {len(files)} mod file(s) at {root}.")
            continue
        if choice == "vortex":
            try:
                import mwmanager_gui
                launched = mwmanager_gui.vortex_install_path()
                if launched:
                    subprocess.Popen([str(launched)], cwd=str(launched.parent))
            except OSError:
                launched = None
            if not launched:
                progress("Preflight: Vortex could not be started; open it manually and purge Morrowind.")
            if progress.ask_yes_no("Re-check after Vortex purge",
                                   "In Vortex, purge the deployed Morrowind mods, then return here. Re-check now?"):
                continue
        return False
    return True


def run_preinstall(game_dir: Path, manager_dir: Path, progress, *,
                   check_latest=mwmanager_prereqs.latest_mwse_build, runner=subprocess.run,
                   launcher=None, first_run: bool = False, archive_source: str | None = None,
                   archive_dir=None) -> bool:
    """Run the preflight through the GUI progress window. False stops before anything is deployed."""
    game_dir, manager_dir = Path(game_dir).resolve(), Path(manager_dir).resolve()
    if not ensure_archive_tool(manager_dir, progress, runner=runner):
        return False
    latest_info = check_latest() if check_latest else (None, "not checked")
    latest_check = lambda: latest_info
    prereq_state = {}
    baseline_ready = False
    progress("Preflight: checking Morrowind.exe, the masters and BSAs, Morrowind.ini, the Code Patch, MGE XE "
             "and MWSE…")
    report = inspect_install(game_dir, manager_dir, progress=progress, check_latest=latest_check)
    if first_run:
        prerequisite_prefixes = ("The Morrowind Code Patch", "MCP Beta:", "MGE XE", "MWSE.dll")
        game_blockers = [item for item in report["blockers"]
                         if not item.startswith(prerequisite_prefixes)]
        if game_blockers:
            progress.ask_yes_no("Preflight stopped",
                                "Fix the base game before installing prerequisites:\n\n- " +
                                "\n- ".join(game_blockers) + "\n\n" + recovery_text(game_dir))
            return False
        import mwmanager_journal
        journal = mwmanager_journal.Journal(manager_dir / "journal")
        valid_baseline = journal.has_baseline() and journal.baseline_matches_install(game_dir)[0]
        if not valid_baseline and any(item.get("present") for item in report["prerequisites"].values()):
            progress.ask_yes_no(
                "A clean store baseline is required",
                "Prerequisite files already exist, but MWManager has not recorded the clean GOG installation. "
                "Use GOG Verify/Repair (or reinstall), then run MWManager before installing MWSE, MCP or MGE XE. "
                "This prevents prerequisite files from entering baseline.json.")
            return False
        if report["hash_mismatches"]:
            details = "\n".join(f"- {name}: expected {expected[:16]}…, found {actual[:16]}…"
                                for name, expected, actual in report["hash_mismatches"])
            if not progress.ask_yes_no(
                    "Restore altered core game files",
                    "These core ESM/BSA files differ from the originals shared by GOG, Steam and other stores.\n\n" +
                    details + "\n\n" + recovery_text(game_dir) +
                    "\n\nContinue without restoring them? This is not recommended."):
                return False
        if not _prepare_first_run(game_dir, manager_dir, progress, archive_source):
            return False
        if not _offer_baseline(game_dir, manager_dir, progress):
            return False
        baseline_ready = True
    while True:
        for line in report["status"]:
            progress("Preflight: " + line)
        if not report["blockers"]:
            break
        if not _offer_missing(report, progress, game_dir, manager_dir, archive_dir,
                              latest_info[0], runner, prereq_state):
            return False
        progress("Preflight: re-checking after prerequisite installation…")
        report = inspect_install(game_dir, manager_dir, progress=progress, check_latest=latest_check)

    overrides = []
    if report["hash_mismatches"] and not first_run:
        details = "\n".join(f"- {name}: expected {expected[:16]}…, found {actual[:16]}…"
                            for name, expected, actual in report["hash_mismatches"])
        if not progress.ask_yes_no(
                "Restore altered core game files",
                "These core ESM/BSA files differ from the originals shared by GOG, Steam and other stores.\n\n" +
                details + "\n\n" + recovery_text(game_dir) +
                "\n\nContinue without restoring them? This is not recommended."):
            return False
        overrides.append("core-file-hash-mismatch")

    for key, text in report["recommendations"]:
        if key == "mge_g7":
            choice = _choose(progress, "MGE XE: Greatness7 build recommended", text,
                             [("Continue", "continue"), ("Open Greatness7 page", "open")], "continue")
            if choice == "open":
                mwmanager_prereqs.open_download_page("mge_g7", manager_dir / "pre-reqs" / "mge_xe")
                progress("Opened " + mwmanager_prereqs.PAGES["mge_g7"]["page"])
            overrides.append("mge-not-confirmed-g7")
        else:
            progress("Preflight: " + text)

    mwse = report["mwse"]
    if mwse.get("state") == "outdated" and mwse.get("updater") and not prereq_state.get("mwse_update_offered"):
        have = mwse["installed"][:12] if mwse.get("installed") else "unknown (no mwse-version.txt)"
        if progress.ask_yes_no(
                "MWSE update available",
                f"MWManager always recommends the latest MWSE.\n\nInstalled build: {have}\n"
                f"Latest build: {mwse['latest'][:12]}\n\nRun MWSE-Update.exe now? Close Morrowind first. "
                "Choose No to continue with the installed MWSE."):
            if not run_mwse_updater(game_dir, mwse["latest"], progress, runner=runner):
                overrides.append("mwse-update-incomplete")
        else:
            overrides.append("mwse-update-declined")
    elif mwse.get("state") == "outdated":
        overrides.append("mwse-update-incomplete-or-declined")
    elif mwse.get("state") == "unknown":
        overrides.append("mwse-latest-unknown")

    if not confirm_first_launch(game_dir, progress, overrides, launcher=launcher):
        return False
    if not baseline_ready and not _offer_baseline(game_dir, manager_dir, progress):
        return False
    import mwmanager_journal
    prerequisite_record = mwmanager_journal.Journal(manager_dir / "journal").capture_prerequisites(game_dir)
    progress(f"Prerequisite checkpoint recorded: {len(prerequisite_record['files'])} files, "
             f"{manager_dir / 'journal' / 'prerequisites' / 'prerequisites.json'}")
    if first_run:
        for line in move_official_addon_docs(game_dir, manager_dir):
            progress("Preflight: " + line)
        if not _install_mwse_lua_suite(game_dir, progress):
            return False
    # The report was built before the first-launch prompt; refresh the log-derived lines after that launch.
    statuses = [line for line in report["status"]
                if not line.startswith(("Code Patch features seen by the game", "MWSE.log:"))]
    statuses.extend(mwmanager_prereqs.startup_log_lines(mwmanager_prereqs.startup_logs(game_dir)))
    _record_preflight(manager_dir, game_dir, overrides, statuses)
    progress("Preflight passed.")
    return True
