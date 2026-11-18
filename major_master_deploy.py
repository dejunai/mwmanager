"""Deploy already-tidied major masters from staging into the configured game."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from contextlib import closing
import subprocess
import tempfile
import threading
import time
import uuid
import struct
from collections import deque

from workflow_safety import DurableTransaction, mutation_lock, require_space
import managed_processes

BSA_FOLDERS = {"bookart", "icons", "meshes", "textures", "video"}
SKIP_FOLDERS = {"off", "bugreports", "fomod", "extras", ".mwmanager-pack"}
GAME_ROOT_FOLDERS = {"mge3"}          # a package's top-level folders that belong beside Morrowind.exe, not in Data Files


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
    for package in sorted((p for p in staging.iterdir() if p.is_dir() and not p.name.startswith(".")
                           and not p.is_symlink()), key=lambda p: p.name.casefold()):
        for base, dirs, files in os.walk(package):
            dirs[:] = [name for name in dirs if name.casefold() not in SKIP_FOLDERS and not name.startswith(".")]
            for filename in files:
                key = filename.casefold()
                if key in wanted and Path(filename).suffix.casefold() == ".esm":
                    found.setdefault(key, Path(base) / filename)
    return found


def esm_master_names(path):
    """Read a bounded TES3 dependency header without loading the plugin."""
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(16)
            if len(header) != 16 or header[:4] != b"TES3":
                return set()
            size = struct.unpack_from("<I", header, 4)[0]
            if size > 16 * 1024 * 1024:
                raise ValueError(f"Unreasonably large TES3 header: {path}")
            body = stream.read(size)
        if len(body) != size:
            raise ValueError(f"Truncated TES3 header: {path}")
        names, offset = set(), 0
        while offset + 8 <= len(body):
            tag, length = body[offset:offset + 4], struct.unpack_from("<I", body, offset + 4)[0]
            offset += 8
            if offset + length > len(body):
                raise ValueError(f"Truncated TES3 master entry: {path}")
            if tag == b"MAST":
                names.add(body[offset:offset + length].split(b"\0", 1)[0].decode("cp1252").casefold())
            offset += length
        return names
    except OSError:
        raise ValueError(f"Cannot inspect dependencies: {path}") from None


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
        from core_addons import lua_used_images
        keep_loose = {_data_relative(package, package / rel).as_posix().casefold()
                      for rel in lua_used_images(package)}
        for base, dirs, files in os.walk(package):
            rel = _data_relative(package, Path(base))
            dirs[:] = [d for d in dirs if d.casefold() not in SKIP_FOLDERS and not d.startswith(".")]
            if not rel.parts or rel.parts[0].casefold() not in BSA_FOLDERS:
                continue
            count += sum(1 for f in files if not f.startswith(".")
                         and (rel / f).as_posix().casefold() not in keep_loose)
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
    # The configured order is the community load order, and it is also the conflict resolution: link in that
    # order, so a later master's file replaces an earlier one's, as its records do (Patch for Purists, 01, loses).
    return sorted(((package, sorted(names, key=lambda n: priority.get(n.casefold(), 99999)))
                   for package, names in result.items()),
                  key=lambda item: min((priority.get(name.casefold(), 99999) for name in item[1]), default=99999))


def _bsa_files(package: Path) -> list[Path]:
    return sorted((p for p in package.glob("*.bsa") if p.is_file()), key=lambda p: p.name.casefold())


def find_bsapack() -> Path | None:
    """BSAPack from MWManager's own tools folder, else from PATH (no fixed install locations)."""
    bundled = Path(__file__).resolve().parent / "tools" / "bsapack.dej.exe"
    if bundled.is_file():
        return bundled
    located = shutil.which("bsapack.dej") or shutil.which("bsapack.dej.exe")
    return Path(located) if located else None


def _pack_assets(package: Path, plugin_name: str, tool: Path, progress=None) -> list[Path]:
    with mutation_lock(package):
        return _pack_assets_locked(package.resolve(), plugin_name, tool, progress)


def _pack_assets_locked(package, plugin_name, tool, progress):
    """Pack on the package's volume; write-ahead every publication/loose move."""
    from core_addons import lua_used_images
    from mwmanager_workflow import checkpoint
    transaction_root = package / ".mwmanager-pack"
    DurableTransaction.recover(transaction_root, [package])
    DurableTransaction.recover(package / ".mwmanager-unpack", [package])
    for stale in package.glob(".mwmanager-pack-work-*"):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale)
    keep_loose = {_data_relative(package, package / rel).as_posix().casefold()
                  for rel in lua_used_images(package)}
    files = []
    for base, dirs, names in os.walk(package):
        dirs[:] = [d for d in dirs if d.casefold() not in SKIP_FOLDERS and not d.startswith(".")]
        rel = _data_relative(package, Path(base))
        if rel.parts and rel.parts[0].casefold() in BSA_FOLDERS:
            for name in names:
                if name.startswith("."):
                    continue
                source = Path(base) / name
                if source.is_symlink():
                    raise ValueError(f"Refusing to pack a symbolic link: {source}")
                if (rel / name).as_posix().casefold() not in keep_loose:
                    files.append((source, rel / name))
    if not files:
        return []
    # Includes a conservative copy fallback plus archive output, on this volume.
    require_space(package, sum(p.stat().st_size for p, _ in files) * 2 + 1024 * 1024)
    with tempfile.TemporaryDirectory(prefix=".mwmanager-pack-work-", dir=package) as temp_name:
        temp = Path(temp_name)
        for count, (src, relative) in enumerate(files, 1):
            checkpoint(progress)
            dst = temp / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                raise ValueError(f"Colliding normalized BSA path: {relative}")
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
            if progress and count % 2000 == 0:
                progress(f"Preparing BSA for {plugin_name}: {count:,} assets ready…")
        base_name = Path(plugin_name).stem + "_assets.bsa"
        if progress:
            progress(f"Building BSA for {plugin_name} from {count:,} assets; please be patient…")
        proc = managed_processes.popen([str(tool), "pack", base_name], cwd=temp, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", bufsize=1,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = deque(maxlen=80)
        reader_errors = []

        def relay_output():
            try:
                with (temp / "bsapack.log").open("w", encoding="utf-8") as log:
                    for line in proc.stdout:
                        log.write(line)
                        line = line.rstrip()
                        if line:
                            output.append(line[-4000:])
                            if progress:
                                progress(f"BSAPack: {line}")
            except Exception as exc:
                reader_errors.append(exc)

        reader = threading.Thread(target=relay_output, name="MWManager BSAPack output", daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 7200
            while True:
                checkpoint(progress)
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"BSAPack timed out after two hours for {plugin_name}.")
                try:
                    returncode = proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            managed_processes.terminate(proc)
            raise
        finally:
            reader.join(timeout=2)
            if reader.is_alive():
                managed_processes.terminate(proc)
                reader.join(timeout=2)
        if reader.is_alive():
            raise RuntimeError("BSAPack output pipe did not close; packing was not published.")
        if reader_errors:
            raise reader_errors[0]
        log_path = package / "Off" / Path(plugin_name).stem / "bsapack.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(temp / "bsapack.log", log_path)
        if returncode:
            raise RuntimeError(f"BSAPack failed for {plugin_name}: {' | '.join(output)[-2000:]}")
        outputs = sorted(temp.glob(Path(base_name).stem + "*.bsa"), key=lambda p: p.name.casefold())
        if not outputs:
            raise RuntimeError(f"BSAPack reported success but created no archive for {plugin_name}.")
        off = package / "Off" / Path(plugin_name).stem / "bsa packed" / uuid.uuid4().hex
        tx = DurableTransaction(transaction_root, [package]).begin()
        try:
            record_path = package / ".mwmanager-bsa-state.json"
            tx.snapshot(record_path)
            published = []
            for source in outputs:
                checkpoint(progress)
                destination = package / source.name
                tx.move(source, destination)
                published.append(destination)
            for source, _relative in files:
                checkpoint(progress)
                tx.move(source, off / source.relative_to(package))
            from workflow_safety import atomic_json
            atomic_json(record_path, {
                "version": 1,
                "archives": [{"path": str(path.relative_to(package)), "sha256": sha256(path)} for path in published],
                "files": [{"source": str(Path(op["source"]).relative_to(package)),
                           "backup": str(Path(op["target"]).relative_to(package)), "sha256": op["sha256"]}
                          for op in tx.state["operations"] if Path(op["source"]).is_relative_to(package)
                          and Path(op["target"]).is_relative_to(off)]})
            tx.commit()
            return published
        except BaseException:
            tx.rollback()
            raise


def restore_packed_assets(package):
    """Restore retained loose inputs before changing options; remove only our exact outputs."""
    package = Path(package).resolve()
    with mutation_lock(package):
        DurableTransaction.recover(package / ".mwmanager-pack", [package])
        root = package / ".mwmanager-unpack"
        DurableTransaction.recover(root, [package])
        record_path = package / ".mwmanager-bsa-state.json"
        if not record_path.exists():
            return False
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("version") != 1:
            raise ValueError("Unsupported generated BSA record; restage this package before changing its options.")

        def inside(relative):
            path = package / relative
            if not path.resolve().is_relative_to(package):
                raise ValueError(f"Unsafe generated BSA path: {relative}")
            return path

        archives = [(inside(item["path"]), item["sha256"]) for item in record["archives"]]
        files = [(inside(item["source"]), inside(item["backup"]), item["sha256"]) for item in record["files"]]
        for path, expected in archives:
            if not path.is_file() or sha256(path) != expected:
                raise RuntimeError(f"Generated BSA changed; preserving it for review: {path}")
        for source, backup, expected in files:
            if not backup.is_file() or sha256(backup) != expected:
                raise RuntimeError(f"Retained BSA input changed: {backup}")
            if source.exists() and (not source.is_file() or sha256(source) != expected):
                raise RuntimeError(f"Loose BSA input changed; preserving it for review: {source}")
        tx = DurableTransaction(root, [package]).begin()
        try:
            for index, (archive, _hash) in enumerate(archives):
                tx.move(archive, root / f"archive-{index}")
            for source, backup, _hash in files:
                if not source.exists():
                    tx.move(backup, source)
            tx.move(record_path, root / "old-state.json")
            tx.commit()
            return True
        except BaseException:
            tx.rollback()
            raise


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
        if (isinstance(data, dict) and isinstance(data.get("deployments"), list)
                and all(isinstance(entry, dict) and isinstance(entry.get("operations", []), list)
                        for entry in data["deployments"])):
            return data
    except FileNotFoundError:
        return {"version": 1, "deployments": []}
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Deployment state is corrupt; preserve it and recover before continuing.") from exc
    raise RuntimeError("Deployment state is invalid; refusing to discard its ownership records.")


def _write_deployment_state(manager_dir: Path, state: dict) -> None:
    from workflow_safety import atomic_json
    atomic_json(_deployment_state_path(manager_dir), state)


def recovery_pending(manager_dir):
    return any((Path(manager_dir) / "transactions" / name).is_dir()
               for name in ("major-master-deploy", "major-master-remove", "ghost-statics"))


def recover_deployment(manager_dir, game_dir, staging=None):
    manager_dir, game_dir = Path(manager_dir).resolve(), Path(game_dir).resolve()
    with mutation_lock(manager_dir, game_dir):
        recovered = False
        for name in ("major-master-remove", "major-master-deploy", "ghost-statics"):
            recovered |= DurableTransaction.recover(
                manager_dir / "transactions" / name,
                [manager_dir, game_dir, Path(staging) if staging else manager_dir / "staging" / "major_masters"])
        return recovered


def _snapshot_metadata(tx, manager_dir, data_dir):
    for path in (data_dir.parent / "Morrowind.ini", _deployment_state_path(manager_dir),
                 manager_dir / "journal" / "install.jsonl", manager_dir / "journal" / "runs.jsonl"):
        tx.snapshot(path)
    tx.snapshot(manager_dir / "game_catalog.sqlite3", database=True)


def assert_package_disposable(package, staging, data_dir, manager_dir, configured, *, check_dependencies=True):
    """Backend guard shared by Unstage, re-extraction, and staging reset."""
    original = Path(package)
    if original.is_symlink() or getattr(original, "is_junction", lambda: False)():
        raise ValueError(f"Refusing to delete a linked package: {original}")
    package, staging = original.resolve(), Path(staging).resolve()
    if package == staging or package.parent != staging:
        raise ValueError(f"Unsafe package deletion: {package}")
    if recovery_pending(manager_dir):
        raise RuntimeError("Recover the interrupted deployment before changing staging.")
    for deployment in _read_deployment_state(Path(manager_dir))["deployments"]:
        for op in deployment.get("operations", []):
            if any(op.get(key) and Path(op[key]).resolve().is_relative_to(package)
                   for key in ("source", "backup")):
                raise ValueError(f"{package.name} is referenced by a deployment; remove that deployment first.")
    if package_has_live_deployment(package, data_dir):
        raise ValueError(f"{package.name} still owns live deployed files; remove its deployment first.")
    for name, source in staged_masters(staging, configured).items():
        if source.resolve().is_relative_to(package) and any(
                path.name.casefold() == name for path in Path(data_dir).glob("*") if path.is_file()):
            raise ValueError(f"{package.name} contains an installed master; remove its deployment first.")
    if not check_dependencies:
        return
    # A fresh dependency scan, never GUI cached data, before any deletion.
    staged = staged_masters(staging, configured)
    masters = {name.casefold() for name, path in staged.items() if path.resolve().is_relative_to(package)}
    for root in (staging, Path(data_dir)):
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.casefold() not in SKIP_FOLDERS and not d.startswith(".")]
            if Path(base).resolve().is_relative_to(package):
                dirs[:] = []
                continue
            for name in files:
                if Path(name).suffix.casefold() in {".esm", ".esp"}:
                    if esm_master_names(Path(base) / name) & masters:
                        raise ValueError(f"{package.name} is required by {name}; it cannot be discarded.")


def unstage_packages(packages, staging, data_dir, manager_dir, configured, progress=None):
    with mutation_lock(manager_dir, Path(data_dir).parent):
        for package in packages:
            assert_package_disposable(package, staging, data_dir, manager_dir, configured)
        from core_addons import _rmtree_clear_readonly
        for package in packages:
            if progress:
                progress(f"Removing staged package: {Path(package).name}")
            shutil.rmtree(package, onerror=_rmtree_clear_readonly)


def tracked_deployment_masters(manager_dir: Path) -> list[str]:
    state = _read_deployment_state(manager_dir)
    return list(dict.fromkeys(name for deployment in state["deployments"]
                              for name in deployment.get("masters", [])))


def deployment_statuses(names, data_dir: Path, manager_dir: Path, *, staged=()) -> dict[str, dict]:
    """Classify configured masters with one deployment-state read."""
    data_dir, manager_dir = Path(data_dir).resolve(), Path(manager_dir).resolve()
    names = list(names)
    staged_keys = ({str(name).casefold() for name in staged}
                   if not isinstance(staged, bool) else set())
    deployments = _read_deployment_state(manager_dir)["deployments"]
    tracked_names = {str(master).casefold()
                     for entry in deployments if isinstance(entry, dict)
                     for master in entry.get("masters", [])}
    active_targets = {
        os.path.normcase(os.path.abspath(operation["target"]))
        for entry in deployments if isinstance(entry, dict)
        for operation in entry.get("operations", [])
        if operation.get("target")
    }
    recovering = (manager_dir / "transactions" / "major-master-deploy").is_dir()
    statuses = {}
    for name in names:
        key = str(name).casefold()
        path = data_dir / name
        live = path.is_file()
        tracked = key in tracked_names
        if recovering:
            status = {"state": "recovery", "label": "Recovery required",
                      "live": live, "tracked": tracked}
        elif live and tracked and os.path.normcase(os.path.abspath(path)) in active_targets:
            status = {"state": "deployed", "label": "Deployed", "live": True, "tracked": True}
        elif tracked:
            status = {"state": "missing", "label": "Deployment incomplete",
                      "live": live, "tracked": True}
        elif live:
            status = {"state": "untracked", "label": "Untracked on disk",
                      "live": True, "tracked": False}
        elif staged is True or key in staged_keys:
            status = {"state": "ready", "label": "Ready for install",
                      "live": False, "tracked": False}
        else:
            status = {"state": "absent", "label": "Not staged", "live": False, "tracked": False}
        statuses[key] = status
    return statuses


def deployment_status(name: str, data_dir: Path, manager_dir: Path,
                      *, staged: bool = False) -> dict:
    """Classify one configured master from live files plus durable ownership state."""
    return deployment_statuses([name], data_dir, manager_dir, staged=staged)[str(name).casefold()]


def package_has_live_deployment(package: Path, data_dir: Path) -> bool:
    """An ESM or payload file in this package still owns a live game target."""
    package, data_dir = Path(package).resolve(), Path(data_dir).resolve()
    for source in package.rglob("*"):
        try:
            relative_parts = source.relative_to(package).parts
        except ValueError:
            continue
        if any(part.casefold() in SKIP_FOLDERS or part.startswith(".")
               for part in relative_parts[:-1]):
            continue
        if not source.is_file() or source.name.startswith("."):
            continue
        relative = _data_relative(package, source)
        if not relative.parts:
            continue
        target = (data_dir.parent / relative
                  if relative.parts[0].casefold() in GAME_ROOT_FOLDERS
                  else data_dir / source.name
                  if source.suffix.casefold() in {".esm", ".esp", ".bsa"}
                  else data_dir / relative)
        if not target.is_file():
            continue
        try:
            if os.path.samefile(source, target):
                return True
        except OSError:
            if target.stat().st_size == source.stat().st_size and sha256(target) == sha256(source):
                return True
    return False


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
        dirs[:] = [name for name in dirs if name.casefold() not in SKIP_FOLDERS and not name.startswith(".")]
        if any(name.casefold() == "tr_firemoth_remover.esp" for name in files):
            return True
    return False


def remove_deployed(staging: Path, data_dir: Path, manager_dir: Path, progress=None) -> dict:
    staging, data_dir, manager_dir = (Path(p).resolve() for p in (staging, data_dir, manager_dir))
    with mutation_lock(manager_dir, Path(data_dir).parent):
        recover_deployment(manager_dir, Path(data_dir).parent, staging)
        tx = DurableTransaction(manager_dir / "transactions" / "major-master-remove",
                                [manager_dir, data_dir.parent, staging]).begin()
        try:
            _snapshot_metadata(tx, manager_dir, data_dir)
            result = _remove_deployed(staging, data_dir, manager_dir, progress, tx=tx)
            changed = {Path(op[key]) for op in tx.state["operations"] for key in ("source", "target")
                       if Path(op[key]).is_relative_to(data_dir.parent)}
            if changed:
                from mwmanager_journal import Journal, rel_of
                ledger = Journal(manager_dir / "journal")
                run = ledger.start_run("major_master_deploy.py", {"operation": "remove deployment"})
                for path in sorted(changed):
                    ledger.record(run["run"], rel_of(data_dir.parent, path),
                                  "replace" if path.is_file() else "remove",
                                  sha256(path) if path.is_file() else None,
                                  {"kind": "absent"}, tag="major-master-removal")
                ledger.finish_run(run)
            tx.commit()
            return result
        except BaseException:
            tx.rollback()
            raise


def _remove_deployed(staging: Path, data_dir: Path, manager_dir: Path, progress=None, *, tx) -> dict:
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
            if not target.is_absolute() or not (target.is_relative_to(data_dir) or any(
                    target.is_relative_to(data_dir.parent / name) for name in GAME_ROOT_FOLDERS)):
                raise ValueError(f"Deployment state contains an unsafe target path: {target}")
            if source.is_absolute() and not source.is_relative_to(staging):
                raise ValueError(f"Deployment state contains an unsafe source path: {source}")
            if (backup and backup.is_file() and operation.get("original_sha256")
                    and sha256(backup) != operation["original_sha256"]):
                pending_operations.append(operation)
                continue
            if (target.is_file() and operation.get("original_sha256") and
                    sha256(target) == operation["original_sha256"] and not (backup and backup.exists())):
                continue  # An older interrupted removal already restored this original.
            source_matches = False
            if target.is_file() and source.is_file():
                try:
                    source_matches = os.path.samefile(source, target)
                except OSError:
                    pass
            if source_matches and operation.get("sha256"):
                source_matches = sha256(target) == operation["sha256"]
            # Legacy records have no installed hash: preserve uncertain edits.
            elif source_matches and operation.get("verification") != "hardlink":
                source_matches = False
            if source_matches:
                tx.move(target, tx.root / ("removed-" + uuid.uuid4().hex))
                removed += 1
            elif target.exists():
                # Preserve modified/uncertain targets and their backups for a later safe retry.
                pending_operations.append(operation)
                continue
            if backup and backup.is_file() and (backup.is_relative_to(staging) or
                    backup.is_relative_to(manager_dir / "deployment_backups")) and not target.exists():
                if operation.get("original_sha256") and sha256(backup) != operation["original_sha256"]:
                    pending_operations.append(operation)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                tx.move(backup, target)
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
            tx.move(target, source)
            restored += 1
        if pending_operations or pending_moves:
            pending_deployments.append({**deployment, "operations": list(reversed(pending_operations)),
                                        "moves": list(reversed(pending_moves))})
            continue
        registrations += unregister_archives(ini_path, deployment.get("registered_archives", []))
        catalog = manager_dir / "game_catalog.sqlite3"
        if catalog.is_file():
            with closing(sqlite3.connect(catalog)) as db, db:
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
           include_assets: bool = True, progress=None, selected: list[str] | None = None) -> dict:
    with mutation_lock(manager_dir, Path(data_dir).parent):
        recover_deployment(manager_dir, Path(data_dir).parent, staging)
        return _deploy(staging, data_dir, manager_dir, configured, build_bsa=build_bsa,
                       include_assets=include_assets, progress=progress, selected=selected)


def _deploy(staging: Path, data_dir: Path, manager_dir: Path, configured: list[str], *, build_bsa=False,
            include_assets=True, progress=None, selected=None) -> dict:
    """Hardlink staged package files, register archives, and index ESM/BSA data."""
    staging, data_dir, manager_dir = staging.resolve(), data_dir.resolve(), manager_dir.resolve()
    if os.path.splitdrive(str(staging))[0].casefold() != os.path.splitdrive(str(data_dir))[0].casefold():
        raise ValueError("Major-master staging and Data Files must be on the same drive for hardlinks.")
    staged = staged_masters(staging, configured)
    selected_keys = {name.casefold() for name in selected} if selected is not None else None
    ready = [name for name in configured if name.casefold() in staged and not (data_dir / name).is_file()
             and (selected_keys is None or name.casefold() in selected_keys)]
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
                _pack_assets(package, plugin, tool, progress)
    data_dir.mkdir(parents=True, exist_ok=True)
    linked, backed_up, skipped = [], [], []
    operations = []
    moves = []
    registered_archives = []
    tx = DurableTransaction(manager_dir / "transactions" / "major-master-deploy",
                            [manager_dir, data_dir.parent, staging]).begin()
    backup_root = manager_dir / "deployment_backups" / uuid.uuid4().hex
    try:
        _snapshot_metadata(tx, manager_dir, data_dir)
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
            if progress:
                progress("Preserving shipped Siege at Firemoth.esp in Off; hashing and moving it…")
            moved = tx.move(source, target)
            digest = moved["sha256"]
            moves.append({"source": str(source), "target": str(target), "sha256": digest})
            if progress: progress("Moved shipped Siege at Firemoth.esp to Off/Official Add-Ons because Tamriel Rebuilt was deployed without Remove Firemoth.")
        for package, plugins in package_info:
            package_label = Path(package).name
            if progress:
                progress(f"MWMANAGER-STAGE: Hardlinking {package_label}: scanning deployable files…")
            file_count = 0
            last_heartbeat = time.monotonic()
            batch = []
            batch_rel = []
            batch_targets = set()

            def flush_batch():
                nonlocal last_heartbeat
                if not batch:
                    return
                new_operations = tx.link_many(batch)
                operations.extend(new_operations)
                linked.extend(batch_rel)
                batch.clear()
                batch_rel.clear()
                batch_targets.clear()
                if progress:
                    progress(f"MWMANAGER-STAGE: Hardlinking {package_label}: "
                             f"{file_count:,} files scanned; {len(operations):,} linked")
                last_heartbeat = time.monotonic()

            for base, dirs, files in os.walk(package):
                dirs[:] = [d for d in dirs if d.casefold() not in SKIP_FOLDERS and not d.startswith(".")]
                for filename in files:
                    file_count += 1
                    source = Path(base) / filename
                    if source.is_symlink():
                        raise ValueError(f"Refusing to deploy a symbolic link: {source}")
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
                    target = (data_dir.parent / rel if rel.parts[0].casefold() in GAME_ROOT_FOLDERS
                              else data_dir / filename if source.suffix.casefold() in {".esm", ".esp", ".bsa"}
                              else data_dir / rel)
                    target_key = os.path.normcase(str(target))
                    if target_key in batch_targets:
                        flush_batch()
                    backup = backup_root / package.name / str(len(operations)) / rel
                    source_digest = None
                    target_digest = None
                    if target.exists():
                        if target.is_file() and target.stat().st_size == source.stat().st_size:
                            target_digest = sha256(target)
                            source_digest = sha256(source)
                            if target_digest == source_digest:
                                skipped.append(str(rel))
                                continue
                        if not target.is_file():
                            raise IsADirectoryError(f"A directory blocks deployment of {target}")
                        backed_up.append((target, backup))
                    batch.append((source, target, backup, source_digest, target_digest))
                    batch_rel.append(str(rel))
                    batch_targets.add(target_key)
                    if len(batch) >= 256 or time.monotonic() - last_heartbeat >= 5:
                        flush_batch()
            flush_batch()
            if progress:
                progress(
                    f"Hardlinking {package_label}: scan complete, {file_count:,} file(s) examined.")
        ini_path = data_dir.parent / "Morrowind.ini"
        archives = []
        seen_archives = set()
        for package, _plugins in package_info:
            for archive in _bsa_files(package):
                if archive.name.casefold() not in seen_archives:
                    archives.append(archive)
                    seen_archives.add(archive.name.casefold())
        registered_archives = register_archives(ini_path, archives, manager_dir)
        if progress:
            progress(f"Registered {len(registered_archives):,} BSA archive(s); building catalog…")
        # Index after deployment. The catalog keeps full record bodies and BSA member provenance.
        catalog = manager_dir / "database"
        import sys
        sys.path.insert(0, str(catalog))
        import catalog as catalog_module
        db_path = manager_dir / "game_catalog.sqlite3"
        with closing(sqlite3.connect(db_path)) as db, db:
            catalog_module.create_schema(db, catalog / "schema.sql")
            priority = {name.casefold(): index + 3 for index, name in enumerate(configured)}
            for plugin in ready:
                if progress:
                    progress(f"Cataloguing major master {plugin}…")
                catalog_module.index_esm(db, data_dir / plugin, priority[plugin.casefold()])
            for plugin in configured:
                if (data_dir / plugin).is_file():
                    db.execute("UPDATE esm_record SET load_order=? WHERE lower(source_file)=lower(?)",
                               (priority[plugin.casefold()], plugin))
            for archive in archives:
                if progress:
                    progress(f"Cataloguing archive {archive.name}…")
                catalog_module.index_bsa(db, archive)
            if progress:
                progress("Cataloguing complete; checking record conflicts…")
            placeholders = ",".join("?" for _ in ready)
            conflicts = db.execute(
                f"SELECT record_tag, editor_id, group_concat(source_file), COUNT(DISTINCT hex(body_sha256)) "
                f"FROM esm_record WHERE editor_id_norm IS NOT NULL AND EXISTS ("
                f"SELECT 1 FROM esm_record AS selected WHERE selected.record_tag=esm_record.record_tag "
                f"AND selected.editor_id_norm=esm_record.editor_id_norm AND selected.source_file IN ({placeholders})) "
                f"GROUP BY record_tag, editor_id_norm HAVING COUNT(DISTINCT source_file) > 1 "
                f"AND COUNT(DISTINCT hex(body_sha256)) > 1 ORDER BY record_tag, editor_id_norm",
                ready).fetchall()
        deployment = {
            "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "masters": ready,
            "archives": [Path(item["source"]).name for item in operations
                         if Path(item["source"]).suffix.casefold() == ".bsa"],
            "registered_archives": registered_archives,
            "operations": operations,
            "moves": moves,
        }
        if progress:
            progress("MWMANAGER-STAGE: Verifying deployed files for rollback journal…")
        import mwmanager_journal
        mwmanager_journal.Journal(manager_dir / "journal").record_deployment(
            data_dir.parent, operations, moves=moves, tool="major_master_deploy.py", tag="major-master-deployment",
            inputs={"masters": ready, "include_assets": include_assets}, progress=progress)
        deployment_state = _read_deployment_state(manager_dir)
        deployment_state["deployments"].append(deployment)
        _write_deployment_state(manager_dir, deployment_state)
        if progress:
            progress("Deployment ownership recorded; finishing transaction…")
        tx.commit()
    except Exception:
        tx.rollback()
        raise
    return {"ready": ready, "linked": linked, "backed_up": [str(x[0]) for x in backed_up],
            "skipped_identical": skipped, "archives": [p.name for p in archives],
            "record_count": len(ready), "conflicts": conflicts,
            "staging": str(staging), "data_dir": str(data_dir)}
