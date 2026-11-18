"""MWManager's Tidy, laid over upstream vortex_tidy.py (run inside its namespace by mwmanager_upstream.compose_tidy).

Upstream vortex_tidy does the plan / --apply work unchanged: plan mode writes the plan file and changes nothing;
--apply works the plan out again and enacts it only when it matches the reviewed one (exit 2 otherwise, nothing
changed), then the Lua check's patches and (with the asset-repair switch on) the asset repairs. MWManager adds:

  * its per-mod staging layout and rules: plan_mod (OpenMW payloads kept under Off\\<mod>\\OpenMW, normal maps
    without a diffuse left in place, FOMOD/BAIN payload handling, official add-on docs), review (OpenMW-only mods
    are kept, never deleted), the journal with empty-folder pruning and option undo;
  * the installer's BAIN/FOMOD option step (mock_fomod) and the re-tidy of the payloads it activates;
  * .omwaddon master renames and master version alignment after --apply;
  * the Data Files document tidy used by --init (--data-files);
  * the Data Files folder upstream reads next to itself comes from --game-data-files instead.
"""
import argparse
import filecmp
import hashlib
import shutil
import stat
import struct
import traceback
import threading
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

import mwdialog_check as _scanner

OUTPUT_DIR = _scanner.OUTPUT_DIR
MANAGER_DIR = _scanner.MANAGER_DIR
globals().update(_scanner.tidy_state_names(MANAGER_DIR))
_TIDY_BACKUP_CACHE_DATA = None
SUPPORT_DIRS = {"mwse", "mge3", "shaders", "grass", "mcpatch", "fomod"}
# A game folder the author misnamed: renamed at the plugin root and reported as a packing error.
MISNAMED_DIRS = {"sounds": "Sound", "splashes": "Splash", "texture": "Textures", "mesh": "Meshes",
                 "icon": "Icons", "videos": "Video", "bookarts": "BookArt", "musics": "Music"}
PACKING_MARK = "packing error: "


def misnamed_reason(folder):
    return f"{PACKING_MARK}the author packed '{folder}', which the game never reads; renamed to " \
           f"'{MISNAMED_DIRS[folder.casefold()]}'"
OUR_DIRS = {"docs", "extras", "off", "bugreports"}
LINK_PREFIX = "link: "              # a planned 00 Core file: hard-linked into the root, the 00 folder kept in Extras
STAGING_DIRS = OUR_DIRS | {"screenshots", "grass"}
OPENMW_DIRS = {"openmw", "scripts", "l10n", "playlists", "animations"}
CORE_OPTION = re.compile(r"^00(?:\s*-\s*|\s+).+$", re.I)
NUMBERED_OPTION = re.compile(r"^(\d{2})(?:\s*-\s*|\s+)(.+)$", re.I)
VORTEX_FILES = {"__folder_managed_by_vortex", ".mwmanager-stage.json"}


def package_name(folder):
    """Use the processing identity, staged primary master, Vortex name, then the download-folder name."""
    fallback = mod_name(os.path.basename(folder.rstrip("\\/")))
    established = os.environ.get("MWMANAGER_PLUGIN_NAME", "").strip()
    if established:
        return BAD_CHARS.sub("_", established).rstrip(" .") or fallback
    try:
        with open(os.path.join(folder, ".mwmanager-stage.json"), encoding="utf-8") as marker_file:
            marker = json.load(marker_file)
        marker_name = str(marker.get("PLUGIN_NAME") or "").strip()
        if marker_name:
            return BAD_CHARS.sub("_", marker_name).rstrip(" .") or fallback
        masters = marker.get("selected_masters", [])
        if masters:
            return BAD_CHARS.sub("_", os.path.splitext(os.path.basename(masters[0]))[0]).rstrip(" .") or fallback
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        import mwdialog_assets
        value = mwdialog_assets.vortex_mod_metadata(folder).get("name", "").strip()
        if value:
            return BAD_CHARS.sub("_", value).rstrip(" .") or fallback
    except Exception:
        pass
    return fallback


def optional_folder_in_docs(mod_dir, folder_name):
    """Best-effort detect an optional payload explicitly named in a local readme/manual."""
    terms = re.escape(folder_name.rstrip("s")) + r"s?"
    pattern = re.compile(r"(?:\b(?:optional|option|select|install)\b.{0,120}\b" + terms +
                         r"\b|\b" + terms + r"\b.{0,120}\b(?:optional|option|select)\b)", re.I | re.S)
    for path in files_under(mod_dir):
        if not is_doc(path) or os.path.basename(path).casefold().endswith(".bugreport.txt"):
            continue
        if re.search(r"change.?log|version.?history", os.path.basename(path), re.I):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                content = f.read(500_000)
        except OSError:
            continue
        if pattern.search(content):
            return True
    return False


def files_under(path):
    for base, dirs, fns in os.walk(path):
        dirs[:] = [d for d in dirs if d.casefold() not in {"off", "bugreports"}]
        for fn in fns:
            yield os.path.join(base, fn)
_MODEL_NAMED_TEXTURES = None


def model_named_textures(mod_dir):
    """Normal-map stems referenced by installed models or NIFs in this package."""
    global _MODEL_NAMED_TEXTURES
    if _MODEL_NAMED_TEXTURES is None:
        _MODEL_NAMED_TEXTURES = set()
        cache_path = os.path.join(OUTPUT_DIR, "mwmanager_asset_cache.json")
        try:
            with open(cache_path, encoding="utf-8") as stream:
                cache = json.load(stream)
            for key, value in cache.items():
                result = value.get("r") if isinstance(value, dict) else None
                if isinstance(result, dict):
                    names = result.get("tex", ())
                elif isinstance(result, list) and str(key).casefold().startswith("bsa:"):
                    names = result
                else:
                    continue
                for name in names:
                    stem = os.path.splitext(os.path.basename(str(name).replace("/", "\\")))[0].casefold()
                    if NORMAL_MAP.search(stem):
                        _MODEL_NAMED_TEXTURES.add(stem)
        except (OSError, ValueError, TypeError):
            pass

    named = set(_MODEL_NAMED_TEXTURES)
    try:
        import mwdialog_assets
        for path in files_under(mod_dir):
            if os.path.splitext(path)[1].casefold() != ".nif":
                continue
            try:
                with open(path, "rb") as stream:
                    textures = mwdialog_assets.names_by_ext(stream.read(), (b"dds", b"tga", b"bmp"))
            except OSError:
                continue
            for name, _extension in textures:
                stem = os.path.splitext(os.path.basename(name.replace("/", "\\")))[0].casefold()
                if NORMAL_MAP.search(stem):
                    named.add(stem)
    except (ImportError, OSError):
        pass
    return named
NORMAL_MAP_SKIPS = {}


def has_diffuse_sibling(path):
    """Whether this normal map has a same-folder diffuse texture with the normal suffix removed."""
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    diffuse = NORMAL_MAP.sub("", stem)
    try:
        names = os.listdir(os.path.dirname(path))
    except OSError:
        return False
    return any(os.path.splitext(name)[0].lower() == diffuse
               and os.path.splitext(name)[1].lower() in {".dds", ".tga", ".bmp"} for name in names)


def note_unmatched_normal_name(mod_dir, path):
    """Record a normal-map-looking filename with no diffuse sibling, without moving or warning on it."""
    key = os.path.normcase(os.path.abspath(mod_dir))
    NORMAL_MAP_SKIPS.setdefault(key, set()).add(os.path.relpath(path, mod_dir))


def fomod_declared_paths(mod_dir):
    """Return FOMOD configs and every declared source/image path to preserve for reinstall."""
    protected_files, protected_dirs, recovered_images = set(), set(), []
    mod_root = Path(mod_dir).resolve()
    screenshots = next((mod_root / name for name in os.listdir(mod_root)
                        if name.casefold() == "screenshots" and (mod_root / name).is_dir()), None)
    screenshot_images = {}
    if screenshots:
        for base, _dirs, files in os.walk(screenshots):
            for filename in files:
                if Path(filename).suffix.casefold() in IMAGE_EXT:
                    screenshot_images.setdefault(filename.casefold(), []).append(Path(base) / filename)
    for base, dirs, files in os.walk(mod_dir):
        dirs[:] = [name for name in dirs if name.casefold() not in {"off", "bugreports", "screenshots"}]
        if Path(base).name.casefold() != "fomod":
            continue
        config = next((Path(base) / name for name in files
                       if name.casefold() == "moduleconfig.xml"), None)
        if config is None:
            continue
        protected_files.add(os.path.normcase(str(config.resolve())))
        protected_dirs.add(os.path.normcase(str(config.parent.resolve())))
        try:
            root = ET.parse(config).getroot()
        except (OSError, ET.ParseError):
            continue
        source_root = config.parent.parent if config.parent.name.casefold() == "fomod" else config.parent
        references = []
        for node in root.iter():
            tag = node.tag.rsplit("}", 1)[-1].casefold()
            if tag in {"file", "folder"} and node.attrib.get("source"):
                references.append((node.attrib["source"], tag == "folder"))
            if tag == "moduleimage":
                image_path = node.attrib.get("path")
                if image_path:
                    references.append((image_path, False))
            elif tag == "image":
                image_path = node.attrib.get("path")
                if image_path:
                    references.append((image_path, False))
        for relative, is_directory in references:
            candidate = (source_root / Path(relative.replace("\\", os.sep))).resolve()
            try:
                candidate.relative_to(mod_root)
            except ValueError:
                continue
            key = os.path.normcase(str(candidate))
            if is_directory:
                protected_dirs.add(key)
            else:
                protected_files.add(key)
                if (not candidate.is_file()
                        and candidate.suffix.casefold() in IMAGE_EXT):
                    matches = screenshot_images.get(candidate.name.casefold(), [])
                    if matches:
                        recovered_images.append((str(matches[0]), str(candidate)))
    return protected_files, protected_dirs, recovered_images


def plan_mod(mod_dir):
    """[(source, destination, reason)] for one mod, or a str saying why it's left alone."""
    mod_dir = os.path.realpath(mod_dir)  # Resolve Windows 8.3 aliases before deriving FOMOD-relative paths.
    entries = os.listdir(mod_dir)
    if any(os.path.splitext(e)[1].lower() in GAME_ROOT_EXT for e in entries
           if os.path.isfile(os.path.join(mod_dir, e))):
        return "installs into the game folder (exe/dll/Data Files at its root)"
    name = package_name(mod_dir)
    fomod_files, fomod_dirs, fomod_recoveries = fomod_declared_paths(mod_dir)
    data_folder = next((os.path.join(mod_dir, e) for e in entries
                        if e.casefold() == "data files" and os.path.isdir(os.path.join(mod_dir, e))), None)
    data_entries = os.listdir(data_folder) if data_folder else []
    plugin_names = [e for e in entries + data_entries
                    if os.path.splitext(e)[1].casefold() in KEEP_FILE_EXT]
    plugins = {os.path.splitext(e)[0].lower() for e in plugin_names}
    nested_core_esms = []
    for base, dirs, files in os.walk(mod_dir):
        dirs[:] = [d for d in dirs if d.casefold() not in {"off", "docs", "bugreports", "screenshots"}]
        relative_parts = os.path.relpath(base, mod_dir).split(os.sep)
        if any(CORE_OPTION.match(part) for part in relative_parts if part not in {".", ""}):
            nested_core_esms.extend(os.path.splitext(filename)[0] for filename in files
                                     if filename.casefold().endswith(".esm"))
    # Off and Docs use the Vortex package identity, never an arbitrary contained ESP/ESM filename.
    off_owner = name
    addon_paths = [(e, os.path.join(mod_dir, e)) for e in entries
                   if e.casefold().endswith(".omwaddon") and os.path.isfile(os.path.join(mod_dir, e))]
    if data_folder:
        addon_paths.extend((e, os.path.join(data_folder, e)) for e in data_entries
                           if e.casefold().endswith(".omwaddon") and os.path.isfile(os.path.join(data_folder, e)))
    addons = [name for name, _path in addon_paths]
    has_mw_plugin = False
    for base, dirs, files in os.walk(mod_dir):
        dirs[:] = [d for d in dirs if d.casefold() not in {"off", "docs", "bugreports", "screenshots"}]
        if any(os.path.splitext(filename)[1].casefold() in (".esp", ".esm") for filename in files):
            has_mw_plugin = True
            break
    scripts = openmw_scripts(mod_dir, entries)
    openmw_only = bool(scripts) and not has_mw_plugin
    if openmw_only:
        REVIEW[mod_dir] = (addons, scripts)
    docs = os.path.join(mod_dir, existing(mod_dir, "Docs"))
    shots = os.path.join(mod_dir, existing(mod_dir, "Screenshots"))
    extras = os.path.join(mod_dir, existing(mod_dir, "Extras"), name)
    sounds_are_optional = optional_folder_in_docs(mod_dir, "sounds")
    moves, taken, planned_sources = [], set(), set()

    def fomod_path_state(path):
        candidate = os.path.normcase(os.path.abspath(path))
        is_directory = os.path.isdir(path)
        for protected in fomod_files:
            try:
                common = os.path.commonpath([candidate, protected])
            except ValueError:
                continue
            if candidate == protected:
                return "file"
            if is_directory and common == candidate:
                return "contains"
        for protected in fomod_dirs:
            try:
                common = os.path.commonpath([candidate, protected])
            except ValueError:
                continue
            if common == protected:
                return "inside_dir"
            if is_directory and common == candidate:
                return "contains"
        return ""

    extras_key = os.path.normcase(os.path.abspath(extras))

    def already_in_fomod_extras(path):
        candidate = os.path.normcase(os.path.abspath(path))
        try:
            return os.path.commonpath([candidate, extras_key]) == extras_key
        except ValueError:
            return False

    def move(src, dst, why, *, force=False):
        fomod_state = fomod_path_state(src)
        if dst and fomod_state == "file" and not force:
            # Leave individual declared files in place until their enclosing option
            # folder is moved, or relocate loose referenced files in the final pass.
            return
        if dst and fomod_state == "contains" and not force:
            # Keep FOMOD paths reusable, but move them out of the game-data root.
            # Relocating an ancestor preserves the complete archive-relative tree.
            if not already_in_fomod_extras(src):
                relative = os.path.relpath(src, mod_dir)
                dst = os.path.join(extras, relative)
                why = "FOMOD support source -> Extras/<plugin> with its original relative path"
        elif dst and fomod_state == "inside_dir" and not force:
            # The source directory itself will be relocated as a unit later in this plan.
            return
        source_key = os.path.normcase(os.path.abspath(src))
        if source_key in planned_sources:
            return
        planned_sources.add(source_key)
        if dst and source_key == os.path.normcase(os.path.abspath(dst)):
            return
        if dst and os.path.isfile(src) and os.path.isfile(dst) \
                and os.path.normcase(os.path.abspath(dst)) not in taken \
                and filecmp.cmp(src, dst, shallow=False):
            # A repeated 00 Core payload can already be active from an earlier
            # install pass. Drop only byte-identical duplicates; the journal
            # records the surviving copy so undo can restore the source.
            moves.append((src, None, "identical duplicate from " + dst))
            return
        dst = free_path(dst, taken)
        moves.append((src, dst, why))

    # Archive tools preserve host-OS metadata that is never Morrowind content.
    junk_dirs = {"__macos", "__macosx", ".appledouble", ".fseventsd", ".spotlight-v100", ".trashes",
                 "$recycle.bin", "system volume information"}
    junk_files = {".directory", ".ds_store", "desktop.ini", "ehthumbs.db", "ehthumbs_vista.db", "icon\r",
                  "thumbs.db"}
    packaging_junk = []
    for base, dirs, files in os.walk(mod_dir):
        dirs[:] = [directory for directory in dirs if directory.casefold() != "off"]
        for directory in list(dirs):
            if directory.casefold() in junk_dirs or directory.casefold().startswith(".trash-"):
                packaging_junk.append(os.path.join(base, directory))
                dirs.remove(directory)
        packaging_junk.extend(os.path.join(base, filename) for filename in files
                              if filename.casefold() in junk_files or filename.startswith("._"))

    # Consolidate legacy Off/<raw folder or plugin filename>/ trees under the
    # package's canonical Vortex display name, preserving their feature paths.
    off_root = os.path.join(mod_dir, existing(mod_dir, "Off"))
    if os.path.isdir(off_root):
        for child in sorted(os.listdir(off_root), key=str.casefold):
            child_dir = os.path.join(off_root, child)
            if not os.path.isdir(child_dir) or child.casefold() == off_owner.casefold():
                continue
            for source in files_under(child_dir):
                relative = os.path.relpath(source, child_dir)
                move(source, os.path.join(off_root, off_owner, relative),
                     "normalize legacy Off owner to the Vortex package name")
            moves.append((child_dir, None, "remove emptied legacy Off owner folder"))

    def named(fn):                                   # already says which mod it's from
        return has_prefix(fn) or fn.lower().startswith(name.lower())

    def doc_dest(src, rel=None):
        # Keep every document under one package-named directory; don't reproduce archive wrappers
        # such as Docs/Docs or Data Files/Docs.
        return os.path.join(docs, name, os.path.basename(src))

    def screenshot_dest(rel):
        return os.path.join(mod_dir, "Screenshots", off_owner, rel)

    def image_only(folder):
        files = list(files_under(folder))
        return bool(files) and all(os.path.splitext(path)[1].casefold() in IMAGE_EXT for path in files)

    def move_image_folder(folder):
        if fomod_path_state(folder):
            return False
        if fomod_files:
            if already_in_fomod_extras(folder):
                return False
            relative = os.path.relpath(folder, mod_dir)
            move(folder, os.path.join(extras, relative),
                 "FOMOD support image folder -> Extras/<plugin> with its relative path")
            return True
        for path in files_under(folder):
            move(path, screenshot_dest(os.path.relpath(path, folder)),
                 "image-only folder -> Screenshots/<plugin name>")
        moves.append((folder, None, "remove the emptied image-only folder"))
        return True

    # Some packages wrap valid game splash screens as Samples/Splash. Keep those
    # image files in the real Data Files/Splash tree instead of classifying them
    # as screenshots merely because their extensions are images.
    for base, dirs, _files in os.walk(mod_dir):
        rel_parts = os.path.relpath(base, mod_dir).replace("/", "\\").split("\\")
        if (len(rel_parts) >= 2 and rel_parts[0].casefold() == "samples"
                and rel_parts[1].casefold() == "splash"):
            for path in files_under(base):
                rel = os.path.relpath(path, base)
                move(path, os.path.join(mod_dir, "Splash", rel),
                     "Samples/Splash contains valid Morrowind loading splash assets")
            dirs[:] = []

    # Recover loading screens that an earlier Tidy pass misfiled as screenshots.
    # The old image-only rule preserved the relative Splash/ subfolder there.
    old_splash_samples = os.path.join(shots, off_owner, "Splash")
    if os.path.isdir(old_splash_samples):
        for path in files_under(old_splash_samples):
            if os.path.basename(path).casefold().startswith("splash_"):
                rel = os.path.relpath(path, old_splash_samples)
                move(path, os.path.join(mod_dir, "Splash", rel),
                     "restore loading splash asset previously classified as a screenshot")

    # Documentation is supporting material rather than an install option. Flatten archive wrappers
    # and keep every document in one consistent, package-scoped Docs location.
    for document in files_under(mod_dir):
        if (not is_doc(document) or os.path.basename(document).casefold() in VORTEX_FILES
                or os.path.basename(document).casefold().endswith(".bugreport.txt")):
            continue
        destination = doc_dest(document)
        if os.path.normcase(os.path.abspath(document)) != os.path.normcase(os.path.abspath(destination)):
            move(document, destination, "organize documentation under Docs/<Vortex mod name>")

    # Remove now-empty Docs/Readme/Manual wrappers after the package-wide document moves.
    for base, dirs, _files in os.walk(mod_dir, topdown=False):
        dirs[:] = [d for d in dirs if d.casefold() not in {"off", "bugreports"}]
        if os.path.normcase(os.path.abspath(base)) == os.path.normcase(os.path.abspath(os.path.join(docs, name))):
            continue
        if os.path.basename(base).casefold() in DOC_DIR_NAMES:
            moves.append((base, None, "remove emptied documentation wrapper"))

    def unpack_core(core_dir):
        """Unpack a required 00 Core/00 - Core folder into the plugin root."""
        # Docs inside required core payloads must be moved out with the core;
        # otherwise they keep a 00 Data Files wrapper behind.
        ignored = (OUR_DIRS - {"docs"}) | {"screenshots"}
        for base, dirs, files in os.walk(core_dir):
            dirs[:] = sorted([d for d in dirs if d.casefold() not in ignored], key=str.casefold)
            for filename in sorted(files, key=str.casefold):
                source = os.path.join(base, filename)
                rel = os.path.relpath(source, core_dir)
                parts = rel.replace("/", "\\").split("\\")
                while parts and parts[0].casefold() in {"data files", "00 data files"}:
                    parts = parts[1:]
                if not parts:
                    continue
                first = parts[0].casefold()
                packing = ""
                if len(parts) > 1 and first in MISNAMED_DIRS:
                    packing = "; " + misnamed_reason(parts[0])
                    parts[0] = MISNAMED_DIRS[first]
                    first = parts[0].casefold()
                ext = os.path.splitext(filename)[1].casefold()
                recognized = GAME_DIRS | SUPPORT_DIRS | OPENMW_DIRS | OUR_DIRS | {
                    "data files", "docs", "screenshots", "grass"
                }
                if first in DOC_DIR_NAMES or (is_doc(filename) and len(parts) == 1):
                    dest = doc_dest(source)
                    reason = "unpack 00 Core, then move its Docs contents into Docs/<Vortex mod name>"
                elif len(parts) == 1 and ext in IMAGE_EXT:
                    dest = screenshot_dest(filename)
                    reason = "unpack 00 Core, then move its root image into Screenshots"
                elif len(parts) == 1 and ext not in KEEP_FILE_EXT and ext != ".toml":
                    dest = os.path.join(extras, filename)
                    reason = "unpack 00 Core, then reclassify its unsupported root file under Extras"
                elif len(parts) > 1 and first in {"images", "image", "screenshots"} \
                        and ext in IMAGE_EXT:
                    dest = screenshot_dest(os.path.join(*parts[1:]))
                    reason = "unpack 00 Core, then classify its image folder as Screenshots"
                elif (ext == ".omwscripts" or first in OPENMW_DIRS) and first not in GAME_DIRS:
                    feature = "OpenMW"
                    dest = os.path.join(mod_dir, "Off", off_owner, feature, *parts)
                    reason = "unpack 00 Core, then retain OpenMW-only content under Off/<plugin name>"
                elif len(parts) > 1 and first not in recognized:
                    dest = os.path.join(extras, *parts)
                    reason = "unpack 00 Core, then reclassify its unsupported folder under Extras"
                else:
                    dest = os.path.join(mod_dir, *parts)
                    reason = "unpack required 00 Core content into the plugin root"
                link(source, dest, reason + packing)
        core_name = os.path.basename(core_dir.rstrip(os.sep))
        core_home = os.path.join(extras, core_name)
        if os.path.normcase(os.path.abspath(core_dir)) != os.path.normcase(os.path.abspath(core_home)):
            moves.append((core_dir, free_path(core_home, taken),
                          "keep the 00 folder under Extras/<plugin> so the install can be repeated"))

    def link(src, dst, why):
        """A 00 Core file goes into the root as a hard link; its 00 folder stays whole under Extras. A file an option
        already put at that path wins: the core copy is still in Extras, so nothing is renamed or lost."""
        source_key = os.path.normcase(os.path.abspath(src))
        if source_key in planned_sources:
            return
        planned_sources.add(source_key)
        key = os.path.normcase(os.path.abspath(dst))
        if os.path.exists(dst) or key in taken:
            return
        taken.add(key)
        moves.append((src, dst, LINK_PREFIX + why))

    # Migrate the earlier LuaRepair layout and remove the source-tree wrappers
    # from backup paths. Preserve each original under the owning ESM's folder.
    legacy_lua_backup = os.path.join(mod_dir, existing(os.path.join(mod_dir, "Off"), "LuaRepair")) \
        if os.path.isdir(os.path.join(mod_dir, "Off")) else ""
    if legacy_lua_backup and os.path.isdir(legacy_lua_backup):
        for base, dirs, files in os.walk(legacy_lua_backup):
            dirs[:] = sorted(dirs, key=str.casefold)
            for filename in sorted(files, key=str.casefold):
                source = os.path.join(base, filename)
                relative = os.path.relpath(source, legacy_lua_backup).split(os.sep)
                mwse_index = next((i for i, part in enumerate(relative)
                                   if part.casefold() == "mwse"), None)
                if mwse_index is not None:
                    relative = relative[mwse_index:]
                move(source, os.path.join(mod_dir, "Off", off_owner, "LuaRepairs", *relative),
                     "migrate Lua repair original under Off/<plugin name>/LuaRepairs")
        moves.append((legacy_lua_backup, None, "remove the emptied legacy LuaRepair folder"))

    # Search nested BAIN/FOMOD and Data Files layouts too. Groundcover ESPs belong
    # in this mod's Grass folder so MGE XE can find them; they are not game plugins.
    # Normal maps can sit inside numbered option folders or an existing Extras tree,
    # so quarantine them recursively before those parent folders are moved.
    for base, dirs, files in os.walk(mod_dir):
        dirs[:] = [directory for directory in dirs if directory.casefold() not in
                   {"off", "docs", "bugreports", "screenshots", "grass"}]
        for filename in sorted(files, key=str.casefold):
            source = os.path.join(base, filename)
            relative = os.path.relpath(source, mod_dir)
            relative_parts = relative.replace("/", "\\").split("\\")
            ext = os.path.splitext(filename)[1].casefold()
            stem = os.path.splitext(filename)[0].casefold()
            in_splash = any(part.casefold() == "splash" for part in relative_parts[:-1])
            if in_splash and ext == ".tga":
                sibling_names = {name.casefold() for name in files}
                if f"{stem}.jpg" in sibling_names or f"{stem}.jpeg" in sibling_names:
                    splash_index = next(i for i, part in enumerate(relative_parts[:-1])
                                        if part.casefold() == "splash")
                    splash_relative = os.path.join(*relative_parts[splash_index + 1:])
                    move(source, os.path.join(mod_dir, "Off", off_owner, "Splash", splash_relative),
                         "redundant TGA splash source retained under Off; matching JPEG is installed")
                    continue
            openmw_path = ext == ".omwscripts" or any(part.casefold() in OPENMW_DIRS for part in relative_parts)
            openmw_option_index = next((i for i, part in enumerate(relative_parts[:-1])
                                        if (option_match := NUMBERED_OPTION.match(part))
                                        and "openmw" in option_match.group(2).casefold()), None)
            normal_map_folder = any(re.search(r"normal[\s_-]*maps?|\bnormals\b", part, re.I)
                                    for part in relative_parts[:-1])
            normal_map_file = ext in {".dds", ".tga", ".bmp"} and NORMAL_MAP.search(stem)
            normal_map_named_by_path = normal_map_folder
            normal_map_has_diffuse = normal_map_file and has_diffuse_sibling(source)
            if normal_map_file and not normal_map_named_by_path and not normal_map_has_diffuse:
                note_unmatched_normal_name(mod_dir, source)
            if openmw_option_index is not None:
                payload_parts = relative_parts[openmw_option_index + 1:]
                while payload_parts and payload_parts[0].casefold() in {"data files", "00 data files"}:
                    payload_parts = payload_parts[1:]
                destination = os.path.join(mod_dir, "Off", off_owner, "OpenMW", *payload_parts) \
                    if payload_parts else os.path.join(mod_dir, "Off", off_owner, "OpenMW", filename)
                move(source, destination, "numbered OpenMW option retained under Off/<Vortex mod name>/OpenMW")
                continue
            if openmw_path or normal_map_folder or normal_map_has_diffuse:
                reason = "OpenMW-only file or confirmed normal map retained under Off/<Vortex mod name>/OpenMW"
                move(source, os.path.join(mod_dir, "Off", off_owner, "OpenMW", relative),
                     reason, force=openmw_path)
            optional_payload = any(
                (match := re.match(r"^(\d{1,2})(?:\s+|[_-])", part))
                and int(match.group(1)) > 0
                for part in relative_parts[:-1])
            if (os.path.splitext(filename)[1].casefold() == ".esp" and is_grass_plugin(source)
                    and not optional_payload):
                move(source, os.path.join(mod_dir, existing(mod_dir, "Grass"), filename),
                     "MGE XE grass plugin -> Grass\\ (read by MGE XE's distant land generator, not loaded by the game)")

    # Archives often wrap their actual BAIN package one or two levels below
    # Extras/<package name>. Unpack only required 00 folders there and retain the
    # FOMOD directory/config beside its optional payload for install-time use.
    extras_container = os.path.join(mod_dir, existing(mod_dir, "Extras"))
    if os.path.isdir(extras_container):
        nested_core = []
        for base, dirs, _files in os.walk(extras_container):
            dirs[:] = [d for d in dirs if d.casefold() not in {"off", "bugreports", "screenshots"}]
            nested_core.extend(os.path.join(base, d) for d in dirs if CORE_OPTION.match(d))
        for core_dir in sorted(nested_core, key=lambda p: p.count(os.sep), reverse=True):
            if os.path.isdir(core_dir):
                unpack_core(core_dir)
        # Without a FOMOD, image-only folders inside a wrapped BAIN package are
        # likely screenshots. A FOMOD package may use those same images in its
        # option descriptions, so leave its Extras tree intact for the selector.
        image_folders = []
        if not fomod_files:
            for base, dirs, _files in os.walk(extras_container):
                dirs[:] = [d for d in dirs if d.casefold() not in {"off", "bugreports", "screenshots", "fomod"}
                           and not CORE_OPTION.match(d)]
                image_folders.extend(os.path.join(base, d) for d in dirs)
        selected_image_folders = []
        for folder in sorted(image_folders, key=lambda p: p.count(os.sep)):
            if any(os.path.commonpath([folder, parent]) == parent for parent in selected_image_folders):
                continue
            if fomod_path_state(folder):
                # FOMOD images and option data remain source assets even when an
                # option folder happens to contain only image files.
                continue
            if os.path.isdir(folder) and image_only(folder):
                move_image_folder(folder)
                selected_image_folders.append(folder)
    for e, src in addon_paths:
        dest_name = os.path.splitext(e)[0] + ".esp"
        if openmw_only:
            move(src, os.path.join(mod_dir, "Off", off_owner, "OpenMW", e),
                 "OpenMW-only plugin retained under Off for possible future conversion")
        elif os.path.exists(os.path.join(mod_dir, dest_name)):
            move(src, os.path.join(mod_dir, "Off", off_owner, "OpenMW-addon-duplicate", e),
                 "duplicate .omwaddon kept Off because its .esp destination already exists")
        else:
            move(src, os.path.join(mod_dir, dest_name),
                 "OpenMW-compatible plugin renamed .esp for vanilla Morrowind")
            CONVERTED_ADDONS[e.casefold()] = dest_name
    for e in sorted(entries):
        src = os.path.join(mod_dir, e)
        el, ext = e.lower(), os.path.splitext(e)[1].lower()
        if os.path.isfile(src):
            if el in VORTEX_FILES or ext in KEEP_FILE_EXT or ext == ".toml" \
                    or el.endswith(".bugreport.txt"):          # our own bug reports stay at the root
                continue
            if ext == ".omwscripts":
                feature = "OpenMW"
                move(src, os.path.join(mod_dir, "Off", off_owner, feature, e), "OpenMW script retained outside vanilla Data Files")
            elif ext in IMAGE_EXT and fomod_files:
                move(src, os.path.join(extras, e),
                     "FOMOD support image -> Extras/<plugin> for installer preview")
            elif ext in IMAGE_EXT:
                move(src, os.path.join(shots, e if named(e) else f"{name} - {e}"), "image at the root")
            elif is_doc(e):
                move(src, doc_dest(src), "document at the root")
            else:
                move(src, os.path.join(extras, e), "not game data")
            continue
        if el == "data files":
            for child in sorted(os.listdir(src), key=str.casefold):
                child_src = os.path.join(src, child)
                if child.casefold().endswith(".omwaddon"):
                    continue  # already planned for conversion or Off retention above
                if os.path.isdir(child_src) and CORE_OPTION.match(child):
                    unpack_core(child_src)
                    continue
                if os.path.isdir(child_src) and NUMBERED_OPTION.match(child) \
                        and int(NUMBERED_OPTION.match(child).group(1)) > 0:
                    move(child_src, os.path.join(extras, child),
                         "optional numbered payload inside Data Files -> expose to mock BAIN/FOMOD")
                    continue
                if os.path.isdir(child_src) and image_only(child_src):
                    move_image_folder(child_src)
                    continue
                if child.casefold().endswith(".omwscripts") or (os.path.isdir(child_src) and child.casefold() in OPENMW_DIRS):
                    feature = "OpenMW"
                    move(child_src, os.path.join(mod_dir, "Off", off_owner, feature, "Data Files", child),
                         "OpenMW script/resource retained outside vanilla Data Files")
                elif child.casefold() == "sounds" and os.path.isdir(child_src) and sounds_are_optional:
                    move(child_src, os.path.join(extras, "01 - Optional Sounds"),
                         "readme identifies Sounds as optional; expose it in the mock BAIN/FOMOD")
                elif child.casefold() in MISNAMED_DIRS and os.path.isdir(child_src):
                    for misnamed_file in files_under(child_src):
                        move(misnamed_file, os.path.join(mod_dir, MISNAMED_DIRS[child.casefold()],
                                                         os.path.relpath(misnamed_file, child_src)),
                             misnamed_reason(child))
                    moves.append((child_src, None, "remove the emptied misnamed folder"))
                else:
                    move(child_src, os.path.join(mod_dir, child),
                         "flatten the mod's Data Files folder into its staging root")
            moves.append((src, None, "remove the emptied Data Files folder"))
            continue
        if os.path.isdir(src) and CORE_OPTION.match(e):
            unpack_core(src)
            continue
        if os.path.isdir(src) and el in MISNAMED_DIRS and not (el == "sounds" and sounds_are_optional):
            for misnamed_file in files_under(src):
                move(misnamed_file, os.path.join(mod_dir, MISNAMED_DIRS[el], os.path.relpath(misnamed_file, src)),
                     misnamed_reason(e))
            moves.append((src, None, "remove the emptied misnamed folder"))
            continue
        if el == "textures":
            # Normal maps are OpenMW-only; keep them under the package's Off/OpenMW tree.
            for p in files_under(src):
                stem = os.path.splitext(os.path.basename(p))[0].lower()
                if os.path.splitext(p)[1].lower() in (".dds", ".tga", ".bmp") and NORMAL_MAP.search(stem):
                    relative_parts = os.path.relpath(p, mod_dir).replace("/", "\\").split("\\")
                    path_names_normal_maps = any(
                        re.search(r"normal[\s_-]*maps?|\bnormals\b", part, re.I)
                        for part in relative_parts[:-1]
                    )
                    if not path_names_normal_maps and not has_diffuse_sibling(p):
                        note_unmatched_normal_name(mod_dir, p)
                        continue
                    rel = os.path.relpath(p, mod_dir)
                    reason = "normal map retained under Off/<Vortex mod name>/OpenMW"
                    move(p, os.path.join(mod_dir, "Off", off_owner, "OpenMW", rel),
                         reason)
            continue
        # Vortex packages commonly keep installable payloads under Extras/.
        # Treat it as a container so tidy never plans to move it into its own
        # descendant (Extras/<mod>/...), which fails on Windows and can recurse.
        if el == "extras" or el in GAME_DIRS or el in SUPPORT_DIRS:
            continue
        if el == "screenshots":
            for path in files_under(src):
                if os.path.splitext(path)[1].casefold() not in IMAGE_EXT:
                    continue
                rel = os.path.relpath(path, src)
                first = rel.replace("/", "\\").split("\\", 1)[0]
                if first.casefold() == off_owner.casefold():
                    continue
                move(path, screenshot_dest(rel), "organize screenshots under Screenshots/<plugin name>")
            continue
        if os.path.isdir(src) and image_only(src):
            move_image_folder(src)
            continue
        if el == "docs":
            continue  # the package-wide pass above already grouped these under Docs/<Vortex mod name>
        if el == "sounds":
            if sounds_are_optional:
                move(src, os.path.join(extras, "01 - Optional Sounds"),
                     "readme identifies Sounds as optional; expose it in the mock BAIN/FOMOD")
            else:
                for p in files_under(src):
                    rel = os.path.relpath(p, src)
                    move(p, os.path.join(mod_dir, "Sound", rel),
                         "normalize pluralized Sounds folder to the game's Sound data folder")
                moves.append((src, None, "remove emptied pluralized Sounds folder"))
            continue
        if el in OUR_DIRS:
            continue
        if el in OPENMW_DIRS:
            feature = "OpenMW"
            if el == "openmw":
                # Migrate the folder created by earlier tidy versions into this plugin's Off tree.
                for child in sorted(os.listdir(src), key=str.casefold):
                    move(os.path.join(src, child), os.path.join(mod_dir, "Off", off_owner, feature, child),
                         "legacy root OpenMW content -> plugin Off tree")
                moves.append((src, None, "remove the emptied legacy OpenMW folder"))
            else:
                move(src, os.path.join(mod_dir, "Off", off_owner, feature, e),
                     "OpenMW resource retained outside vanilla Data Files")
            continue
        inside = [p for p in files_under(src) if os.path.basename(p).lower() not in VORTEX_FILES]
        named_for = el in plugins or el == name.lower() or el in DOC_DIR_NAMES
        all_docs = bool(inside) and all(is_doc(p) for p in inside)
        if named_for and any(is_doc(p) for p in inside) or all_docs:
            for p in inside:
                rel = os.path.relpath(p, src)
                if is_doc(p):
                    move(p, doc_dest(p, rel), f"readme folder '{e}' -> Docs")
                else:
                    move(p, os.path.join(extras, e, rel), f"non-document in readme folder '{e}'")
            moves.append((src, None, "remove the emptied folder"))
            continue
        move(src, os.path.join(extras, e), "not a game folder")

    # FOMOD and every source/image they declare belong under Extras/<plugin>,
    # with their archive-relative layout intact so later option changes still work.
    # Root support folders are intentionally skipped by the ordinary game-data pass,
    # so schedule them here and avoid duplicate moves for paths covered by a parent.
    def covered_by_fomod_folder(path):
        candidate = os.path.normcase(os.path.abspath(path))
        for source, destination, _reason in moves:
            if destination is None or not os.path.isdir(source):
                continue
            source_key = os.path.normcase(os.path.abspath(source))
            try:
                if os.path.commonpath([candidate, source_key]) == source_key:
                    return True
            except ValueError:
                continue
        return False

    def fomod_extras_relative(path):
        parts = Path(os.path.relpath(path, mod_dir)).parts
        # Existing Extras containers and package-name wrappers are staging
        # envelopes, not part of the installer payload's meaningful layout.
        while parts and parts[0].casefold() == "extras":
            parts = parts[1:]
        if parts and parts[0].casefold() == name.casefold():
            parts = parts[1:]
        elif parts:
            envelope = Path(mod_dir) / existing(mod_dir, "Extras") / parts[0]
            if (envelope / "fomod" / "moduleconfig.xml").is_file():
                parts = parts[1:]
        return os.path.join(*parts) if parts else os.path.basename(path)

    # Reserve misplaced FOMOD images before the ordinary Screenshots pass sees
    # them. Restore each one beside its config's declared source path below.
    recovery_sources = {os.path.normcase(os.path.abspath(source)) for source, _target in fomod_recoveries}
    planned_sources.update(recovery_sources)

    for path_key in sorted(fomod_dirs, key=lambda value: (value.count(os.sep), value.casefold())):
        path = os.path.abspath(path_key)
        if os.path.isdir(path) and not already_in_fomod_extras(path) and not covered_by_fomod_folder(path):
            relative = fomod_extras_relative(path)
            move(path, os.path.join(extras, relative),
                 "FOMOD support folder -> Extras/<plugin> with its original relative path")
    for path_key in sorted(fomod_files, key=str.casefold):
        path = os.path.abspath(path_key)
        if os.path.isfile(path) and not already_in_fomod_extras(path) and not covered_by_fomod_folder(path):
            relative = fomod_extras_relative(path)
            move(path, os.path.join(extras, relative),
                 "FOMOD-declared file -> Extras/<plugin> with its original relative path")

    fomod_config_in_extras = any(already_in_fomod_extras(path) for path in fomod_dirs)
    restored_sources = set()
    for source, target in fomod_recoveries:
        source, target = os.path.abspath(source), os.path.abspath(target)
        source_key = os.path.normcase(source)
        if source_key in restored_sources or not os.path.isfile(source):
            continue
        restored_sources.add(source_key)
        destination = target if fomod_config_in_extras else os.path.join(extras, fomod_extras_relative(target))
        if os.path.normcase(source) != os.path.normcase(os.path.abspath(destination)):
            moves.append((source, free_path(destination, taken),
                          "restore FOMOD support image from Screenshots to its declared source path"))

    # Empty extraction directories are not selectable payloads. Schedule their removal,
    # including already-empty normal-map options and wrappers moved wholly to Off/OpenMW.
    extras_root = os.path.join(mod_dir, existing(mod_dir, "Extras"))
    if os.path.isdir(extras_root):
        for base, dirs, files in os.walk(extras_root, topdown=False):
            if not dirs and not files:
                moves.append((base, None, "remove empty Extras folder"))

    # Remove earlier classifications of debris files inside a debris folder, then quarantine the parent once.
    if packaging_junk:
        junk_keys = [(os.path.normcase(os.path.abspath(path)), os.path.isdir(path)) for path in packaging_junk]
        moves = [item for item in moves if not any(
            os.path.normcase(os.path.abspath(item[0])) == key
            or (is_dir and os.path.normcase(os.path.abspath(item[0])).startswith(key + os.sep))
            for key, is_dir in junk_keys)]
        for source in packaging_junk:
            relative = os.path.relpath(source, mod_dir)
            destination = free_path(os.path.join(mod_dir, "Off", off_owner, "Packaging Metadata", relative), taken)
            moves.append((source, destination, "remove platform packaging metadata from installable content"))
    return moves
CONVERTED_ADDONS = {}  # source .omwaddon filename -> destination .esp filename


def openmw_scripts(mod_dir, entries):
    """Find OpenMW Lua scripts in the package, including a nested Data Files folder."""
    found = []
    for base, dirs, files in os.walk(mod_dir):
        dirs[:] = [d for d in dirs if d.casefold() != "off"]
        relbase = os.path.relpath(base, mod_dir)
        for directory in dirs:
            if directory.casefold() == "scripts":
                found.append(os.path.join(relbase, directory).lstrip(".\\") + "\\")
        for filename in files:
            if filename.casefold().endswith(".omwscripts"):
                found.append(os.path.join(relbase, filename).lstrip(".\\"))
    return sorted(set(found), key=str.casefold)


def review(staging, moves, assume_keep=False):
    """Alert about OpenMW-only packages, which the plan retains under their package Off tree."""
    for mod_dir, (addons, scripts) in sorted(REVIEW.items()):
        name = os.path.basename(mod_dir)
        print("\n" + "!" * 70)
        print(f"OpenMW-only mod: {name}")
        print(f"  OpenMW plugin(s): {', '.join(addons) if addons else '(none)'}; no .esp or .esm")
        print(f"  OpenMW scripts: {', '.join(scripts)}")
        print("  OpenMW's Lua scripting is required; these files will be retained under Off\\<Vortex mod name>\\OpenMW.")
        print("  A future MWSE conversion pass may be able to replace them. To uninstall, remove the mod in Vortex.")
    return moves, []


def _rewrite_masters(data, mapping):
    if len(data) < 16 or data[:4] != b"TES3":
        return data, []
    end = min(len(data), 16 + struct.unpack_from("<I", data, 4)[0])
    pos, body, changes = 16, bytearray(), []
    while pos + 8 <= end:
        tag = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        stop = pos + 8 + size
        if stop > end:
            return data, []
        payload = data[pos + 8:stop]
        if tag == b"MAST":
            master = payload.split(b"\0", 1)[0].decode("cp1252", "replace")
            replacement = mapping.get(master.casefold())
            if replacement:
                tail = b"\0" if payload.endswith(b"\0") else b""
                payload = replacement.encode("cp1252", "replace") + tail
                changes.append((master, replacement))
        body.extend(tag + struct.pack("<I", len(payload)) + payload)
        pos = stop
    body.extend(data[end:])
    header = b"TES3" + struct.pack("<I", len(body) - (len(data) - end)) + data[8:16]
    return header + bytes(body), changes


def _plugin_header(data):
    """Read a TES3 plugin's HEDR version and MAST names."""
    if len(data) < 16 or data[:4] != b"TES3":
        return None, []
    end = min(len(data), 16 + struct.unpack_from("<I", data, 4)[0])
    pos, version, masters = 16, None, []
    while pos + 8 <= end:
        tag = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        stop = pos + 8 + size
        if stop > end:
            return None, []
        payload = data[pos + 8:stop]
        if tag == b"HEDR" and len(payload) >= 4:
            version = struct.unpack_from("<f", payload)[0]
        elif tag == b"MAST":
            masters.append(payload.split(b"\0", 1)[0].decode("cp1252", "replace"))
        pos = stop
    return version, masters


def _rewrite_header_version(data, new_version):
    if len(data) < 16 or data[:4] != b"TES3":
        return data
    end = min(len(data), 16 + struct.unpack_from("<I", data, 4)[0])
    pos, body = 16, bytearray()
    while pos + 8 <= end:
        tag = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        stop = pos + 8 + size
        if stop > end:
            return data
        payload = bytearray(data[pos + 8:stop])
        if tag == b"HEDR" and len(payload) >= 4:
            struct.pack_into("<f", payload, 0, new_version)
        body.extend(tag + struct.pack("<I", len(payload)) + payload)
        pos = stop
    body.extend(data[end:])
    new_size = len(body) - (len(data) - end)
    return b"TES3" + struct.pack("<I", new_size) + data[8:16] + bytes(body)


def _tidy_backup_original(path, staging, original_bytes):
    """Create one shared pristine backup per staged plugin path and reuse it on later tidy passes."""
    global _TIDY_BACKUP_CACHE_DATA
    if _TIDY_BACKUP_CACHE_DATA is None:
        try:
            with open(TIDY_BACKUP_CACHE, encoding="utf-8") as cache_file:
                _TIDY_BACKUP_CACHE_DATA = json.load(cache_file)
        except (OSError, ValueError):
            _TIDY_BACKUP_CACHE_DATA = {}
    key = os.path.normcase(os.path.abspath(path))
    cached = _TIDY_BACKUP_CACHE_DATA.get(key)
    if cached and os.path.isfile(cached.get("backup", "")):
        return cached["backup"]

    relative = os.path.relpath(path, staging)
    parts = Path(relative).parts
    package = os.path.join(staging, parts[0]) if len(parts) > 1 else staging
    plugin_name = package_name(package)
    backup = os.path.join(package, "Off", plugin_name, "Reprocessed plugin headers", os.path.relpath(path, package))
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    if not os.path.exists(backup):
        with open(backup, "wb") as backup_file:
            backup_file.write(original_bytes)
    _TIDY_BACKUP_CACHE_DATA[key] = {
        "backup": backup,
        "package": package,
        "original_sha256": hashlib.sha256(original_bytes).hexdigest(),
    }
    os.makedirs(os.path.dirname(TIDY_BACKUP_CACHE), exist_ok=True)
    temporary = TIDY_BACKUP_CACHE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as cache_file:
        json.dump(_TIDY_BACKUP_CACHE_DATA, cache_file, indent=2)
        cache_file.write("\n")
    os.replace(temporary, TIDY_BACKUP_CACHE)
    return backup


def align_master_versions(staging, data_files, *, apply_changes):
    """Raise staged ESP headers to the newest HEDR version among their available masters."""
    lookup = {}
    roots = [data_files, staging]
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.casefold() != "off"]
            for filename in files:
                if os.path.splitext(filename)[1].casefold() in (".esp", ".esm", ".omwaddon"):
                    path = os.path.join(base, filename)
                    key = os.path.normcase(os.path.abspath(path))
                    if key not in seen:
                        seen.add(key)
                        lookup[filename.casefold()] = path
    for old, new in CONVERTED_ADDONS.items():
        if old in lookup:
            lookup[new.casefold()] = lookup[old]
        elif new.casefold() in lookup:
            lookup[old] = lookup[new.casefold()]

    changed = 0
    for base, dirs, files in os.walk(staging):
        dirs[:] = [d for d in dirs if d.casefold() != "off"]
        for filename in files:
            ext = os.path.splitext(filename)[1].casefold()
            if ext != ".esp" and not (ext == ".omwaddon" and filename.casefold() in CONVERTED_ADDONS):
                continue
            path = os.path.join(base, filename)
            try:
                original = Path(path).read_bytes()
            except OSError:
                continue
            current, masters = _plugin_header(original)
            if current is None:
                continue
            versions = []
            for master in masters:
                master_path = lookup.get(master.casefold())
                if not master_path:
                    continue
                try:
                    master_version, _ = _plugin_header(Path(master_path).read_bytes())
                except OSError:
                    continue
                if master_version is not None:
                    versions.append(master_version)
            target = max(versions, default=current)
            if target <= current:
                continue
            changed += 1
            print(f"  Update {os.path.relpath(path, staging)} HEDR version: {current:g} -> {target:g} "
                  f"(master version)")
            if apply_changes:
                backup = _tidy_backup_original(path, staging, original)
                Path(path).write_bytes(_rewrite_header_version(original, target))
                with open(JOURNAL, "a", encoding="utf-8") as journal:
                    journal.write(json.dumps({"restore": path, "backup": backup,
                                              "plugin": _path_plugin(path, staging),
                                              "reason": "restore original HEDR version during tidy undo"}) + "\n")
    if not apply_changes and changed:
        print(f"  Plan: {changed} staged plugin header(s) need a newer master version.")
    return changed


def update_omwaddon_masters(staging, *, apply_changes):
    """Update MAST references to renamed .omwaddon files, preserving a plugin backup first."""
    mapping = {old: new for old, new in CONVERTED_ADDONS.items()}
    if not mapping:
        return 0
    changed_count = 0
    for base, dirs, files in os.walk(staging):
        dirs[:] = [d for d in dirs if d.casefold() != "off"]
        for filename in files:
            if os.path.splitext(filename)[1].casefold() not in (".esp", ".esm", ".omwaddon"):
                continue
            path = os.path.join(base, filename)
            try:
                original = Path(path).read_bytes()
            except OSError:
                continue
            rewritten, changes = _rewrite_masters(original, mapping)
            if not changes:
                continue
            changed_count += 1
            print(f"  Update masters in {os.path.relpath(path, staging)}: " +
                  ", ".join(f"{old} -> {new}" for old, new in changes))
            if apply_changes:
                backup = _tidy_backup_original(path, staging, original)
                Path(path).write_bytes(rewritten)
                with open(JOURNAL, "a", encoding="utf-8") as journal:
                    journal.write(json.dumps({"restore": path, "backup": backup,
                                              "plugin": _path_plugin(path, staging),
                                              "reason": "restore original plugin header during tidy undo"}) + "\n")
    if not apply_changes and changed_count:
        print(f"  Plan: {changed_count} plugin header(s) need updated MAST names.")
    return changed_count


def drop_covered_moves(res):
    """Leave out a move a folder move of the same plan already makes (the file would land in the same place
    with its folder; moved first, its folder could no longer be moved onto the destination)."""
    if isinstance(res, str) or not res:
        return res
    dirs = [(os.path.normcase(src) + os.sep, dst) for src, dst, _why in res if dst is not None and os.path.isdir(src)]
    keep = []
    for src, dst, why in res:
        key = os.path.normcase(src)
        covered = dst is not None and any(
            key.startswith(top) and os.path.normcase(dst) == os.path.normcase(os.path.join(top_dst, src[len(top):]))
            for top, top_dst in dirs)
        if not covered:
            keep.append((src, dst, why))
    return keep


@contextmanager
def _long_phase_heartbeat(message, interval=30):
    """Keep long silent scans visibly active; the caller writes exact progress when available."""
    stop = threading.Event()

    def pulse():
        while not stop.wait(interval):
            print(f"MWMANAGER-PROGRESS: {message} — still working; this tool is active, not hung.",
                  flush=True)

    thread = threading.Thread(target=pulse, name="MWManager Tidy heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


def run_plan(args):
    REVIEW.clear()
    CONVERTED_ADDONS.clear()
    NOTES.clear()
    NORMAL_MAP_SKIPS.clear()
    mods = sorted(d for d in os.listdir(args.staging) if os.path.isdir(os.path.join(args.staging, d))
                  and d.lower() not in STAGING_DIRS)              # manager folders are never treated as mods
    if args.only:
        mods = [d for d in mods if any(d.casefold() == pat.casefold()
                                       or fnmatch.fnmatch(d.casefold(), pat.casefold()) for pat in args.only)]
    lines, all_moves, skipped, touched = [], [], [], 0
    ORPHAN_NORMALS.clear()
    NORMAL_PACKS.clear()
    _TEX_INDEX.clear()
    pats = blacklist(create=bool(getattr(args, "apply", False))) + (args.skip or [])
    for d in mods:
        hit = next((pt for pt in pats if fnmatch.fnmatch(d.lower(), pt.lower())), None)
        if hit:
            skipped.append((d, f"blacklisted ({hit})"))
            continue
        if os.environ.get("MWMANAGER_GUI") == "1":
            print(f"MWMANAGER-PROGRESS: Planning Tidy rules for {d}", flush=True)
        with _long_phase_heartbeat(f"Planning Tidy rules for {d}"):
            res = drop_covered_moves(plan_mod(os.path.join(args.staging, d)))
        if isinstance(res, str):
            skipped.append((d, res))
            continue
        if not res:
            continue
        touched += 1
        lines.append(f"\n{d}   [{package_name(os.path.join(args.staging, d))}]")
        for src, dst, why in res:
            rs = os.path.relpath(src, os.path.join(args.staging, d))
            if dst is None:
                lines.append(f"    remove empty  {rs}")
            else:
                lines.append(f"    {rs}  ->  {os.path.relpath(dst, os.path.join(args.staging, d))}   ({why})")
        all_moves += res
    head = [f"vortex_tidy plan, {time.strftime('%Y-%m-%d %H:%M')}: {len(mods)} mod(s), {touched} to change, "
            f"{sum(1 for m in all_moves if m[1])} move(s); {len(skipped)} left alone"]
    for d, why in skipped:
        head.append(f"  - left alone: {d} ({why})")
    for d, n in sorted(NORMAL_PACKS.items()):
        NOTES.append(f"{d}: a normal-map add-on pack ({n} normal map(s) under a 'normal maps' path) - not "
                     f"applicable to classic Morrowind (only OpenMW uses normal maps); consider removing it in Vortex")
    if ORPHAN_NORMALS:
        NOTES.append(f"{len(ORPHAN_NORMALS)} normal map(s) with no diffuse texture anywhere left in place (no "
                     f"stand-in); mwdialog_check --full's unused-asset purge removes the unused ones")
    for note in NOTES:
        head.append(f"  NOTE: {note}")
    for key, paths in sorted(NORMAL_MAP_SKIPS.items()):
        examples = sorted(paths, key=str.casefold)[:3]
        more = f"; and {len(paths) - len(examples)} more" if len(paths) > len(examples) else ""
        head.append(f"  INFO: left {len(paths):,} normal-map-like texture(s) in place without a diffuse sibling "
                    f"for {os.path.basename(key)} (possible diffuse textures): "
                    + ", ".join(examples) + more)
    for mod_dir, (addons, scripts) in sorted(REVIEW.items()):
        head.append(f"  OPENMW-ONLY (will be retained under Off/<Vortex mod name>/OpenMW): {os.path.basename(mod_dir)} "
                    f"(plugins {', '.join(addons) or 'none'}; scripts {', '.join(scripts)})")
    args._moves_text = "\n".join(head + lines) + "\n"
    if getattr(args, "_write_plan", True):
        PF.write_atomic(PLAN, args._moves_text)
    print("\n".join(head))
    print("\n".join(lines[:200]))
    if len(lines) > 200:
        print(f"... ({len(lines) - 200} more lines in {PLAN})")
    print(f"\nFull plan: {PLAN}")
    update_omwaddon_masters(args.staging, apply_changes=False)
    game_data_files = args.game_data_files or os.environ.get("MWMANAGER_DATA_FILES")
    align_master_versions(args.staging, game_data_files, apply_changes=False)
    return all_moves


def run_mock_options(args):
    """Run the numeric BAIN/FOMOD selector for the requested staged mod folders."""
    print("MWMANAGER-PROGRESS: Reviewing required and optional BAIN/FOMOD components", flush=True)
    from mock_fomod import OptionsCancelled
    try:
        from mock_fomod import process_staging
        data_files = args.game_data_files or os.environ.get("MWMANAGER_DATA_FILES")
        args.post_option_mods = process_staging(
            args.staging, data_files, OUTPUT_DIR, only=args.only,
            reopen_existing=bool(getattr(args, "options_only", False))) or []
    except OptionsCancelled as exc:
        print(f"MWMANAGER-CANCELLED: {exc}", flush=True)
        return 3
    except Exception as exc:
        print(f"  Mock BAIN/FOMOD option step failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


def clear_readonly_flags(staging, only=None):
    """Archives can mark their files and folders read-only, which blocks moving and removing them on Windows.
    Returns how many flags were cleared."""
    import fnmatch
    cleared = 0
    for name in os.listdir(staging):
        folder = os.path.join(staging, name)
        if not os.path.isdir(folder) or (only and not any(
                name.casefold() == pat.casefold() or fnmatch.fnmatch(name.casefold(), pat.casefold())
                for pat in only)):
            continue
        for base, dirs, files in os.walk(folder):
            for path in [base] + [os.path.join(base, f) for f in files]:
                try:
                    mode = os.stat(path).st_mode
                    desired = mode | stat.S_IWRITE | stat.S_IREAD
                    if os.path.isdir(path) and os.name != "nt":
                        desired |= stat.S_IXUSR
                    if desired != mode:
                        os.chmod(path, desired)
                        cleared += 1
                except OSError:
                    pass                          # the step that needs the path reports it
    return cleared


def unprocessed_bain_packages(staging, only=None):
    """Staged packages still in their archive's layout: numbered folders at the root. A FOMOD package is tidied
    first too: its fomod folder and declared sources move to Extras/<plugin> with their packed paths, which the
    option window reads them from, and its 00 folder is already linked into the root (kept in Extras)."""
    import fnmatch
    from mock_fomod import OPTION_RE
    found = []
    for name in sorted(os.listdir(staging), key=str.casefold):
        folder = os.path.join(staging, name)
        if not os.path.isdir(folder):
            continue
        if only and not any(name.casefold() == pat.casefold() or fnmatch.fnmatch(name.casefold(), pat.casefold())
                            for pat in only):
            continue
        if any(OPTION_RE.match(child) and os.path.isdir(os.path.join(folder, child)) for child in os.listdir(folder)):
            found.append(name)
    return found


def run_pre_option_tidy(args):
    """Tidy BAIN packages before their options are offered: 00 Core unpacks into the root, the numbered optional
    folders move to Extras/<plugin> and the documentation to Docs/<plugin>, which is where the option window
    looks for the choices and their descriptions."""
    args.staging = os.path.realpath(args.staging)
    raw = unprocessed_bain_packages(args.staging, args.only)
    if raw:
        print("MWMANAGER-PROGRESS: Unpacking BAIN packages before their options", flush=True)
        run_post_option_tidy(args, raw)


def run_post_option_tidy(args, changed=None):
    """Re-tidy packages whose BAIN/FOMOD payloads changed, then check their assets."""
    changed = set(changed if changed is not None else getattr(args, "post_option_mods", []) or [])
    if not changed:
        removed = prune_empty_directories(args.staging)
        if removed:
            print(f"Removed {removed} empty staged director{'y' if removed == 1 else 'ies'}.")
        return
    previous_only = list(args.only or [])
    args.only = sorted(changed | set(previous_only), key=str.casefold)
    try:
        print("MWMANAGER-PROGRESS: Reorganizing selected option payloads", flush=True)
        moves = run_plan(args)
        moves, gone = review(args.staging, moves, assume_keep=True)
        if moves or gone:
            print("MWMANAGER-PROGRESS: Applying tidy changes after option selection", flush=True)
            apply(gone + moves, args.staging)
        removed = prune_empty_directories(args.staging)
        if removed:
            print(f"Removed {removed} empty staged director{'y' if removed == 1 else 'ies'}.")
    finally:
        args.only = previous_only


def lua_apply(args, lres):
    """Patch each package's Lua files with its originals kept inside that package."""
    if not lres or not lres["n_ok"]:
        return
    if lres["checker"] is None:
        print("  not patching Lua: no syntax checker (LuaJIT / luac) found")
        return
    if not args.yes and input(f"\nPatch {lres['n_ok']} Lua file(s) in place? [y/N] ").strip().lower() not in \
            ("y", "yes"):
        print("  Lua files not changed.")
        return
    lf, written, refused = lres["lf"], 0, []
    for package_name_raw, report in lres["reps"]:
        package = os.path.join(args.staging, package_name_raw)
        owner = package_name(package)
        count, rejected = lf.apply_report(
            report, journal=JOURNAL,
            backup_root=os.path.join(package, "Off", owner, "LuaRepairs"),
            backup_relative_to=package,
            head_extra=dict({"staging": args.staging, "plugin": package_name_raw, "lua": True},
                            **getattr(args, "_journal_extra", {})), done=lres["done"])
        lines, _counts = lf.format_report(report, indent="  ", done=lres["done"], refused=dict(rejected))
        ah = _scanner.asset_health_module()
        plugin_file = next((name for name in sorted(os.listdir(package), key=str.casefold)
                            if os.path.splitext(name)[1].casefold() in (".esp", ".esm")), None)
        report_name = plugin_file or owner
        bugreport = ah.package_bug_report_path(package, report_name, "mwse")
        header = (ah.bug_report_header(os.path.join(package, plugin_file), _scanner, mod_folder=package)
                  if plugin_file else [f"Bug report for {owner}", f"Mod: {owner} (no plugin)",
                                       f"Generated {time.strftime('%Y-%m-%d %H:%M')} by MWManager Tidy.",
                                       "Paths are relative to this staged package."])
        ah.update_bug_report(bugreport, "vortex_tidy (MWSE Lua)", header, lines)
        ah.update_bug_report(ah.package_bug_report_path(package, report_name),
                             "vortex_tidy (MWSE Lua)", header, [])
        legacy = os.path.join(package, "bugreports", owner + ".mwse.bugreport.txt")
        if os.path.normcase(legacy) != os.path.normcase(bugreport):
            try:
                os.remove(legacy)
            except OSError:
                pass
        written += count
        refused.extend(rejected)
    for path, why in refused:
        print(f"    not patched: {os.path.relpath(path, args.staging)}: {why}")
    print(f"  patched {written} Lua file(s) (originals kept in each package's Off tree; "
          f"journal {JOURNAL}; --undo --only <package> puts one package's back)")


def undo_lua_repairs():
    """Restore the last parser-based Lua repair run, if Tidy created one."""
    journal = os.path.join(OUTPUT_DIR, "mwmanager_luafix_journal.jsonl")
    if not os.path.isfile(journal):
        return 0
    try:
        import mwdialog_luafix as lf
        return lf.undo(journal=journal, log=lambda message: print(message, flush=True))
    except Exception as exc:
        print(f"Could not undo MWSE Lua repairs: {exc}", file=sys.stderr)
        return 1


_upstream_assets_apply = assets_apply


def _write_package_asset_bug_reports(ares):
    """Persist every staged package's asset findings beside that package, including unrepairable findings."""
    ah, m = ares["ah"], ares["m"]
    for package_name_raw, entry in ares["mods"].items():
        issues, root = entry["issues"], entry["root"]
        if not issues:
            continue
        plugin_file = next((name for name in sorted(os.listdir(root), key=str.casefold)
                            if os.path.splitext(name)[1].casefold() in (".esp", ".esm")), None)
        owner = package_name(root)
        header = (ah.bug_report_header(os.path.join(root, plugin_file), m, mod_folder=root) if plugin_file else
                  [f"Bug report for {owner}", f"Mod: {owner} (no plugin)",
                   f"Generated {time.strftime('%Y-%m-%d %H:%M')} by MWManager Tidy.",
                   "Paths are relative to this staged package."])
        lines = [f"Problems in the mod's files: {len(issues)}"]
        lines += [f"  - {severity} {relative}: {message}" for severity, relative, message in issues]
        lines += ["", "Repairable files were fixed locally; their originals are retained in this package's Off tree."]
        report_path = ah.package_bug_report_path(root, plugin_file or owner)
        ah.update_bug_report(report_path, "vortex_tidy (files)", header, lines)


def assets_apply(args, ares, moved_mods=()):
    """Run upstream asset repairs while adding exact per-package restores to Tidy's journal."""
    if not ares:
        return _upstream_assets_apply(args, ares, moved_mods)
    ah, m, game_dir = ares["ah"], ares["m"], ares["game_dir"]
    lpm = m.longpaths_module()
    original_folder, original_longpaths = ah.apply_folder, lpm.apply_mod
    by_plugin = {}

    def folder(root, fixes, module, current_game_dir, plugin_name, *positional, **keywords):
        operations = []
        keywords["journal_ops"] = operations
        result = original_folder(root, fixes, module, current_game_dir, package_name(root), *positional, **keywords)
        by_plugin.setdefault(_path_plugin(root, args.staging), []).extend(operations)
        return result

    def longpaths(root, plan, module, current_game_dir, plugin_name=None, log=print):
        before = {json.dumps(entry, sort_keys=True) for entry in lpm.Record(current_game_dir).moves}
        result = original_longpaths(root, plan, module, current_game_dir, plugin_name, log)
        operations = by_plugin.setdefault(_path_plugin(root, args.staging), [])
        added = [entry for entry in lpm.Record(current_game_dir).moves
                 if json.dumps(entry, sort_keys=True) not in before
                 and entry.get("mod", "").casefold() == os.path.basename(os.path.normpath(root)).casefold()]
        for record in added:
            operations += [{"src": src, "dst": dst} for src, dst in record.get("files", [])]
            for plugin_path in record.get("esp", []):
                operations.append({"restore": plugin_path,
                                   "backup": module.off_dest(root, "longpaths", os.path.basename(plugin_path),
                                                             os.path.basename(plugin_path))})
            for nif_path in record.get("nif", []):
                operations.append({"restore": nif_path,
                                   "backup": module.off_dest(root, "longpaths", plugin_name,
                                                             os.path.relpath(nif_path, root))})
        if added:
            operations.append({"longpath_records": [entry["orig"] for entry in added],
                               "game_dir": current_game_dir})
        return result

    ah.apply_folder, lpm.apply_mod = folder, longpaths
    try:
        result = _upstream_assets_apply(args, ares, moved_mods)
        _write_package_asset_bug_reports(ares)
        return result
    finally:
        ah.apply_folder, lpm.apply_mod = original_folder, original_longpaths
        for plugin, operations in by_plugin.items():
            if not plugin or not operations:
                continue
            with open(JOURNAL, "a", encoding="utf-8") as journal:
                journal.write(json.dumps({"run": time.strftime("%Y-%m-%d %H:%M:%S"),
                                          "staging": args.staging, "plugins": [plugin], "assets": True}) + "\n")
                for operation in operations:
                    operation["plugin"] = plugin
                    journal.write(json.dumps(operation) + "\n")


def _prune_empty_directories(staging, journal):
    """Remove every empty directory under staged packages, deepest first."""
    removed = 0
    for package in sorted((p for p in Path(staging).iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        for base, _dirs, _files in os.walk(package, topdown=False):
            if os.path.normcase(os.path.abspath(base)) == os.path.normcase(os.path.abspath(package)):
                continue
            try:
                if os.listdir(base):
                    continue
                os.rmdir(base)
            except PermissionError:
                try:
                    os.chmod(base, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
                    if os.listdir(base):
                        continue
                    os.rmdir(base)
                except OSError:
                    continue
            except OSError:
                continue
            journal.write(json.dumps({"rmdir": base, "plugin": package.name}) + "\n")
            journal.flush()
            removed += 1
    return removed


def prune_empty_directories(staging):
    """Final cleanup pass, including after FOMOD activation or a no-op tidy plan."""
    if not os.path.isdir(staging):
        return 0
    import io
    buf = io.StringIO()
    removed = _prune_empty_directories(staging, buf)
    if removed:                                       # journaled as a run of its own only when it did something
        with open(JOURNAL, "a", encoding="utf-8") as journal:
            journal.write(json.dumps({"run": time.strftime("%Y-%m-%d %H:%M:%S"), "staging": staging}) + "\n")
            journal.write(buf.getvalue())
    return removed


def apply(moves, staging, progress=None, head_extra=None):
    """Make the moves, journaling each. progress (a list) gets the index of every move done, as it is done,
    so an interrupted run knows what is left. Returns the moves that failed."""
    done, failed, packing = 0, [], {}
    plugins = sorted({_path_plugin(src, staging) for src, _dst, _why in moves if _path_plugin(src, staging)},
                     key=str.casefold)
    total = len(moves)
    last_reported = -1

    def mark(index, *, force=False, record=True):
        nonlocal last_reported
        if record and progress is not None:
            progress.append(index)
        if (os.environ.get("MWMANAGER_GUI") == "1"
                and (force or index == 0 or ((index + 1) % 2000 == 0 and index != total - 1)
                     or index == total - 1)
                and index != last_reported):
            print(f"MWMANAGER-PROGRESS: Applying Tidy moves {index + 1:,} of {total:,}",
                  file=sys.stderr, flush=True)
            last_reported = index

    if total and os.environ.get("MWMANAGER_GUI") == "1":
        print(f"MWMANAGER-PROGRESS: Applying Tidy moves 0 of {total:,}",
              file=sys.stderr, flush=True)
    head = {"run": time.strftime("%Y-%m-%d %H:%M:%S"), "staging": staging, "plugins": plugins}
    head.update(head_extra or {})
    with open(JOURNAL, "a", encoding="utf-8") as j:
        j.write(json.dumps(head) + "\n")
        j.flush()
        for i, (src, dst, why) in enumerate(moves):
            try:
                if dst is None:
                    if why.startswith("identical duplicate from "):
                        target = why.removeprefix("identical duplicate from ")
                        os.remove(src)
                        j.write(json.dumps({"duplicate": target, "restore": src,
                                            "plugin": _path_plugin(src, staging)}) + "\n")
                        j.flush()
                        done += 1
                        mark(i)
                        continue
                    for base, dirs, fns in os.walk(src, topdown=False):   # only empty folders go
                        if not os.listdir(base):
                            try:
                                os.rmdir(base)
                            except PermissionError:
                                # Archive tools can mark empty directories read-only.
                                # Windows refuses to remove those until the attribute
                                # is cleared; this touches only a verified-empty folder.
                                if os.name == "nt":
                                    import ctypes
                                    get_attrs = ctypes.windll.kernel32.GetFileAttributesW
                                    set_attrs = ctypes.windll.kernel32.SetFileAttributesW
                                    get_attrs.argtypes = [ctypes.c_wchar_p]
                                    get_attrs.restype = ctypes.c_uint32
                                    set_attrs.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
                                    set_attrs.restype = ctypes.c_int
                                    attrs = get_attrs(base)
                                    if attrs != 0xFFFFFFFF:
                                        set_attrs(base, attrs & ~0x1)  # FILE_ATTRIBUTE_READONLY
                                else:
                                    os.chmod(base, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
                                os.rmdir(base)
                            j.write(json.dumps({"rmdir": base, "plugin": _path_plugin(base, staging)}) + "\n")
                            j.flush()
                    mark(i)
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if why.startswith(LINK_PREFIX):
                    # 00 Core content: the root gets a hard link (no extra space) and the 00 folder itself is kept
                    # under Extras, so the install can be repeated from the package
                    try:
                        os.link(src, dst)
                    except OSError:
                        shutil.copy2(src, dst)
                    j.write(json.dumps({"link": dst, "source": src, "plugin": _path_plugin(src, staging)}) + "\n")
                    j.flush()
                    if PACKING_MARK in why:
                        packing.setdefault(_path_plugin(src, staging), []).append((src, dst, why))
                    done += 1
                    mark(i)
                    continue
                # journaled (on disk) before the move: a crash between the two never leaves a move --undo can't see
                j.write(json.dumps({"src": src, "dst": dst, "plugin": _path_plugin(src, staging)}) + "\n")
                j.flush()
                os.fsync(j.fileno())
                os.rename(src, dst)          # staging stays on one drive, so moves are cheap
                if PACKING_MARK in why:
                    packing.setdefault(_path_plugin(src, staging), []).append((src, dst, why))
                done += 1
                mark(i)
                if NO_DIFFUSE_WARNING in why:
                    print(f"  WARNING: moved {src} -> {dst}: {NO_DIFFUSE_WARNING}")
            except OSError as e:
                print(f"  could not move {src}: {e}", file=sys.stderr)
                failed.append((src, dst, why))
                mark(i)
        # Prune by checking the live directory contents: os.walk's bottom-up
        # dirs list still includes children removed earlier in the same walk.
        _prune_empty_directories(staging, j)
    _write_packing_reports(staging, packing)
    print(f"Moved {done} item(s). Journal: {JOURNAL} (each entry names its package: --undo --only <package> "
          f"puts back that package's changes from every run, --undo alone the whole last run).")
    if total and last_reported != total - 1:
        mark(total - 1, force=True, record=False)
    return failed


def _write_packing_reports(staging, packing):
    """Each package whose author misnamed a game folder gets a "packing" section in its bug report: what was
    renamed, and every file the rename moved."""
    if not packing:
        return
    ah = _scanner.asset_health_module()
    for package_dir_name, items in packing.items():
        package = os.path.join(staging, package_dir_name)
        owner = package_name(package)
        plugin_file = next((name for name in sorted(os.listdir(package), key=str.casefold)
                            if os.path.splitext(name)[1].casefold() in (".esp", ".esm")), None)
        header = (ah.bug_report_header(os.path.join(package, plugin_file), _scanner, mod_folder=package)
                  if plugin_file else [f"Bug report for {owner}", f"Mod: {owner} (no plugin)",
                                       f"Generated {time.strftime('%Y-%m-%d %H:%M')} by MWManager Tidy.",
                                       "Paths are relative to this staged package."])
        problems = sorted({why[why.index(PACKING_MARK) + len(PACKING_MARK):] for _s, _d, why in items})
        lines = [f"Packing errors: {len(problems)}"] + [f"  - ERROR {p}" for p in problems]
        lines += ["", f"Files moved by the rename ({len(items)}):"]
        lines += [f"  {os.path.relpath(src, package)} -> {os.path.relpath(dst, package)}" for src, dst, _w in items]
        ah.update_bug_report(ah.package_bug_report_path(package, plugin_file or owner),
                             "vortex_tidy (packing)", header, lines)


def _path_plugin(path, staging):
    """The top-level staging package containing path, or None for records outside staging."""
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(staging))
    except (OSError, TypeError, ValueError):
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return rel.split(os.sep, 1)[0]


def _op_plugin(op, staging):
    if op.get("plugin"):
        return str(op["plugin"])
    for name in ("src", "restore", "generated", "luafix", "rmdir", "dst"):
        if op.get(name):
            plugin = _path_plugin(op[name], staging)
            if plugin:
                return plugin
    return None


def undo(staging=STAGING, plugin=None):
    if not os.path.exists(JOURNAL):
        try:
            from mock_fomod import undo_staging_options
            undone = undo_staging_options(staging, OUTPUT_DIR, only=plugin)
        except Exception as exc:
            print(f"Could not undo option selections: {exc}", file=sys.stderr)
            return 1
        had_lua_journal = os.path.isfile(os.path.join(OUTPUT_DIR, "mwmanager_luafix_journal.jsonl"))
        lua_undone = 0 if plugin else undo_lua_repairs()
        if not undone and (plugin or not had_lua_journal):
            print("Nothing to undo (no journal).")
            return 1
        return lua_undone
    runs, cur = [], None
    with open(JOURNAL, encoding="utf-8") as jf:
        journal_lines = jf.read().splitlines()
    for line in journal_lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:                            # a line cut short when an --apply was interrupted
            print(f"  (skipped an unreadable journal line: {line[:80]!r})", file=sys.stderr)
            continue
        if "run" in rec:
            cur = {"head": rec, "ops": []}
            runs.append(cur)
        elif cur is not None:
            cur["ops"].append(rec)
    if not runs:
        print("Nothing to undo.")
        return 1
    wanted = plugin.casefold() if plugin else None
    targets = ([i for i, run in enumerate(runs)
                if any((_op_plugin(op, staging) or "").casefold() == wanted for op in run["ops"])]
               if plugin else [len(runs) - 1])
    if not targets:
        try:
            from mock_fomod import undo_staging_options
            undone = undo_staging_options(staging, OUTPUT_DIR, only=plugin)
        except Exception as exc:
            print(f"Could not undo option selections for {plugin}: {exc}", file=sys.stderr)
            return 1
        print(f"No Tidy journal entries for {plugin}.")
        return 0 if undone else 1
    latest_run = runs[targets[-1]]["head"]["run"]
    back = failed_count = 0
    for target in reversed(targets):
        last = runs[target]
        selected = {i for i, op in enumerate(last["ops"])
                    if not plugin or (_op_plugin(op, staging) or "").casefold() == wanted}
        failed = set()
        for index in sorted(selected, reverse=True):
            op = last["ops"][index]
            if "luafix" in op:                        # a Lua file mwdialog_luafix patched in place
                lf = _luafix()
                ok, msg = lf.restore_op(op) if lf else (False, f"{op['luafix']}: mwdialog_luafix.py not found")
                if ok:
                    back += 1
                else:
                    print("  " + msg, file=sys.stderr)
                    failed.add(index)
                continue
            try:
                if "longpath_records" in op:
                    record = _scanner.longpaths_module().Record(op["game_dir"])
                    record_plugin = wanted or (_op_plugin(op, staging) or "").casefold()
                    originals = {name.replace("/", "\\").casefold() for name in op["longpath_records"]}
                    before = len(record.moves)
                    record.moves = [entry for entry in record.moves
                                    if not (entry.get("mod", "").casefold() == record_plugin
                                            and entry.get("orig", "").replace("/", "\\").casefold() in originals)]
                    record.save()
                    back += before - len(record.moves)
                elif "rmdir" in op:
                    os.makedirs(op["rmdir"], exist_ok=True)
                elif "duplicate" in op:
                    if os.path.isfile(op["duplicate"]):
                        os.makedirs(os.path.dirname(op["restore"]), exist_ok=True)
                        shutil.copy2(op["duplicate"], op["restore"])
                        back += 1
                    else:
                        failed.add(index)
                elif "restore" in op:
                    if os.path.isfile(op["backup"]):
                        shutil.copy2(op["backup"], op["restore"])
                        back += 1
                    else:
                        failed.add(index)
                elif "link" in op:                    # a 00 Core file linked into the root: only the link goes
                    if os.path.isfile(op["link"]):
                        os.remove(op["link"])
                    back += 1
                elif "generated" in op:
                    generated = op["generated"]
                    if not os.path.exists(generated):
                        back += 1
                    elif (op.get("sha256") and hashlib.sha256(Path(generated).read_bytes()).hexdigest()
                          != op["sha256"]):
                        print(f"  not removed (generated file changed): {generated}", file=sys.stderr)
                        failed.add(index)
                    else:
                        os.remove(generated)
                        back += 1
                elif os.path.exists(op["dst"]):
                    if os.path.exists(op["src"]):
                        print(f"  not put back (something is at {op['src']} already): {op['dst']}", file=sys.stderr)
                        failed.add(index)
                        continue
                    os.makedirs(os.path.dirname(op["src"]), exist_ok=True)
                    os.rename(op["dst"], op["src"])
                    back += 1
                elif os.path.exists(op["src"]):
                    back += 1                         # journaled, but the move itself never happened
                else:
                    print(f"  not put back (no longer there): {op['dst']}", file=sys.stderr)
                    failed.add(index)
            except OSError as e:
                print(f"  could not put back {op.get('dst')}: {e}", file=sys.stderr)
                failed.add(index)
        top = last["head"].get("staging") or staging   # never the staging folder itself or anything above it
        top = os.path.normcase(os.path.abspath(top))
        for d in {os.path.dirname(last["ops"][i]["dst"]) for i in selected if "dst" in last["ops"][i]}:
            try:
                while d and os.path.normcase(os.path.abspath(d)).startswith(top + os.sep)                         and os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
                    d = os.path.dirname(d)
            except OSError:
                pass
        remaining = [op for i, op in enumerate(last["ops"]) if i not in selected or i in failed]
        marker = last["head"].get("once_marker")
        if marker and not remaining:
            try:
                os.remove(marker)
                print(f"Cleared one-time marker: {marker}")
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"  could not clear one-time marker {marker}: {e}", file=sys.stderr)
        if remaining:
            runs[target] = {"head": last["head"], "ops": remaining}
        else:
            runs.pop(target)
        failed_count += len(failed)
    temp_journal = JOURNAL + ".tmp"
    with open(temp_journal, "w", encoding="utf-8") as j:
        for r in runs:
            j.write(json.dumps(r["head"]) + "\n")
            for op in r["ops"]:
                j.write(json.dumps(op) + "\n")
    os.replace(temp_journal, JOURNAL)
    try:
        from mock_fomod import undo_staging_options
        undo_staging_options(staging, OUTPUT_DIR, only=plugin)
    except Exception as exc:
        print(f"  Could not undo option selections: {exc}", file=sys.stderr)
    lua_undone = 0 if plugin else undo_lua_repairs()
    scope = f" for {plugin}" if plugin else ""
    print(f"Put back {back} item(s){scope} through the run of {latest_run}.")
    if failed_count or lua_undone:
        print(f"{failed_count} tidy item(s) could not be put back; the remaining journal entries can be retried.")
        return 1
    return 0


def _doc_key(name):
    """Normalize a plugin/readme stem for exact association, without fuzzy matching unrelated mods."""
    stem = os.path.splitext(os.path.basename(name))[0].casefold()
    stem = re.sub(r"^(?:read\s*me|readme|manual|documentation|docs?)[\s._-]*", "", stem)
    stem = re.sub(r"[\s._-]*(?:read\s*me|readme|manual|documentation|docs?)$", "", stem)
    return re.sub(r"[^a-z0-9]", "", stem)
DOC_PLUGIN_ALIASES = {"firemoth": "siegeatfiremoth", "ebartifact": "ebqartifact"}
OFFICIAL_ADDON_DOCS = "Official Add-Ons"     # Data Files\Docs\<this>: the shipped add-ons' readmes
OFFICIAL_ADDON_DOC_KEYS = {
    "adamantiumarmor", "areaeffectarrows", "bcsounds", "ebqartifact",
    "entertainers", "lefemmarmor", "masterindex", "siegeatfiremoth",
}


def plan_data_files(data_dir):
    """Move root-level documents into Data Files\\Docs, grouping official add-on docs."""
    try:
        entries = os.listdir(data_dir)
    except OSError as exc:
        return [], [str(exc)]
    plugin_keys = {_doc_key(e) for e in entries
                   if os.path.isfile(os.path.join(data_dir, e))
                   and os.path.splitext(e)[1].lower() in (".esp", ".esm")}
    plugin_keys.discard("")
    docs = next((e for e in entries if e.casefold() == "docs"
                 and os.path.isdir(os.path.join(data_dir, e))), "docs")
    docs_dir = os.path.join(data_dir, docs)
    official_dir = os.path.join(docs_dir, OFFICIAL_ADDON_DOCS)
    moves, taken, errors = [], set(), []
    for name in sorted(entries, key=str.casefold):
        src = os.path.join(data_dir, name)
        if not os.path.isfile(src) or not is_doc(name) or name.casefold() in VORTEX_FILES:
            continue
        if name.casefold().endswith(".bugreport.txt"):
            continue
        key = _doc_key(name)
        key = DOC_PLUGIN_ALIASES.get(key, key)
        associated = bool(key and key in plugin_keys)
        official = associated and key in OFFICIAL_ADDON_DOC_KEYS
        new_name = name if associated or name.casefold().startswith("no known mod - ") else f"No Known Mod - {name}"
        target_dir = official_dir if official else docs_dir
        dst = free_path(os.path.join(target_dir, new_name), taken)
        reason = ("official base-game add-on documentation" if official else
                  "document associated with an installed plugin" if associated else
                  "document has no matching .esp/.esm; label as No Known Mod")
        moves.append((src, dst, reason))
    return moves, errors


def plan_official_addon_docs(data_dir):
    """Plan migration of already-organized official docs into Docs\\Official Add-Ons
    (including from the older misspelled "Official Addon-Ons" folder)."""
    entries = os.listdir(data_dir)
    docs = next((name for name in entries if name.casefold() == "docs"
                 and os.path.isdir(os.path.join(data_dir, name))), "docs")
    docs_dir = os.path.join(data_dir, docs)
    try:
        names = os.listdir(docs_dir)
    except OSError:
        return [], []
    official_dir = os.path.join(docs_dir, OFFICIAL_ADDON_DOCS)
    moves, taken = [], set()
    sources = [(docs_dir, name) for name in names]
    for folder in names:
        folder_path = os.path.join(docs_dir, folder)
        if (os.path.isdir(folder_path) and folder.casefold().replace(" ", "")
                in {"officialadd-ons", "officialaddon-ons", "officialaddons"}):
            try:
                sources.extend((folder_path, name) for name in os.listdir(folder_path))
            except OSError:
                continue
    for parent, name in sorted(sources, key=lambda item: (item[0].casefold(), item[1].casefold())):
        if os.path.normcase(parent) == os.path.normcase(official_dir):
            continue
        source = os.path.join(parent, name)
        if not os.path.isfile(source) or not is_doc(name):
            continue
        key = DOC_PLUGIN_ALIASES.get(_doc_key(name), _doc_key(name))
        if key not in OFFICIAL_ADDON_DOC_KEYS:
            continue
        destination = free_path(os.path.join(official_dir, name), taken)
        if os.path.normcase(source) != os.path.normcase(destination):
            moves.append((source, destination, "official base-game add-on documentation"))
    return moves, []


def run_data_files_tidy(data_dir, *, apply_changes=False, marker=None, dry_run=False):
    data_dir = os.path.abspath(data_dir)
    if not os.path.isdir(data_dir):
        print(f"Data Files folder not found: {data_dir}", file=sys.stderr)
        return 2
    if marker and os.path.isfile(marker):
        moves, errors = plan_official_addon_docs(data_dir)
        if not moves and not errors:
            print(f"Data Files tidy already completed for this installation (marker: {marker}); official add-on docs are organized.")
            return 0
    else:
        moves, errors = plan_data_files(data_dir)
    lines = [f"Data Files tidy plan: {len(moves)} document(s) to move."]
    for src, dst, why in moves:
        lines.append(f"  {os.path.relpath(src, data_dir)} -> {os.path.relpath(dst, data_dir)} ({why})")
    lines.extend(f"  ERROR: {e}" for e in errors)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(DATA_FILES_PLAN, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"Full plan: {DATA_FILES_PLAN}")
    if errors:
        return 1
    if dry_run or not apply_changes:
        print("No files changed." if dry_run else "Plan only; pass --apply to move documents.")
        return 0

    import mwmanager_journal
    install_ledger = mwmanager_journal.Journal(Path(MANAGER_DIR) / "journal")
    if not install_ledger.has_baseline():
        print("Data Files tidy refused: record the install baseline first so every original move can be rolled back.",
              file=sys.stderr)
        return 2
    ledger_run = install_ledger.start_run("mwmanager_install.py", {"operation": "Data Files document tidy"})
    done, failed = 0, []
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as legacy_journal:
        header = {"run": time.strftime("%Y-%m-%d %H:%M:%S"), "data_files": data_dir}
        if marker:
            header["once_marker"] = os.path.abspath(marker)
        legacy_journal.write(json.dumps(header) + "\n")
        for src, dst, why in moves:
            try:
                install_ledger.move_original(Path(data_dir).parent, src, dst, ledger_run["run"],
                                             tag="data-files-document")
                legacy_journal.write(json.dumps({"src": src, "dst": dst, "why": why}) + "\n")
                legacy_journal.flush()
                done += 1
            except (OSError, ValueError, RuntimeError) as exc:
                failed.append(f"{src}: {exc}")
    install_ledger.finish_run(ledger_run)
    if failed:
        print(f"Moved {done} document(s); {len(failed)} could not be moved. The one-time marker was not written.",
              file=sys.stderr)
        for item in failed:
            print("  " + item, file=sys.stderr)
        return 1
    if marker:
        os.makedirs(os.path.dirname(os.path.abspath(marker)), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            json.dump({"data_files": data_dir, "completed": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "documents_moved": done}, f, indent=2)
            f.write("\n")
    print(f"Moved {done} document(s) into Data Files\\Docs. Journal: {JOURNAL} (--undo restores them).")
    return 0


def run_initial_tidy(data_dir, *, apply_changes=True):
    """Tidy the live initial install, then apply the scanner's safe plugin and asset repairs."""
    rc = run_data_files_tidy(data_dir, apply_changes=apply_changes, dry_run=not apply_changes)
    if rc:
        return rc
    command = [data_dir, "--fix", "--asset-check", "--no-interactive"]
    command += ["--yes"] if apply_changes else ["--dry-run"]
    print("\nInitial-install Tidy: checking official plugins and live Data Files assets.")
    try:
        result = _scanner.main(command)
        if apply_changes and result == 1:
            print("Initial-install Tidy repaired reported errors; --full will verify the result.")
            return 0
        return result
    finally:
        # --full runs next in another process; commit the SQLite facts it will reuse first.
        _scanner.asset_health_module().close_caches()


# ---- the Data Files folder upstream looks for next to vortex_tidy.py --------------------------------------------

GAME_DATA_FILES = None                               # set from --game-data-files by mwmanager_main()


def mwmanager_data_files(default):
    """--game-data-files (or MWMANAGER_DATA_FILES), else upstream's own choice."""
    return GAME_DATA_FILES or os.environ.get("MWMANAGER_DATA_FILES") or default


def _rebase_data_dir(name):
    """Recompile upstream's `name` with its Data Files folder taken from mwmanager_data_files(); unchanged (with a
    note) if its source no longer has the expected line."""
    import inspect
    import textwrap
    func = globals()[name]
    if getattr(func, "_mwmanager_rebased", False):
        return True
    try:
        code = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return False
    new = code
    for expr in ('os.path.join(game_dir, "Data Files")', 'os.path.join(HERE, "Data Files")'):
        new = new.replace(f"data_dir = {expr}", f"data_dir = mwmanager_data_files({expr})")
    if new == code:
        print(f"(MWManager: vortex_tidy.{name} no longer reads Data Files the expected way; using it unchanged)",
              file=sys.stderr)
        return False
    ns = {}
    exec(compile(new, inspect.getsourcefile(func) or "<vortex_tidy>", "exec"), globals(), ns)
    ns[name]._mwmanager_rebased = True
    globals()[name] = ns[name]
    return True


for _name in ("texture_index", "lua_check", "assets_check"):
    _rebase_data_dir(_name)
del _name


# ---- command line ----------------------------------------------------------------------------------------------

_upstream_apply_main = apply_main


def apply_main(args):
    """Allow the GUI's explicit post-retry override to enact the plan calculated by this pass."""
    if not getattr(args, "force_plan", False):
        return _upstream_apply_main(args)
    compare = PF.compare
    PF.compare = lambda _reviewed, _fresh, _parts: ([], [])
    try:
        print("WARNING: applying the freshly calculated plan by explicit user request.")
        return _upstream_apply_main(args)
    finally:
        PF.compare = compare

def bundled_luajit():
    exe = os.path.join(MANAGER_DIR, "tools", "luajit", "luajit.exe" if os.name == "nt" else "luajit")
    return exe if os.path.isfile(exe) else None


def mwmanager_parser():
    ap = MT.parser(description="MWManager Tidy: choose staged BAIN/FOMOD options, then plan and apply the tidy of "
                               "MWManager's per-mod staging folders, or organize Data Files documents.")
    ap.add_argument("--data-files", metavar="PATH", help="organize root-level documents in a Data Files folder")
    ap.add_argument("--init", action="store_true",
                    help="initial install: tidy and auto-repair the live --game-data-files tree")
    ap.add_argument("--once-marker", metavar="PATH",
                    help="with --data-files: write this marker after a successful tidy; skip if it exists")
    ap.add_argument("--dry-run", action="store_true", help="with --data-files: show the plan, change nothing")
    ap.add_argument("--staging", default=STAGING, help=f"per-mod staging folder (default {STAGING})")
    ap.add_argument("--game-data-files", metavar="PATH",
                    help="the real game Data Files used to look up installed masters, textures, and BSAs "
                         "(or set MWMANAGER_DATA_FILES; required for staged Tidy)")
    ap.add_argument("--only", nargs="+", metavar="PATTERN", help="only these mod folders (wildcards allowed)")
    ap.add_argument("--skip", nargs="+", metavar="PATTERN",
                    help="leave these mod folders alone this time too (to always skip one, add it to "
                         f"{os.path.basename(BLACKLIST_FILE)})")
    ap.add_argument("--options", action="store_true",
                    help="step 1: choose the BAIN/FOMOD options of the mods (applied as chosen), then write the plan")
    ap.add_argument("--options-only", action="store_true",
                    help="like --options, reopening choices made before")
    ap.add_argument("--fomod-first", action="store_true", help=argparse.SUPPRESS)   # the order is always options first
    ap.add_argument("--apply", action="store_true",
                    help="step 3: enact the reviewed plan file (refused, exit 2, if the plan changed since)")
    ap.add_argument("--force-plan", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--yes", action="store_true", help="with --apply: don't ask first")
    ap.add_argument("--undo", action="store_true",
                    help="put back the last --apply, or only the package named by --only")
    ap.add_argument("--no-assets", action="store_true", help="skip the asset check entirely")
    ap.add_argument("--no-lua", action="store_true", help="skip the Lua check (mwdialog_luafix)")
    ap.add_argument("--lua-level", choices=("high", "medium"), default="high",
                    help="which Lua findings --apply patches (default high)")
    ap.add_argument("--luajit", default=None, help="the LuaJIT (or luac) to check with (default: tools\\luajit)")
    ap.add_argument("--lua-log", choices=("flagged", "all", "off"), help="which mods the Lua check gives the log shim")
    return ap


def _plan_state(args):
    parts = ["moves"] + ([] if args.no_lua else [LUA_HEAD]) + ([] if args.no_assets else [ASSET_HEAD])
    pending = PF.state(PLAN) == "pending" and PF.has_any_actions(PF.read(PLAN), parts)
    print(f"MWMANAGER-PLAN: {'pending' if pending else 'empty'} {PLAN}", flush=True)
    return pending


def mwmanager_main(argv=None):
    """Steps: --options (choose BAIN/FOMOD options) -> plan (no --apply; nothing changed) -> --apply. Exit codes:
    0 done, 1 failed, 2 refused (plan mismatch), 3 option selection cancelled."""
    global GAME_DATA_FILES
    MT.install()
    ap = mwmanager_parser()
    args = ap.parse_args(argv)
    if args.force_plan and not args.apply:
        ap.error("--force-plan requires --apply")
    if args.init:
        if args.undo or args.options or args.options_only:
            ap.error("--init cannot be combined with --undo or --options")
        data_files = args.game_data_files or os.environ.get("MWMANAGER_DATA_FILES")
        if not data_files:
            ap.error("--init requires --game-data-files")
        return run_initial_tidy(data_files, apply_changes=args.apply)
    if args.data_files:
        if args.undo or args.options or args.options_only:
            ap.error("--undo and --options cannot be combined with --data-files")
        return run_data_files_tidy(args.data_files, apply_changes=args.apply, marker=args.once_marker,
                                   dry_run=args.dry_run)
    if args.once_marker or args.dry_run:
        ap.error("--once-marker and --dry-run require --data-files")
    options = args.options or args.options_only
    if options and (args.apply or args.undo):
        ap.error("--options writes a plan to review: run --apply afterwards (and --undo on its own)")
    if args.undo:
        if args.only and len(args.only) != 1:
            ap.error("--undo accepts exactly one package name with --only")
        return undo(args.staging, args.only[0] if args.only else None)
    if not os.path.isdir(args.staging):
        print(f"Staging folder not found: {args.staging}", file=sys.stderr)
        return 2
    GAME_DATA_FILES = args.game_data_files or os.environ.get("MWMANAGER_DATA_FILES")
    if not GAME_DATA_FILES:
        ap.error("staged Tidy requires --game-data-files or MWMANAGER_DATA_FILES")
    if not os.path.isdir(GAME_DATA_FILES):
        ap.error(f"game Data Files folder not found: {GAME_DATA_FILES}")
    args.game_data_files = GAME_DATA_FILES
    args.luajit = args.luajit or bundled_luajit()
    args.lua_only = False
    cleared = clear_readonly_flags(args.staging, args.only)
    if cleared:
        print(f"Cleared the read-only flag on {cleared} staged path(s).")
    if options:
        print("MWMANAGER-PROGRESS: Reviewing staged BAIN/FOMOD choices", flush=True)
        run_pre_option_tidy(args)
        option_result = run_mock_options(args)
        if option_result:
            return option_result
        run_post_option_tidy(args)
    if not args.apply:
        print("MWMANAGER-PROGRESS: Organizing plugin folders and checking tidy rules", flush=True)
        with _long_phase_heartbeat("Planning Tidy, Lua checks, and asset repairs"):
            rc = plan_main(args)
        _plan_state(args)
        return rc
    print("MWMANAGER-PROGRESS: Applying the reviewed tidy plan", flush=True)
    with _long_phase_heartbeat("Validating and applying the reviewed tidy plan"):
        rc = apply_main(args)
    if rc == 2:
        return rc
    update_omwaddon_masters(args.staging, apply_changes=True)
    align_master_versions(args.staging, GAME_DATA_FILES, apply_changes=True)
    return rc
