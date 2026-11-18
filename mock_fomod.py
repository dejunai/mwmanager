"""BAIN-style numeric option selection for staged mods without an installed FOMOD installer.

The selector handles mandatory 00 groups, exclusive same-numbered groups, optional numbered
groups, documentation previews, plugin-master compatibility defaults, and later reevaluation.
"""
from __future__ import annotations

import json
import fnmatch
import os
import re
import shutil
import struct
import time
import sys
import html
import zipfile
import configparser
import urllib.request
import urllib.parse
import webbrowser
import threading
import hashlib
import subprocess
import tempfile
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from pathlib import Path

# BAIN packages use numeric prefix folder spellings ("00 - Core", "00 Core", "00_Core", "000 - Core").
OPTION_RE = re.compile(r"^(\d{1,3})(?:\s*[-_.:)]\s*|\s+)(.+)$")
DOC_EXTS = {".txt", ".md", ".rtf", ".pdf", ".docx", ".doc", ".html", ".htm"}
DOC_LIMIT = 200_000                      # characters kept per doc; only the excerpt shown is short
GAME_DIRS = {"meshes", "textures", "icons", "sound", "music", "bookart", "splash", "fonts",
             "video", "distantland", "mwse", "mge3", "shaders", "grass", "mcpatch"}
GAME_DIRS.add("sounds")  # Some packages pluralize Sound; activation normalizes it below.
PLUGIN_EXTS = {".esp", ".esm", ".omwaddon"}
PAYLOAD_EXTS = PLUGIN_EXTS | {".bsa", ".toml"}
PLUGIN_REF_RE = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:esm|esp|omwaddon)\b", re.I)
BASE_GAME_MASTERS = {"morrowind.esm", "tribunal.esm", "bloodmoon.esm"}
STAGING_RESERVED = {"off", "docs", "extras", "openmw", "bugreports", "screenshots", "grass"}
_NEXUS_TEXT_CACHE: dict[str, str] = {}
_NEXUS_FETCH_ERRORS: dict[str, str] = {}
_FOMOD_IMAGE_INDEX: dict[str, dict[str, list[Path]]] = {}


class OptionsCancelled(RuntimeError):
    """The user closed an unanswered selector; never mark this as accepted."""

# Precompiled helper patterns for fast option evaluation
EXCLUSIVE_DOC_RE = re.compile(
    r"\b(?:mutually exclusive|choose (?:only )?one|select (?:only )?one|"
    r"do not use both|don't use both|either [^.\n\r]{0,100}\bor)\b", re.I
)
PATCH_LIKE_RE = re.compile(r"\b(?:compat|patch|addon|add-on|for|with)\b", re.I)
NORMAL_MAP_RE = re.compile(r"\bnormal[\s_-]*maps?\b|\bnormals\b", re.I)
OPENMW_RE = re.compile(r"\bopen\s*mw\b|\bomw\b", re.I)
MWSE_RE = re.compile(r"\bmwse\b", re.I)
MGE_XE_RE = re.compile(r"\bmge\s*xe\b|\bmge3\b", re.I)
CORE_STEM_RE = re.compile(r"(?:^|[\s_-])core(?:[\s_-]|$)", re.I)
CHALLENGE_RE = re.compile(r"captcha|verify you are human|are you a robot|access denied|cloudflare", re.I)
HTML_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


def _package_display_name(mod_dir: Path) -> str:
    """Use the processing identity, staged primary master, Vortex name, then the folder name."""
    established = os.environ.get("MWMANAGER_PLUGIN_NAME", "").strip()
    if established:
        return re.sub(r'[<>:"/\\|?*]', "_", established).rstrip(" .") or mod_dir.name
    try:
        marker = json.loads((mod_dir / ".mwmanager-stage.json").read_text(encoding="utf-8"))
        marker_name = str(marker.get("PLUGIN_NAME") or "").strip()
        if marker_name:
            return re.sub(r'[<>:"/\\|?*]', "_", marker_name).rstrip(" .") or mod_dir.name
        masters = marker.get("selected_masters", [])
        if masters:
            return re.sub(r'[<>:"/\\|?*]', "_", Path(masters[0]).stem).rstrip(" .") or mod_dir.name
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    candidates = [mod_dir / "meta.ini"]
    extras = next((p for p in mod_dir.iterdir() if p.is_dir() and p.name.casefold() == "extras"), None)
    if extras:
        candidates.extend(p for p in _safe_rglob(extras) if p.is_file() and p.name.casefold() == "meta.ini")
    for path in candidates:
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(path, encoding="utf-8-sig")
            section = parser["General"] if parser.has_section("General") else parser.defaults()
            value = section.get("name", "").strip()
            if value:
                return re.sub(r'[<>:"/\\|?*]', "_", value).rstrip(" .") or mod_dir.name
        except (OSError, configparser.Error, UnicodeError):
            continue
    name = mod_dir.name
    for pattern in (r"^(?P<name>.+?) \d{2,7} \S+ \d{4}-\d\d-\d\dT\d\d-\d\dZ \w{6,10}$",
                    r"^(?P<name>.+?)-\d{2,7}(?:-[0-9A-Za-z.]+)*$"):
        match = re.match(pattern, name)
        if match:
            name = match.group("name")
            break
    return name.strip(" .") or mod_dir.name


def _search_mod_title(mod_dir: Path, fallback: str) -> str:
    """Remove Vortex download/version decorations while keeping descriptive title suffixes."""
    name = Path(mod_dir).name or fallback
    # Staged archive folders commonly look like "Title 1.2-12345-1-2-... [3] Optional Patch".
    name = re.sub(r"\s+\d+(?:\.\d+)+(?:-[A-Za-z0-9.]+)*(?:\s+\[\d+\])?", " ", name, count=1)
    # Also remove retry/index suffixes when there is no version token to anchor the pattern.
    name = re.sub(r"\s+\[\d+(?:\s+retry\s+\d+)?\]", " ", name, flags=re.I)
    name = re.sub(r"\s+\d+\s+retry\s+\d+\b", " ", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" -_.")
    return name or fallback.strip()


def _safe_rglob(root: Path):
    """Walk a package tree without letting one unreadable archive directory abort install."""
    root = Path(root)
    if not root.is_dir():
        return

    def onerror(exc):
        print(f"  Skipping unreadable package path {exc.filename}: {exc}", file=sys.stderr)

    for base, dirs, files in os.walk(root, onerror=onerror):
        for name in dirs:
            yield Path(base) / name
        for name in files:
            yield Path(base) / name


def _plugin_masters(path: Path) -> set[str]:
    """Read MAST subrecords from a TES3 header, returning case-folded plugin filenames."""
    try:
        data = path.read_bytes()
        if len(data) < 16 or data[:4] != b"TES3":
            return set()
        size = struct.unpack_from("<I", data, 4)[0]
        end = min(16 + size, len(data))
        pos, result = 16, set()
        while pos + 8 <= end:
            tag = data[pos:pos + 4]
            length = struct.unpack_from("<I", data, pos + 4)[0]
            pos += 8
            if length > end - pos:
                break
            value = data[pos:pos + length]
            if tag == b"MAST":
                result.add(value.split(b"\0", 1)[0].decode("cp1252", "replace").casefold())
            pos += length
        return result
    except (OSError, struct.error):
        return set()


def _has_fomod(mod_dir: Path) -> bool:
    for base, dirs, files in os.walk(mod_dir):
        dirs[:] = [d for d in dirs if d.casefold() != "off"]
        if Path(base).name.casefold() == "fomod" and any(f.casefold() == "moduleconfig.xml" for f in files):
            return True
    return False


def _fomod_config(mod_dir: Path) -> Path | None:
    for base, dirs, files in os.walk(mod_dir):
        dirs[:] = [d for d in dirs if d.casefold() != "off"]
        if Path(base).name.casefold() == "fomod":
            config = next((Path(base) / f for f in files if f.casefold() == "moduleconfig.xml"), None)
            if config:
                return config
    return None


def _fomod_image_path(mod_dir: Path, config: Path, image_path: str | None) -> Path | None:
    """Resolve a FOMOD image, recovering it if an earlier Tidy pass misplaced it."""
    if not image_path:
        return None
    source_root = config.parent.parent if config.parent.name.casefold() == "fomod" else config.parent
    relative = Path(image_path.replace("\\", os.sep))
    candidate = (source_root / relative).resolve()
    try:
        candidate.relative_to(mod_dir.resolve())
    except ValueError:
        return None
    if candidate.is_file():
        return candidate

    # Tidy has previously moved FOMOD sample images into Screenshots, and older
    # layouts may have nested the source tree under another Extras/package pair.
    # Match the longest trailing path, not just the basename, to disambiguate.
    root_key = str(mod_dir.resolve()).casefold()
    index = _FOMOD_IMAGE_INDEX.get(root_key)
    if index is None:
        index = {}
        for base, dirs, files in os.walk(mod_dir):
            dirs[:] = [name for name in dirs if name.casefold() not in {"off", "bugreports"}]
            for name in files:
                if Path(name).suffix.casefold() in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}:
                    index.setdefault(name.casefold(), []).append(Path(base) / name)
        _FOMOD_IMAGE_INDEX[root_key] = index
    requested = tuple(part.casefold() for part in relative.parts)
    candidates = index.get(relative.name.casefold(), [])
    if not candidates:
        return None

    def match_rank(path: Path):
        actual = tuple(part.casefold() for part in path.relative_to(mod_dir).parts)
        suffix = 0
        for wanted, found in zip(reversed(requested), reversed(actual)):
            if wanted != found:
                break
            suffix += 1
        return suffix, -len(actual), str(path).casefold()

    best = max(candidates, key=match_rank)
    return best if match_rank(best)[0] else None


def _load_fomod_preview(path: Path, tk, max_size=(360, 240)):
    """Load a FOMOD image for Tk, converting JPEG/other formats through ffmpeg if needed."""
    path = Path(path)
    max_width, max_height = max_size
    try:
        image = tk.PhotoImage(file=str(path))
    except Exception:
        image = None
    if image is None:
        try:
            from PIL import Image, ImageTk
            with Image.open(path) as source:
                source.thumbnail((max_width, max_height))
                return ImageTk.PhotoImage(source.copy())
        except Exception:
            pass
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        try:
            stat = path.stat()
            key = hashlib.sha256(
                f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{max_width}x{max_height}".encode()
            ).hexdigest()[:24]
            converted = Path(tempfile.gettempdir()) / "MWManager" / "fomod_previews" / f"{key}.png"
            if not converted.is_file():
                converted.parent.mkdir(parents=True, exist_ok=True)
                quiet = {}
                if os.name == "nt":
                    startup = subprocess.STARTUPINFO()
                    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startup.wShowWindow = subprocess.SW_HIDE
                    quiet = {"startupinfo": startup,
                             "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
                result = subprocess.run(
                    [ffmpeg, "-loglevel", "error", "-y", "-i", str(path), "-vf",
                     f"scale={max_width}:{max_height}:force_original_aspect_ratio=decrease",
                     "-frames:v", "1", str(converted)],
                    capture_output=True, timeout=30, **quiet)
                if result.returncode or not converted.is_file():
                    return None
            image = tk.PhotoImage(file=str(converted))
        except (OSError, subprocess.SubprocessError, tk.TclError):
            return None
    try:
        factor = max(1, (image.width() + max_width - 1) // max_width,
                     (image.height() + max_height - 1) // max_height)
        return image.subsample(factor, factor) if factor > 1 else image
    except Exception:
        return image


def _xml_local(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element.iter() if child.tag.rsplit("}", 1)[-1].casefold() == name.casefold()]


def _fomod_payload(mod_dir: Path, plugin: ET.Element, payload_root: Path | None = None) -> dict[str, Path]:
    """Resolve a FOMOD plugin's file and folder declarations to safe staged payload paths."""
    payload: dict[str, Path] = {}
    source_root = payload_root or mod_dir
    for element in plugin.iter():
        tag = element.tag.rsplit("}", 1)[-1].casefold()
        if tag not in {"file", "folder"}:
            continue
        source_text = element.attrib.get("source", "")
        destination_text = element.attrib.get("destination", "")
        source = (source_root / Path(source_text.replace("\\", os.sep))).resolve()
        try:
            source.relative_to(source_root.resolve())
        except ValueError:
            continue
        if not source.exists() and source_root != mod_dir:
            # Tidy moved the fomod folder to Extras/<plugin> but left an option folder where it was packed
            fallback = (mod_dir / Path(source_text.replace("\\", os.sep))).resolve()
            try:
                fallback.relative_to(mod_dir.resolve())
                source = fallback
            except ValueError:
                pass
        if not source.exists():
            relative_source = Path(source_text.replace("\\", os.sep))
            off_candidates = [path for path in (mod_dir / "Off").rglob(relative_source.as_posix())
                              if path.exists()] if (mod_dir / "Off").is_dir() else []
            if off_candidates:
                source = sorted(off_candidates, key=lambda path: str(path).casefold())[0]
        destination = Path(destination_text.replace("\\", "/"))
        if destination.is_absolute() or ".." in destination.parts:
            continue
        parts = list(destination.parts)
        if parts and parts[0].casefold() == "data files":
            parts = parts[1:]
        if tag == "file" and source.is_file():
            # Documentation is read for descriptions, never installed as game payload.
            if source.suffix.casefold() in DOC_EXTS or (parts and parts[0].casefold() == "docs"):
                continue
            relative = Path(*parts, source.name) if parts else Path(source.name)
            if relative.parts and relative.parts[0].casefold() == "sounds":
                relative = Path("Sound", *relative.parts[1:])
            payload[str(relative)] = source
        elif tag == "folder" and source.is_dir():
            def walk_error(exc):
                print(f"  Could not inspect FOMOD payload folder {exc.filename}: {exc}; continuing with readable files.",
                      file=sys.stderr)
            for base, dirs, files in os.walk(source, onerror=walk_error):
                dirs[:] = [name for name in dirs if name.casefold() != "off"]
                for filename in files:
                    file = Path(base) / filename
                    try:
                        if not file.is_file():
                            continue
                        if file.suffix.casefold() in DOC_EXTS:
                            continue
                        relative_parts = file.relative_to(source).parts
                    except OSError as exc:
                        print(f"  Could not inspect FOMOD payload file {file}: {exc}; skipping it.", file=sys.stderr)
                        continue
                    relative = Path(*parts, *relative_parts) if parts else Path(*relative_parts)
                    if relative.parts and relative.parts[0].casefold() == "sounds":
                        relative = Path("Sound", *relative.parts[1:])
                    payload[str(relative)] = file
    return payload


def _discover_fomod(mod_dir: Path, installed: set[str]) -> list[dict]:
    """Translate a FOMOD ModuleConfig into the same choice records used by the mock installer UI."""
    config = _fomod_config(mod_dir)
    if not config:
        return []
    try:
        root = ET.parse(config).getroot()
    except (OSError, ET.ParseError):
        return []
    shared_docs = _read_docs([mod_dir / "Docs", mod_dir / "docs"])
    module_image_node = next(iter(_xml_local(root, "moduleImage")), None)
    module_image = _fomod_image_path(
        mod_dir, config, module_image_node.attrib.get("path") if module_image_node is not None else None)
    steps = _xml_local(root, "installStep")
    records = []
    for step_index, step in enumerate(steps):
        step_name = step.attrib.get("name", f"Install step {step_index + 1}")
        for group_index, group in enumerate(_xml_local(step, "group")):
            group_name = group.attrib.get("name", f"Options {group_index + 1}")
            if re.search(r"\b(?:documentation|readme|changelog|manuals?)\b",
                         f"{step_name} {group_name}", re.I):
                # Docs are still read locally and retained under Extras; don't show them as choices.
                continue
            group_type = group.attrib.get("type", "SelectAny")
            key = f"fomod:{step_index}:{group_index}"
            for choice_index, plugin in enumerate(_xml_local(group, "plugin")):
                label = plugin.attrib.get("name", f"Option {choice_index + 1}")
                description_node = next((node for node in plugin.iter()
                                         if node.tag.rsplit("}", 1)[-1].casefold() == "description"), None)
                fomod_description = " ".join("".join(description_node.itertext()).split()) \
                    if description_node is not None else ""
                image_node = next((node for node in plugin.iter()
                                   if node.tag.rsplit("}", 1)[-1].casefold() == "image"), None)
                image = _fomod_image_path(
                    mod_dir, config, image_node.attrib.get("path") if image_node is not None else None) or module_image
                type_node = next((node for node in plugin.iter()
                                  if node.tag.rsplit("}", 1)[-1].casefold() == "type"), None)
                option_type = (type_node.attrib.get("name", "Optional") if type_node is not None else "Optional")
                if option_type.casefold() in {"notusable", "not usable", "unusable"}:
                    # FOMOD authors sometimes encode readmes/changelogs as NotUsable entries.
                    continue
                source_root = config.parent.parent if config.parent.name.casefold() == "fomod" else config.parent
                payload = _fomod_payload(mod_dir, plugin, source_root)
                option_plugins = [src for src in payload.values() if src.suffix.casefold() in PLUGIN_EXTS]
                masters = set().union(*(_plugin_masters(path) for path in option_plugins)) if option_plugins else set()
                targets = masters - BASE_GAME_MASTERS
                missing = sorted(targets - installed)
                compatible = sorted(targets & installed)
                docs = _relevant_doc_excerpt(
                    _read_docs([config.parent]) or shared_docs, label,
                    [Path(rel).name for rel in payload])
                records.append({"key": f"{key}:{choice_index}", "group_key": key,
                                "group_label": f"{step_name} — {group_name}",
                                "selection_type": group_type, "path": config.parent,
                                "number": step_index * 100 + group_index, "label": label,
                                "required": option_type.casefold() == "required",
                                "recommended": option_type.casefold() == "recommended",
                                "targets": sorted(targets), "compatible": compatible,
                                "missing_masters": missing, "docs": docs,
                                "fomod_description": fomod_description,
                                "image_path": image,
                                "all_payload_files": payload,
                                "payload_files": (payload if option_type.casefold() == "required"
                                                  else {rel: src for rel, src in payload.items()
                                                        if src.suffix.casefold() not in {".esp", ".esm", ".omwaddon"}}),
                                "option_type": option_type, "has_plugin_payload": bool(option_plugins),
                                "mod_dir": mod_dir})
    return records


def _is_core_folder(name: str) -> bool:
    """A BAIN 00 folder ("00 Core", "00 - Data Files"): always installed, never offered as a choice."""
    match = OPTION_RE.match(name)
    return bool(match and int(match.group(1)) == 0)


def _unpacked_by_tidy(folder: Path) -> bool:
    """A 00 folder Tidy kept after linking its files into the package root: its files have a second name."""
    for path in _safe_rglob(folder):
        try:
            if path.is_file():
                return path.stat().st_nlink > 1
        except OSError:
            return False
    return False


def _option_dirs(mod_dir: Path) -> list[Path]:
    extras = next((p for p in mod_dir.iterdir() if p.is_dir() and p.name.casefold() == "extras"), None)
    if not extras:
        return []
    found: list[Path] = []
    payload_suffixes = PLUGIN_EXTS | {".bsa", ".nif", ".dds", ".tga", ".bmp", ".wav", ".mp3",
                                     ".kf", ".omwscripts", ".toml"}

    ignored = {"docs", "doc", "documentation", "readme", "bugreports", "unused-at-install", "off",
               "fomod", "logs", "cache", "outputs", "previously merged", "__pycache__"}
    search_root = extras
    directories = sorted((p for p in _safe_rglob(search_root) if p.is_dir()
                          and not any(part.casefold() in ignored for part in p.relative_to(search_root).parts)),
                         key=lambda p: (len(p.relative_to(search_root).parts), str(p).casefold()))
    # Numeric BAIN folders retain their order. Older packages often use names such as
    # "Optional textures", "No grass", or "Patch for X" instead. Treat plausible
    # payload folders as options, while avoiding a plugin-name container that only
    # groups its own option subfolders.
    kept_cores: list[Path] = []
    for folder in directories:
        if any(parent in found for parent in folder.parents):
            continue
        if any(parent in kept_cores for parent in folder.parents):
            continue
        if _is_core_folder(folder.name) and _unpacked_by_tidy(folder):
            kept_cores.append(folder)     # Tidy linked it into the root; the copy here is for repeating the install
            continue
        if OPTION_RE.match(folder.name):
            found.append(folder)
            continue
        # Package folders often wrap numbered choices one level deeper, e.g.
        # Extras/Cyr_Main/00 Core. Do not expose the wrapper as an optional choice.
        nested_option_dirs = [p for p in _safe_rglob(folder) if p.is_dir() and OPTION_RE.match(p.name)]
        if nested_option_dirs:
            continue
        if any(p.is_file() and p.suffix.casefold() in payload_suffixes for p in _safe_rglob(folder)):
            found.append(folder)
    return sorted(set(found), key=lambda p: (len(p.parts), str(p).casefold()))


def _decode_doc(raw: bytes) -> str:
    """Readmes are UTF-8, UTF-16 with a BOM, or (older and non-English ones) Windows-1252."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
                         "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", " ": " "})
_MARKUP_RE = re.compile(r"\[/?(?:b|i|u|s|size|color|font|url|center|quote|list|\*)(?:=[^\]]*)?\]|\*\*|__|`|^\s*#{1,6}\s+",
                        re.IGNORECASE)


def _normalize_line(line: str) -> str:
    """A readme line as matched, never as shown: one kind of dash and quote, no markup, single spaces, casefolded."""
    line = _MARKUP_RE.sub("", line.translate(_DASHES))
    return re.sub(r"\s+", " ", line).strip(" \t-*•>").casefold()


def _read_docs(paths: list[Path]) -> str:
    chunks = []
    seen = set()
    support_dirs = {"off", "bugreports", "extras", "screenshots", "grass", "openmw",
                    "fomod", "logs", "cache", "outputs", "previously merged", "__pycache__", "__macosx"}
    for root in paths:
        if not root.exists():
            continue
        if root.name.casefold() in support_dirs:
            continue
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.casefold() not in support_dirs]
            for filename in sorted(files, key=str.casefold):
                path = Path(base) / filename
                if path.suffix.casefold() not in DOC_EXTS or str(path).casefold() in seen:
                    continue
                seen.add(str(path).casefold())
                try:
                    if path.suffix.casefold() == ".pdf":
                        try:
                            from pypdf import PdfReader
                            content = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
                        except ImportError:
                            content = "PDF included; install pypdf to preview its text in the selector."
                    elif path.suffix.casefold() == ".docx":
                        with zipfile.ZipFile(path) as archive:
                            tree = ET.fromstring(archive.read("word/document.xml"))
                        content = " ".join(node.text or "" for node in tree.iter()
                                           if node.tag.endswith("}t"))
                    elif path.suffix.casefold() == ".doc":
                        # Word 97 keeps its text as plain character runs: enough for matching
                        content = "\n".join(re.findall(r"[\x20-\x7e\t]{4,}", path.read_bytes().decode("latin-1")))
                    else:
                        content = _decode_doc(path.read_bytes())
                        if path.suffix.casefold() in {".html", ".htm"}:
                            content = HTML_SCRIPT_STYLE_RE.sub(" ", content)
                            content = html.unescape(re.sub(r"<[^>]+>", " ", content))
                        elif path.suffix.casefold() == ".rtf":
                            content = re.sub(r"\\'[0-9a-fA-F]{2}|\\[a-zA-Z]+-?\d* ?|[{}]", "", content)
                    content = content.replace("﻿", "")[:DOC_LIMIT]
                except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError):
                    continue
                chunks.append(f"--- {path.name} ---\n{content}")
    return "\n\n".join(chunks)


_KEY_STOP = {"the", "and", "for", "with", "from", "this", "that", "data", "files", "file", "option", "optional",
             "install", "patch", "addon", "version", "mod", "core", "main"}
_LABEL_PREFIX_RE = re.compile(r"^(?:patch|addon|add-on|optional|option)\s*-\s*", re.IGNORECASE)
_LABEL_SUFFIX_RE = re.compile(r"\s*(?:-\s*(?:merged|modular)|\(choose one\)|\bv?\d+(?:\.\d+)+)\s*$", re.IGNORECASE)
_PATHLIKE_RE = re.compile(r"(?:^|\s)\.{1,2}/|--\w|\b\w+[\\/]\w+[\\/]|&>|^(?:python|wine|\$|cd)\s")  # commands, paths
_DIVIDER_RE = re.compile(r"^\s*[-=*_~#]{3,}\s*$")
_DOC_SEPARATOR_RE = re.compile(r"^--- (.+) ---$")              # put between readme files by _read_docs
_TIER_SCORE = {"heading": 100, "name": 80, "plugin": 70, "number": 60}


def _option_keys(option: dict, common: set[str], shared_plugins: set[str] = frozenset()) -> dict:
    """What a readme line may call this option: its folder name, label variants, number, plugins, and the label
    words no sibling option shares (the package's own name is in every label and tells them apart from nothing)."""
    folder = _normalize_line(option["folder"].rsplit("/", 1)[-1])
    label = _normalize_line(option.get("label") or folder)
    variants = {label, _normalize_line(_LABEL_SUFFIX_RE.sub("", _LABEL_PREFIX_RE.sub("", label)))} - {""}
    tokens = {t for t in re.findall(r"[a-z0-9]{3,}", label) if t not in _KEY_STOP} - common
    plugins = [p.casefold() for p in option.get("plugins", ()) if p and p.casefold() not in shared_plugins]
    # "01 Patch" for "01 Patch Solstheim - Tomb of the Snow Prince": the number keeps a short name specific
    short = re.match(r"(\d{1,3})\s*[-_.:)]?\s*([a-z0-9']{3,})", folder)
    return {"folder": folder, "variants": variants, "short": f"{short.group(1)} {short.group(2)}" if short else None, "tokens": tokens, "number": option.get("number"),
            "padded": bool(re.match(r"0\d", folder)), "plugins": plugins}


def _line_tier(line: str, keys: dict) -> tuple[int, str] | None:
    """The best way this normalized line names the option, scored; None when it does not."""
    if not line:
        return None
    number = keys["number"]
    numbered = rf"(?:(?:option|module|step|folder)\s*)?[\[(]?0*{number}[\])]?\s*[-:.)]?\s*" if number is not None else ""
    for variant in keys["variants"] | {keys["folder"]}:
        if line == variant or line.rstrip(":") == variant or (
                numbered and re.fullmatch(numbered + re.escape(variant) + r"\s*:?", line)):
            return _TIER_SCORE["heading"], "heading"
    if _PATHLIKE_RE.search(line):
        return None
    if len(keys["folder"]) >= 4 and keys["folder"] in line:
        return _TIER_SCORE["name"], "name"
    if any(len(v) >= 6 and f'"{v}"' in line for v in keys["variants"]):
        return _TIER_SCORE["name"], "name"
    if keys["short"] and re.search(rf"(?<![\w.]){re.escape(keys['short'])}\b", line):
        return _TIER_SCORE["name"], "name"
    if any(re.search(rf"(?<![\w.]){re.escape(p)}(?![\w])", line) for p in keys["plugins"]):
        return _TIER_SCORE["plugin"], "plugin"
    words = set(re.findall(r"[a-z0-9]{3,}", line))
    if number is not None:
        lead = re.match(r"[\[(]?(\d{1,3})[\])]?\s*[-:.)]", line)
        span = re.match(r"(\d{1,3})\s*-\s*(\d{1,3})\s*[-:]", line)
        if lead and int(lead.group(1)) == number and (
                (keys["padded"] and lead.group(1).startswith("0")) or keys["tokens"] & words):
            return _TIER_SCORE["number"], "number"
        if span and int(span.group(1)) <= number <= int(span.group(2)):
            return _TIER_SCORE["number"], "number"
    tokens = keys["tokens"]
    if tokens:
        coverage = len(tokens & words) / len(tokens)
        if coverage >= 0.75 and (len(tokens & words) >= 2 or len(tokens) == 1):
            return int(40 * coverage), "fuzzy"
    return None


def _option_excerpts(text: str, options: list[dict]) -> dict[str, str]:
    """The readme text for each option of one package, keyed by folder. Each line belongs to the option(s) naming it
    best, so siblings no longer share one line. An option the readme does not mention gets no text."""
    lines = text.splitlines()
    norm = [_normalize_line(line) for line in lines]
    token_sets = [{t for t in re.findall(r"[a-z0-9]{3,}", _normalize_line(o.get("label") or o["folder"]))}
                  for o in options]
    common = set.intersection(*token_sets) if len(token_sets) > 1 else set()
    seen_plugins: dict[str, int] = {}
    for o in options:
        for plugin in {p.casefold() for p in o.get("plugins", ())}:
            seen_plugins[plugin] = seen_plugins.get(plugin, 0) + 1
    shared_plugins = {p for p, count in seen_plugins.items() if count > 1}   # a patch replacing the core's .esp
    keys = [_option_keys(o, common, shared_plugins) for o in options]
    owners: dict[int, list[tuple[int, int, str]]] = {}
    separators = {index for index, line in enumerate(lines) if _DOC_SEPARATOR_RE.match(line)}
    for index, line in enumerate(norm):
        if index in separators:
            # "--- ropes woven explanations.txt ---": a readme named after an option is all about that option
            stem = set(re.findall(r"[a-z0-9]{3,}", Path(_DOC_SEPARATOR_RE.match(lines[index]).group(1)).stem.casefold()))
            scored = [(90, n, "document") for n, k in enumerate(keys)
                      if len(k["tokens"] & stem) >= 2 and len(k["tokens"] & stem) / len(k["tokens"]) >= 0.75]
            if len(scored) == 1:                      # a readme fitting several options is the package's own
                owners[index] = scored
            continue
        scored = [(hit[0], n, hit[1]) for n, k in enumerate(keys) if (hit := _line_tier(line, k))]
        if scored:
            best = max(score for score, _n, _tier in scored)
            owners[index] = [item for item in scored if item[0] == best]

    def block(start: int, tier: str) -> list[str]:
        end = start + 1
        blanks = 0
        while end < len(lines):
            if end in separators:
                break
            if tier == "document":                     # the whole readme file, up to the next one
                end += 1
                continue
            if _DIVIDER_RE.match(lines[end]) or (end in owners and owners[end][0][2] in ("heading", "number")):
                break
            if not norm[end]:
                blanks += 1
                if tier != "heading" or blanks >= 2:
                    break
            else:
                blanks = 0
                if tier != "heading" and re.match(r"\s*(?:[-*•]|\d{1,3}[.)])\s", lines[end]) \
                        and not lines[end].startswith((" ", "\t")):
                    break
            end += 1
        return [line.rstrip() for line in lines[start:end] if line.strip()]

    result = {}
    for n, option in enumerate(options):
        hits = sorted(((score, index, tier) for index, owned in owners.items()
                       for score, owner, tier in owned if owner == n), key=lambda h: (-h[0], h[1]))
        chunks, used = [], set()
        for _score, index, tier in hits[:4]:
            if index in used:
                continue
            chunk = block(index, tier)
            used.update(range(index, index + len(chunk)))
            chunks.append("\n".join(chunk))
        if not chunks and keys[n]["tokens"]:
            # named nowhere: the one prose line sharing most of its distinctive words, even if a sibling owns it
            # (a readme often covers two variants in one sentence)
            # ties go to a line that names some option, then to the earliest (install notes precede changelogs)
            best = max(((len(keys[n]["tokens"] & set(re.findall(r"[a-z0-9]{3,}", line))), index in owners, -index)
                        for index, line in enumerate(norm) if len(line) >= 8 and index not in separators
                        and not _PATHLIKE_RE.search(line)),
                       default=(0, False, 0))
            # one shared word is chance ("potions", "vivec") unless it is the option's only distinctive word
            if best[0] >= min(2, len(keys[n]["tokens"])):
                chunks.append(lines[-best[2]].strip())
        result[option["folder"]] = "\n\n".join(dict.fromkeys(chunks))[:1800]
    return result


def _plugin_description(path: Path) -> str:
    """The description an author typed into the plugin's TES3 header (HEDR: version, flags, 32-byte author,
    256-byte description)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(512)
    except OSError:
        return ""
    at = head.find(b"HEDR")
    if not head.startswith(b"TES3") or at < 0 or len(head) < at + 8 + 8 + 32 + 256:
        return ""
    raw = head[at + 8 + 8 + 32:at + 8 + 8 + 32 + 256].split(b"\0", 1)[0]
    return raw.decode("cp1252", errors="replace").strip()


def _option_outputs(path: Path) -> set[str]:
    """The files an option would install, relative to its own root and casefolded; docs are not output."""
    return {str(p.relative_to(path)).replace("\\", "/").casefold() for p in _safe_rglob(path)
            if p.is_file() and p.suffix.casefold() not in DOC_EXTS}


def _contents_summary(outputs: set[str]) -> str:
    """'2 plugins, 31 meshes, 12 textures' for an option the readme says nothing about."""
    kinds = {"plugin": 0, "mesh": 0, "texture": 0, "sound": 0, "script": 0, "other": 0}
    for rel in outputs:
        suffix = Path(rel).suffix
        kind = ("plugin" if suffix in PLUGIN_EXTS else "mesh" if suffix in {".nif", ".kf"} else
                "texture" if suffix in {".dds", ".tga", ".bmp"} else "sound" if suffix in {".wav", ".mp3"} else
                "script" if suffix in {".lua", ".omwscripts", ".toml"} else "other")
        kinds[kind] += 1
    plural = {"mesh": "meshes"}
    parts = [f"{n} {plural.get(k, k + 's') if n != 1 else k}" for k, n in kinds.items() if n]
    return ", ".join(parts)


_REQUIRED_RE = re.compile(r"\b(?:always|must|required|mandatory)\b.{0,30}\binstall|\binstall\b.{0,20}\balways\b"
                          r"|^\s*0*0\s*[-_ .]*\s*(?:core|main|base|required|data files)\b", re.IGNORECASE)
_CHOOSE_ONE_RE = re.compile(r"\b(?:choose|pick|select|use|install)\s+(?:only\s+)?one\b|mutually exclusive|"
                            r"\binstead of\b|\bnot both\b|\(choose one\)", re.IGNORECASE)
_REQUIRES_RE = re.compile(r"\b(?:requires?|needs?)\s+(?:the\s+)?(?P<a>[^.,;:()\n]{2,60})"
                          r"|\bif you (?:use|have|play with|are using)\s+(?:the\s+)?(?P<b>[^.,;:()\n]{2,60})"
                          r"|^\s*(?:\d{1,3}\s*[-_.:)]?\s*)?patch for\s+(?P<c>[^.,;:()\n]{2,60})", re.IGNORECASE)
_ORDER_RE = re.compile(r"\b(?:install|load)\s+(?:this\s+|it\s+)?(?:after|on top of|over)\s+(?P<x>[^.,;:()\n]{2,60})",
                       re.IGNORECASE)
_LANGUAGE_RE = re.compile(r"\b(german|deutsch|french|fran[cç]ais|russian|polish|spanish)\b", re.IGNORECASE)
# readme shorthand for the common shared masters
_REQUIRE_ALIASES = {"td": "tamriel_data", "tamriel data": "tamriel_data", "oaab": "oaab_data",
                    "oaab data": "oaab_data", "tr": "tr_mainland", "tamriel rebuilt": "tr_mainland",
                    "pt": "cyr_main", "project tamriel": "cyr_main", "mwse": "mwse"}


def _plain_name(text: str) -> str:
    text = re.sub(r"\.(?:esp|esm|omwaddon)\b", "", text.casefold())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _requirement_met(name: str, installed: set[str]) -> bool | None:
    """True or False when the requirement names a plugin MWManager can see; None when it cannot tell."""
    plain = _plain_name(re.split(r"\s+(?:and|or|by|from|to|for|if|which|that)\s+", name.casefold())[0])
    if not plain:
        return None
    target = _plain_name(_REQUIRE_ALIASES.get(plain, plain))
    if target == "mwse":
        return None
    names = {_plain_name(p) for p in installed}
    if target in names or any(len(target) >= 5 and (n.startswith(target) or target in n) for n in names):
        return True
    plugin_like = re.search(r"\.(?:esp|esm)\b|^\w+_\w+$", name.strip(), re.IGNORECASE)   # TR_Mainland, Sky_Main
    return False if _REQUIRE_ALIASES.get(plain) or plugin_like else None


def _option_badges(folder: str, docs: str, installed: set[str]) -> list[str]:
    """Short notes for the option window, from the option's readme text and its folder name."""
    text = f"{folder}\n{docs}"
    badges = []
    if _REQUIRED_RE.search(folder) or _REQUIRED_RE.search(docs):
        badges.append("Always install")
    if _CHOOSE_ONE_RE.search(text):
        badges.append("Choose one")
    seen = set()
    for match in _REQUIRES_RE.finditer(text):
        name = re.split(r"\s+-\s+|\s+(?:to|for|if|which|that|so|because|in order)\s+",
                        (match.group("a") or match.group("b") or match.group("c")))[0].strip(" '\"-")
        name = re.sub(r"\s+(?:is\s+)?installed$", "", name, flags=re.IGNORECASE)
        key = _plain_name(name)
        # a name, not a phrase: capitalized as written, a plugin file, or known shorthand ("a texture replacer" is not)
        named = name[:1].isupper() or re.search(r"\.(?:esp|esm)\b", name, re.IGNORECASE) or key in _REQUIRE_ALIASES
        if not key or not named or key in seen or len(seen) >= 3 or key in {"openmw", "mwse", "this", "this package"}:
            continue
        seen.add(key)
        met = _requirement_met(name, installed)
        badges.append(f"Requires {name}" + {True: " (installed)", False: " (not installed)", None: ""}[met])
    order = _ORDER_RE.search(docs)
    if order:
        badges.append(f"Install after {order.group('x').strip()}")
    if re.search(r"\bopenmw\b", folder, re.IGNORECASE):
        badges.append("OpenMW")
    if re.search(r"\bmwse\b", folder, re.IGNORECASE) and not re.search(r"\bno[ _-]?mwse\b", folder, re.IGNORECASE):
        badges.append("MWSE")
    language = _LANGUAGE_RE.search(folder)
    if language:
        badges.append(f"Language: {language.group(1).title()}")
    return badges


def _relevant_doc_excerpt(text: str, label: str, plugin_names=(), option_number: int | None = None) -> str:
    """The readme text for one choice, matched without its siblings (FOMOD and plugin choices)."""
    if option_number is not None and option_number < 1000:
        folder = f"{option_number:02d} {label}"
    else:
        folder, option_number = label, None
    option = {"folder": folder, "label": label, "number": option_number, "plugins": [Path(p).name for p in plugin_names]}
    return _option_excerpts(text, [option])[folder]


def _active_staged_plugins(mod_dir: Path) -> set[str]:
    """Return plugins in a processed mod's active staging tree, excluding options and parked files."""
    found = set()
    if not mod_dir.is_dir():
        return found
    excluded = {"off", "extras", "docs", "screenshots", "openmw", "bugreports", "grass"}
    for base, dirs, files in os.walk(mod_dir):
        dirs[:] = [name for name in dirs if name.casefold() not in excluded]
        found.update(name.casefold() for name in files
                     if Path(name).suffix.casefold() in {".esm", ".esp"})
    return found


def _available_staged_plugins(mod_dir: Path) -> set[str]:
    """Include active plugins and those in mandatory BAIN core/data folders."""
    found = _active_staged_plugins(mod_dir)
    if not mod_dir.is_dir():
        return found
    required_dirs = {"00 core", "00 - core", "00 data files", "00 - data files"}
    excluded = {"off", "docs", "screenshots", "openmw", "bugreports", "grass", "unused-at-install"}
    for base, dirs, files in os.walk(mod_dir):
        relative_parts = {re.sub(r"[_-]+", " ", part).strip().casefold()
                          for part in Path(base).relative_to(mod_dir).parts}
        dirs[:] = [name for name in dirs if name.casefold() not in excluded]
        if relative_parts & required_dirs:
            found.update(name.casefold() for name in files
                         if Path(name).suffix.casefold() in {".esm", ".esp"})
    return found


def _installed_plugins(data_files: Path, prior: set[str]) -> set[str]:
    found = set(prior)
    for base, dirs, files in os.walk(data_files):
        dirs[:] = [d for d in dirs if d.casefold() not in {"off", "extras", "docs", "screenshots", "openmw"}]
        found.update(Path(name).name.casefold() for name in files
                     if Path(name).suffix.casefold() in PLUGIN_EXTS)
    return found


def _discover(mod_dir: Path, installed: set[str]) -> list[dict]:
    option_dirs = _option_dirs(mod_dir)
    if not option_dirs:
        return []
    base_plugins = [p for p in mod_dir.iterdir() if p.is_file() and p.suffix.casefold() in PLUGIN_EXTS]
    base_masters = set().union(*(_plugin_masters(p) for p in base_plugins)) if base_plugins else set()
    base_plugin_stems = {re.sub(r"[^a-z0-9]+", " ", p.stem.casefold()).strip() for p in base_plugins}
    shared_docs = _read_docs([mod_dir / "Docs", mod_dir / "docs", mod_dir])
    specs = []
    for path in option_dirs:
        match = OPTION_RE.match(path.name)
        specs.append({"folder": str(path.relative_to(mod_dir)), "number": int(match.group(1)) if match else None,
                      "label": match.group(2).strip() if match else path.name,
                      "plugins": [p.name for p in _safe_rglob(path) if p.is_file()
                                  and p.suffix.casefold() in PLUGIN_EXTS]})
    shared_excerpts = _option_excerpts(shared_docs, specs) if shared_docs else {}
    records = []
    loose_records = []
    for order, path in enumerate(option_dirs):
        match = OPTION_RE.match(path.name)
        number = int(match.group(1)) if match else 1000 + order
        label = match.group(2).strip() if match else re.sub(r"[_-]+", " ", path.name).strip()
        option_docs = _read_docs([path])
        option_plugins = [p for p in _safe_rglob(path) if p.is_file() and p.suffix.casefold() in PLUGIN_EXTS
                          and "off" not in {part.casefold() for part in p.relative_to(path).parts}]
        # the option's own readme speaks only of it; the package readme is shared out among the siblings
        docs = (_relevant_doc_excerpt(option_docs, label, [plugin.name for plugin in option_plugins],
                                      option_number=number) if option_docs
                else shared_excerpts.get(str(path.relative_to(mod_dir)), ""))
        masters = set().union(*(_plugin_masters(p) for p in option_plugins)) if option_plugins else set()
        targets = masters - base_masters - BASE_GAME_MASTERS
        mentioned = {m.group(0).strip().casefold() for m in PLUGIN_REF_RE.finditer(docs)}
        targets.update(mentioned - base_masters)
        compatible = sorted(targets & installed)
        missing_masters = sorted(masters - BASE_GAME_MASTERS - installed)
        normalized_option = re.sub(r"[^a-z0-9]+", " ", f"{label} {path.name}".casefold())
        informational_utility = "tamriel data" in normalized_option and bool(re.search(r"\bhd\b", normalized_option))
        normalized_folder = re.sub(r"[^a-z0-9]+", " ", path.name.casefold()).strip()
        core_bundle = normalized_folder in base_plugin_stems
        record = {"key": str(path.relative_to(mod_dir)), "path": path, "number": number,
                        "label": label, "required": bool(match and number == 0) or core_bundle,
                        "core_bundle": core_bundle,
                        "has_plugin_payload": bool(option_plugins),
                        "has_master_payload": any(p.suffix.casefold() == ".esm" for p in option_plugins),
                        "informational_utility": informational_utility,
                        "targets": sorted(targets), "compatible": compatible,
                        "missing_masters": missing_masters, "docs": docs, "mod_dir": mod_dir,
                        "badges": _option_badges(path.name, docs, installed),
                        "outputs": _option_outputs(path)}
        if not docs:
            # no readme text: the plugin header's own description, else what the folder holds
            described = [d for d in (_plugin_description(p) for p in option_plugins) if d]
            record["fallback_text"] = ("From the plugin header: " + " / ".join(dict.fromkeys(described))
                                       if described else "Contents: " + (_contents_summary(record["outputs"])
                                                                         or "no files"))
        record["required"] = _is_hidden_required_option(record)
        if match:
            records.append(record)
        else:
            loose_records.append(record)

    # Several "00" folders are all core and installed without asking (every 00 is required); only a readme saying
    # they are alternatives turns them into one choice
    zeros = [r for r in records if r["number"] == 0]
    if len(zeros) > 1 and any("Choose one" in r["badges"] or EXCLUSIVE_DOC_RE.search(r["docs"]) for r in zeros):
        for r in zeros:
            r.update(required=False, always_optional=True)
    # numbered options are installed in number order; a later one replacing an earlier one's files is worth saying
    for later in records:
        for earlier in records:
            if earlier["number"] < later["number"] and earlier["outputs"] & later["outputs"]:
                later["badges"].append(f"Replaces {len(earlier['outputs'] & later['outputs'])} file(s) "
                                       f"from {earlier['path'].name}")
    if loose_records:
        all_docs = "\n\n".join(record["docs"] for record in loose_records).casefold()
        explicit = bool(EXCLUSIVE_DOC_RE.search(all_docs))
        plugin_sets = [set().union(*(_plugin_masters(p) for p in _safe_rglob(record["path"])
                                     if p.is_file() and p.suffix.casefold() in PLUGIN_EXTS))
                       for record in loose_records]
        same_masters = len(plugin_sets) > 1 and bool(plugin_sets[0] - BASE_GAME_MASTERS) \
            and all(value - BASE_GAME_MASTERS == plugin_sets[0] - BASE_GAME_MASTERS for value in plugin_sets)
        stems = [set(re.findall(r"[a-z0-9]{3,}", item["label"].casefold())) for item in loose_records]
        similar = any(len(a & b) / max(1, len(a | b)) >= 0.4
                      for i, a in enumerate(stems) for b in stems[i + 1:])
        # folders that install the same files cannot both be kept: they are alternatives whatever the readme says
        overlapping = any(a["outputs"] & b["outputs"] for i, a in enumerate(loose_records)
                          for b in loose_records[i + 1:])
        exclusive = explicit or (same_masters and similar) or overlapping
        for record in loose_records:
            if exclusive:
                record.update(number=900, group_key="legacy-folder-options",
                              group_label="Legacy optional folders — likely exclusive",
                              selection_type="SelectAtMostOne")
            else:
                record["group_label"] = "Legacy optional folder — best-effort choice"
            records.append(record)
    return sorted(records, key=lambda x: (x["number"], x["label"].casefold(), x["key"].casefold()))


def _represented_option_sources(records: list[dict]) -> set[Path]:
    represented = set()
    for record in records:
        payload = record.get("payload_files")
        if payload is None:
            payload = _payload_files(record)
        for source in payload.values():
            try:
                represented.add(Path(source).resolve())
            except (OSError, TypeError):
                continue
    return represented


def _discover_plugin_choices(mod_dir: Path, installed: set[str],
                             represented: set[Path] | None = None) -> list[dict]:
    """Review sibling ESP/ESM files, using header and documentation clues for exclusivity."""
    candidates: dict[str, tuple[str, Path]] = {}
    # Only root-level ESP choices are moved to Extras. Keep the existing review
    # of ESMs and Data Files plugins in place for their established behavior.
    roots = [mod_dir, mod_dir / "Data Files"]
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.iterdir():
            # A root ESM is the package's identity/master, not an optional install
            # choice. Nested ESMs remain discoverable below when genuinely optional.
            if path.is_file() and path.suffix.casefold() == ".esp":
                rel = str(path.relative_to(mod_dir))
                candidates[rel.casefold()] = (rel, path)
    # Every optional nested ESP/ESM gets its own opt-in entry, regardless of folder depth.
    # The base payload folders remain governed by their required 00 package choice.
    nested_records = []
    excluded_parts = {"off", "docs", "screenshots", "bugreports", "openmw", "grass", "fomod", "unused-at-install"}
    required_parts = {"00 core", "00 data files", "00 - core", "00 - data files"}
    for path in sorted(_safe_rglob(mod_dir), key=lambda item: str(item).casefold()):
        if not path.is_file() or path.suffix.casefold() not in {".esp", ".esm"}:
            continue
        relative = path.relative_to(mod_dir)
        if len(relative.parts) < 2:
            continue
        # A lone "Data Files" wrapper is packaging, not a choice: its masters are the package itself.
        if path.suffix.casefold() == ".esm" and [part.casefold() for part in relative.parts[:-1]] == ["data files"]:
            continue
        normalized_parts = {re.sub(r"[_-]+", " ", part).strip().casefold() for part in relative.parts[:-1]}
        if any(part.casefold() in excluded_parts for part in relative.parts[:-1]):
            continue
        if normalized_parts & required_parts or (any(_is_core_folder(part) for part in relative.parts[:-1])
                                                 and path.stat().st_nlink > 1):     # a kept 00 copy, already active
            continue
        if represented and path.resolve() in represented:
            continue
        rel = str(relative)
        masters = _plugin_masters(path)
        targets = masters - BASE_GAME_MASTERS
        nested_records.append({"key": "nested-plugin:" + rel.casefold(),
                               "group_key": "nested-plugin-files",
                               "group_label": "Optional nested ESP/ESM files",
                               "selection_type": "SelectAny", "path": path, "number": 700,
                               "label": rel, "required": False, "always_optional": True,
                               "targets": sorted(targets), "compatible": sorted(targets & installed),
                               "missing_masters": sorted(targets - installed),
                               "docs": _relevant_doc_excerpt(
                                   _read_docs([mod_dir / "Docs", mod_dir / "docs", path.parent]), path.name,
                                   [path.name]),
                               "payload_files": {path.name: path}, "mod_dir": mod_dir})

    # A root ESP already owned by a numbered option is represented by that option.
    option_plugin_names = {
        p.name.casefold()
        for p in _safe_rglob(mod_dir)
        if p.is_file() and p.suffix.casefold() in {".esp", ".esm"}
        and any(OPTION_RE.match(part) for part in p.relative_to(mod_dir).parts[:-1])
    }
    candidates = {key: value for key, value in candidates.items()
                  if value[0].casefold().split("\\")[-1].split("/")[-1] not in option_plugin_names}

    # Root ESPs previously parked as unused remain available for later review.
    extras_root = mod_dir / "Extras"
    if extras_root.is_dir():
        for path in extras_root.glob("*/unused-at-install/*.esp"):
            rel = path.name
            candidates.setdefault(rel.casefold(), (rel, path))
    if len(candidates) < 2:
        return nested_records

    docs = _read_docs([mod_dir / "Docs", mod_dir / "docs", mod_dir])
    docs_folded = docs.casefold()
    entries = list(candidates.values())
    names_mentioned = sum(Path(rel).name.casefold() in docs_folded for rel, _ in entries)
    explicit = bool(EXCLUSIVE_DOC_RE.search(docs_folded)) and names_mentioned >= 2
    master_sets = [_plugin_masters(path) - BASE_GAME_MASTERS for _, path in entries]
    same_nontrivial_masters = bool(master_sets[0]) and all(value == master_sets[0] for value in master_sets)
    stems = [re.sub(r"[^a-z0-9]+", " ", Path(rel).stem.casefold()).split() for rel, _ in entries]
    similar_names = any(len(set(left) & set(right)) / max(1, len(set(left) | set(right))) >= 0.5
                        for i, left in enumerate(stems) for right in stems[i + 1:])
    patch_like = any(PATCH_LIKE_RE.search(Path(rel).stem.casefold()) for rel, _ in entries)
    exclusive = explicit or (same_nontrivial_masters and similar_names and not patch_like)
    evidence = ("package documentation says these plugin choices are alternatives"
                if explicit else "plugin headers share the same non-game master list and filenames look like variants"
                if exclusive else "no strong exclusivity signal; choices are allowed together")
    selection_type = "SelectAtMostOne" if exclusive else "SelectAny"
    records = []
    for index, (rel, source) in enumerate(entries):
        masters = _plugin_masters(source)
        targets = masters - BASE_GAME_MASTERS
        records.append({"key": "plugin-choice:" + rel.casefold(), "group_key": "plugin-choices",
                        "group_label": "ESP/ESM plugin choices" + (" — likely exclusive" if exclusive
                                                                      else " — may be used together"),
                        "selection_type": selection_type, "path": source, "number": 500 + index,
                        "label": Path(rel).name, "required": False, "targets": sorted(targets),
                        "compatible": sorted(targets & installed),
                        "missing_masters": sorted(targets - installed),
                        "docs": _relevant_doc_excerpt(docs, source.name, [source.name]),
                        "payload_files": {rel: source}, "plugin_choice": True, "mod_dir": mod_dir,
                        "evidence": evidence})
    return records + nested_records


def _apply_plugin_choices(mod_dir: Path, records: list[dict], chosen: set[str]) -> int:
    """Park unused root ESP choices in Extras and restore choices enabled again."""
    changed = 0
    for item in records:
        if not item.get("plugin_choice"):
            continue
        for rel, source in item["payload_files"].items():
            destination = mod_dir / rel
            root_esp = Path(rel).parent == Path(".") and Path(rel).suffix.casefold() == ".esp"
            if root_esp:
                extras = mod_dir / "Extras"
                # Tidy may already have created the plugin-named Extras folder.
                mod_folder = next((p for p in extras.iterdir() if p.is_dir()
                                   and p.name.casefold() == mod_dir.name.casefold()), None) \
                    if extras.is_dir() else None
                if mod_folder is None:
                    mod_folder = extras / mod_dir.name
                unused_root = mod_folder / "unused-at-install"
            else:
                unused_root = mod_dir / "Off" / _package_display_name(mod_dir) / "FOMOD option plugin choices"
            try:
                parked = source.resolve().is_relative_to(unused_root.resolve())
            except (OSError, ValueError, AttributeError):
                parked = False
            if item["key"] in chosen:
                if parked:
                    if destination.exists():
                        print(f"  Could not restore selected plugin {rel}: a file already occupies its path.")
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(destination))
                    changed += 1
            elif not parked and source.exists():
                target = unused_root / Path(rel).name if root_esp else unused_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = target.with_name(f"{target.stem}.mwmanager-{int(time.time())}{target.suffix}")
                shutil.move(str(source), str(target))
                changed += 1
    return changed


def _nexus_page_url(mod_dir: Path) -> str:
    """Find the package's Nexus or author page from Vortex metadata and folder ID."""
    try:
        from mwdialog_assets import nexus_id, vortex_mod_metadata
        metadata = vortex_mod_metadata(str(mod_dir))
        mod_id = str(metadata.get("mod_id") or nexus_id(str(mod_dir)) or "").strip()
        if not mod_id.isdigit() or int(mod_id) <= 0:
            return ""
        page = str(metadata.get("mod_page") or "").strip()
        allowed_prefixes = ("https://www.nexusmods.com/morrowind/mods/",
                            "https://nexusmods.com/morrowind/mods/")
        if page.startswith(allowed_prefixes):
            match = re.search(r"/mods/(\d+)(?:/|$)", page)
            if match and match.group(1) == mod_id:
                return page
        # Vortex download/staging folder names often contain the Nexus mod ID,
        # while meta.ini is stored elsewhere and is not included in the archive.
        return f"https://www.nexusmods.com/morrowind/mods/{mod_id}"
    except (ImportError, AttributeError, OSError):
        return ""


MAINTAINER_NOTES = Path(__file__).resolve().with_name("mock_fomod_notes.json")


def _maintainer_notes(item: dict) -> str:
    """MWManager's own notes for an option, from mock_fomod_notes.json: what the FOMOD, the readmes and the mod
    page do not say (a modder's resource, a patch nobody needs). Shown after all of them, never instead."""
    try:
        mods = json.loads(MAINTAINER_NOTES.read_text(encoding="utf-8")).get("mods", [])
    except (OSError, ValueError):
        return ""
    mod_dir = Path(item.get("mod_dir") or item["path"])
    try:
        mod_id = _nexus_mod_id(mod_dir)
    except Exception:
        mod_id = ""
    path = Path(item["path"]) if item.get("path") else None
    try:
        relative = str(path.relative_to(mod_dir)) if path else ""
    except ValueError:
        relative = ""
    names = {str(value).replace("\\", "/").casefold()
             for value in (item.get("label"), item.get("key"), path.name if path else "", relative) if value}
    notes = []
    for mod in mods:
        if not ((mod_id and str(mod.get("nexus_id", "")) == mod_id)
                or any(fnmatch.fnmatch(mod_dir.name.casefold(), pattern.casefold())
                       for pattern in mod.get("packages", []))):
            continue
        for pattern, text in mod.get("options", {}).items():
            pattern = pattern.replace("\\", "/").casefold()
            if any(fnmatch.fnmatch(name, pattern) for name in names):
                notes.append(str(text).strip())
    return "\n\n".join(dict.fromkeys(note for note in notes if note))


def _nexus_mod_id(mod_dir: Path) -> str:
    match = re.search(r"/mods/(\d+)(?:/|$)", _nexus_page_url(mod_dir))
    return match.group(1) if match else ""


def _nexus_page_text(mod_dir: Path, *, refresh=False) -> str:
    """Retrieve searchable page text on demand; retain actionable fetch diagnostics."""
    cache_key = str(mod_dir.resolve()).casefold()
    if not refresh and cache_key in _NEXUS_TEXT_CACHE:
        return _NEXUS_TEXT_CACHE[cache_key]
    if not refresh:
        try:
            import mwnexus_browser
            cached = mwnexus_browser.cached_page(_nexus_mod_id(mod_dir))
            if cached:
                _NEXUS_TEXT_CACHE[cache_key] = str(cached.get("text", ""))
                return _NEXUS_TEXT_CACHE[cache_key]
        except (ImportError, OSError, ValueError):
            pass
    _NEXUS_FETCH_ERRORS.pop(cache_key, None)
    page = _nexus_page_url(mod_dir)
    if not page:
        return ""

    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.description = ""
            self.suppressed = 0

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag.casefold() in {"script", "style", "noscript"}:
                self.suppressed += 1
            elif tag.casefold() in {"p", "br", "div", "li", "section", "article", "h1", "h2", "h3", "h4"}:
                self.parts.append("\n")
            if tag.casefold() == "meta" and (values.get("name", "").casefold() in {"description", "og:description"}
                                              or values.get("property", "").casefold() == "og:description"):
                self.description = values.get("content", "")

        def handle_endtag(self, tag):
            if tag.casefold() in {"script", "style", "noscript"} and self.suppressed:
                self.suppressed -= 1
            elif tag.casefold() in {"p", "div", "li", "section", "article", "h1", "h2", "h3", "h4"}:
                self.parts.append("\n")

        def handle_data(self, data):
            if not self.suppressed and data.strip():
                self.parts.append(data.strip())

    try:
        request = urllib.request.Request(page, headers={"User-Agent": "MWManager mod option helper"})
        with urllib.request.urlopen(request, timeout=15) as response:
            content = response.read(600_000).decode("utf-8", "replace")
        challenge = CHALLENGE_RE.search(content)
        if challenge:
            _NEXUS_FETCH_ERRORS[cache_key] = (
                "Nexus returned a bot-protection/CAPTCHA page. Open page on Nexus, complete the "
                "challenge in the MWManager browser, then click Read page from browser."
            )
            return ""
        text = Text()
        text.feed(content)
        body_text = "\n".join(text.parts)
        result = (text.description + "\n" + body_text if text.description else body_text)
        result = re.sub(r"[ \t]+", " ", result)
        result = re.sub(r"\n\s*\n+", "\n", result).strip()[:30000]
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            _NEXUS_FETCH_ERRORS[cache_key] = (
                "Nexus returned HTTP 403 Forbidden. This can indicate bot protection or a CAPTCHA; "
                "open page on Nexus, complete any challenge in the MWManager browser, then click "
                "Read page from browser."
            )
        else:
            _NEXUS_FETCH_ERRORS[cache_key] = f"Nexus returned HTTP {exc.code} {exc.reason}."
        result = ""
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        _NEXUS_FETCH_ERRORS[cache_key] = f"Could not connect to Nexus: {reason}."
        result = ""
    except (OSError, ValueError) as exc:
        _NEXUS_FETCH_ERRORS[cache_key] = f"Nexus page fetch failed: {exc}."
        result = ""
    if result:
        _NEXUS_TEXT_CACHE[cache_key] = result
    return result


def _option_description(record: dict, mod_dir: Path, page_text: str = "") -> str:
    """Choose the most relevant packaged or already-fetched online description."""
    if record.get("fomod_description"):
        return ""
    labels = [record.get("label", ""), *record.get("targets", [])]
    labels.extend(Path(path).name for path in record.get("payload_files", {}).keys())
    terms = {token.casefold() for label in labels for token in re.findall(r"[A-Za-z0-9]{3,}", label)}
    stop = {"the", "and", "for", "with", "from", "this", "that", "data", "files", "plugin", "option"}
    terms -= stop

    def best(text: str) -> str:
        lines = [re.sub(r"\s+", " ", line).strip(" \t-*•") for line in text.splitlines()]
        ranked = [(sum(1 for term in terms if term in line.casefold()), line)
                  for line in lines if len(line) >= 18]
        chosen = [line for score, line in sorted(ranked, key=lambda item: (-item[0], len(item[1]))) if score]
        return "\n".join(dict.fromkeys(chosen[:5]))

    # readme text was already matched to this option, in reading order: shown as it is, never re-ranked
    if record.get("docs", "").strip():
        return record["docs"].strip()
    if not page_text:
        return ""
    # Mod-page text: stemmed, definition-aware matching on the folder's own name
    # ("01 Replacer" -> "Replacer - a replacer for some of the worst offenders ..."),
    # so payload file names and changelog lines do not drown out the real sentence.
    try:
        from mwnexus_parse import best_option_sentences
    except ImportError:
        return best(page_text)
    folder = record.get("path")
    label = str(record.get("label", "")) or (Path(folder).name if folder else "")
    names = [label]
    if folder and Path(folder).name.casefold() != label.casefold():
        names.append(Path(folder).name)
    for name in names:
        chosen = best_option_sentences(page_text, name, extra_terms=record.get("targets", []))
        if chosen:
            return "\n".join(chosen)
    return ""


def _is_hidden_required_option(item: dict) -> bool:
    """Recognize payload that is part of the mod's required base installation."""
    if item.get("informational_utility"):
        return False
    # A paired OpenMW/MWSE installer choice is resolved automatically: OpenMW is
    # hidden for this game and the MWSE payload is selected without a dialog.
    if item.get("preset_technology") == "mwse":
        return True
    if item.get("informational_utility") or item.get("always_optional"):
        return False
    if item.get("required"):
        return True
    if item.get("number") == 0 and not str(item.get("group_key", "")).startswith("fomod:"):
        return True
    if str(item.get("option_type", "")).casefold() == "required":
        return True
    label = str(item.get("label", "")).strip().casefold()
    path = item.get("path")
    path_name = path.name.casefold() if isinstance(path, Path) else ""
    core_label = re.sub(r"[._-]+", " ", Path(label).stem if item.get("plugin_choice") else label)
    if re.fullmatch(r"\s*(?:00\s*[- ]\s*)?(?:core(?:\s+(?:plugin|files?))?|main(?:\s+plugin)?|base(?:\s+files?)?|required(?:\s+files?)?)\s*", core_label):
        return True
    # Tidy may expose canonical Morrowind Data Files directories as apparent Extras.
    # Treat these as base payload folders, while leaving named optional packs selectable.
    if path_name in GAME_DIRS | {"data files"}:
        return True
    normalized_label = re.sub(r"[_-]+", " ", label).strip()
    if normalized_label in {"00 data files", "data files"}:
        return True
    if label in GAME_DIRS | {"data files"}:
        return True
    if item.get("plugin_choice") and CORE_STEM_RE.search(Path(label).stem):
        return True
    return False


def _is_openmw_normal_map_option(item: dict) -> bool:
    """Hide optional normal-map-only packages because vanilla Morrowind cannot use them."""
    if item.get("required") or str(item.get("option_type", "")).casefold() == "required":
        return False
    if item.get("has_plugin_payload"):
        return False
    path = item.get("path")
    description = " ".join((str(item.get("label", "")),
                            str(item.get("group_label", "")),
                            str(path) if path else ""))
    return bool(NORMAL_MAP_RE.search(description))


def _technology_choice(item: dict) -> str | None:
    """Identify explicit OpenMW/MWSE installer choices by label and payload paths."""
    label = str(item.get("label", ""))
    openmw_in_label = bool(OPENMW_RE.search(label))
    mwse_in_label = bool(MWSE_RE.search(label))
    if openmw_in_label != mwse_in_label:
        return "openmw" if openmw_in_label else "mwse"
    payload = " ".join(str(path) for path in item.get("all_payload_files", {}).values())
    payload += " " + " ".join(str(path) for path in item.get("payload_files", {}).values())
    path = item.get("path")
    if isinstance(path, Path) and path.is_dir():
        payload += " " + path.name
    if OPENMW_RE.search(payload):
        return "openmw"
    if MWSE_RE.search(payload):
        return "mwse"
    return None


def _select_vanilla_technology(records: list[dict]) -> list[dict]:
    """Hide OpenMW variants and lock the MWSE variant when a paired choice exists."""
    groups = {}
    for item in records:
        group_key = item.get("group_key", f"bain:{item.get('number', 0):02d}")
        groups.setdefault(group_key, []).append(item)
    hidden = set()
    for items in groups.values():
        kinds = {item["key"]: _technology_choice(item) for item in items}
        has_openmw = any(kind == "openmw" for kind in kinds.values())
        for item in items:
            kind = kinds[item["key"]]
            if MGE_XE_RE.search(str(item.get("label", ""))):
                item["required"] = True
                item["preset_technology"] = "mge xe"
                if item.get("all_payload_files") is not None:
                    item["payload_files"] = item["all_payload_files"]
            if kind == "openmw":
                hidden.add(item["key"])
            elif kind == "mwse" and has_openmw:
                item["required"] = True
                item["preset_technology"] = "mwse"
                # Optional FOMOD records normally omit plugin files from their
                # activation payload. The selected MWSE variant needs all declared files.
                if item.get("all_payload_files") is not None:
                    item["payload_files"] = item["all_payload_files"]
    return [item for item in records if item["key"] not in hidden]


def _show_selector(mod_name: str, records: list[dict], previous: set[str], force_targets: set[str],
                   answered_groups: set[str] | None = None) -> set[str] | None:
    answered_groups = answered_groups or set()
    records = _select_vanilla_technology(records)
    # These assets are OpenMW-only. Keep them out of both the UI and the activation
    # set; tidy also moves loose *_n / *_nm / *_normal textures under each mod's Off.
    records = [item for item in records if not _is_openmw_normal_map_option(item)]
    utility_alerts = []
    utility_scan_cache = {}
    utility_alerted = set()
    for item in records:
        source = item.get("path")
        folder = source if isinstance(source, Path) and source.is_dir() else \
                 source.parent if isinstance(source, Path) else Path(item.get("mod_dir", "."))
        item["_inspect_folder"] = folder
        scan_key = str(folder).casefold()
        if scan_key not in utility_scan_cache:
            descendants = list(_safe_rglob(folder))
            exe_files = [p for p in descendants if p.is_file() and p.suffix.casefold() == ".exe"]
            plugin_files = [p for p in descendants if p.is_file()
                            and p.suffix.casefold() in {".esm", ".esp"}]
            utility_scan_cache[scan_key] = (exe_files, plugin_files)
        exe_files, plugin_files = utility_scan_cache[scan_key]
        if exe_files:
            item["utility_folder"] = True
            package_root = Path(item.get("mod_dir") or folder)
            def relative_or_name(path):
                try:
                    return str(path.relative_to(package_root))
                except ValueError:
                    return path.name
            item["exe_files"] = [relative_or_name(p) for p in exe_files[:8]]
            item["nested_plugin_files"] = [relative_or_name(p) for p in plugin_files[:12]]
            if scan_key not in utility_alerted:
                utility_alerted.add(scan_key)
                utility_alerts.append(item)
    hidden_required = {item["key"] for item in records if _is_hidden_required_option(item)}
    records = [item for item in records if item["key"] not in hidden_required]
    if not records:
        return hidden_required
    try:
        import tkinter as tk
        from tkinter import ttk
        import mwmanager_dialogs as messagebox
    except ImportError:
        print("Tk is unavailable; option selection was left unchanged.")
        return None
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Tk option window unavailable ({exc}); option selection was left unchanged.")
        return None
    root.title(f"MWManager options — {mod_name}")
    from mwmanager_theme import COLORS, apply_theme
    apply_theme(root)
    screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
    window_width = min(860, max(720, screen_width - 80))
    window_height = min(680, max(480, screen_height - 100))
    window_x = max(0, (screen_width - window_width) // 2)
    window_y = max(0, (screen_height - window_height) // 2)
    root.geometry(f"{window_width}x{window_height}+{window_x}+{window_y}")
    root.minsize(min(720, window_width), min(480, window_height))
    normalized_mod_name = re.sub(r"[^a-z0-9]+", " ", mod_name.casefold())
    is_tamriel_data_hd = "tamriel data" in normalized_mod_name and bool(re.search(r"\bhd\b", normalized_mod_name))
    if is_tamriel_data_hd:
        notice = ("Tamriel Data (HD) package detected. The 00 Data Files master is required; "
                  "optional HD/normal-map content can remain in Extras unless selected.")
        ttk.Label(root, text=notice, wraplength=820).pack(fill="x", padx=12, pady=(0, 8))
    elif any(item.get("informational_utility") for item in records):
        ttk.Label(root, text="Optional package utility is included; it is not required for the master. See Details before choosing it.",
                  wraplength=820).pack(fill="x", padx=12, pady=(0, 8))
    ttk.Label(root, text=mod_name, style="H2.TLabel").pack(fill="x", padx=12, pady=(12, 0))
    ttk.Label(root, text="Choose options. Required options are locked; exclusive groups allow one choice.",
              style="Muted.TLabel", wraplength=820).pack(fill="x", padx=12, pady=(2, 10))
    footer = ttk.Frame(root)
    footer.pack(side="bottom", fill="x", padx=12, pady=10)
    body = ttk.Frame(root)
    body.pack(fill="both", expand=True, padx=10, pady=4)
    left_pane = ttk.Frame(body, width=390)
    left_pane.pack(side="left", fill="y", padx=(0, 10))
    left_canvas = tk.Canvas(left_pane, highlightthickness=1, highlightbackground=COLORS["border"],
                            background=COLORS["canvas"], width=390)
    left_scrollbar = ttk.Scrollbar(left_pane, orient="vertical", command=left_canvas.yview)
    left_canvas.configure(yscrollcommand=left_scrollbar.set)
    left_scrollbar.pack(side="right", fill="y")
    left_canvas.pack(side="left", fill="both", expand=True)
    left = ttk.Frame(left_canvas)
    left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")
    left.bind("<Configure>", lambda _event: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
    left_canvas.bind("<Configure>", lambda event: left_canvas.itemconfigure(left_window, width=event.width))

    def scroll_options(event):
        x1, y1 = left_canvas.winfo_rootx(), left_canvas.winfo_rooty()
        if not (x1 <= event.x_root <= x1 + left_canvas.winfo_width()
                and y1 <= event.y_root <= y1 + left_canvas.winfo_height()):
            return
        if getattr(event, "num", None) == 4:
            amount = -1
        elif getattr(event, "num", None) == 5:
            amount = 1
        else:
            amount = -1 if event.delta > 0 else 1
        left_canvas.yview_scroll(amount, "units")
        return "break"

    root.bind_all("<MouseWheel>", scroll_options)
    root.bind_all("<Button-4>", scroll_options)
    root.bind_all("<Button-5>", scroll_options)
    detail_pane = ttk.Frame(body)
    detail_pane.pack(side="right", fill="both", expand=True)
    preview = ttk.Label(detail_pane, anchor="center")
    preview.pack(fill="x", padx=6, pady=(4, 8))
    preview.image = None
    preview.bind("<Button-1>", lambda _event: open_image_preview())
    preview.bind("<Enter>", lambda _event: preview.configure(cursor="hand2"))
    preview.bind("<Leave>", lambda _event: preview.configure(cursor=""))
    preview_hint = ttk.Label(detail_pane, text="Click image to inspect", anchor="center")
    preview_hint.pack(fill="x", padx=6, pady=(0, 4))
    search_bar = ttk.Frame(detail_pane)
    search_bar.pack(fill="x")
    info_state = {"item": None, "status": {}}
    image_window_state = {"window": None, "label": None, "photo": None, "path": None}
    detail = tk.Text(detail_pane, wrap="word", width=52, height=30,
                     background=COLORS["field"], foreground=COLORS["text"],
                     insertbackground=COLORS["gold"], selectbackground=COLORS["select"],
                     selectforeground=COLORS["text_strong"], relief="flat", padx=8, pady=8,
                     highlightthickness=1, highlightbackground=COLORS["border"])
    detail_scrollbar = ttk.Scrollbar(detail_pane, orient="vertical", command=detail.yview)
    detail.configure(yscrollcommand=detail_scrollbar.set)
    detail.pack(side="left", fill="both", expand=True)
    detail_scrollbar.pack(side="right", fill="y")
    states, selected = {}, set(hidden_required)
    grouped = {}
    for item in records:
        group_key = item.get("group_key", f"bain:{item['number']:02d}")
        grouped.setdefault(group_key, []).append(item)

    page_text = ""

    def search_for_info():
        item = info_state["item"]
        if not item:
            return
        mod_dir = Path(item.get("mod_dir") or item["path"])
        key = item["key"]
        cache_key = str(mod_dir.resolve()).casefold()
        retrying = bool(_NEXUS_FETCH_ERRORS.get(cache_key))
        if retrying:
            # Once direct HTTP has failed, retry through the visible session so
            # page content and any CAPTCHA clearance come from the same browser.
            read_nexus_browser_page(search_button)
            return
        info_state["status"][key] = "Searching the mod page for more information…"
        search_button.configure(state="disabled", text="Searching Nexus…")
        show(item)
        page_url = _nexus_page_url(mod_dir)
        search_title = _search_mod_title(mod_dir, mod_name)
        query = f"{search_title} Morrowind mod"

        def open_web_search():
            try:
                if page_url:
                    import mwnexus_browser
                    mwnexus_browser.open_page(page_url)
                    info_state["status"][key] = (
                        "Nexus opened in the MWManager browser. Complete any verification, "
                        "then click Read page from browser."
                    )
                else:
                    url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
                    webbrowser.open(url)
            except Exception as exc:
                info_state["status"][key] = f"Could not open the MWManager Nexus browser: {exc}"

        # On the first manual search, open the page so the user can read it or
        # complete a site challenge. A retry should only repeat the fetch; the
        # separate footer button opens Nexus when the user wants it.
        if not retrying:
            open_web_search()

        def fetch():
            found = _nexus_page_text(mod_dir, refresh=True)

            def finish():
                nonlocal page_text
                page_text = found
                search_button.configure(state="normal")
                if found:
                    info_state["status"][key] = "Page text retrieved. Review the result below."
                    search_button.configure(text="Search for more info")
                else:
                    cache_key = str(mod_dir.resolve()).casefold()
                    info_state["status"][key] = _NEXUS_FETCH_ERRORS.get(
                        cache_key,
                        "Nexus returned no readable page text. The web search is open so you can check for alternate information."
                    )
                    search_button.configure(text="Retry from browser")
                show(item)

            try:
                root.after(0, finish)
            except tk.TclError:
                pass

        threading.Thread(target=fetch, daemon=True).start()

    def read_nexus_browser_page(trigger_button=None):
        item = info_state["item"]
        if not item:
            return
        mod_dir = Path(item.get("mod_dir") or item["path"])
        key = item["key"]
        mod_id = _nexus_mod_id(mod_dir)
        if not mod_id:
            info_state["status"][key] = "No valid Nexus mod ID is available for this package."
            if trigger_button:
                trigger_button.configure(state="normal", text="Retry from browser")
            show(item)
            return
        info_state["status"][key] = "Reading the rendered Nexus page from the MWManager browser…"
        capture_started = time.monotonic()
        if trigger_button:
            trigger_button.configure(state="disabled", text="Reading browser page…")
        read_browser_button.configure(state="disabled")
        show(item)

        def capture():
            try:
                import mwnexus_browser
                page = mwnexus_browser.capture_page(mod_id)
                mwnexus_browser.cache_page(mod_id, page)
                found = page["text"]
                error = ""
            except Exception as exc:
                found = ""
                error = str(exc)

            def finish():
                nonlocal page_text
                read_browser_button.configure(state="normal")
                if trigger_button:
                    trigger_button.configure(
                        state="normal", text="Search for more info" if found else "Retry from browser"
                    )
                if found:
                    page_text = found
                    cache_key = str(mod_dir.resolve()).casefold()
                    _NEXUS_TEXT_CACHE[cache_key] = found
                    _NEXUS_FETCH_ERRORS.pop(cache_key, None)
                    search_button.configure(text="Search for more info")
                    info_state["status"][key] = "Page read from the MWManager browser and cached for future lookups."
                else:
                    info_state["status"][key] = (
                        f"Browser page was not captured: {error} Open the Nexus page, finish any "
                        "verification, then click Read page from browser."
                    )
                show(item)

            try:
                # Keep the busy state visible even when the local DevTools check
                # fails immediately, so a click never appears to do nothing.
                delay_ms = max(0, 650 - int((time.monotonic() - capture_started) * 1000))
                root.after(delay_ms, finish)
            except tk.TclError:
                pass

        threading.Thread(target=capture, daemon=True).start()

    search_button = ttk.Button(search_bar, text="Search for more info", command=search_for_info)
    read_browser_button = ttk.Button(search_bar, text="Read page from browser",
                                     command=read_nexus_browser_page)

    def refresh_image_window(path: Path, raise_window=False):
        window = image_window_state["window"]
        if not window or not window.winfo_exists():
            image_window_state.update(window=None, label=None, photo=None, path=None)
            return False
        max_width = min(1100, max(320, window.winfo_screenwidth() - 80))
        max_height = min(800, max(240, window.winfo_screenheight() - 120))
        image = _load_fomod_preview(path, tk, (max_width, max_height))
        if image is None:
            return False
        image_window_state["photo"] = image
        image_window_state["path"] = Path(path)
        image_window_state["label"].configure(image=image)
        window.title("FOMOD image — " + Path(path).name)
        window.geometry(f"{image.width()}x{image.height()}")
        if raise_window:
            window.deiconify()
            window.attributes("-topmost", True)
            window.lift()
            window.focus_force()
        return True

    def open_image_preview():
        path = getattr(preview, "image_path", None)
        if not path or not Path(path).is_file():
            return
        if refresh_image_window(Path(path), raise_window=True):
            return
        window = tk.Toplevel(root)
        window.title("FOMOD image — " + Path(path).name)
        max_width = min(1100, max(320, window.winfo_screenwidth() - 80))
        max_height = min(800, max(240, window.winfo_screenheight() - 120))
        image = _load_fomod_preview(path, tk, (max_width, max_height))
        if image is None:
            window.destroy()
            return
        width = min(max_width, image.width())
        height = min(max_height, image.height())
        screen_width, screen_height = window.winfo_screenwidth(), window.winfo_screenheight()
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        window.transient(root)
        window.geometry(f"{width}x{height}+{x}+{y}")
        image_view = ttk.Label(window, image=image, anchor="center")
        image_view.image = image
        image_view.pack(fill="both", expand=True)
        image_window_state.update(window=window, label=image_view, photo=image, path=Path(path))
        window.attributes("-topmost", True)
        window.lift()
        window.focus_force()

        def close_image_window():
            image_window_state.update(window=None, label=None, photo=None, path=None)
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", close_image_window)

    def show(item, target=detail):
        if target is detail:
            info_state["item"] = item
            preview_path = item.get("image_path")
            preview_image = _load_fomod_preview(preview_path, tk) if preview_path else None
            preview.configure(image=preview_image if preview_image else "")
            preview.image = preview_image
            preview.image_path = preview_path if preview_image else None
            preview_hint.configure(text="Click image to inspect" if preview_image else "")
            if preview_image and image_window_state["window"]:
                refresh_image_window(preview_path)
        target.configure(state="normal")
        target.delete("1.0", "end")
        caption = item.get("group_label") or f"{item['number']:02d}"
        target.insert("end", f"{caption} — {item['label']}\n\n")
        if item.get("informational_utility"):
            target.insert("end", "Optional package: Tamriel Data (HD). This is included as an optional package utility and is not required for the Tamriel Data master installation.\n\n")
        if item.get("utility_folder"):
            target.insert("end", "EXECUTABLE DETECTED: this folder contains an .exe utility. Any ESP/ESM files listed here may be inputs or outputs for that utility rather than installable plugins. Inspect the folder before selecting it.\n")
            target.insert("end", "Executable(s): " + ", ".join(item.get("exe_files", [])) + "\n")
            if item.get("nested_plugin_files"):
                target.insert("end", "Nested ESP/ESM files (also offered separately where detected): "
                              + ", ".join(item["nested_plugin_files"]) + "\n\n")
            else:
                target.insert("end", "\n")
        if item.get("preset_technology") == "mwse":
            target.insert("end", "MWSE version selected automatically and locked. OpenMW files are unsupported by this vanilla Morrowind install.\n\n")
        if item.get("badges"):
            target.insert("end", "Readme notes: " + " · ".join(item["badges"]) + "\n\n")
        if item.get("evidence"):
            target.insert("end", "Plugin grouping: " + item["evidence"] + ".\n\n")
        if item.get("option_type"):
            target.insert("end", f"FOMOD choice type: {item['option_type']}\n\n")
        if item["targets"]:
            target.insert("end", "Compatibility plugins: " + ", ".join(item["targets"]) + "\n\n")
        if item.get("missing_masters"):
            target.insert("end", "MISSING MASTERS: " + ", ".join(item["missing_masters"])
                          + "\nThis option may be installed, but Morrowind may refuse to load it or abort at runtime.\n\n")
        if item.get("fomod_description"):
            target.insert("end", item["fomod_description"] + "\n\n")
        description = _option_description(item, Path(item.get("mod_dir") or item["path"]), page_text)
        note = _maintainer_notes(item)
        if description:
            target.insert("end", "Suggested option description:\n" + description + "\n\n")
        elif item.get("fallback_text"):
            target.insert("end", "No readme text found for this option.\n" + item["fallback_text"] + "\n\n")
        elif not item.get("fomod_description") and not note:
            target.insert("end", "No relevant option-specific documentation found.\n\n")
        if note:                                   # last: after the FOMOD, readme and mod-page text
            target.insert("end", "MWManager Insight:\n" + note + "\n\n")
        status = info_state["status"].get(item["key"])
        if status:
            target.insert("end", status)
        if target is detail:
            if not (item.get("fomod_description") or description):
                search_button.pack(side="left", fill="x", expand=True, padx=(6, 3), pady=(0, 6))
                read_browser_button.pack(side="left", fill="x", expand=True, padx=(3, 6), pady=(0, 6))
            else:
                search_button.pack_forget()
                read_browser_button.pack_forget()
        target.configure(state="disabled")

    def show_details(item):
        window = tk.Toplevel(root)
        window.title("Option details — " + item["label"])
        detail_width, detail_height = 620, 480
        window.update_idletasks()
        parent_x, parent_y = root.winfo_rootx(), root.winfo_rooty()
        parent_width, parent_height = root.winfo_width(), root.winfo_height()
        detail_x = max(0, parent_x + (parent_width - detail_width) // 2)
        detail_y = max(0, parent_y + (parent_height - detail_height) // 2)
        window.geometry(f"{detail_width}x{detail_height}+{detail_x}+{detail_y}")
        window.transient(root)
        text = tk.Text(window, wrap="word")
        scrollbar = ttk.Scrollbar(window, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        text.insert("end", "Loading option details…")
        text.configure(state="disabled")
        window.lift()
        window.focus_force()
        window.after(50, lambda: show(item, text))

    def inspect_folder(item):
        folder = item.get("_inspect_folder")
        try:
            if not folder or not Path(folder).is_dir():
                raise FileNotFoundError(f"Option folder is unavailable: {folder}")
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(folder)])
        except (OSError, FileNotFoundError) as exc:
            messagebox.showerror("Could not open option folder", str(exc), parent=root)

    def option_row(box, item):
        row = ttk.Frame(box)
        row.pack(fill="x", padx=5, pady=2)
        ttk.Button(row, text="Open Folder", command=lambda item=item: inspect_folder(item),
                   width=11).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Details", command=lambda item=item: show_details(item),
                   width=9).pack(side="right", padx=(6, 0))
        return row

    for group_key, items in grouped.items():
        first = items[0]
        number = first["number"]
        selection_type = first.get("selection_type", "SelectExactlyOne" if len(items) > 1 else "SelectAny")
        is_bain = not first.get("group_key")
        exclusive = selection_type.casefold() in {"selectexactlyone", "selectatmostone"}
        select_all = selection_type.casefold() == "selectall"
        label = first.get("group_label", f"{number:02d}")
        box = ttk.LabelFrame(left, text=label + (" — choose one" if exclusive else
                               " — required" if select_all or any(i["required"] for i in items) else " — optional"))
        box.pack(fill="x", pady=4)
        required = [i for i, item in enumerate(items) if item["required"] and not item.get("informational_utility")]
        if required:
            defaults = required
        elif group_key in answered_groups:
            # An explicit answer, including an empty group, always wins over current
            # recommendations and compatibility heuristics.
            defaults = [i for i, item in enumerate(items)
                        if item["key"] in previous and not item.get("informational_utility")]
        else:
            defaults = [i for i, item in enumerate(items)
                        if not item.get("informational_utility") and not item.get("always_optional")
                        and (item.get("recommended") or (set(item["compatible"]) & force_targets))]
            if not defaults:
                defaults = [i for i, item in enumerate(items) if item["compatible"]
                            and not item.get("informational_utility") and not item.get("always_optional")]
            if not defaults and first.get("plugin_choice") and not exclusive:
                defaults = [i for i, item in enumerate(items) if not item.get("informational_utility")]
            if not defaults and first.get("plugin_choice") and exclusive:
                defaults = [i for i, item in enumerate(items) if not item.get("informational_utility")][:1]
        if is_bain and number == 0:
            for item in items:
                row = option_row(box, item)
                if item.get("informational_utility"):
                    ttk.Button(row, text=item["label"] + "  (optional package; stored in Extras)",
                               command=lambda item=item: show_details(item)).pack(side="left", fill="x", expand=True)
                    continue
                selected.add(item["key"])
                ttk.Button(row, text="✓ " + item["label"] + "  (required)",
                           command=lambda item=item: show(item)).pack(side="left", fill="x", expand=True)
        elif select_all:
            selected.update(item["key"] for item in items)
            for item in items:
                row = option_row(box, item)
                ttk.Button(row, text="✓ " + item["label"] + "  (required)",
                           command=lambda item=item: show(item)).pack(side="left", fill="x", expand=True)
        elif required and exclusive:
            selected.add(items[required[0]]["key"])
            for i, item in enumerate(items):
                row = option_row(box, item)
                if i in required:
                    ttk.Button(row, text="✓ " + item["label"] + "  (required)",
                               command=lambda item=item: show(item)).pack(side="left", fill="x", expand=True)
                else:
                    ttk.Label(row, text=item["label"] + "  (not selectable in this required group)").pack(side="left", fill="x", expand=True)
        elif exclusive and len(items) > 1:
            variable = tk.IntVar(value=defaults[0] if defaults else -1)
            states[group_key] = ("radio", variable, items)
            for i, item in enumerate(items):
                row = option_row(box, item)
                ttk.Radiobutton(row, text=item["label"], value=i, variable=variable,
                                command=lambda item=item: show(item)).pack(side="left", fill="x", expand=True)
        elif required:
            for i, item in enumerate(items):
                row = option_row(box, item)
                if item["required"]:
                    selected.add(item["key"])
                    ttk.Button(row, text="✓ " + item["label"] + "  (required)",
                               command=lambda item=item: show(item)).pack(side="left", fill="x", expand=True)
                else:
                    variable = tk.BooleanVar(value=(items.index(item) in defaults))
                    states[item["key"]] = ("check", variable, item)
                    ttk.Checkbutton(row, variable=variable,
                                    command=lambda item=item: show(item)).pack(side="left")
                    label = ttk.Label(row, text=item["label"], anchor="w", cursor="hand2")
                    label.pack(side="left", fill="x", expand=True)
                    label.bind("<Button-1>", lambda _event, item=item: show(item))
        else:
            for i, item in enumerate(items):
                row = option_row(box, item)
                variable = tk.BooleanVar(value=(i in defaults))
                states[item["key"]] = ("check", variable, item)
                ttk.Checkbutton(row, variable=variable,
                                command=lambda item=item: show(item)).pack(side="left")
                label = ttk.Label(row, text=item["label"], anchor="w", cursor="hand2")
                label.pack(side="left", fill="x", expand=True)
                label.bind("<Button-1>", lambda _event, item=item: show(item))
    result = {"value": None}
    source_archive = Path(os.environ["MWMANAGER_SOURCE_ARCHIVE"]) \
        if os.environ.get("MWMANAGER_SOURCE_ARCHIVE") else None
    nexus_mod_dir = Path(records[0].get("mod_dir") or records[0]["path"]) if records else None
    if (source_archive is None or not source_archive.is_file()) and nexus_mod_dir:
        from core_addons import staged_source_archive      # the staging record, wherever the window was opened from
        source_archive = staged_source_archive(nexus_mod_dir)
    nexus_page = _nexus_page_url(nexus_mod_dir) if nexus_mod_dir else ""

    def inspect_source_archive():
        if source_archive is None or not source_archive.is_file():
            messagebox.showinfo("Source archive unavailable",
                                "MWManager does not have an available source archive for this option screen.",
                                parent=root)
            return
        try:
            root.attributes("-topmost", False)
            root.update_idletasks()
            os.startfile(str(source_archive))
        except OSError as exc:
            messagebox.showerror("Could not open source archive", str(exc), parent=root)

    def open_nexus_page():
        if not nexus_page:
            return
        try:
            import mwnexus_browser
            mwnexus_browser.open_page(nexus_page)
            current = info_state.get("item")
            if current:
                info_state["status"][current["key"]] = (
                    "Nexus opened in the MWManager browser. Complete any verification there, "
                    "then click Read page from browser."
                )
                show(current)
        except Exception as exc:
            messagebox.showerror("Could not open Nexus page", str(exc), parent=root)

    def accept():
        chosen = set(selected)
        for key, (kind, variable, items) in states.items():
            if kind == "check":
                if variable.get():
                    chosen.add(key)
            elif 0 <= variable.get() < len(items):
                chosen.add(items[variable.get()]["key"])
        for group_key, items in grouped.items():

            rule = items[0].get("selection_type", "")
            if rule.casefold() in {"selectexactlyone", "selectatleastone"} \
                    and not any(item["key"] in chosen for item in items):
                messagebox.showwarning("Choose a required option",
                                       f"Select at least one option in {items[0].get('group_label', group_key)}.",
                                       parent=root)
                return
        missing = sorted({master for item in records if item["key"] in chosen
                          for master in item.get("missing_masters", [])})
        if missing:
            messagebox.showwarning(
                "Selected option has missing masters",
                "The selected option requires master plugin(s) that are not installed:\n\n"
                + "\n".join(missing)
                + "\n\nYou can continue installing it, but Morrowind may refuse to load the plugin or abort at runtime.",
                parent=root)
        result["value"] = chosen
        root.destroy()

    ttk.Button(footer, text="Apply selected options", style="Primary.TButton", command=accept).pack(side="right")
    if nexus_page:
        if nexus_page:
            ttk.Button(footer, text="Open Nexus in MWManager browser", command=open_nexus_page).pack(
                side="right", padx=(0, 8))
    ttk.Button(footer, text="Open source archive", command=inspect_source_archive).pack(side="right", padx=(0, 8))
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    if records:
        show(records[0])
    root.update_idletasks()
    root.lift()
    root.attributes("-topmost", True)
    root.focus_force()
    root.after(500, lambda: root.attributes("-topmost", False))
    if utility_alerts:
        summaries = []
        for item in utility_alerts:
            plugin_summary = ", ".join(item.get("nested_plugin_files", [])) or "none found in this folder"
            summaries.append(f"{item['label']}\n"
                             f"  Executable(s): {', '.join(item.get('exe_files', []))}\n"
                             f"  Nested ESP/ESM file(s): {plugin_summary}")
        root.after(150, lambda: messagebox.showwarning(
            "Executable utility found",
            "One or more option folders contain an .exe utility. Any nested ESP/ESM files may be utility inputs or outputs, not intended for installation. "
            "The plugins remain available as separate choices; use Open Folder to inspect the files.\n\n"
            + "\n\n".join(summaries), parent=root))
    try:
        root.mainloop()
    finally:
        # Apply, the window X, or an error: the options screen is gone, so close the
        # MWManager Nexus browser it may have opened. Runs in the background; a
        # frozen or already-closed browser never delays the install.
        _close_nexus_browser_after_dialog()
    return result["value"]


def _close_nexus_browser_after_dialog():
    """Close the Nexus browser when the options dialog closes (opt out: MWMANAGER_KEEP_NEXUS_BROWSER=1)."""
    if os.environ.get("MWMANAGER_KEEP_NEXUS_BROWSER", "").strip() in {"1", "true", "yes"}:
        return None
    try:
        import mwnexus_browser
        return mwnexus_browser.close_browser_in_background(timeout=5.0)
    except Exception:
        return None


def _payload_files(option: dict) -> dict[str, Path]:
    if "payload_files" in option:
        return dict(option["payload_files"])
    root: Path = option["path"]
    data_roots = [p for p in _safe_rglob(root) if p.is_dir() and p.name.casefold() == "data files"]
    payload_root = sorted(data_roots, key=lambda p: len(p.parts))[0] if data_roots else root
    result = {}
    for base, dirs, files in os.walk(payload_root):
        dirs[:] = [d for d in dirs if d.casefold() not in {"off", "docs", "fomod", "extras", "screenshots"}]
        for filename in files:
            source = Path(base) / filename
            rel = source.relative_to(payload_root)
            top = rel.parts[0].casefold() if len(rel.parts) > 1 else ""
            ext = source.suffix.casefold()
            if option.get("utility_folder") and ext in {".esp", ".esm", ".omwaddon"}:
                continue
            if top in GAME_DIRS or ext in PAYLOAD_EXTS:
                if rel.parts and rel.parts[0].casefold() == "sounds":
                    rel = Path("Sound", *rel.parts[1:])
                result[str(rel)] = source
    return result


def _safe_backup_path(backup_root: Path, rel: str) -> Path:
    from core_addons import stamped_backup_path
    target = stamped_backup_path(backup_root / Path(rel))     # recorded in the activation journal
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _prune_option_backups(backup_root: Path, activation: dict) -> None:
    """Keep only source files still referenced by the active selection journal."""
    if not backup_root.is_dir():
        return
    referenced = set()
    for info in activation.get("files", {}).values():
        if info.get("backup"):
            try:
                referenced.add(Path(info["backup"]).resolve())
            except (OSError, ValueError):
                continue
    for path in sorted(backup_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_file() and path.resolve() not in referenced:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            continue
    try:
        if not any(backup_root.iterdir()):
            backup_root.rmdir()
    except OSError:
        pass


def _activate(mod_dir: Path, plugin: str, records: list[dict], chosen: set[str], prior_state: dict,
              backup_root: Path) -> dict:
    reusable = _restore_activation(mod_dir, prior_state)
    for rel, backup_name in list(reusable.items()):
        if not backup_name:
            continue
        old_backup = Path(backup_name)
        if not old_backup.is_absolute():
            old_backup = mod_dir / old_backup
        if not old_backup.is_file():
            reusable[rel] = None
            continue
        try:
            old_backup.resolve().relative_to(backup_root.resolve())
            continue
        except ValueError:
            pass
        new_backup = _safe_backup_path(backup_root, rel)
        shutil.copy2(old_backup, new_backup)
        reusable[rel] = str(new_backup.resolve())
        # Migrate backups created by earlier versions out of the staged Off tree.
        try:
            old_backup.unlink()
            off_marker = (mod_dir / "Off").resolve()
            parent = old_backup.parent
            while parent.resolve() != off_marker and off_marker in parent.resolve().parents:
                if any(parent.iterdir()):
                    break
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass
    desired = {}
    for option in records:
        if option["key"] in chosen and not option.get("plugin_choice"):
            desired.update(_payload_files(option))      # ascending number: higher groups win overlaps
    new_state = {}
    kept_in_extras = None
    for rel, source in desired.items():
        destination = mod_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup_rel = reusable.get(rel)
        if destination.exists() and source.resolve() == destination.resolve():
            new_state[rel] = {"backup": backup_rel, "source": str(source)}
            continue
        restore_from = None
        if destination.exists() and backup_rel is None:
            # A file Tidy linked in from a 00 folder still has its copy under Extras: no backup is needed, undo
            # links it back. Anything else (a package's own loose Textures\...) is backed up first.
            if kept_in_extras is None:
                kept_in_extras = _extras_file_ids(mod_dir)
            stat = destination.stat()
            restore_from = kept_in_extras.get((stat.st_dev, stat.st_ino))
            if restore_from is None:
                backup = _safe_backup_path(backup_root, rel)
                shutil.copy2(destination, backup)
                backup_rel = str(backup.resolve())
        if destination.exists():
            destination.unlink()          # never write through a hard link into the Extras or Data Files copy
        shutil.copy2(source, destination)
        new_state[rel] = {"backup": backup_rel, "source": str(source)}
        if restore_from:
            new_state[rel]["restore_from"] = restore_from
    result = {"files": new_state, "selected": sorted(chosen)}
    _prune_option_backups(backup_root, result)
    return result


def _extras_file_ids(mod_dir: Path) -> dict[tuple[int, int], str]:
    """(device, file id) -> path for every file under the package's Extras: a root file with the same id is a hard
    link Tidy made from a 00 folder kept there."""
    ids = {}
    extras = next((p for p in mod_dir.iterdir() if p.is_dir() and p.name.casefold() == "extras"), None)
    for path in _safe_rglob(extras) if extras else ():
        try:
            if path.is_file():
                stat = path.stat()
                ids[(stat.st_dev, stat.st_ino)] = str(path)
        except OSError:
            continue
    return ids


def _restore_activation(mod_dir: Path, prior_state: dict) -> dict[str, str | None]:
    reusable = {}
    for rel, info in prior_state.get("files", {}).items():
        destination = mod_dir / rel
        backup = mod_dir / info["backup"] if info.get("backup") else None
        source = Path(info.get("source", ""))
        # Tidy may consolidate an identical option source into its active target.
        # Keep that sole surviving copy instead of deleting data we cannot reapply.
        if destination.exists() and not (source.is_file() or (backup and backup.is_file())):
            reusable[rel] = None
            continue
        if destination.exists():
            destination.unlink()
        kept = Path(info["restore_from"]) if info.get("restore_from") else None
        if backup and backup.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, destination)
            reusable[rel] = info.get("backup")
        elif kept and kept.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(kept, destination)      # the 00 Core file again, from its copy under Extras
            except OSError:
                shutil.copy2(kept, destination)
            reusable[rel] = None
        else:
            reusable[rel] = None
    return reusable


def _save_journal(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _selection_identity(mod_dir: Path) -> str:
    """Key saved choices to the Vortex/Nexus package, not a shared plugin filename."""
    try:
        from mwdialog_assets import nexus_id, vortex_mod_metadata
        metadata = vortex_mod_metadata(str(mod_dir))
        mod_id = metadata.get("mod_id") or nexus_id(str(mod_dir))
    except (ImportError, AttributeError, OSError):
        mod_id = None
    if mod_id:
        return "nexus:" + str(mod_id).casefold()

    marker = {}
    for path in _safe_rglob(mod_dir):
        if path.is_file() and path.name.casefold() == ".mwmanager-stage.json":
            try:
                marker = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                continue
    digest = str(marker.get("sha256", "")).casefold()
    if digest:
        return "sha256:" + digest
    masters = sorted(Path(name).name.casefold() for name in marker.get("selected_masters", []) if name)
    return "masters:" + "|".join(masters) if masters else "package:" + mod_dir.name.casefold()


def _option_backup_root(output_dir: Path, identity: str) -> Path:
    digest = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()
    return output_dir / "mwmanager_option_backups" / digest


def _legacy_master_identity(mod_dir: Path) -> str | None:
    """Read the old master-filename key so unique legacy choices can migrate safely."""
    for path in _safe_rglob(mod_dir):
        if path.is_file() and path.name.casefold() == ".mwmanager-stage.json":
            try:
                marker = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            masters = sorted(Path(name).name.casefold() for name in marker.get("selected_masters", []) if name)
            return "masters:" + "|".join(masters) if masters else None
    return None


def _answer_file_path(output_dir: Path, identity: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", identity).strip("._-") or "package"
    return output_dir / "fomod_answers" / f"{safe}.json"


def _load_answer_file(output_dir: Path, identity: str) -> dict | None:
    path = _answer_file_path(output_dir, identity)
    try:
        answer = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return answer if answer.get("identity") == identity and isinstance(answer.get("selected"), list) else None


PENDING_FILE = "mwmanager_pending_options.json"


def record_pending_options(output_dir: Path, mod_dir: Path, records: list[dict], chosen: set[str],
                           installed: set[str]) -> dict:
    """The chosen options of one package that need a plugin that is neither installed nor staged (an option
    plugin's master), kept in PENDING_FILE as {package: {option key: {"label", "needs", "status"}}} for the
    batch to fetch first; a package with none is dropped from it. Returns this package's entries."""
    have = {name.casefold() for name in installed}
    waiting = {}
    for item in records:
        needs = sorted(m for m in (item.get("missing_masters") or []) if m.casefold() not in have)
        if needs:
            # not chosen: kept so that when the mod arrives the user is offered the option again
            waiting[item["key"]] = {"label": item.get("label") or item["key"], "needs": needs,
                                    "status": "waiting" if item["key"] in chosen else "not chosen"}
    path = Path(output_dir) / PENDING_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if waiting:
        data[mod_dir.name] = waiting
    else:
        data.pop(mod_dir.name, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    for item in waiting.values():
        if item["status"] == "waiting":
            print(f"  {mod_dir.name}: option '{item['label']}' waits on {', '.join(item['needs'])} (not installed)")
    return waiting


def held_options(records: list[dict], chosen: set[str], installed: set[str]) -> set[str]:
    """Chosen options held back: one of their plugins needs a master that is neither installed nor staged."""
    have = {name.casefold() for name in installed}
    return {item["key"] for item in records if item["key"] in chosen
            and any(m.casefold() not in have for m in (item.get("missing_masters") or []))}


def ask_see_options(package: str, arrived: list[str], labels: list[str]) -> bool:
    """A mod that an option the user didn't choose works with has just been installed: offer to show it."""
    text = (f"You just installed {', '.join(arrived)}.\n\n{package} has an optional component you didn't choose "
            f"before that works with it: {', '.join(labels)}.\n\nSee it now?")
    try:
        import tkinter as tk
        import mwmanager_dialogs as messagebox
        root = tk.Tk()
    except Exception:                       # no display: the option window couldn't open either
        print(text.replace("\n\n", " "))
        return False
    root.withdraw()
    try:
        return bool(messagebox.askyesno("Optional component now available", text, parent=root))
    finally:
        root.destroy()


def _save_answer_file(output_dir: Path, identity: str, selected: set[str], records: list[dict],
                      previous_answer: dict | None = None) -> None:
    path = _answer_file_path(output_dir, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    answered = set((previous_answer or {}).get("answered_groups", []))
    answered.update(item.get("group_key", f"bain:{item.get('number', 0):02d}") for item in records)
    payload = {"version": 1, "identity": identity, "selected": sorted(selected),
               "answered_groups": sorted(answered), "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _legacy_answer_groups(records: list[dict]) -> set[str]:
    return {item.get("group_key", f"bain:{item.get('number', 0):02d}") for item in records}


def _reuse_saved_answers() -> bool:
    """mwmanager.ini [Install] reuse_fomod_answers = true (or MWMANAGER_REUSE_FOMOD=1): a package whose every
    option group was answered before installs with those answers, without opening the option window."""
    if os.environ.get("MWMANAGER_REUSE_FOMOD") == "1":
        return True
    ini = configparser.ConfigParser()
    try:
        ini.read(Path(__file__).with_name("mwmanager.ini"), encoding="utf-8")
        return ini.getboolean("Install", "reuse_fomod_answers", fallback=False)
    except (configparser.Error, ValueError, OSError):
        return False


def process_staging(staging: str | os.PathLike, data_files: str | os.PathLike,
                    output_dir: str | os.PathLike, *, only: list[str] | None = None,
                    reopen_existing: bool = False) -> None:
    _FOMOD_IMAGE_INDEX.clear()
    staging, data_files, output_dir = Path(staging), Path(data_files), Path(output_dir)
    journal_path = output_dir / "mwmanager_compatibility_journal.json"
    try:
        state = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"version": 1, "known_installed_plugins": [], "mods": {}}
    old_installed = set(state.get("known_installed_plugins", []))
    mods = state.setdefault("mods", {})
    saved_selections = state.setdefault("saved_selections", {})
    changed_mods = set()
    installed = _installed_plugins(data_files, old_installed)
    # Required staged masters may still be nested under Extras before activation.
    for candidate in staging.iterdir():
        if candidate.is_dir() and candidate.name.casefold() not in STAGING_RESERVED:
            installed.update(_available_staged_plugins(candidate))
    for entry in mods.values():
        if entry.get("mod_dir"):
            installed.update(_available_staged_plugins(Path(entry["mod_dir"])))
    eligible = []
    for p in sorted(staging.iterdir(), key=lambda x: x.name.casefold()):
        if p.name.startswith("."):
            continue
        if not p.is_dir() or p.name.casefold() in STAGING_RESERVED:
            continue
        if only and not any(p.name.casefold() == pat.casefold()
                            or fnmatch.fnmatch(p.name.casefold(), pat.casefold()) for pat in only):
            continue
        eligible.append(p)
    eligible_keys = {str(p.resolve()).casefold() for p in eligible}
    legacy_identity_counts = {}
    for package_dir in eligible:
        legacy_identity = _legacy_master_identity(package_dir)
        if legacy_identity:
            legacy_identity_counts[legacy_identity] = legacy_identity_counts.get(legacy_identity, 0) + 1

    # A later-installed plugin can make a previously unselected compatibility option relevant.
    compatibility_reviewed = set()
    for key, entry in list(mods.items()):
        targets = set(entry.get("targets", []))
        selected = set(entry.get("selected", []))
        reviewed = set(entry.get("reviewed_targets", []))
        newly_relevant = (targets & installed) - reviewed
        mod_dir = Path(entry.get("mod_dir", ""))
        if not newly_relevant or not mod_dir.is_dir() or key in eligible_keys:
            continue
        records = (_discover_fomod(mod_dir, installed) if _has_fomod(mod_dir)
                   else _discover(mod_dir, installed))
        records.extend(_discover_plugin_choices(mod_dir, installed, _represented_option_sources(records)))
        held = set(entry.get("held", []))
        pending = [r for r in records if set(r["targets"]) & newly_relevant and r["key"] not in selected]
        ready = {r["key"] for r in pending if r["key"] in held} - held_options(records, held, installed)
        if pending:
            identity = _selection_identity(mod_dir)
            answer = _load_answer_file(output_dir, identity)
            if ready:
                # chosen before, held for a mod that is here now: installed without asking again
                chosen = selected | held
                for r in pending:
                    if r["key"] in ready:
                        print(f"  {entry.get('plugin') or mod_dir.name}: installing option '{r.get('label') or r['key']}'"
                              f" now that what it needs is installed")
            else:
                print(f"\nCompatibility option(s) may now apply to {entry.get('plugin')}: "
                      f"{', '.join(sorted(newly_relevant))}")
                real = {p.name.casefold(): p.name for p in data_files.iterdir()
                        if p.suffix.casefold() in PLUGIN_EXTS} if data_files.is_dir() else {}
                if not ask_see_options(entry.get("plugin") or mod_dir.name,
                                       sorted(real.get(n, n) for n in newly_relevant),
                                       [r.get("label") or r["key"] for r in pending if r["key"] not in held]):
                    entry["reviewed_targets"] = sorted(reviewed | newly_relevant)   # No: not asked again
                    _save_journal(journal_path, state)
                    compatibility_reviewed.add(key)
                    continue
                answered = set(answer.get("answered_groups", [])) if answer else _legacy_answer_groups(records)
                chosen = _show_selector(mod_dir.name, records, selected | held, newly_relevant, answered)
            if chosen is not None:
                still = held_options(records, chosen, installed)
                backup_root = _option_backup_root(output_dir, identity)
                applied = _activate(mod_dir, entry.get("plugin", mod_dir.name), records, chosen - still,
                                    entry.get("activation", {}), backup_root)
                _apply_plugin_choices(mod_dir, records, chosen - still)
                entry["activation"] = applied
                entry["selected"] = applied["selected"]
                entry["held"] = sorted(still)
                entry["reviewed_targets"] = sorted(reviewed | newly_relevant)
                saved_selections[identity] = applied["selected"]
                _save_answer_file(output_dir, identity, set(applied["selected"]) | still, records, answer)
                record_pending_options(output_dir, mod_dir, records, set(applied["selected"]) | still, installed)
                changed_mods.add(mod_dir.name)
                _save_journal(journal_path, state)
                compatibility_reviewed.add(key)
                print(f"  Re-evaluated options for {entry.get('plugin')}.")

    for mod_dir in eligible:
        try:
            from nexus_updates import check_and_offer_update
            if check_and_offer_update(mod_dir, manager_dir=staging.parent):
                print(f"  {mod_dir.name}: Nexus file(s) downloaded to the Vortex downloads folder; skipped this staged copy.")
                print("  Re-stage the selected update and optional patch/hotfix files before continuing.")
                continue
        except Exception as exc:
            print(f"  {mod_dir.name}: Nexus update check unavailable: {exc}")
        if _has_fomod(mod_dir):
            records = _discover_fomod(mod_dir, installed)
            if not records:
                print(f"  {mod_dir.name}: FOMOD config was found but could not be parsed into options; left unchanged.")
                continue
            print(f"  {mod_dir.name}: using MWManager's FOMOD selection window.")
        else:
            records = _discover(mod_dir, installed)
        records.extend(_discover_plugin_choices(mod_dir, installed, _represented_option_sources(records)))
        if not _has_fomod(mod_dir) and len(records) == 1 and records[0].get("has_master_payload"):
            # A sole ESP/ESM-bearing BAIN folder is the package's core payload, not
            # an optional installer choice. Mark it required so it is activated
            # automatically and _show_selector can return without opening a window.
            records[0]["required"] = True
        if not records:
            continue
        plugin = next((p.name for p in sorted(mod_dir.iterdir(), key=lambda x: x.name.casefold())
                       if p.is_file() and p.suffix.casefold() in PLUGIN_EXTS), mod_dir.name)
        key = str(mod_dir.resolve()).casefold()
        entry = mods.get(key, {})
        selection_identity = _selection_identity(mod_dir)
        # The staging directory is freshly extracted between install attempts. Only the
        # answer file carries choices forward; activation paths/backups belong to that
        # old extraction and must never suppress this installer's selection pass.
        answer = _load_answer_file(output_dir, selection_identity)
        answered_groups = set(answer.get("answered_groups", [])) if answer else set()
        previous = set(answer.get("selected", [])) if answer else set(saved_selections.get(selection_identity, []))
        if answer is None and selection_identity in saved_selections:
            answered_groups = _legacy_answer_groups(records)
        if answer is None and selection_identity not in saved_selections and entry.get("selection_identity") == selection_identity:
            previous = set(entry.get("selected", []))
            answered_groups = _legacy_answer_groups(records)
        legacy_identity = _legacy_master_identity(mod_dir)
        if (answer is None and not previous and legacy_identity
                and legacy_identity_counts.get(legacy_identity) == 1
                and legacy_identity in saved_selections):
            previous = set(saved_selections[legacy_identity])
            saved_selections[selection_identity] = sorted(previous)
            answered_groups = _legacy_answer_groups(records)
        if answer is None and not previous and entry.get("mod_dir") and \
                str(Path(entry["mod_dir"]).resolve()).casefold() == str(mod_dir.resolve()).casefold():
            # Migrate path-specific choices from the previous journal format. Never migrate its
            # activation/backup paths: a clean archive extraction has fresh payload files.
            previous = set(entry.get("selected", []))
            saved_selections[selection_identity] = sorted(previous)
            answered_groups = _legacy_answer_groups(records)
        if answer is not None and _reuse_saved_answers() and _legacy_answer_groups(records) <= answered_groups:
            chosen = sorted(previous)       # every group already answered and nothing new: no window
            print(f"{mod_dir.name}: saved installer choices applied")
        else:
            chosen = _show_selector(mod_dir.name, records, previous, installed, answered_groups)
        if chosen is None:
            raise OptionsCancelled(f"Option selection cancelled for {mod_dir.name}; this package is not ready.")
        prior_activation = (entry.get("activation", {}) if reopen_existing
                            and entry.get("selection_identity") == selection_identity else {})
        backup_root = _option_backup_root(output_dir, selection_identity)
        held = held_options(records, set(chosen), installed)      # chosen, but what it needs isn't here yet
        applied = _activate(mod_dir, plugin, records, set(chosen) - held, prior_activation, backup_root)
        changed_plugins = _apply_plugin_choices(mod_dir, records, set(chosen) - held)
        targets = sorted({target for item in records for target in item["targets"]})
        reviewed = set(entry.get("reviewed_targets", []))
        # Anything already installed was considered in the selector's defaults.
        reviewed.update(set(targets) & installed)
        mods[key] = {"mod_dir": str(mod_dir.resolve()), "plugin": plugin, "targets": targets,
                     "selection_identity": selection_identity,
                     "selected": applied["selected"], "activation": applied, "held": sorted(held),
                     "reviewed_targets": sorted(reviewed - {m.casefold() for r in records if r["key"] in held
                                                            for m in (r.get("missing_masters") or [])})}
        saved_selections[selection_identity] = applied["selected"]
        _save_answer_file(output_dir, selection_identity, set(applied["selected"]) | held, records, answer)
        record_pending_options(output_dir, mod_dir, records, set(applied["selected"]) | held, installed)
        changed_mods.add(mod_dir.name)
        _save_journal(journal_path, state)
        print(f"  {mod_dir.name}: selected {len(applied['selected'])} option(s); "
              f"{changed_plugins} ESP/ESM choice file(s) moved between active and Off.")
    for mod_dir in eligible:
        installed.update(_available_staged_plugins(mod_dir))
    state["known_installed_plugins"] = sorted(installed)
    state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_journal(journal_path, state)
    return sorted(changed_mods, key=str.casefold)


def undo_staging_options(staging: str | os.PathLike, output_dir: str | os.PathLike, only: str | None = None) -> int:
    """Undo active option payloads, restoring each mod's original staged files from Off."""
    path = Path(output_dir) / "mwmanager_compatibility_journal.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    root = Path(staging).resolve()
    undone = 0
    for entry in state.get("mods", {}).values():
        mod_dir = Path(entry.get("mod_dir", ""))
        try:
            mod_dir.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if only and mod_dir.name.casefold() != only.casefold():
            continue
        activation = entry.get("activation", {})
        _restore_activation(mod_dir, activation)
        # Remove only backups named by this journal after restoring their originals.
        for info in activation.get("files", {}).values():
            backup_name = info.get("backup")
            if not backup_name:
                continue
            backup = Path(backup_name)
            if not backup.is_absolute():
                backup = mod_dir / backup
            try:
                if backup.is_file():
                    backup.unlink()
                    boundary = mod_dir.resolve()
                    parent = backup.parent
                    while parent.resolve() != boundary and boundary in parent.resolve().parents:
                        if any(parent.iterdir()):
                            break
                        parent.rmdir()
                        parent = parent.parent
            except OSError:
                pass
        identity = entry.get("selection_identity")
        if identity:
            _prune_option_backups(_option_backup_root(Path(output_dir), identity), {"files": {}})
        if activation.get("files"):
            undone += len(activation["files"])
        off_root = mod_dir / "Off"
        choice_roots = list(off_root.glob("*/FOMOD option plugin choices"))
        choice_roots += list(off_root.glob("*/fomod-options/plugin-choices"))  # legacy layout
        extras_root = mod_dir / "Extras"
        if extras_root.is_dir():
            choice_roots.extend(extras_root.glob("*/unused-at-install"))
        for choice_root in choice_roots:
            if not choice_root.is_dir():
                continue
            for source in sorted(_safe_rglob(choice_root), key=lambda p: len(p.parts)):
                if not source.is_file() or source.suffix.casefold() not in {".esp", ".esm"}:
                    continue
                destination = mod_dir / source.name if choice_root.name.casefold() == "unused-at-install" \
                    else mod_dir / source.relative_to(choice_root)
                if destination.exists():
                    location = "Extras" if choice_root.name.casefold() == "unused-at-install" else "Off"
                    print(f"Kept {source.name} under {location}; its original path is occupied.")
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                undone += 1
        entry["activation"] = {"files": {}, "selected": []}
        entry["selected"] = []
    if undone:
        _save_journal(path, state)
        print(f"Restored {undone} option-activated file(s) from their parked locations.")
    return undone
