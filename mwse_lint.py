"""Review and conservatively repair MWSE Lua modules in MWManager staging.

The scanner reports risky nil access and scheduling patterns for human review. It only
automatically normalizes explicitly numeric, extreme event priorities and redirects
logging calls found inside recurring callbacks to a per-module log file.
"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PureWindowsPath

RECURRING = re.compile(r"\b(?:simulate|enterFrame|preUpdate|update|cameraControl)\b", re.I)
EVENT_REGISTER = re.compile(r"event\.register\s*\(", re.I)
PRIORITY = re.compile(r"(\bpriority\s*=\s*)([-+]?\d+)", re.I)
MWSE_LOG = re.compile(r"\bmwse\.log\s*\(")
NIL_RISK = re.compile(r"\b(?:e\.(?:target|reference|mobile|info|dialogue|element)|\w+\.data|\w+\.keyCode)\b")
CAMERA_SMOOTH = re.compile(r"camera|smooth|interpolat|lerp|animat|drag", re.I)


def _is_mwse_lua(path: Path) -> bool:
    parts = {p.casefold() for p in path.parts}
    return (path.suffix.casefold() == ".lua" and "mwse" in parts
            and not parts.intersection({"off", "extras", "bugreports"}))


def _analysis(path: Path, text: str) -> tuple[list[dict], set[int], bool]:
    lines = text.splitlines(keepends=True)
    issues: list[dict] = []
    recurring_lines: set[int] = set()
    current_callback = False
    callback_start = 0
    for i, line in enumerate(lines):
        if EVENT_REGISTER.search(line):
            current_callback = bool(RECURRING.search("".join(lines[i:i + 4])))
            callback_start = i
        if current_callback and i - callback_start > 120:
            current_callback = False
        if current_callback:
            recurring_lines.add(i)
        for match in PRIORITY.finditer(line):
            value = int(match.group(2))
            if abs(value) > 10000:
                issues.append({"line": i + 1, "kind": "extreme_priority", "value": value,
                               "message": f"Priority {value} exceeds MWManager's 10000 cap; normalized to 10000."})
        if NIL_RISK.search(line) and not re.search(r"\bif\s+\w+\s+then|\bassert\s*\(|\bor\s+return\b", line):
            issues.append({"line": i + 1, "kind": "nil_review", "message": "Potentially nil-sensitive access; verify the value is checked before indexing or calling."})
        if EVENT_REGISTER.search(line) and RECURRING.search("".join(lines[i:i + 4])):
            callback = "".join(lines[i:min(len(lines), i + 20)])
            if not CAMERA_SMOOTH.search(callback):
                issues.append({"line": i + 1, "kind": "per_frame_review", "message": "Recurring frame event without an obvious camera/smoothing purpose; consider a timer or 0.35s throttle."})
    spam_lines = {i for i in recurring_lines if MWSE_LOG.search(lines[i])}
    for i in sorted(spam_lines):
        issues.append({"line": i + 1, "kind": "core_log_spam", "message": "mwse.log() runs inside a recurring event; redirected to this module's log."})
    return issues, spam_lines, bool(spam_lines)


def _module_root(path: Path, staging: Path) -> tuple[Path, Path]:
    rel = path.relative_to(staging)
    parts = rel.parts
    mwse_i = next((i for i, part in enumerate(parts) if part.casefold() == "mwse"), None)
    # Support both staging/<package>/MWSE and staging/<collection>/<package>/MWSE.
    package = staging.joinpath(*parts[:mwse_i]) if mwse_i is not None else \
        (staging / parts[0] if len(parts) > 1 else staging)
    if mwse_i is None:
        mwse_i = 0
    module_index = mwse_i + 1
    if module_index < len(parts) and parts[module_index].casefold() in {"mods", "lib", "core"}:
        module_index += 1
    module_name = parts[module_index] if module_index < len(parts) else package.name
    return package, Path(module_name)


def _logger_block(module_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", module_name) or "MWSE_Module"
    return ("\n-- MWManager: verbose recurring logs are isolated from MWSE.log.\n"
            "local mwmanagerLog = function(message, ...)\n"
            f"    local modName = {json.dumps(safe)}\n"
            "    local path = \"Data Files\\\\MWSE\\\\logs\\\\\" .. modName .. \".log\"\n"
            "    local formatted = tostring(message)\n"
            "    if select(\"#\", ...) > 0 then\n"
            "        local ok, value = pcall(string.format, message, ...)\n"
            "        if ok then formatted = value end\n"
            "    end\n"
            "    if lfs and lfs.mkdir then pcall(lfs.mkdir, \"Data Files\\\\MWSE\\\\logs\") end\n"
            "    local ok, file = pcall(io.open, path, \"a\")\n"
            "    if ok and file then\n"
            "        file:write(os.date(\"[%Y-%m-%d %H:%M:%S] \"), formatted, \"\\n\")\n"
            "        file:flush(); file:close()\n"
            "    end\n"
            "end\n")


def _safe_nil_repairs(text: str) -> tuple[str, list[dict]]:
    """Apply narrow guards to common reference-data, event-field and keybind reads."""
    lines = text.splitlines(keepends=True)
    changes: list[dict] = []
    patterns = [
        (re.compile(r"^(\s*local\s+[A-Za-z_]\w*\s*=\s*)(ref|reference)(\.data(?:\.[A-Za-z_]\w*|\[[^\]\r\n]+\])*)\s*$", re.I),
         lambda m: f"{m.group(1)}({m.group(2)} and {m.group(2)}.supportsLuaData and {m.group(2)}.data{m.group(3)[5:] if m.group(3).startswith('.data') else ''})"),
        (re.compile(r"^(\s*local\s+[A-Za-z_]\w*\s*=\s*)(e)\.(target|reference|mobile|info|dialogue|element)((?:\.[A-Za-z_]\w*|\[[^\]\r\n]+\])*)\s*$", re.I),
         lambda m: f"{m.group(1)}({m.group(2)} and {m.group(2)}.{m.group(3)}{m.group(4)})"),
        (re.compile(r"^(\s*local\s+[A-Za-z_]\w*\s*=\s*)([A-Za-z_]\w*)\.(keyCode)\s*$", re.I),
         lambda m: f"{m.group(1)}({m.group(2)} and {m.group(2)}.{m.group(3)})"),
        (re.compile(r"^(\s*if\s+)(e)\.(target|reference|mobile|info|dialogue|element)(\s+then\s*(?:--.*)?)$", re.I),
         lambda m: f"{m.group(1)}{m.group(2)} and {m.group(2)}.{m.group(3)}{m.group(4)}"),
        (re.compile(r"^(\s*if\s+)([A-Za-z_]\w*)\.(keyCode)(\s+then\s*(?:--.*)?)$", re.I),
         lambda m: f"{m.group(1)}{m.group(2)} and {m.group(2)}.{m.group(3)}{m.group(4)}"),
    ]
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        for pattern, replace in patterns:
            match = pattern.match(body)
            if match:
                repaired = replace(match)
                if repaired != body:
                    lines[index] = repaired + ending
                    changes.append({"line": index + 1, "kind": "nil_guard",
                                    "before": body.strip(), "after": repaired.strip(),
                                    "reason": "guarded a common optional MWSE reference or keybind before reading it"})
                break
    return "".join(lines), changes


def _guarded_nil_risk(line: str) -> bool:
    checks = (
        r"\b(ref|reference)\s+and\s+\1\.supportsLuaData\s+and\s+\1\.data\b",
        r"\be\s+and\s+e\.(?:target|reference|mobile|info|dialogue|element)\b",
        r"\b([A-Za-z_]\w*)\s+and\s+\1\.keyCode\b",
    )
    return any(re.search(pattern, line, re.I) for pattern in checks) or bool(
        re.search(r"\bif\s+\w+\s+then|\bassert\s*\(|\bor\s+return\b", line))


def _syntax_check(text: str, checker: str, checker_kind: str) -> str | None:
    """Check candidate text before replacing its original file."""
    fd, temp_name = tempfile.mkstemp(suffix=".lua", prefix="mwmanager_lua_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        if checker_kind == "luajit":
            command = [checker, "-b", temp_name, os.devnull]
        else:
            command = [checker, "-p", temp_name]
        result = subprocess.run(command, capture_output=True, text=True,
                                cwd=Path(checker).resolve().parent if checker_kind == "luajit" else None)
        return (result.stderr or result.stdout).strip() if result.returncode else None
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _bugreport_game_path(path_text: str, staging_text: str) -> str:
    """Render a Lua file location relative to the Morrowind installation."""
    path = Path(path_text)
    staging = Path(staging_text) if staging_text else None
    try:
        relative = path.relative_to(staging) if staging is not None else path
        parts = list(relative.parts)
    except (ValueError, OSError):
        parts = list(path.parts)
    mwse_index = next((i for i, part in enumerate(parts) if part.casefold() == "mwse"), None)
    if mwse_index is not None:
        parts = parts[mwse_index:]
    else:
        parts = [path.name]
    return str(PureWindowsPath("Data Files", *parts))


def _write_bugreport(path: Path, report: dict) -> None:
    relevant = [item for item in report["files"]
                if (item.get("changes", {}).get("modified") and item.get("changes", {}).get("details"))
                or any(change.get("kind") == "previous_repair"
                       for change in item.get("changes", {}).get("details", []))
                or item.get("remaining_issues") or item.get("error")
                or any(issue.get("kind") in {"repair_write_error", "repair_syntax_check"}
                       for issue in item.get("issues", []))]
    lines = [
        "MWSE Lua Repair Bug Report",
        "Generated by MWManager; review before sending to the MWSE team.",
        f"Generated: {report['generated']}",
        f"Files with changes or findings: {len(relevant)}",
        f"Files changed: {report['modified_files']}",
        "",
        "Automated edits are deliberately limited to guarded optional-value reads, extreme numeric priorities,",
        "and recurring mwse.log calls. Scheduling, UI lifecycle, and behavior-dependent findings are left for review.",
        "",
    ]
    if not relevant:
        lines.append("No MWSE Lua repairs or findings were recorded in this run.")
    for item in relevant:
        lines.append(f"FILE: {_bugreport_game_path(item['path'], report.get('staging', ''))}")
        if item.get("backup"):
            backup_parts = Path(item["backup"]).parts
            off_index = next((i for i, part in enumerate(backup_parts) if part.casefold() == "off"), None)
            if off_index is None:   # MWSE core originals: Data Files\MWSE\backups\...
                off_index = next((i for i, part in enumerate(backup_parts[:-1]) if part.casefold() == "mwse"
                                  and backup_parts[i + 1].casefold() == "backups"), None)
            backup_label = str(PureWindowsPath(*backup_parts[off_index:])) if off_index is not None else "MWManager backup folder"
            lines.append(f"Original backup: {backup_label}")
        for change in item.get("changes", {}).get("details", []):
            line = f"Line {change['line']}" if change.get("line") is not None else "Change"
            detail = f"  {line}: {change['kind']}: {change['description']}"
            if change.get("before") is not None:
                detail += f" | before: {change['before']} | after: {change['after']}"
            lines.append(detail)
        unresolved = list(item.get("remaining_issues", item.get("issues", [])))
        unresolved.extend(issue for issue in item.get("issues", [])
                          if issue.get("kind") in {"repair_write_error", "repair_syntax_check"}
                          and issue not in unresolved)
        if unresolved:
            lines.append("Review findings remaining:")
            lines.extend(f"  Line {issue.get('line', '?')}: {issue['kind']}: {issue['message']}" for issue in unresolved)
        if item.get("error"):
            lines.append(f"  Scan error: {item['error']}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def process(staging: Path, *, apply: bool, report_path: Path, only: list[str] | None = None,
            bugreport_path: Path | None = None, backup_root: Path | None = None,
            exclude: list[str] | None = None, backup_relative_to: Path | None = None,
            prior_backup_roots: list[Path] | None = None) -> int:
    files = sorted((p for p in staging.rglob("*.lua") if _is_mwse_lua(p)), key=lambda p: str(p).casefold())
    if only:
        files = [p for p in files if any(fnmatch.fnmatch(p.relative_to(staging).parts[0].casefold(), pat.casefold())
                                         for pat in only)]
    if exclude:
        skip = [tuple(part.casefold() for part in re.split(r"[\\/]+", ex.strip("\\/")) if part) for ex in exclude]
        files = [p for p in files
                 if not any(tuple(part.casefold() for part in p.relative_to(staging).parts[:len(s)]) == s
                            for s in skip if s)]
    report: dict = {"staging": str(staging), "mode": "apply" if apply else "plan", "files": [],
                    "generated": time.strftime("%Y-%m-%d %H:%M:%S"), "modified_files": 0}
    syntax_checker = shutil.which("luac") or shutil.which("luac5.1")
    syntax_checker_kind = "luac" if syntax_checker else ""
    if not syntax_checker:
        bundled_checker = Path(__file__).resolve().parent / "tools" / "luajit" / ("luajit.exe" if os.name == "nt" else "luajit")
        if bundled_checker.is_file():
            syntax_checker = str(bundled_checker)
            syntax_checker_kind = "luajit"
    for path in files:
        try:
            raw = path.read_bytes()
            has_bom = raw.startswith(b"\xef\xbb\xbf")
            original = raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            report["files"].append({"path": str(path), "error": str(exc)})
            continue
        original_issues, spam_lines, spam = _analysis(path, original)
        updated, changes = _safe_nil_repairs(original)
        package, _module = _module_root(path, staging)
        try:
            package_relative = path.relative_to(package)
        except ValueError:
            package_relative = path.relative_to(staging)
        plugins = [p for p in package.iterdir()
                   if p.is_file() and p.suffix.casefold() in {".esp", ".esm"}] if package.is_dir() else []
        plugins.sort(key=lambda p: (p.suffix.casefold() != ".esm", p.stem.casefold()))
        plugin_owner = plugins[0].stem if plugins else package.name
        try:
            from mwmanager_install import staging_package_name
            owner = staging_package_name(str(package))
        except (ImportError, AttributeError, OSError):
            owner = package.name
        owner = re.sub(r'[<>:"/\\|?*]', "_", owner).rstrip(" .") or package.name
        prior_backups = [package / "Off" / "LuaRepair" / package_relative,
                         package / "Off" / owner / "LuaRepairs" / package_relative,
                         package / "Off" / plugin_owner / "LuaRepairs" / package_relative,
                         package / "Off" / package.name / "LuaRepairs" / package_relative]
        if backup_root is not None:   # an original kept by an earlier run in this or an older central location
            prior_backups.insert(0, backup_root / path.relative_to(backup_relative_to or staging))
        older_backups = [root / path.relative_to(staging) for root in (prior_backup_roots or [])]
        prior_backups.extend(older_backups)
        prior_backup = next((backup for backup in prior_backups if backup.is_file()), None)
        if prior_backup is not None:
            try:
                before = prior_backup.read_text(encoding="utf-8-sig").splitlines()
                after = original.splitlines()
                if before != after:
                    diff_lines = list(difflib.unified_diff(before, after,
                                                           fromfile="original backup", tofile="active Lua",
                                                           lineterm=""))
                    if len(diff_lines) > 120:
                        diff_lines = diff_lines[:120] + ["... diff truncated after 120 lines ..."]
                    changes.append({"line": None, "kind": "previous_repair",
                                    "description": "Previously applied Lua repair, reconstructed from its retained Off backup:\n"
                                                   + "\n".join(diff_lines)})
            except (OSError, UnicodeError):
                pass
        original_lines = original.splitlines()
        for match in PRIORITY.finditer(original):
            value = int(match.group(2))
            if abs(value) > 10000:
                line_no = original.count("\n", 0, match.start()) + 1
                changes.append({"line": line_no, "kind": "extreme_priority",
                                "before": match.group(2), "after": "10000",
                                "description": f"normalized priority {match.group(2)} to 10000"})
        updated, _priority_matches = PRIORITY.subn(
            lambda m: m.group(1) + "10000" if abs(int(m.group(2))) > 10000 else m.group(0), updated)
        if spam:
            _package, module = _module_root(path, staging)
            src_lines = updated.splitlines(keepends=True)
            _, current_spam, _ = _analysis(path, updated)
            for index in sorted(current_spam, reverse=True):
                count = len(MWSE_LOG.findall(src_lines[index]))
                src_lines[index] = MWSE_LOG.sub("mwmanagerLog(", src_lines[index])
                changes.append({"line": index + 1, "kind": "core_log_spam",
                                "description": f"redirected {count} recurring mwse.log call(s) to this module's MWManager log"})
            updated = _logger_block(module.name) + "".join(src_lines)
            changes.append({"line": 1, "kind": "module_logger_added",
                            "description": f"added a local file-logging wrapper for {module.name}"})
        file_changed = updated != original
        candidate_error = _syntax_check(updated, syntax_checker, syntax_checker_kind) if syntax_checker and file_changed else None
        backup = None
        blocked = False
        if candidate_error:
            original_issues.append({"line": None, "kind": "repair_syntax_check",
                                    "message": f"Automatic edits were not applied because luac rejected the candidate: {candidate_error}"})
            blocked = True
        if apply and file_changed and not blocked:
            relative = path.relative_to(staging)
            if backup_root is not None:
                backup_path = backup_root / path.relative_to(backup_relative_to or staging)
            else:
                try:
                    relative = path.relative_to(package)
                except ValueError:
                    relative = path.relative_to(staging)
                backup_path = package / "Off" / owner / "LuaRepairs" / relative
            try:
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                if not backup_path.exists():         # keep the first original; never overwrite it
                    shutil.copy2(path, backup_path)
                payload = updated.encode("utf-8")
                if has_bom:
                    payload = b"\xef\xbb\xbf" + payload
                path.write_bytes(payload)
                backup = str(backup_path)
                report["modified_files"] += 1
            except OSError as exc:
                original_issues.append({"line": None, "kind": "repair_write_error",
                                        "message": f"Could not save repaired Lua or backup: {exc}"})
                blocked = True
        remaining_issues, _remaining_spam, _ = _analysis(path, updated if apply and file_changed and not blocked else original)
        if not apply and file_changed:
            remaining_issues, _remaining_spam, _ = _analysis(path, updated)
        details = []
        if changes:
            for change in changes:
                description = change.get("description") or change.get("reason") or change["kind"].replace("_", " ")
                details.append({**change, "description": description})
        record = {"path": str(path), "issues": original_issues, "remaining_issues": remaining_issues,
                  "changes": {"details": details, "planned": bool(file_changed and not blocked),
                              "modified": bool(apply and file_changed and not blocked)},
                  "backup": backup or (str(prior_backup) if prior_backup else None)}
        report["files"].append(record)
    report["syntax_checker"] = syntax_checker or "not installed; syntax validation skipped"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if bugreport_path is not None:
        if bugreport_path.suffix.casefold() == ".txt":
            _write_bugreport(bugreport_path, report)
            print(f"  Sendable bug report: {bugreport_path}")
        else:
            # Keep sendable repair notes with each staged package. Passing lint
            # files are excluded by _write_bugreport's relevant-record filter.
            grouped: dict[Path, list[dict]] = {}
            for item in report["files"]:
                package, _module = _module_root(Path(item["path"]), staging)
                grouped.setdefault(package, []).append(item)
            for package, records in grouped.items():
                package_report = {**report, "files": records,
                                  "modified_files": sum(bool(r.get("changes", {}).get("modified"))
                                                         or any(change.get("kind") == "previous_repair"
                                                                for change in r.get("changes", {}).get("details", []))
                                                         for r in records)}
                relevant = any(
                    (r.get("changes", {}).get("modified") and r.get("changes", {}).get("details"))
                    or any(change.get("kind") == "previous_repair"
                           for change in r.get("changes", {}).get("details", []))
                    or r.get("remaining_issues") or r.get("error")
                    or any(issue.get("kind") in {"repair_write_error", "repair_syntax_check"}
                           for issue in r.get("issues", []))
                    for r in records
                )
                if not relevant:
                    continue
                plugins = [p for p in package.iterdir()
                           if p.is_file() and p.suffix.casefold() in {".esp", ".esm"}] \
                    if package.is_dir() else []
                plugins.sort(key=lambda p: (p.suffix.casefold() != ".esm", p.stem.casefold()))
                label = plugins[0].stem if len(plugins) == 1 else package.name
                try:
                    from mwdialog_assets import nexus_id, vortex_mod_metadata
                    metadata = vortex_mod_metadata(str(package))
                    nexus = metadata.get("mod_id") or nexus_id(str(package))
                except (ImportError, AttributeError, OSError):
                    nexus = None
                nexus_suffix = f" [Nexus {nexus}]" if nexus else ""
                destination = package / "bugreports" / f"{label}{nexus_suffix}.mwse.bugreport.txt"
                _write_bugreport(destination, package_report)
                print(f"  Sendable bug report: {destination}")
    finding_count = sum(len(item.get("remaining_issues", [])) + bool(item.get("error")) for item in report["files"])
    print(f"MWSE Lua review: {len(files)} Lua file(s), {finding_count} finding(s), {report['modified_files']} file(s) repaired.")
    print(f"  JSON report: {report_path}")
    if not apply and any(item.get("changes", {}).get("planned") for item in report["files"]):
        print("  Plan only; safe repairs are listed but no files were edited.")
    return 1 if any(item.get("error") for item in report["files"]) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staging", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", nargs="+", help="restrict to these staged package folders (wildcards allowed)")
    ap.add_argument("--bugreport", help="write a plain-text report suitable for review and sharing")
    ap.add_argument("--backup-root", help="store originals under this directory instead of each package's Off/<plugin>/LuaRepairs")
    ap.add_argument("--backup-relative-to", help="with --backup-root: mirror each file's path relative to this folder "
                                                 "(default: --staging)")
    ap.add_argument("--prior-backup-root", action="append", default=[],
                    help="an older --backup-root (laid out relative to --staging), read to report earlier repairs; "
                         "its files are never moved or overwritten")
    ap.add_argument("--exclude", nargs="+", help="skip these paths relative to --staging (e.g. MWSE/mods)")
    args = ap.parse_args()
    return process(Path(args.staging).resolve(), apply=args.apply, report_path=Path(args.report).resolve(),
                   only=args.only, bugreport_path=Path(args.bugreport).resolve() if args.bugreport else None,
                   backup_root=Path(args.backup_root).resolve() if args.backup_root else None,
                   exclude=args.exclude,
                   backup_relative_to=Path(args.backup_relative_to).resolve() if args.backup_relative_to else None,
                   prior_backup_roots=[Path(p).resolve() for p in args.prior_backup_root])


if __name__ == "__main__":
    raise SystemExit(main())
