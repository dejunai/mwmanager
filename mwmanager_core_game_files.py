"""Launch-time check of the six shipped Morrowind masters and BSA archives.

Morrowind.esm, Tribunal.esm, Bloodmoon.esm, Morrowind.bsa, Tribunal.bsa and
Bloodmoon.bsa in Data Files are compared with known-good SHA-256 hashes (the
user's recorded baseline and, when verified, the official GOG/Steam values in
mwmanager_core_hashes.json). Unchanged files stay silent. Altered or missing
files get a CLI warning or a non-blocking GUI modal that strongly recommends
restoring them; Play is never blocked.

Re-hashing is skipped when the recorded size and mtime are unchanged, so the
large BSA files are not read on every launch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

DETAIL_PREFIX = "MWMANAGER-DETAIL: "
INI_SECTION = "Integrity"
INI_DEFAULTS = {"check_core_game_files": True}
CORE_GAME_FILES = (
    "Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm",
    "Morrowind.bsa", "Tribunal.bsa", "Bloodmoon.bsa",
)
STATE_NAME = "core_game_file_state.json"
MUTE_NAME = "core_game_file_mute.json"
SUMS_NAMES = ("SHA256SUMS", "SHA256SUMS.txt", "core_game_files.sha256")
CHECKED_ENV = "MWMANAGER_CORE_FILES_CHECKED"


def default_manager_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _manager_dir(manager_dir=None):
    return os.path.abspath(manager_dir or default_manager_dir())


def ini_path(manager_dir=None):
    return os.path.join(_manager_dir(manager_dir), "mwmanager.ini")


def output_dir(manager_dir=None):
    return os.path.join(_manager_dir(manager_dir), "outputs")


def state_path(manager_dir=None):
    return os.path.join(output_dir(manager_dir), STATE_NAME)


def mute_path(manager_dir=None):
    return os.path.join(output_dir(manager_dir), MUTE_NAME)


def detail_log_path(manager_dir=None):
    return os.path.join(output_dir(manager_dir), "mwmanager_status_detail.log")


def ini_flag(name="check_core_game_files", manager_dir=None, default=None):
    """Read an [Integrity] boolean from mwmanager.ini. Missing keys default to true."""
    import configparser
    if name not in INI_DEFAULTS:
        raise KeyError(name)
    fallback = INI_DEFAULTS[name] if default is None else default
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(ini_path(manager_dir), encoding="utf-8")
        return parser.getboolean(INI_SECTION, name, fallback=fallback)
    except (OSError, ValueError, configparser.Error):
        return fallback


def write_ini_flags(flags, manager_dir=None):
    import configparser
    path = ini_path(manager_dir)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    if not parser.has_section(INI_SECTION):
        parser.add_section(INI_SECTION)
    for key, fallback in INI_DEFAULTS.items():
        value = flags[key] if key in flags else ini_flag(key, manager_dir, fallback)
        parser.set(INI_SECTION, key, "true" if value else "false")
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as stream:
        parser.write(stream)
    os.replace(tmp, path)
    return path


def append_detail_log(lines, manager_dir=None):
    if not lines:
        return
    path = detail_log_path(manager_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            for line in lines:
                text = str(line).rstrip()
                if text.startswith(DETAIL_PREFIX):
                    text = text[len(DETAIL_PREFIX):].lstrip()
                stream.write(text + "\n")
    except OSError:
        pass


def official_hashes():
    """Verified original SHA-256 values from mwmanager_core_hashes.json. Never invented here."""
    import mwmanager_prereqs
    table = {}
    for name, hashes in mwmanager_prereqs.CORE_FILE_HASHES.items():
        if name in CORE_GAME_FILES:
            table[name] = tuple(str(item).casefold() for item in hashes)
    return table


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().casefold()


def _find(folder, name):
    try:
        return next((os.path.join(folder, item) for item in os.listdir(folder)
                     if item.casefold() == name.casefold()
                     and os.path.isfile(os.path.join(folder, item))), None)
    except OSError:
        return None


def _load_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(tmp, path)


def load_state(manager_dir=None):
    return _load_json(state_path(manager_dir))


def save_state(state, manager_dir=None):
    _save_json(state_path(manager_dir), state)
    return state


def load_mute(manager_dir=None):
    data = _load_json(mute_path(manager_dir))
    files = data.get("files") if isinstance(data.get("files"), dict) else data
    return files if isinstance(files, dict) else {}


def save_mute(files, manager_dir=None):
    _save_json(mute_path(manager_dir), {"files": files})
    return files


def _stat_fingerprint(path):
    info = os.stat(path)
    mtime = getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))
    return {"size": int(info.st_size), "mtime_ns": int(mtime)}


def parse_sha256sums(text):
    """Return {canonical core file name: sha256} from a SHA256SUMS body. Other paths are ignored."""
    aliases = {name.casefold(): name for name in CORE_GAME_FILES}
    found = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
        if not match:
            continue
        digest, label = match.group(1).casefold(), match.group(2).strip().strip('"')
        base = os.path.basename(label.replace("\\", "/")).casefold()
        name = aliases.get(base)
        if name:
            found[name] = digest
    return found


def load_sums_hashes(manager_dir=None):
    """Hashes for the six core files from a SHA256SUMS file in outputs, if one is present."""
    folder = output_dir(manager_dir)
    found = {}
    for name in SUMS_NAMES:
        path = os.path.join(folder, name)
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        found.update(parse_sha256sums(text))
    return found


def known_good_hashes(manager_dir=None, known=None):
    """Official hashes plus any user baseline / SHA256SUMS values already recorded."""
    table = {name: set() for name in CORE_GAME_FILES}
    official = known if known is not None else official_hashes()
    for name, hashes in official.items():
        if name in table:
            table[name].update(hashes)
    state = load_state(manager_dir)
    files = state.get("files") if isinstance(state.get("files"), dict) else {}
    for name, entry in files.items():
        if name in table and isinstance(entry, dict) and entry.get("baseline"):
            digest = str(entry.get("sha256") or "").casefold()
            if len(digest) == 64:
                table[name].add(digest)
    for name, digest in load_sums_hashes(manager_dir).items():
        table[name].add(digest)
    return table


def recovery_advice(game_dir):
    try:
        from preinstall import recovery_text
        return recovery_text(Path(game_dir))
    except Exception:
        return ("Restore the files from the original GOTY media, or use the store's "
                "verify/repair option.")


def _mute_key(problem):
    if problem["status"] == "missing":
        return {"state": "missing", "sha256": None}
    return {"state": "altered", "sha256": problem["actual"]}


def is_muted(problem, muted):
    entry = muted.get(problem["name"])
    if not isinstance(entry, dict):
        return False
    wanted = _mute_key(problem)
    return entry.get("state") == wanted["state"] and (
        (entry.get("sha256") or None) == (wanted["sha256"] or None)
    )


def mute_problems(problems, manager_dir=None):
    """Remember the current altered/missing hash per file. A different hash warns again."""
    muted = load_mute(manager_dir)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for problem in problems:
        muted[problem["name"]] = {**_mute_key(problem), "muted_at": now}
    save_mute(muted, manager_dir)
    return muted


def hash_core_file(path, cached=None, hasher=None):
    """Return (sha256, from_cache). Re-hash only when size or mtime changed."""
    fingerprint = _stat_fingerprint(path)
    if (isinstance(cached, dict)
            and cached.get("size") == fingerprint["size"]
            and cached.get("mtime_ns") == fingerprint["mtime_ns"]
            and len(str(cached.get("sha256") or "")) == 64):
        return str(cached["sha256"]).casefold(), True, fingerprint
    digest = (hasher or sha256_file)(path)
    return digest.casefold(), False, fingerprint


def inspect_core_game_files(game_dir, *, manager_dir=None, known=None, hasher=None):
    """Compare the six shipped files with known-good hashes. Never writes to the game folder."""
    game_dir = os.path.abspath(game_dir)
    data_dir = os.path.join(game_dir, "Data Files")
    expected = known_good_hashes(manager_dir, known=known)
    state = load_state(manager_dir)
    cached_files = state.get("files") if isinstance(state.get("files"), dict) else {}
    if os.path.normcase(str(state.get("game_dir") or "")) != os.path.normcase(game_dir):
        cached_files = {}
    results = []
    problems = []
    new_files = {}
    for name in CORE_GAME_FILES:
        path = _find(data_dir, name)
        entry = {"name": name, "path": path}
        if path is None:
            entry.update(status="missing", actual=None, expected=sorted(expected.get(name) or []),
                         cached=False)
            results.append(entry)
            problems.append(entry)
            continue
        digest, from_cache, fingerprint = hash_core_file(path, cached_files.get(name), hasher=hasher)
        good = set(expected.get(name) or [])
        ok = digest in good
        entry.update(status="ok" if ok else "altered", actual=digest, expected=sorted(good),
                     cached=from_cache, size=fingerprint["size"], mtime_ns=fingerprint["mtime_ns"])
        results.append(entry)
        if not ok:
            problems.append(entry)
        new_files[name] = {
            "sha256": digest,
            "size": fingerprint["size"],
            "mtime_ns": fingerprint["mtime_ns"],
            "baseline": bool(ok or (isinstance(cached_files.get(name), dict)
                                    and cached_files[name].get("baseline")
                                    and cached_files[name].get("sha256") == digest)),
        }
        if ok:
            new_files[name]["baseline"] = True
    state = {
        "game_dir": os.path.normcase(game_dir),
        "files": new_files,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        save_state(state, manager_dir)
    except OSError:
        pass
    return {"results": results, "problems": problems, "state": state, "game_dir": game_dir}


def short_warning(problems):
    names = ", ".join(item["name"] for item in problems)
    if any(item["status"] == "missing" for item in problems) and any(
            item["status"] == "altered" for item in problems):
        kind = "missing or altered"
    elif all(item["status"] == "missing" for item in problems):
        kind = "missing"
    else:
        kind = "altered"
    return (f"WARNING: shipped Morrowind core file(s) {kind}: {names}. "
            "Restore them with your store's verify/repair before relying on this install. "
            "Play will continue.")


def detail_lines(report, muted_names=None):
    muted_names = set(muted_names or [])
    lines = [f"core game file check: {len(report['results'])} shipped ESM/BSA file(s)"]
    for item in report["results"]:
        expected = ", ".join(item.get("expected") or []) or "(no known-good hash)"
        actual = item.get("actual") or "(missing)"
        flag = " [muted]" if item["name"] in muted_names else ""
        lines.append(f"  {item['status'].upper()}: {item['name']}{flag}")
        lines.append(f"      expected {expected}")
        lines.append(f"      actual   {actual}")
        if item.get("cached"):
            lines.append("      hash reused (size and mtime unchanged)")
    return lines


def _show_modal(title, message, parent=None):
    import mwmanager_dialogs as messagebox
    return messagebox.ask_continue_dont_warn(title, message, parent=parent)


def notify_problems(problems, game_dir, *, notify=None, parent=None, cli=True):
    """Tell the user. notify returns 'mute' to silence this exact hash, anything else continues."""
    message = short_warning(problems) + "\n\n" + recovery_advice(game_dir)
    missing = [item["name"] for item in problems if item["status"] == "missing"]
    altered = [item["name"] for item in problems if item["status"] == "altered"]
    if missing:
        message += "\n\nMissing: " + ", ".join(missing)
    if altered:
        message += "\n\nAltered: " + ", ".join(altered)
    message += ("\n\nThis does not block Play. \"Don't warn again\" only silences these exact "
                "file versions; a different hash will warn again.")
    if cli:
        print(short_warning(problems))
    if callable(notify):
        return notify("Shipped Morrowind files changed", message) or "continue"
    if notify == "gui":
        try:
            return _show_modal("Shipped Morrowind files changed", message, parent=parent) or "continue"
        except Exception:
            return "continue"
    return "continue"


def ensure_core_game_files_for_play(game_dir, *, manager_dir=None, known=None, hasher=None,
                                    notify=None, parent=None, cli=True, check=None):
    """Same launch path as the MWSE core check. Never blocks Play."""
    if check is None:
        check = ini_flag("check_core_game_files", manager_dir)
    if not check:
        return {"block": False, "status": "skipped", "problems": [], "message": None,
                "detail": ["core game file check skipped (check_core_game_files is false)"]}
    report = inspect_core_game_files(game_dir, manager_dir=manager_dir, known=known, hasher=hasher)
    muted = load_mute(manager_dir)
    visible = [item for item in report["problems"] if not is_muted(item, muted)]
    hidden = [item for item in report["problems"] if is_muted(item, muted)]
    if hidden or visible:
        append_detail_log(detail_lines(report, muted_names=[item["name"] for item in hidden]),
                          manager_dir)
    if not visible:
        return {"block": False, "status": "ok" if not hidden else "muted",
                "problems": report["problems"], "visible": [], "message": None,
                "detail": detail_lines(report, muted_names=[item["name"] for item in hidden]),
                "report": report}
    choice = notify_problems(visible, game_dir, notify=notify, parent=parent, cli=cli)
    if choice == "mute":
        mute_problems(visible, manager_dir)
        append_detail_log(
            [f"Don't warn again: muted current version of {item['name']} "
             f"({item.get('actual') or 'missing'})" for item in visible],
            manager_dir)
    return {"block": False, "status": "issues", "problems": report["problems"], "visible": visible,
            "message": short_warning(visible), "choice": choice, "report": report,
            "detail": detail_lines(report, muted_names=[item["name"] for item in hidden])}
