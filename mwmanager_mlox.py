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
(or the current files, if nothing was fetched), then save the pruned effective
copies and the download metadata. --sort reads the effective copies and leaves
the originals in place. A rule in mlox_user.txt or
mlox_my_rules.txt is omitted from the effective copy when:

  * it duplicates an [Order] pair, or an identical rule, already in mlox_base.txt
  * it reverses an [Order] pair, or would form a cycle with, [Order] rules in
    mlox_base.txt
  * every plugin it names is not installed, so the rule cannot apply

mlox_base.txt itself is only trimmed of rules that name no installed plugin,
and of identical duplicate blocks. MWManager's own rules file is not rewritten.
mlox_my_rules.txt is never edited in place; the effective copy is what --sort
reads. Each dropped rule is logged with its source file and the reason.
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

from mwdialog_loadorder import name_matcher, parse_rules

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
    "Existing community rule files are backed up before they are replaced. "
    "Your personal file mlox_my_rules.txt is not downloaded or overwritten; "
    "put your own rules there. They take priority over the community files.\n\n"
    "The mlox tool is MIT-licensed. If you have edited mlox_user.txt yourself, "
    "move those edits into mlox_my_rules.txt (or choose Keep using my own mlox "
    "rules) so a later download does not replace them."
)


def mlox_rules_folder(manager_dir):
    """Where MWManager keeps mlox rule files, relative to the configured manager directory."""
    return os.path.join(manager_dir, "mlox")


def mlox_rules_backup_dir(manager_dir):
    return os.path.join(manager_dir, "backups", "rules")


def effective_rule_name(name):
    root, ext = os.path.splitext(name)
    return root + ".effective" + ext


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

    An effective copy is preferred when present, so pruning can drop conflicts
    without rewriting the user's originals.
    """
    folder = mlox_rules_folder(manager_dir)
    paths = []
    for name in MLOX_SOURCE_NAMES:
        effective = os.path.join(folder, effective_rule_name(name))
        original = os.path.join(folder, name)
        if os.path.isfile(effective):
            paths.append(effective)
        elif os.path.isfile(original):
            paths.append(original)
    return paths


def backup_rule_file(source, backup_dir, suffix="pre-download"):
    """Copy an existing rules file aside before it is overwritten. Never reuses a backup name."""
    os.makedirs(backup_dir, exist_ok=True)
    name = os.path.basename(source)
    dest = os.path.join(backup_dir, f"{name}.{suffix}")
    if os.path.exists(dest):
        n = 2
        while os.path.exists(os.path.join(backup_dir, f"{name}.{suffix}-{n}")):
            n += 1
        dest = os.path.join(backup_dir, f"{name}.{suffix}-{n}")
    shutil.copy2(source, dest)
    return dest


def copy_game_mlox_rules(game_dir, folder, *, backup_dir=None, only_missing=True, names=None):
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
        if os.path.isfile(target) and backup_dir and not personal:
            backup_rule_file(target, backup_dir, suffix="pre-game-import")
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


def download_mlox_rules(folder, *, names=None, overwrite=True, opener=None, backup_dir=None, timeout=20,
                        conditional=False, dry_run=False, save_meta=True):
    """Fetch community rule files into folder.

    All requested files are downloaded and checked first. Existing files are
    backed up, then replaced only if every requested file arrived intact. A
    failed or offline download leaves existing files in place. Personal
    mlox_my_rules.txt is never fetched or overwritten.

    When conditional is True, If-None-Match / If-Modified-Since is sent from
    the managed metadata file (and a 304 is treated as already up to date).

    Returns a dict with ok, written, backed_up, unchanged, errors, status.
    backed_up maps a rule name to the backup path (for tests and the detail log).
    """
    wanted = [name for name in (names if names is not None else MLOX_RULE_NAMES) if name != MLOX_MY_RULES]
    result = {"ok": True, "written": [], "backed_up": {}, "unchanged": [], "errors": [],
              "status": "ok", "meta": None}
    if not overwrite:
        wanted = [name for name in wanted if not os.path.isfile(os.path.join(folder, name))]
    if not wanted:
        result["status"] = "unchanged"
        return result
    opener = opener or urllib.request.urlopen
    if backup_dir is None:
        backup_dir = os.path.join(os.path.dirname(folder), "backups", "rules")
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
            if os.path.isfile(target):
                result["backed_up"][name] = backup_rule_file(target, backup_dir)
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
    backup_dir = mlox_rules_backup_dir(manager_dir)
    present = present_mlox_rules(folder)
    import_personal_rules(game_dir, folder)

    if not first_run:
        if present and not missing_mlox_rules(folder):
            _status(progress, "Preflight: using the mlox rules already in MWManager.")
        return True

    choice = _ask_mlox_choice(progress)
    if choice != CHOICE_DOWNLOAD:
        copied = copy_game_mlox_rules(game_dir, folder, backup_dir=backup_dir, only_missing=True)
        if present or copied or os.path.isfile(os.path.join(folder, MLOX_MY_RULES)):
            _status(progress, "Preflight: keeping your existing mlox rules.")
        else:
            _status(progress, "Preflight: continuing without mlox rules.")
        return True

    _status(progress, "Preflight: downloading the latest mlox rules...")
    result = download_mlox_rules(folder, overwrite=True, opener=opener, backup_dir=backup_dir)
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


# ---- pruning (effective copies for --sort) ---------------------------------

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
            return "duplicates [Order] pairs already in mlox_base.txt"
        for a, b in pairs:
            if a == b:
                continue
            if reaches(b, a):
                return f"conflicts with mlox_base.txt ([Order] {a} before {b} is reversed or would cycle)"
    return None


def _write_effective(path, source_name, kept):
    lines = [
        "; Effective mlox rules written by MWManager. Do not edit this file.",
        f"; Source: {source_name}",
        "",
    ]
    for kind, entries, _src in kept:
        tag = {"order": "Order", "nearstart": "NearStart", "nearend": "NearEnd", "exact": "Exact"}.get(kind, kind)
        lines.append(f"[{tag}]")
        lines.extend(entries)
        lines.append("")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines).rstrip() + "\n")


def prune_mlox_rules(folder, plugins=None, *, progress=None, dry_run=False):
    """Write effective copies of the managed mlox files. Originals are not rewritten.

    Returns a list of (source, reason) for each dropped rule. dry_run computes
    the same list without writing the effective copies.
    """
    installed_low = _installed_map(plugins) if plugins is not None else None
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
        kept, seen = [], set()
        for kind, entries, src in base_rules:
            key = (kind, tuple(e.casefold() for e in entries))
            if key in seen:
                dropped.append((src, "duplicate of an earlier rule in mlox_base.txt"))
                continue
            if installed_low is not None and not _rule_has_installed_plugins(kind, entries, installed_low):
                dropped.append((src, "mentions only plugins that are not installed"))
                continue
            seen.add(key)
            kept.append((kind, entries, src))
        plans.append(("mlox_base.txt", kept))

    for name in ("mlox_user.txt", MLOX_MY_RULES):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        rules, _ignored = parse_rules(path)
        kept = []
        for kind, entries, src in rules:
            reason = _prune_reason(kind, entries, src, base_rules, base_pairs, reaches, installed_low)
            if reason:
                dropped.append((src, reason))
            else:
                kept.append((kind, entries, src))
        plans.append((name, kept))

    if not dry_run:
        for name, kept in plans:
            _write_effective(os.path.join(folder, effective_rule_name(name)), name, kept)

    if progress is not None:
        for src, reason in dropped:
            _detail(progress, f"skipped {src}: {reason}")
    return dropped


def update_rules(progress, manager_dir, *, plugins=None, game_dir=None, opener=None, dry_run=False):
    """Shared path for Tools > Update rules and --update-rules.

    Runs in this order:
      1. Check upstream and fetch community files only if they are newer.
      2. Run conflicting-rule cleanup against those NEW files (or the current
         files, if nothing new came down).
      3. Save the pruned effective copies and the download metadata.
    """
    folder = mlox_rules_folder(manager_dir)
    backup_dir = mlox_rules_backup_dir(manager_dir)
    os.makedirs(folder, exist_ok=True)
    import_personal_rules(game_dir, folder)
    if plugins is None and game_dir:
        plugins = installed_plugins(game_dir)

    _status(progress, "Checking for newer mlox rules...")
    # 1. Check and fetch. Community files are replaced now so step 2 reads them.
    #    Metadata is not written yet.
    result = download_mlox_rules(folder, overwrite=True, opener=opener, backup_dir=backup_dir,
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

    # 3. Save metadata last, with the effective copies already written by prune.
    if not dry_run and result.get("meta") is not None:
        _save_meta(folder, result["meta"])
    return result["ok"] or os.path.isfile(os.path.join(folder, "mlox_base.txt"))
