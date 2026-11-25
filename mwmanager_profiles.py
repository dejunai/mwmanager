"""Portable MWManager install profiles for managed choices and ordering."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

PROFILE_VERSION = 1
PROFILE_DIR = "profiles"
STATE_FILES = (
    "mwmanager_sort_order.txt",
    "mwmanager_rules.txt",
    "mwmanager_disabled.txt",
    "mwmanager_conflict_answers.json",
    "mwmanager_cell_winners.txt",
    "mwmanager_core_addons.json",
    "mwmanager_plugin_overrides.json",
    "protected_replacer_textures.json",
    "outputs/mwmanager_pending_options.json",
)
SECRET_KEYS = {"apikey", "api_key", "nexus_api_key", "token", "access_token", "password", "secret"}


def _safe_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    if not clean:
        raise ValueError("Profile name cannot be empty.")
    return clean


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None


def _sanitize(value):
    if isinstance(value, dict):
        return {str(key): _sanitize(item)
                for key, item in value.items() if str(key).casefold() not in SECRET_KEYS}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def profile_dir(manager_dir: Path) -> Path:
    return Path(manager_dir) / PROFILE_DIR


def save(manager_dir: Path, name: str, *, note: str = "") -> Path:
    """Capture manager-owned decisions, not plugin/archive binaries or credentials."""
    manager_dir = Path(manager_dir).resolve()
    files = {}
    for relative in STATE_FILES:
        path = manager_dir / relative
        if not path.is_file():
            continue
        data = path.read_bytes()
        files[relative] = {"sha256": _sha256(data), "bytes": len(data), "data": data.decode("utf-8", "strict")}

    settings_path = manager_dir / "mwmanager_gui_settings.json"
    settings = _read_json(settings_path)
    if isinstance(settings, dict):
        data = (json.dumps(_sanitize(settings), indent=2, sort_keys=True) + "\n").encode("utf-8")
        files["mwmanager_gui_settings.json"] = {
            "sha256": _sha256(data), "bytes": len(data), "data": data.decode("utf-8"),
        }

    profile = {
        "version": PROFILE_VERSION,
        "name": name,
        "note": note[:2000],
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "files": files,
    }
    payload = (json.dumps(profile, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    destination = profile_dir(manager_dir) / f"{_safe_name(name)}.json"
    _atomic_write(destination, payload)
    return destination


def list_profiles(manager_dir: Path) -> list[dict]:
    found = []
    for path in sorted(profile_dir(manager_dir).glob("*.json"), key=lambda item: item.name.casefold()):
        value = _read_json(path)
        if not isinstance(value, dict) or value.get("version") != PROFILE_VERSION:
            continue
        files = value.get("files") or {}
        found.append({"name": value.get("name", path.stem), "note": value.get("note", ""),
                      "saved_at": value.get("saved_at"), "file_count": len(files),
                      "path": str(path.resolve())})
    return found


def load(manager_dir: Path, name: str) -> dict:
    path = profile_dir(manager_dir) / f"{_safe_name(name)}.json"
    value = _read_json(path)
    if not isinstance(value, dict) or value.get("version") != PROFILE_VERSION:
        raise ValueError(f"Profile is missing or invalid: {name}")
    return value


def compare(manager_dir: Path, name: str) -> dict:
    manager_dir = Path(manager_dir).resolve()
    profile = load(manager_dir, name)
    wanted = profile.get("files") or {}
    changed, missing, unchanged, extra = [], [], [], []
    for relative, record in sorted(wanted.items()):
        path = manager_dir / relative
        if not path.is_file():
            missing.append(relative)
            continue
        digest = _sha256(path.read_bytes())
        (unchanged if digest == record.get("sha256") else changed).append(relative)
    for relative in STATE_FILES:
        if (manager_dir / relative).is_file() and relative not in wanted:
            extra.append(relative)
    return {"profile": name, "changed": changed, "missing": missing,
            "unchanged": unchanged, "extra": extra}


def apply(manager_dir: Path, name: str, *, backup: bool = True) -> dict:
    """Restore manager decisions only; deployment files and archives are never touched."""
    from workflow_safety import mutation_lock

    manager_dir = Path(manager_dir).resolve()
    profile = load(manager_dir, name)
    files = profile.get("files") or {}
    if not isinstance(files, dict):
        raise ValueError("Profile files are invalid.")
    allowed = set(STATE_FILES) | {"mwmanager_gui_settings.json"}
    prepared = []
    for relative, record in sorted(files.items()):
        relative_path = Path(relative)
        if (relative_path.is_absolute() or ".." in relative_path.parts
                or relative_path.as_posix() not in allowed):
            raise ValueError(f"Profile contains an unsafe path: {relative}")
        if not isinstance(record, dict):
            raise ValueError(f"Profile entry is invalid: {relative}")
        text = record.get("data")
        if not isinstance(text, str):
            raise ValueError(f"Profile entry has no content: {relative}")
        data = text.encode("utf-8")
        if _sha256(data) != record.get("sha256"):
            raise ValueError(f"Profile entry hash does not match: {relative}")
        target = manager_dir / relative_path
        if relative == "mwmanager_gui_settings.json" and target.is_file():
            current = _read_json(target)
            desired = _read_json_from_text(data)
            if isinstance(current, dict) and isinstance(desired, dict):
                for key, value in current.items():
                    if key.casefold() in SECRET_KEYS:
                        desired.setdefault(key, value)
                data = (json.dumps(desired, indent=2, sort_keys=True) + "\n").encode("utf-8")
        prepared.append((relative_path, target, data))

    backup_dir = None
    originals = []
    with mutation_lock(manager_dir):
        if backup:
            backup_dir = manager_dir / "backups" / "profiles" / time.strftime(
                f"{_safe_name(name)}-%Y%m%d-%H%M%S")
            backup_dir.mkdir(parents=True, exist_ok=False)
        for relative_path, target, _data in prepared:
            original = target.read_bytes() if target.is_file() else None
            originals.append((target, original))
            if backup and original is not None:
                kept = backup_dir / relative_path
                kept.parent.mkdir(parents=True, exist_ok=True)
                kept.write_bytes(original)
        try:
            for _relative_path, target, data in prepared:
                _atomic_write(target, data)
        except BaseException:
            for target, original in reversed(originals):
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(target, original)
            raise
    return {"profile": name, "restored": [path.as_posix() for path, _target, _data in prepared],
            "backup_dir": str(backup_dir) if backup_dir else None}


def _read_json_from_text(data: bytes):
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError, TypeError):
        return None


def personal_marks(manager_dir: Path, profile: dict) -> list[dict]:
    """The user's personal mods (nexus_ids) this profile names, by mod or by one of its deployed plugins:
    [{"mod", "plugins"}]. An exported profile lists them as not distributable."""
    import nexus_ids
    table = nexus_ids.overrides(manager_dir)
    names = nexus_ids.personal_names(manager_dir)
    if not names:
        return []
    text = json.dumps(profile.get("files") or {}, ensure_ascii=False).casefold()
    plugins: dict[str, set[str]] = {}
    deployments = _read_json(Path(manager_dir) / "plugin_deployments.json")
    for entry in (deployments or {}).get("deployments", []) if isinstance(deployments, dict) else ():
        for source in (entry.get("archive"), entry.get("staging_package"), entry.get("label")):
            if source and nexus_ids.is_personal(Path(str(source)).name, table=table):
                plugins.setdefault(nexus_ids.name_key(Path(str(source)).name), set()).update(
                    str(p) for p in entry.get("plugins") or ())
    marks = []
    for name in names:
        key = nexus_ids.name_key(name)
        named = sorted((p for p in plugins.get(key, ()) if p.casefold() in text), key=str.casefold)
        if key in text or named:
            marks.append({"mod": name, "plugins": named})
    return marks


def _with_marks(manager_dir: Path, profile: dict) -> dict:
    marks = personal_marks(manager_dir, profile)
    if marks:
        profile = dict(profile, not_distributable=marks,
                       not_distributable_note="Personal mods: the user's own or hand-adjusted files, not for sharing.")
    return profile


def export(manager_dir: Path, name: str, destination: Path) -> Path:
    """Copy a profile out; personal mods it names are listed under "not_distributable"."""
    destination = Path(destination).resolve()
    source = profile_dir(manager_dir) / f"{_safe_name(name)}.json"
    if not source.is_file():
        raise FileNotFoundError(f"Profile does not exist: {name}")
    value = _read_json(source)
    marked = _with_marks(manager_dir, value) if isinstance(value, dict) else value
    if marked is value:
        shutil.copy2(source, destination)
    else:
        _atomic_write(destination, (json.dumps(marked, indent=2, sort_keys=True, ensure_ascii=False)
                                    + "\n").encode("utf-8"))
    return destination


def import_profile(manager_dir: Path, source: Path, *, replace: bool = False) -> Path:
    source = Path(source).resolve()
    value = _read_json(source)
    if not isinstance(value, dict) or value.get("version") != PROFILE_VERSION:
        raise ValueError("Imported profile is invalid or from an unsupported version.")
    name = str(value.get("name") or source.stem)
    destination = profile_dir(manager_dir) / f"{_safe_name(name)}.json"
    if destination.exists() and not replace:
        raise FileExistsError(f"Profile already exists: {name}")
    _atomic_write(destination, source.read_bytes())
    return destination


def archive(manager_dir: Path, name: str, destination: Path) -> Path:
    """Export a profile and its referenced manager decisions as a zip."""
    destination = Path(destination).resolve()
    profile = _with_marks(manager_dir, load(manager_dir, name))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        profile_file = root / f"{_safe_name(name)}.json"
        profile_file.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.write(profile_file, profile_file.name)
    return destination
