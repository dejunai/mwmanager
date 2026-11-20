"""MWManager install ledger: a clean-install baseline, an append-only record of every change MWManager makes
to the game install, and the checks that make --rollback and redeploy possible.

Everything lives in MWManager's own folder (journal\\). Nothing is written into the game install and nothing
under Data Files\\MWSE\\mods.

  baseline\\baseline.json  a snapshot of the store install before prerequisites are installed.
  prerequisites\\prerequisites.json
                           the prerequisite layer captured after MWSE, MCP and MGE XE are ready. Normal rollback
                           returns here; a GOG-only rollback then applies the baseline as a second stage.
                           Files are only compared with
                           their own snapshot entry, never with a store list; the one exception is the three
                           masters and three BSAs, which are checked against known store-independent hashes
                           and reported (not refused) when modified. A copy of Morrowind.ini is kept too.
  install.jsonl            one line per install path MWManager creates/replaces/changes/removes; the first line
                           for a path records where its original is (absent = new file). Hash-chained.
  runs.jsonl               one line per run: tool, version, the ledger range it wrote (with its hash), inputs.
  inputs\\<sha256>.json     content-addressed copies of the choices a run used (FOMOD answers, options, order).
  mods.json                per mod: archive name + sha256, Nexus ids, staged folder, answers - mod rollback
                           re-extracts from the archive; nothing else is kept for mod files.

Command line: --capture-baseline, --verify, --rollback (with --dry-run, --gog-only, --no-staging) and
mod-rollback --dry-run. Mod re-extraction is implemented for tests but not offered on the command line yet.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import textwrap
import time
import uuid
import zipfile
from pathlib import Path

MANAGER_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = MANAGER_DIR / "journal"
WIDTH = 115

PREREQ_RULES = (
    ("prereq:mcp", ("mcpatch/*", "morrowind code patch.exe", "mcp readme.txt", "mcp history.txt",
                    "morrowind.original.exe", "mwedit/customfunctions.dat")),
    ("prereq:mge_xe", ("d3d8.dll", "d3d9.dll", "dinput8.dll", "mgexegui.exe", "mgegui.exe", "mgehost64.exe",
                       "mge xe readme.md", "mgexe.toml", "mge.ini", "mge3/*", "data files/distantland/*",
                       "data files/shaders/*", "data files/textures/mge/*", "data files/mwse/mods/mge xe options/*",
                       "data files/meshes/*_dist.nif", "data files/xe sky variations.esp")),
    ("prereq:mwse", ("mwse.dll", "mwse-update.exe", "mwse-update.exe.config", "mwse-update.pdb",
                     "mwse-update.exe.tmp", "newtonsoft.json.dll.tmp",
                     "mwse-version.txt", "lua51.dll", "csse.dll",
                     "libcrypto-1_1.dll", "libssl-1_1.dll", "newtonsoft.json.dll", "newtonsoft.json.xml",
                     "morrowind.pdb", "data files/mwse/core/*", "data files/mwse/lib/*", "data files/mwse/config/*",
                     "data files/mwse/.vscode/*", "data files/mwse/mods/readme.txt", "data files/textures/mwse/*",
                     "data files/meshes/mwse/*")),
    ("user-data", ("saves/*", "*.log", "warnings.txt", "mwse_minidump.dmp", "programflow.txt")),
    ("store-meta", ("goggame-*", "goglog.ini", "gog.ico", "support.ico", "unins000.*", "*.lnk",
                    "steam_api*.dll", "steam_appid.txt", ".egstore/*")),
    ("game-exe", ("morrowind.exe",)),
)
DEFAULT_LAYER = "start"
IGNORED_DIFF_LAYERS = ("user-data",)          # saves and logs change on every launch
BACKUP_PREFIXES = ("data files/off/", "data files/mwse/backups/")


# ----------------------------------------------------------------------------------------------- helpers
def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def rel_of(game_dir: Path, path: Path) -> str:
    return path.relative_to(game_dir).as_posix()


def key(rel: str) -> str:
    return rel.replace("\\", "/").casefold()


def hashes(path: Path) -> tuple[str, str]:
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha.update(block)
            md5.update(block)
    return sha.hexdigest(), md5.hexdigest()


def sha256_file(path: Path, progress=None, label: str | None = None) -> str:
    if not progress or not label:
        return hashes(path)[0]
    digest = hashlib.sha256()
    total = Path(path).stat().st_size
    done = 0
    last_report = time.monotonic()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
            done += len(block)
            now = time.monotonic()
            if now - last_report >= 5:
                progress(f"MWMANAGER-STAGE: {label} ({done / (1 << 20):,.0f} of "
                         f"{total / (1 << 20):,.0f} MiB hashed)")
                last_report = now
    return digest.hexdigest()


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def wrap(text: str, bullet: bool = True) -> str:
    lead = "- " if bullet else ""
    return textwrap.fill(text, WIDTH, initial_indent=lead, subsequent_indent="  ")


def walk_files(game_dir: Path):
    for base, dirs, files in os.walk(game_dir):
        dirs.sort()
        for name in sorted(files):
            yield Path(base) / name


def classify(rel: str) -> str:
    k = key(rel)
    for layer, patterns in PREREQ_RULES:
        if any(fnmatch.fnmatchcase(k, pat) for pat in patterns):
            return layer
    return DEFAULT_LAYER


# ------------------------------------------------------------------------------------- GOG's own hash list
def find_hashdb(game_dir: Path) -> Path | None:
    found = sorted(game_dir.glob("goggame-*.hashdb"))
    return found[0] if found else None


def read_hashdb(path: Path) -> dict[str, tuple[str, str]]:
    """{casefolded rel path: (rel path, md5)} from goggame-<id>.hashdb (a zip holding 1056-byte records:
    a 12-byte header, then per file a 1024-byte name and a 32-character MD5)."""
    with zipfile.ZipFile(path) as archive:
        data = archive.read(archive.namelist()[0])
    out, offset = {}, 12
    while offset + 1056 <= len(data):
        name = data[offset:offset + 1024].split(b"\0", 1)[0].decode("utf-8", "replace")
        md5 = data[offset + 1024:offset + 1056].decode("ascii", "replace").lower()
        if name:
            rel = name.replace("\\", "/")
            out[key(rel)] = (rel, md5)
        offset += 1056
    return out


# ------------------------------------------------------------------------------------------------ journal
class Journal:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.baseline_file = self.root / "baseline" / "baseline.json"
        self.baseline_ini = self.root / "baseline" / "Morrowind.ini"
        self.prerequisites_file = self.root / "prerequisites" / "prerequisites.json"
        self.ledger = self.root / "install.jsonl"
        self.runs = self.root / "runs.jsonl"
        self.mods_file = self.root / "mods.json"
        self.inputs = self.root / "inputs"

    # ---------------------------------------------------------------------------------------- baseline
    def has_baseline(self) -> bool:
        return self.baseline_file.is_file()

    @staticmethod
    def _identity_path(game_dir: Path) -> Path | None:
        candidates = sorted(game_dir.glob("goggame-*.hashdb"))
        candidates += [game_dir / name for name in ("steam_api.dll", "steam_api64.dll",
                                                      "Morrowind Launcher.exe", "Bethesda.TXT")]
        return next((path for path in candidates if path.is_file()), None)

    def baseline_matches_install(self, game_dir) -> tuple[bool, str]:
        """Reject a baseline from an install that was repaired, reinstalled, or replaced in place."""
        if not self.has_baseline():
            return False, "no baseline exists"
        game_dir = Path(game_dir).resolve()
        try:
            baseline = self.baseline()
        except (OSError, ValueError):
            return False, "the baseline file is unreadable"
        if baseline.get("game_dir_name", "").casefold() != game_dir.name.casefold():
            return False, "the selected game folder does not match the recorded baseline"
        identity = baseline.get("install_identity")
        if not identity:  # version 1 baselines already contain the same file facts
            recorded = baseline.get("files", {})
            path = self._identity_path(game_dir)
            if path:
                rel = rel_of(game_dir, path)
                info = recorded.get(rel) or next((v for r, v in recorded.items() if key(r) == key(rel)), None)
                identity = dict(info or {}, path=rel)
        if not identity or not identity.get("path"):
            return False, "the baseline has no store identity for the game folder"
        current = game_dir / identity["path"]
        if not current.is_file():
            return False, f"the recorded game-folder marker is missing: {identity['path']}"
        stat = current.stat()
        if stat.st_size != identity.get("size") or stat.st_mtime_ns != identity.get("mtime_ns"):
            return False, f"the store game-folder marker changed: {identity['path']}"
        if sha256_file(current) != identity.get("sha256"):
            return False, f"the store game-folder marker content changed: {identity['path']}"
        return True, "baseline belongs to this game folder"

    def archive_current(self, reason: str) -> Path:
        """Move an obsolete journal aside without discarding any rollback evidence."""
        target = self.root / "history" / f"install-{_dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        target.mkdir(parents=True, exist_ok=False)
        for path in (self.baseline_file.parent, self.prerequisites_file.parent, self.ledger, self.runs, self.mods_file,
                     self.inputs, self.root / "originals"):
            if path.exists():
                destination = target / path.name
                os.replace(path, destination)
        (target / "reason.txt").write_text(reason.rstrip() + "\n", encoding="utf-8")
        return target

    def capture_baseline(self, game_dir, *, force: bool = False, hashdb: Path | None = None) -> dict:
        """Read-only on the install. Refuses to overwrite an existing baseline unless force=True."""
        game_dir = Path(game_dir).resolve()
        if self.has_baseline() and not force:
            raise FileExistsError(f"A baseline already exists: {self.baseline_file}")
        if self.has_baseline():
            self.archive_current("Baseline replaced explicitly with --force-baseline.")
        hashdb = hashdb or find_hashdb(game_dir)
        try:                                    # optional extra: GOG's own list, only when present
            gog = read_hashdb(hashdb) if hashdb else {}
        except (OSError, zipfile.BadZipFile, IndexError):
            gog = {}
        from mwmanager_prereqs import CORE_FILE_HASHES
        core = {("data files/" + name).casefold(): (name, known) for name, known in CORE_FILE_HASHES.items()}
        files, warnings = {}, []
        for path in walk_files(game_dir):
            rel = rel_of(game_dir, path)
            st = path.stat()
            sha, md5 = hashes(path)
            gog_entry = gog.get(key(rel))
            files[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha, "md5": md5,
                          "layer": classify(rel), "store_match": (gog_entry[1] == md5) if gog_entry else None}
            if files[rel]["layer"].startswith("prereq:") or files[rel]["layer"] == "game-exe":
                kept = self.root / "baseline" / "files" / sha
                if not kept.exists():
                    kept.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, kept)
                files[rel]["backup"] = f"journal:baseline/files/{sha}"
            if key(rel) in core:
                name, known = core.pop(key(rel))
                files[rel]["core_ok"] = sha in known
                if sha not in known:
                    warnings.append(f"Data Files/{name} has been modified (it does not match the known original "
                                    "shared by GOG, Steam and other stores). Restore it from your store if that "
                                    "is unexpected.")
        for name, _known in core.values():
            warnings.append(f"Data Files/{name} is missing.")
        summary = {}
        for item in files.values():
            summary[item["layer"]] = summary.get(item["layer"], 0) + 1
        identity_path = self._identity_path(game_dir)
        identity = None
        if identity_path:
            identity_stat = identity_path.stat()
            identity = {"path": rel_of(game_dir, identity_path), "size": identity_stat.st_size,
                        "mtime_ns": identity_stat.st_mtime_ns, "sha256": sha256_file(identity_path)}
        record = {"version": 2, "captured": now(), "game_dir_name": game_dir.name,
                  "install_identity": identity,
                  "gog": {"hashdb": hashdb.name if hashdb else None,
                          "hashdb_sha256": sha256_file(hashdb) if hashdb else None, "files": len(gog)},
                  "summary": summary, "warnings": warnings, "files": files}
        self.baseline_file.parent.mkdir(parents=True, exist_ok=True)
        ini = game_dir / "Morrowind.ini"
        if ini.is_file():
            shutil.copy2(ini, self.baseline_ini)
            record["ini_sha256"] = sha256_file(self.baseline_ini)
        tmp = self.baseline_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=0, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.baseline_file)
        return record

    def capture_prerequisites(self, game_dir) -> dict:
        """Record the installed prerequisite layer and keep exact copies for the default rollback target."""
        game_dir = Path(game_dir).resolve()
        baseline = {key(rel): info for rel, info in self.baseline()["files"].items()}
        files = {}
        for path in walk_files(game_dir):
            rel = rel_of(game_dir, path)
            layer = classify(rel)
            prior = baseline.get(key(rel))
            digest = sha256_file(path)
            if not (layer.startswith("prereq:") or layer == "game-exe" or
                    key(rel) == "mwedit/customfunctions.dat"):
                continue
            stat = path.stat()
            kept = self.root / "prerequisites" / "files" / digest
            if not kept.exists():
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, kept)
            files[rel] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest,
                          "layer": layer, "baseline_sha256": prior["sha256"] if prior else None,
                          "backup": f"journal:prerequisites/files/{digest}"}
        record = {"version": 1, "captured": now(), "game_dir_name": game_dir.name, "files": files}
        self.prerequisites_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.prerequisites_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=0, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.prerequisites_file)
        return record

    def prerequisites(self) -> dict:
        if not self.prerequisites_file.is_file():
            return {"files": {}}
        return json.loads(self.prerequisites_file.read_text(encoding="utf-8"))

    def preserve_file(self, path) -> dict:
        """Keep a content-addressed copy of a recoverable original."""
        if not self.has_baseline():
            raise RuntimeError("game files cannot be changed before the game baseline is recorded")
        path = Path(path).resolve()
        if not path.is_file():
            return {"kind": "absent"}
        digest = sha256_file(path)
        kept = self.root / "originals" / digest
        if kept.exists() and sha256_file(kept) != digest:
            raise RuntimeError(f"journal original is corrupt: {kept}")
        if not kept.exists():
            kept.parent.mkdir(parents=True, exist_ok=True)
            temporary = kept.with_suffix(".tmp")
            shutil.copy2(path, temporary)
            os.replace(temporary, kept)
        return {"kind": "journal", "location": f"journal:originals/{digest}", "sha256": digest}

    def preserve_original(self, game_dir, path) -> dict:
        """Keep a content-addressed copy before a game file is overwritten or removed."""
        game_dir, path = Path(game_dir).resolve(), Path(path).resolve()
        rel_of(game_dir, path)  # validates scope
        return self.preserve_file(path)

    def record_deployment(self, game_dir, operations: list[dict], *, tool: str, tag: str,
                          inputs: dict | None = None, moves: list[dict] | None = None,
                          progress=None) -> dict:
        """Bridge a completed GUI deployment's operation list into the install rollback ledger."""
        game_dir = Path(game_dir).resolve()
        if not self.has_baseline():
            raise RuntimeError("deployment requires a game baseline so rollback can remove it")
        run = self.start_run(tool, inputs)
        records = []
        total = len(operations)
        for index, operation in enumerate(operations, 1):
            target = Path(operation["target"]).resolve()
            rel = rel_of(game_dir, target)
            backup = Path(operation["backup"]).resolve() if operation.get("backup") else None
            original = self.preserve_file(backup) if backup else {"kind": "absent"}
            if not target.is_file():
                raise FileNotFoundError(f"deployed file disappeared before journaling: {target}")
            if progress and (index == 1 or index % 250 == 0):
                progress(f"MWMANAGER-STAGE: Recording deployment: {index:,} of {total:,} files verified")
            deployed_hash = operation.get("sha256") or sha256_file(
                target, progress, f"Recording deployment: {index:,} of {total:,} files — {target.name}")
            operation["sha256"] = deployed_hash
            records.append({"run": run["run"], "path": rel,
                            "action": "replace" if backup else "create",
                            "after_sha256": deployed_hash, "original": original, "tag": tag})
        for move in moves or []:
            source, target = Path(move["source"]).resolve(), Path(move["target"]).resolve()
            original = self.preserve_file(target)
            records.append({"run": run["run"], "path": rel_of(game_dir, source), "action": "move",
                            "after_sha256": None, "original": original, "tag": tag})
            records.append({"run": run["run"], "path": rel_of(game_dir, target), "action": "create",
                            "after_sha256": original.get("sha256"), "original": {"kind": "absent"},
                            "tag": tag})
        self.record_many(records)
        return self.finish_run(run)

    def baseline(self) -> dict:
        return json.loads(self.baseline_file.read_text(encoding="utf-8"))

    # ----------------------------------------------------------------------------------------- ledger
    @staticmethod
    def _read_jsonl(path: Path) -> tuple[list[dict], list[str]]:
        entries, problems, prev = [], [], None
        if not path.is_file():
            return entries, problems
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                if number == len(lines):
                    problems.append(f"{path.name}: line {number} is incomplete (an interrupted write); ignored")
                    break
                problems.append(f"{path.name}: line {number} is not valid JSON")
                continue
            body = {k: v for k, v in entry.items() if k != "hash"}
            expected = hashlib.sha256(((prev or "") + canon(body)).encode("utf-8")).hexdigest()
            if entry.get("prev") != prev or entry.get("hash") != expected:
                problems.append(f"{path.name}: hash chain broken at line {number}")
            entries.append(entry)
            prev = entry.get("hash")
        return entries, problems

    @classmethod
    def _append(cls, path: Path, entry: dict) -> dict:
        return cls._append_many(path, [entry])[0]

    @classmethod
    def _append_many(cls, path: Path, new_entries: list[dict]) -> list[dict]:
        if not new_entries:
            return []
        entries, _problems = cls._read_jsonl(path)
        prev = entries[-1]["hash"] if entries else None
        seq = (entries[-1]["seq"] + 1) if entries else 1
        prepared = []
        for entry in new_entries:
            entry = dict(entry, seq=seq, prev=prev)
            entry.pop("hash", None)
            entry["hash"] = hashlib.sha256(((prev or "") + canon(entry)).encode("utf-8")).hexdigest()
            prepared.append(entry)
            prev, seq = entry["hash"], seq + 1
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.writelines(canon(entry) + "\n" for entry in prepared)
            stream.flush()
            os.fsync(stream.fileno())
        return prepared

    def entries(self) -> tuple[list[dict], list[str]]:
        return self._read_jsonl(self.ledger)

    def first_original(self, rel: str, entries=None) -> dict | None:
        for entry in entries if entries is not None else self.entries()[0]:
            if key(entry["path"]) == key(rel) and entry.get("original"):
                return entry["original"]
        return None

    def record(self, run: str, rel: str, action: str, after_sha256: str | None, original: dict | None = None,
               tag: str | None = None, mod: str | None = None) -> dict:
        return self.record_many([{"run": run, "path": rel, "action": action,
                                  "after_sha256": after_sha256, "original": original,
                                  "tag": tag, "mod": mod}])[0]

    def record_many(self, records: list[dict]) -> list[dict]:
        """One install change. original = {"kind": absent|gog|off|mwse-backups|mwmanager|disabled|baseline,
        "location": "game:<rel>" | "manager:<rel>" | "journal:<rel>", "sha256": ...}. Morrowind.ini edits use
        kind "baseline" with location "journal:baseline/Morrowind.ini".; only the first original for a path counts."""
        existing, _problems = self.entries()
        original_paths = {key(entry["path"]) for entry in existing if entry.get("original")}
        entries = []
        for record in records:
            action = record["action"]
            if action not in ("create", "replace", "modify", "remove", "move", "disable"):
                raise ValueError(f"unknown action {action}")
            original = record.get("original")
            if original and original.get("kind") != "absent" and not original.get("sha256"):
                raise ValueError("an original needs its sha256")
            path = str(record["path"]).replace("\\", "/")
            if key(path) in original_paths:
                original = None
            elif original:
                original_paths.add(key(path))
            entry = {"ts": now(), "run": record["run"], "path": path, "action": action,
                     "after_sha256": record.get("after_sha256"), "original": original}
            if record.get("tag"):
                entry["tag"] = record["tag"]
            if record.get("mod"):
                entry["mod"] = record["mod"]
            entries.append(entry)
        return self._append_many(self.ledger, entries)

    def move_original(self, game_dir, source, destination, run: str, *, tag: str | None = None) -> dict:
        """Move an original file while preserving a journal-owned copy for reliable rollback."""
        if not self.has_baseline():
            raise RuntimeError("original files cannot be moved before the game baseline is recorded")
        game_dir, source, destination = Path(game_dir).resolve(), Path(source).resolve(), Path(destination).resolve()
        try:
            source_rel = rel_of(game_dir, source)
        except ValueError as exc:
            raise ValueError(f"the moved original is outside the game folder: {source}") from exc
        if not source.is_file():
            raise FileNotFoundError(source)
        original = self.preserve_original(game_dir, source)
        digest = original["sha256"]
        if destination.exists():
            raise FileExistsError(destination)
        self.record(run, source_rel, "move", None,
                    original, tag=tag)
        try:
            destination_rel = rel_of(game_dir, destination)
        except ValueError:
            destination_rel = None
        if destination_rel is not None:
            self.record(run, destination_rel, "create", digest, {"kind": "absent"}, tag=tag)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return {"source": source_rel, "destination": str(destination), "sha256": digest}

    def migrate_unknown_asset_backups(self, game_dir) -> int:
        """Rename the old manager-owned Off/Unknown Plugin tree and bring every file into the ledger."""
        game_dir = Path(game_dir).resolve()
        old = game_dir / "Data Files" / "Off" / "Unknown Plugin"
        if not old.is_dir():
            return 0
        files = [path for path in old.rglob("*") if path.is_file()]
        destinations = [(path, old.parent / "Original Install" / path.relative_to(old)) for path in files]
        collision = next((target for _source, target in destinations if target.exists()), None)
        if collision:
            raise FileExistsError(f"cannot migrate legacy asset backup over {collision}")
        run = self.start_run("mwmanager_journal.py", {"operation": "rename legacy Unknown Plugin asset backups"})
        for source, target in destinations:
            digest = sha256_file(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            self.record(run["run"], rel_of(game_dir, target), "create", digest, {"kind": "absent"},
                        tag="asset-repair-backup")
        for folder, _dirs, _names in os.walk(old, topdown=False):
            Path(folder).rmdir()
        self.finish_run(run)
        return len(files)

    def migrate_official_asset_backups(self, game_dir) -> int:
        """Consolidate the obsolete second shipped-asset owner under Off/Original Install."""
        game_dir = Path(game_dir).resolve()
        old = game_dir / "Data Files" / "Off" / "Official Assets"
        if not old.is_dir():
            return 0
        files = [path for path in old.rglob("*") if path.is_file()]
        owner = old.parent / "Original Install" / "Official Assets"
        destinations = [(path, owner / path.relative_to(old)) for path in files]
        collision = next((target for _source, target in destinations if target.exists()), None)
        if collision:
            raise FileExistsError(f"cannot consolidate asset backup over {collision}")
        run = self.start_run("mwmanager_journal.py", {"operation": "consolidate Official Assets owner"})
        for source, target in destinations:
            digest = sha256_file(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            self.record(run["run"], rel_of(game_dir, target), "create", digest, {"kind": "absent"},
                        tag="asset-repair-backup")
        for folder, _dirs, _names in os.walk(old, topdown=False):
            Path(folder).rmdir()
        self.finish_run(run)
        return len(files)

    # ------------------------------------------------------------------------------------- runs / inputs
    def store_input(self, value) -> str:
        data = (canon(value) if not isinstance(value, (bytes, str)) else value)
        data = data.encode("utf-8") if isinstance(data, str) else data
        digest = hashlib.sha256(data).hexdigest()
        target = self.inputs / f"{digest}.json"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return f"inputs/{digest}.json"

    def start_run(self, tool: str, inputs: dict | None = None) -> dict:
        tool_path = MANAGER_DIR / tool
        version = sha256_file(tool_path)[:16] if tool_path.is_file() else None
        stored = {name: self.store_input(value) for name, value in (inputs or {}).items()}
        entries, _ = self.entries()
        return {"run": _dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6], "tool": tool,
                "tool_version": version, "inputs": stored, "first_seq": (entries[-1]["seq"] + 1) if entries else 1}

    def finish_run(self, run: dict) -> dict:
        entries, _ = self.entries()
        mine = [e for e in entries if e["run"] == run["run"]]
        span = "".join(e["hash"] for e in mine)
        return self._append(self.runs, {"ts": now(), "run": run["run"], "tool": run["tool"],
                                        "tool_version": run["tool_version"], "inputs": run["inputs"],
                                        "ledger": [mine[0]["seq"], mine[-1]["seq"]] if mine else None,
                                        "ledger_sha256": hashlib.sha256(span.encode()).hexdigest()})

    # ------------------------------------------------------------------------------------------- mods
    def mods(self) -> dict:
        try:
            return json.loads(self.mods_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def upsert_mod(self, mod_key: str, **fields) -> dict:
        mods = self.mods()
        mods.setdefault(mod_key, {}).update({k: v for k, v in fields.items() if v is not None})
        self.mods_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.mods_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(mods, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.mods_file)
        return mods[mod_key]

    def import_stage_markers(self, staging: Path) -> list[str]:
        """Register every staged mod from its .mwmanager-stage.json (archive path + sha256)."""
        added = []
        for marker in sorted(Path(staging).rglob(".mwmanager-stage.json")):
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            archive = data.get("archive")
            if not archive or not data.get("sha256"):
                continue
            name = Path(archive).name
            match = re.search(r"(?:^|[ _-])(\d{3,6})(?=[ -])", Path(archive).stem)
            mod_key = f"sha256-{str(data['sha256']).lower()[:16]}"
            self.upsert_mod(mod_key, archive=str(archive), archive_name=name, archive_sha256=str(data["sha256"]).lower(),
                            nexus_mod_id=data.get("nexus_mod_id") or (match.group(1) if match else None),
                            nexus_file_id=data.get("nexus_file_id"), staged=str(marker.parent),
                            selected_masters=data.get("selected_masters"))
            added.append(mod_key)
        return added

    def plan_mod_rollback(self, mod_key: str, downloads: Path | None = None) -> dict:
        """Mod rollback = wipe the staged folder and re-extract the archive. Checks the archive first."""
        mod = self.mods().get(mod_key)
        if not mod:
            return {"status": "unknown-mod", "mod": mod_key}
        candidates = [Path(mod["archive"])] if mod.get("archive") else []
        if downloads and mod.get("archive_name"):
            candidates.append(Path(downloads) / mod["archive_name"])
        archive = next((p for p in candidates if p.is_file()), None)
        page = (f"https://www.nexusmods.com/morrowind/mods/{mod['nexus_mod_id']}?tab=files"
                if mod.get("nexus_mod_id") else None)
        result = {"mod": mod_key, "archive_name": mod.get("archive_name"), "staged": mod.get("staged"),
                  "download_page": page}
        if archive is None:
            return dict(result, status="archive-missing")
        if sha256_file(archive) != mod.get("archive_sha256"):
            return dict(result, status="archive-changed", archive=str(archive))
        return dict(result, status="ready", archive=str(archive))

    # ------------------------------------------------------------------------------------ verify / state
    def checkpoint_state(self, preserve_prerequisites: bool = True) -> dict[str, str | None]:
        state = {key(rel): info["sha256"] for rel, info in self.baseline()["files"].items()}
        if preserve_prerequisites:
            for rel, info in self.prerequisites()["files"].items():
                state[key(rel)] = info["sha256"]
        return state

    def expected_state(self) -> dict[str, str | None]:
        """{casefolded rel: sha256 or None (should not exist)} after the last recorded change."""
        state = self.checkpoint_state()
        for entry in self.entries()[0]:
            state[key(entry["path"])] = entry.get("after_sha256")
        return state

    def _originals(self, entries) -> dict[str, dict]:
        out = {}
        for entry in entries:
            k = key(entry["path"])
            if k not in out and entry.get("original"):
                out[k] = dict(entry["original"], path=entry["path"])
        return out

    def _resolve(self, game_dir: Path, location: str) -> Path:
        scope, _, rel = location.partition(":")
        base = game_dir if scope == "game" else MANAGER_DIR if scope == "manager" else self.root
        path = (base / rel).resolve()
        if base.resolve() not in path.parents and path != base.resolve():
            raise ValueError(f"unsafe location {location}")
        return path

    def verify(self, game_dir, against: str = "baseline", full_hash: bool = False) -> dict:
        game_dir = Path(game_dir).resolve()
        base = self.baseline()
        layers = {key(r): i["layer"] for r, i in base["files"].items()}
        stats = {key(r): i for r, i in base["files"].items()}
        entries, problems = self.entries()
        run_entries, run_problems = self._read_jsonl(self.runs)
        problems += run_problems
        originals = self._originals(entries)
        backup_keys = {key(o["location"].partition(":")[2]) for o in originals.values()
                       if o.get("location", "").startswith("game:")}
        if against == "baseline":
            expected = self.checkpoint_state(False)
        elif against == "prerequisites":
            expected = self.checkpoint_state(True)
            for rel, info in self.prerequisites()["files"].items():
                stats[key(rel)] = info
                layers[key(rel)] = info["layer"]
        else:
            expected = self.expected_state()
        if against == "deployed":
            for original in originals.values():
                if original.get("location", "").startswith("game:"):
                    expected[key(original["location"].partition(":")[2])] = original["sha256"]
        journaled = {key(e["path"]) for e in entries}
        actual = {key(rel_of(game_dir, p)): p for p in walk_files(game_dir)}
        report = {"against": against, "changed": [], "missing": [], "extra": [], "unexplained": [],
                  "journal_problems": problems, "original_problems": [], "ignored": 0}
        # Against the deployed state, two things are MWManager's own and are not journaled change by change:
        # Morrowind.ini (either ours or the original; the original is kept at baseline\\Morrowind.ini and a
        # rollback restores it), and Data Files\\Off\\ (the originals MWManager kept before repairing a file).
        own_ini = self.baseline_ini.is_file()
        for k, want in expected.items():
            if layers.get(k) in IGNORED_DIFF_LAYERS or (own_ini and k == "morrowind.ini"):
                report["ignored"] += 1
                continue
            path = actual.get(k)
            if want is None:
                if path is not None:
                    report["extra"].append(rel_of(game_dir, path))
                continue
            if path is None:
                report["missing"].append(k)
                if k not in journaled:
                    report["unexplained"].append(k)
                continue
            st, info = path.stat(), stats.get(k)
            same_quick = (info is not None and info["sha256"] == want and st.st_size == info["size"]
                          and st.st_mtime_ns == info["mtime_ns"])
            if same_quick and not full_hash:
                continue
            if sha256_file(path) != want:
                report["changed"].append(rel_of(game_dir, path))
                if k not in journaled:
                    report["unexplained"].append(rel_of(game_dir, path))
        for k, path in actual.items():
            if k in expected or classify(k) in IGNORED_DIFF_LAYERS:
                continue
            if against == "deployed" and k.startswith("data files/off/"):
                report["ignored"] += 1
                continue
            report["extra"].append(rel_of(game_dir, path))
            if k not in journaled and k not in backup_keys:
                report["unexplained"].append(rel_of(game_dir, path))
        for k, original in originals.items():
            if original["kind"] == "absent":
                continue
            current = game_dir / original["path"]
            if current.is_file() and sha256_file(current) == original["sha256"]:
                continue                                  # already back in place; the copy is no longer needed
            try:
                source = self._resolve(game_dir, original["location"])
            except (KeyError, ValueError) as exc:
                report["original_problems"].append(f"{original['path']}: {exc}")
                continue
            if not source.is_file():
                report["original_problems"].append(f"{original['path']}: original missing at {original['location']}")
            elif sha256_file(source) != original["sha256"]:
                report["original_problems"].append(f"{original['path']}: original at {original['location']} "
                                                   "does not match its recorded hash")
        report["ok"] = not (report["changed"] or report["missing"] or report["extra"] or problems
                            or report["original_problems"])
        return report

    def archive_ledger(self) -> Path | None:
        """After a verified rollback the install is the baseline again: start a new ledger, keep the old one."""
        if not self.ledger.is_file():
            return None
        target = self.root / "history" / f"install-{_dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.ledger, target)
        return target

    def clear_staging(self) -> int:
        """Remove MWManager's staging tree after a verified, explicitly requested rollback."""
        manager = self.root.parent.resolve()
        staging = manager / "staging"
        if not staging.exists():
            return 0
        if staging.is_symlink() or not staging.is_dir() or staging.resolve().parent != manager:
            raise ValueError(f"Refusing to clear an unexpected staging path: {staging}")
        entries = len(list(staging.iterdir()))
        shutil.rmtree(staging)
        return entries

    # -------------------------------------------------------------------------------------- rollback
    def plan_rollback(self, game_dir, *, preserve_prerequisites: bool = True) -> dict:
        """Return to the prerequisite checkpoint by default; optionally add a store-only second stage."""
        game_dir = Path(game_dir).resolve()
        base = self.baseline()
        entries, problems = self.entries()
        originals = self._originals(entries)
        steps, prerequisite_steps, refuse, used_backups = [], [], list(problems), []
        rollback_checkpoint = self.checkpoint_state(True)
        last_seq = {}                               # one pass: a per-path scan of the ledger was O(n^2)
        for e in entries:
            k = key(e["path"])
            last_seq[k] = max(last_seq.get(k, e["seq"]), e["seq"])
        for k, original in sorted(originals.items(), key=lambda kv: -last_seq[kv[0]]):
            if k == "morrowind.ini":
                continue                         # the whole baseline INI is authoritative, including old ledgers
            rel = original["path"]
            if k not in rollback_checkpoint:
                # A later repair may have preserved the bytes of a file generated after
                # the checkpoint. Checkpoint ownership is authoritative: if the file
                # did not exist at the prerequisite point, it must be deleted, not
                # reconstructed from its later first journaled state.
                steps.append({"op": "delete", "path": rel})
                continue
            if original["kind"] == "absent":
                steps.append({"op": "delete", "path": rel})
                continue
            current = game_dir / rel
            checkpoint_hash = rollback_checkpoint.get(k)
            if checkpoint_hash is not None and current.is_file() and sha256_file(current) == checkpoint_hash:
                continue
            if current.is_file() and sha256_file(current) == original["sha256"]:
                continue                                  # already the original
            try:
                source = self._resolve(game_dir, original["location"])
            except (KeyError, ValueError) as exc:
                refuse.append(f"{rel}: {exc}")
                continue
            if not source.is_file() or sha256_file(source) != original["sha256"]:
                refuse.append(f"{rel}: original missing or changed at {original['location']}")
                continue
            if checkpoint_hash is not None and checkpoint_hash != original["sha256"]:
                refuse.append(f"{rel}: the recorded original does not match the prerequisite checkpoint")
                continue
            steps.append({"op": "restore", "path": rel, "from": original["location"]})
            if key(original["location"].partition(":")[2]).startswith(BACKUP_PREFIXES) and \
                    original["location"].startswith("game:"):
                used_backups.append(original["location"])
        ini = game_dir / "Morrowind.ini"
        ini_source = "journal:baseline/Morrowind.ini"
        ini_hash = sha256_file(self.baseline_ini) if self.baseline_ini.is_file() else None
        if ini_hash and (not ini.is_file() or sha256_file(ini) != ini_hash):
            steps.append({"op": "restore", "path": "Morrowind.ini", "from": ini_source})
        if not preserve_prerequisites:
            base_by_key = {key(name): item for name, item in base["files"].items()}
            for rel, info in self.prerequisites()["files"].items():
                baseline_info = base["files"].get(rel) or base_by_key.get(key(rel))
                if baseline_info is None:
                    prerequisite_steps.append({"op": "delete", "path": rel})
                elif baseline_info["sha256"] != info["sha256"]:
                    source = ("journal:baseline/Morrowind.ini" if key(rel) == "morrowind.ini"
                              and self.baseline_ini.is_file() else baseline_info.get("backup"))
                    if source:
                        prerequisite_steps.append({"op": "restore", "path": rel, "from": source})
                    else:
                        refuse.append(f"{rel}: no store-original copy was retained")
            planned = {key(step["path"]) for step in prerequisite_steps}
            for path in walk_files(game_dir):
                rel = rel_of(game_dir, path)
                k = key(rel)
                if k in planned or not classify(rel).startswith("prereq:"):
                    continue
                baseline_info = base["files"].get(rel) or base_by_key.get(k)
                if baseline_info is None:
                    prerequisite_steps.append({"op": "delete", "path": rel})
                elif sha256_file(path) != baseline_info["sha256"]:
                    if baseline_info.get("backup"):
                        prerequisite_steps.append({"op": "restore", "path": rel,
                                                   "from": baseline_info["backup"]})
                    else:
                        refuse.append(f"{rel}: no store-original copy was retained")
        journaled = {key(e["path"]) for e in entries}
        prereq_paths = {key(rel) for rel in self.prerequisites()["files"]}
        planned_paths = {key(step["path"]) for step in steps + prerequisite_steps}
        against = "prerequisites" if preserve_prerequisites else "baseline"
        unexplained = [p for p in self.verify(game_dir, against=against)["unexplained"]
                       if key(p) not in journaled and key(p) not in prereq_paths and key(p) not in planned_paths]
        return {"steps": steps, "prerequisite_steps": prerequisite_steps, "refuse": refuse,
                "unexplained": unexplained,
                "cleanup_after_verify": sorted(set(used_backups))}

    def execute_rollback(self, game_dir, plan: dict) -> None:
        """Carry out a plan. Restores are copies; backups stay until cleanup. Folders a delete empties go too."""
        if plan["refuse"]:
            raise RuntimeError("rollback refused: " + "; ".join(plan["refuse"]))
        game_dir = Path(game_dir).resolve()
        steps = plan["steps"] + plan.get("prerequisite_steps", [])
        for step in steps:                          # a damaged ledger line must not reach outside the install
            if not (game_dir / step["path"]).resolve().is_relative_to(game_dir):
                raise ValueError(f"rollback refused: {step['path']} is outside the game folder")
        for step in steps:
            target = game_dir / step["path"]
            if step["op"] == "delete":
                if target.is_file():
                    target.unlink()
            elif step["op"] == "restore":
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self._resolve(game_dir, step["from"]), target)
            elif step["op"] == "restore-ini":
                shutil.copy2(self.baseline_ini, target)
        keep = {game_dir, game_dir / "Data Files"}
        for step in steps:
            if step["op"] == "delete":
                folder = (game_dir / step["path"]).parent
                while folder not in keep and folder.is_relative_to(game_dir):
                    try:
                        folder.rmdir()                  # only succeeds while empty: logs and saves keep theirs
                    except OSError:
                        break
                    folder = folder.parent

    def cleanup_used_backups(self, game_dir, plan: dict, *, apply: bool = False) -> list[str]:
        """After a verified rollback: remove the Data Files\\Off / MWSE\\backups copies that were used."""
        game_dir = Path(game_dir).resolve()
        removed = []
        for location in plan["cleanup_after_verify"]:
            path = self._resolve(game_dir, location)
            if path.is_file():
                removed.append(location)
                if apply:
                    path.unlink()
        if apply:
            for prefix in ("Data Files/Off", "Data Files/MWSE/backups"):
                root = game_dir / prefix
                for base, _dirs, _files in sorted(os.walk(root), key=lambda x: -len(x[0])) if root.is_dir() else []:
                    if not os.listdir(base):
                        os.rmdir(base)
        return removed


# ------------------------------------------------------------------------------------------------- CLI
def print_report(lines: list[str], title: str) -> None:
    print(wrap(title, bullet=False))
    for line in lines:
        print(wrap(line))


def run_cli(game_dir: Path, args, journal: Journal) -> int:
    if args.capture_baseline:
        try:
            record = journal.capture_baseline(game_dir, force=args.force_baseline)
        except FileExistsError as exc:
            print_report([str(exc), "Use --force-baseline only to replace the snapshot on purpose; rollback then "
                          "returns to the new one."], "Baseline not captured:")
            return 2
        summary = [f"{layer}: {count} file(s)" for layer, count in sorted(record["summary"].items())]
        print_report(summary + [f"Warning: {w}" for w in record["warnings"]],
                     f"Baseline captured: {journal.baseline_file}")
        return 0
    if args.capture_prerequisites:
        if not journal.has_baseline():
            print_report(["Capture the store baseline first."], "Prerequisites not captured:")
            return 2
        record = journal.capture_prerequisites(game_dir)
        print_report([f"Prerequisite-layer files: {len(record['files'])}"],
                     f"Prerequisites captured: {journal.prerequisites_file}")
        return 0
    if args.mod_rollback:
        if not args.dry_run:
            print_report(["Only --mod-rollback KEY --dry-run is available in this version."], "Mod rollback not run:")
            return 2
        plan = journal.plan_mod_rollback(args.mod_rollback, Path(args.archive_dir) if args.archive_dir else None)
        lines = [f"{k}: {v}" for k, v in plan.items() if v]
        if plan["status"] == "archive-missing":
            lines.append("The archive is missing, so the mod cannot be restored. Re-download it from its Nexus page"
                         + (f": {plan['download_page']}" if plan.get("download_page") else " (no Nexus id recorded)."))
            if args.open_download and plan.get("download_page"):
                import mwnexus_browser
                mwnexus_browser.open_page(plan["download_page"])
        print_report(lines, "Mod rollback plan (dry run, nothing changed):")
        return 0 if plan["status"] == "ready" else 1
    if not journal.has_baseline():
        print_report(["No baseline exists yet. Run --capture-baseline on the game folder as you start from it (fresh or "
                      "already modded) before MWManager deploys anything."], "Nothing to compare against:")
        return 2
    if args.verify:
        report = journal.verify(game_dir, against=args.verify_against, full_hash=args.full_hash)
        lines = [f"{label}: {len(report[name])}" + (f" (first: {report[name][0]})" if report[name] else "")
                 for label, name in (("Changed", "changed"), ("Missing", "missing"), ("Extra", "extra"),
                                     ("Not explained by the journal", "unexplained"))]
        lines += report["journal_problems"] + report["original_problems"]
        print_report(lines, f"Verify against {args.verify_against}: {'OK' if report['ok'] else 'differences found'}")
        return 0 if report["ok"] else 1
    if args.rollback:
        plan = journal.plan_rollback(game_dir, preserve_prerequisites=not args.gog_only)
        lines = [f"{s['op']}: {s['path']}" + (f" (from {s['from']})" if s.get("from") else "") for s in plan["steps"]]
        lines += [f"GOG-only second stage: {s['op']}: {s['path']}" +
                  (f" (from {s['from']})" if s.get("from") else "")
                  for s in plan["prerequisite_steps"]]
        lines += [f"REFUSE: {r}" for r in plan["refuse"]]
        lines += [f"Not explained by the journal (left alone, review): {p}" for p in plan["unexplained"]]
        lines += [f"After a verified rollback, remove the used backup {c}" for c in plan["cleanup_after_verify"]]
        if args.no_staging:
            lines.append(f"After verification, clear MWManager staging: {journal.root.parent / 'staging'}")
        if args.dry_run:
            print_report(lines or ["The game folder already matches the baseline."],
                         "Rollback plan (dry run, nothing changed):")
            return 1 if plan["refuse"] else 0
        if plan["refuse"] or plan["unexplained"]:
            print_report(lines, "Rollback refused:")
            return 1
        journal.execute_rollback(game_dir, plan)
        against = "baseline" if args.gog_only else "prerequisites"
        report = journal.verify(game_dir, against=against, full_hash=True)
        if not report["ok"]:
            problems = report["changed"] + report["missing"] + report["extra"] + \
                report["journal_problems"] + report["original_problems"]
            print_report(problems, "Rollback files were applied, but verification failed; backups were retained:")
            return 1
        try:
            cleared_staging = journal.clear_staging() if args.no_staging else 0
        except (OSError, ValueError) as exc:
            print_report([str(exc)], "Game rollback verified, but staging was not cleared:")
            return 1
        removed = journal.cleanup_used_backups(game_dir, plan, apply=True)
        archived = journal.archive_ledger()
        manager_dir = journal.root.parent
        # plugins turned off in the GUI belong to the install that was just rolled back: keep them with its ledger
        stamp = archived.stem if archived else f"install-{_dt.datetime.now():%Y%m%d-%H%M%S}"
        for name in ("disabled_plugins", "mwmanager_disabled.txt"):
            if (manager_dir / name).exists():
                (journal.root / "history").mkdir(parents=True, exist_ok=True)
                os.replace(manager_dir / name, journal.root / "history" / f"{stamp}-{name}")
        try:
            from workflow_safety import atomic_json
            atomic_json(manager_dir / "major_master_deployments.json", {"version": 1, "deployments": []})
        except OSError as exc:
            print_report([str(exc)], "Game rollback verified, but deployment ownership could not be reset:")
            return 1
        settings_path = manager_dir / "mwmanager_gui_settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings.update(first_run=1, init_complete=0)
            settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError, AttributeError):   # missing or damaged settings: setup starts over anyway
            pass
        try:
            import mwmanager_setup_state as setup_state
            state_path = manager_dir / "mwmanager_setup_state.json"
            setup_state.start(state_path, game_dir)
            setup_state.complete(state_path, game_dir, "paths_confirmed")
            # preflight_complete is not marked, even when the prerequisites were kept: the rollback also took
            # out the MWSE Lua suite and the moved add-on documents, which the preflight puts back
        except OSError as exc:
            print_report([str(exc)], "Game rollback verified, but setup checkpoints could not be reset:")
            return 1
        summary = [f"Verified against {against} with full hashes."]
        if args.no_staging:
            summary.append(f"Cleared {cleared_staging} staging entr{'y' if cleared_staging == 1 else 'ies'}.")
        summary += [f"Removed {len(removed)} used backup file(s).",
                    f"Archived the completed ledger at {archived}."]
        print_report(summary, "Rollback complete:")
        return 0
    return 2


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--capture-baseline", action="store_true",
                    help="snapshot the clean store install before prerequisites (read-only on the install)")
    ap.add_argument("--capture-prerequisites", action="store_true",
                    help="record MWSE, MCP and MGE XE as the default rollback checkpoint")
    ap.add_argument("--force-baseline", action="store_true", help="replace an existing baseline")
    ap.add_argument("--verify", action="store_true", help="compare the install with the baseline or deployed state")
    ap.add_argument("--verify-against", choices=("baseline", "prerequisites", "deployed"), default="baseline")
    ap.add_argument("--full-hash", action="store_true", help="hash every file instead of trusting size and date")
    ap.add_argument("--rollback", action="store_true",
                    help="return to the prerequisite checkpoint; --dry-run reports the plan without changes")
    ap.add_argument("--gog-only", action="store_true",
                    help="with --rollback: add a second stage that removes prerequisites too")
    ap.add_argument("--no-staging", action="store_true",
                    help="with --rollback: clear MWManager staging after the rollback verifies")
    ap.add_argument("--mod-rollback", metavar="MOD_KEY", help="with --dry-run: check a mod can be re-extracted")
    ap.add_argument("--archive-dir", help="folder to look for a mod's archive in")
    ap.add_argument("--open-download", action="store_true",
                    help="with --mod-rollback: open the mod's Nexus page if its archive is missing")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MWManager install ledger: baseline, verify, rollback (dry run).")
    ap.add_argument("--game-dir", required=True, help="the Morrowind folder (holding Morrowind.exe)")
    ap.add_argument("--journal-dir", help="defaults to journal\\ in MWManager's folder")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--import-mods", metavar="STAGING", help="register staged mods from their stage markers")
    add_arguments(ap)
    args = ap.parse_args(argv)
    journal = Journal(args.journal_dir)
    if args.import_mods:
        added = journal.import_stage_markers(Path(args.import_mods))
        print_report([f"{k}: {journal.mods()[k].get('archive_name')}" for k in added], f"Registered {len(added)} mod(s):")
        return 0
    return run_cli(Path(args.game_dir).resolve(), args, journal)


if __name__ == "__main__":
    sys.exit(main())
