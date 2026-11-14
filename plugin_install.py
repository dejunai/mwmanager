"""Archive discovery, ordering, extraction, and regular-plugin deployment helpers."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import uuid
import zipfile
from datetime import datetime

from core_addons import (ARCHIVE_EXTENSIONS, _members, _member_preflight,
                         _archive_rank, _archive_esm_masters,
                         _quiet_subprocess_options, _seven_zip)

SKIP_DIRS = {"off", "bugreports", "fomod", "extras", "docs", "screenshots"}
BSA_FOLDERS = {"bookart", "icons", "meshes", "textures", "video"}
WINDOWS_MAX_PATH = 260  # MAX_PATH includes the terminating NUL; paths must stay below this length.
PLUGIN_RENAMES = {"anthologysolstheim.esp": "AnthologySolstheimPatched.esp"}
MASTER_RENAMES = {"tr_data.esm": "Tamriel_Data.esm"}
ARCHIVE_SCAN_CACHE_VERSION = 1
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
     "Morrowind Code Patch is a standalone utility and is not installed as a plugin."),
    ("mwse", "plugin", re.compile(r"^(?:mwse(?:[ _-](?:v?\d.*|core|installer|setup))?|morrowind[ _-]+script[ _-]+extender(?:[ _-].*)?)$", re.I),
     "MWSE is a standalone utility and is not installed as a plugin."),
    ("mge_xe", "plugin", re.compile(r"^(?:mge[ _-]*xe|morrowind[ _-]+graphics[ _-]+extender[ _-]+xe)(?:[ _-].*)?$", re.I),
     "MGE XE is a standalone utility and is not installed as a plugin."),
    ("nifzer0edit", "root", re.compile(r"^nifzer0edit(?:[ _-].*)?$", re.I),
     "NifZer0Edit installs into the Morrowind root; root utility deployment is not available yet."),
    ("breezify", "root", re.compile(r"^breezify(?:[ _-].*)?$", re.I),
     "Breezify installs into the Morrowind root; root utility deployment is not available yet."),
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


def _canonical_plugin_name(name: str) -> str:
    renamed = PLUGIN_RENAMES.get(name.casefold())
    if renamed:
        return renamed
    if Path(name).suffix.casefold() == ".esp" and re.sub(r"[^a-z0-9]", "", Path(name).stem.casefold()) == "anthologysolstheim":
        return "AnthologySolstheimPatched.esp"
    return name


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .") or "plugin-archive"


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
        return None, None
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
    for index, archive in enumerate(archives, 1):
        cache_key = str(archive.resolve()).casefold()
        fingerprint = _scan_cache_fingerprint(archive)
        cached = old_cache.get(cache_key)
        if cached and cached.get("fingerprint") == fingerprint and isinstance(cached.get("item"), dict):
            if progress:
                progress(f"Using cached scan {index} of {len(archives)}: {archive.name}")
            item = cached["item"]
            _apply_nexus_id_fallback(item, archive)
            found.append(item)
            new_cache[cache_key] = cached
            continue
        if progress:
            progress(f"Inspecting archive {index} of {len(archives)}: {archive.name}")
        names, error = _members(archive, seven_zip)
        normalized = [name.replace("\\", "/").lstrip("/") for name in names]
        blocked_payloads = sorted({name for name in normalized
                                   if Path(name).suffix.casefold() in BLOCKED_PAYLOAD_EXTENSIONS
                                   and not any(part.casefold() == "docs" for part in Path(name).parts)
                                   and not any("openmw" in part.casefold() for part in Path(name).parts[:-1])
                                   and not _allowed_script_payload(name)},
                                  key=str.casefold)
        archive_plugins = sorted({Path(name).name for name in normalized
                                  if Path(name).suffix.casefold() in {".esp", ".esm"}}, key=str.casefold)
        plugin_masters = {}
        for archive_plugin in archive_plugins:
            plugin = _canonical_plugin_name(archive_plugin)
            masters, master_error = _archive_esm_masters(archive, archive_plugin, manager, names)
            if not master_error:
                plugin_masters[plugin] = [MASTER_RENAMES.get(master.casefold(), master) for master in masters]
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
                "blocked_payloads": blocked_payloads,
                "blocked": bool(error or unsafe or collisions or utility or blocked_payloads),
                "problem": "; ".join(filter(None, [error, "; ".join(unsafe + collisions),
                                                     ("Contains blocked executable/library/script file(s): "
                                                      + ", ".join(blocked_payloads[:12])
                                                      + (f" (and {len(blocked_payloads) - 12} more)"
                                                         if len(blocked_payloads) > 12 else ""))
                                                     if blocked_payloads else None,
                                                     utility[1] if utility else None])),
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
            count += sum(1 for name in files if not name.startswith("."))
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


def pack_package_assets(package: Path, plugin_name: str, manager: Path) -> list[Path]:
    from major_master_deploy import _pack_assets, find_bsapack
    tool = find_bsapack()
    if not tool:
        raise FileNotFoundError("bsapack.dej.exe was not found in MWManager's tools folder or on PATH.")
    return _pack_assets(package, plugin_name, tool)


def extract_archive(item: dict, destination: Path, manager: Path) -> Path:
    archive = Path(item["archive"])
    if item.get("blocked"):
        raise ValueError(f"Archive needs review: {archive.name}: {item.get('problem') or 'unsafe archive'}")
    names, error = _members(archive, _seven_zip(manager))
    if error:
        raise RuntimeError(f"Could not inspect {archive.name}: {error}")
    unsafe, collisions = _member_preflight(names)
    if unsafe or collisions:
        raise ValueError(f"Archive paths are unsafe: {archive.name}: " + "; ".join(unsafe + collisions))
    destination.mkdir(parents=True, exist_ok=False)
    if archive.suffix.casefold() == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for entry in zf.infolist():
                if entry.is_dir():
                    continue
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Archive contains a symbolic link: {entry.filename}")
                target = (destination / Path(entry.filename.replace("\\", "/"))).resolve()
                if destination.resolve() not in target.parents:
                    raise ValueError(f"Archive path escapes the temporary install: {entry.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
    else:
        seven_zip = _seven_zip(manager)
        if not seven_zip:
            raise FileNotFoundError("7-Zip is required to install this archive format.")
        result = subprocess.run([seven_zip, "x", str(archive), f"-o{destination}", "-y"],
                                capture_output=True, text=True, errors="replace", timeout=7200,
                                **_quiet_subprocess_options())
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or f"7-Zip exited {result.returncode}")[-3000:])
    return destination


def deploy_package(package: Path, data_dir: Path, plugin_label: str,
                   protected_texture_file: Path | None = None) -> tuple[int, int, list[dict]]:
    """Deploy tidied files, retaining replacer textures against later unrelated overrides."""
    check_deploy_path_lengths(package, data_dir, plugin_label)
    linked = skipped = 0
    operations = []
    off_root = package / "Off" / _safe_name(Path(plugin_label).stem) / "replaced"
    texture_paths = set()
    meshes_present = False
    for base, dirs, files in os.walk(package):
        dirs[:] = [name for name in dirs if name.casefold() not in SKIP_DIRS]
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
        dirs[:] = [name for name in dirs if name.casefold() not in SKIP_DIRS]
        for name in files:
            source = Path(base) / name
            rel = source.relative_to(package)
            if not rel.parts or name.startswith("."):
                continue
            if source.suffix.casefold() in {".esp", ".esm", ".bsa"}:
                rel = Path(name)
            target = data_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            rel_key = str(rel).replace("\\", "/").casefold()
            is_texture = source.suffix.casefold() in {".dds", ".tga", ".bmp", ".png", ".jpg", ".jpeg"}
            if is_texture and target.exists() and rel_key in protected and not protect_this_package:
                retained = package / "Off" / _safe_name(Path(plugin_label).stem) / "protected textures" / rel
                retained.parent.mkdir(parents=True, exist_ok=True)
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
                if source.suffix.casefold() == ".lua":
                    # The source archive is retained under Vortex Downloads; don't duplicate an old Lua in Off.
                    target.unlink()
                else:
                    backup = off_root / rel
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    if backup.exists():
                        backup = backup.with_name(backup.name + ".previous")
                    os.replace(target, backup)
            copied = False
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
                copied = True
            linked += 1
            operations.append({"source": str(source.resolve()), "target": str(target.resolve()),
                               "backup": str(backup.resolve()) if backup is not None else None,
                               "size": source.stat().st_size,
                               "sha256": _sha256_file(source) if copied else None})
    if protect_this_package:
        protected.update(texture_paths)
    if protected_texture_file:
        protected_texture_file.parent.mkdir(parents=True, exist_ok=True)
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
                backup = off_root / rel
                if backup.exists():
                    backup = backup.with_name(backup.name + ".previous")
                check(backup)
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
                      archives: list[Path], registrations: list[str]) -> dict:
    state = _read_deployment_state(manager)
    source_archive = item.get("archive")
    _apply_nexus_id_fallback(item, Path(source_archive)) if source_archive else None
    label = item.get("archive_label") or (Path(source_archive).name if source_archive else package.name)
    entry = {"id": uuid.uuid4().hex, "label": label,
             "archive": str(Path(source_archive).resolve()) if source_archive and not item.get("staged_only") else None,
             "nexus_mod_id": item.get("nexus_mod_id"), "nexus_file_id": item.get("nexus_file_id"),
             "rank": item.get("archive_rank"), "plugins": item.get("selected_plugins", item.get("plugins", [])),
             "staging_package": str(package.resolve()),
             "archives": [path.name for path in archives],
             "registered_archives": registrations, "operations": operations}
    state["deployments"].append(entry)
    _write_deployment_state(manager, state)
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
    kept, removed, restored, pending = [], 0, 0, []
    ini_path = data_dir.parent / "Morrowind.ini"
    catalog_path = manager / "game_catalog.sqlite3"
    touched_plugins = set()
    touched_archives = set()
    for deployment in reversed(state["deployments"]):
        if deployment.get("id") not in deployment_ids:
            kept.append(deployment)
            continue
        operations_pending = []
        for operation in reversed(deployment.get("operations", [])):
            source = Path(operation.get("source", ""))
            target = Path(operation.get("target", ""))
            backup = Path(operation["backup"]) if operation.get("backup") else None
            package = Path(deployment.get("staging_package", ""))
            if not target.is_absolute() or not target.is_relative_to(data_dir):
                raise ValueError(f"Deployment state contains an unsafe target path: {target}")
            if not source.is_absolute() or not source.is_relative_to(package):
                raise ValueError(f"Deployment state contains an unsafe source path: {source}")
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
                except OSError:
                    pass
            if source_matches:
                target.unlink()
                removed += 1
            elif target.exists():
                operations_pending.append(operation)
                continue
            if backup and backup.is_file() and not target.exists():
                if not backup.is_relative_to(package):
                    raise ValueError(f"Deployment state contains an unsafe backup path: {backup}")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
                restored += 1
        if operations_pending:
            pending.append({**deployment, "operations": list(reversed(operations_pending))})
            continue
        touched_plugins.update(deployment.get("plugins", []))
        touched_archives.update(deployment.get("archives", []))
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
        index_deployment(manager, data_dir, restored_plugins, [data_dir / name for name in restored_archives])
    remaining_ids = {entry.get("id") for entry in kept + pending}
    state["deployments"] = [entry for entry in state["deployments"]
                            if entry.get("id") in remaining_ids]
    _write_deployment_state(manager, state)
    return {"removed": removed, "restored": restored, "pending": sum(len(item["operations"]) for item in pending)}


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


def pull_updated_steam_luas(package: Path) -> list[str]:
    """Refresh packaged Lua files from matching Steam MWSE paths; archive remains the backup."""
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
            try:
                marker = lower.index("mwse")
                lua_rel = Path(*parts[marker + 1:])
            except ValueError:
                try:
                    marker = lower.index("mods")
                    lua_rel = Path(*parts[marker + 1:])
                except ValueError:
                    continue
            if not lua_rel.parts:
                continue
            candidate = steam_root / lua_rel
            if not candidate.is_file():
                continue
            # Update the staged copy before linking so deployment payload tracking remains exact.
            shutil.copy2(candidate, source)
            updated.append(str(lua_rel))
    return updated

