"""Durable write-ahead operations and cross-process mutation locks.

An active transaction is rolled back before another mutation starts. Committed
transactions only need cleanup. Recovery refuses to overwrite changed files.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import uuid
from contextlib import closing


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _replace(temporary, path, attempts=40, delay=0.05):
    """os.replace, retried briefly on Windows while another reader holds `path` open without delete sharing
    (WinError 5/32: a thread reading the batch manifest made the 3.9 suite fail
    test_blocked_rescan_does_not_stop_the_tidy_of_other_packages now and then). Gives up after ~2 s."""
    import time
    for attempt in range(attempts):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if os.name != "nt" or attempt == attempts - 1:
                raise
            time.sleep(delay)


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temporary, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path, data):
    atomic_bytes(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"))


_locks = {}
_tokens = {}
_guard = threading.RLock()


def child_environment(environment=None):
    """Delegate held locks only to subprocesses explicitly launched by this job."""
    env = dict(os.environ if environment is None else environment)
    try:
        tokens = json.loads(env.get("MWMANAGER_DELEGATED_LOCKS", "{}"))
    except ValueError:
        tokens = {}
    with _guard:
        tokens.update({key: _tokens[key] for key, held in _locks.items()
                       if held[0] == threading.get_ident()})
    env["MWMANAGER_DELEGATED_LOCKS"] = json.dumps(tokens)
    return env


def inherited_lock_fds():
    """Keep POSIX locks alive if a GUI dies while its tool is still running."""
    if os.name == "nt":
        return ()
    descriptors = set()
    for value in os.environ.get("MWMANAGER_LOCK_FDS", "").split(","):
        try:
            descriptor = int(value)
            os.fstat(descriptor)
            descriptors.add(descriptor)
        except (ValueError, OSError):
            continue
    with _guard:
        descriptors.update(held[2].fileno() for held in _locks.values()
                           if held[0] == threading.get_ident())
    return tuple(sorted(descriptors))


@contextmanager
def mutation_lock(manager_dir, game_dir=None):
    """Non-blocking OS locks; nesting is allowed only on the owning thread."""
    keys = {os.path.normcase(str(Path(manager_dir).resolve()))}
    if game_dir is not None:
        keys.add(os.path.normcase(str(Path(game_dir).resolve())))
    acquired = []
    try:
        for key in sorted(keys):
            with _guard:
                held = _locks.get(key)
                owner = threading.get_ident()
                if held:
                    if held[0] != owner:
                        raise RuntimeError("Another MWManager job is modifying this game or staging tree.")
                    _locks[key] = (owner, held[1] + 1, held[2])
                    acquired.append(key)
                    continue
                folder = Path(tempfile.gettempdir()) / "mwmanager-locks"
                folder.mkdir(exist_ok=True)
                stream = (folder / (hashlib.sha256(key.encode()).hexdigest() + ".lock")).open("a+b")
                delegated = False
                try:
                    if os.name == "nt":
                        import msvcrt
                        stream.seek(0, os.SEEK_END)
                        if stream.tell() == 0:
                            stream.write(b"\0")
                            stream.flush()
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    try:
                        stream.seek(1)  # Windows denies reads of the locked byte itself.
                        lock_owner = json.loads(stream.read())
                        inherited = json.loads(os.environ.get("MWMANAGER_DELEGATED_LOCKS", "{}"))
                        delegated = bool(inherited.get(key) and inherited[key] == lock_owner["token"])
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                    if not delegated:
                        stream.close()
                        raise RuntimeError("Another MWManager instance is modifying this game or staging tree.") from exc
                if not delegated:
                    token = uuid.uuid4().hex
                    stream.seek(1)  # Byte zero is reserved for the OS lock.
                    stream.truncate()
                    stream.write(json.dumps({"pid": os.getpid(), "token": token}).encode())
                    stream.flush()
                else:
                    token = inherited[key]
                _tokens[key] = token
                _locks[key] = (owner, 1, stream)
                acquired.append(key)
        yield
    finally:
        for key in reversed(acquired):
            with _guard:
                owner, count, stream = _locks[key]
                if count > 1:
                    _locks[key] = (owner, count - 1, stream)
                else:
                    stream.close()
                    del _locks[key]
                    del _tokens[key]


def require_space(folder, needed):
    if shutil.disk_usage(folder).free < needed:
        raise OSError(f"Not enough free space in {folder}: need at least {needed:,} bytes.")


class DurableTransaction:
    def __init__(self, root, allowed_roots):
        self.root = Path(root).resolve()
        self.allowed = [Path(p).resolve() for p in allowed_roots]
        if not any(self.root.is_relative_to(p) for p in self.allowed):
            raise ValueError("Transaction storage is outside the managed roots.")
        self.path = self.root / "state.json"
        self.state = {"version": 1, "phase": "active", "operations": [], "snapshots": []}

    def safe(self, path):
        path = Path(path)
        if not path.is_absolute() or not any(self._resolved(path).is_relative_to(p) for p in self.allowed):
            raise ValueError(f"Unsafe transaction path: {path}")
        return path

    def _resolved(self, path):
        """path.resolve(), with each folder resolved once: resolving every one of 600,000 files opened each."""
        if path.name in ("", ".", "..") or path.is_symlink():
            return path.resolve()
        cache = self.__dict__.setdefault("_resolved_dirs", {})
        parent = cache.get(path.parent)
        if parent is None:
            parent = cache[path.parent] = path.parent.resolve()
        return parent / path.name

    def save(self):
        atomic_json(self.path, self.state)

    def begin(self):
        if self.root.exists():
            raise RuntimeError(f"Recover the previous transaction first: {self.root}")
        self.root.mkdir(parents=True)
        self.save()
        atomic_bytes(self.root / "operations.jsonl", b"")
        return self

    def record(self, operation):
        self.record_many([operation])

    def record_many(self, operations):
        """Durably record a batch before applying any of its operations."""
        if not operations:
            return
        # Append-only intents avoid rewriting an ever-growing list per asset.
        with (self.root / "operations.jsonl").open("ab") as stream:
            for operation in operations:
                stream.write((json.dumps(operation) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        self.state["operations"].extend(operations)

    def snapshot(self, path, *, database=False):
        path = self.safe(path)
        entry = {"path": str(path), "exists": path.is_file()}
        if entry["exists"]:
            require_space(self.root, path.stat().st_size + 1024 * 1024)
            saved = self.root / f"snapshot-{len(self.state['snapshots'])}"
            if database:
                with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(saved)) as target:
                    source.backup(target)
                with saved.open("r+b") as stream:
                    os.fsync(stream.fileno())
            else:
                with path.open("rb") as source, saved.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                shutil.copystat(path, saved)
            entry.update(saved=str(saved), sha256=digest(saved))
        self.state["snapshots"].append(entry)
        self.save()

    def link(self, source, target, backup, *, source_sha256=None, target_sha256=None):
        return self.link_many([(source, target, backup, source_sha256, target_sha256)])[0]

    def link_many(self, links):
        """Record unique-target hardlink intents with one flush, then apply them in order."""
        operations = []
        targets = set()
        for source, target, backup, source_sha256, target_sha256 in links:
            source, target, backup = (self.safe(p) for p in (source, target, backup))
            target_key = os.path.normcase(str(target))
            if target_key in targets:
                raise ValueError(f"Duplicate target in hardlink batch: {target}")
            targets.add(target_key)
            if target.exists() and not target.is_file():
                raise IsADirectoryError(target)
            if backup.exists():
                raise FileExistsError(f"Refusing to overwrite a recovery backup: {backup}")
            target_exists = target.exists()
            operation = {"kind": "link", "source": str(source), "target": str(target),
                         "backup": str(backup) if target_exists else None,
                         "sha256": str(source_sha256) if source_sha256 else None,
                         "original_sha256": (str(target_sha256 or digest(target))
                                              if target_exists else None)}
            # a hardlink shares size and mtime with its source: removal compares these instead of hashing
            stat = source.stat()
            operation["size"], operation["mtime_ns"] = stat.st_size, stat.st_mtime_ns
            if not target_exists:
                operation["verification"] = "hardlink"
            elif not operation["sha256"]:
                operation["sha256"] = digest(source)
            operations.append(operation)
        self.record_many(operations)  # Durable before any backup or link changes the game.
        made = self.__dict__.setdefault("_made_dirs", set())
        for operation in operations:
            source, target = Path(operation["source"]), Path(operation["target"])
            backup = Path(operation["backup"]) if operation.get("backup") else None
            if target.parent not in made:
                target.parent.mkdir(parents=True, exist_ok=True)
                made.add(target.parent)
            if backup:
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)
            os.link(source, target)
        return operations

    def copy(self, source, target, backup):
        """Copy source to target while journaling the replacement before mutation."""
        source, target, backup = (self.safe(p) for p in (source, target, backup))
        if target.exists() and not target.is_file():
            raise IsADirectoryError(target)
        if backup.exists():
            raise FileExistsError(f"Refusing to overwrite a recovery backup: {backup}")
        operation = {"kind": "copy", "source": str(source), "target": str(target),
                     "backup": str(backup) if target.exists() else None,
                     "sha256": digest(source),
                     "original_sha256": digest(target) if target.exists() else None}
        self.record(operation)
        target.parent.mkdir(parents=True, exist_ok=True)
        if operation["backup"]:
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, backup)
        shutil.copy2(source, target)
        return operation

    def move(self, source, target, *, source_sha256=None):
        return self.move_many([(source, target)], source_sha256=source_sha256)[0]

    def move_many(self, moves, *, source_sha256=None):
        """Record a batch of moves with one flush, then apply them in order."""
        operations = []
        for source, target, *known in moves:             # (source, target) or (source, target, sha256)
            source, target = self.safe(source), self.safe(target)
            if target.exists():
                raise FileExistsError(target)
            operations.append({"kind": "move", "source": str(source), "target": str(target),
                               "sha256": str((known and known[0]) or source_sha256 or digest(source))})
        self.record_many(operations)
        for operation in operations:
            target = Path(operation["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(operation["source"], target)
        return operations

    def commit(self):
        self.state["phase"] = "committed"
        self.save()
        shutil.rmtree(self.root, ignore_errors=True)

    def rollback(self):
        for op in reversed(self.state["operations"]):
            source, target = self.safe(op["source"]), self.safe(op["target"])
            if op["kind"] == "move":
                if target.exists():
                    if source.exists() or not target.is_file() or digest(target) != op["sha256"]:
                        raise RuntimeError(f"Recovery needs review; moved file changed: {target}")
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, source)
                elif not source.is_file() or digest(source) != op["sha256"]:
                    raise RuntimeError(f"Recovery needs review; moved file is missing: {source}")
                continue
            backup = self.safe(op["backup"]) if op.get("backup") else None
            if backup and backup.exists() and digest(backup) != op["original_sha256"]:
                raise RuntimeError(f"Recovery backup changed: {backup}")
            if target.exists():
                if op.get("verification") == "hardlink":
                    try:
                        linked = source.is_file() and target.is_file() and os.path.samefile(source, target)
                    except OSError:
                        linked = False
                    if not linked:
                        raise RuntimeError(f"Recovery needs review; deployed hardlink changed: {target}")
                    actual = None
                else:
                    actual = digest(target) if target.is_file() else None
                if (op.get("original_sha256") is not None and actual == op["original_sha256"]
                        and not (backup and backup.exists())):
                    continue  # Intent not started, or already rolled back.
                if op.get("verification") != "hardlink" and actual != op["sha256"]:
                    raise RuntimeError(f"Recovery needs review; deployed file changed: {target}")
                if backup and not backup.is_file():
                    raise RuntimeError(f"Recovery backup is missing: {backup}")
                target.unlink()
            if backup and backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
            elif op.get("original_sha256") and not target.exists():
                raise RuntimeError(f"Recovery original is missing: {target}")
        for snapshot in reversed(self.state["snapshots"]):
            path = self.safe(snapshot["path"])
            if snapshot["exists"]:
                saved = self.safe(snapshot["saved"])
                if digest(saved) != snapshot["sha256"]:
                    raise RuntimeError(f"Recovery snapshot changed: {saved}")
                # Copy-to-replace, never truncate a potentially hardlinked file.
                temp = path.with_name(path.name + ".recovery.tmp")
                shutil.copy2(saved, temp)
                os.replace(temp, path)
            else:
                path.unlink(missing_ok=True)
        shutil.rmtree(self.root)

    @classmethod
    def recover(cls, root, allowed_roots):
        transaction = cls(root, allowed_roots)
        if not transaction.root.exists():
            return False
        if not transaction.path.exists():
            # begin() was interrupted before its first intent; no mutation ran.
            if any(transaction.root.iterdir()):
                raise RuntimeError(f"Invalid recovery record: {transaction.root}")
            transaction.root.rmdir()
            return True
        transaction.state = json.loads(transaction.path.read_text(encoding="utf-8"))
        if transaction.state.get("version") != 1:
            raise RuntimeError("Unsupported recovery record; refusing to modify files.")
        intents = transaction.root / "operations.jsonl"
        if intents.exists():
            lines = intents.read_bytes().splitlines(keepends=True)
            transaction.state["operations"] = [json.loads(line) for line in lines if line.endswith(b"\n")]
        if transaction.state["phase"] == "committed":
            shutil.rmtree(transaction.root)
        elif transaction.state["phase"] == "active":
            transaction.rollback()
        else:
            raise RuntimeError("Invalid transaction phase; recovery needs review.")
        return True


def is_data_files_tree(path) -> bool:
    """True when `path` is a Data Files folder (the live game tree, not a staging root)."""
    from pathlib import Path as _P
    try:
        resolved = _P(path).resolve()
    except OSError:
        resolved = _P(os.path.abspath(path))
    return resolved.name.casefold() == "data files"


def require_staging_write_root(path, *, data_dir=None, game_dir=None, what="Tidy"):
    """Refuse a write/working root that is (or is inside) the live Data Files or game folder.

    Tidy, luafix, the omw2mwse converter, and other pre-deploy repairs write under
    MWManager or Vortex staging. Data Files is only written by the final deploy step
    and by the existing deploy-time stand-ins and patches.
    """
    from pathlib import Path as _P

    def _resolve(value):
        try:
            return _P(value).resolve()
        except OSError:
            return _P(os.path.abspath(value))

    root = _resolve(path)
    if is_data_files_tree(root):
        raise ValueError("%s must write under staging, not Data Files: %s" % (what, root))
    forbidden = []
    if data_dir:
        forbidden.append(_resolve(data_dir))
    if game_dir:
        game = _resolve(game_dir)
        if root == game:
            raise ValueError("%s must write under staging, not the game folder: %s" % (what, root))
        forbidden.append(game / "Data Files")
    for base in forbidden:
        if root == base:
            raise ValueError("%s must write under staging, not %s: %s" % (what, base, root))
        try:
            root.relative_to(base)
        except ValueError:
            continue
        raise ValueError("%s must write under staging, not under %s: %s" % (what, base, root))
    return root
