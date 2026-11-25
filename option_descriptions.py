"""Keep BAIN/folder option descriptions current: readme text first, then the Nexus mod page, both cleaned by regex.

    python option_descriptions.py update [--fetch cache|browser|http] [--refresh-days N] [--limit N] [--report FILE]
                                         [--archives DIR]
    python option_descriptions.py show [ARCHIVE_SUBSTRING]

The packages come from the mock-FOMOD corpus (outputs/mock_fomod_corpus/index.json; build it with
`python mock_fomod_corpus.py build --archives DIR`). For each option, the readme excerpt the option window shows
(mock_fomod's matcher) is the first source. An option it leaves empty gets the mod page's best sentences
(mwnexus_parse.best_option_sentences) from the page text:

- cache (default): only pages the MWManager browser already read (LOCALAPPDATA\\MWManager\\NexusBrowser\\pages);
- browser: also reads missing or stale pages in the MWManager browser (it must be open; Nexus security checks are
  left to the user there);
- http: also tries a plain page fetch (Nexus often answers it with a security check, which is reported, not passed).

Every chosen text goes through refine() (BBCode, markdown, links, page chrome, changelog lines, length). The result
is outputs/option_descriptions.json, keyed by archive and option folder; a run keeps entries whose source text did
not change and reports what was added or changed. Pages older than --refresh-days are read again (browser/http).

An archive whose name has no Nexus id is looked up with nexus_ids (Vortex's records for the file in --archives,
default Vortex's downloads folder; with browser/http also a Nexus search). One that is not a Nexus mod gets no page."""
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
OUT = APP / "outputs" / "option_descriptions.json"
MAX_CHARS = 600

_BB_LINK = re.compile(r"\[url=[^\]]*\](.*?)\[/url\]", re.I | re.S)
_BB_TAG = re.compile(r"\[/?(?:b|i|u|s|size|color|font|center|left|right|quote|spoiler|list|\*|line|img|youtube|"
                     r"heading|code|url)(?:=[^\]]*)?\]", re.I)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]*\)")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_MD_EMPH = re.compile(r"(\*\*|__|~~|`)")
_CHROME = re.compile(r"^(?:endorse(?:ments?)?|track|download|manual(?: download)?|mod manager(?: download)?|vortex|"
                     r"report(?: abuse)?|share|like|log ?in|sign ?up|unique dls?|total dls?|total views|version|"
                     r"last updated|original upload|created by|uploaded by|virus scan|preview file contents|"
                     r"\d[\d,.]*\s*(?:kb|mb|gb|bytes)|\d{1,2} \w{3} \d{4}(?:, \d{1,2}:\d{2}(?:am|pm)?)?)\b.{0,30}$", re.I)
_CHANGELOG = re.compile(r"^(?:v(?:ersion)?\s*\d+(?:\.\d+)+\b|\d+(?:\.\d+)+\s*[:-]|changelog|version history)", re.I)
_LEAD = re.compile(r"^[\s\-*\u2022\u2013\u2014>#]+")


def refine(text: str, limit: int = MAX_CHARS) -> str:
    """Readable option text: markup and links reduced to their words, page chrome and changelog lines dropped,
    repeated lines once, cut at a sentence end near `limit` characters."""
    text = _BB_LINK.sub(r"\1", str(text or ""))
    text = _MD_LINK.sub(r"\1", text)
    text = _BB_TAG.sub("", text)
    text = _URL.sub("", text)
    text = _MD_EMPH.sub("", text)
    out, seen = [], set()
    in_changelog = False
    for raw in text.splitlines():
        line = _LEAD.sub("", re.sub(r"[ \t\u00a0\u200b\ufeff]+", " ", raw)).strip()
        line = re.sub(r"\(\s*\)", "", line).strip(" ,;")
        if _CHANGELOG.match(line):
            in_changelog = True         # the entry's indented or bulleted continuation lines go with it
            continue
        if in_changelog and line and (raw[:1] in " \t" or re.match(r"\s*[-*\u2022]", raw)):
            continue
        if line:
            in_changelog = False
        if len(line) < 3 or _CHROME.match(line):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    result = "\n".join(out)
    if len(result) > limit:
        cut = result[:limit]
        end = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("! "), cut.rfind("? "))
        result = (cut[:end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + " …").strip()
    return result


def nexus_id(archive: str) -> str:
    """Nexus mod id from a download's name: 'AURA 6.0.0-48255-6-0-0-1779801776.7z' or the newer
    'Almalexia'S Chamber Overhaul 52737 1.95 2026-10-11T01-30Z OmEgmfDk.zip'."""
    stem = re.sub(r"\.(?:7z|zip|rar)$", "", archive, flags=re.I)
    m = re.search(r"-(\d{3,6})-(?:[\w.]+-)*\d{9,11}$", stem)
    if m:
        return m.group(1)
    m = re.search(r"\s(\d{3,6})\s+\S+\s+\d{4}-\d{2}-\d{2}T", stem)
    return m.group(1) if m else ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def page_text(mod_id: str, fetch: str, refresh_days: float, log=print) -> tuple[str, float | None, str]:
    """(text, cached_at, problem) for a mod page, from the browser cache or a new read."""
    import mwnexus_browser
    cached = mwnexus_browser.cached_page(mod_id) if mod_id else None
    fresh = cached and time.time() - float(cached.get("cached_at") or 0) < refresh_days * 86400
    if cached and (fresh or fetch == "cache"):
        return str(cached.get("text", "")), cached.get("cached_at"), ""
    if not mod_id or fetch == "cache":
        return "", None, "" if mod_id else "no Nexus id in the archive name"
    url = f"https://www.nexusmods.com/morrowind/mods/{mod_id}"
    if fetch == "browser":
        try:
            import mock_fomod
            page = mock_fomod._capture_with_browser(mwnexus_browser, mod_id, url)
            mwnexus_browser.cache_page(mod_id, page)
            return str(page.get("text", "")), time.time(), ""
        except (RuntimeError, ValueError, OSError) as exc:
            log(f"  {mod_id}: browser read failed: {exc}")
            return (str(cached.get("text", "")), cached.get("cached_at"), str(exc)) if cached else ("", None, str(exc))
    import urllib.error
    import urllib.request
    from mwnexus_parse import html_to_text, is_challenge
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MWManager option descriptions"})
        with urllib.request.urlopen(req, timeout=20) as r:
            markup = r.read(800_000).decode("utf-8", "replace")
        title = (re.search(r"(?is)<title>(.*?)</title>", markup) or [None, ""])[1]
        text = html_to_text(markup)
        if is_challenge(title, text):
            return "", None, "Nexus answered with a security check (complete it in the MWManager browser)"
        return text, time.time(), ""
    except urllib.error.HTTPError as exc:
        return "", None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "", None, f"fetch failed: {exc}"


def _resolver(archives, fetch: str):
    """archive name -> (mod id, source) for names without an id: nexus_ids with its cache, Vortex's records for files
    in `archives`, and a Nexus search when pages may be fetched."""
    import nexus_ids
    cache = nexus_ids.read_cache(APP)
    search = nexus_ids.default_search() if fetch != "cache" else None
    folder = Path(archives) if archives else None

    def lookup(name: str) -> tuple[str, str]:
        result = nexus_ids.resolve((folder / name) if folder else Path(name), APP, vortex=None if folder else False,
                                   search=search if folder else None, cache=cache)
        return result["mod_id"], result["source"]
    lookup.save = lambda: nexus_ids.write_cache(APP, cache) if cache else None
    return lookup


def update(fetch="cache", refresh_days=14.0, limit=None, out=OUT, corpus=None, log=print, delay=2.0,
           archives=None, resolver=None) -> dict:
    import mock_fomod_corpus as corpus_mod
    from mwnexus_parse import best_option_sentences
    corpus = Path(corpus) if corpus else corpus_mod.CORPUS
    corpus_mod.CORPUS = corpus
    records = json.loads((corpus / "index.json").read_text(encoding="utf-8"))
    old = json.loads(Path(out).read_text(encoding="utf-8")) if Path(out).is_file() else {"options": {}}
    entries = {}
    stats = {"packages": 0, "options": 0, "readme": 0, "nexus": 0, "empty": 0, "refined": 0, "added": 0,
             "changed": 0, "kept": 0, "pages_read": 0, "page_problems": {}}
    samples = []
    reads = 0
    lookup = resolver or _resolver(archives, fetch)
    import nexus_ids
    manual = nexus_ids.overrides(APP)
    stats["not_nexus"] = stats["personal"] = 0
    for record in records:
        if not record.get("options"):
            continue
        stats["packages"] += 1
        mod_id, id_source = nexus_id(record["archive"]), "filename"
        if nexus_ids.is_personal(record["archive"], table=manual):
            mod_id, id_source = "", "personal"
        elif not mod_id:
            mod_id, id_source = lookup(record["archive"])
        texts = {o["folder"]: corpus_mod.option_text(record, o) for o in record["options"]}
        page, cached_at = "", None
        if id_source in ("not_nexus", "personal"):
            stats[id_source] += 1
            if any(not t.strip() for t in texts.values()):
                stats["page_problems"][record["archive"]] = ("a personal mod" if id_source == "personal"
                                                             else "not a Nexus mod")
        elif any(not t.strip() for t in texts.values()):
            if limit is None or reads < limit:
                before = time.time()
                page, cached_at, problem = page_text(mod_id, fetch, refresh_days, log)
                if problem:
                    stats["page_problems"][record["archive"]] = problem
                if cached_at and cached_at >= before:
                    reads += 1
                    stats["pages_read"] += 1
                    time.sleep(delay)
        for option in record["options"]:
            folder = option["folder"]
            raw, source = texts[folder].strip(), "readme"
            if not raw and page:
                raw, source = "\n".join(best_option_sentences(page, folder)), "nexus"
            if not raw:
                source = ""
            text = refine(raw)
            stats["options"] += 1
            stats[source or "empty"] += 1
            stats["refined"] += bool(raw) and text != raw
            key = f"{record['archive']} | {folder}"
            entry = {"archive": record["archive"], "folder": folder, "nexus_id": mod_id, "source": source,
                     "text": text, "raw_sha": _sha(raw), "page_cached_at": cached_at if source == "nexus" else None}
            prev = old["options"].get(key)
            if prev and prev.get("raw_sha") == entry["raw_sha"] and prev.get("text") == text:
                entry["updated_at"] = prev.get("updated_at")
                stats["kept"] += 1
            else:
                entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                stats["changed" if prev else "added"] += 1
            entries[key] = entry
            if raw and text != raw and len(samples) < 12:
                samples.append({"option": key, "source": source, "before": raw[:400], "after": text[:400]})
    getattr(lookup, "save", lambda: None)()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(out).with_name(Path(out).name + ".tmp")
    tmp.write_text(json.dumps({"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "options": entries}, indent=1,
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    stats["samples"] = samples
    return stats


def _vortex_downloads():
    try:
        import vortex_state
        template = vortex_state.read_prefix("settings###downloads###path").get("settings###downloads###path")
    except Exception:
        return None
    folder = Path(str(template).replace("{game}", "morrowind")) if template else None
    return folder if folder and folder.is_dir() else None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update")
    u.add_argument("--fetch", choices=("cache", "browser", "http"), default="cache")
    u.add_argument("--refresh-days", type=float, default=14.0)
    u.add_argument("--limit", type=int, help="read at most N pages this run")
    u.add_argument("--report", type=Path, help="also write the stats and samples as JSON")
    u.add_argument("--archives", type=Path, help="the downloads folder (default: Vortex's) for looking up "
                                                 "archives whose name has no Nexus id")
    s = sub.add_parser("show")
    s.add_argument("match", nargs="?", default="")
    a = p.parse_args(argv)
    if a.cmd == "show":
        data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.is_file() else {"options": {}}
        for key, e in data["options"].items():
            if a.match.casefold() in key.casefold():
                print(f"{key} [{e['source'] or '-'}]\n    " + (e["text"].replace("\n", "\n    ") or "(none)"))
        return 0
    stats = update(a.fetch, a.refresh_days, a.limit, archives=a.archives or _vortex_downloads())
    if a.report:
        a.report.write_text(json.dumps(stats, indent=1, ensure_ascii=False), encoding="utf-8")
    samples = stats.pop("samples")
    problems = stats.pop("page_problems")
    print(json.dumps(stats))
    print(f"{len(problems)} page(s) not read; first: {next(iter(problems.items()), None)}")
    for s_ in samples[:5]:
        print(f"--- {s_['option']} ({s_['source']})\n  before: {s_['before'][:200]!r}\n  after:  {s_['after'][:200]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
