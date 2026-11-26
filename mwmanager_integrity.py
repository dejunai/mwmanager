"""Read-only drift and ownership checks for deployed plugin files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# statuses that are not drift: "disabled" is a deployed plugin MWManager itself moved to disabled_plugins (the
# CR-28 stress run counted every newly deployed, not yet enabled plugin as "missing")
OK_STATUSES = frozenset({"ok", "disabled"})


def _disabled_copy(manager_dir: Path, data_dir: Path, target: Path) -> Path | None:
    """Where MWManager keeps the plugin `target` while it is disabled (its own folder, then the older place)."""
    for folder in (manager_dir / "disabled_plugins", data_dir / "Off" / "Disabled Plugins"):
        candidate = folder / target.name
        if candidate.is_file():
            return candidate
    return None


def _states(manager_dir: Path) -> list[tuple[str, dict]]:
    return [
        ("plugin", _read_json(manager_dir / "plugin_deployments.json")),
        ("major-master", _read_json(manager_dir / "major_master_deployments.json")),
    ]


def _operation_expected(operation: dict, target: Path) -> tuple[Path | None, str | None]:
    source = Path(operation["source"]) if operation.get("source") else None
    expected = str(operation.get("sha256") or "").lower() or None
    if not expected and source and source.is_file():
        try:
            if not target.is_file() or not os.path.samefile(source, target):
                expected = sha256(source)
        except OSError:
            expected = None
    return source, expected


def inspect(manager_dir: Path, data_dir: Path, *, include_untracked: bool = False) -> dict:
    """Compare every tracked deployment operation against its source or recorded hash."""
    manager_dir, data_dir = Path(manager_dir).resolve(), Path(data_dir).resolve()
    candidates = {}
    for deployment_kind, state in _states(manager_dir):
        deployments = state.get("deployments")
        if not isinstance(deployments, list):
            continue
        for deployment_index, deployment in enumerate(deployments):
            for operation_index, operation in enumerate(deployment.get("operations") or []):
                if not operation.get("target"):
                    continue
                target = Path(operation["target"]).resolve()
                source, expected = _operation_expected(operation, target)
                item = {
                    "kind": deployment_kind,
                    "deployment_id": deployment.get("id"),
                    "label": deployment.get("label"),
                    "target": str(target),
                    "source": str(source.resolve()) if source else None,
                    "status": "ok",
                    "expected_sha256": expected,
                    "deployed_stat": ((operation.get("size"), operation.get("mtime_ns"))
                                      if operation.get("size") is not None and operation.get("mtime_ns") else None),
                    "deployed_at": deployment.get("deployed_at"),
                    "deployment_index": deployment_index,
                    "operation_index": operation_index,
                }
                candidates.setdefault(os.path.normcase(str(target)), []).append(item)
    results, tracked_targets = [], set(candidates)
    for candidate_items in candidates.values():
        target = Path(candidate_items[-1]["target"])
        actual = None
        if target.is_file():
            try:
                actual = sha256(target)
            except OSError:
                actual = None
        ambiguous = False
        stamped = [candidate for candidate in candidate_items if candidate.get("deployed_at")]
        if stamped:
            item = dict(max(stamped, key=lambda candidate: (
                str(candidate["deployed_at"]), candidate["kind"],
                int(candidate["deployment_index"]), int(candidate["operation_index"]))))
        elif len({candidate["kind"] for candidate in candidate_items}) == 1:
            item = dict(max(candidate_items, key=lambda candidate: (
                int(candidate["deployment_index"]), int(candidate["operation_index"]))))
        else:
            item = dict(candidate_items[-1])
            ambiguous = True
        source = Path(item["source"]) if item.get("source") else None
        expected = item.pop("expected_sha256", None)
        deployed_stat = item.pop("deployed_stat", None)
        item.pop("deployed_at", None)
        item.pop("deployment_index", None)
        item.pop("operation_index", None)
        try:
            target.relative_to(data_dir)
        except ValueError:
            item.update(status="unsafe-state", detail="target is outside Data Files")
            results.append(item)
            continue
        if ambiguous:
            item.update(status="ownership-ambiguous",
                        detail="historical deployment records lack an ordering timestamp")
        elif not target.exists() and _disabled_copy(manager_dir, data_dir, target):
            item.update(status="disabled", detail="disabled by MWManager (kept in disabled_plugins)")
        elif not target.exists():
            item.update(status="missing", detail="deployed target is missing")
        elif not target.is_file():
            item.update(status="wrong-type", detail="target is not a regular file")
        elif actual is None:
            item.update(status="unreadable", detail="target hash could not be read")
        elif expected:
            if actual != expected:
                item.update(status="modified", expected_sha256=expected,
                            actual_sha256=actual)
            elif source and source.is_file():
                try:
                    if os.path.samefile(source, target):
                        item["link"] = "same-file"
                    else:
                        item["link"] = "copy"
                except OSError:
                    item["link"] = "unknown"
        elif deployed_stat:
            # current deployments record the size and time as deployed instead of a hash (plugin_install): a
            # hard-linked file changes both names at once, so a change shows as another size or time
            now = target.stat()
            if (now.st_size, now.st_mtime_ns) != tuple(deployed_stat):
                item.update(status="modified", detail="size or modification time differs from the deployment")
        else:
            item.update(status="unverifiable",
                        detail="deployment predates content hashing and the live source is hard-linked")
        results.append(item)
    untracked = []
    if include_untracked:
        for path in data_dir.rglob("*"):
            if path.is_file() and os.path.normcase(str(path.resolve())) not in tracked_targets:
                untracked.append(str(path))
        untracked.sort(key=str.casefold)
    counts = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"data_dir": str(data_dir), "files_checked": len(results), "counts": counts,
            "items": sorted(results, key=lambda item: (item["status"], item["target"].casefold())),
            "untracked": untracked}


def changed_only(report: dict) -> list[dict]:
    return [item for item in report.get("items", [])
            if item.get("status") not in OK_STATUSES]


def render(report: dict) -> list[str]:
    lines = [f"Integrity: {report['files_checked']} tracked file(s) checked."]
    for status, count in sorted(report.get("counts", {}).items()):
        lines.append(f"  {status}: {count}")
    for item in changed_only(report):
        relative = Path(item["target"]).name
        lines.append(f"  {item['status'].upper()}: {relative} ({item.get('label') or item.get('deployment_id')})")
    if report.get("untracked"):
        lines.append(f"  untracked: {len(report['untracked'])} file(s)")
    return lines


def capture_snapshot(manager_dir: Path, data_dir: Path, name: str) -> Path:
    """Capture a regression manifest for a game folder without retaining file contents."""
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .") or "snapshot"
    root = Path(data_dir).resolve()
    files = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        files[relative] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                           "sha256": sha256(path)}
    state = {"version": 1, "name": name, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "data_dir": str(root), "files": files}
    destination = Path(manager_dir) / "snapshots" / f"{clean}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def compare_snapshot(manager_dir: Path, data_dir: Path, name: str) -> dict:
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .") or "snapshot"
    path = Path(manager_dir) / "snapshots" / f"{clean}.json"
    saved = _read_json(path)
    old = saved.get("files") if isinstance(saved.get("files"), dict) else None
    if old is None:
        raise ValueError(f"Snapshot is missing or invalid: {name}")
    data_dir = Path(data_dir).resolve()
    current = {path.relative_to(data_dir).as_posix(): sha256(path)
               for path in sorted(data_dir.rglob("*"), key=lambda item: item.as_posix().casefold())
               if path.is_file()}
    added = sorted(set(current) - set(old), key=str.casefold)
    removed = sorted(set(old) - set(current), key=str.casefold)
    changed = sorted((key for key in set(old) & set(current)
                      if old[key].get("sha256") != current[key]), key=str.casefold)
    return {"snapshot": name, "added": added, "removed": removed,
            "changed": changed, "unchanged": len(set(old) & set(current)) - len(changed)}
