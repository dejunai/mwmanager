"""MWSE updater offers and KnownBug re-apply for MWSE core Lua files.

Conflict Tools owns the fingerprints and the apply/undo work in mwdialog_luafix.py.
This module is the thin MWManager adapter around apply_core_known_bugs: dry-run
first, then apply. Statuses map to a short user message; the full CoreResult
detail goes to the status log.

The official updater is MWSE-Update.exe in the game folder. Freshness is the
build-automatic GitHub release (mwmanager_prereqs.latest_mwse_build), which is
what the updater installs, recorded in mwse-version.txt.

Nothing here writes to a game folder other than the game_dir it is given.
--play does no network I/O.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

DETAIL_PREFIX = "MWMANAGER-DETAIL: "
INI_SECTION = "MWSE"
INI_DEFAULTS = {
    "check_on_play": True,
    "apply_core_patches": True,
    "offer_update_on_full": True,
}
STAMP_NAME = "mwse_core_patch_state.json"
DETAIL_LOG_NAME = "mwmanager_status_detail.log"
HELPER_APPLY = "apply_core_known_bugs"
CORE_REL = "core/mcm/components/pages/exclusionspage.lua"
PLAY_BLOCK_CODE = 3  # same as a --play load-order / missing-master stop


def default_manager_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _manager_dir(manager_dir=None):
    return os.path.abspath(manager_dir or default_manager_dir())


def ini_path(manager_dir=None):
    return os.path.join(_manager_dir(manager_dir), "mwmanager.ini")


def output_dir(manager_dir=None):
    return os.path.join(_manager_dir(manager_dir), "outputs")


def stamp_path(manager_dir=None):
    return os.path.join(output_dir(manager_dir), STAMP_NAME)


def detail_log_path(manager_dir=None):
    return os.path.join(output_dir(manager_dir), DETAIL_LOG_NAME)


def luafix_journal_path(manager_dir=None):
    return os.path.join(output_dir(manager_dir), "mwmanager_luafix_journal.jsonl")


def luafix_backup_root(manager_dir=None):
    return os.path.join(_manager_dir(manager_dir), "backups", "luafix")


def ini_flag(name, manager_dir=None, default=None):
    """Read an [MWSE] boolean from mwmanager.ini. Missing keys default to true."""
    import configparser
    if name not in INI_DEFAULTS:
        raise KeyError(name)
    fallback = INI_DEFAULTS[name] if default is None else default
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(ini_path(manager_dir), encoding="utf-8")
        return parser.getboolean(INI_SECTION, name, fallback=fallback)
    except (OSError, ValueError, configparser.Error):
        return fallback


def write_ini_flags(flags, manager_dir=None):
    """Write the known [MWSE] keys, leaving every other section as it is."""
    import configparser
    path = ini_path(manager_dir)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    if not parser.has_section(INI_SECTION):
        parser.add_section(INI_SECTION)
    for key, fallback in INI_DEFAULTS.items():
        value = flags[key] if key in flags else ini_flag(key, manager_dir, fallback)
        parser.set(INI_SECTION, key, "true" if value else "false")
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as stream:
        parser.write(stream)
    os.replace(tmp, path)
    return path


def append_detail_log(lines, manager_dir=None):
    """Append detail lines to outputs/mwmanager_status_detail.log. Never raises."""
    if not lines:
        return
    path = detail_log_path(manager_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            for line in lines:
                text = str(line).rstrip()
                if text.startswith(DETAIL_PREFIX):
                    text = text[len(DETAIL_PREFIX):].lstrip()
                stream.write(text + "\n")
    except OSError:
        pass


def emit(short=None, detail=None, progress=None, manager_dir=None):
    """Short status goes to the user (or a progress callback). Detail goes to the log."""
    if short:
        if callable(progress):
            progress(short)
        else:
            print(short)
    lines = list(detail or [])
    if lines:
        append_detail_log(lines, manager_dir)
        if callable(progress):
            for line in lines:
                text = str(line)
                if not text.startswith(DETAIL_PREFIX):
                    text = DETAIL_PREFIX + text
                progress(text)


def load_luafix():
    import mwdialog_luafix
    return mwdialog_luafix


def mwse_dir(game_dir):
    return os.path.join(os.path.abspath(game_dir), "Data Files", "MWSE")


def core_lua_path(game_dir, rel=CORE_REL):
    return os.path.join(mwse_dir(game_dir), *rel.split("/"))


def read_mwse_version(game_dir):
    marker = os.path.join(os.path.abspath(game_dir), "mwse-version.txt")
    try:
        text = open(marker, encoding="ascii", errors="ignore").read().strip()
    except OSError:
        return None
    return text.lower() if re.fullmatch(r"[0-9a-fA-F]{7,40}", text) else None


def rules_fingerprint(lf=None):
    """Invalidate the stamp when core KnownBug fingerprints change."""
    lf = lf or load_luafix()
    core = [(rule, rel, old) for rule, _plugin, rel, old, _new, _what in lf.KNOWN_BUGS
            if rel.startswith("core/")]
    payload = json.dumps(core, sort_keys=True).encode("utf-8")
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()[:16] + f"v{getattr(lf, 'VERSION', 0)}"


def file_sha(lf, path):
    with open(path, "rb") as stream:
        return lf.sha(stream.read())


def known_core_hashes(game_dir, lf=None):
    lf = lf or load_luafix()
    files = {}
    for path in lf.known_core_files(mwse_dir(game_dir)):
        rel = os.path.relpath(path, mwse_dir(game_dir)).replace("\\", "/")
        try:
            files[rel] = file_sha(lf, path)
        except OSError:
            files[rel] = None
    return files


def load_stamp(manager_dir=None):
    path = stamp_path(manager_dir)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_stamp(game_dir, manager_dir=None, lf=None, extra=None):
    lf = lf or load_luafix()
    payload = {
        "game_dir": os.path.normcase(os.path.abspath(game_dir)),
        "mwse_version": read_mwse_version(game_dir),
        "files": known_core_hashes(game_dir, lf),
        "rules_rev": rules_fingerprint(lf),
        "patched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rules": sorted({rule for rule, _plugin, rel, _old, _new, _what in lf.KNOWN_BUGS
                         if rel.startswith("core/")}),
    }
    if extra:
        payload.update(extra)
    path = stamp_path(manager_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(tmp, path)
    return payload


def stamp_matches(game_dir, manager_dir=None, lf=None):
    """Cheap local comparison: version, core-file SHA-1s, fingerprint revision, game folder."""
    stamp = load_stamp(manager_dir)
    if not stamp:
        return False
    lf = lf or load_luafix()
    wanted_game = os.path.normcase(os.path.abspath(game_dir))
    if os.path.normcase(str(stamp.get("game_dir") or "")) != wanted_game:
        return False
    if stamp.get("mwse_version") != read_mwse_version(game_dir):
        return False
    if stamp.get("rules_rev") != rules_fingerprint(lf):
        return False
    return stamp.get("files") == known_core_hashes(game_dir, lf)


def _fingerprint_message(status, pending=None, partial=None):
    pending = list(pending or [])
    partial = list(partial or [])
    if status == "current":
        return "MWSE core patches already current"
    if status == "pending":
        return "MWSE core patches need reapplying" + (f" ({', '.join(pending)})" if pending else "")
    if status == "partial":
        return ("MWSE core patches could not be reapplied: the core file only partly "
                "matches a KnownBug fingerprint" + (f" ({', '.join(partial)})" if partial else ""))
    if status == "missing":
        return "MWSE core Lua files named by KnownBug rules were not found"
    return "MWSE core patches could not be reapplied"


def data_files_dir(game_dir, lf=None):
    """The Data Files folder for game_dir, using the helper when present."""
    lf = lf or load_luafix()
    helper = getattr(lf, "core_data_dir", None)
    if callable(helper):
        _game, data_dir = helper(game_dir)
        return data_dir
    return os.path.join(os.path.abspath(game_dir), "Data Files")


def _result_status(r):
    return getattr(r, "status", None) or (r.get("status") if isinstance(r, dict) else None)


def _result_field(r, name, default=None):
    if isinstance(r, dict):
        return r.get(name, default)
    return getattr(r, name, default)


def _result_lines(results, dry_run=False, lf=None):
    lf = lf or load_luafix()
    helper = getattr(lf, "core_result_lines", None)
    if callable(helper):
        try:
            return list(helper(results, dry_run))
        except TypeError:
            return list(helper(results))
    lines = []
    for r in results or []:
        rel = _result_field(r, "rel") or _result_field(r, "path")
        status = _result_status(r) or "unknown"
        reason = _result_field(r, "reason") or ""
        kb = " ".join(_result_field(r, "kb_ids") or [])
        parts_m = _result_field(r, "parts_matched", 0)
        parts_t = _result_field(r, "parts_total", 0)
        lines.append(f"{status.upper()}: {rel} ({kb}; parts {parts_m}/{parts_t})")
        if reason:
            lines.append(f"    {reason}")
        if _result_field(r, "written"):
            lines.append(f"    journal entry {_result_field(r, 'journal_entry')}; backup {_result_field(r, 'backup')}")
    return lines


def _overall_from_results(results, dry_run=False):
    """Roll a list of CoreResult into the adapter's overall status."""
    statuses = [_result_status(r) for r in (results or [])]
    pending_rules = []
    partial_rules = []
    refused_reasons = []
    for r in results or []:
        for kb in _result_field(r, "kbs") or []:
            state = kb.get("state") if isinstance(kb, dict) else None
            name = kb.get("kb") if isinstance(kb, dict) else None
            if state == "matches" and name:
                pending_rules.append(name)
            if state == "partial" and name:
                partial_rules.append(name)
        if _result_status(r) == "applied":
            pending_rules.extend(_result_field(r, "kb_ids") or [])
        if _result_status(r) == "partial":
            partial_rules.extend(_result_field(r, "kb_ids") or [])
        if _result_status(r) == "refused":
            refused_reasons.append(_result_field(r, "reason") or "refused")
    pending_rules = sorted(set(pending_rules))
    partial_rules = sorted(set(partial_rules))
    if "partial" in statuses:
        overall = "partial"
    elif "refused" in statuses:
        overall = "refused"
    elif "applied" in statuses:
        overall = "pending" if dry_run else "applied"
    elif not statuses:
        overall = "missing"
    else:
        overall = "current"
    if overall == "pending":
        message = _fingerprint_message("pending", pending_rules)
    elif overall == "applied":
        message = "MWSE core patches reapplied" + (f" ({', '.join(pending_rules)})" if pending_rules else "")
    elif overall == "refused":
        message = "MWSE core patches could not be reapplied"
    else:
        message = _fingerprint_message(overall, pending_rules, partial_rules)
        if overall == "partial":
            message = _fingerprint_message("partial", pending_rules, partial_rules)
    return {
        "status": overall,
        "pending": overall == "pending",
        "partial": overall == "partial",
        "pending_rules": pending_rules,
        "partial_rules": partial_rules,
        "results": results,
        "message": message,
        "detail": _result_lines(results, dry_run),
        "refused_reasons": refused_reasons,
    }


def apply_core_known_bugs(game_dir, *, dry_run=False, lf=None, checker=None, manager_dir=None,
                          journal=None, backup_root=None, no_syntax_check=False):
    """Thin adapter around mwdialog_luafix.apply_core_known_bugs.

    Always pass a manager or temp journal/backup root so tests never write into a live game.
    """
    lf = lf or load_luafix()
    helper = getattr(lf, "apply_core_known_bugs", None)
    if not callable(helper):
        raise RuntimeError("mwdialog_luafix.apply_core_known_bugs is not available")
    game_dir = os.path.abspath(game_dir)
    try:
        _game, data_dir = lf.core_data_dir(game_dir)
        game_dir = _game
    except (AttributeError, ValueError):
        data_dir = os.path.join(game_dir, "Data Files")
    kwargs = {
        "game_dir": game_dir,
        "journal": journal or luafix_journal_path(manager_dir),
        "backup_root": backup_root or luafix_backup_root(manager_dir),
        "no_syntax_check": no_syntax_check,
    }
    if checker is not None:
        kwargs["checker"] = checker
    return list(helper(data_dir, dry_run, **kwargs))


def pending_core_patches(game_dir, *, lf=None, checker=None, manager_dir=None, no_syntax_check=False):
    """Dry-run the core-only helper and map CoreResult statuses to the adapter summary."""
    lf = lf or load_luafix()
    results = apply_core_known_bugs(game_dir, dry_run=True, lf=lf, checker=checker,
                                    manager_dir=manager_dir, no_syntax_check=no_syntax_check)
    return _overall_from_results(results, dry_run=True)


def apply_known_core(game_dir, *, lf=None, checker=None, manager_dir=None, dry_run=False,
                     journal=None, backup_root=None, no_syntax_check=False):
    """Apply (or dry-run) core KnownBug patches through the Conflict Tools helper."""
    lf = lf or load_luafix()
    results = apply_core_known_bugs(
        game_dir, dry_run=dry_run, lf=lf, checker=checker, manager_dir=manager_dir,
        journal=journal, backup_root=backup_root, no_syntax_check=no_syntax_check)
    summary = _overall_from_results(results, dry_run=dry_run)
    written = sum(1 for r in results if _result_field(r, "written"))
    refused = [(_result_field(r, "path") or _result_field(r, "rel"),
                _result_field(r, "reason") or "refused")
               for r in results if _result_status(r) == "refused"]
    if dry_run and summary["status"] == "pending":
        status = "applied"
        message = "--dry-run: would reapply MWSE core patches"
        if summary["pending_rules"]:
            message += f" ({', '.join(summary['pending_rules'])})"
    else:
        status = summary["status"]
        if status == "pending":
            status = "applied"
        message = summary["message"]
        if status == "current":
            message = "MWSE core patches already current"
    return {
        "written": written,
        "refused": refused,
        "rules": summary["pending_rules"],
        "status": status,
        "message": message,
        "detail": summary["detail"],
        "results": results,
        "partial": summary["partial"],
        "pending": summary["pending"],
        "dry_run": dry_run,
    }


def morrowind_process_running():
    """True if Morrowind.exe appears to be running. Never tries to stop it."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Morrowind.exe", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "morrowind.exe" in (out or "").lower()


def reapply_core_patches(game_dir, *, manager_dir=None, lf=None, checker=None, dry_run=False,
                         progress=None, require_closed=True, no_syntax_check=False):
    """Dry-run first, then apply pending KnownBug patches and refresh the stamp."""
    lf = lf or load_luafix()
    if require_closed and morrowind_process_running():
        result = {
            "written": 0, "status": "refused",
            "message": "MWSE core patches could not be reapplied",
            "detail": ["Morrowind.exe is running; close it before core files are written. "
                       "Nothing was killed and nothing was written."],
            "refused": [("<core>", "Morrowind.exe is running")],
            "results": [],
        }
        emit(result["message"], result["detail"], progress, manager_dir)
        return result
    preview = apply_known_core(game_dir, lf=lf, checker=checker, manager_dir=manager_dir,
                               dry_run=True, no_syntax_check=no_syntax_check)
    if preview.get("status") in {"partial", "refused"}:
        emit(preview["message"], preview.get("detail"), progress, manager_dir)
        return preview
    if not preview.get("pending"):
        detail = list(preview.get("detail") or [])
        if not dry_run:
            try:
                stamp = write_stamp(game_dir, manager_dir, lf)
                detail.append(f"stamp written: {stamp_path(manager_dir)} version={stamp.get('mwse_version')}")
            except OSError as exc:
                detail.append(f"stamp could not be written: {exc}")
        preview["detail"] = detail
        preview["status"] = "unchanged"
        preview["message"] = preview.get("message") or "MWSE core patches already current"
        emit(preview["message"], detail, progress, manager_dir)
        return preview
    if dry_run:
        emit(preview["message"], preview.get("detail"), progress, manager_dir)
        return preview
    result = apply_known_core(game_dir, lf=lf, checker=checker, manager_dir=manager_dir,
                              dry_run=False, no_syntax_check=no_syntax_check)
    detail = list(result.get("detail") or [])
    fingerprints_ok = (
        result.get("status") in {"applied", "current", "unchanged"}
        and not result.get("refused") and not result.get("partial")
    )
    if fingerprints_ok:
        try:
            stamp = write_stamp(game_dir, manager_dir, lf)
            detail.append(f"stamp written: {stamp_path(manager_dir)} version={stamp.get('mwse_version')}")
        except OSError as exc:
            detail.append(f"stamp could not be written: {exc}")
    result["detail"] = detail
    emit(result["message"], detail, progress, manager_dir)
    return result


def ensure_core_patches_for_play(game_dir, *, manager_dir=None, lf=None, checker=None,
                                 play_anyway=False, progress=None, apply=None, check=None,
                                 no_syntax_check=False):
    """No-network --play check: stamp first, then a dry-run of the helper, then apply if needed.

    partial and refused block launch unless --play-anyway.
    """
    lf = lf or load_luafix()
    if check is None:
        check = ini_flag("check_on_play", manager_dir)
    if apply is None:
        apply = ini_flag("apply_core_patches", manager_dir)
    if not check:
        return {"block": False, "code": 0, "status": "skipped",
                "message": None, "detail": ["--play MWSE core check skipped (check_on_play is false)"],
                "result": None}
    if stamp_matches(game_dir, manager_dir, lf):
        message = "MWSE core patches already current"
        emit("--play: " + message, [f"stamp matches at {stamp_path(manager_dir)}"], progress, manager_dir)
        return {"block": False, "code": 0, "status": "current", "message": message,
                "detail": [f"stamp matches at {stamp_path(manager_dir)}"], "result": None}

    preview = pending_core_patches(game_dir, lf=lf, checker=checker, manager_dir=manager_dir,
                                   no_syntax_check=no_syntax_check)
    detail = [f"stamp miss at {stamp_path(manager_dir)}"] + list(preview.get("detail") or [])

    if preview["status"] == "current":
        try:
            write_stamp(game_dir, manager_dir, lf)
            detail.append("stamp refreshed; no pending fingerprints")
        except OSError as exc:
            detail.append(f"stamp could not be written: {exc}")
        message = "MWSE core patches already current"
        emit("--play: " + message, detail, progress, manager_dir)
        return {"block": False, "code": 0, "status": "current", "message": message,
                "detail": detail, "result": None}

    applied = None
    blocking = preview["status"] in {"partial", "refused"}
    if preview["status"] == "pending" and apply:
        applied = reapply_core_patches(game_dir, manager_dir=manager_dir, lf=lf, checker=checker,
                                       progress=progress, require_closed=True,
                                       no_syntax_check=no_syntax_check)
        detail.extend(applied.get("detail") or [])
        if (applied.get("status") in {"applied", "unchanged", "current"}
                and not applied.get("refused") and not applied.get("partial")):
            return {"block": False, "code": 0, "status": applied["status"],
                    "message": applied["message"], "detail": detail, "result": applied}
        message = applied.get("message") or "MWSE core patches could not be reapplied"
        blocking = (applied.get("status") in {"partial", "refused"}
                    or bool(applied.get("refused") or applied.get("partial")))
    else:
        message = preview["message"]
        if preview["status"] == "pending" and not apply:
            detail.append("apply_core_patches is false; core files were not written")
        emit("--play: " + message, detail, progress, manager_dir)
        blocking = preview["status"] in {"partial", "refused", "pending"}

    if blocking and play_anyway:
        detail.append("--play-anyway: starting the game without reapplying MWSE core patches")
        emit("--play: starting anyway (--play-anyway).", detail, progress, manager_dir)
        return {"block": False, "code": 0, "status": (applied or preview).get("status"),
                "message": message, "detail": detail, "result": applied, "play_anyway": True}

    if blocking:
        emit("--play: NOT starting the game. " + message, detail, progress, manager_dir)
        return {"block": True, "code": PLAY_BLOCK_CODE,
                "status": (applied or preview).get("status"),
                "message": message, "detail": detail, "result": applied}

    emit("--play: " + message, detail, progress, manager_dir)
    return {"block": False, "code": 0, "status": (applied or preview).get("status"),
            "message": message, "detail": detail, "result": applied}


def mwse_update_status(game_dir, *, latest=None, latest_error=None, check_latest=True):
    """Local files plus, optionally, the build-automatic target commit. Dict, no writes."""
    game_dir = os.path.abspath(game_dir)
    updater = os.path.join(game_dir, "MWSE-Update.exe")
    dll = os.path.join(game_dir, "MWSE.dll")
    installed = read_mwse_version(game_dir)
    present = os.path.isfile(dll)
    updater_present = os.path.isfile(updater)
    error = latest_error
    if check_latest and latest is None and error is None:
        import mwmanager_prereqs
        latest, error = mwmanager_prereqs.latest_mwse_build()
    if not updater_present:
        state = "missing-updater"
    elif not present:
        state = "incomplete"
    elif latest and installed and (latest.startswith(installed) or installed == latest):
        state = "current"
    elif latest and not installed:
        state = "outdated"
    elif latest and installed:
        state = "outdated"
    else:
        state = "unknown"
    return {
        "state": state,
        "installed": installed,
        "latest": latest,
        "error": error,
        "present": present,
        "updater": updater if updater_present else "",
        "updater_present": updater_present,
        "dll": present,
    }


def _ask_user(title, message, *, yes=False, ask=None, progress=None):
    if yes:
        return True
    if ask is not None:
        return bool(ask(title, message))
    if progress is not None and hasattr(progress, "ask_yes_no"):
        return bool(progress.ask_yes_no(title, message))
    try:
        import tkinter as tk
        import mwmanager_dialogs as messagebox
        root = tk.Tk()
        root.withdraw()
        answer = messagebox.askyesno(title, message, parent=root)
        root.destroy()
        return bool(answer)
    except Exception:
        try:
            return input(message + " [y/N] ").strip().casefold() in {"y", "yes"}
        except (EOFError, OSError):
            return False


def run_official_updater(game_dir, *, latest=None, latest_error=None, dry_run=False, yes=False,
                         force=False, progress=None, ask=None, runner=None, manager_dir=None,
                         checker=None, reapply=True, require_offer_flag=False):
    """One path for Tools > Update MWSE, preflight, --init and --full.

    Compares mwse-version.txt with the build-automatic release, offers MWSE-Update.exe
    when the build is outdated or unknown (or, with force, when the user opened the
    Tools action), waits for it, then re-applies KnownBug core patches.
    """
    if require_offer_flag and not force and not ini_flag("offer_update_on_full", manager_dir):
        emit("MWSE update offer skipped (offer_update_on_full is false).",
             None, progress, manager_dir)
        return {"state": "skipped-offer", "updated": False, "patches": None,
                "message": "MWSE update offer skipped (offer_update_on_full is false)."}

    import mwmanager_prereqs
    status = mwse_update_status(game_dir, latest=latest, latest_error=latest_error,
                                check_latest=latest is None and latest_error is None)
    latest = status["latest"]
    installed = status["installed"]
    updater = status["updater"]

    if not status["updater_present"]:
        emit("WARNING: MWSE-Update.exe was not found in the Morrowind game folder. "
             "MWSE may be missing or its updater may need to be restored.",
             [f"game_dir={os.path.abspath(game_dir)}"], progress, manager_dir)
        if not dry_run:
            try:
                from mwmanager_init import show_download_interface, PREREQUISITE_URLS
                show_download_interface([("MWSE and its updater",
                                          "MGE XE normally includes MWSE-Update.exe. "
                                          "Install/update MWSE, then rerun MWManager.",
                                          PREREQUISITE_URLS["mwse"])], "MWSE updater missing")
            except Exception as exc:
                emit(None, [f"could not open the MWSE download page: {exc}"], progress, manager_dir)
        return {"state": "missing-updater", "updated": False, "patches": None,
                "message": "MWSE-Update.exe was not found.", "status": status}

    if not status["dll"]:
        emit("MWSE appears incomplete (MWSE.dll missing).", None, progress, manager_dir)
    elif installed:
        emit(f"MWSE installation found; installed build marker: {installed}.", None, progress, manager_dir)
    else:
        emit("MWSE installation found; installed build marker: unrecognized or missing.",
             None, progress, manager_dir)

    if status["state"] == "current":
        emit("MWSE is current according to the upstream build marker; no update prompt is needed.",
             [f"installed={installed} latest={latest}"], progress, manager_dir)
        patches = None
        # Tools > Update MWSE still re-applies in case a previous updater wiped the patches.
        # --init / --full only re-apply after the official updater has actually run.
        if force and reapply and ini_flag("apply_core_patches", manager_dir):
            patches = reapply_core_patches(game_dir, manager_dir=manager_dir, checker=checker,
                                           progress=progress, require_closed=True)
        return {"state": "current", "updated": False, "patches": patches, "status": status,
                "message": "MWSE is current according to the upstream build marker; no update prompt is needed."}

    if status["state"] == "outdated" and installed and latest:
        reason = "A newer MWSE build is available."
        emit(f"A newer MWSE upstream revision is available ({installed[:12]} -> {latest[:12]}).",
             [f"installed={installed} latest={latest}"], progress, manager_dir)
    elif status["state"] == "incomplete" or not status["dll"]:
        reason = "MWSE is missing or incomplete."
    else:
        why = status["error"] or "unrecognized installed build marker"
        emit(f"WARNING: MWSE update status could not be verified ({why}).",
             [f"installed={installed} latest={latest} error={why}"], progress, manager_dir)
        reason = "MWSE update status could not be verified. Run its official updater to check for an update?"

    if dry_run:
        emit(f"--dry-run: would ask whether to run MWSE-Update.exe. Reason: {reason}",
             None, progress, manager_dir)
        return {"state": status["state"], "updated": False, "dry_run": True, "status": status,
                "message": reason, "patches": None}

    if yes:
        emit("--yes: running the official MWSE updater because the installed build is outdated "
             "or could not be verified.", None, progress, manager_dir)
        answer = True
    else:
        answer = _ask_user("MWSE update available",
                           reason + "\n\nMWManager will run MWSE-Update.exe and wait for it to finish.",
                           yes=False, ask=ask, progress=progress)
    if not answer:
        emit("MWSE update skipped; continuing with the installed build.", None, progress, manager_dir)
        return {"state": status["state"], "updated": False, "skipped": True, "status": status,
                "message": "MWSE update skipped; continuing with the installed build.", "patches": None}

    if morrowind_process_running():
        emit("WARNING: Morrowind.exe is running. Close it, then run the MWSE updater. "
             "Nothing was started and nothing was written.",
             None, progress, manager_dir)
        return {"state": status["state"], "updated": False, "status": status,
                "message": "Morrowind.exe is running; the updater was not started.", "patches": None}

    runner = runner or subprocess.run
    try:
        result = runner([updater], cwd=os.path.abspath(game_dir), check=False)
    except OSError as exc:
        emit(f"WARNING: could not start MWSE-Update.exe: {exc}", [repr(exc)], progress, manager_dir)
        return {"state": status["state"], "updated": False, "status": status,
                "message": f"could not start MWSE-Update.exe: {exc}", "patches": None}
    code = getattr(result, "returncode", 0) or 0
    if code:
        emit(f"WARNING: MWSE updater exited with code {code}; continuing.",
             [f"returncode={code}"], progress, manager_dir)
        return {"state": status["state"], "updated": False, "status": status,
                "message": f"MWSE updater exited with code {code}.", "patches": None,
                "returncode": code}

    after = read_mwse_version(game_dir)
    if latest and after and (latest.startswith(after) or after == latest):
        emit(f"MWSE updater finished; installed build is now {after[:12]}.",
             [f"installed={after} latest={latest}"], progress, manager_dir)
    else:
        emit("MWSE updater finished.",
             [f"mwse-version.txt after updater: {after}; expected latest={latest}"],
             progress, manager_dir)

    patches = None
    if reapply and ini_flag("apply_core_patches", manager_dir):
        patches = reapply_core_patches(game_dir, manager_dir=manager_dir, checker=checker,
                                       progress=progress, require_closed=True)
    return {"state": "updated", "updated": True, "status": status, "installed": after,
            "message": "MWSE updater finished.", "patches": patches, "returncode": 0}


def offer_mwse_update(game_dir, *, dry_run=False, yes=False, force=False, progress=None,
                      ask=None, runner=None, manager_dir=None, checker=None, reapply_after=True):
    """Public entry used by --init, --full, Tools > Update MWSE and preflight."""
    return run_official_updater(
        game_dir, dry_run=dry_run, yes=yes, force=force, progress=progress, ask=ask,
        runner=runner, manager_dir=manager_dir, checker=checker, reapply=reapply_after,
        require_offer_flag=not force)


def run_updater_then_reapply(game_dir, latest=None, progress=None, runner=None, manager_dir=None,
                             checker=None):
    """Preflight already decided to run the official updater: run it, then re-apply patches."""
    updater = os.path.join(os.path.abspath(game_dir), "MWSE-Update.exe")
    runner = runner or subprocess.run
    emit(f"Running {updater} (the MWSE updater)…", None, progress, manager_dir)
    try:
        result = runner([updater], cwd=str(game_dir), check=False)
    except OSError as exc:
        hint = (" Windows says it needs administrator rights; run MWSE-Update.exe yourself as administrator."
                if getattr(exc, "winerror", None) == 740 else "")
        emit(f"Preflight: MWSE-Update.exe could not be started: {exc}.{hint}",
             [repr(exc)], progress, manager_dir)
        return False
    if getattr(result, "returncode", 0):
        emit(f"Preflight: MWSE-Update.exe exited with code {result.returncode}.",
             [f"returncode={result.returncode}"], progress, manager_dir)
        return False
    build = read_mwse_version(game_dir)
    if latest and build and (latest.startswith(build) or build == latest):
        emit(f"Preflight: MWSE is now the latest build ({latest[:12]}).",
             [f"installed={build} latest={latest}"], progress, manager_dir)
        ok = True
    else:
        emit("Preflight: MWSE-Update.exe finished, but mwse-version.txt does not show the latest build yet.",
             [f"installed={build} latest={latest}"], progress, manager_dir)
        ok = False
    if ini_flag("apply_core_patches", manager_dir):
        reapply_core_patches(game_dir, manager_dir=manager_dir, checker=checker,
                             progress=progress, require_closed=True)
    return ok
