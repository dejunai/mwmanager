"""Generated files MWManager and the vendored tools write, recorded for cleanup.

Every generated path is either under a known generated root, or registered at the
point it is written into the game folder. The manager's outputs/ folder is the
generated root: reports, caches, dummy stand-in sources, hash records, the
generated-files record, and this module's cleanup manifest. Game-side stand-ins
are listed in outputs/mwmanager_generated_files.txt when write_generated makes
them; generated patches and MWSE runtime data are registered when written.

cleanup_generated runs on rollback and when every deployed mod is removed. It
deletes those files, then removes empty folders under the generated roots and
empty folders the cleanup left under the game folder. User files, mod originals,
backups, the journal, staging, and anything outside the generated roots or the
registered game-side paths are left alone. Paths are never taken from a hand
list of filenames: the roots are swept, and game-side files come from the
write-time records.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# Directory name under the MWManager folder. Swept in full on cleanup.
GENERATED_ROOT_NAME = "outputs"

# Write-time list of game-folder paths (relative to the game dir, posix).
MANIFEST_NAME = "mwmanager_cleanup_manifest.txt"

# Stand-ins write_generated already records (relative to Data Files).
GENERATED_RECORD_NAME = "mwmanager_generated_files.txt"

# Catalog cache lives in the manager folder, not under outputs/. Treated as a
# generated sidecar of the outputs root, not as a filename sweep list.
_CATALOG_STEM = "game_catalog.sqlite3"


def generated_roots(manager_dir) -> list[Path]:
    """The known generated roots under the MWManager folder (currently outputs/)."""
    return [Path(manager_dir).resolve() / GENERATED_ROOT_NAME]


def manifest_path(manager_dir) -> Path:
    return Path(manager_dir).resolve() / GENERATED_ROOT_NAME / MANIFEST_NAME


def generated_record_path(manager_dir) -> Path:
    return Path(manager_dir).resolve() / GENERATED_ROOT_NAME / GENERATED_RECORD_NAME


def register_game_generated(manager_dir, game_dir, path) -> bool:
    """Record a generated file written under the game folder. Returns False if
    `path` is outside the game folder and is not recorded."""
    game_dir = Path(game_dir).resolve()
    path = Path(path)
    if not path.exists() and not path.is_absolute():
        path = game_dir / path
    try:
        path = path.resolve()
        rel = path.relative_to(game_dir)
    except (OSError, ValueError):
        return False
    if not rel.parts or ".." in rel.parts:
        return False
    dest = manifest_path(manager_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    line = rel.as_posix()
    key = line.casefold()
    if key in {p.casefold() for p in _read_lines(dest)}:
        return True
    with open(dest, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return True


def cleanup_generated(manager_dir, game_dir=None, *, dry_run=False) -> dict:
    """Delete recorded game-side generated files and sweep generated roots.

    Empty folders under those roots, and empty folders the game-side deletes
    left, are removed. Data Files and the game folder itself are kept. Returns
    the paths removed or skipped.
    """
    manager_dir = Path(manager_dir).resolve()
    game_dir = Path(game_dir).resolve() if game_dir is not None else None
    removed, skipped, pruned = [], [], []
    protected = _protected_game_rels(manager_dir) if game_dir is not None else set()

    if game_dir is not None:
        for target in _game_targets(manager_dir, game_dir):
            if not _inside(target, game_dir):
                skipped.append(str(target))
                continue
            rel = _rel_key(game_dir, target)
            if rel in protected:
                skipped.append(str(target))
                continue
            if dry_run:
                if target.is_file() or target.is_symlink():
                    removed.append(str(target))
                continue
            if _unlink_file(target):
                removed.append(str(target))
                parent = target.parent
                while _prune_dir(parent, stop=game_dir, also_keep=game_dir / "Data Files"):
                    pruned.append(str(parent))
                    parent = parent.parent

    for root in generated_roots(manager_dir):
        if not _is_allowed_root(root, manager_dir):
            continue
        gone, empty, skip = _sweep_root(root, dry_run=dry_run)
        removed.extend(gone)
        pruned.extend(empty)
        skipped.extend(skip)

    for sidecar in _catalog_sidecars(manager_dir):
        if dry_run:
            removed.append(str(sidecar))
            continue
        if _unlink_file(sidecar):
            removed.append(str(sidecar))

    return {"removed": removed, "pruned": pruned, "skipped": skipped}


def leftovers(manager_dir, game_dir=None) -> list[str]:
    """Generated paths that are still present (empty after a finished cleanup)."""
    report = cleanup_generated(manager_dir, game_dir, dry_run=True)
    return list(report["removed"])


def _is_allowed_root(root: Path, manager_dir: Path) -> bool:
    """Only sweep manager_dir/outputs, never a symlink that leaves the manager folder."""
    manager_dir = Path(manager_dir).resolve()
    try:
        if root.is_symlink():
            return False
        resolved = root.resolve()
    except OSError:
        return False
    if not _inside(resolved, manager_dir) or resolved == manager_dir:
        return False
    return resolved.name.casefold() == GENERATED_ROOT_NAME.casefold()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return path.resolve() != root.resolve()
    except (OSError, ValueError):
        return False


def _rel_key(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix().casefold()
    except (OSError, ValueError):
        return ""


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        out.append(line.split("\t")[0].strip())
    return out


def _game_targets(manager_dir: Path, game_dir: Path) -> list[Path]:
    """Game-folder files recorded at write time. Order is irrelevant; uniqueness is."""
    seen = set()
    found = []
    data_dir = game_dir / "Data Files"
    for rel in _read_lines(generated_record_path(manager_dir)):
        # The generated-files record is relative to Data Files (it may use \\ or /).
        candidate = data_dir.joinpath(*Path(rel.replace("/", os.sep)).parts)
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            found.append(candidate)
    for rel in _read_lines(manifest_path(manager_dir)):
        candidate = game_dir.joinpath(*Path(rel.replace("/", os.sep)).parts)
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            found.append(candidate)
    return found


def _protected_game_rels(manager_dir: Path) -> set[str]:
    """Baseline and prerequisite game files: never deleted, even if listed."""
    protected = set()
    journal_root = Path(manager_dir) / "journal"
    baseline = journal_root / "baseline" / "baseline.json"
    if not baseline.is_file():
        return protected
    try:
        import mwmanager_journal
        journal = mwmanager_journal.Journal(journal_root)
        for rel in (journal.baseline().get("files") or {}):
            protected.add(str(rel).replace("\\", "/").casefold())
        for rel in (journal.prerequisites().get("files") or {}):
            protected.add(str(rel).replace("\\", "/").casefold())
    except (OSError, ValueError, KeyError):
        return protected
    return protected


def _catalog_sidecars(manager_dir: Path) -> list[Path]:
    folder = Path(manager_dir).resolve()
    found = []
    for name in (_CATALOG_STEM, _CATALOG_STEM + "-wal", _CATALOG_STEM + "-shm"):
        path = folder / name
        if path.is_file() and not path.is_symlink() and path.parent.resolve() == folder:
            found.append(path)
    return found


def _unlink_file(path: Path) -> bool:
    """Unlink a file or symlink. Retries briefly on a Windows sharing violation."""
    if not path.is_symlink() and not path.is_file():
        return False
    deadline = time.time() + 2.0
    while True:
        try:
            path.unlink()
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(0.05)


def _prune_dir(folder: Path, *, stop: Path, also_keep=None) -> bool:
    keep = {stop.resolve()}
    if also_keep is not None:
        keep.add(Path(also_keep).resolve())
    try:
        folder = folder.resolve()
        if folder in keep or not _inside(folder, stop):
            return False
        folder.rmdir()
        return True
    except OSError:
        return False


def _sweep_root(root: Path, *, dry_run=False) -> tuple[list[str], list[str], list[str]]:
    removed, pruned, skipped = [], [], []
    if not root.exists():
        return removed, pruned, skipped
    if root.is_symlink():
        skipped.append(str(root))
        return removed, pruned, skipped
    if not root.is_dir():
        if dry_run or _unlink_file(root):
            removed.append(str(root))
        return removed, pruned, skipped
    # Bottom-up: files first, then empty directories, then the root itself.
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        folder = Path(dirpath)
        if folder.is_symlink():
            skipped.append(str(folder))
            continue
        for name in filenames:
            path = folder / name
            if dry_run:
                removed.append(str(path))
                continue
            if _unlink_file(path):
                removed.append(str(path))
            else:
                skipped.append(str(path))
        if folder.resolve() == root.resolve():
            continue
        if dry_run:
            pruned.append(str(folder))
            continue
        try:
            folder.rmdir()
            pruned.append(str(folder))
        except OSError:
            skipped.append(str(folder))
    if not dry_run:
        try:
            root.rmdir()
            pruned.append(str(root))
        except OSError:
            pass
    else:
        pruned.append(str(root))
    return removed, pruned, skipped
