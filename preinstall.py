"""Preflight: the prerequisites MWManager checks before it changes anything, and nothing more.

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
- Informational: the Code Patch features the game logged at its last start (mgeXE.log line 2, else the top of
  MWSE.log). Install order is Code Patch, then MGE XE, then MWSE, so that log reflects the patched game.
- First launch: mgeXE.log and MWSE.log must exist and be newer than the installed Code Patch, MGE XE and MWSE,
  which proves the setup starts. If not, the user is asked to launch Morrowind once to the main menu and quit;
  a Launch Morrowind button starts the selected Morrowind.exe on their click (MWManager never waits on or
  closes the game), Re-check re-reads the logs, and Skip continues with a warning.
Missing prerequisites are never installed: preflight offers each official page instead. After a passing
preflight, the first run offers to record the install's starting state for rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import mwmanager_prereqs

CORE_FILES = ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm",
              "Morrowind.bsa", "Tribunal.bsa", "Bloodmoon.bsa")


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


def inspect_install(game_dir: Path, manager_dir: Path | None = None, progress=None, *,
                    check_latest=mwmanager_prereqs.latest_mwse_build) -> dict:
    """Read-only. Returns blockers (missing requirements), hash_mismatches, recommendations and status lines."""
    game_dir = Path(game_dir).resolve()
    data_dir = game_dir / "Data Files"
    report = {"blockers": [], "missing_keys": [], "hash_mismatches": [], "recommendations": [], "status": [],
              "mwse": {}}
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
        return True
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


def _offer_missing(report: dict, progress) -> None:
    keys = [k for k in mwmanager_prereqs.INSTALL_ORDER if k in report["missing_keys"]]
    pages = "\n".join(f"- {mwmanager_prereqs.PAGES[k]['name']}: {mwmanager_prereqs.PAGES[k]['page']}\n"
                      f"  {mwmanager_prereqs.PAGES[k]['note']}" for k in keys)
    message = ("Preflight stopped. MWSE, MGE XE and the Morrowind Code Patch with MCP Beta: Skunk Works are "
               "prerequisites you install yourself; MWManager never downloads or installs them. Install them in "
               "this order: the Code Patch (with Skunk Works) first, then MGE XE, then MWSE, so that the patched "
               "game is what MGE XE and MWSE start with and log. For missing Morrowind files, use your store's "
               "repair or verify action.\n\n"
               + "\n".join("- " + item for item in report["blockers"]))
    if not keys:
        progress.ask_yes_no("Preflight stopped", message + "\n\nFix these, then run preflight again.")
        return
    first = keys[0]
    choices = [(f"Open {mwmanager_prereqs.PAGES[first]['name']}", first)]
    if first == "mge_xe":
        choices = [("Open Greatness7 build (recommended)", "mge_g7"), ("Open official MGE XE", "mge_xe")]
    choices.append(("Not now", "none"))
    choice = _choose(progress, "Required prerequisites missing",
                     message + "\n\nOfficial pages, in install order:\n" + pages +
                     "\n\nOpen the first one now? The next is offered when you run preflight again.",
                     choices, "none")
    if choice and choice != "none":
        where = mwmanager_prereqs.open_download_page(choice)
        progress(f"Opened {mwmanager_prereqs.PAGES[choice]['page']} in the "
                 + ("MWManager browser." if where == "embedded" else "system browser."))


def run_preinstall(game_dir: Path, manager_dir: Path, progress, *,
                   check_latest=mwmanager_prereqs.latest_mwse_build, runner=subprocess.run,
                   launcher=None) -> bool:
    """Run the preflight through the GUI progress window. False stops before anything is deployed."""
    game_dir, manager_dir = Path(game_dir).resolve(), Path(manager_dir).resolve()
    progress("Preflight: checking Morrowind.exe, the masters and BSAs, Morrowind.ini, the Code Patch, MGE XE "
             "and MWSE…")
    report = inspect_install(game_dir, manager_dir, progress=progress, check_latest=check_latest)
    for line in report["status"]:
        progress("Preflight: " + line)
    if report["blockers"]:
        _offer_missing(report, progress)
        return False

    overrides = []
    if report["hash_mismatches"]:
        details = "\n".join(f"- {name}: expected {expected[:16]}…, found {actual[:16]}…"
                            for name, expected, actual in report["hash_mismatches"])
        if not progress.ask_yes_no(
                "Core game files differ from the originals",
                "These files differ from the originals, which are the same on GOG, Steam and other stores. Use "
                "your store's repair or verify action if this is unexpected.\n\n" + details +
                "\n\nContinue anyway?"):
            return False
        overrides.append("core-file-hash-mismatch")

    for key, text in report["recommendations"]:
        if key == "mge_g7":
            choice = _choose(progress, "MGE XE: Greatness7 build recommended", text,
                             [("Continue", "continue"), ("Open Greatness7 page", "open")], "continue")
            if choice == "open":
                mwmanager_prereqs.open_download_page("mge_g7")
                progress("Opened " + mwmanager_prereqs.PAGES["mge_g7"]["page"])
            overrides.append("mge-not-confirmed-g7")
        else:
            progress("Preflight: " + text)

    mwse = report["mwse"]
    if mwse.get("state") == "outdated" and mwse.get("updater"):
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
    elif mwse.get("state") == "unknown":
        overrides.append("mwse-latest-unknown")

    if not confirm_first_launch(game_dir, progress, overrides, launcher=launcher):
        return False
    if not _offer_baseline(game_dir, manager_dir, progress):
        return False
    _record_preflight(manager_dir, game_dir, overrides, report["status"])
    progress("Preflight passed.")
    return True
