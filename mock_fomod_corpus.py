"""Corpus for measuring how well the mock FOMOD finds readme text for BAIN and folder options.

    python mock_fomod_corpus.py build --archives DIR     list every archive, extract only the docs of option packages
    python mock_fomod_corpus.py measure [--report FILE]  run the option-text matcher on the corpus and report

The corpus goes to outputs/mock_fomod_corpus (git-ignored): the docs are third-party text and are never committed.
tests/mock_fomod_golden.json holds hand-checked expectations; see MOCK_FOMOD_REGEX.MD.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "outputs" / "mock_fomod_corpus"
GOLDEN = HERE / "tests" / "mock_fomod_golden.json"
ARCHIVE_EXTS = (".7z", ".zip", ".rar")
OPTION_RE = re.compile(r"^(\d{1,3})(?:\s*[-_.:)]\s*|\s+)(.+)$")
DATA_DIRS = {"meshes", "textures", "icons", "sound", "sounds", "music", "bookart", "splash", "mwse", "fonts", "video",
             "distantland", "shaders", "data files"}
PLUGIN_EXTS = (".esp", ".esm", ".omwaddon", ".omwscripts", ".bsa")
DOC_EXTS = (".txt", ".md", ".pdf", ".html", ".htm", ".rtf", ".doc", ".docx", ".odt")
NOT_OPTIONS = {"docs", "doc", "documentation", "readme", "screenshots", "images", "__macosx", "source", "fomod"}
NESTED = {"optional", "optionals", "optional files", "options", "extras", "alternatives", "alternative", "addons",
          "add-ons", "patches", "compatibility"}


def _seven() -> str | None:
    from core_addons import _seven_zip
    return _seven_zip(HERE)


def _listing(archive: Path) -> list[tuple[str, int, bool]]:
    """(path, size, is_dir) for every entry, read with 7-Zip without extracting."""
    run = subprocess.run([_seven(), "l", "-ba", "-slt", str(archive)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    entries, current = [], None
    for line in run.stdout.splitlines():
        if line.startswith("Path = "):
            if current:
                entries.append(tuple(current))
            current = [line[7:].replace("\\", "/"), 0, False]
        elif current and line.startswith("Size = "):
            current[1] = int(line[7:] or 0)
        elif current and line.startswith("Attributes = "):
            current[2] = current[2] or "D" in line[13:].split(" ")[0]
        elif current and line.startswith("Folder = "):
            current[2] = current[2] or line[9:].strip() == "+"
    if current:
        entries.append(tuple(current))
    return entries


def _payload(paths) -> bool:
    for path in paths:
        parts = path.casefold().split("/")
        if parts[-1].endswith(PLUGIN_EXTS) or any(part in DATA_DIRS for part in parts[:-1]):
            return True
    return False


def classify(name: str, entries) -> dict:
    """The package's layout and its option folders, from the archive listing alone."""
    files = [path for path, _size, is_dir in entries if not is_dir]
    sizes = {path: size for path, size, _ in entries}
    record = {"archive": name, "kind": "simple", "options": []}
    if not files:
        record["kind"] = "empty"
        return record
    if any(f.casefold().endswith("fomod/moduleconfig.xml") for f in files):
        record["kind"] = "fomod"
        return record
    wrap = ""                                      # single wrapper folders, as Tidy would strip them
    while True:
        rest = [f[len(wrap):] for f in files]
        tops = {f.split("/")[0] for f in rest}
        top = next(iter(tops))
        if len(tops) == 1 and all("/" in f for f in rest) and top.casefold() not in DATA_DIRS:
            wrap += top + "/"
        else:
            break
    rel = [f[len(wrap):] for f in files]
    groups: dict[str, list[str]] = {}
    for f in rel:
        if "/" in f:
            groups.setdefault(f.split("/")[0], []).append(f)
    numbered = [g for g in groups if OPTION_RE.match(g)]
    unnumbered = [g for g, fs in groups.items() if g.casefold() not in DATA_DIRS | NOT_OPTIONS | NESTED
                  and _payload(fs)]
    nested = [g for g in groups if g.casefold() in NESTED]
    if len(numbered) >= 2:
        record["kind"], folders = "bain", numbered
    elif len(unnumbered) >= 2:
        record["kind"], folders = "multi", unnumbered
    elif nested:
        record["kind"] = "nested"
        folders = sorted({"/".join(f.split("/")[:2]) for g in nested for f in groups[g] if f.count("/") >= 2
                          and _payload([f]) and f.split("/")[1].casefold() not in DATA_DIRS | NOT_OPTIONS})
    else:
        return record
    for folder in sorted(folders, key=str.casefold):
        name_part = folder.rsplit("/", 1)[-1]
        match = OPTION_RE.match(name_part)
        record["options"].append({
            "folder": folder,
            "number": int(match.group(1)) if match else None,
            "label": match.group(2) if match else name_part,
            "plugins": sorted({f.rsplit("/", 1)[-1] for f in rel if f.startswith(folder + "/")
                               and f.casefold().endswith(PLUGIN_EXTS[:3])}),
        })
    record["wrap"] = wrap
    record["docs"] = [f for f in files if (f.casefold().endswith(DOC_EXTS) or "readme" in f.rsplit("/", 1)[-1].casefold())
                      and sizes.get(f, 0) < 3_000_000
                      and not f.casefold().rsplit("/", 1)[-1].startswith(("license", "licence"))]
    return record


def build(archives: Path) -> list[dict]:
    if not _seven():
        sys.exit("7-Zip was not found.")
    names = sorted(n for n in os.listdir(archives) if n.casefold().endswith(ARCHIVE_EXTS))
    docs_root = CORPUS / "docs"
    docs_root.mkdir(parents=True, exist_ok=True)

    def one(name):
        record = classify(name, _listing(archives / name))
        if record["options"] and record.get("docs"):
            folder = docs_root / hashlib.md5(name.encode("utf-8")).hexdigest()[:12]
            folder.mkdir(exist_ok=True)
            listfile = CORPUS / f"_{folder.name}.lst"
            listfile.write_text("\n".join(d.replace("/", "\\") for d in record["docs"]), encoding="utf-8")
            subprocess.run([_seven(), "x", "-y", f"-o{folder}", str(archives / name), f"@{listfile}", "-scsUTF-8"],
                           capture_output=True)
            listfile.unlink()
            record["doc_dir"] = folder.name
        return record

    with ThreadPoolExecutor(8) as pool:
        records = list(pool.map(one, names))
    (CORPUS / "index.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
    return records


def option_text(record: dict, option: dict) -> str:
    """What the mock FOMOD shows for this option today (the function under measurement)."""
    import mock_fomod
    if not record.get("doc_dir"):
        return ""
    text = mock_fomod._read_docs([CORPUS / "docs" / record["doc_dir"]])
    return mock_fomod._option_excerpts(text, record["options"])[option["folder"]]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u2013", "-").replace("\u2014", "-")).strip(" \t-*\u2022").casefold()


def measure(report: Path | None = None) -> dict:
    records = json.loads((CORPUS / "index.json").read_text(encoding="utf-8"))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.is_file() else {"packages": {}}
    by_name = {r["archive"]: r for r in records}
    stats = {"packages": 0, "documented": 0, "options": 0, "with_text": 0, "golden": 0, "golden_pass": 0,
             "golden_fail": []}
    lines = []
    for record in records:
        if not record["options"]:
            continue
        stats["packages"] += 1
        stats["documented"] += bool(record.get("doc_dir"))
        for option in record["options"]:
            text = option_text(record, option)
            stats["options"] += 1
            stats["with_text"] += bool(text)
            lines.append(f"{record['archive']} | {option['folder']} | {(text.splitlines() or [''])[0][:140]}")
    for archive, expected in golden["packages"].items():
        record = by_name.get(archive)
        for folder, want in expected.items():
            stats["golden"] += 1
            option = next((o for o in (record or {}).get("options", []) if o["folder"] == folder), None)
            got = _norm(option_text(record, option)) if option else ""
            ok = (not got) if want["expect"] is None else _norm(want["expect"]) in got
            if ok:
                stats["golden_pass"] += 1
            else:
                stats["golden_fail"].append(f"{archive} :: {folder}")
    if report:
        report.write_text("\n".join(lines), encoding="utf-8")
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--archives", type=Path, required=True, help="the archive downloads folder")
    m = sub.add_parser("measure")
    m.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        records = build(args.archives)
        from collections import Counter
        print(dict(Counter(r["kind"] for r in records)))
        print(sum(1 for r in records if r.get("doc_dir")), "option packages with docs extracted")
        return 0
    stats = measure(args.report)
    fails = stats.pop("golden_fail")
    print(json.dumps(stats))
    print("\n".join("FAIL " + f for f in fails))
    return 0


if __name__ == "__main__":
    sys.exit(main())
