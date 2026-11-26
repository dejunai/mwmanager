"""Which Nexus mod an archive came from, when its name and its own metadata don't say.

    python nexus_ids.py resolve [DOWNLOADS_DIR] [--search api|none] [--only-missing] [--report FILE]
    python nexus_ids.py show [ARCHIVE_SUBSTRING]
    python nexus_ids.py set NAME (MOD_ID | none | personal) [--manager]   a manual answer; wins over everything else
    python nexus_ids.py personal                                 list the mods marked personal
    python nexus_ids.py rescan (ARCHIVE_NAME ... | --all)       forget cached answers so they are looked up again

Resolution order (the first that answers wins):
  1. a manual override (nexus_id_overrides.json);
  2. the archive name ("Mod-12345-1-0-1700000000.7z", "Mod 12345 1.0 2026-10-11T01-30Z abc.zip");
  3. the archive's own Vortex .meta file;
  4. Vortex's records, read-only: its download list and installed-mod list (state.v2, matched by file name, else by
     size and MD5), then the staged mod folder's meta.ini. Only for archives inside a Vortex downloads folder
     (one holding __vortex_downloads_folder);
  5. an older-style Nexus name ("Mod-12345-1-0.7z") when Vortex has no record of the file;
  6. a Nexus search by mod and plugin name (the public Nexus API, no key): accepted only when one mod's Files list
     has this exact file name, or this file's exact size and a similar name, or a close name and the same version;
  7. otherwise the archive is not a Nexus mod. That answer is remembered (nexus_ids.json) so it is not searched
     again every run, and the Nexus update check and page text are skipped for it. `set` or `rescan` changes it.
A search that could not run (offline, Nexus down) answers nothing and is not remembered.

Personal mods ("set NAME personal"): the user's own or hand-adjusted mods. They are never update- or currency-checked,
never searched on Nexus, shown as personal, and listed as not distributable in an exported profile. NAME is an archive
name, a staged mod folder or an MWSE mod name; it matches with or without the archive extension.

Manual answers live in the user's own nexus_id_overrides.json (%LOCALAPPDATA%\\MWManager, so they outlast a reset or
rollback of the MWManager folder); one in the MWManager folder (--manager) wins over it."""

from __future__ import annotations
import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
CACHE_NAME = "nexus_ids.json"
OVERRIDES_NAME = "nexus_id_overrides.json"
CACHE_VERSION = 1
GAME_DOMAIN = "morrowind"
GAME_ID_FALLBACK = 100
NEXUS_GRAPHQL = "https://api.nexusmods.com/v2/graphql"
DOWNLOADS_MARKER = "__vortex_downloads_folder"
NOT_NEXUS = "not_nexus"
PERSONAL = "personal"
UNRESOLVED = "unresolved"
CACHED_SOURCES = {"vortex_download", "vortex_mod", "vortex_staging", "nexus_search", NOT_NEXUS}

_STRICT = (re.compile(r"-(\d{3,7})-(?:[A-Za-z][A-Za-z0-9.]*|\d{1,3})(?:-(?:[A-Za-z][A-Za-z0-9.]*|\d{1,3}))*-\d{6,}$"),
           re.compile(r"-(\d{3,7})-(?:[\w.]+-)*\d{9,11}$"),
           re.compile(r"\s(\d{3,7})\s+\S+\s+\d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z(?:\s+\w+)?$"))
_LOOSE = (re.compile(r"-(\d{3,7})-[\w.\- ()\[\]]*$"),
          re.compile(r"-(\d{3,7})-?$"))
_DUPLICATE = re.compile(r"\s*\(\d+\)$")


class SearchUnavailable(RuntimeError):
    """The Nexus search could not run; the archive stays unresolved (not marked as a non-Nexus mod)."""


def _stem(name: str) -> str:
    stem = re.sub(r"\.(?:7z|zip|rar|tar|gz|bz2|xz)$", "", Path(str(name)).name, flags=re.I)
    return _DUPLICATE.sub("", stem)


def ids_from_name(name: str, loose: bool = False) -> tuple[str, str]:
    """(mod id, "") from a Nexus download name; ("", "") when it carries none. The number at the end of a Vortex
    download name is Nexus' upload time, not a file id, so no file id comes from a name. `loose` also reads the older
    'Name-12345-1-0' form, which plain names with a number in them can look like."""
    stem = _stem(name)
    for pattern in _STRICT:
        match = pattern.search(stem)
        if match:
            return match.group(1), ""
    if loose:
        for pattern in _LOOSE:
            match = pattern.search(stem)
            if match:
                return match.group(1), ""
    return "", ""


def _digits(value) -> str:
    text = str(value if value is not None else "").strip().strip('"')
    return text if text.isdigit() and int(text) > 0 else ""


def _meta_value(data, keys: set[str]):
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).casefold() in keys and value not in (None, ""):
                return value
        for value in data.values():
            found = _meta_value(value, keys) if isinstance(value, dict) else None
            if found is not None:
                return found
    return None


def archive_meta(archive: Path) -> dict:
    """The archive's own adjacent Vortex .meta file, when there is one."""
    for path in (archive.with_name(archive.name + ".meta"), archive.with_suffix(".meta")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}


def ids_from_meta(metadata: dict) -> tuple[str, str]:
    mod_id = _digits(_meta_value(metadata, {"modid", "mod_id", "nexusmodid"}))
    file_id = _digits(_meta_value(metadata, {"fileid", "file_id", "nexusfileid"}))
    return mod_id, (file_id if mod_id else "")


def in_vortex_downloads(archive: Path) -> bool:
    """True for an archive inside a Vortex downloads folder (Vortex keeps its marker file in the folder's root)."""
    try:
        parents = list(Path(archive).resolve().parents)[:3]
    except OSError:
        return False
    return any((parent / DOWNLOADS_MARKER).is_file() for parent in parents)


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class VortexIndex:
    """Vortex's own records of downloads and installed mods, read once (read-only) and looked up by archive."""

    def __init__(self, downloads=(), mods=(), staging: str | os.PathLike | None = None):
        self.downloads, self.mods = list(downloads), list(mods)
        self.staging = Path(staging) if staging else None
        self._by_name: dict[str, list[tuple[str, dict]]] = {}
        self._by_size: dict[int, list[tuple[str, dict]]] = {}
        for kind, rows in (("vortex_download", self.downloads), ("vortex_mod", self.mods)):
            for row in rows:
                name = Path(str(row.get("file") or row.get("archive") or "")).name.casefold()
                if name:
                    self._by_name.setdefault(name, []).append((kind, row))
                try:
                    size = int(row.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                if size:
                    self._by_size.setdefault(size, []).append((kind, row))

    @classmethod
    def load(cls, game: str = GAME_DOMAIN, state_dir=None) -> "VortexIndex":
        import vortex_state
        settings = vortex_state.read_prefix("settings###mods###installPath###", state_dir)
        staging = str(settings.get(f"settings###mods###installPath###{game}") or "").replace("{game}", game)
        return cls(vortex_state.downloads(game, state_dir), vortex_state.staged_mods(game, state_dir), staging or None)

    def lookup(self, archive: Path) -> tuple[str, str, str, str]:
        """(mod id, file id, source, detail), or ("", "", "", "") when Vortex has no ID for this file."""
        name = archive.name.casefold()
        for kind, row in self._by_name.get(name, []):
            if row.get("mod_id"):
                return row["mod_id"], row.get("file_id", ""), kind, f"Vortex record for {archive.name}"
        try:
            size = archive.stat().st_size
        except OSError:
            size = 0
        sized = [(k, r) for k, r in self._by_size.get(size, []) if r.get("mod_id") and r.get("md5")] if size else []
        if sized:
            try:
                md5 = _md5(archive)
            except OSError:
                md5 = ""
            for kind, row in sized:
                if md5 and row["md5"] == md5:
                    return row["mod_id"], row.get("file_id", ""), kind, \
                        f"Vortex record with the same size and MD5 ({Path(str(row.get('file') or row.get('archive'))).name})"
        folder = self._staged_folder(archive)
        if folder:
            mod_id = _meta_ini_id(folder)
            if mod_id:
                return mod_id, "", "vortex_staging", f"meta.ini in {folder.name}"
        return "", "", "", ""

    def _staged_folder(self, archive: Path) -> Path | None:
        stem = _stem(archive.name)
        for row in self.mods:
            if Path(str(row.get("archive") or "")).name.casefold() == archive.name.casefold():
                stem = str(row.get("folder") or stem)
                break
        if not self.staging:
            return None
        folder = self.staging / stem
        return folder if folder.is_dir() else None


def _meta_ini_id(folder: Path) -> str:
    import configparser
    path = folder / "meta.ini"
    if not path.is_file():
        return ""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str.lower
    try:
        parser.read(path, encoding="utf-8-sig")
        section = parser["General"] if parser.has_section("General") else parser.defaults()
        return _digits(section.get("modid") or section.get("nexusid"))
    except (OSError, UnicodeError, configparser.Error):
        return ""


# ---- Nexus search -------------------------------------------------------------------------------------------------

def search_terms(archive: Path, plugins=()) -> list[str]:
    """Mod names to search for: the archive name without its Nexus suffix, version and noise, then plugin names."""
    terms = []
    for raw in [_stem(archive.name)] + [Path(str(p)).stem for p in plugins or ()]:
        text = re.sub(r"\.(?:7z|zip|rar)\b", " ", raw, flags=re.I)
        text = re.sub(r"-\d{3,7}(?:-[\w.]*)*$", "", text)                         # "-12345-1-0" Nexus suffix
        text = re.sub(r"\s\d{3,7}\s+\S+\s+\d{4}-\d{2}-\d{2}T.*$", "", text)
        text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)                          # CamelCase
        text = re.sub(r"_(?!\d)|(?<!\d)_", " ", text)                             # keep "0_4" for the version
        text = re.sub(r"\b[vV]?\d+(?:[._]\d+)+[a-z]?\b|\b[vV]\d+[a-z]?\b", " ", text)
        text = text.replace("_", " ")
        text = re.sub(r"\b(?:eng|english|main file|complete|final|beta|alt ver)\b", " ", text, flags=re.I)
        text = re.sub(r"\b\d{6,8}\b", " ", text)                                  # build dates ("200814")
        text = re.sub(r"[\s\-.,()\[\]]+", " ", text).strip()
        if len(text) >= 3 and text.casefold() not in {t.casefold() for t in terms}:
            terms.append(text)
    return terms[:3]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text).casefold())


def similarity(a: str, b: str) -> float:
    """0..1: how alike two names are, by characters and by shared words."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    chars = difflib.SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio()
    shared = len(set(wa) & set(wb)) / max(len(set(wa)), len(set(wb)))
    return max(chars, shared)


def _version(text: str) -> tuple[int, ...]:
    found = re.findall(r"(?<![\d])v?(\d+(?:[._-]\d+)*)", str(text), flags=re.I)
    return tuple(int(n) for n in re.findall(r"\d+", found[-1])) if found else ()


def _archive_version(archive: Path) -> tuple[int, ...]:
    stem = _stem(archive.name).replace("_", " ")
    match = re.search(r"-\d{3,7}-((?:\d+-?)+)", stem)
    if match:
        return tuple(int(n) for n in re.findall(r"\d+", match.group(1)) if len(n) < 9)
    match = re.search(r"\b[vV]?(\d+(?:[._]\d+)+)\b", stem)
    return tuple(int(n) for n in re.findall(r"\d+", match.group(1))) if match else ()


def match_files(archive: Path, term: str, mod: dict, files: list[dict]) -> tuple[int, str, str]:
    """(confidence tier, file id, why) of one Nexus mod's Files list for this archive: 3 the same file name,
    2 the same size and a similar name, 1 a close name and the same version, 0 no match."""
    name = _DUPLICATE.sub("", Path(archive.name).stem).casefold() + archive.suffix.casefold()
    try:
        size = archive.stat().st_size
    except OSError:
        size = 0
    version = _archive_version(archive)
    best = (0, "", "")
    mod_sim = similarity(term, mod.get("name", ""))
    for item in files:
        uri = str(item.get("uri") or "")
        file_id = str(item.get("fileId") or "")
        if uri and uri.casefold() == name:
            return 3, file_id, f"Nexus file {uri}"
        file_sim = max(similarity(term, item.get("name", "")), similarity(_stem(archive.name), _stem(uri)))
        try:
            nexus_size = int(item.get("sizeInBytes") or 0)
        except (TypeError, ValueError):
            nexus_size = 0
        if size and nexus_size == size and max(file_sim, mod_sim) >= 0.6:
            best = max(best, (2, file_id, f"Nexus file {uri or item.get('name')} has the same size ({size:,} bytes)"))
        elif version and _version(item.get("version", "")) == version and max(file_sim, mod_sim) >= 0.85:
            best = max(best, (1, file_id, f"Nexus file {item.get('name')} {item.get('version')} (same version, "
                                          f"name {max(file_sim, mod_sim):.0%} alike)"))
    return best


class NexusSearch:
    """Searches Nexus (the public GraphQL API, no key) for an archive's mod. `query(document, variables)` is the one
    network call, replaceable in tests."""

    def __init__(self, query=None, timeout: float = 20, max_mods: int = 6):
        self.query = query or self._post
        self.timeout, self.max_mods = timeout, max_mods
        self._game_id = None

    def _post(self, document: str, variables: dict) -> dict:
        import urllib.error
        import urllib.request
        request = urllib.request.Request(
            NEXUS_GRAPHQL, data=json.dumps({"query": document, "variables": variables}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Application-Name": "MWManager",
                     "Application-Version": "0.1", "User-Agent": "MWManager/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise SearchUnavailable(f"Nexus search failed: {exc}") from exc
        if payload.get("errors"):
            raise SearchUnavailable(str(payload["errors"][0].get("message", "Nexus query failed")))
        return payload.get("data") or {}

    def game_id(self) -> int:
        if self._game_id is None:
            data = self.query('query { game(domainName: "%s") { id } }' % GAME_DOMAIN, {})
            self._game_id = int((data.get("game") or {}).get("id") or GAME_ID_FALLBACK)
        return self._game_id

    def mods(self, term: str) -> list[dict]:
        data = self.query("query($f: ModsFilter) { mods(filter: $f, count: 10) { nodes { modId name version } } }",
                          {"f": {"gameDomainName": [{"value": GAME_DOMAIN, "op": "EQUALS"}],
                                 "name": [{"value": term, "op": "WILDCARD"}]}})
        return list(((data.get("mods") or {}).get("nodes")) or [])

    def files(self, mod_id) -> list[dict]:
        data = self.query("query($m: ID!, $g: ID!) { modFiles(modId: $m, gameId: $g) "
                          "{ fileId name version uri sizeInBytes } }", {"m": str(mod_id), "g": str(self.game_id())})
        return list(data.get("modFiles") or [])

    def __call__(self, archive: Path, plugins=()) -> dict | None:
        """{"mod_id", "file_id", "detail"} for one confident match; None when nothing matches confidently."""
        terms = search_terms(archive, plugins)
        if not terms:
            return None
        candidates: dict[str, tuple[float, str, dict]] = {}
        for term in terms:
            words = term.split()
            queries = [term] + ([" ".join(words[:3])] if len(words) > 3 else [])
            for text in queries:
                for mod in self.mods(text):
                    mod_id = _digits(mod.get("modId"))
                    score = similarity(term, mod.get("name", ""))
                    if mod_id and score > candidates.get(mod_id, (-1, "", {}))[0]:
                        candidates[mod_id] = (score, term, mod)
        ranked = sorted(candidates.items(), key=lambda kv: -kv[1][0])[:self.max_mods]
        results = []
        for mod_id, (_score, term, mod) in ranked:
            tier, file_id, why = match_files(archive, term, mod, self.files(mod_id))
            if tier:
                results.append((tier, mod_id, file_id, f"{why} on Nexus mod {mod_id} ({mod.get('name')})"))
        if not results:
            return None
        top = max(r[0] for r in results)
        winners = {r[1] for r in results if r[0] == top}
        if len(winners) != 1:
            return None                                            # two mods match equally well: not confident
        tier, mod_id, file_id, detail = next(r for r in results if r[0] == top)
        return {"mod_id": mod_id, "file_id": file_id, "detail": detail}


# ---- cache, overrides and resolution -----------------------------------------------------------------------------

def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_cache(manager_dir) -> dict:
    data = _read(Path(manager_dir) / CACHE_NAME)
    return data.get("entries", {}) if data.get("version") == CACHE_VERSION else {}


def write_cache(manager_dir, entries: dict) -> None:
    _write(Path(manager_dir) / CACHE_NAME, {"version": CACHE_VERSION, "entries": entries})


def user_dir() -> Path | None:
    """The user's own MWManager data folder (%LOCALAPPDATA%\\MWManager), or None when there is no such variable."""
    base = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(base) / "MWManager" if base else None


def name_key(name: str) -> str:
    """How override names are compared: casefolded, without the archive extension or a " (1)" duplicate mark."""
    return _stem(str(name).strip().rstrip("\\/")).casefold()


def overrides(manager_dir=None) -> dict[str, str]:
    """{name key: mod id, "none" or "personal"} the user set: the user's own file, then the MWManager folder's."""
    found = {}
    for folder in (user_dir(), Path(manager_dir) if manager_dir else None):
        if folder is not None:
            found.update({name_key(k): str(v).strip().casefold() for k, v in _read(folder / OVERRIDES_NAME).items()})
    return found


def override_for(name, manager_dir=None, table: dict | None = None) -> str | None:
    return (overrides(manager_dir) if table is None else table).get(name_key(Path(str(name)).name))


def is_personal(name, manager_dir=None, table: dict | None = None) -> bool:
    """True for an archive, staged folder or mod name the user marked personal."""
    return bool(name) and override_for(name, manager_dir, table) == PERSONAL


def personal_names(manager_dir=None) -> list[str]:
    names = {}
    for folder in (user_dir(), Path(manager_dir) if manager_dir else None):
        if folder is not None:
            for key, value in _read(folder / OVERRIDES_NAME).items():
                if str(value).strip().casefold() == PERSONAL:
                    names.setdefault(name_key(key), str(key))
                else:
                    names.pop(name_key(key), None)
    return sorted(names.values(), key=str.casefold)


def set_override(manager_dir, archive_name: str, value: str | None) -> None:
    """Record a manual answer ("12345", "none" for not a Nexus mod, "personal" for the user's own mod) in
    manager_dir's nexus_id_overrides.json (user_dir() for the user's own file); None removes it."""
    path = Path(manager_dir) / OVERRIDES_NAME
    key = name_key(Path(archive_name).name)
    data = {str(k): v for k, v in _read(path).items() if name_key(k) != key}
    if value is not None:
        text = str(value).strip().casefold()
        if text not in {"none", PERSONAL} and not _digits(text):
            raise ValueError("a Nexus mod id is a number, 'none' or 'personal'")
        data[Path(archive_name).name] = text
    Path(manager_dir).mkdir(parents=True, exist_ok=True)
    _write(path, data)


def rescan(manager_dir, names=None) -> int:
    """Forget cached answers (all, or for these archive names) so the next resolve looks them up again."""
    entries = read_cache(manager_dir)
    keys = list(entries) if names is None else [Path(n).name.casefold() for n in names]
    dropped = sum(entries.pop(key, None) is not None for key in keys)
    write_cache(manager_dir, entries)
    return dropped


_VORTEX: dict[str, VortexIndex] = {}


def _vortex_for(archive: Path, vortex) -> VortexIndex | None:
    if vortex is not None:
        return vortex or None
    if not in_vortex_downloads(archive):
        return None
    if "default" not in _VORTEX:
        try:
            _VORTEX["default"] = VortexIndex.load()
        except Exception:                                          # Vortex state unreadable: no Vortex answers
            _VORTEX["default"] = VortexIndex()
    return _VORTEX["default"]


def _result(mod_id="", file_id="", source=UNRESOLVED, detail="") -> dict:
    return {"mod_id": mod_id, "file_id": file_id, "source": source, "detail": detail}


def resolve(archive, manager_dir=None, *, metadata: dict | None = None, plugins=(), vortex=None, search=None,
            cache: dict | None = None, save: bool = True) -> dict:
    """{"mod_id", "file_id", "source", "detail"} for one archive (see the module docstring for the order).
    `vortex`: a VortexIndex, False for none, None to read Vortex's own records for archives in its downloads folder.
    `search`: a callable(archive, plugins) like NexusSearch, or None to skip the network step (an archive nothing
    else answers then stays "unresolved" and is not remembered). `cache`: an open cache dict to share between calls
    (the caller saves it); otherwise the manager's cache file is read and, when `save`, written back."""
    archive = Path(archive)
    key = archive.name.casefold()
    manager = Path(manager_dir) if manager_dir else None
    override = override_for(archive.name, manager)
    if override == PERSONAL:
        return _result(source=PERSONAL, detail="a personal mod: no Nexus lookup, update check or sharing")
    if override:
        return (_result(source=NOT_NEXUS, detail="set by hand") if override == "none"
                else _result(override, "", "override", "set by hand"))
    mod_id, file_id = ids_from_name(archive.name)
    if mod_id:
        return _result(mod_id, file_id, "filename", archive.name)
    mod_id, file_id = ids_from_meta(archive_meta(archive) if metadata is None else metadata)
    if mod_id:
        return _result(mod_id, file_id, "archive_meta", "the archive's .meta file")
    own_cache = cache is None
    entries = (read_cache(manager) if manager else {}) if own_cache else cache
    try:
        size = archive.stat().st_size
    except OSError:
        size = None
    hit = entries.get(key)
    if hit and hit.get("source") in CACHED_SOURCES and (hit.get("size") in (None, size) or size is None):
        return _result(hit.get("mod_id", ""), hit.get("file_id", ""), hit["source"], hit.get("detail", ""))
    found = None
    index = _vortex_for(archive, vortex)
    if index:
        mod_id, file_id, source, detail = index.lookup(archive)
        if mod_id:
            found = _result(mod_id, file_id, source, detail)
    if not found:
        mod_id, _f = ids_from_name(archive.name, loose=True)
        if mod_id:
            return _result(mod_id, "", "filename_loose", archive.name)
    if not found and search is not None:
        try:
            hit = search(archive, plugins)
        except SearchUnavailable as exc:
            return _result(detail=str(exc))
        found = (_result(hit["mod_id"], hit.get("file_id", ""), "nexus_search", hit.get("detail", ""))
                 if hit else _result(source=NOT_NEXUS, detail="no Vortex record and no confident Nexus match"))
    if not found:
        return _result(detail="no Vortex record; Nexus not searched")
    entries[key] = dict(found, archive=archive.name, size=size, resolved_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    if own_cache and manager and save:
        write_cache(manager, entries)
    return found


def apply(item: dict, manager_dir, *, vortex=None, search=None, cache=None) -> dict:
    """Fill a scanned archive item's nexus_mod_id/nexus_file_id when it has none (or when a previous resolve filled
    them, so an override or rescan takes effect); records where the answer came from in nexus_id_source."""
    if is_personal(Path(str(item["archive"])).name, manager_dir):
        item.update(nexus_mod_id=None, nexus_file_id=None, nexus_id_source=PERSONAL, personal=True)
        return item
    item.pop("personal", None)
    if item.get("nexus_mod_id") and not item.get("nexus_id_source"):
        return item
    result = resolve(item["archive"], manager_dir, metadata=item.get("vortex_metadata") or {},
                     plugins=item.get("plugins") or (), vortex=vortex, search=search, cache=cache)
    item["nexus_mod_id"] = result["mod_id"] or None
    item["nexus_file_id"] = (result["file_id"] or item.get("nexus_file_id")) if result["mod_id"] else None
    item["nexus_id_source"] = result["source"]
    return item


def resolve_all(archives, manager_dir, *, vortex=None, search=None, plugins_by_archive=None) -> dict[str, dict]:
    """resolve() for many archives with one cache read and write."""
    entries = read_cache(manager_dir)
    results = {}
    for archive in archives:
        plugins = (plugins_by_archive or {}).get(Path(archive).name.casefold(), ())
        results[str(archive)] = resolve(archive, manager_dir, plugins=plugins, vortex=vortex, search=search,
                                        cache=entries)
    write_cache(manager_dir, entries)
    return results


def default_search(enabled: bool = True):
    """The Nexus search MWManager uses, or None when switched off (MWMANAGER_NEXUS_SEARCH=0)."""
    if not enabled or os.environ.get("MWMANAGER_NEXUS_SEARCH", "1").strip() in {"0", "off", "no"}:
        return None
    return NexusSearch()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("downloads", nargs="?", type=Path)
    r.add_argument("--search", choices=("api", "none"), default="none")
    r.add_argument("--only-missing", action="store_true", help="list only archives the name and .meta don't answer")
    r.add_argument("--report", type=Path)
    s = sub.add_parser("show")
    s.add_argument("needle", nargs="?", default="")
    o = sub.add_parser("set")
    o.add_argument("archive")
    o.add_argument("value")
    o.add_argument("--manager", action="store_true", help="write the MWManager folder's file, not the user's own")
    sub.add_parser("personal")
    c = sub.add_parser("rescan")
    c.add_argument("archives", nargs="*")
    c.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "set":
        target = APP if args.manager or user_dir() is None else user_dir()
        set_override(target, args.archive, None if args.value.casefold() == "clear" else args.value)
        print(f"{args.archive}: {args.value} ({target / OVERRIDES_NAME})")
        return 0
    if args.cmd == "personal":
        for name in personal_names(APP):
            print(name)
        return 0
    if args.cmd == "rescan":
        if not args.all and not args.archives:
            parser.error("name archives or use --all")
        print(f"forgot {rescan(APP, None if args.all else args.archives)} cached answer(s)")
        return 0
    if args.cmd == "show":
        for key, entry in sorted(read_cache(APP).items()):
            if args.needle.casefold() in key:
                print(f"{entry.get('archive', key)}: {entry.get('mod_id') or '-'} ({entry.get('source')}) "
                      f"{entry.get('detail', '')}")
        return 0
    downloads = args.downloads
    if downloads is None:
        try:
            import vortex_state
            template = vortex_state.read_prefix("settings###downloads###path").get("settings###downloads###path")
            downloads = Path(str(template).replace("{game}", GAME_DOMAIN)) if template else None
        except Exception:
            downloads = None
    if downloads is None or not downloads.is_dir():
        parser.error("give the downloads folder (Vortex's could not be read)")
    archives = sorted(p for p in downloads.iterdir() if p.is_file() and p.suffix.casefold() in {".7z", ".zip", ".rar"})
    results = resolve_all(archives, APP, search=NexusSearch() if args.search == "api" else None)
    counts: dict[str, int] = {}
    for path, result in results.items():
        counts[result["source"]] = counts.get(result["source"], 0) + 1
        if not args.only_missing or result["source"] not in {"filename", "archive_meta"}:
            print(f"{Path(path).name}: {result['mod_id'] or '-'} ({result['source']}) {result['detail']}")
    print("sources: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if args.report:
        args.report.write_text(json.dumps({"counts": counts, "results": results}, indent=1, ensure_ascii=False),
                               encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
