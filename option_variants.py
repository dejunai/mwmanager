"""Option rules that need no per-mod knowledge (Dej, t87u/t88u).

Exclusive plugin variants in one folder
    Two or more ESP/ESM files directly in one content folder are one exclusive choice when
      * their names share a long prefix and differ only in a short variant tail
        ("TLAD Full - Still - Necro Colors" / "... - Original Colors"): the shared prefix, cut back to a word
        boundary, is at least 8 letters and at least 40% of every name, and each tail is at most four words;
      * no tail looks like a patch or add-on (patch, addon, add-on, compat, fix, for, with, plugin, master), and
        neither file lists the other as a master; and
      * there is strict evidence they replace each other: the readme or page says so ("choose one", "use only
        one", "either ... or", "not both", the names joined by "or"), or both headers list exactly the same
        masters and their records overlap (same record type and ID) by at least 60%.
    Patches and add-ons beside a main plugin (FGI_*_Patch.esp, "BTBGI Creature Buffs.esp") fail the name or the
    record test, so they keep loading together.

Optional plugins and warnings
    A plugin is optional when its file or folder name has the word "optional", or the docs or page call it an
    optional ESP/ESM/plugin. A sentence that warns against an option ("don't use", "not recommended", "outdated",
    "a better/more advanced version", "recommend ... instead") is shown first and the option starts unticked.
"""
from __future__ import annotations

import os
import re
import struct
from pathlib import Path

PLUGIN_SUFFIXES = {".esp", ".esm"}
_SEPARATORS = " -_.()[]"
_PATCH_TAIL = re.compile(r"\b(?:patch(?:es)?|addon|add[\s_-]?on|compat\w*|fix(?:es)?|for|with|plugin|master|esm)\b",
                         re.I)
_CHOOSE_ONE = re.compile(r"\b(?:choose|pick|select|use|install)\s+(?:only\s+)?one\b|\bmutually exclusive\b|"
                         r"\bnot both\b|\bdo(?:n'?t| not) use both\b|\beither\b[^.\n]{0,120}\bor\b", re.I)
_OPTIONAL_NAME = re.compile(r"(?:^|[^a-z])optional(?:[^a-z]|$)", re.I)
_OPTIONAL_DOC = re.compile(r"\boptional\s+(?:\w+\s+){0,2}(?:esp|esm|plugins?|file)\b", re.I)
WARNING_RE = re.compile(
    r"\b(?:do(?:n'?t| not)|never)\s+(?:use|install|enable|activate)\b|\bnot\s+recommended\b|\bnot\s+needed\s+anymore\b"
    r"|\b(?:outdated|obsolete|deprecated|superseded|abandoned)\b|\bno\s+longer\s+(?:needed|maintained|supported|updated)\b"
    r"|\b(?:much\s+|far\s+)?(?:better|more\s+advanced|improved|newer|superior)\s+(?:and\s+\w+\s+)?"
    r"(?:version|alternative|replacement|mod|option)\b"
    r"|\brecommend\w*\s+(?:\w+\s+){0,3}instead\b|\binstead\s+(?:use|using)\b|\buse\s+[^.\n]{2,80}?\s+instead\b", re.I)


def natural_key(text: str):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(text))]


def _letters(text: str) -> int:
    return len(re.sub(r"[^a-z0-9]", "", text.casefold()))


def _common_prefix(names: list[str]) -> str:
    folded = [n.casefold() for n in names]
    prefix = folded[0]
    for name in folded[1:]:
        size = 0
        while size < min(len(prefix), len(name)) and prefix[size] == name[size]:
            size += 1
        prefix = prefix[:size]
    if all(len(n) > len(prefix) for n in folded) or len(set(folded)) > 1:
        # cut back to a word boundary unless the shorter name ends exactly there
        cut = len(prefix)
        if not all(len(n) == cut or n[cut] in _SEPARATORS for n in folded):
            while cut and prefix[cut - 1] not in _SEPARATORS:
                cut -= 1
        prefix = prefix[:cut]
    return names[0][:len(prefix)]


def _tail(stem: str, prefix: str) -> str:
    return stem[len(prefix):].strip(_SEPARATORS)


def _variant_names(paths: list[Path]) -> tuple[str, list[str]] | None:
    stems = [p.stem for p in paths]
    prefix = _common_prefix(stems)
    if _letters(prefix) < 8 or any(_letters(prefix) * 5 < _letters(s) * 2 for s in stems):
        return None
    tails = [_tail(s, prefix) for s in stems]
    if len({t.casefold() for t in tails}) != len(tails) or sum(1 for t in tails if not t) > 1:
        return None
    if any(len(t.split()) > 4 or _PATCH_TAIL.search(t) for t in tails):
        return None
    return prefix, tails


def plugin_masters(path: Path) -> list[str]:
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
            if head[:4] != b"TES3":
                return []
            body = fh.read(struct.unpack("<I", head[4:8])[0])
    except OSError:
        return []
    masters, pos = [], 0
    while pos + 8 <= len(body):
        tag, size = body[pos:pos + 4], struct.unpack("<I", body[pos + 4:pos + 8])[0]
        if tag == b"MAST":
            masters.append(body[pos + 8:pos + 8 + size].split(b"\0")[0].decode("latin-1").casefold())
        pos += 8 + size
    return masters


def record_ids(path: Path, limit: int = 64 * 1024 * 1024) -> set[tuple[str, str]]:
    """(record type, ID) of every record; INFO by its INAM, an exterior CELL by its grid."""
    ids = set()
    try:
        data = Path(path).read_bytes()[:limit]
    except OSError:
        return ids
    pos = 0
    while pos + 16 <= len(data):
        rtype = data[pos:pos + 4].decode("latin-1")
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 16:pos + 16 + size]
        pos += 16 + size
        if rtype == "TES3":
            continue
        fields, sub = {}, 0
        while sub + 8 <= len(body):
            tag = body[sub:sub + 4]
            length = struct.unpack("<I", body[sub + 4:sub + 8])[0]
            fields.setdefault(tag, body[sub + 8:sub + 8 + length])
            sub += 8 + length
        name = fields.get(b"INAM" if rtype == "INFO" else b"NAME", b"").split(b"\0")[0]
        if rtype == "CELL" and not name and len(fields.get(b"DATA", b"")) >= 12:
            ident = "grid:" + fields[b"DATA"][4:12].hex()
        else:
            ident = name.decode("latin-1").casefold()
        ids.add((rtype, ident))
    return ids


def _records_overlap(paths: list[Path], threshold: float = 0.6) -> bool:
    sets = [record_ids(p) for p in paths]
    if not all(sets):
        return False
    base = sets[0]
    return all(len(base & other) / max(1, len(base | other)) >= threshold for other in sets[1:])


def _docs_say_exclusive(docs: str, paths: list[Path], tails: list[str]) -> bool:
    if not docs:
        return False
    folded = docs.casefold()
    named = sum(1 for p, t in zip(paths, tails) if p.name.casefold() in folded or p.stem.casefold() in folded
                or (t and t.casefold() in folded))
    if named < 2:
        return False
    if _CHOOSE_ONE.search(docs):
        return True
    words = [re.escape(t.casefold()) for t in tails if t]
    return len(words) >= 2 and bool(re.search(r"(?:%s)[^.\n]{0,60}\bor\b[^.\n]{0,60}(?:%s)" % (
        "|".join(words), "|".join(words)), folded))


def variant_groups(paths, docs: str = "") -> list[list[Path]]:
    """Groups of exclusive variants among plugins in one folder (each group sorted in natural order)."""
    plugins = sorted((Path(p) for p in paths if Path(p).suffix.casefold() in PLUGIN_SUFFIXES),
                     key=lambda p: natural_key(p.stem))
    by_folder: dict[str, list[Path]] = {}
    for p in plugins:
        by_folder.setdefault(os.path.normcase(str(p.parent)), []).append(p)
    groups = []
    for members in by_folder.values():
        # names that pair up as variants, joined into connected sets; each set must still read as one variant family
        parent = {p: p for p in members}

        def find(p):
            while parent[p] != p:
                p = parent[p]
            return p
        for i, left in enumerate(members):
            for right in members[i + 1:]:
                if _variant_names([left, right]):
                    parent[find(right)] = find(left)
        families: dict[Path, list[Path]] = {}
        for p in members:
            families.setdefault(find(p), []).append(p)
        for family in families.values():
            if len(family) < 2:
                continue
            shape = _variant_names(family)
            if shape is None:
                continue
            names = {p.name.casefold() for p in family}
            masters = [plugin_masters(p) for p in family]
            if any(set(m) & names for m in masters):
                continue                        # one is built on the other: an add-on, not a variant
            strict = _docs_say_exclusive(docs, family, shape[1]) or (
                all(m == masters[0] for m in masters) and _records_overlap(family))
            if strict:
                groups.append(sorted(family, key=lambda p: natural_key(p.stem)))
    return groups


def sentences(text: str) -> list[str]:
    out = []
    for line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip(" \t-*\u2022")
        if line:
            out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", line) if s.strip())
    return out


def _mentions(sentence: str, words: set[str], optional: bool) -> bool:
    folded = sentence.casefold()
    if optional and _OPTIONAL_DOC.search(sentence):
        return True
    return bool(words) and sum(1 for w in words if w in folded) >= min(2, len(words))


def option_words(name: str) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "this", "that", "data", "files", "plugin", "option", "optional",
            "esp", "esm", "mod", "version"}
    return {w for w in re.findall(r"[a-z0-9]{3,}", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).casefold())
            if w not in stop}


def recommended_variant(group: list[Path], texts) -> Path:
    """The variant the docs or page recommend; else the first in natural order."""
    prefix_tails = _variant_names(group)
    tails = prefix_tails[1] if prefix_tails else [p.stem for p in group]
    for text in texts:
        for sentence in sentences(text):
            if not re.search(r"\brecommend", sentence, re.I) or WARNING_RE.search(sentence):
                continue
            hits = [p for p, t in zip(group, tails) if (t and t.casefold() in sentence.casefold())
                    or p.stem.casefold() in sentence.casefold()]
            if len(hits) == 1:
                return hits[0]
    return group[0]


def is_optional_plugin(path: Path, texts=(), root: Path | None = None) -> bool:
    parts = [path.name]
    if root is not None:
        try:
            parts.extend(Path(path).relative_to(root).parts[:-1])
        except ValueError:
            pass
    if any(_OPTIONAL_NAME.search(Path(part).stem if part == path.name else part) for part in parts):
        return True
    for text in texts:
        for sentence in sentences(text):
            if _OPTIONAL_DOC.search(sentence) and (path.name.casefold() in sentence.casefold()
                                                   or path.stem.casefold() in sentence.casefold()
                                                   or not re.search(r"\.(?:esp|esm)\b", sentence, re.I)):
                return True
    return False


def warning_for(name: str, texts, optional: bool = False) -> str:
    """The first sentence warning against this option, quoted as written; '' when there is none.

    The sentence counts when it, or the sentence just before it, speaks of this option (two of its name's words,
    or "optional ESP/plugin" for an optional plugin)."""
    words = option_words(name)
    for text in texts:
        found = sentences(text)
        for i, sentence in enumerate(found):
            if not WARNING_RE.search(sentence):
                continue
            if _mentions(sentence, words, optional) or (i and _mentions(found[i - 1], words, optional)):
                return sentence
    return ""


def about_sentences(name: str, texts, optional: bool = False, limit: int = 2, skip: str = "") -> list[str]:
    """Sentences that speak of this option, in reading order (the warning itself left out)."""
    words = option_words(name)
    texts = list(texts)
    candidates = [s for text in texts for s in sentences(_prose(text)) if s != skip]
    found = []
    # A sentence that names the option itself ("optional ESP" for an optional plugin) is used before one that only
    # names the mod, which is a fallback; within each pass, reading order.
    passes = ([lambda s: bool(_OPTIONAL_DOC.search(s))] if optional else []) + [lambda s: _mentions(s, words, optional)]
    for test in passes:
        for sentence in candidates:
            if not _said(sentence, found + [skip]) and test(sentence):
                found.append(sentence)
                if len(found) >= limit:
                    return found
        if found:
            return found  # the weaker pass only fills in when the stronger one found nothing
    return found


def _said(sentence: str, found) -> bool:
    """True when the sentence repeats one already taken (same words, or one inside the other)."""
    key = " ".join(re.findall(r"[a-z0-9]+", sentence.casefold()))
    for other in found:
        other_key = " ".join(re.findall(r"[a-z0-9]+", str(other or "").casefold()))
        if key and other_key and (key in other_key or other_key in key):
            return True
    return False


def _prose(text) -> str:
    """Text without markdown headings and code lines, so a description is built from sentences only."""
    try:
        from mwnexus_parse import strip_code_lines
        text = strip_code_lines(str(text or ""))
    except Exception:
        text = str(text or "")
    return "\n".join(line for line in text.splitlines() if not re.match(r"\s*(?:#{1,6}\s|```|=+$|-{3,}$)", line))
