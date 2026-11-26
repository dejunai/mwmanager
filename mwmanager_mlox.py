"""Download and locate mlox load-order rules for MWManager.

Rules are stored in a mlox folder under the configured MWManager directory --
the same place --init and --sort already use. Paths are always built from
that directory and the game folder the caller passes in; nothing here hardcodes
a drive letter or install location.

The community files come from the mlox-rules project on GitHub
(DanaePlays/mlox-rules), which is the source official mlox downloads. The mlox
tool itself is MIT-licensed.
"""
from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.request

MLOX_RULE_URLS = {
    "mlox_base.txt": "https://raw.githubusercontent.com/DanaePlays/mlox-rules/main/mlox_base.txt",
    "mlox_user.txt": "https://raw.githubusercontent.com/DanaePlays/mlox-rules/main/mlox_user.txt",
}
MLOX_RULE_NAMES = tuple(MLOX_RULE_URLS)
MLOX_RULES_REPO = "https://github.com/DanaePlays/mlox-rules"
MLOX_USER_AGENT = "MWManager/1.0"
MLOX_MAX_BYTES = 8 * 1024 * 1024

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
    "Existing rule files are backed up before they are replaced.\n\n"
    "The mlox tool is MIT-licensed. If you have edited mlox_user.txt yourself, "
    "choose Keep using my own mlox rules so those edits are not overwritten."
)


def mlox_rules_folder(manager_dir):
    """Where MWManager keeps mlox_base.txt and mlox_user.txt, relative to the configured manager directory."""
    return os.path.join(manager_dir, "mlox")


def mlox_rules_backup_dir(manager_dir):
    return os.path.join(manager_dir, "backups", "rules")


def present_mlox_rules(folder):
    return [name for name in MLOX_RULE_NAMES if os.path.isfile(os.path.join(folder, name))]


def missing_mlox_rules(folder):
    return [name for name in MLOX_RULE_NAMES if not os.path.isfile(os.path.join(folder, name))]


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


def copy_game_mlox_rules(game_dir, folder, *, backup_dir=None, only_missing=True):
    """Copy rule files from <game>/mlox into the managed folder. Returns the names copied."""
    if not game_dir:
        return []
    game_rules = os.path.join(game_dir, "mlox")
    copied = []
    for name in MLOX_RULE_NAMES:
        source = os.path.join(game_rules, name)
        if not os.path.isfile(source):
            continue
        target = os.path.join(folder, name)
        if only_missing and os.path.isfile(target):
            continue
        os.makedirs(folder, exist_ok=True)
        if os.path.isfile(target) and backup_dir:
            backup_rule_file(target, backup_dir, suffix="pre-game-import")
        shutil.copy2(source, target)
        copied.append(name)
    return copied


def _url_of(request):
    return getattr(request, "full_url", None) or getattr(request, "get_full_url", lambda: request)()


def _fetch_rule(name, *, opener, timeout):
    url = MLOX_RULE_URLS[name]
    request = urllib.request.Request(url, headers={"User-Agent": MLOX_USER_AGENT})
    with opener(request, timeout=timeout) as response:
        content = response.read(MLOX_MAX_BYTES + 1)
    if len(content) > MLOX_MAX_BYTES or b"[order]" not in content.lower():
        raise ValueError("download was unexpectedly large or did not look like an mlox rules file")
    return content


def download_mlox_rules(folder, *, names=None, overwrite=True, opener=None, backup_dir=None, timeout=20):
    """Fetch community rule files into folder.

    All files are downloaded and checked first. Existing files are backed up,
    then replaced only if every requested file arrived intact. A failed or
    offline download leaves existing files in place.

    Returns a dict with ok, written, backed_up, errors. backed_up maps a
    rule name to the backup path (for tests and the detail log).
    """
    wanted = list(names) if names is not None else list(MLOX_RULE_NAMES)
    result = {"ok": True, "written": [], "backed_up": {}, "errors": []}
    if not overwrite:
        wanted = [name for name in wanted if not os.path.isfile(os.path.join(folder, name))]
    if not wanted:
        return result
    opener = opener or urllib.request.urlopen
    if backup_dir is None:
        backup_dir = os.path.join(os.path.dirname(folder), "backups", "rules")
    fetched = {}
    try:
        os.makedirs(folder, exist_ok=True)
        for name in wanted:
            fetched[name] = _fetch_rule(name, opener=opener, timeout=timeout)
    except Exception as exc:
        name = next((key for key in wanted if key not in fetched), wanted[-1])
        url = MLOX_RULE_URLS.get(name, "")
        result["ok"] = False
        result["errors"].append(f"could not download {name} from {url}: {exc}")
        return result

    temps = []
    try:
        for name, content in fetched.items():
            target = os.path.join(folder, name)
            temp = target + ".download"
            with open(temp, "wb") as stream:
                stream.write(content)
            temps.append((name, target, temp))
        for name, target, temp in temps:
            if os.path.isfile(target):
                result["backed_up"][name] = backup_rule_file(target, backup_dir)
            os.replace(temp, target)
            result["written"].append(name)
    except Exception as exc:
        result["ok"] = False
        result["errors"].append(f"could not save downloaded mlox rules: {exc}")
        return result
    finally:
        for _name, _target, temp in temps:
            if os.path.isfile(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass
    return result


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

    if not first_run:
        if present and not missing_mlox_rules(folder):
            _status(progress, "Preflight: using the mlox rules already in MWManager.")
        return True

    choice = _ask_mlox_choice(progress)
    if choice != CHOICE_DOWNLOAD:
        copied = copy_game_mlox_rules(game_dir, folder, backup_dir=backup_dir, only_missing=True)
        if present or copied:
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
