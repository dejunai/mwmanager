"""MWManager install ledger: a clean-install baseline, an append-only record of every change MWManager makes
to the game install, and the checks that make --rollback and redeploy possible.

Everything lives in MWManager's own folder (journal\\). Nothing is written into the game install and nothing
under Data Files\\MWSE\\mods.

  baseline\\baseline.json  a snapshot of the install as the user starts from it - a fresh install or a
                           playthrough that already has mods - with size, date, sha256 and a label per file:
                           prereq:mwse / prereq:mge_xe / prereq:mcp, game-exe, user-data, store-meta, start
                           (everything else). Rollback returns to this snapshot. Files are only compared with
                           their own snapshot entry, never with a store list; the one exception is the three
                           masters and three BSAs, which are checked against known store-independent hashes
                           and reported (not refused) when modified. A copy of Morrowind.ini is kept too.
  install.jsonl            one line per install path MWManager creates/replaces/changes/removes; the first line
                           for a path records where its original is (absent = new file). Hash-chained.
  runs.jsonl               one line per run: tool, version, the ledger range it wrote (with its hash), inputs.
  inputs\\<sha256>.json     content-addressed copies of the choices a run used (FOMOD answers, options, order).
  mods.json                per mod: archive name + sha256, Nexus ids, staged folder, answers - mod rollback
                           re-extracts from the archive; nothing else is kept for mod files.

Phase 1: --capture-baseline, --verify, --rollback --dry-run and mod-rollback --dry-run. The real rollback,
mod re-extraction and backup cleanup are implemented for tests but not offered on the command line yet.
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
import uuid
import zipfile
from pathlib import Path

MANAGER_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = MANAGER_DIR / "journal"
WIDTH = 115

PREREQ_RULES = (
    ("prereq:mcp", ("mcpatch/*", "morrowind code patch.exe", "mcp readme.txt", "mcp history.txt",
                    "morrowind.original.exe")),
    ("prereq:mge_xe", ("d3d8.dll", "d3d9.dll", "dinput8.dll", "mgexegui.exe", "mgegui.exe", "mgehost64.exe",
                       "mge xe readme.md", "mgexe.toml", "mge.ini", "mge3/*", "data files/distantland/*",
                       "data files/shaders/*", "data files/textures/mge/*", "data files/mwse/mods/mge xe options/*",
                       "data files/meshes/*_dist.nif", "data files/xe sky variations.esp")),
    ("prereq:mwse", ("mwse.dll", "mwse-update.exe", "mwse-version.txt", "lua51.dll", "csse.dll",
                     "libcrypto-1_1.dll", "libssl-1_1.dll", "newtonsoft.json.dll", "newtonsoft.json.xml",
                     "morrowind.pdb", "data files/mwse/core/*", "data files/mwse/lib/*", "data files/mwse/config/*",
                     "data files/mwse/.vscode/*", "data files/mwse/mods/readme.txt", "data files/textures/mwse/*",
                     "data files/meshes/mwse/*")),
    ("user-data", ("saves/*", "*.log", "mwse_minidump.dmp", "programflow.txt")),
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


def sha256_file(path: Path) -> str:
    return hashes(path)[0]


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
        self.ledger = self.root / "install.jsonl"
        self.runs = self.root / "runs.jsonl"
        self.mods_file = self.root / "mods.json"
        self.inputs = self.root / "inputs"

    # ---------------------------------------------------------------------------------------- baseline
    def has_baseline(self) -> bool:
        return self.baseline_file.is_file()

    def capture_baseline(self, game_dir, *, force: bool = False, hashdb: Path | None = None) -> dict:
        """Read-only on the install. Refuses to overwrite an existing baseline unless force=True."""
        game_dir = Path(game_dir).resolve()
        if self.has_baseline() and not force:
            raise FileExistsError(f"A baseline already exists: {self.baseline_file}")
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
        record = {"version": 1, "captured": now(), "game_dir_name": game_dir.name,
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
        entries, _problems = cls._read_jsonl(path)
        prev = entries[-1]["hash"] if entries else None
        entry = dict(entry, seq=(entries[-1]["seq"] + 1) if entries else 1, prev=prev)
        entry.pop("hash", None)
        entry["hash"] = hashlib.sha256(((prev or "") + canon(entry)).encode("utf-8")).hexdigest()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(canon(entry) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return entry

    def entries(self) -> tuple[list[dict], list[str]]:
        return self._read_jsonl(self.ledger)

    def first_original(self, rel: str, entries=None) -> dict | None:
        for entry in entries if entries is not None else self.entries()[0]:
            if key(entry["path"]) == key(rel) and entry.get("original"):
                return entry["original"]
        return None

    def record(self, run: str, rel: str, action: str, after_sha256: str | None, original: dict | None = None,
               tag: str | None = None, mod: str | None = None) -> dict:
        """One install change. original = {"kind": absent|gog|off|mwse-backups|mwmanager|disabled|baseline,
        "location": "game:<rel>" | "manager:<rel>" | "journal:<rel>", "sha256": ...}. Morrowind.ini edits use
        kind "baseline" with location "journal:baseline/Morrowind.ini".; only the first original for a path counts."""
        if action not in ("create", "replace", "modify", "remove", "move", "disable"):
            raise ValueError(f"unknown action {action}")
        if original and original.get("kind") != "absent" and not original.get("sha256"):
            raise ValueError("an original needs its sha256")
        if self.first_original(rel):
            original = None
        entry = {"ts": now(), "run": run, "path": rel.replace("\\", "/"), "action": action,
                 "after_sha256": after_sha256, "original": original}
        if tag:
            entry["tag"] = tag
        if mod:
            entry["mod"] = mod
        return self._append(self.ledger, entry)

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
    def expected_state(self) -> dict[str, str | None]:
        """{casefolded rel: sha256 or None (should not exist)} after the last recorded change."""
        state = {key(rel): info["sha256"] for rel, info in self.baseline()["files"].items()}
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
        expected = ({k: i["sha256"] for k, i in stats.items()} if against == "baseline" else self.expected_state())
        if against == "deployed":
            for original in originals.values():
                if original.get("location", "").startswith("game:"):
                    expected[key(original["location"].partition(":")[2])] = original["sha256"]
        journaled = {key(e["path"]) for e in entries}
        actual = {key(rel_of(game_dir, p)): p for p in walk_files(game_dir)}
        report = {"against": against, "changed": [], "missing": [], "extra": [], "unexplained": [],
                  "journal_problems": problems, "original_problems": [], "ignored": 0}
        for k, want in expected.items():
            if layers.get(k) in IGNORED_DIFF_LAYERS:
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

    # -------------------------------------------------------------------------------------- rollback
    def plan_rollback(self, game_dir) -> dict:
        """Steps that return the install to the baseline. Prerequisites and user data are kept."""
        game_dir = Path(game_dir).resolve()
        base = self.baseline()
        entries, problems = self.entries()
        originals = self._originals(entries)
        steps, refuse, used_backups = [], list(problems), []
        for k, original in sorted(originals.items(), key=lambda kv: -max(e["seq"] for e in entries
                                                                          if key(e["path"]) == kv[0])):
            rel = original["path"]
            if original["kind"] == "absent":
                steps.append({"op": "delete", "path": rel})
                continue
            current = game_dir / rel
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
            base_info = base["files"].get(rel) or next((i for r, i in base["files"].items() if key(r) == k), None)
            if base_info and base_info["sha256"] != original["sha256"]:
                refuse.append(f"{rel}: the recorded original does not match the baseline")
                continue
            steps.append({"op": "restore", "path": rel, "from": original["location"]})
            if key(original["location"].partition(":")[2]).startswith(BACKUP_PREFIXES) and \
                    original["location"].startswith("game:"):
                used_backups.append(original["location"])
        ini = game_dir / "Morrowind.ini"
        if "morrowind.ini" not in originals and self.baseline_ini.is_file() and (not ini.is_file() or sha256_file(ini) != sha256_file(self.baseline_ini)):
            steps.append({"op": "restore-ini", "path": "Morrowind.ini", "from": "journal:baseline/Morrowind.ini"})
        journaled = {key(e["path"]) for e in entries}
        unexplained = [p for p in self.verify(game_dir)["unexplained"] if key(p) not in journaled]
        return {"steps": steps, "refuse": refuse, "unexplained": unexplained,
                "cleanup_after_verify": sorted(set(used_backups))}

    def execute_rollback(self, game_dir, plan: dict) -> None:
        """Carry out a plan (tests only in phase 1). Restores are copies; backups stay until cleanup."""
        if plan["refuse"]:
            raise RuntimeError("rollback refused: " + "; ".join(plan["refuse"]))
        game_dir = Path(game_dir).resolve()
        for step in plan["steps"]:
            target = game_dir / step["path"]
            if step["op"] == "delete":
                if target.is_file():
                    target.unlink()
            elif step["op"] == "restore":
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self._resolve(game_dir, step["from"]), target)
            elif step["op"] == "restore-ini":
                shutil.copy2(self.baseline_ini, target)

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
    print(title)
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
        print_report(["No baseline exists yet. Run --capture-baseline on the install as you start from it (fresh or "
                      "already modded) before MWManager installs anything."], "Nothing to compare against:")
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
        if not args.dry_run:
            print_report(["Only --rollback --dry-run is available in this version; it reports what a rollback "
                          "would do and changes nothing."], "Rollback not run:")
            return 2
        plan = journal.plan_rollback(game_dir)
        lines = [f"{s['op']}: {s['path']}" + (f" (from {s['from']})" if s.get("from") else "") for s in plan["steps"]]
        lines += [f"REFUSE: {r}" for r in plan["refuse"]]
        lines += [f"Not explained by the journal (left alone, review): {p}" for p in plan["unexplained"]]
        lines += [f"After a verified rollback, remove the used backup {c}" for c in plan["cleanup_after_verify"]]
        print_report(lines or ["The install already matches the baseline."], "Rollback plan (dry run, nothing changed):")
        return 1 if plan["refuse"] else 0
    return 2


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--capture-baseline", action="store_true",
                    help="snapshot every file of the install as you start from it, fresh or already modded, so "
                         "--rollback can return to it (read-only on the install)")
    ap.add_argument("--force-baseline", action="store_true", help="replace an existing baseline")
    ap.add_argument("--verify", action="store_true", help="compare the install with the baseline or deployed state")
    ap.add_argument("--verify-against", choices=("baseline", "deployed"), default="baseline")
    ap.add_argument("--full-hash", action="store_true", help="hash every file instead of trusting size and date")
    ap.add_argument("--rollback", action="store_true", help="with --dry-run: report how to return to the baseline")
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
