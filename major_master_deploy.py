"""Deploy already-tidied major masters from staging into the configured game."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time

BSA_FOLDERS = {"bookart", "icons", "meshes", "textures", "video"}
SKIP_FOLDERS = {"off", "bugreports", "fomod", "extras"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def staged_masters(staging: Path, configured: list[str]) -> dict[str, Path]:
    wanted = {name.casefold(): name for name in configured}
    found: dict[str, Path] = {}
    if not staging.is_dir():
        return found
    for package in sorted((p for p in staging.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        for base, dirs, files in os.walk(package):
            dirs[:] = [name for name in dirs if name.casefold() not in SKIP_FOLDERS]
            for filename in files:
                key = filename.casefold()
                if key in wanted and Path(filename).suffix.casefold() == ".esm":
                    found.setdefault(key, Path(base) / filename)
    return found


def _data_relative(package: Path, path: Path) -> Path:
    """Strip common archive wrapper folders when deriving a Data Files path."""
    rel = path.relative_to(package)
    parts = list(rel.parts)
    while parts and parts[0].casefold() in {"00 data files", "data files"}:
        parts.pop(0)
    return Path(*parts)


def count_bsa_eligible(staging: Path, masters: list[str]) -> int:
    packages = set()
    for name, esm in staged_masters(staging, masters).items():
        packages.add(next((parent for parent in esm.parents if parent.parent == staging), esm.parent))
    count = 0
    for package in packages:
        for base, dirs, files in os.walk(package):
            rel = _data_relative(package, Path(base))
            dirs[:] = [d for d in dirs if d.casefold() not in SKIP_FOLDERS]
            if not rel.parts or rel.parts[0].casefold() not in BSA_FOLDERS:
                continue
            count += sum(1 for f in files if not f.startswith("."))
    return count


def _package_roots(staging: Path, configured: list[str]) -> list[tuple[Path, list[str]]]:
    wanted = {name.casefold() for name in configured}
    priority = {name.casefold(): i for i, name in enumerate(configured)}
    result: dict[Path, set[str]] = {}
    for key, esm in staged_masters(staging, configured).items():
        if key not in wanted:
            continue
        package = next((parent for parent in esm.parents if parent.parent == staging), esm.parent)
        result.setdefault(package, set()).add(esm.name)
    # Process lowest UI priority first so higher-priority packages replace them last.
    return sorted(((package, sorted(names, key=lambda n: priority.get(n.casefold(), 99999)))
                   for package, names in result.items()),
                  key=lambda item: min((priority.get(name.casefold(), 99999) for name in item[1]), default=99999),
                  reverse=True)


def _bsa_files(package: Path) -> list[Path]:
    return sorted((p for p in package.glob("*.bsa") if p.is_file()), key=lambda p: p.name.casefold())


def find_bsapack() -> Path | None:
    """BSAPack from MWManager's own tools folder, else from PATH (no fixed install locations)."""
    bundled = Path(__file__).resolve().parent / "tools" / "bsapack.dej.exe"
    if bundled.is_file():
        return bundled
    located = shutil.which("bsapack.dej") or shutil.which("bsapack.dej.exe")
    return Path(located) if located else None


def _pack_assets(package: Path, plugin_name: str, tool: Path) -> list[Path]:
    """Pack supported asset folders. The tool handles automatic 2 GB splitting."""
    with tempfile.TemporaryDirectory(prefix="mwmanager-bsa-") as temp_name:
        temp = Path(temp_name)
        count = 0
        for base, dirs, files in os.walk(package):
            rel = _data_relative(package, Path(base))
            dirs[:] = [d for d in dirs if d.casefold() not in SKIP_FOLDERS]
            if not rel.parts or rel.parts[0].casefold() not in BSA_FOLDERS:
                continue
            for name in files:
                src = Path(base) / name
                dst = temp / rel / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
                count += 1
        if not count:
            return []
        base_name = Path(plugin_name).stem + "_assets.bsa"
        proc = subprocess.run([str(tool), "pack", base_name], cwd=temp, capture_output=True,
                              text=True, errors="replace", timeout=7200,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode:
            raise RuntimeError(f"BSAPack failed for {plugin_name}: {(proc.stderr or proc.stdout)[-2000:]}")
        outputs = sorted(temp.glob(Path(base_name).stem + "*.bsa"), key=lambda p: p.name.casefold())
        if not outputs:
            raise RuntimeError(f"BSAPack reported success but created no archive for {plugin_name}.")
        off = package / "Off" / Path(plugin_name).stem / "bsa packed"
        moved: list[Path] = []
        for source in outputs:
            destination = package / source.name
            if destination.exists():
                destination = package / (source.stem + "_mwmanager" + source.suffix)
            shutil.move(str(source), destination)
            moved.append(destination)
        # Move packed loose assets to Off/<plugin>/bsa packed/<asset path>, including wrapped layouts.
        asset_roots = []
        for base, dirs, _files in os.walk(package):
            normalized = _data_relative(package, Path(base))
            dirs[:] = [d for d in dirs if d.casefold() not in SKIP_FOLDERS]
            if len(normalized.parts) == 1 and normalized.parts[0].casefold() in BSA_FOLDERS:
                asset_roots.append(Path(base))
        for folder in asset_roots:
            dest = off / folder.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(folder), dest)
        return moved


def register_archives(ini_path: Path, archives: list[Path], manager_dir: Path | None = None) -> list[str]:
    """Register archives and return only the exact entries this call added."""
    if not archives:
        return []
    text = ini_path.read_text(encoding="cp1252", errors="replace") if ini_path.exists() else ""
    lines = text.splitlines()
    section = None
    archive_indices = []
    known = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].casefold()
        elif section == "archives" and "=" in line:
            key, value = line.split("=", 1)
            if key.strip().casefold().startswith("archive"):
                try:
                    archive_indices.append(int(key.strip()[7:].strip()))
                except ValueError:
                    pass
                known.add(value.strip().casefold())
    additions = [p.name for p in archives if p.name.casefold() not in known]
    if not additions:
        return []
    if any(line.strip().casefold() == "[archives]" for line in lines):
        start = next(i for i, line in enumerate(lines) if line.strip().casefold() == "[archives]")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        next_index = max(archive_indices, default=-1) + 1
        added_lines = [f"Archive {next_index + i}={name}" for i, name in enumerate(additions)]
        lines[end:end] = added_lines
    else:
        if lines and lines[-1].strip():
            lines.append("")
        added_lines = [f"Archive {i}={name}" for i, name in enumerate(additions)]
        lines += ["[Archives]"] + added_lines
    if manager_dir is None and ini_path.is_file():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = ini_path.with_name(f"{ini_path.name}.mwmanager-predeploy-{stamp}.bak")
        suffix = 1
        while backup.exists():
            backup = ini_path.with_name(f"{ini_path.name}.mwmanager-predeploy-{stamp}-{suffix}.bak")
            suffix += 1
        shutil.copy2(ini_path, backup)
    temp = ini_path.with_suffix(ini_path.suffix + ".mwmanager-tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="cp1252", errors="replace")
    os.replace(temp, ini_path)
    return added_lines


def unregister_archives(ini_path: Path, entries: list[str]) -> int:
    """Remove only the archive index/name entries saved by a deployment."""
    if not entries or not ini_path.is_file():
        return 0
    wanted = set()
    for entry in entries:
        if "=" in entry:
            key, value = entry.split("=", 1)
            wanted.add((key.strip().casefold(), value.strip().casefold()))
    lines = ini_path.read_text(encoding="cp1252", errors="replace").splitlines()
    section = None
    kept = []
    removed = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].casefold()
        if section == "archives" and "=" in line:
            key, value = line.split("=", 1)
            if (key.strip().casefold(), value.strip().casefold()) in wanted:
                removed += 1
                continue
        kept.append(line)
    if removed:
        temp = ini_path.with_suffix(ini_path.suffix + ".mwmanager-tmp")
        temp.write_text("\n".join(kept) + "\n", encoding="cp1252", errors="replace")
        os.replace(temp, ini_path)
    return removed


def _deployment_state_path(manager_dir: Path) -> Path:
    return manager_dir / "major_master_deployments.json"


def _read_deployment_state(manager_dir: Path) -> dict:
    try:
        data = json.loads(_deployment_state_path(manager_dir).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("deployments"), list):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "deployments": []}


def _write_deployment_state(manager_dir: Path, state: dict) -> None:
    path = _deployment_state_path(manager_dir)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def tracked_deployment_masters(manager_dir: Path) -> list[str]:
    state = _read_deployment_state(manager_dir)
    return list(dict.fromkeys(name for deployment in state["deployments"]
                              for name in deployment.get("masters", [])))


def has_tracked_deployment(manager_dir: Path) -> bool:
    state = _read_deployment_state(manager_dir)
    return any(Path(operation.get(key, "")).exists()
               for deployment in state["deployments"]
               for operation in deployment.get("operations", [])
               for key in ("target", "backup") if operation.get(key)) or any(
        Path(move.get(key, "")).exists()
        for deployment in state["deployments"]
        for move in deployment.get("moves", [])
        for key in ("source", "target") if move.get(key))


def _remove_firemoth_selected(manager_dir: Path, package: Path) -> bool:
    """Read the saved TR option state; fall back to an active remover ESP in the package."""
    journal = manager_dir / "outputs" / "mwmanager_compatibility_journal.json"
    try:
        state = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        state = {}
    for entry in state.get("mods", {}).values():
        if not isinstance(entry, dict):
            continue
        try:
            if Path(entry.get("mod_dir", "")).resolve() != package.resolve():
                continue
        except (OSError, ValueError):
            continue
        selected = [str(value).casefold() for value in entry.get("selected", [])]
        activation = entry.get("activation", {})
        active_files = [str(value).casefold() for value in activation.get("files", {})]
        if any("firemoth" in value and ("remov" in value or "replacer" in value)
               for value in [*selected, *active_files]):
            return True
        # A matching journal entry is authoritative, including when the option is unselected.
        return False
    for base, dirs, files in os.walk(package):
        dirs[:] = [name for name in dirs if name.casefold() not in SKIP_FOLDERS]
        if any(name.casefold() == "tr_firemoth_remover.esp" for name in files):
            return True
    return False


def remove_deployed(staging: Path, data_dir: Path, manager_dir: Path, progress=None) -> dict:
    """Remove only tracked MWManager links, restore overwritten files, and undo registrations/indexing."""
    staging, data_dir, manager_dir = staging.resolve(), data_dir.resolve(), manager_dir.resolve()
    state = _read_deployment_state(manager_dir)
    deployments = state["deployments"]
    if not deployments:
        return {"masters": [], "removed": 0, "restored": 0, "archives": [], "registrations": 0, "pending": 0}
    if os.path.splitdrive(str(staging))[0].casefold() != os.path.splitdrive(str(data_dir))[0].casefold():
        raise ValueError("Major-master staging and Data Files must be on the same drive.")
    removed = restored = registrations = 0
    all_masters, all_archives, pending_deployments = [], [], []
    ini_path = data_dir.parent / "Morrowind.ini"
    for deployment in reversed(deployments):
        all_masters.extend(deployment.get("masters", []))
        all_archives.extend(deployment.get("archives", []))
        operations = deployment.get("operations", [])
        pending_operations = []
        pending_moves = []
        for index, operation in enumerate(reversed(operations), 1):
            source = Path(operation.get("source", ""))
            target = Path(operation.get("target", ""))
            backup_name = operation.get("backup")
            backup = Path(backup_name) if backup_name else None
            if not target.is_absolute() or not target.is_relative_to(data_dir):
                raise ValueError(f"Deployment state contains an unsafe target path: {target}")
            if source.is_absolute() and not source.is_relative_to(staging):
                raise ValueError(f"Deployment state contains an unsafe source path: {source}")
            source_matches = False
            if target.is_file() and source.is_file():
                try:
                    source_matches = os.path.samefile(source, target)
                except OSError:
                    pass
            if source_matches:
                target.unlink()
                removed += 1
            elif target.exists() and (backup and backup.exists() or not source.exists()):
                # Preserve modified/uncertain targets and their backups for a later safe retry.
                pending_operations.append(operation)
                continue
            if backup and backup.is_file() and backup.is_relative_to(staging) and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
                restored += 1
            if progress and index % 100 == 0:
                progress(f"Removing deployed masters: checked {index:,} of {len(operations):,} files.")
        for move in reversed(deployment.get("moves", [])):
            source, target = Path(move.get("source", "")), Path(move.get("target", ""))
            if not source.is_absolute() or not target.is_absolute() or not source.is_relative_to(data_dir) or not target.is_relative_to(data_dir):
                raise ValueError(f"Deployment state contains an unsafe moved-file path: {source} -> {target}")
            if not target.exists():
                continue
            if source.exists() or (move.get("sha256") and sha256(target).casefold() != str(move["sha256"]).casefold()):
                pending_moves.append(move)
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)
            restored += 1
        if pending_operations or pending_moves:
            pending_deployments.append({**deployment, "operations": list(reversed(pending_operations)),
                                        "moves": list(reversed(pending_moves))})
            continue
        registrations += unregister_archives(ini_path, deployment.get("registered_archives", []))
        catalog = manager_dir / "game_catalog.sqlite3"
        if catalog.is_file():
            with sqlite3.connect(catalog) as db:
                for name in deployment.get("masters", []):
                    db.execute("DELETE FROM esm_record WHERE lower(source_file)=lower(?)", (name,))
                for name in deployment.get("archives", []):
                    db.execute("DELETE FROM bsa_asset WHERE lower(archive_file)=lower(?)", (name,))
                db.commit()
    state["deployments"] = list(reversed(pending_deployments))
    _write_deployment_state(manager_dir, state)
    return {"masters": list(dict.fromkeys(all_masters)), "removed": removed, "restored": restored,
            "archives": list(dict.fromkeys(all_archives)), "registrations": registrations,
            "pending": sum(len(item.get("operations", [])) + len(item.get("moves", []))
                            for item in pending_deployments)}

def deploy(staging: Path, data_dir: Path, manager_dir: Path, configured: list[str], *, build_bsa: bool = False,
           include_assets: bool = True, progress=None) -> dict:
    """Hardlink staged package files, register archives, and index ESM/BSA data."""
    staging, data_dir = staging.resolve(), data_dir.resolve()
    if os.path.splitdrive(str(staging))[0].casefold() != os.path.splitdrive(str(data_dir))[0].casefold():
        raise ValueError("Major-master staging and Data Files must be on the same drive for hardlinks.")
    staged = staged_masters(staging, configured)
    ready = [name for name in configured if name.casefold() in staged and not (data_dir / name).is_file()]
    if not ready:
        raise ValueError("No staged, uninstalled major masters are ready to deploy.")
    package_info = _package_roots(staging, ready)
    selected_for_bsa = ({name.casefold() for name in ready} if build_bsa is True else
                        {str(name).casefold() for name in build_bsa} if build_bsa else set())
    if selected_for_bsa:
        tool = find_bsapack()
        if not tool:
            raise FileNotFoundError("bsapack.dej.exe was not found in MWManager's tools folder or on PATH.")
        for package, plugins in package_info:
            selected = [plugin for plugin in plugins if plugin.casefold() in selected_for_bsa]
            if selected:
                plugin = selected[0]
                if progress: progress(f"Building BSA for {plugin} (BSAPack will split archives at 2 GB).")
                _pack_assets(package, plugin, tool)
    data_dir.mkdir(parents=True, exist_ok=True)
    linked, backed_up, skipped = [], [], []
    operations = []
    moves = []
    registered_archives = []
    rollback = []
    try:
        for package, plugins in package_info:
            if not any(name.casefold() == "tr_mainland.esm" for name in plugins):
                continue
            if _remove_firemoth_selected(manager_dir, package):
                if progress: progress("Tamriel Rebuilt includes the selected Firemoth remover; preserving the shipped Siege at Firemoth plugin.")
                continue
            source = data_dir / "Siege at Firemoth.esp"
            target = data_dir / "Off" / "Official Add-Ons" / source.name
            if not source.is_file():
                continue
            if target.exists():
                if progress: progress(f"Firemoth was not moved because the preservation path already exists: {target}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = sha256(source)
            os.replace(source, target)
            moves.append({"source": str(source), "target": str(target), "sha256": digest})
            if progress: progress("Moved shipped Siege at Firemoth.esp to Off/Official Add-Ons because Tamriel Rebuilt was deployed without Remove Firemoth.")
        for package, plugins in package_info:
            for base, dirs, files in os.walk(package):
                dirs[:] = [d for d in dirs if d.casefold() not in SKIP_FOLDERS]
                for filename in files:
                    source = Path(base) / filename
                    if filename.casefold() == ".mwmanager-stage.json" or filename.startswith("."):
                        continue
                    # Only deploy configured masters; other plugin files in the same archive remain staged.
                    if source.suffix.casefold() == ".esm" and filename.casefold() not in {n.casefold() for n in ready}:
                        continue
                    if not include_assets and source.suffix.casefold() not in {".esm", ".bsa", ".esp"}:
                        continue
                    rel = _data_relative(package, source)
                    if not rel.parts:
                        continue
                    # Plugins and archives must live at the Data Files root even if tidy left them nested.
                    target = (data_dir / filename if source.suffix.casefold() in {".esm", ".esp", ".bsa"}
                              else data_dir / rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    backup_path = None
                    if target.exists():
                        if target.is_file() and target.stat().st_size == source.stat().st_size and sha256(target) == sha256(source):
                            skipped.append(str(target.relative_to(data_dir)))
                            continue
                        if not target.is_file():
                            raise IsADirectoryError(f"A directory blocks deployment of {target}")
                        backup = package / "Off" / (plugins[0] if plugins else package.name) / "replaced" / rel
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        if backup.exists():
                            backup = backup.with_name(backup.name + ".previous")
                        os.replace(target, backup)
                        backup_path = backup
                        backed_up.append((target, backup))
                        rollback.append(("backup", target, backup))
                    os.link(source, target)
                    linked.append(str(rel))
                    operations.append({"source": str(source.resolve()), "target": str(target.resolve()),
                                       "backup": str(backup_path.resolve()) if backup_path else None})
                    rollback.append(("link", target, source))
        ini_path = data_dir.parent / "Morrowind.ini"
        archives = []
        seen_archives = set()
        for package, _plugins in package_info:
            for archive in _bsa_files(package):
                if archive.name.casefold() not in seen_archives:
                    archives.append(archive)
                    seen_archives.add(archive.name.casefold())
        registered_archives = register_archives(ini_path, archives, manager_dir)
        # Index after deployment. The catalog keeps full record bodies and BSA member provenance.
        catalog = manager_dir / "database"
        import sys
        sys.path.insert(0, str(catalog))
        import catalog as catalog_module
        db_path = manager_dir / "game_catalog.sqlite3"
        with sqlite3.connect(db_path) as db:
            catalog_module.create_schema(db, catalog / "schema.sql")
            priority = {name.casefold(): index + 3 for index, name in enumerate(configured)}
            for plugin in ready:
                catalog_module.index_esm(db, data_dir / plugin, priority[plugin.casefold()])
            for plugin in configured:
                if (data_dir / plugin).is_file():
                    db.execute("UPDATE esm_record SET load_order=? WHERE lower(source_file)=lower(?)",
                               (priority[plugin.casefold()], plugin))
            for archive in archives:
                catalog_module.index_bsa(db, archive)
            placeholders = ",".join("?" for _ in ready)
            conflicts = db.execute(
                f"SELECT record_tag, editor_id, group_concat(source_file), COUNT(DISTINCT hex(body_sha256)) "
                f"FROM esm_record WHERE editor_id_norm IS NOT NULL AND EXISTS ("
                f"SELECT 1 FROM esm_record AS selected WHERE selected.record_tag=esm_record.record_tag "
                f"AND selected.editor_id_norm=esm_record.editor_id_norm AND selected.source_file IN ({placeholders})) "
                f"GROUP BY record_tag, editor_id_norm HAVING COUNT(DISTINCT source_file) > 1 "
                f"AND COUNT(DISTINCT hex(body_sha256)) > 1 ORDER BY record_tag, editor_id_norm",
                ready).fetchall()
        deployment_state = _read_deployment_state(manager_dir)
        deployment_state["deployments"].append({
            "masters": ready,
            "archives": [Path(item["source"]).name for item in operations
                         if Path(item["source"]).suffix.casefold() == ".bsa"],
            "registered_archives": registered_archives,
            "operations": operations,
            "moves": moves,
        })
        _write_deployment_state(manager_dir, deployment_state)
    except Exception:
        if registered_archives:
            try:
                unregister_archives(data_dir.parent / "Morrowind.ini", registered_archives)
            except OSError:
                pass
        try:
            with sqlite3.connect(manager_dir / "game_catalog.sqlite3") as db:
                for plugin in ready:
                    db.execute("DELETE FROM esm_record WHERE lower(source_file)=lower(?)", (plugin,))
                for archive in locals().get("archives", []):
                    db.execute("DELETE FROM bsa_asset WHERE lower(archive_file)=lower(?)", (archive.name,))
                db.commit()
        except sqlite3.Error:
            pass
        for kind, target, source in reversed(rollback):
            try:
                if kind == "link" and target.exists(): target.unlink()
                elif kind == "backup" and source.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, target)
            except OSError:
                pass
        for move in reversed(moves):
            try:
                source, target = Path(move["source"]), Path(move["target"])
                if target.is_file() and not source.exists() and sha256(target).casefold() == move["sha256"].casefold():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, source)
            except OSError:
                pass
        raise
    import mwmanager_journal
    mwmanager_journal.Journal(manager_dir / "journal").record_deployment(
        data_dir.parent, operations, moves=moves, tool="major_master_deploy.py", tag="major-master-deployment",
        inputs={"masters": ready, "include_assets": include_assets})
    return {"ready": ready, "linked": linked, "backed_up": [str(x[0]) for x in backed_up],
            "skipped_identical": skipped, "archives": [p.name for p in archives],
            "record_count": len(ready), "conflicts": conflicts,
            "staging": str(staging), "data_dir": str(data_dir)}
