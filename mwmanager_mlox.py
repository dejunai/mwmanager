"""Download, update and locate mlox load-order rules for MWManager.

Rules are stored in a mlox folder under the configured MWManager directory --
the same place --init and --sort already use. Paths are always built from
that directory and the game folder the caller passes in; nothing here hardcodes
a drive letter or install location.

Community files (mlox_base.txt, mlox_user.txt) come from the mlox-rules project
on GitHub (DanaePlays/mlox-rules). Personal rules live in mlox_my_rules.txt in
the same folder and are never overwritten by a download.

Downstream pruning
------------------
Local rule files sit downstream of the published mlox-rules base (mlox_base.txt).
Tools > Update rules and --update-rules first check upstream and fetch only if
the community files are newer, then run this cleanup against those new files
(or the current files, if nothing was fetched), then save the download
metadata. --sort reads the live mlox_base.txt, mlox_user.txt and
mlox_my_rules.txt in the managed folder. A conflicting rule is commented out
in place, with one MWManager comment line just above it, when:

  * it duplicates an [Order] pair, or an identical rule, already in mlox_base.txt
  * it reverses an [Order] pair, or would form a cycle with, [Order] rules in
    mlox_base.txt
  * every plugin it names is not installed, so the rule cannot apply

mlox_base.txt itself is only commented for rules that name no installed plugin,
and for identical duplicate blocks. MWManager's own rules file is not rewritten.
Community files are not backed up (the published copies stay on GitHub).
mlox_my_rules.txt gets at most one rolling backup, overwritten when the file
actually changes. Each dropped rule is logged with its source file and the reason.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.request
from email.utils import format_datetime
from datetime import datetime, timezone

from mwdialog_loadorder import name_matcher, parse_rules, rule_entry_lines
from mwdialog_state import CONFLICT_RULE_NOTE, CONFLICT_RULES_HEADER

MLOX_RULE_URLS = {
    "mlox_base.txt": "https://raw.githubusercontent.com/DanaePlays/mlox-rules/main/mlox_base.txt",
    "mlox_user.txt": "https://raw.githubusercontent.com/DanaePlays/mlox-rules/main/mlox_user.txt",
}
MLOX_RULE_NAMES = tuple(MLOX_RULE_URLS)
MLOX_MY_RULES = "mlox_my_rules.txt"
MLOX_SOURCE_NAMES = (MLOX_MY_RULES, "mlox_user.txt", "mlox_base.txt")
MLOX_RULES_REPO = "https://github.com/DanaePlays/mlox-rules"
MLOX_USER_AGENT = "MWManager/1.0"
MLOX_MAX_BYTES = 8 * 1024 * 1024
MLOX_META_FILE = "mlox_rules_meta.json"
MANAGER_RULES_FILE = "mwmanager_rules.txt"
PLUGIN_EXTS = (".esp", ".esm", ".omwaddon")

DOWNLOAD_LABEL = "Download the latest mlox rules (from the mlox project on GitHub)"
KEEP_OWN_LABEL = "Keep using my own mlox rules"
CHOICE_TITLE = "mlox load-order rules"
CHOICE_DOWNLOAD = "download"
CHOICE_OWN = "own"
CHOICE_MESSAGE = (
    "MWManager can use mlox community rules when it sorts your plugins.\n\n"
    "Download the latest mlox rules (from the mlox project on GitHub), or keep "
    "using your own mlox rules if you already have them.\n\n"
    "The download fetches mlox_base.txt and mlox_user.txt from the mlox-rules "
    "project on GitHub (DanaePlays/mlox-rules), the source official mlox uses. "
    "Community rule files are replaced when a newer copy is downloaded; the "
    "published originals stay on GitHub, so MWManager does not keep a backup "
    "of them. "
    "Your personal file mlox_my_rules.txt is not downloaded or overwritten; "
    "put your own rules there. They take priority over the community files.\n\n"
    "The mlox tool is MIT-licensed. If you have edited mlox_user.txt yourself, "
    "move those edits into mlox_my_rules.txt (or choose Keep using my own mlox "
    "rules) so a later download does not replace them."
)


def mlox_rules_folder(manager_dir):
    """Where MWManager keeps mlox rule files, relative to the configured manager directory."""
    return os.path.join(manager_dir, "mlox")


def present_mlox_rules(folder, names=None):
    names = names or MLOX_RULE_NAMES
    return [name for name in names if os.path.isfile(os.path.join(folder, name))]


def missing_mlox_rules(folder, names=None):
    names = names or MLOX_RULE_NAMES
    return [name for name in names if not os.path.isfile(os.path.join(folder, name))]


def installed_plugins(game_dir):
    """Plugin filenames in the game's Data Files folder (empty if it cannot be read)."""
    if not game_dir:
        return []
    data = os.path.join(game_dir, "Data Files")
    try:
        names = os.listdir(data)
    except OSError:
        return []
    return [name for name in names if name.lower().endswith(PLUGIN_EXTS)]


def mlox_sort_paths(manager_dir):
    """Managed mlox files --sort reads, highest mlox priority first (personal, user, base).

    Live files only: mlox_base.txt, mlox_user.txt and mlox_my_rules.txt. Stale
    *.effective.txt copies from earlier MWManager versions are ignored.
    """
    folder = mlox_rules_folder(manager_dir)
    return [os.path.join(folder, name)
            for name in MLOX_SOURCE_NAMES
            if os.path.isfile(os.path.join(folder, name))]


def personal_rules_backup_path(folder):
    """Single rolling backup of the user's mlox_my_rules.txt, beside the live file."""
    return os.path.join(folder, MLOX_MY_RULES + ".bak")


def manager_rules_path(manager_dir):
    """MWManager's own sort-rules file, in the managed manager directory."""
    return os.path.join(manager_dir, MANAGER_RULES_FILE)


def manager_rules_backup_path(manager_dir):
    """Single rolling backup of mwmanager_rules.txt, beside the live file."""
    return os.path.join(manager_dir, MANAGER_RULES_FILE + ".bak")


def stale_effective_path(folder, name):
    root, ext = os.path.splitext(name)
    return os.path.join(folder, root + ".effective" + ext)


def remove_stale_effective_rules(folder):
    """Delete leftover *.effective.txt copies for the known mlox rule files."""
    removed = []
    for name in MLOX_SOURCE_NAMES:
        path = stale_effective_path(folder, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                continue
            removed.append(path)
    return removed


def copy_game_mlox_rules(game_dir, folder, *, only_missing=True, names=None):
    """Copy rule files from <game>/mlox into the managed folder. Returns the names copied.

    mlox_my_rules.txt is only copied when the managed copy is missing: it is the
    user's personal file and is never overwritten.
    """
    if not game_dir:
        return []
    names = list(names) if names is not None else list(MLOX_SOURCE_NAMES)
    game_rules = os.path.join(game_dir, "mlox")
    copied = []
    for name in names:
        source = os.path.join(game_rules, name)
        if not os.path.isfile(source):
            continue
        target = os.path.join(folder, name)
        personal = name == MLOX_MY_RULES
        if (only_missing or personal) and os.path.isfile(target):
            continue
        os.makedirs(folder, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(name)
    return copied


def import_personal_rules(game_dir, folder):
    """If the managed mlox_my_rules.txt is missing, copy it once from the game folder."""
    return copy_game_mlox_rules(game_dir, folder, only_missing=True, names=[MLOX_MY_RULES])


def _url_of(request):
    return getattr(request, "full_url", None) or getattr(request, "get_full_url", lambda: request)()


def _header(headers, name):
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter:
        value = getter(name)
        if value:
            return value
        if hasattr(headers, "items"):
            for key, val in headers.items():
                if str(key).lower() == name.lower():
                    return val
        return None
    if isinstance(headers, dict):
        for key, val in headers.items():
            if str(key).lower() == name.lower():
                return val
    return None


def _response_status(response):
    return getattr(response, "status", None) or getattr(response, "code", 200) or 200


def _load_meta(folder):
    path = os.path.join(folder, MLOX_META_FILE)
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_meta(folder, meta):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, MLOX_META_FILE)
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(meta, stream, indent=2, sort_keys=True)


def _file_http_date(path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return format_datetime(datetime.fromtimestamp(mtime, timezone.utc), usegmt=True)


def _fetch_rule(name, *, opener, timeout, headers=None):
    url = MLOX_RULE_URLS[name]
    request_headers = {"User-Agent": MLOX_USER_AGENT}
    if headers:
        request_headers.update({k: v for k, v in headers.items() if v})
    request = urllib.request.Request(url, headers=request_headers)
    with opener(request, timeout=timeout) as response:
        status = _response_status(response)
        resp_headers = getattr(response, "headers", {})
        if status == 304:
            return "not_modified", None, resp_headers
        content = response.read(MLOX_MAX_BYTES + 1)
    if len(content) > MLOX_MAX_BYTES or b"[order]" not in content.lower():
        raise ValueError("download was unexpectedly large or did not look like an mlox rules file")
    return "updated", content, resp_headers


def _conditional_headers(folder, name, meta):
    info = meta.get(name) if isinstance(meta.get(name), dict) else {}
    headers = {}
    if info.get("etag"):
        headers["If-None-Match"] = info["etag"]
    elif info.get("last_modified"):
        headers["If-Modified-Since"] = info["last_modified"]
    else:
        dated = _file_http_date(os.path.join(folder, name))
        if dated:
            headers["If-Modified-Since"] = dated
    return headers


def _classify_fetch_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 304:
            return "not_modified"
        if exc.code in (403, 429):
            return "rate_limited"
        return "error"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return "offline"
    return "error"


def download_mlox_rules(folder, *, names=None, overwrite=True, opener=None, timeout=20,
                        conditional=False, dry_run=False, save_meta=True):
    """Fetch community rule files into folder.

    All requested files are downloaded and checked first, then replaced only
    if every requested file arrived intact. Community files are not backed up
    (the published copies stay on GitHub). A failed or offline download leaves
    existing files in place. Personal mlox_my_rules.txt is never fetched or
    overwritten.

    When conditional is True, If-None-Match / If-Modified-Since is sent from
    the managed metadata file (and a 304 is treated as already up to date).

    Returns a dict with ok, written, unchanged, errors, status.
    """
    wanted = [name for name in (names if names is not None else MLOX_RULE_NAMES) if name != MLOX_MY_RULES]
    result = {"ok": True, "written": [], "unchanged": [], "errors": [],
              "status": "ok", "meta": None}
    if not overwrite:
        wanted = [name for name in wanted if not os.path.isfile(os.path.join(folder, name))]
    if not wanted:
        result["status"] = "unchanged"
        return result
    opener = opener or urllib.request.urlopen
    meta = _load_meta(folder)
    result["meta"] = meta
    fetched = {}
    header_map = {}
    try:
        os.makedirs(folder, exist_ok=True)
        for name in wanted:
            extra = _conditional_headers(folder, name, meta) if conditional else {}
            try:
                status, content, headers = _fetch_rule(name, opener=opener, timeout=timeout, headers=extra)
            except Exception as exc:
                kind = _classify_fetch_error(exc)
                if kind == "not_modified":
                    result["unchanged"].append(name)
                    continue
                result["ok"] = False
                result["status"] = kind
                result["errors"].append(f"could not download {name} from {MLOX_RULE_URLS[name]}: {exc}")
                return result
            if status == "not_modified":
                result["unchanged"].append(name)
                continue
            fetched[name] = content
            header_map[name] = headers
    except Exception as exc:
        name = next((key for key in wanted if key not in fetched and key not in result["unchanged"]), wanted[-1])
        url = MLOX_RULE_URLS.get(name, "")
        result["ok"] = False
        result["status"] = _classify_fetch_error(exc)
        result["errors"].append(f"could not download {name} from {url}: {exc}")
        return result

    if not fetched:
        result["status"] = "unchanged"
        return result

    if dry_run:
        result["status"] = "updated"
        result["written"] = list(fetched)
        return result

    temps = []
    try:
        for name, content in fetched.items():
            target = os.path.join(folder, name)
            if os.path.isfile(target) and hashlib.sha256(open(target, "rb").read()).digest() == hashlib.sha256(content).digest():
                result["unchanged"].append(name)
                _store_file_meta(meta, name, header_map.get(name), content)
                continue
            temp = target + ".download"
            with open(temp, "wb") as stream:
                stream.write(content)
            temps.append((name, target, temp, content))
        for name, target, temp, content in temps:
            os.replace(temp, target)
            result["written"].append(name)
            _store_file_meta(meta, name, header_map.get(name), content)
        result["meta"] = meta
        if save_meta:
            _save_meta(folder, meta)
    except Exception as exc:
        result["ok"] = False
        result["status"] = "error"
        result["errors"].append(f"could not save downloaded mlox rules: {exc}")
        return result
    finally:
        for _name, _target, temp, _content in temps:
            if os.path.isfile(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass
    if result["written"]:
        result["status"] = "updated"
    elif result["unchanged"]:
        result["status"] = "unchanged"
    return result


def _store_file_meta(meta, name, headers, content):
    entry = dict(meta.get(name) or {})
    etag = _header(headers, "ETag")
    last_modified = _header(headers, "Last-Modified")
    if etag:
        entry["etag"] = etag
    if last_modified:
        entry["last_modified"] = last_modified
    entry["sha256"] = hashlib.sha256(content).hexdigest()
    meta[name] = entry


def _ask_mlox_choice(progress):
    choices = ((DOWNLOAD_LABEL, CHOICE_DOWNLOAD), (KEEP_OWN_LABEL, CHOICE_OWN))
    if hasattr(progress, "ask_choice"):
        return progress.ask_choice(CHOICE_TITLE, CHOICE_MESSAGE, list(choices), CHOICE_OWN)
    if hasattr(progress, "ask_yes_no"):
        return CHOICE_DOWNLOAD if progress.ask_yes_no(CHOICE_TITLE, CHOICE_MESSAGE) else CHOICE_OWN
    return CHOICE_OWN


def _status(progress, message):
    if callable(progress):
        progress(message)


def _detail(progress, message):
    _status(progress, "MWMANAGER-DETAIL: " + message)


def offer_mlox_rules(progress, game_dir, manager_dir, *, first_run=False, opener=None):
    """GUI preflight choice: download the latest community rules, or keep the user's own.

    Never raises for a network failure. User-facing lines stay plain; details go
    to the detail log. Returns True so a failed download does not stop preflight.
    """
    folder = mlox_rules_folder(manager_dir)
    present = present_mlox_rules(folder)
    import_personal_rules(game_dir, folder)

    if not first_run:
        if present and not missing_mlox_rules(folder):
            _status(progress, "Preflight: using the mlox rules already in MWManager.")
        return True

    choice = _ask_mlox_choice(progress)
    if choice != CHOICE_DOWNLOAD:
        copied = copy_game_mlox_rules(game_dir, folder, only_missing=True)
        if present or copied or os.path.isfile(os.path.join(folder, MLOX_MY_RULES)):
            _status(progress, "Preflight: keeping your existing mlox rules.")
        else:
            _status(progress, "Preflight: continuing without mlox rules.")
        return True

    _status(progress, "Preflight: downloading the latest mlox rules...")
    result = download_mlox_rules(folder, overwrite=True, opener=opener)
    if result["ok"]:
        _status(progress, "Preflight: downloaded the latest mlox rules.")
        return True
    if present:
        _status(progress, "Preflight: could not download the latest mlox rules; existing rules were left as they are.")
    else:
        _status(progress, "Preflight: could not download the latest mlox rules; continuing without them.")
    for error in result["errors"]:
        _detail(progress, error)
    return True


# ---- pruning (in-place comments on the live files) ---------------------------

# mlox treats a semicolon as the start of a comment (parse_rules splits on ";").
# Official rule files use ";;" for whole-line comments. MWManager does the same.
DISABLE_MARKER = ";; MWManager: disabled"


def _installed_map(plugins):
    return {name.casefold(): name for name in (plugins or [])}


def _matches(entry, installed_low):
    rx = name_matcher(entry)
    if rx is None:
        hit = installed_low.get(entry.casefold())
        return [hit] if hit else []
    return [name for key, name in installed_low.items() if rx.match(name)]


def _rule_has_installed_plugins(kind, entries, installed_low):
    if installed_low is None:
        return True
    matched = []
    for entry in entries:
        matched.extend(_matches(entry, installed_low))
    unique = list(dict.fromkeys(name.casefold() for name in matched))
    if kind in ("order", "exact"):
        return len(unique) >= 2
    return len(unique) >= 1


def _order_pairs(entries):
    return [(a.casefold(), b.casefold()) for a, b in zip(entries, entries[1:])]


def _reachable(pairs):
    succ = {}
    for a, b in pairs:
        succ.setdefault(a, set()).add(b)

    def reaches(src, dst):
        stack, seen = [src], {src}
        while stack:
            x = stack.pop()
            if x == dst:
                return True
            for y in succ.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return False

    return reaches


def _identical(kind, entries, others):
    key = (kind, tuple(e.casefold() for e in entries))
    return any((k, tuple(e.casefold() for e in ents)) == key for k, ents, _src in others)


def _prune_reason(kind, entries, src, base_rules, base_pairs, reaches, installed_low):
    if not _rule_has_installed_plugins(kind, entries, installed_low):
        return "mentions only plugins that are not installed"
    if _identical(kind, entries, base_rules):
        return "duplicates a rule in mlox_base.txt"
    if kind == "order":
        pairs = _order_pairs(entries)
        if pairs and all(pair in base_pairs for pair in pairs):
            return "duplicates [Order] in mlox_base.txt"
        for a, b in pairs:
            if a == b:
                continue
            if (b, a) in base_pairs:
                return "reverses [Order] in mlox_base.txt"
            if reaches(b, a):
                return "would cycle with [Order] in mlox_base.txt"
    return None


def _is_comment_line(line):
    return line.lstrip(" 	").startswith(";")


def _is_disable_comment(line):
    return line.lstrip(" 	").lstrip(";").lstrip().startswith("MWManager: disabled")


def _comment_out_line(line):
    """Prefix an active rule line with ';; '. Leave existing comments untouched."""
    if _is_comment_line(line):
        return line
    stripped = line.lstrip(" 	")
    leading = line[:len(line) - len(stripped)]
    return f"{leading};; {stripped}"


def _disable_comment(reason, when):
    return f"{DISABLE_MARKER} - {reason} ({when.isoformat()})"


def _source_line(src):
    name, sep, num = src.rpartition(":")
    if not sep:
        return src, None
    try:
        return name, int(num)
    except ValueError:
        return src, None


def _read_rule_file(path):
    with open(path, "rb") as stream:
        data = stream.read()
    newline = "\r\n" if b"\r\n" in data.splitlines(keepends=True)[:1] or b"\r\n" in data else "\n"
    if b"\r\n" in data:
        newline = "\r\n"
    elif b"\n" in data:
        newline = "\n"
    else:
        newline = "\n"
    text = data.decode("cp1252", errors="replace")
    ended = text.endswith(("\n", "\r"))
    return text.splitlines(), newline, ended, data


def _disable_rule_in_lines(lines, tag_line, reason, when):
    """Comment out one rule block in a list of lines. tag_line is 1-based.

    Idempotent: no second MWManager comment if one is already immediately
    above the tag, and lines that are already comments are left as they are.
    """
    tag_idx = tag_line - 1
    if tag_idx < 0 or tag_idx >= len(lines):
        return False
    changed = False
    if tag_idx == 0 or not _is_disable_comment(lines[tag_idx - 1]):
        lines.insert(tag_idx, _disable_comment(reason, when))
        tag_idx += 1
        changed = True
    commented = _comment_out_line(lines[tag_idx])
    if commented != lines[tag_idx]:
        lines[tag_idx] = commented
        changed = True
    for index in rule_entry_lines(lines, tag_idx + 1):
        commented = _comment_out_line(lines[index])
        if commented != lines[index]:
            lines[index] = commented
            changed = True
    return changed


def _write_rule_file(path, lines, newline, ended, original, *, backup=False, backup_path=None):
    text = newline.join(lines)
    if ended or lines:
        if not text.endswith(("\r\n", "\n")):
            text += newline
    new = text.encode("cp1252", errors="replace")
    if new == original:
        return False
    if backup and original:
        bak = backup_path or (path + ".bak")
        with open(bak, "wb") as stream:
            stream.write(original)
    temp = path + ".tmp"
    with open(temp, "wb") as stream:
        stream.write(new)
    os.replace(temp, path)
    return True


def prune_mlox_rules(folder, plugins=None, *, progress=None, dry_run=False, when=None):
    """Comment out conflicting rules in the live managed mlox files.

    Returns a list of (source, reason) for each newly disabled rule. dry_run
    computes the same list without writing. Running it again is idempotent:
    already-disabled rules are ignored by parse_rules, existing MWManager
    comments are not repeated, and already-commented lines are not re-prefixed.
    """
    from datetime import date as date_type

    installed_low = _installed_map(plugins) if plugins is not None else None
    when = when or datetime.now().date()
    if not isinstance(when, date_type) and hasattr(when, "date"):
        when = when.date()
    dropped = []
    base_path = os.path.join(folder, "mlox_base.txt")
    base_rules, _ignored = parse_rules(base_path) if os.path.isfile(base_path) else ([], None)
    base_pairs = set()
    for kind, entries, _src in base_rules:
        if kind == "order":
            base_pairs.update(_order_pairs(entries))
    reaches = _reachable(base_pairs)

    plans = []
    # Base is pruned only for missing plugins and identical duplicate blocks.
    if os.path.isfile(base_path):
        disable, seen = [], set()
        for kind, entries, src in base_rules:
            key = (kind, tuple(e.casefold() for e in entries))
            if key in seen:
                reason = "duplicate of an earlier rule in mlox_base.txt"
                disable.append((src, reason))
                dropped.append((src, reason))
                continue
            if installed_low is not None and not _rule_has_installed_plugins(kind, entries, installed_low):
                reason = "mentions only plugins that are not installed"
                disable.append((src, reason))
                dropped.append((src, reason))
                continue
            seen.add(key)
        plans.append(("mlox_base.txt", disable))

    for name in ("mlox_user.txt", MLOX_MY_RULES):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        rules, _ignored = parse_rules(path)
        disable = []
        for kind, entries, src in rules:
            reason = _prune_reason(kind, entries, src, base_rules, base_pairs, reaches, installed_low)
            if reason:
                disable.append((src, reason))
                dropped.append((src, reason))
        plans.append((name, disable))

    if not dry_run:
        remove_stale_effective_rules(folder)
        for name, disable in plans:
            if not disable:
                continue
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            lines, newline, ended, original = _read_rule_file(path)
            # Apply from the bottom so earlier line numbers stay valid.
            items = []
            for src, reason in disable:
                _name, tag_line = _source_line(src)
                if tag_line:
                    items.append((tag_line, reason))
            items.sort(reverse=True)
            for tag_line, reason in items:
                _disable_rule_in_lines(lines, tag_line, reason, when)
            _write_rule_file(path, lines, newline, ended, original,
                             backup=(name == MLOX_MY_RULES),
                             backup_path=(personal_rules_backup_path(folder)
                                          if name == MLOX_MY_RULES else None))

    if progress is not None:
        for src, reason in dropped:
            _detail(progress, f"skipped {src}: {reason}")
    return dropped


CONTEXT_BEGIN = "[Context]"
CONTEXT_END = "; ---- end of [Context] ----"


def _protected_manager_rule_lines(lines):
    """1-based line numbers in [Context] and the --cell-conflicts block, which must not be edited."""
    protected = set()
    if lines and lines[0].strip().lower() == CONTEXT_BEGIN.lower():
        end = next((i for i, line in enumerate(lines) if line.strip() == CONTEXT_END), None)
        if end is not None:
            protected.update(range(1, end + 2))
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith(CONFLICT_RULES_HEADER):
            protected.add(i + 1)
            i += 1
            continue
        block = [line.strip() for line in lines[i:i + 4]]
        if (len(block) == 4 and block[0].lower() == "[order]"
                and block[1] and block[2]
                and not block[1].startswith((";", "[")) and not block[2].startswith((";", "["))
                and CONFLICT_RULE_NOTE.match(block[3])):
            protected.update(range(i + 1, i + 5))
            i += 4
            continue
        i += 1
    return protected


def _manager_prune_reason(kind, entries, seen, base_rules, installed_low):
    if not _rule_has_installed_plugins(kind, entries, installed_low):
        return "mentions only plugins that are not installed"
    key = (kind, tuple(e.casefold() for e in entries))
    if key in seen:
        return f"duplicate of an earlier rule in {MANAGER_RULES_FILE}"
    if _identical(kind, entries, base_rules):
        return "duplicates a rule in mlox_base.txt"
    return None


def prune_manager_rules(manager_dir, plugins=None, *, mlox_folder=None, progress=None, dry_run=False, when=None):
    """Comment out obsolete rules in mwmanager_rules.txt, if that file already exists.

    Never creates the file. Leaves the [Context] section and the --cell-conflicts
    block untouched. Does not disable rules that reverse or cycle with mlox_base
    (this file is the override layer). Returns a list of (source, reason).
    """
    from datetime import date as date_type

    path = manager_rules_path(manager_dir)
    if not os.path.isfile(path):
        return []
    installed_low = _installed_map(plugins) if plugins is not None else None
    when = when or datetime.now().date()
    if not isinstance(when, date_type) and hasattr(when, "date"):
        when = when.date()
    folder = mlox_folder or mlox_rules_folder(manager_dir)
    base_path = os.path.join(folder, "mlox_base.txt")
    base_rules, _ignored = parse_rules(base_path) if os.path.isfile(base_path) else ([], None)

    lines, newline, ended, original = _read_rule_file(path)
    protected = _protected_manager_rule_lines(lines)
    rules, _ignored = parse_rules(path)
    dropped = []
    disable = []
    seen = set()
    for kind, entries, src in rules:
        _name, tag_line = _source_line(src)
        if tag_line and tag_line in protected:
            key = (kind, tuple(e.casefold() for e in entries))
            seen.add(key)
            continue
        reason = _manager_prune_reason(kind, entries, seen, base_rules, installed_low)
        key = (kind, tuple(e.casefold() for e in entries))
        seen.add(key)
        if reason:
            disable.append((src, reason))
            dropped.append((src, reason))

    if not dry_run and disable:
        items = []
        for src, reason in disable:
            _name, tag_line = _source_line(src)
            if tag_line:
                items.append((tag_line, reason))
        items.sort(reverse=True)
        for tag_line, reason in items:
            _disable_rule_in_lines(lines, tag_line, reason, when)
        _write_rule_file(path, lines, newline, ended, original, backup=True,
                         backup_path=manager_rules_backup_path(manager_dir))

    if progress is not None:
        for src, reason in dropped:
            _detail(progress, f"skipped {src}: {reason}")
    return dropped


def update_rules(progress, manager_dir, *, plugins=None, game_dir=None, opener=None, dry_run=False):
    """Shared path for Tools > Update rules and --update-rules.

    Runs in this order:
      1. Check upstream and fetch community files only if they are newer.
      2. Run conflicting-rule cleanup in place against those NEW files (or the
         current files, if nothing new came down).
      3. If mwmanager_rules.txt already exists, comment out obsolete rules
         there (uninstalled plugins, duplicates in the file, exact duplicates
         of mlox_base). Override rules that reverse or cycle with base, the
         [Context] section, and the --cell-conflicts block are left alone.
      4. Save the download metadata (the live files were already written by
         prune, including any single rolling backup of mlox_my_rules.txt or
         mwmanager_rules.txt).
    """
    folder = mlox_rules_folder(manager_dir)
    os.makedirs(folder, exist_ok=True)
    import_personal_rules(game_dir, folder)
    if plugins is None and game_dir:
        plugins = installed_plugins(game_dir)

    _status(progress, "Checking for newer mlox rules...")
    # 1. Check and fetch. Community files are replaced now so step 2 reads them.
    #    Metadata is not written yet. Community files are not backed up.
    result = download_mlox_rules(folder, overwrite=True, opener=opener,
                                 conditional=True, dry_run=dry_run, save_meta=False)
    if not result["ok"]:
        if result["status"] == "offline":
            _status(progress, "Could not check for mlox rule updates (offline). Existing rules were left as they are.")
        elif result["status"] == "rate_limited":
            _status(progress, "Could not check for mlox rule updates (GitHub rate limit). Existing rules were left as they are.")
        else:
            _status(progress, "Could not check for mlox rule updates. Existing rules were left as they are.")
        for error in result["errors"]:
            _detail(progress, error)
    elif result["status"] == "updated":
        _status(progress, "Downloaded the latest mlox rules." if not dry_run
                else "Would download newer mlox rules.")
    else:
        _status(progress, "mlox rules are already up to date.")

    # 2. Cleanup against the files now in the folder (new, if a fetch succeeded).
    dropped = prune_mlox_rules(folder, plugins, progress=progress, dry_run=dry_run)
    if dropped:
        _status(progress, f"Skipped {len(dropped)} mlox rules that conflict with newer rules or mention mods you don't have.")

    # 2b. After the mlox prune: clean mwmanager_rules.txt if it already exists.
    manager_dropped = prune_manager_rules(manager_dir, plugins, mlox_folder=folder,
                                          progress=progress, dry_run=dry_run)
    if manager_dropped:
        _status(progress, f"Skipped {len(manager_dropped)} rules in {MANAGER_RULES_FILE} that were duplicates or mention mods you don't have.")

    # 3. Save metadata last, after prune has updated the live files.
    if not dry_run and result.get("meta") is not None:
        _save_meta(folder, result["meta"])
    return result["ok"] or os.path.isfile(os.path.join(folder, "mlox_base.txt"))
