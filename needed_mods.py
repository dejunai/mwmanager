"""Batch step 3: find the download archive that holds a plugin a chosen option needs.

Each archive's plugin names are listed once and kept in an index keyed by the archive's size and modification
time, so a batch looks a plugin up without opening ~1,500 archives again; a new or changed archive is listed
the next time. An archive that can't be listed is remembered as holding nothing until it changes."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import core_addons

ARCHIVE_EXTS = (".7z", ".zip", ".rar")
PLUGIN_EXTS = (".esp", ".esm")
INDEX_VERSION = 1


def archive_index(downloads: Path, index_path: Path, seven_zip: str | None, progress=None) -> dict[str, list[str]]:
    """{archive file name: [plugin file names]} for every archive in `downloads`."""
    try:
        cached = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}
    entries = cached.get("archives", {}) if isinstance(cached, dict) and cached.get("version") == INDEX_VERSION else {}
    try:
        names = sorted(n for n in os.listdir(downloads) if n.casefold().endswith(ARCHIVE_EXTS))
    except OSError:
        return {}
    fresh, listed = {}, 0
    for name in names:
        try:
            st = os.stat(os.path.join(downloads, name))
        except OSError:
            continue
        stamp = f"{st.st_size}|{st.st_mtime_ns}"
        old = entries.get(name)
        if isinstance(old, dict) and old.get("stamp") == stamp:
            fresh[name] = old
            continue
        members, error = core_addons._members(Path(downloads) / name, seven_zip)
        if error:                           # e.g. no 7-Zip yet, or still downloading: tried again next run
            if isinstance(old, dict):
                fresh[name] = old
            continue
        fresh[name] = {"stamp": stamp, "plugins": sorted({m.replace("\\", "/").rsplit("/", 1)[-1] for m in members
                                                          if m.casefold().endswith(PLUGIN_EXTS)})}
        listed += 1
        if progress and listed % 25 == 0:
            progress(f"Indexing download archives: {listed} new or changed so far")
    if listed or set(fresh) != set(entries):
        index_path = Path(index_path)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        temp = index_path.with_suffix(index_path.suffix + ".tmp")
        temp.write_text(json.dumps({"version": INDEX_VERSION, "archives": fresh}), encoding="utf-8")
        os.replace(temp, index_path)
    return {name: entry["plugins"] for name, entry in fresh.items()}


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.casefold()) if len(w) > 1}


# Words that say nothing about which mod it is ("Definitive", "Replacer", "HD", "v2", "All in one").
_FILLER = {"the", "of", "and", "for", "an", "in", "one", "all", "by", "some", "mod", "replacer", "replacement",
           "definitive", "definative", "improved", "remastered", "retexture", "edition", "version", "hd"}
_VORTEX_TITLES = {}         # (path, mtime) -> {archive stem / mod folder name, casefolded: Nexus title}


def vortex_mod_titles(game="morrowind") -> dict[str, str]:
    """Each Vortex mod's Nexus title by its archive and mod-folder names, from Vortex's hourly state backup
    (%APPDATA%\\Vortex\\temp\\state_backups_full\\hourly.json). Empty when Vortex isn't installed.
    shortcut: Vortex users only; a Nexus search by name -> mod id -> file names would cover everyone."""
    path = Path(os.environ.get("APPDATA", "")) / "Vortex" / "temp" / "state_backups_full" / "hourly.json"
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {}
    if key not in _VORTEX_TITLES:
        titles = {}
        try:
            mods = json.loads(path.read_text(encoding="utf-8"))["persistent"]["mods"][game]
        except (OSError, ValueError, KeyError, TypeError):
            mods = {}
        for folder, mod in mods.items():
            attributes = mod.get("attributes") or {} if isinstance(mod, dict) else {}
            title = attributes.get("modName")
            if isinstance(title, str) and title.strip():
                for name in (folder, attributes.get("name"), Path(str(attributes.get("fileName") or "")).stem):
                    if name:
                        titles[str(name).casefold()] = title.strip()
        _VORTEX_TITLES.clear()
        _VORTEX_TITLES[key] = titles
    return _VORTEX_TITLES[key]


def _key_words(text: str) -> set[str]:
    words = set()
    for word in re.split(r"[^a-z0-9]+", text.casefold()):
        if not word or word.isdigit() or word in _FILLER or re.fullmatch(r"v\d+|\d+k", word):
            continue
        words.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)   # plants ~ plant
    return words


def names_mod(needed: str, name: str) -> bool:
    """Does this archive or staged folder name hold the mod an installer option names ("Epic Plants (mod)")?
    Its Nexus title from Vortex first (Epic Plants ships as "All-in-one"), else the name itself. Word match,
    ignoring punctuation, plurals, versions and filler; a patch never stands in for the mod itself."""
    want = _key_words(needed.removesuffix(" (mod)"))
    if not want:
        return False
    title = vortex_mod_titles().get(name.casefold()) or vortex_mod_titles().get(Path(name).stem.casefold())
    return any(_words_match(want, _key_words(text)) for text in (title, name) if text)


def _words_match(want: set[str], have: set[str]) -> bool:
    if "patch" in have and "patch" not in want:
        return False
    return want <= have or (len(have) >= 2 and have <= want)


def find_needed(needs, downloads: Path, index_path: Path, manager_dir: Path, progress=None) -> dict[str, Path | None]:
    """{needed plugin: the archive that holds it, or None}. Several archives hold it: the one whose name shares
    the most words with the plugin's (its own mod rather than a patch bundle that ships a copy), then the newest."""
    index = archive_index(Path(downloads), Path(index_path), core_addons._seven_zip(Path(manager_dir)), progress)
    found = {}
    for need in needs:
        if need.endswith(" (mod)"):            # an asset-only mod: no plugin to look for, so go by its name
            try:
                names = [n for n in os.listdir(downloads) if n.casefold().endswith(ARCHIVE_EXTS)]
            except OSError:
                names = []
            holders = [n for n in names if names_mod(need, Path(n).stem)]
            want = _key_words(need.removesuffix(" (mod)"))
            # the closest name (fewest extra words: the mod, not its textures or add-ons), then the newest
            found[need] = (Path(downloads) / min(holders, key=lambda n: (
                len(_key_words(Path(n).stem) - want), -os.stat(os.path.join(downloads, n)).st_mtime_ns))
                if holders else None)
            continue
        holders = [name for name, plugins in index.items() if any(p.casefold() == need.casefold() for p in plugins)]
        if not holders:
            found[need] = None
            continue
        want = _words(Path(need).stem)

        def rank(name):
            try:
                mtime = os.stat(os.path.join(downloads, name)).st_mtime_ns
            except OSError:
                mtime = 0
            return len(want & _words(name)), mtime

        found[need] = Path(downloads) / max(holders, key=rank)
    return found


def nexus_search_url(plugin: str) -> str:
    """A Nexus search for the mod a needed plugin comes from (its file name, words only)."""
    from urllib.parse import quote
    words = " ".join(re.split(r"[^A-Za-z0-9']+", Path(plugin).stem)).strip()
    return f"https://www.nexusmods.com/games/morrowind/mods?keyword={quote(words)}"


def search_on_nexus(plugin: str, downloads: Path) -> None:
    """Not in the downloads: open the embedded browser on a search for it; what the user downloads lands in
    the archive downloads folder, where the next lookup finds it."""
    import mwnexus_browser
    mwnexus_browser.open_page(nexus_search_url(plugin), download_dir=downloads)
