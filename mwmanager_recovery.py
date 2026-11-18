"""Read-only recovery discovery and narrowly scoped recovery operations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from workflow_safety import DurableTransaction, atomic_json, mutation_lock


VERSION = 1
STAGE_NAMES = (".mwmanager-pack", ".mwmanager-unpack")


def _safe_label(value: Any, fallback: str = "Recovery item") -> str:
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ")
    text = "".join(character if character.isprintable() else " " for character in text)
    text = " ".join(text.split()).strip()
    return (text[:157] + "...") if len(text) > 160 else (text or fallback)


def _identifier(kind: str, value: str) -> str:
    return f"{kind}:{hashlib.sha256(value.encode('utf-8', 'replace')).hexdigest()[:20]}"


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _read_object(path: Path, list_key: str) -> tuple[dict | None, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": VERSION, list_key: []}, []
    except (OSError, UnicodeError, ValueError) as exc:
        return None, [f"{path.name} is unreadable: {_safe_label(exc, 'invalid data')}"]
    if not isinstance(value, dict) or not isinstance(value.get(list_key), list):
        return None, [f"{path.name} has an invalid {list_key} list"]
    return value, []


def _problem_item(source: str, message: str, index: int) -> dict:
    return {
        "id": _identifier("problem", f"{source}|{index}|{message}"),
        "kind": "problem",
        "source": source,
        "label": _safe_label(f"{source}: {message}"),
        "status": "needs-review",
        "recoverable": False,
        "action": "review",
        "details": {"message": message},
    }


def _plugin_state_items(manager: Path) -> list[dict]:
    state, problems = _read_object(manager / "plugin_deployments.json", "deployments")
    items = []
    if state is None:
        return [_problem_item("plugin deployment state", message, index)
                for index, message in enumerate(problems)]
    deployment_ids = [str(entry.get("id") or "") for entry in state["deployments"]
                      if isinstance(entry, dict)]
    for index, deployment in enumerate(state["deployments"]):
        if not isinstance(deployment, dict):
            items.append(_problem_item("plugin deployment state", f"entry {index + 1} is invalid", index))
            continue
        deployment_id = str(deployment.get("id") or "")
        label = _safe_label(deployment.get("label"), f"Plugin deployment {index + 1}")
        plugins = [str(value) for value in deployment.get("plugins", [])
                   if isinstance(value, (str, int, float))] if isinstance(
                       deployment.get("plugins", []), list) else []
        operations = deployment.get("operations")
        operations_count = len(operations) if isinstance(operations, list) else 0
        valid = bool(deployment_id and isinstance(operations, list)
                     and deployment_ids.count(deployment_id) == 1)
        items.append({
            "id": f"plugin:{deployment_id}" if valid else _identifier(
                "plugin-invalid", f"{index}|{label}"),
            "kind": "plugin_deployment",
            "source": "plugin_deployments.json",
            "label": label,
            "status": "deployed" if valid else "needs-review",
            "recoverable": valid,
            "action": "remove_plugin_deployment" if valid else "review",
            "details": {"deployment_id": deployment_id or None,
                        "plugins": sorted(plugins, key=str.casefold),
                        "operations": operations_count,
                        "staging_package": str(deployment.get("staging_package") or "")},
        })
    return items


def _major_state_items(manager: Path) -> list[dict]:
    state, problems = _read_object(manager / "major_master_deployments.json", "deployments")
    if state is None:
        return [_problem_item("major-master deployment state", message, index)
                for index, message in enumerate(problems)]
    deployments = state["deployments"]
    invalid = [index + 1 for index, entry in enumerate(deployments) if not isinstance(entry, dict)]
    if invalid:
        return [_problem_item("major-master deployment state",
                              f"entries {', '.join(map(str, invalid))} are invalid", 0)]
    invalid_fields = [
        index + 1 for index, entry in enumerate(deployments)
        if not isinstance(entry.get("masters", []), list)
        or not isinstance(entry.get("operations", []), list)
        or not isinstance(entry.get("moves", []), list)
    ]
    if invalid_fields:
        return [_problem_item("major-master deployment state",
                              f"entries {', '.join(map(str, invalid_fields))} have invalid fields", 0)]
    if not deployments:
        return []
    masters = sorted({str(name) for entry in deployments for name in entry.get("masters", [])
                      if isinstance(entry.get("masters"), list)
                      and isinstance(name, (str, int, float))}, key=str.casefold)
    operations = sum(len(entry.get("operations", [])) for entry in deployments
                     if isinstance(entry.get("operations"), list))
    moves = sum(len(entry.get("moves", [])) for entry in deployments
                if isinstance(entry.get("moves"), list))
    return [{
        "id": "major-master:deployments",
        "kind": "major_master_deployment",
        "source": "major_master_deployments.json",
        "label": _safe_label(f"Major masters: {', '.join(masters)}", "Major-master deployments"),
        "status": "deployed",
        "recoverable": True,
        "action": "remove_major_master_deployments",
        "details": {"deployments": len(deployments), "masters": masters,
                    "operations": operations, "moves": moves},
    }]


def _candidate_transaction_roots(manager: Path) -> list[tuple[Path, Path]]:
    roots: list[tuple[Path, Path]] = []
    transaction_dir = manager / "transactions"
    if transaction_dir.is_dir():
        for root in sorted(transaction_dir.iterdir(), key=lambda path: path.name.casefold()):
            if root.is_dir():
                roots.append((root, manager))
    for staging_name in ("plugins", "major_masters"):
        staging = manager / "staging" / staging_name
        if not staging.is_dir():
            continue
        for package in sorted(staging.iterdir(), key=lambda path: path.name.casefold()):
            if not package.is_dir():
                continue
            for name in STAGE_NAMES:
                root = package / name
                if root.is_dir():
                    roots.append((root, package))
    return roots


def _transaction_items(manager: Path) -> list[dict]:
    items = []
    for root, allowed in _candidate_transaction_roots(manager):
        identity = _relative(manager, root)
        state_path = root / "state.json"
        state: dict | None = None
        error = None
        try:
            if root.is_symlink() or not root.resolve().is_relative_to(manager.resolve()):
                error = "transaction root escapes the manager directory"
        except OSError:
            error = "transaction root cannot be resolved safely"
        if error is None:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    raise ValueError("state is not an object")
            except FileNotFoundError:
                try:
                    if any(root.iterdir()):
                        error = "transaction state is missing from a non-empty root"
                except OSError:
                    error = "transaction root cannot be inspected safely"
            except (OSError, UnicodeError, ValueError) as exc:
                error = f"transaction state is unreadable: {_safe_label(exc, 'invalid data')}"
        valid = error is None and (
            state is None or (state.get("version") == 1
                              and state.get("phase") in {"active", "committed"}))
        if valid and state and not isinstance(state.get("operations", []), list):
            valid = False
            error = "transaction operations list is invalid"
        operation_count = len(state.get("operations", [])) if isinstance(
            state and state.get("operations"), list) else 0
        intents = root / "operations.jsonl"
        if valid and intents.is_file():
            try:
                lines = intents.read_bytes().splitlines(keepends=True)
                operations = [json.loads(line) for line in lines if line.endswith(b"\n")]
                operation_count = len(operations)
                if any(not isinstance(operation, dict) for operation in operations):
                    raise ValueError("intent is not an object")
            except (OSError, UnicodeError, ValueError):
                valid = False
                error = "transaction intent log is invalid or unreadable"
        items.append({
            "id": _identifier("transaction", identity),
            "kind": "transaction",
            "source": identity,
            "label": _safe_label(f"Interrupted transaction: {identity}"),
            "status": (state or {}).get("phase", "incomplete") if valid else "needs-review",
            "recoverable": valid,
            "action": "recover_transaction" if valid else "review",
            "details": {"root": str(root), "phase": (state or {}).get("phase", "incomplete"),
                        "operations": operation_count, "problem": error},
        })
    return items


def _ready_stage_items(manager: Path) -> list[dict]:
    path = manager / "staging" / "plugins" / ".mwmanager-plugin-stages.json"
    state, problems = _read_object(path, "ready")
    if state is None:
        return [_problem_item("ready plugin stages", message, index)
                for index, message in enumerate(problems)]
    root = (manager / "staging" / "plugins").resolve()
    items = []
    for index, entry in enumerate(state["ready"]):
        if not isinstance(entry, dict):
            items.append(_problem_item("ready plugin stages", f"entry {index + 1} is invalid", index))
            continue
        package = Path(str(entry.get("staging_package") or ""))
        try:
            resolved = package.resolve()
            safe = resolved.is_relative_to(root) and resolved != root
        except (OSError, ValueError):
            safe = False
        archive = _safe_label(Path(str(entry.get("archive") or "")).name,
                              package.name if package.name else f"Plugin stage {index + 1}")
        valid_plugins = isinstance(entry.get("selected_plugins"), list)
        status = "ready" if safe and package.is_dir() and entry.get("archive") and valid_plugins else "stale"
        items.append({
            "id": _identifier("stage", f"{index}|{package}"),
            "kind": "ready_plugin_stage",
            "source": path.name,
            "label": _safe_label(f"Ready plugin stage: {archive}"),
            "status": status,
            "recoverable": safe,
            "action": "forget_ready_plugin_stage" if safe else "review",
            "details": {"staging_package": str(package), "archive": str(entry.get("archive") or ""),
                        "selected_plugins": sorted(
                            (str(value) for value in entry.get("selected_plugins", [])),
                            key=str.casefold) if valid_plugins else [],
                        "manifest_index": index},
        })
    return items


def _journal_items(manager: Path) -> list[dict]:
    from mwmanager_journal import Journal

    journal = Journal(manager / "journal")
    problems = []
    try:
        _entries, entry_problems = journal.entries()
        problems.extend(entry_problems)
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, KeyError) as exc:
        problems.append(f"install.jsonl cannot be parsed safely: {_safe_label(exc, 'invalid data')}")
    try:
        _runs, run_problems = journal._read_jsonl(journal.runs)
        problems.extend(run_problems)
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, KeyError) as exc:
        problems.append(f"runs.jsonl cannot be parsed safely: {_safe_label(exc, 'invalid data')}")
    problems = sorted(problems)
    return [_problem_item("journal", message, index)
            for index, message in enumerate(problems)]


def build_recovery_inventory(manager_dir: str | Path, game_dir: str | Path | None = None,
                             staging: str | Path | None = None) -> dict:
    """Return a deterministic inventory without changing staging, journal, or game files."""
    manager = Path(manager_dir).resolve()
    items = (
        _plugin_state_items(manager)
        + _major_state_items(manager)
        + _transaction_items(manager)
        + _ready_stage_items(manager)
        + _journal_items(manager)
    )
    items.sort(key=lambda item: (
        {"transaction": 0, "plugin_deployment": 1, "major_master_deployment": 2,
         "ready_plugin_stage": 3, "problem": 4}.get(item["kind"], 9),
        str(item["id"]),
    ))
    return {
        "version": VERSION,
        "manager_dir": str(manager),
        "game_dir": str(Path(game_dir).resolve()) if game_dir else None,
        "staging": str(Path(staging).resolve()) if staging else str(
            (manager / "staging" / "major_masters").resolve()),
        "items": items,
        "counts": {
            "items": len(items),
            "recoverable": sum(bool(item["recoverable"]) for item in items),
            "needs_review": sum(item["status"] in {"needs-review", "stale"} for item in items),
        },
    }


def _selected(inventory: dict, selected: str | dict) -> dict:
    item_id = selected.get("id") if isinstance(selected, dict) else str(selected)
    for item in inventory.get("items", []):
        if item.get("id") == item_id:
            return item
    raise KeyError(f"Recovery item not found: {_safe_label(item_id, 'unknown item')}")


def _transaction_for_item(manager: Path, inventory: dict, item: dict, game: Path,
                          major_staging: Path) -> tuple[Path, list[Path]]:
    target = Path(str(item.get("details", {}).get("root") or ""))
    selected_id = item.get("id")
    fresh = _transaction_items(manager)
    match = next((candidate for candidate in fresh if candidate.get("id") == selected_id), None)
    if match is None:
        raise RuntimeError("The selected transaction is no longer present.")
    if Path(str(match.get("details", {}).get("root") or "")).resolve() != target.resolve():
        raise RuntimeError("The selected transaction changed during recovery.")
    if target.name in STAGE_NAMES:
        package = target.parent.resolve()
        plugin_root = (manager / "staging" / "plugins").resolve()
        major_root = (manager / "staging" / "major_masters").resolve()
        if package.is_relative_to(plugin_root) or package.is_relative_to(major_root):
            return target, [package]
    if target.resolve().is_relative_to(manager.resolve()):
        return target, [manager.resolve(), game.resolve(), major_staging.resolve()]
    raise ValueError("Refusing to recover a transaction outside managed roots.")


def _forget_ready_stage(manager: Path, item: dict) -> dict:
    path = manager / "staging" / "plugins" / ".mwmanager-plugin-stages.json"
    package = str(item.get("details", {}).get("staging_package") or "")
    with mutation_lock(manager):
        state, problems = _read_object(path, "ready")
        if state is None:
            raise RuntimeError("; ".join(problems))
        retained = []
        removed = 0
        for entry in state["ready"]:
            if isinstance(entry, dict) and str(entry.get("staging_package") or "") == package:
                removed += 1
            else:
                retained.append(entry)
        if removed == 0:
            raise RuntimeError("The selected ready stage is no longer present.")
        state["ready"] = retained
        atomic_json(path, state)
    return {"action": "forget_ready_plugin_stage", "removed": removed,
            "staging_preserved": True, "staging_package": package}


def recover_item(manager_dir: str | Path, game_dir: str | Path, selected: str | dict,
                 staging: str | Path | None = None) -> dict:
    """Recover one inventory item, retaining changed files and refusing unsafe records."""
    manager = Path(manager_dir).resolve()
    game = Path(game_dir).resolve()
    major_staging = Path(staging).resolve() if staging else (manager / "staging" / "major_masters").resolve()
    inventory = build_recovery_inventory(manager, game, major_staging)
    item = _selected(inventory, selected)
    if not item.get("recoverable"):
        raise RuntimeError(f"Recovery needs review: {item['label']}")
    kind = item["kind"]
    if kind == "transaction":
        root, allowed = _transaction_for_item(manager, inventory, item, game, major_staging)
        with mutation_lock(manager, game):
            recovered = DurableTransaction.recover(root, allowed)
        return {"action": "recover_transaction", "recovered": recovered, "id": item["id"]}
    if kind == "plugin_deployment":
        from plugin_install import remove_deployments

        deployment_id = str(item.get("details", {}).get("deployment_id") or "")
        with mutation_lock(manager, game):
            result = remove_deployments(manager, game / "Data Files", {deployment_id})
        return {"action": "remove_plugin_deployment", **result, "id": item["id"]}
    if kind == "major_master_deployment":
        from major_master_deploy import remove_deployed

        result = remove_deployed(major_staging, game / "Data Files", manager)
        return {"action": "remove_major_master_deployments", **result, "id": item["id"]}
    if kind == "ready_plugin_stage":
        return _forget_ready_stage(manager, item)
    raise RuntimeError(f"Unsupported recovery action: {item.get('action')}")


def recover_transaction(manager_dir: str | Path, game_dir: str | Path,
                        selected: str | dict, staging: str | Path | None = None) -> dict:
    """Recover a selected transaction after rebuilding and validating its inventory entry."""
    return recover_item(manager_dir, game_dir, selected, staging)


scan_recovery = build_recovery_inventory
recovery_inventory = build_recovery_inventory
recover_selected = recover_item
recover_recovery_item = recover_item
