"""Compare two staged package versions without changing either one."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from mwdialog_tes3 import FormatError, iter_records, record_id
from plugin_install import SKIP_DIRS


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_inventory(package: Path) -> dict[str, dict]:
    package = Path(package).resolve()
    found = {}
    for base, dirs, files in os.walk(package):
        dirs[:] = [name for name in dirs if name.casefold() not in SKIP_DIRS and not name.startswith(".")]
        for name in sorted(files, key=str.casefold):
            path = Path(base) / name
            if name.startswith(".") or path.is_symlink():
                continue
            relative = path.relative_to(package).as_posix()
            found[relative.casefold()] = {
                "relative": relative,
                "size": path.stat().st_size,
                "sha256": sha256(path),
                "extension": path.suffix.casefold(),
            }
    return found


def plugin_inventory(package: Path) -> dict[str, dict]:
    plugins = {}
    for base, dirs, files in os.walk(package):
        dirs[:] = [name for name in dirs if name.casefold() not in SKIP_DIRS and not name.startswith(".")]
        for filename in files:
            path = Path(base) / filename
            if path.suffix.casefold() not in {".esp", ".esm"}:
                continue
            records = {}
            errors = []
            try:
                for ordinal, (_offset, tag, flags, body) in enumerate(iter_records(path.read_bytes())):
                    rid = record_id(tag, body)
                    key = (tag, (rid or "").casefold() if isinstance(rid, str) else "", ordinal)
                    records[key] = {
                        "tag": tag,
                        "id": rid,
                        "flags": flags,
                        "body_sha256": hashlib.sha256(body).hexdigest(),
                        "ordinal": ordinal,
                    }
            except (OSError, FormatError, ValueError) as exc:
                errors.append(str(exc))
            plugins[path.name.casefold()] = {
                "name": path.name,
                "path": str(path),
                "sha256": sha256(path),
                "records": records,
                "errors": errors,
            }
    return plugins


def compare(old_package: Path, new_package: Path) -> dict:
    old_files, new_files = file_inventory(old_package), file_inventory(new_package)
    added = sorted(set(new_files) - set(old_files))
    removed = sorted(set(old_files) - set(new_files))
    changed = sorted(key for key in set(old_files) & set(new_files)
                     if old_files[key]["sha256"] != new_files[key]["sha256"])
    old_plugins, new_plugins = plugin_inventory(old_package), plugin_inventory(new_package)
    plugin_changes = []
    for name in sorted(set(old_plugins) | set(new_plugins), key=str.casefold):
        old, new = old_plugins.get(name), new_plugins.get(name)
        if not old:
            plugin_changes.append({"plugin": new["name"], "status": "added"})
            continue
        if not new:
            plugin_changes.append({"plugin": old["name"], "status": "removed"})
            continue
        old_records, new_records = old["records"], new["records"]
        by_id = lambda records: {(tag, rid): value for (tag, rid, _ordinal), value in records.items()}
        old_by_id, new_by_id = by_id(old_records), by_id(new_records)
        added_records = sorted(set(new_by_id) - set(old_by_id))
        removed_records = sorted(set(old_by_id) - set(new_by_id))
        changed_records = sorted(key for key in set(old_by_id) & set(new_by_id)
                                 if old_by_id[key]["body_sha256"] != new_by_id[key]["body_sha256"])
        plugin_changes.append({
            "plugin": new["name"],
            "status": "unchanged" if old["sha256"] == new["sha256"] else "changed",
            "records_added": len(added_records),
            "records_removed": len(removed_records),
            "records_changed": len(changed_records),
            "changed_record_ids": [{"tag": tag, "id": rid} for tag, rid in changed_records[:200]],
            "errors": sorted(set(old["errors"] + new["errors"])),
        })
    return {
        "old_package": str(Path(old_package).resolve()),
        "new_package": str(Path(new_package).resolve()),
        "files": {
            "added": [new_files[key]["relative"] for key in added],
            "removed": [old_files[key]["relative"] for key in removed],
            "changed": [new_files[key]["relative"] for key in changed],
            "unchanged": len(set(old_files) & set(new_files)) - len(changed),
        },
        "plugins": plugin_changes,
    }


def risk_score(impact: dict) -> int:
    """Small ordering score; details remain authoritative."""
    score = len(impact["files"]["removed"]) * 3 + len(impact["files"]["changed"])
    for plugin in impact["plugins"]:
        score += int(plugin.get("records_removed", 0) * 5)
        score += int(plugin.get("records_changed", 0) * 2)
        score += 20 if plugin.get("status") == "removed" else 0
    return score
