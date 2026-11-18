"""Deterministic, read-only installation plans for staged plugin packages."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from plugin_install import SKIP_DIRS, check_deploy_path_lengths
from mwdialog_mastercheck import header_masters


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pending_options(manager_dir: Path, package: Path) -> dict:
    try:
        all_pending = json.loads((manager_dir / "outputs" / "mwmanager_pending_options.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return all_pending.get(package.name) or {}


def build(item: dict, package: Path, data_dir: Path, manager_dir: Path) -> dict:
    """Describe the exact file collision/dependency plan without mutating package or game data."""
    package, data_dir, manager_dir = (Path(path).resolve()
                                      for path in (package, data_dir, manager_dir))
    package.relative_to(manager_dir)
    selected = [str(name) for name in item.get("selected_plugins", item.get("plugins", []))]
    selected_keys = {name.casefold() for name in selected}
    operations, plugins, warnings = [], [], []
    try:
        check_deploy_path_lengths(package, data_dir, item.get("archive_label") or package.name)
    except ValueError as exc:
        warnings.append(str(exc))

    present = {path.name.casefold(): path for path in data_dir.iterdir() if path.is_file()}
    present.update({name.casefold(): package / name for name in selected})
    for base, dirs, files in os.walk(package):
        dirs[:] = [name for name in dirs if name.casefold() not in SKIP_DIRS and not name.startswith(".")]
        for filename in files:
            if Path(filename).suffix.casefold() in {".esp", ".esm"}:
                present[filename.casefold()] = Path(base) / filename
    for base, dirs, files in os.walk(package):
        base_path = Path(base)
        dirs[:] = [name for name in dirs if name.casefold() not in SKIP_DIRS and not name.startswith(".")]
        for filename in sorted(files, key=str.casefold):
            source = base_path / filename
            if filename.startswith(".") or source.is_symlink():
                continue
            rel = source.relative_to(package)
            target_rel = Path(filename) if source.suffix.casefold() in {".esp", ".esm", ".bsa"} else rel
            target = data_dir / target_rel
            digest = _sha256(source)
            action = "create"
            if target.exists():
                if target.is_file() and _sha256(target) == digest:
                    action = "same-content"
                elif target.is_file():
                    action = "replace"
                else:
                    action = "blocked"
                    warnings.append(f"A directory blocks deployment of {target_rel}")
            operations.append({
                "source": str(source),
                "target": str(target),
                "relative_target": target_rel.as_posix(),
                "size": source.stat().st_size,
                "sha256": digest,
                "action": action,
            })
            if source.suffix.casefold() in {".esp", ".esm"} and (
                    not selected_keys or source.name.casefold() in selected_keys):
                try:
                    masters = header_masters(source.read_bytes())
                except OSError:
                    masters = []
                plugins.append({
                    "name": source.name,
                    "path": str(source),
                    "masters": [{"name": name, "recorded_size": recorded_size}
                                for name, recorded_size in masters],
                    "missing_masters": [
                        name for name, _recorded_size in masters if name.casefold() not in present
                    ],
                })

    pending = _pending_options(manager_dir, package)
    held = [entry for entry in pending.values()
            if isinstance(entry, dict) and entry.get("status") == "waiting"]
    if held:
        warnings.append(f"{len(held)} installer option(s) are waiting for missing dependencies.")
    counts = {}
    for operation in operations:
        counts[operation["action"]] = counts.get(operation["action"], 0) + 1
    missing_masters = sorted({master for plugin in plugins
                              for master in plugin["missing_masters"]}, key=str.casefold)
    if missing_masters:
        warnings.append("Missing plugin masters: " + ", ".join(missing_masters))
    return {
        "version": 1,
        "archive": str(Path(item.get("archive", ""))) if item.get("archive") else None,
        "package": str(package),
        "selected_plugins": selected,
        "operation_counts": counts,
        "operations": operations,
        "plugins": plugins,
        "missing_masters": missing_masters,
        "pending_options": pending,
        "warnings": warnings,
        "blocked": bool(missing_masters or held or any(operation["action"] == "blocked"
                                                       for operation in operations)),
    }


def render(plan: dict) -> list[str]:
    counts = plan.get("operation_counts") or {}
    lines = [
        f"Install plan for {Path(plan['package']).name}",
        "  " + ", ".join(f"{count} {action}" for action, count in sorted(counts.items())),
        "  plugins: " + (", ".join(plan.get("selected_plugins") or []) or "none"),
    ]
    for warning in plan.get("warnings") or []:
        lines.append("  WARNING: " + warning)
    lines.append("  " + ("BLOCKED" if plan.get("blocked") else "READY"))
    return lines
