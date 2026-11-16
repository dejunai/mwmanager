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


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
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
                        stream.seek(0)
                        lock_owner = json.loads(stream.read().lstrip(b"\0"))
                        inherited = json.loads(os.environ.get("MWMANAGER_DELEGATED_LOCKS", "{}"))
                        delegated = bool(inherited.get(key) and inherited[key] == lock_owner["token"])
                    except (ValueError, KeyError, TypeError):
                        pass
                    if not delegated:
                        stream.close()
                        raise RuntimeError("Another MWManager instance is modifying this game or staging tree.") from exc
                if not delegated:
                    token = uuid.uuid4().hex
                    stream.seek(0)
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
        if not path.is_absolute() or not any(path.resolve().is_relative_to(p) for p in self.allowed):
            raise ValueError(f"Unsafe transaction path: {path}")
        return path

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
        # Append-only intents avoid rewriting an ever-growing list per asset.
        with (self.root / "operations.jsonl").open("ab") as stream:
            stream.write((json.dumps(operation) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        self.state["operations"].append(operation)

    def snapshot(self, path, *, database=False):
        path = self.safe(path)
        entry = {"path": str(path), "exists": path.is_file()}
        if entry["exists"]:
            require_space(self.root, path.stat().st_size + 1024 * 1024)
            saved = self.root / f"snapshot-{len(self.state['snapshots'])}"
            if database:
                with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(saved)) as target:
                    source.backup(target)
            else:
                shutil.copy2(path, saved)
            with saved.open("rb") as stream:
                os.fsync(stream.fileno())
            entry.update(saved=str(saved), sha256=digest(saved))
        self.state["snapshots"].append(entry)
        self.save()

    def link(self, source, target, backup):
        source, target, backup = (self.safe(p) for p in (source, target, backup))
        if target.exists() and not target.is_file():
            raise IsADirectoryError(target)
        if backup.exists():
            raise FileExistsError(f"Refusing to overwrite a recovery backup: {backup}")
        operation = {"kind": "link", "source": str(source), "target": str(target),
                     "backup": str(backup) if target.exists() else None,
                     "sha256": digest(source),
                     "original_sha256": digest(target) if target.exists() else None}
        self.record(operation)  # Durable before either backup or link changes the game.
        target.parent.mkdir(parents=True, exist_ok=True)
        if operation["backup"]:
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, backup)
        os.link(source, target)
        return operation

    def move(self, source, target):
        source, target = self.safe(source), self.safe(target)
        if target.exists():
            raise FileExistsError(target)
        operation = {"kind": "move", "source": str(source), "target": str(target),
                     "sha256": digest(source)}
        self.record(operation)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        return operation

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
                actual = digest(target) if target.is_file() else None
                if (op.get("original_sha256") is not None and actual == op["original_sha256"]
                        and not (backup and backup.exists())):
                    continue  # Intent not started, or already rolled back.
                if actual != op["sha256"]:
                    raise RuntimeError(f"Recovery needs review; deployed file changed: {target}")
                if backup and not backup.is_file():
                    raise RuntimeError(f"Recovery backup is missing: {backup}")
                target.unlink()
            if backup and backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
            elif op.get("original_sha256") and not target.exists():
                raise RuntimeError(f"Recovery original is missing: {target}")
        for snapshot in self.state["snapshots"]:
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
