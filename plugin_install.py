"""Archive discovery, ordering, extraction, and regular-plugin deployment helpers."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import time
import uuid
import zipfile
from datetime import datetime

from core_addons import (ARCHIVE_EXTENSIONS, _members, _member_preflight,
                         _archive_rank, _archive_esm_masters, _seven_zip_listing,
                         LARGE_ARCHIVE_BYTES,
                         _quiet_subprocess_options, _seven_zip, stamped_backup_path)

SKIP_DIRS = {"off", "bugreports", "fomod", "extras", "docs", "screenshots"}
from major_master_deploy import BSA_EXTENSIONS, BSA_FOLDERS
WINDOWS_MAX_PATH = 260  # MAX_PATH includes the terminating NUL; paths must stay below this length.
PLUGIN_RENAMES = {"anthologysolstheim.esp": "AnthologySolstheimPatched.esp"}
MASTER_RENAMES = {"tr_data.esm": "Tamriel_Data.esm"}
ARCHIVE_SCAN_CACHE_VERSION = 2
MAX_EXPANDED_ARCHIVE_BYTES = 8 * 1024 ** 3
MAX_EXPANDED_MEMBER_BYTES = 2 * 1024 ** 3
MAX_ARCHIVE_MEMBERS = 100_000
COMPRESSION_RATIO_LIMIT = 100
COMPRESSION_RATIO_SAMPLE_BYTES = 1024 ** 2
BLOCKED_PAYLOAD_EXTENSIONS = {
    # Executables, native libraries, installers, and common script formats.
    ".exe", ".dll", ".com", ".scr", ".msi", ".msp", ".cpl", ".ocx", ".sys",
    ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".sh", ".bash", ".zsh", ".fish",
    ".lua", ".py", ".pyw", ".pyc", ".js", ".jse", ".vbs", ".vbe", ".wsf", ".wsh",
    ".hta", ".pl", ".pm", ".rb", ".php", ".tcl", ".awk", ".ahk", ".jar", ".lnk",
}
SCRIPT_PAYLOAD_EXTENSIONS = {
    ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".sh", ".bash", ".zsh", ".fish",
    ".lua", ".py", ".pyw", ".pyc", ".js", ".jse", ".vbs", ".vbe", ".wsf", ".wsh",
    ".hta", ".pl", ".pm", ".rb", ".php", ".tcl", ".awk", ".ahk",
}

# Standalone tools are never regular plugin installs. Keep their intended route
# here so adding a dedicated game-root utility installer does not weaken this
# exclusion. The root route is intentionally blocked until that installer can
# journal and safely remove/restore files under the game directory.
UTILITY_ARCHIVES = (
    ("mcp", "plugin", re.compile(r"^(?:morrowind[ _-]+code[ _-]+patch(?:[ _-].*)?|mcp(?:[ _-](?:beta|v?\d.*|installer|setup))?)$", re.I),
     "Morrowind Code Patch is a standalone utility and is not deployed as a plugin."),
    ("mwse", "plugin", re.compile(r"^(?:mwse(?:[ _-](?:v?\d.*|core|installer|setup))?|morrowind[ _-]+script[ _-]+extender(?:[ _-].*)?)$", re.I),
     "MWSE is a standalone utility and is not deployed as a plugin."),
    ("mge_xe", "plugin", re.compile(r"^(?:mge[ _-]*xe|morrowind[ _-]+graphics[ _-]+extender[ _-]+xe)(?:[ _-].*)?$", re.I),
     "MGE XE is a standalone utility and is not deployed as a plugin."),
    ("nifzer0edit", "root", re.compile(r"^nifzer0edit(?:[ _-].*)?$", re.I),
     "NifZer0Edit belongs in the Morrowind root; root utility deployment is not available yet."),
    ("breezify", "root", re.compile(r"^breezify(?:[ _-].*)?$", re.I),
     "Breezify belongs in the Morrowind root; root utility deployment is not available yet."),
)


def _utility_archive_route(archive: Path, metadata: dict) -> tuple[str, str] | None:
    """Return a managed utility route based on the archive/metadata title."""
    candidates = [archive.stem]
    for key in ("name", "modname", "mod_name", "title"):
        value = _metadata_value(metadata, {key})
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    for _key, route, pattern, reason in UTILITY_ARCHIVES:
        if any(pattern.fullmatch(candidate.strip()) for candidate in candidates):
            return route, reason
    return None


def _allowed_script_payload(name: str) -> bool:
    """Scripts below OpenMW/Morrowind script trees are plugin payload, not tools."""
    parts = [part.casefold() for part in Path(name).parts]
    suffix = Path(name).suffix.casefold()
    if suffix in SCRIPT_PAYLOAD_EXTENSIONS and "scripts" in parts:
        return True
    if suffix in {".lua", ".dll"} and "mwse" in parts:
        return True
    return False


def _blocked_archive_payloads(names: list[str]) -> list[str]:
    normalized = [name.replace("\\", "/").lstrip("/") for name in names]
    return sorted(
        {name for name in normalized
         if Path(name).suffix.casefold() in BLOCKED_PAYLOAD_EXTENSIONS
         and not any(part.casefold() == "docs" for part in Path(name).parts)
         and not any("openmw" in part.casefold() for part in Path(name).parts[:-1])
         and not _allowed_script_payload(name)},
        key=str.casefold,
    )


ALLOWED_PAYLOADS_FILE = "mwmanager_allowed_payloads.json"


def allowed_payloads(manager: Path) -> dict[str, list[str]]:
    """{archive file name, casefolded: [member paths]} the user chose to install although they are executables,
    libraries or scripts (a modder's utility shipped inside an otherwise normal mod)."""
    try:
        data = json.loads((Path(manager) / ALLOWED_PAYLOADS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def allow_payloads(manager: Path, archive: str | Path, payloads: list[str]) -> None:
    data = allowed_payloads(manager)
    data[Path(archive).name.casefold()] = sorted(set(data.get(Path(archive).name.casefold(), [])) | set(payloads),
                                                  key=str.casefold)
    (Path(manager) / ALLOWED_PAYLOADS_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _risk_finding(severity: str, code: str, message: str, **details) -> dict:
    finding = {"severity": severity, "code": code, "message": message}
    finding.update(details)
    return finding


def _technical_flag(value) -> bool:
    return str(value or "").strip().casefold() in {"+", "yes", "true", "on", "1"}


def _technical_size(value) -> int:
    try:
        return max(0, int(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _technical_link_value(value) -> bool:
    return bool(str(value or "").strip()) and str(value).strip().casefold() not in {
        "-", "0", "false", "no", "none",
    }


def _seven_zip_member_is_link(record: dict[str, str]) -> bool:
    attributes = str(record.get("attributes", "")).casefold()
    mode = str(record.get("mode", "")).casefold()
    link_type = str(record.get("link type", "")).casefold()
    return bool(
        _technical_link_value(record.get("symbolic link")) or
        _technical_link_value(record.get("hard link")) or
        "symbolic link" in attributes or "reparse" in attributes or
        link_type in {"symbolic", "hard", "junction"} or
        mode.startswith(("l", "j"))
    )


def analyze_archive(item: dict, manager: Path) -> dict:
    """Inspect an archive without extracting it and return structured risk findings."""
    archive = Path(item["archive"])
    seven_zip = _seven_zip(manager)
    names, inspection_error = _members(archive, seven_zip)
    findings = []
    if inspection_error:
        findings.append(_risk_finding(
            "high", "archive_inspection_error",
            f"Archive could not be inspected: {inspection_error}",
        ))

    unsafe, collisions = _member_preflight(names)
    findings.extend(_risk_finding("high", "unsafe_member_path", message)
                    for message in unsafe)
    findings.extend(_risk_finding("high", "path_collision", message)
                    for message in collisions)

    if not item.get("trusted_prerequisite"):
        allowed = {name.casefold() for name in item.get("allowed_payloads") or ()}
        for payload in (p for p in _blocked_archive_payloads(names) if p.casefold() not in allowed):
            findings.append(_risk_finding(
                "high", "suspicious_payload",
                f"Archive contains executable, library, or script payload: {payload}",
                path=payload,
            ))

    expanded_bytes = member_count = 0
    encrypted_paths = set()
    link_paths = set()
    solid = False
    if not inspection_error and archive.suffix.casefold() == ".zip":
        try:
            with zipfile.ZipFile(archive) as zf:
                infos = zf.infolist()
                member_count = len(infos)
                for info in infos:
                    expanded_bytes += max(0, info.file_size)
                    mode = info.external_attr >> 16
                    is_link = stat.S_ISLNK(mode)
                    # 0x400 is FILE_ATTRIBUTE_REPARSE_POINT in ZIP MS-DOS attributes.
                    is_reparse = bool(info.external_attr & 0x400)
                    if is_link or is_reparse:
                        link_paths.add(info.filename)
                    if info.flag_bits & 0x1:
                        encrypted_paths.add(info.filename)
                    ratio = info.file_size / max(1, info.compress_size)
                    if (not info.is_dir() and info.file_size >= COMPRESSION_RATIO_SAMPLE_BYTES
                            and ratio >= COMPRESSION_RATIO_LIMIT):
                        findings.append(_risk_finding(
                            "high", "compression_ratio_anomaly",
                            f"Member has an unusually high compression ratio ({ratio:.1f}:1): "
                            f"{info.filename}",
                            path=info.filename, ratio=round(ratio, 2),
                            expanded_bytes=info.file_size, packed_bytes=info.compress_size,
                        ))
                    if info.file_size > MAX_EXPANDED_MEMBER_BYTES:
                        findings.append(_risk_finding(
                            "high", "oversized_member",
                            f"Member expands beyond the safety limit: {info.filename}",
                            path=info.filename, expanded_bytes=info.file_size,
                        ))
        except (OSError, zipfile.BadZipFile) as exc:
            findings.append(_risk_finding(
                "high", "archive_inspection_error",
                f"Archive details could not be inspected: {exc}",
            ))
    elif not inspection_error:
        archive_record, records, detail_error = _seven_zip_listing(archive, seven_zip)
        if detail_error:
            findings.append(_risk_finding(
                "high", "archive_inspection_error",
                f"Archive details could not be inspected: {detail_error}",
            ))
        else:
            member_count = len(records)
            all_records = [archive_record, *records]
            solid = any(_technical_flag(record.get("solid")) for record in all_records)
            for record in records:
                path = record.get("path", "")
                size = _technical_size(record.get("size"))
                packed = _technical_size(record.get("packed size"))
                expanded_bytes += size
                if (_technical_flag(record.get("encrypted"))
                        or "encrypted" in str(record.get("method", "")).casefold()
                        or "aes" in str(record.get("method", "")).casefold()):
                    encrypted_paths.add(path)
                if any(_technical_link_value(record.get(field)) for field in
                       ("symbolic link", "hard link", "reparse", "reparse point")):
                    link_paths.add(path)
                elif _seven_zip_member_is_link(record):
                    link_paths.add(path)
                if (record.get("packed size") is not None and packed
                        and size >= COMPRESSION_RATIO_SAMPLE_BYTES
                        and size / packed >= COMPRESSION_RATIO_LIMIT):
                    ratio = size / max(1, packed)
                    findings.append(_risk_finding(
                        "high", "compression_ratio_anomaly",
                        f"Member has an unusually high compression ratio ({ratio:.1f}:1): {path}",
                        path=path, ratio=round(ratio, 2),
                        expanded_bytes=size, packed_bytes=packed,
                    ))
                if size > MAX_EXPANDED_MEMBER_BYTES:
                    findings.append(_risk_finding(
                        "high", "oversized_member",
                        f"Member expands beyond the safety limit: {path}",
                        path=path, expanded_bytes=size,
                    ))
            if (not encrypted_paths and any(
                    _technical_flag(record.get("encrypted"))
                    or "aes" in str(record.get("method", "")).casefold()
                    for record in all_records)):
                encrypted_paths.add("")

    for path in sorted(encrypted_paths, key=str.casefold):
        findings.append(_risk_finding(
            "high", "encrypted_member",
            "Archive or member is encrypted" + (f": {path}" if path else ""),
            **({"path": path} if path else {}),
        ))
    for path in sorted(link_paths, key=str.casefold):
        findings.append(_risk_finding(
            "high", "link_member",
            f"Archive contains a symbolic link, hard link, junction, or reparse point: {path}",
            path=path,
        ))
    if solid:
        findings.append(_risk_finding(
            "medium", "solid_archive",
            "Archive is solid; one corrupted member can affect extraction of later members.",
        ))
    if member_count > MAX_ARCHIVE_MEMBERS:
        findings.append(_risk_finding(
            "high", "excessive_member_count",
            f"Archive contains an excessive number of members: {member_count}",
            member_count=member_count,
        ))
    if expanded_bytes > MAX_EXPANDED_ARCHIVE_BYTES:
        findings.append(_risk_finding(
            "high", "oversized_archive",
            f"Archive expands beyond the safety limit: {expanded_bytes} bytes",
            expanded_bytes=expanded_bytes,
        ))
    try:
        archive_bytes = archive.stat().st_size
    except OSError:
        archive_bytes = 0
    if archive_bytes > LARGE_ARCHIVE_BYTES:
        findings.append(_risk_finding(
            "medium", "large_archive",
            f"Archive is unusually large and may require significant disk and time: "
            f"{archive_bytes} bytes",
            archive_bytes=archive_bytes,
        ))

    high = [finding for finding in findings if finding["severity"] == "high"]
    return {
        "archive": str(archive),
        "safe": not high,
        "blocked": bool(high),
        "findings": findings,
        "member_names": names,
        "member_count": member_count or len(names),
        "expanded_bytes": expanded_bytes,
        "archive_bytes": archive_bytes,
        "high_severity_findings": high,
    }


def _canonical_plugin_name(name: str) -> str:
    renamed = PLUGIN_RENAMES.get(name.casefold())
    if renamed:
        return renamed
    if Path(name).suffix.casefold() == ".esp" and re.sub(r"[^a-z0-9]", "", Path(name).stem.casefold()) == "anthologysolstheim":
        return "AnthologySolstheimPatched.esp"
    return name


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .") or "plugin-archive"


def _path_is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    try:
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _safe_child(root: Path, relative: Path, *, label: str) -> Path:
    """Return a contained path whose existing components below root are not links or junctions."""
    relative = Path(relative)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} is not a safe relative path: {relative}")
    root = Path(root).resolve()
    target = root / relative
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if _path_is_link(cursor):
            raise ValueError(f"{label} crosses a symbolic link or junction: {cursor}")
        if cursor.exists() or cursor.is_symlink():
            try:
                cursor.resolve().relative_to(root)
            except ValueError as exc:
                raise ValueError(f"{label} escapes its root: {cursor}") from exc
    return target


def _assert_no_links(root: Path) -> None:
    for base, dirs, files in os.walk(root):
        here = Path(base)
        for name in dirs + files:
            path = here / name
            if _path_is_link(path):
                raise ValueError(f"Archive contains a symbolic link or junction: "
                                 f"{path.relative_to(root)}")


def _seven_zip_link_members(seven_zip: str, archive: Path) -> list[str]:
    result = subprocess.run([seven_zip, "l", "-slt", "-ba", "--", str(archive)],
                            capture_output=True, text=True, errors="replace", timeout=180,
                            **_quiet_subprocess_options())
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or
                            f"7-Zip exited {result.returncode}")[-3000:])
    linked, current, fields = [], None, {}

    def finish():
        nonlocal current, fields
        attributes = str(fields.get("attributes", "")).lower()
        mode = str(fields.get("mode", "")).lower()
        if current and (
                _technical_link_value(fields.get("symbolic link")) or
                _technical_link_value(fields.get("hard link")) or
                "symbolic link" in attributes or "reparse" in attributes or
                "reparse point" in attributes or mode.startswith(("l", "j"))):
            linked.append(current)
        current, fields = None, {}

    for line in result.stdout.splitlines():
        if not line.strip():
            finish()
            continue
        key, separator, value = line.partition(" = ")
        if not separator:
            continue
        key, value = key.strip().casefold(), value.strip()
        fields[key] = value
        if key == "path":
            if current:
                finish()
            current = value
            fields["path"] = value
    finish()
    return linked


def _archive_metadata(archive: Path) -> dict:
    """Read adjacent Vortex metadata when available; tolerate its absence or schema changes."""
    candidates = [archive.with_suffix(archive.suffix + ".meta"), archive.with_suffix(".meta"),
                  archive.parent / (archive.name + ".meta")]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8-sig")
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}


def _nexus_ids_from_archive_name(archive: Path) -> tuple[str | None, str | None]:
    """Recover Nexus IDs from the conventional Nexus download filename suffix.

    Vortex does not create an adjacent .meta file for every download. Nexus archive
    names commonly end in ``-<mod id>-<version>-<file id>``; version components
    may themselves be separated by hyphens.
    """
    stem = archive.stem
    match = re.search(
        r"-(?P<modid>\d{4,})-(?P<version>(?:[A-Za-z][A-Za-z0-9.]*|\d{1,3})"
        r"(?:-(?:[A-Za-z][A-Za-z0-9.]*|\d{1,3}))*)-(?P<fileid>\d{6,})$",
        stem,
    )
    if not match:
        # Newer Nexus names: "<name> <mod id> <version> <upload time> <tag>", with no file id
        match = re.search(r"\s(?P<modid>\d{3,})\s\S+\s\d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z(?:\s|$)", stem)
        return (match.group("modid"), None) if match else (None, None)
    return match.group("modid"), match.group("fileid")


def _apply_nexus_id_fallback(item: dict, archive: Path) -> None:
    """Fill missing IDs from the archive name while preserving Vortex metadata values."""
    mod_id, file_id = _nexus_ids_from_archive_name(archive)
    if not item.get("nexus_mod_id") and mod_id:
        item["nexus_mod_id"] = mod_id
    if not item.get("nexus_file_id") and file_id:
        item["nexus_file_id"] = file_id


def _metadata_value(data: dict, keys: set[str]):
    """Find a Vortex metadata value without assuming one exact schema version."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key.casefold() in keys and value not in (None, ""):
                return value
        for value in data.values():
            found = _metadata_value(value, keys) if isinstance(value, dict) else None
            if found is not None:
                return found
    return None


def _download_timestamp(metadata: dict, fallback: float) -> float:
    value = _metadata_value(metadata, {"downloadedat", "downloadtime", "downloaded", "filetime", "timestamp"})
    if isinstance(value, (int, float)):
        value = float(value)
        return value / 1000 if value > 10_000_000_000 else value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def _scan_cache_fingerprint(archive: Path) -> dict:
    """Use archive and adjacent Vortex metadata stats to invalidate cached inspection."""
    def signature(path: Path):
        try:
            stat = path.stat()
            return {"path": str(path.resolve()).casefold(), "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns}
        except OSError:
            return None

    metadata_paths = [archive.with_suffix(archive.suffix + ".meta"), archive.with_suffix(".meta"),
                      archive.parent / (archive.name + ".meta")]
    return {"archive": signature(archive),
            "metadata": [value for value in (signature(path) for path in metadata_paths) if value]}


def _read_archive_scan_cache(manager: Path) -> dict:
    try:
        value = json.loads((manager / "archive_scan_cache.json").read_text(encoding="utf-8"))
        if value.get("version") == ARCHIVE_SCAN_CACHE_VERSION and isinstance(value.get("entries"), dict):
            return value["entries"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def _write_archive_scan_cache(manager: Path, entries: dict) -> None:
    path = manager / "archive_scan_cache.json"
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps({"version": ARCHIVE_SCAN_CACHE_VERSION, "entries": entries},
                                        separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def scan_archives(downloads: Path, manager: Path, progress=None,
                  installed_plugins: set[str] | None = None,
                  refresh_cache: bool = False) -> list[dict]:
    seven_zip = _seven_zip(manager)
    archives = sorted((p for p in downloads.rglob("*") if p.is_file()
                       and p.suffix.casefold() in ARCHIVE_EXTENSIONS
                       and not any(part.startswith(".") for part in p.relative_to(downloads).parts)),
                      key=lambda p: (p.stat().st_mtime_ns, p.name.casefold()))
    found = []
    old_cache = {} if refresh_cache else _read_archive_scan_cache(manager)
    new_cache = {}
    allowed_by_archive = allowed_payloads(manager)
    for index, archive in enumerate(archives, 1):
        cache_key = str(archive.resolve()).casefold()
        fingerprint = _scan_cache_fingerprint(archive)
        cached = old_cache.get(cache_key)
        allowed = allowed_by_archive.get(archive.name.casefold(), [])
        if (cached and cached.get("fingerprint") == fingerprint and isinstance(cached.get("item"), dict)
                and cached["item"].get("allowed_payloads", []) == allowed):
            if progress:
                progress(f"Using cached scan {index} of {len(archives)}: {archive.name}")
            item = cached["item"]
            _apply_nexus_id_fallback(item, archive)
            found.append(item)
            new_cache[cache_key] = cached
            continue
        if progress:
            progress(f"Inspecting archive {index} of {len(archives)}: {archive.name}")
        risk = analyze_archive({"archive": str(archive), "allowed_payloads": allowed}, manager)
        names = risk["member_names"]
        error = "; ".join(finding["message"] for finding in risk["findings"]
                          if finding["code"] == "archive_inspection_error") or None
        normalized = [name.replace("\\", "/").lstrip("/") for name in names]
        blocked_payloads = [p for p in _blocked_archive_payloads(names)
                            if p.casefold() not in {a.casefold() for a in allowed}]
        archive_plugins = sorted({Path(name).name for name in normalized
                                  if Path(name).suffix.casefold() in {".esp", ".esm"}}, key=str.casefold)
        plugin_masters = {}
        if not risk["blocked"]:
            for archive_plugin in archive_plugins:
                plugin = _canonical_plugin_name(archive_plugin)
                masters, master_error = _archive_esm_masters(archive, archive_plugin, manager, names)
                if not master_error:
                    plugin_masters[plugin] = [
                        MASTER_RENAMES.get(master.casefold(), master) for master in masters
                    ]
        plugins = sorted({_canonical_plugin_name(name) for name in archive_plugins}, key=str.casefold)
        unsafe, collisions = _member_preflight(names)
        assets = [name for name in normalized if name and not name.endswith("/")
                  and Path(name).suffix.casefold() not in {".esp", ".esm", ".bsa", ".txt", ".md", ".html"}
                  and not any(part.casefold() in SKIP_DIRS for part in Path(name).parts)]
        has_mwse = any("mwse" in name.casefold().split("/") for name in normalized)
        has_fomod = any(Path(name).name.casefold() == "moduleconfig.xml" and "fomod" in name.casefold()
                        for name in normalized)
        bain_dirs = {Path(name).parts[0] for name in normalized if Path(name).parts
                     and re.match(r"^\d{2,3}(?:\s|[-_.])", Path(name).parts[0])}
        replacer = bool(plugins and assets and any("meshes/" in ("/" + n.casefold()) for n in normalized))
        try:
            stat = archive.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except OSError:
            mtime, size = 0, 0
        metadata = _archive_metadata(archive)
        utility = _utility_archive_route(archive, metadata)
        item = {"archive": str(archive), "archive_plugins": archive_plugins,
                "plugins": plugins, "plugin_masters": plugin_masters,
                "asset_count": len(assets), "asset_bytes": size, "download_time": mtime,
                "archive_rank": list(_archive_rank(archive)),
                "nexus_mod_id": _metadata_value(metadata, {"modid", "mod_id", "nexusmodid"}),
                "nexus_file_id": _metadata_value(metadata, {"fileid", "file_id", "nexusfileid"}),
                "has_mwse": has_mwse,
                "has_fomod": has_fomod, "has_bain": bool(bain_dirs), "replacer": replacer,
                "utility_route": utility[0] if utility else None,
                "blocked_payloads": blocked_payloads, "allowed_payloads": allowed,
                "archive_risk": risk,
                "blocked": bool(risk["blocked"] or utility),
                "problem": "; ".join(filter(None, [
                    "; ".join(finding["message"] for finding in risk["findings"]
                              if finding["severity"] == "high"),
                    utility[1] if utility else None,
                ])),
                "vortex_metadata": metadata}
        item["download_time"] = _download_timestamp(metadata, mtime)
        _apply_nexus_id_fallback(item, archive)
        found.append(item)
        new_cache[cache_key] = {"fingerprint": fingerprint, "item": item}
    _write_archive_scan_cache(manager, new_cache)
    for item in found:
        has_landmass_master = any(Path(name).suffix.casefold() == ".esm" for name in item["plugins"])
        large = has_landmass_master or item["asset_count"] >= 500 or (item["asset_count"] >= 150 and item["plugins"])
        item["large"] = large
        item["category"] = (0 if item["has_mwse"] and not item["plugins"] else
                            1 if item["plugins"] and large and not item["replacer"] else
                            2 if item["plugins"] and not item["replacer"] else
                            3 if item["replacer"] else 4)
        item["rank"] = (item["category"],
                        -(int(item["has_mwse"]) + int(item["has_fomod"] or item["has_bain"])),
                        -item["asset_count"], -item["download_time"], item["archive"].casefold())
    return deduplicate_and_order(found, installed_plugins or set(), manager)


def deduplicate_and_order(items: list[dict], installed_plugins: set[str], manager: Path) -> list[dict]:
    """Keep the newest Vortex file for each Nexus mod and plugin, then topologically order masters."""
    def as_tuple(value):
        return tuple(as_tuple(part) if isinstance(part, list) else part for part in value) if isinstance(value, list) else value

    deployed_ranks = {}
    for deployment in _read_deployment_state(manager).get("deployments", []):
        rank = as_tuple(deployment.get("rank", []))
        for plugin in deployment.get("plugins", []):
            previous = deployed_ranks.get(plugin.casefold())
            if not previous or rank > previous:
                deployed_ranks[plugin.casefold()] = rank
    winners_by_mod = {}
    for item in items:
        mod_id = str(item.get("nexus_mod_id") or "").strip()
        if not mod_id:
            continue
        current = winners_by_mod.get(mod_id)
        if current is None or _archive_rank(Path(item["archive"])) > _archive_rank(Path(current["archive"])):
            winners_by_mod[mod_id] = item
    mod_winners = {str(item["archive"]).casefold() for item in winners_by_mod.values()}
    without_old_mods = [item for item in items
                        if not item.get("nexus_mod_id")
                        or str(item["archive"]).casefold() in mod_winners]

    latest_plugin_archive = {}
    for item in without_old_mods:
        for plugin in item["plugins"]:
            key = plugin.casefold()
            current = latest_plugin_archive.get(key)
            if current is None or _archive_rank(Path(item["archive"])) > _archive_rank(Path(current["archive"])):
                latest_plugin_archive[key] = item
    filtered = []
    for item in without_old_mods:
        stale = [name for name in item["plugins"]
                 if (latest_plugin_archive.get(name.casefold()) is not item
                     or (name.casefold() in deployed_ranks
                         and as_tuple(item["archive_rank"]) <= deployed_ranks[name.casefold()]))]
        if item["plugins"] and len(stale) == len(item["plugins"]):
            continue
        item["superseded_plugins"] = stale
        # If a multi-plugin archive mixes current and stale plugin versions, retain only
        # current plugin files during install, and surface the condition in the picker.
        item["selected_plugins"] = [name for name in item["plugins"] if name not in stale]
        item["has_duplicate_plugin_payload"] = bool(stale)
        if stale:
            item["problem"] = "; ".join(filter(None, (item.get("problem"),
                "Superseded plugin file(s) will be moved under Off: " + ", ".join(stale))))
        filtered.append(item)

    provider = {}
    for item in filtered:
        for plugin in item.get("selected_plugins", []):
            provider[plugin.casefold()] = item
    installed = {name.casefold() for name in installed_plugins}
    deps = {id(item): set() for item in filtered}
    missing = {id(item): [] for item in filtered}
    for item in filtered:
        for plugin, masters in item.get("plugin_masters", {}).items():
            if plugin not in item.get("selected_plugins", []):
                continue
            for master in masters:
                key = master.casefold()
                if key in installed:
                    continue
                parent = provider.get(key)
                if parent is None:
                    missing[id(item)].append(master)
                elif parent is not item:
                    deps[id(item)].add(id(parent))
    for item in filtered:
        if missing[id(item)]:
            item["blocked"] = True
            note = "Missing required master(s): " + ", ".join(sorted(set(missing[id(item)]), key=str.casefold))
            item["problem"] = "; ".join(filter(None, (item.get("problem"), note)))

    # Stable topological ordering: requirements first, otherwise preserve the auto-install score.
    pending = list(filtered)
    ordered = []
    completed = set()
    while pending:
        ready = [item for item in pending if deps[id(item)] <= completed]
        if not ready:
            cycle_names = [name for item in pending for name in item.get("selected_plugins", [])]
            for item in pending:
                item["blocked"] = True
                item["problem"] = "; ".join(filter(None, (item.get("problem"),
                                           "Required-master cycle: " + ", ".join(cycle_names))))
            ordered.extend(sorted(pending, key=lambda item: item["rank"]))
            break
        chosen = min(ready, key=lambda item: item["rank"])
        ordered.append(chosen)
        completed.add(id(chosen))
        pending.remove(chosen)
    return ordered


def count_bsa_assets(package: Path) -> int:
    count = 0
    for base, dirs, files in os.walk(package):
        rel = Path(base).relative_to(package)
        parts = list(rel.parts)
        while parts and parts[0].casefold() in {"00 data files", "data files"}:
            parts.pop(0)
        dirs[:] = [d for d in dirs if d.casefold() not in SKIP_DIRS]
        if parts and parts[0].casefold() in BSA_FOLDERS:
            count += sum(1 for name in files if not name.startswith(".")
                         and Path(name).suffix.casefold() in BSA_EXTENSIONS)
    return count


def apply_plugin_compatibility(package: Path) -> list[str]:
    """Remaster legacy TR_Data headers and normalize the Anthology Solstheim plugin name."""
    changed = []
    plugin_paths = []
    for base, dirs, files in os.walk(package):
        dirs[:] = [directory for directory in dirs if directory.casefold() not in SKIP_DIRS]
        plugin_paths.extend(Path(base) / name for name in files
                            if Path(name).suffix.casefold() in {".esp", ".esm"})

    for path in plugin_paths:
        try:
            original = path.read_bytes()
        except OSError:
            continue
        if len(original) < 16 or original[:4] != b"TES3":
            continue
        header_end = 16 + int.from_bytes(original[4:8], "little")
        if header_end > len(original):
            continue
        pos = 16
        header = bytearray()
        rewrites = []
        while pos + 8 <= header_end:
            tag = original[pos:pos + 4]
            size = int.from_bytes(original[pos + 4:pos + 8], "little")
            stop = pos + 8 + size
            if stop > header_end:
                header.clear()
                break
            payload = original[pos + 8:stop]
            if tag == b"MAST":
                master = payload.split(b"\0", 1)[0].decode("cp1252", "replace")
                replacement = MASTER_RENAMES.get(master.casefold())
                if replacement:
                    payload = replacement.encode("cp1252") + (b"\0" if payload.endswith(b"\0") else b"")
                    rewrites.append(f"{master} → {replacement}")
            header.extend(tag + len(payload).to_bytes(4, "little") + payload)
            pos = stop
        if pos != header_end or not header:
            continue
        if rewrites:
            # Off is a package-level non-deploy tree, matching Tidy's backup convention.
            backup = package / "Off" / path.stem / "Original plugin headers" / path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(path, backup)
            body = bytes(header) + original[header_end:]
            rewritten = b"TES3" + len(header).to_bytes(4, "little") + original[8:16] + body
            temp = path.with_suffix(path.suffix + ".mwmanager-tmp")
            temp.write_bytes(rewritten)
            os.replace(temp, path)
            changed.append(f"{path.name}: " + ", ".join(rewrites))

    for path in plugin_paths:
        target_name = _canonical_plugin_name(path.name)
        if target_name.casefold() == path.name.casefold() or not path.exists():
            continue
        target = path.with_name(target_name)
        if target.exists():
            raise FileExistsError(f"Cannot rename {path.name}: {target.name} already exists in the staged package.")
        path.rename(target)
        changed.append(f"{path.name} → {target.name}")
    return changed


def pack_package_assets(package: Path, plugin_name: str, manager: Path, progress=None) -> list[Path]:
    from major_master_deploy import _pack_assets, find_bsapack
    tool = find_bsapack()
    if not tool:
        raise FileNotFoundError("bsapack.dej.exe was not found in MWManager's tools folder or on PATH.")
    return _pack_assets(package, plugin_name, tool, progress)


def extract_archive(item: dict, destination: Path, manager: Path) -> Path:
    archive = Path(item["archive"])
    risk = analyze_archive(item, manager)
    if risk["blocked"] or item.get("blocked"):
        reasons = "; ".join(finding["message"] for finding in risk["high_severity_findings"])
        raise ValueError(
            f"Archive needs review: {archive.name}: "
            f"{reasons or item.get('problem') or 'unsafe archive'}"
        )
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Staging destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.extracting"
    temporary.mkdir()
    try:
        if archive.suffix.casefold() == ".zip":
            with zipfile.ZipFile(archive) as zf:
                for entry in zf.infolist():
                    if entry.is_dir():
                        continue
                    if (stat.S_ISLNK(entry.external_attr >> 16)
                            or entry.external_attr & 0x400):
                        raise ValueError(f"Archive contains a symbolic or reparse link: "
                                         f"{entry.filename}")
                    target = _safe_child(temporary, Path(entry.filename.replace("\\", "/")),
                                         label="Archive member")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(entry) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
        else:
            seven_zip = _seven_zip(manager)
            if not seven_zip:
                raise FileNotFoundError("7-Zip is required to extract this archive format.")
            linked = _seven_zip_link_members(seven_zip, archive)
            if linked:
                raise ValueError(f"Archive contains symbolic links or junctions: "
                                 + ", ".join(linked[:10]))
            result = subprocess.run([seven_zip, "x", str(archive), f"-o{temporary}", "-y"],
                                    capture_output=True, text=True, errors="replace", timeout=7200,
                                    **_quiet_subprocess_options())
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout or
                                    f"7-Zip exited {result.returncode}")[-3000:])
        _assert_no_links(temporary)
        # Defender or the indexer can still hold freshly extracted files; renaming the folder then fails.
        for attempt in range(10):
            try:
                os.replace(temporary, destination)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(1 + attempt)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def deploy_package(package: Path, data_dir: Path, plugin_label: str,
                   protected_texture_file: Path | None = None, *,
                   transaction=None) -> tuple[int, int, list[dict]]:
    """Deploy tidied files, retaining replacer textures against later unrelated overrides."""
    check_deploy_path_lengths(package, data_dir, plugin_label)
    linked = skipped = 0
    operations = []
    off_root = package / "Off" / _safe_name(Path(plugin_label).stem) / "replaced"
    texture_paths = set()
    meshes_present = False
    for base, dirs, files in os.walk(package):
        kept = []
        for name in dirs:
            child = Path(base) / name
            if _path_is_link(child):
                raise ValueError(f"Staged package contains a symbolic link or junction: "
                                 f"{child.relative_to(package)}")
            if name.casefold() not in SKIP_DIRS:
                kept.append(name)
        dirs[:] = kept
        for name in files:
            suffix = Path(name).suffix.casefold()
            rel = (Path(base) / name).relative_to(package)
            if suffix == ".nif":
                meshes_present = True
            elif suffix in {".dds", ".tga", ".bmp", ".png", ".jpg", ".jpeg"}:
                texture_paths.add(str(rel).replace("\\", "/").casefold())
    protected = set()
    if protected_texture_file:
        try:
            value = json.loads(protected_texture_file.read_text(encoding="utf-8"))
            if isinstance(value, list):
                protected = {str(path).replace("\\", "/").casefold() for path in value}
        except (OSError, ValueError):
            pass
    protect_this_package = meshes_present and bool(texture_paths)
    for base, dirs, files in os.walk(package):
        kept = []
        for name in dirs:
            child = Path(base) / name
            if _path_is_link(child):
                raise ValueError(f"Staged package contains a symbolic link or junction: "
                                 f"{child.relative_to(package)}")
            if name.casefold() not in SKIP_DIRS:
                kept.append(name)
        dirs[:] = kept
        for name in files:
            source = Path(base) / name
            rel = source.relative_to(package)
            if _path_is_link(source):
                raise ValueError(f"Staged package contains a symbolic link or junction: {rel}")
            try:
                source.resolve().relative_to(package.resolve())
            except ValueError as exc:
                raise ValueError(f"Staged package file escapes its package: {rel}") from exc
            if not rel.parts or name.startswith("."):
                continue
            if source.suffix.casefold() in {".esp", ".esm", ".bsa"}:
                rel = Path(name)
            target = _safe_child(data_dir, rel, label="Deployment target")
            target.parent.mkdir(parents=True, exist_ok=True)
            rel_key = str(rel).replace("\\", "/").casefold()
            is_texture = source.suffix.casefold() in {".dds", ".tga", ".bmp", ".png", ".jpg", ".jpeg"}
            if is_texture and target.exists() and rel_key in protected and not protect_this_package:
                retained = package / "Off" / _safe_name(Path(plugin_label).stem) / "protected textures" / rel
                retained.parent.mkdir(parents=True, exist_ok=True)
                if transaction:
                    transaction.move(source, retained)
                else:
                    shutil.move(str(source), str(retained))
                skipped += 1
                continue
            if target.is_file() and target.stat().st_size == source.stat().st_size:
                # Avoid hashing every large asset; exact same-file links are common during resume.
                try:
                    if os.path.samefile(source, target):
                        skipped += 1
                        continue
                except OSError:
                    pass
            backup = None
            if target.exists():
                if not target.is_file():
                    raise IsADirectoryError(f"A directory blocks deployment of {target}")
                backup = stamped_backup_path(off_root / rel)
                if not transaction:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, backup)
            elif transaction:
                backup = stamped_backup_path(off_root / rel)
            copied = os.path.splitdrive(str(source.resolve()))[0].casefold() != \
                os.path.splitdrive(str(data_dir.resolve()))[0].casefold()
            if transaction:
                if copied:
                    tx_operation = transaction.copy(source, target, backup)
                else:
                    tx_operation = transaction.link(source, target, backup)
                backup = Path(tx_operation["backup"]) if tx_operation.get("backup") else None
            else:
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
                    copied = True
            linked += 1
            operations.append({"source": str(source.resolve()), "target": str(target.resolve()),
                               "backup": str(backup.resolve()) if backup is not None else None,
                               "size": source.stat().st_size,
                               "sha256": _sha256_file(source)})
    if protect_this_package:
        protected.update(texture_paths)
    if protected_texture_file:
        protected_texture_file.parent.mkdir(parents=True, exist_ok=True)
        if transaction:
            transaction.snapshot(protected_texture_file)
        temp = protected_texture_file.with_suffix(protected_texture_file.suffix + ".tmp")
        temp.write_text(json.dumps(sorted(protected), indent=2) + "\n", encoding="utf-8")
        os.replace(temp, protected_texture_file)
    return linked, skipped, operations


def check_deploy_path_lengths(package: Path, data_dir: Path, plugin_label: str) -> None:
    """Fail before changing Data Files if deploy or staged cleanup would exceed classic MAX_PATH."""
    package = package.resolve()
    data_dir = data_dir.resolve()
    off_root = package / "Off" / _safe_name(Path(plugin_label).stem) / "replaced"
    too_long = []
    seen = set()

    def check(path: Path) -> None:
        value = os.path.abspath(str(path))
        key = os.path.normcase(value)
        if key in seen:
            return
        seen.add(key)
        length = len(value)
        if length >= WINDOWS_MAX_PATH:
            too_long.append((length, value))

    for base, dirs, files in os.walk(package):
        base_path = Path(base)
        check(base_path)
        for directory in dirs:
            check(base_path / directory)
        for name in files:
            source = base_path / name
            check(source)  # staged tree may need removal after deploy, including skipped folders
            rel = source.relative_to(package)
            if any(part.casefold() in SKIP_DIRS for part in rel.parts) or name.startswith("."):
                continue
            if source.suffix.casefold() in {".esp", ".esm", ".bsa"}:
                rel = Path(name)
            target = data_dir / rel
            check(target)
            if target.exists() and source.suffix.casefold() != ".lua":
                check(stamped_backup_path(off_root / rel))         # the name deploy_package will use
    if too_long:
        too_long.sort(reverse=True)
        shown = too_long[:12]
        lines = [f"{length} characters: {path}" for length, path in shown]
        rename_plan = _plan_short_filenames(package, data_dir)
        if rename_plan:
            lines.extend(["", "Proposed short-name plan (not applied):"])
            lines.extend(f"{entry['relative_path']} -> {entry['suggested_name']} "
                         f"[ESP refs: {', '.join(entry['esp_refs']) or 'none found'}; "
                         f"ESM refs: {', '.join(entry['esm_refs']) or 'none found'}]"
                         for entry in rename_plan[:8])
        if len(too_long) > len(shown):
            lines.append(f"... and {len(too_long) - len(shown):,} additional overlong paths")
        raise ValueError(
            f"Deployment stopped before changing Data Files: {len(too_long):,} path(s) reach or exceed "
            f"the classic Windows {WINDOWS_MAX_PATH}-character path limit. Shorten the staging or game "
            "path, or enable long-path support for the tool and Windows, then retry. The proposed short-name "
            "plan is informational only; asset renames must update ESP references and route ESM changes through "
            "the core ESM patch.\n\n"
            + "\n".join(lines)
        )


def _plan_short_filenames(package: Path, data_dir: Path) -> list[dict]:
    """Plan deterministic asset basename shortening and identify plugin references; never edits files."""
    package, data_dir = package.resolve(), data_dir.resolve()
    plugins = []
    for base, dirs, files in os.walk(package):
        dirs[:] = [directory for directory in dirs if directory.casefold() not in SKIP_DIRS]
        plugins.extend(Path(base) / name for name in files
                       if Path(name).suffix.casefold() in {".esp", ".esm"})
    plugin_bytes = {}
    for path in plugins:
        try:
            plugin_bytes[path.name] = (path.suffix.casefold(), path.read_bytes().lower())
        except OSError:
            continue

    plan = []
    for base, dirs, files in os.walk(package):
        base_path = Path(base)
        dirs[:] = [directory for directory in dirs if directory.casefold() not in SKIP_DIRS]
        for name in files:
            source = base_path / name
            if name.startswith("."):
                continue
            rel = source.relative_to(package)
            target_rel = Path(name) if source.suffix.casefold() in {".esp", ".esm", ".bsa"} else rel
            target = data_dir / target_rel
            if max(len(os.path.abspath(source)), len(os.path.abspath(target))) < WINDOWS_MAX_PATH:
                continue
            extension = source.suffix
            parent_paths = [source.parent, target.parent]
            available = min(WINDOWS_MAX_PATH - 1 - len(os.path.abspath(parent)) - 1
                            for parent in parent_paths)
            stem = source.stem
            token = hashlib.sha256(str(rel).encode("utf-8", "replace")).hexdigest()[:8]
            room = available - len(extension) - len(token) - 1
            if room < 1:
                continue
            suggested = stem[:room] + "~" + token + extension
            old_name = name.encode("cp1252", "ignore").lower()
            esp_refs = sorted((plugin for plugin, (suffix, data) in plugin_bytes.items()
                               if suffix == ".esp" and old_name and old_name in data), key=str.casefold)
            esm_refs = sorted((plugin for plugin, (suffix, data) in plugin_bytes.items()
                               if suffix == ".esm" and old_name and old_name in data), key=str.casefold)
            plan.append({"relative_path": str(rel), "suggested_name": suggested,
                         "esp_refs": esp_refs, "esm_refs": esm_refs})
    return plan


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def remove_superseded_plugins(package: Path, item: dict) -> list[str]:
    """Keep old copies from multi-plugin archives out of Data Files."""
    stale = {name.casefold() for name in item.get("superseded_plugins", [])}
    moved = []
    if not stale:
        return moved
    for base, dirs, files in os.walk(package):
        dirs[:] = [directory for directory in dirs if directory.casefold() not in SKIP_DIRS]
        for filename in files:
            if filename.casefold() not in stale:
                continue
            source = Path(base) / filename
            rel = source.relative_to(package)
            root = package / "Off" / _safe_name(Path(filename).stem) / "superseded plugins"
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target = target.with_name(target.stem + "_duplicate" + target.suffix)
            shutil.move(str(source), str(target))
            moved.append(filename)
    return moved


def _deployment_state_path(manager: Path) -> Path:
    return manager / "plugin_deployments.json"


def _read_deployment_state(manager: Path) -> dict:
    try:
        value = json.loads(_deployment_state_path(manager).read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("deployments"), list):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "deployments": []}


def _write_deployment_state(manager: Path, state: dict) -> None:
    path = _deployment_state_path(manager)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def record_deployment(manager: Path, item: dict, package: Path, operations: list[dict],
                      archives: list[Path], registrations: list[str], data_dir: Path | None = None) -> dict:
    state = _read_deployment_state(manager)
    source_archive = item.get("archive")
    _apply_nexus_id_fallback(item, Path(source_archive)) if source_archive else None
    label = item.get("archive_label") or (Path(source_archive).name if source_archive else package.name)
    entry = {"id": uuid.uuid4().hex, "label": label,
             "deployed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "archive": str(Path(source_archive).resolve()) if source_archive and not item.get("staged_only") else None,
             "nexus_mod_id": item.get("nexus_mod_id"), "nexus_file_id": item.get("nexus_file_id"),
             "rank": item.get("archive_rank"), "plugins": item.get("selected_plugins", item.get("plugins", [])),
             "staging_package": str(package.resolve()),
             "archives": [path.name for path in archives],
             "registered_archives": registrations, "operations": operations}
    state["deployments"].append(entry)
    _write_deployment_state(manager, state)
    if data_dir is not None:
        import mwmanager_journal
        mwmanager_journal.Journal(manager / "journal").record_deployment(
            Path(data_dir).parent, operations, tool="plugin_install.py", tag="plugin-deployment",
            inputs={"archive": source_archive or label, "plugins": entry["plugins"]})
    return entry


def index_deployment(manager: Path, data_dir: Path, plugins: list[str], archives: list[Path]) -> None:
    """Refresh the persistent record and BSA catalog after a package is deployed."""
    import sys
    catalog_dir = manager / "database"
    if str(catalog_dir) not in sys.path:
        sys.path.insert(0, str(catalog_dir))
    import catalog as catalog_module
    db_path = manager / "game_catalog.sqlite3"
    schema = catalog_dir / "schema.sql"
    active_order = []
    try:
        active_order = [line.strip() for line in (manager / "mwmanager_sort_order.txt").read_text(
            encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith(";")]
    except OSError:
        pass
    priority = {name.casefold(): index + 3 for index, name in enumerate(active_order)}
    with sqlite3.connect(db_path) as db:
        catalog_module.create_schema(db, schema)
        for index, plugin in enumerate(plugins):
            path = data_dir / plugin
            if path.is_file():
                catalog_module.index_esm(db, path, priority.get(plugin.casefold(), len(priority) + index + 3))
        for archive in archives:
            if archive.is_file():
                catalog_module.index_bsa(db, archive)


def list_deployments(manager: Path) -> list[dict]:
    return list(_read_deployment_state(manager)["deployments"])


def remove_deployments(manager: Path, data_dir: Path, deployment_ids: set[str]) -> dict:
    """Remove selected tracked links, restore replaced files, unregister their BSAs, and unindex them."""
    from major_master_deploy import unregister_archives
    manager, data_dir = manager.resolve(), data_dir.resolve()
    state = _read_deployment_state(manager)
    kept, removed, restored, pending, tidy_packages = [], 0, 0, [], []
    ini_path = data_dir.parent / "Morrowind.ini"
    catalog_path = manager / "game_catalog.sqlite3"
    state_path = _deployment_state_path(manager)
    touched_plugins = set()
    touched_archives = set()
    recoveries = []
    original_ini = ini_path.read_bytes() if ini_path.is_file() else None
    original_state = state_path.read_bytes() if state_path.is_file() else None
    catalog_backup = None
    if catalog_path.is_file():
        catalog_backup = manager / f".{catalog_path.name}.{uuid.uuid4().hex}.rollback"
        with sqlite3.connect(catalog_path) as source, sqlite3.connect(catalog_backup) as destination:
            source.backup(destination)

    try:
        for deployment in reversed(state["deployments"]):
            if deployment.get("id") not in deployment_ids:
                kept.append(deployment)
                continue
            package = Path(deployment.get("staging_package", "")).resolve()
            operations_pending = []
            planned = []
            for operation in reversed(deployment.get("operations", [])):
                source_raw = Path(operation.get("source", ""))
                target_raw = Path(operation.get("target", ""))
                backup_raw = Path(operation["backup"]) if operation.get("backup") else None
                if not target_raw.is_absolute():
                    raise ValueError(f"Deployment state contains a non-absolute target path: {target_raw}")
                try:
                    target_rel = target_raw.relative_to(data_dir)
                except ValueError as exc:
                    raise ValueError(f"Deployment state contains an unsafe target path: {target_raw}") from exc
                target = _safe_child(data_dir, target_rel, label="Deployment removal target")
                if not source_raw.is_absolute():
                    raise ValueError(f"Deployment state contains a non-absolute source path: {source_raw}")
                try:
                    source_rel = source_raw.relative_to(package)
                except ValueError as exc:
                    raise ValueError(f"Deployment state contains an unsafe source path: {source_raw}") from exc
                source = _safe_child(package, source_rel, label="Deployment source")
                backup = None
                if backup_raw:
                    if not backup_raw.is_absolute():
                        raise ValueError(f"Deployment state contains a non-absolute backup path: {backup_raw}")
                    try:
                        backup_rel = backup_raw.relative_to(package)
                    except ValueError as exc:
                        raise ValueError(f"Deployment state contains an unsafe backup path: {backup_raw}") from exc
                    backup = _safe_child(package, backup_rel, label="Deployment backup")
                source_matches = False
                if source.is_file() and target.is_file():
                    try:
                        source_matches = os.path.samefile(source, target)
                    except OSError:
                        pass
                if not source_matches and target.is_file() and operation.get("sha256"):
                    try:
                        source_matches = (target.stat().st_size == int(operation.get("size", -1))
                                          and _sha256_file(target) == operation["sha256"])
                    except (OSError, ValueError):
                        pass
                if not source_matches and target.exists():
                    operations_pending.append(operation)
                else:
                    planned.append((target, backup, source_matches))
            for target, backup, source_matches in planned:
                recovery = None
                if source_matches:
                    recovery = target.with_name(f".{target.name}.{uuid.uuid4().hex}.mwmanager-remove")
                    os.replace(target, recovery)
                    recoveries.append((recovery, target, backup))
                    removed += 1
                if backup and backup.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, target)
                    restored += 1
                    if recovery is None:
                        recoveries.append((None, target, backup))
            if operations_pending:
                pending.append({**deployment, "operations": list(reversed(operations_pending))})
                continue
            touched_plugins.update(deployment.get("plugins", []))
            touched_archives.update(deployment.get("archives", []))
            if deployment.get("staging_package"):
                tidy_packages.append(deployment["staging_package"])
            unregister_archives(ini_path, deployment.get("registered_archives", []))
            if catalog_path.is_file():
                with sqlite3.connect(catalog_path) as db:
                    for plugin in deployment.get("plugins", []):
                        db.execute("DELETE FROM esm_record WHERE lower(source_file)=lower(?)", (plugin,))
                    for archive in deployment.get("archives", []):
                        db.execute("DELETE FROM bsa_asset WHERE lower(archive_file)=lower(?)", (archive,))
                    db.commit()
        restored_plugins = [plugin for plugin in touched_plugins if (data_dir / plugin).is_file()]
        if touched_plugins and catalog_path.is_file():
            absent_plugins = [plugin for plugin in touched_plugins if not (data_dir / plugin).is_file()]
            with sqlite3.connect(catalog_path) as db:
                for plugin in absent_plugins:
                    db.execute("DELETE FROM esm_record WHERE lower(source_file)=lower(?)", (plugin,))
                db.commit()
        restored_archives = [name for name in touched_archives if (data_dir / name).is_file()]
        absent_archives = [name for name in touched_archives if not (data_dir / name).is_file()]
        if catalog_path.is_file() and absent_archives:
            with sqlite3.connect(catalog_path) as db:
                for archive in absent_archives:
                    db.execute("DELETE FROM bsa_asset WHERE lower(archive_file)=lower(?)", (archive,))
                db.commit()
        if catalog_path.is_file() and (restored_plugins or restored_archives):
            index_deployment(manager, data_dir, restored_plugins,
                             [data_dir / name for name in restored_archives])
        pending_by_id = {entry.get("id"): entry for entry in pending}
        remaining_ids = {entry.get("id") for entry in kept + pending}
        state["deployments"] = [
            pending_by_id.get(entry.get("id"), entry)
            for entry in state["deployments"] if entry.get("id") in remaining_ids
        ]
        _write_deployment_state(manager, state)
    except BaseException:
        rollback_errors = []
        for recovery, target, backup in reversed(recoveries):
            try:
                if target.exists() and backup and not backup.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, backup)
                if recovery and recovery.exists():
                    os.replace(recovery, target)
            except OSError as exc:
                rollback_errors.append(str(exc))
        try:
            if original_ini is None:
                ini_path.unlink(missing_ok=True)
            else:
                ini_path.write_bytes(original_ini)
        except OSError as exc:
            rollback_errors.append(str(exc))
        try:
            if catalog_backup and catalog_backup.is_file():
                os.replace(catalog_backup, catalog_path)
        except OSError as exc:
            rollback_errors.append(str(exc))
        try:
            if original_state is None:
                state_path.unlink(missing_ok=True)
            else:
                state_path.write_bytes(original_state)
        except OSError as exc:
            rollback_errors.append(str(exc))
        if rollback_errors:
            raise RuntimeError("Deployment removal failed and rollback was incomplete: "
                               + "; ".join(rollback_errors)) from None
        raise
    finally:
        if catalog_backup:
            catalog_backup.unlink(missing_ok=True)
    for recovery, _target, _backup in recoveries:
        if recovery:
            recovery.unlink(missing_ok=True)
    return {"removed": removed, "restored": restored,
            "pending": sum(len(item["operations"]) for item in pending),
            "tidy_packages": sorted(set(tidy_packages), key=str.casefold)}


def steam_morrowind_roots() -> list[Path]:
    """Morrowind folders in the Steam libraries Steam itself records (registry + libraryfolders.vdf)."""
    steam_dirs: list[Path] = []
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                steam_dirs.append(Path(winreg.QueryValueEx(key, "SteamPath")[0]))
        except (OSError, ImportError):
            pass
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(env):
            steam_dirs.append(Path(os.environ[env]) / "Steam")
    libraries: list[Path] = []
    for steam in steam_dirs:
        libraries.append(steam)
        try:
            text = (steam / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        libraries += [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"([^"]+)"', text)]
    roots, seen = [], set()
    for library in libraries:
        root = library / "steamapps" / "common" / "Morrowind"
        key = os.path.normcase(os.path.abspath(root))
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


def steam_lua_root() -> Path | None:
    for root in steam_morrowind_roots():
        lua = root / "Data Files" / "MWSE" / "mods"
        if lua.is_dir():
            return lua
    return None


# REMOVE BEFORE SHIPPING: developer-machine workaround (the user's Steam install holds their luafix-edited Lua).
def pull_updated_steam_luas(package: Path, ask) -> list[str]:
    """Developer machine only: for each staged Lua that differs from the Steam install's copy at the same MWSE path,
    show the diff and import the Steam copy only when ask(title, text) says yes. The archive remains the backup."""
    import difflib
    steam_root = steam_lua_root()
    if not steam_root:
        return []
    updated = []
    for base, dirs, files in os.walk(package):
        dirs[:] = [d for d in dirs if d.casefold() not in SKIP_DIRS]
        for name in files:
            if Path(name).suffix.casefold() != ".lua":
                continue
            source = Path(base) / name
            rel = source.relative_to(package)
            parts = list(rel.parts)
            lower = [part.casefold() for part in parts]
            try:            # the path under MWSE\mods, which is what steam_lua_root() points at
                marker = lower.index("mods")
                lua_rel = Path(*parts[marker + 1:])
            except ValueError:
                continue
            if not lua_rel.parts:
                continue
            candidate = steam_root / lua_rel
            if not candidate.is_file() or candidate.read_bytes() == source.read_bytes():
                continue
            diff = "".join(difflib.unified_diff(
                source.read_text("utf-8", errors="replace").splitlines(keepends=True),
                candidate.read_text("utf-8", errors="replace").splitlines(keepends=True),
                f"staged/{lua_rel.as_posix()}", f"steam/{lua_rel.as_posix()}"))
            if not ask("Lua differs from the Steam copy",
                       f"{lua_rel} in {package.name} differs from the Steam game's copy.\n\n{diff}\n"
                       "Import the Steam copy into this package?"):
                continue
            # Update the staged copy before linking so deployment payload tracking remains exact.
            shutil.copy2(candidate, source)
            updated.append(str(lua_rel))
    return updated

