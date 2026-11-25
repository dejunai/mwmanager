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
  install.jsonl            one line per install path MWManager creates/replaces/changes/removes (setup moves,
                           generated patches), and one pointer line per plugin deployment or removal: its file
                           list (manifests\\) with each file's size and time as deployed. The first line for a path
                           records where its original is (absent = new file). Hash-chained. Only a game original
                           a plugin replaced is copied into originals\\; anything else comes back from an archive.
  mods\\<mod>.jsonl         a plugin's own journal: only what MWManager changed in its files afterwards (asset
                           repairs, Lua edits), with originals. Retired when the plugin is rolled back, since
                           unpacking its archive again undoes the rest (Tidy's moves included).
  manifests\\<sha256>.json  the file list behind a pointer or a large batch.
  Rollback moves a deployed file that changed after deployment (not by MWManager) to
  Data Files\\Off\\<plugin>\\Altered Assets instead of deleting it; deploying the plugin again offers to
  restore, delete or skip those.
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
    # user-data is first so in-game MWSE suite settings are not caught by
    # data files/mwse/config/* (a prerequisite catch-all). The settings list is
    # explicit; a prereq's own config stays prereq:mwse.
    ("user-data", ("saves/*", "*.log", "warnings.txt", "mwse_minidump.dmp", "programflow.txt",
                     "data files/mwse/config/mwguard.json",
                     "data files/mwse/config/mwfloatfix.json",
                     "data files/mwse/config/mwfloatfix_blacklist.json",
                     "data files/mwse/config/mwhangwatch.json",
                     "data files/mwse/config/mwsaveguard.json",
                     "data files/mwse/config/mwsoundguard.json",
                     "data files/mwse/config/mwperformers.json",
                     "data files/mwse/config/mwcollide.json",
                     "data files/mwse/config/mwstranded.json",
                     "data files/mwse/config/mwcompanions.json",
                     "data files/mwse/config/mwcriminal.json",
                     "data files/mwse/config/mwcustomclasspersistence.json",
                     "data files/mwse/config/mweffectinspector.json",
                     "data files/mwse/config/mwexitguard.json",
                     "data files/mwse/config/mwmouseguard.json",
                     "data files/mwse/config/mwpluginpatches.json",
                     "data files/mwse/config/mwquicktoggle.json",
                     "data files/mwse/config/mwteleportguard.json",
                     "data files/mwse/config/mwwarnsuppression.json",
                     )),
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
    ("store-meta", ("goggame-*", "goglog.ini", "gog.ico", "support.ico", "unins000.*", "*.lnk",
                    "steam_api*.dll", "steam_appid.txt", ".egstore/*")),
    ("game-exe", ("morrowind.exe",)),
)
DEFAULT_LAYER = "start"
IGNORED_DIFF_LAYERS = ("user-data",)          # saves and logs change on every launch
# With MGE XE kept (a rollback to the prerequisites), what MGE XE writes for the current load order is left as it is:
# distant land is regenerated by the user, and mgeXE.toml holds their settings. A GOG-only rollback removes them.
MGE_REGENERATED = ("data files/distantland/*", "mgexe.toml")


def mge_regenerated(k: str) -> bool:
    return any(fnmatch.fnmatchcase(k, pat) for pat in MGE_REGENERATED)

# Runtime data written by our MWSE suite (and by --fix for those mods) under
# Data Files\MWSE\config. They are not journaled when the game writes them, so
# rollback would otherwise refuse. They are deleted like other generated files.
# Explicit names, not mwfloatfix_*.json: mwfloatfix_blacklist.json is user-data.
# mwcollide_data.json and mwstranded_data.json are journaled by --full and are
# not listed here.
GENERATED_RUNTIME = (
    "data files/mwse/config/mwfloatfix_data.json",
    "data files/mwse/config/mwfloatfix_rescue.json",
    "data files/mwse/config/mwsoundguard_data.json",
    "data files/mwse/config/mwsoundfix_data.json",
    "data files/mwse/config/mwinfoloop_cache.json",
)


def generated_runtime(k: str) -> bool:
    return any(fnmatch.fnmatchcase(k, pat) for pat in GENERATED_RUNTIME)


def wipe_folder(path) -> None:
    """Delete a folder and everything in it at once, with no per-file bookkeeping. (A robocopy mirror of an
    empty folder was measured at twice the time of a plain delete: 10 s against 4.7 s for 20,000 files.)"""
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
BACKUP_PREFIXES = ("data files/off/", "data files/mwse/backups/")


# ----------------------------------------------------------------------------------------------- helpers
def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def rel_of(game_dir: Path, path: Path) -> str:
    return path.relative_to(game_dir).as_posix()


def key(rel: str) -> str:
    return rel.replace("\\", "/").casefold()


def stat_marker(stat) -> str:
    """after_sha256 of a hardlink MWManager made: its size and modification time (a link shares them with its
    staged source), so recording 600,000 linked files doesn't read every byte of them again."""
    return f"stat:{stat.st_size}:{stat.st_mtime_ns}"


def altered_folder(label: str) -> str:
    """Data Files\\Off\\<plugin>\\Altered Assets: files a plugin deployed that changed afterwards, not by
    MWManager. They are kept when the plugin is rolled back and offered back when it is deployed again."""
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(str(label)).stem).strip(" .") or "plugin-archive"
    return f"Data Files/Off/{name}/Altered Assets"


def is_altered_asset(k: str) -> bool:
    parts = key(k).split("/")
    return len(parts) > 4 and parts[:2] == ["data files", "off"] and parts[3] == "altered assets"


def free_path(path: Path) -> Path:
    """path, or "name (2).ext" and so on when it is taken."""
    candidate, number = Path(path), 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        number += 1
    return candidate


def matches_after(path: Path, want: str) -> bool:
    """Whether the file at path is what the ledger recorded there (a sha256, or a hardlink's stat marker)."""
    if want.startswith("stat:"):
        try:
            return stat_marker(Path(path).stat()) == want
        except OSError:
            return False
    return sha256_file(path) == want


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
_LEDGER_CACHE: dict[str, dict] = {}   # ledger path -> its last read (entries, problems) and the file's size/age then
_MANIFESTS: dict[str, list] = {}       # manifest sha256 -> its [[path, after_sha256], ...]
_MERGED: dict[str, dict] = {}          # journal root -> the install ledger and every mod's ledger, read as one
BATCH_MIN = 1000                       # a record_many call this big keeps its new files in one manifest entry


def _ledger_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _ledger_stat(path: Path):
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


FOUND_AT_SETUP = "Found at Setup"      # Off/<this>: files no mod deployed (in Data Files when setup began)


class Journal:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.baseline_file = self.root / "baseline" / "baseline.json"
        self.baseline_ini = self.root / "baseline" / "Morrowind.ini"
        self.prerequisites_file = self.root / "prerequisites" / "prerequisites.json"
        self.ledger = self.root / "install.jsonl"
        self.manifests = self.root / "manifests"
        self.mod_ledgers = self.root / "mods"      # one ledger per mod: what its deployments changed
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
                          progress=None, mod: str | None = None, label: str | None = None) -> dict:
        """Record a completed deployment in the install ledger as one pointer per plugin (`mod`/`label`, or each
        operation's own): its file list, each file as deployed (size and time). A file it replaced is copied into
        the journal only when it is a game original (baseline or prerequisites): a file another mod deployed
        comes back from that mod's archive instead. Later changes MWManager makes to these files (asset repairs,
        Lua edits) go to the plugin's own journal (journal\\mods)."""
        game_dir = Path(game_dir).resolve()
        if not self.has_baseline():
            raise RuntimeError("deployment requires a game baseline so rollback can remove it")
        run = self.start_run(tool, inputs)
        checkpoint = self.checkpoint_state()
        records = []
        total = len(operations)
        last_report = time.monotonic()
        for index, operation in enumerate(operations, 1):
            # the transaction already checked these paths (resolved, inside the game); resolving each again
            # opened every file a second time
            target = Path(os.path.abspath(operation["target"]))
            rel = rel_of(game_dir, target)
            backup = Path(operation["backup"]).resolve() if operation.get("backup") else None
            original = (self.preserve_file(backup) if backup and key(rel) in checkpoint
                        else {"kind": "absent"})
            try:
                stat = target.stat()
            except OSError:
                stat = None
            if stat is None or not target.is_file():
                raise FileNotFoundError(f"deployed file disappeared before journaling: {target}")
            if progress and (index == 1 or index == total or time.monotonic() - last_report >= 20):
                progress(f"MWMANAGER-STAGE: Recording deployment: {index:,} of {total:,} files")
                last_report = time.monotonic()
            # the file as deployed: a later change (by the user) shows as a different size or time
            deployed = stat_marker(stat)
            records.append({"run": run["run"], "path": rel,
                            "action": "replace" if backup else "create",
                            "after_sha256": deployed, "original": original, "tag": tag,
                            "owner": operation.get("mod") or mod, "label": operation.get("label") or label})
        for move in moves or []:
            source, target = Path(move["source"]).resolve(), Path(move["target"]).resolve()
            original = self.preserve_file(target)
            records.append({"run": run["run"], "path": rel_of(game_dir, source), "action": "move",
                            "after_sha256": None, "original": original, "tag": tag})
            records.append({"run": run["run"], "path": rel_of(game_dir, target), "action": "create",
                            "after_sha256": original.get("sha256"), "original": {"kind": "absent"},
                            "tag": tag})
        self.record_many(records, batch_min=1)
        return self.finish_run(run)

    def baseline(self) -> dict:
        return json.loads(self.baseline_file.read_text(encoding="utf-8"))

    # ----------------------------------------------------------------------------------------- ledger
    @staticmethod
    def _read_jsonl(path: Path) -> tuple[list[dict], list[str]]:
        entries, problems, prev = [], [], None
        if not path.is_file():
            return entries, problems
        # The install ledger reaches hundreds of MB; reading and checking it took ~10 s, and every recorded move
        # did that four times. Reuse the last read while the file is the size and age it was then.
        cache_key = _ledger_key(path)
        stat = _ledger_stat(path)
        cached = _LEDGER_CACHE.get(cache_key)
        if cached is not None and cached["stat"] == stat:
            return list(cached["entries"]), list(cached["problems"])
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
        _LEDGER_CACHE[cache_key] = {"stat": stat, "entries": entries, "problems": problems, "originals": None,
                                    "expanded": None}
        return list(entries), list(problems)

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
        cached = _LEDGER_CACHE.get(_ledger_key(path))
        if cached is not None and cached["stat"] != _ledger_stat(path):
            cached = None                       # changed since it was read: read it fresh next time
        with open(path, "a", encoding="utf-8") as stream:
            stream.writelines(canon(entry) + "\n" for entry in prepared)
            stream.flush()
            os.fsync(stream.fileno())
        if cached is None:
            _LEDGER_CACHE.pop(_ledger_key(path), None)
        else:                                   # what was read, plus what was just written
            cached["entries"].extend(prepared)
            cached["stat"] = _ledger_stat(path)
            if any(e.get("manifest") for e in prepared):
                cached["originals"] = cached["expanded"] = None
            else:
                if cached["originals"] is not None:
                    cached["originals"].update(key(e["path"]) for e in prepared if e.get("original"))
                if cached.get("expanded") is not None:
                    cached["expanded"][0].extend(prepared)
        return prepared

    def mod_ledger(self, mod: str) -> Path:
        return self.mod_ledgers / (re.sub(r"[^A-Za-z0-9._-]+", "_", mod).strip("._")[:100] + ".jsonl")

    def _ledger_files(self) -> list[Path]:
        mods = sorted(self.mod_ledgers.glob("*.jsonl")) if self.mod_ledgers.is_dir() else []
        return [self.ledger] + mods

    def _signature(self, files) -> tuple:
        return tuple((str(f), _ledger_stat(f)) for f in files)

    def entries(self) -> tuple[list[dict], list[str]]:
        """Every recorded change, one entry per path, oldest first: the install ledger and each mod's own ledger
        merged by time; a batch entry (one deployment's files) is expanded from its manifest."""
        files = self._ledger_files()
        signature = self._signature(files)
        merged = _MERGED.get(_ledger_key(self.root))
        if merged is not None and merged["signature"] == signature:
            return list(merged["entries"]), list(merged["problems"])
        parts, problems = [], []
        for path in files:
            entries, file_problems = self._expanded(path)
            parts.append(entries)
            problems += file_problems if path == self.ledger else [f"mods/{path.name}: {p}" for p in file_problems]
        entries = list(parts[0]) if len(parts) == 1 else sorted((e for part in parts for e in part), key=lambda e: e["ts"])
        _MERGED[_ledger_key(self.root)] = {"signature": signature, "entries": entries, "problems": problems,
                                           "originals": None, "owners": None}
        return list(entries), list(problems)

    def _expanded(self, path: Path) -> tuple[list[dict], list[str]]:
        raw, problems = self._read_jsonl(path)
        if not any(e.get("manifest") for e in raw):
            return raw, problems
        cached = _LEDGER_CACHE.get(_ledger_key(path))
        if cached is not None and cached.get("expanded") is not None:
            return cached["expanded"]
        entries, problems = [], list(problems)
        for entry in raw:
            if entry.get("manifest"):
                entries.extend(self._expand(entry, problems))
            else:
                entries.append(entry)
        if cached is not None:
            cached["expanded"] = (entries, problems)
        return entries, problems

    def _expand(self, entry: dict, problems: list[str]) -> list[dict]:
        files = self._manifest(entry, problems)
        base = {k: v for k, v in entry.items() if k not in ("manifest", "count", "after_sha256", "each_original")}
        original = entry.get("each_original", {"kind": "absent"} if entry["action"] == "create" else None)
        return [dict(base, path=path, after_sha256=after, original=original) for path, after in files]

    def _manifest(self, entry: dict, problems: list[str]) -> list:
        digest = entry["manifest"]
        if digest in _MANIFESTS:
            return _MANIFESTS[digest]
        path = self.manifests / f"{digest}.json"
        try:
            data = path.read_bytes()
        except OSError:
            problems.append(f"ledger entry {entry.get('seq')}: its file list is missing ({path.name})")
            return []
        if hashlib.sha256(data).hexdigest() != digest:
            problems.append(f"ledger entry {entry.get('seq')}: its file list does not match its hash ({path.name})")
            return []
        _MANIFESTS[digest] = json.loads(data.decode("utf-8"))
        return _MANIFESTS[digest]

    def _original_paths(self) -> set[str]:
        """Paths whose first original is already recorded (a copy: the caller may add to it)."""
        entries, _problems = self.entries()
        merged = _MERGED[_ledger_key(self.root)]
        if merged["originals"] is None:
            merged["originals"] = {key(entry["path"]) for entry in entries if entry.get("original")}
        return set(merged["originals"])

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

    def record_many(self, records: list[dict], batch_min: int | None = None) -> list[dict]:
        """One install change. original = {"kind": absent|gog|off|mwse-backups|mwmanager|disabled|baseline,
        "location": "game:<rel>" | "manager:<rel>" | "journal:<rel>", "sha256": ...}. Morrowind.ini edits use
        kind "baseline" with location "journal:baseline/Morrowind.ini".; only the first original for a path counts."""
        original_paths = self._original_paths()
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
            for field in ("mod", "owner", "label"):
                if record.get(field):
                    entry[field] = record[field]
            entries.append(entry)
        merged = _MERGED.get(_ledger_key(self.root))      # fresh: _original_paths just read it
        if merged is not None and any(not e.get("mod") and not e.get("owner") for e in entries):
            owners = self._owners(merged)
            for entry in entries:               # MWManager changed a plugin's file: the plugin's own journal
                if not entry.get("mod") and not entry.get("owner") and not str(entry.get("tag") or "").endswith("removal") \
                        and key(entry["path"]) in owners:
                    entry["mod"] = owners[key(entry["path"])]
        by_ledger: dict[Path, list[dict]] = {}
        for entry in entries:
            by_ledger.setdefault(self.mod_ledger(entry["mod"]) if entry.get("mod") else self.ledger, []).append(entry)
        prepared = []
        for path, group in by_ledger.items():
            prepared += self._append_many(path, self._batch(group, BATCH_MIN if batch_min is None else batch_min))
        if merged is not None:
            if any(e.get("manifest") for e in prepared):
                _MERGED.pop(_ledger_key(self.root), None)
            else:                               # what was read, plus what was just written
                merged["entries"].extend(prepared)
                merged["signature"] = self._signature(self._ledger_files())
                if merged["originals"] is not None:
                    merged["originals"].update(key(e["path"]) for e in prepared if e.get("original"))
                merged["owners"] = None
        return prepared

    def _owners(self, merged: dict) -> dict[str, str]:
        """{path: the plugin whose deployment put it there}, from the merged ledger."""
        if merged.get("owners") is None:
            owners = {}
            for entry in merged["entries"]:
                if entry.get("owner"):
                    if entry["action"] == "remove":
                        owners.pop(key(entry["path"]), None)
                    else:
                        owners[key(entry["path"])] = entry["owner"]
            merged["owners"] = owners
        return merged["owners"]

    def retire_mod(self, mod: str | None) -> None:
        """A plugin rolled back to its archive: its journal (repairs, Lua edits) no longer applies; kept in history."""
        if not mod:
            return
        path = self.mod_ledger(mod)
        if path.is_file():
            target = self.root / "history" / f"mod-{_dt.datetime.now():%Y%m%d-%H%M%S}-{path.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, free_path(target))
        _MERGED.pop(_ledger_key(self.root), None)

    def _batch(self, entries: list[dict], batch_min: int) -> list[dict]:
        """Many files from one run (a 600,000-file deployment) become one entry pointing at a manifest of
        [path, after_sha256], so the ledger doesn't grow by a line per file every deployment."""
        groups: dict[str, list[dict]] = {}
        for entry in entries:
            if entry["action"] in ("create", "replace", "remove") and entry["original"] in (None, {"kind": "absent"}):
                groups.setdefault(canon([entry["run"], entry.get("tag"), entry.get("mod"), entry.get("owner"),
                                         entry.get("label"), entry["action"], entry["original"]]), []).append(entry)
        out = entries
        for new in groups.values():
            if len(new) >= batch_min:
                out = self._batch_group(out, new)
        return out

    def _batch_group(self, entries: list[dict], new: list[dict]) -> list[dict]:
        data = canon([[e["path"], e["after_sha256"]] for e in new]).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        self.manifests.mkdir(parents=True, exist_ok=True)
        target = self.manifests / f"{digest}.json"
        if not target.is_file():
            temporary = target.with_suffix(".tmp")
            with open(temporary, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        batch = {"ts": new[0]["ts"], "run": new[0]["run"], "path": f"manifests/{digest}.json",
                 "action": new[0]["action"], "manifest": digest, "count": len(new), "original": None,
                 "each_original": new[0]["original"]}
        for field in ("tag", "mod", "owner", "label"):
            if new[0].get(field):
                batch[field] = new[0][field]
        chosen = {id(e) for e in new}
        out, placed = [], False
        for entry in entries:
            if id(entry) not in chosen:
                out.append(entry)
            elif not placed:
                out.append(batch)
                placed = True
        return out

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
        destinations = [(path, old.parent / FOUND_AT_SETUP / path.relative_to(old)) for path in files]
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
        """Consolidate the obsolete second shipped-asset owner under Off/Found at Setup."""
        game_dir = Path(game_dir).resolve()
        old = game_dir / "Data Files" / "Off" / "Official Assets"
        if not old.is_dir():
            return 0
        files = [path for path in old.rglob("*") if path.is_file()]
        owner = old.parent / FOUND_AT_SETUP / "Official Assets"
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

    def migrate_original_install(self, game_dir, unused_records=()) -> int:
        """Off/Original Install held everything these tools took out of Data Files that no plugin folder took:
        files a staged package deployed went there too. Each one a package deployed (a hard link into staging)
        now goes to that package's Off/<plugin>/<feature>/ and leaves the package's deploy path; the rest go
        to Off/Found at Setup/. The unused-asset records are updated so --restore-unused still finds them."""
        game_dir = Path(game_dir).resolve()
        data_dir = game_dir / "Data Files"
        old = data_dir / "Off" / "Original Install"
        if not old.is_dir():
            return 0
        from mwdialog_datatree import hard_link_names
        data_abs = os.path.normcase(str(data_dir))
        moves = []
        for path in sorted(p for p in old.rglob("*") if p.is_file()):
            feature, *rel_parts = path.relative_to(old).parts
            staged = [Path(p) for p in hard_link_names(str(path))
                      if not os.path.normcase(p).startswith(data_abs + os.sep)
                      and not re.search(r"[\\/]off[\\/]", p, re.I)]
            target, deploy_copy = old.parent / FOUND_AT_SETUP / path.relative_to(old), None
            if staged and rel_parts:
                package = staged[0]
                for _ in rel_parts:
                    package = package.parent
                plugin = next((p.stem for p in sorted(package.iterdir()) if p.suffix.casefold() in (".esp", ".esm")),
                              package.name) if package.is_dir() else package.name
                target, deploy_copy = package / "Off" / plugin / feature / Path(*rel_parts), staged[0]
            if target.exists():
                raise FileExistsError(f"cannot move {path} over {target}")
            moves.append((path, target, deploy_copy))
        run = self.start_run("mwmanager_journal.py", {"operation": "move Off/Original Install"})
        renamed = {}
        for source, target, deploy_copy in moves:
            digest = sha256_file(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            if deploy_copy is not None and deploy_copy.is_file():
                deploy_copy.unlink()            # kept in the package's Off now: not deployed again
            self.record(run["run"], rel_of(game_dir, source), "remove", None, None, tag="asset-repair-backup")
            if target.is_relative_to(game_dir):
                self.record(run["run"], rel_of(game_dir, target), "create", digest, {"kind": "absent"},
                            tag="asset-repair-backup")
            renamed[os.path.normcase(str(source))] = str(target)
        for folder, _dirs, _names in os.walk(old, topdown=False):
            Path(folder).rmdir()
        for record in unused_records:
            record = Path(record)
            if not record.is_file():
                continue
            lines = record.read_text(encoding="utf-8").splitlines()
            fixed = []
            for line in lines:
                parts = line.split("\t")
                if len(parts) >= 3 and os.path.normcase(parts[2]) in renamed:
                    parts[2] = renamed[os.path.normcase(parts[2])]
                fixed.append("\t".join(parts))
            record.write_text("\n".join(fixed) + ("\n" if fixed else ""), encoding="utf-8")
        self.finish_run(run)
        return len(moves)

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
        entries, _ = self._read_jsonl(self.ledger)
        return {"run": _dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6], "tool": tool,
                "tool_version": version, "inputs": stored, "first_seq": (entries[-1]["seq"] + 1) if entries else 1}

    def finish_run(self, run: dict) -> dict:
        mine = [e for path in self._ledger_files() for e in self._read_jsonl(path)[0] if e["run"] == run["run"]]
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

    def mod_key(self, package: Path | str | None = None, archive: Path | str | None = None) -> str | None:
        """The key a mod is journaled under: its archive's sha256 from the staged package's marker, else the
        archive's name; registered in mods.json so the mod can be rolled back by unpacking its archive again."""
        marker = {}
        if package:
            try:
                marker = json.loads((Path(package) / ".mwmanager-stage.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                marker = {}
        archive = archive or marker.get("archive_path") or marker.get("archive")
        if marker.get("sha256"):
            mod = f"sha256-{str(marker['sha256']).lower()[:16]}"
        elif archive:
            mod = "archive-" + re.sub(r"[^A-Za-z0-9._-]+", "_", Path(archive).stem)[:80]
        elif package:
            mod = "package-" + re.sub(r"[^A-Za-z0-9._-]+", "_", Path(package).name)[:80]
        else:
            return None
        known = self.mods().get(mod, {})
        fields = {"archive": str(archive) if archive else None, "archive_name": Path(archive).name if archive else None,
                  "archive_sha256": str(marker["sha256"]).lower() if marker.get("sha256") else None,
                  "staged": str(package) if package else None}
        if any(v is not None and known.get(k) != v for k, v in fields.items()):
            self.upsert_mod(mod, **fields)
        return mod

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
        keep_mge = against == "prerequisites"
        for k, want in expected.items():
            if layers.get(k) in IGNORED_DIFF_LAYERS or (own_ini and k == "morrowind.ini")                     or (keep_mge and mge_regenerated(k)):
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
            if want.startswith("stat:"):
                if not matches_after(path, want):
                    report["changed"].append(rel_of(game_dir, path))
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
            if keep_mge and mge_regenerated(k):
                report["ignored"] += 1
                continue
            if is_altered_asset(k):
                report["ignored"] += 1
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
        stamp = f"{_dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        if self.mod_ledgers.is_dir():
            (self.root / "history").mkdir(parents=True, exist_ok=True)
            os.replace(self.mod_ledgers, self.root / "history" / f"mods-{stamp}")
        _MERGED.pop(_ledger_key(self.root), None)
        if not self.ledger.is_file():
            return None
        target = self.root / "history" / f"install-{stamp}.jsonl"
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
        wipe_folder(staging)
        return entries

    def fast_wipe(self, game_dir, *, preserve_prerequisites: bool = True) -> dict:
        """Dev only (--fast-wipe): back to the checkpoint without hashing or checking a single file. A folder
        holding nothing the checkpoint keeps is deleted whole (wipe_folder); elsewhere every file the checkpoint
        doesn't list goes; the recorded originals (and Morrowind.ini) are copied back. Saves and logs stay, and
        with MGE XE kept its distant land and mgeXE.toml. For a test install known to hold no hand changes."""
        game_dir = Path(game_dir).resolve()
        keep = set(self.checkpoint_state(preserve_prerequisites))

        def protected(k):
            return classify(k) in IGNORED_DIFF_LAYERS or (preserve_prerequisites and mge_regenerated(k))

        kept_dirs = {"/".join(k.split("/")[:i]) for k in keep for i in range(1, k.count("/") + 1)}
        wiped = removed = 0
        for base, dirs, files in os.walk(game_dir):
            base_path = Path(base)
            for name in list(dirs):
                k = key(rel_of(game_dir, base_path / name))
                if k in kept_dirs or protected(k + "/x"):
                    continue
                wipe_folder(base_path / name)
                dirs.remove(name)
                wiped += 1
            for name in files:
                path = base_path / name
                k = key(rel_of(game_dir, path))
                if k not in keep and not protected(k):
                    path.unlink()
                    removed += 1
        restored = 0
        for k, original in self._originals(self.entries()[0]).items():
            if k == "morrowind.ini" or k not in keep or original.get("kind") == "absent":
                continue
            try:
                source = self._resolve(game_dir, original["location"])
            except (KeyError, ValueError):
                continue
            if source.is_file():
                target = game_dir / original["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                restored += 1
        if self.baseline_ini.is_file():
            shutil.copy2(self.baseline_ini, game_dir / "Morrowind.ini")
        return {"folders_wiped": wiped, "files_removed": removed, "originals_restored": restored}

    # -------------------------------------------------------------------------------------- rollback
    def plan_rollback(self, game_dir, *, preserve_prerequisites: bool = True) -> dict:
        """Return to the prerequisite checkpoint by default; optionally add a store-only second stage."""
        game_dir = Path(game_dir).resolve()
        base = self.baseline()
        entries, problems = self.entries()
        originals = self._originals(entries)
        steps, prerequisite_steps, refuse, used_backups = [], [], list(problems), []
        rollback_checkpoint = self.checkpoint_state(True)
        last_seq, last_after, label_of = {}, {}, {}  # one pass: a per-path scan of the ledger was O(n^2)
        for index, e in enumerate(entries):         # oldest first: the last change to a path wins
            k = key(e["path"])
            last_seq[k], last_after[k] = index, e.get("after_sha256")
            if e.get("label"):
                label_of[k] = e["label"]

        def delete_step(rel, k):
            step = {"op": "delete", "path": rel}
            want, label = last_after.get(k), label_of.get(k)
            # shortcut: only files recorded by size and time are checked (older ledgers hold sha256 for every
            # file, and hashing a whole install to roll it back is what made it slow); redeploy to upgrade
            if label and want and want.startswith("stat:") and (game_dir / rel).is_file() \
                    and not matches_after(game_dir / rel, want):
                inner = rel[len("Data Files/"):] if key(rel).startswith("data files/") else rel
                step["altered_to"] = f"{altered_folder(label)}/{inner}"
            return step

        for k, original in sorted(originals.items(), key=lambda kv: -last_seq[kv[0]]):
            if k == "morrowind.ini":
                continue                         # the whole baseline INI is authoritative, including old ledgers
            if preserve_prerequisites and mge_regenerated(k):
                continue                         # MGE XE stays: its distant land and settings are left as they are
            rel = original["path"]
            if k not in rollback_checkpoint:
                # A later repair may have preserved the bytes of a file generated after
                # the checkpoint. Checkpoint ownership is authoritative: if the file
                # did not exist at the prerequisite point, it must be deleted, not
                # reconstructed from its later first journaled state.
                steps.append(delete_step(rel, k))
                continue
            if original["kind"] == "absent":
                steps.append(delete_step(rel, k))
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
        unexplained = []
        for rel in self.verify(game_dir, against=against)["unexplained"]:
            k = key(rel)
            if k in journaled or k in prereq_paths or k in planned_paths:
                continue
            if generated_runtime(k) and k not in rollback_checkpoint:
                steps.append(delete_step(rel, k))
                planned_paths.add(k)
            else:
                unexplained.append(rel)
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
                if target.is_file() and step.get("altered_to"):
                    altered = free_path(game_dir / step["altered_to"])
                    altered.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, altered)
                elif target.is_file():
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
    if args.rollback and getattr(args, "fast_wipe", False):
        what = "the store baseline" if args.gog_only else "the prerequisites"
        if args.dry_run:
            print_report([f"--fast-wipe: back to {what} without hashing or checking files (dev only)."],
                         "Rollback plan (dry run, nothing changed):")
            return 0
        counts = journal.fast_wipe(game_dir, preserve_prerequisites=not args.gog_only)
        return _finish_rollback(journal, game_dir, args, None, [
            f"Fast wipe to {what} (dev only, not verified): {counts['folders_wiped']} folder(s) wiped whole, "
            f"{counts['files_removed']} file(s) removed, {counts['originals_restored']} original(s) put back."])
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
        return _finish_rollback(journal, game_dir, args, plan, [f"Verified against {against} with full hashes."])
    return 2


def _finish_rollback(journal, game_dir, args, plan, summary) -> int:
    """After the game files are back: staging (--no-staging), used backups, the ledger, and setup start over."""
    try:
        cleared_staging = journal.clear_staging() if args.no_staging else 0
    except (OSError, ValueError) as exc:
        print_report([str(exc)], "Game rollback verified, but staging was not cleared:")
        return 1
    removed = journal.cleanup_used_backups(game_dir, plan, apply=True) if plan else []
    archived = journal.archive_ledger()
    manager_dir = journal.root.parent
    # Decisions and derived order state belong to the install that was just rolled back: keep them with its ledger.
    stamp = archived.stem if archived else f"install-{_dt.datetime.now():%Y%m%d-%H%M%S}"
    for name in ("disabled_plugins", "mwmanager_disabled.txt", "mwmanager_conflict_answers.json",
                 "mwmanager_cell_winners.txt", "mwmanager_rules.txt", "mwmanager_sort_order.txt",
                 "mwmanager_added_masters.json", "mwmanager_merged.txt", "mwmanager_merge_rebuild.txt"):
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
    if args.no_staging:
        summary.append(f"Cleared {cleared_staging} staging entr{'y' if cleared_staging == 1 else 'ies'}.")
    summary += [f"Removed {len(removed)} used backup file(s).",
                f"Archived the completed ledger at {archived}."]
    print_report(summary, "Rollback complete:")
    return 0


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
    ap.add_argument("--fast-wipe", action="store_true",
                    help="with --rollback, dev only: delete whole folders at once and "
                         "skip hashing and verifying; for a test install known to hold no hand changes")
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
