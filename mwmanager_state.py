"""MWManager layer: where MWManager keeps its state, and where backups go.

Runs inside the upstream mwdialog_check namespace (see mwmanager_upstream.py); every name set here replaces the
upstream one in mwdialog_check and in each mwdialog_* module that imported it.

Upstream joins its records to the game folder (os.path.join(game_dir, NAME)). Making NAME an absolute path under
the MWManager folder sends every read and write there instead: nothing of MWManager's state goes into the game.
"""
from mwdialog_check import *  # noqa: F401,F403  (names resolve in mwdialog_check when composed)
import codecs
import copy
import csv
import glob
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

MANAGER_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
               else os.path.dirname(os.path.abspath(sys.modules["mwmanager_upstream"].__file__)))

_upstream_replace_asset_in_place = replace_asset_in_place


def replace_asset_in_place(path, data):
    """Atomically replace a live asset and append enough journal state to restore its original."""
    target = Path(path).resolve()
    data_dir = next((parent for parent in (target.parent, *target.parents)
                     if parent.name.casefold() == "data files"), None)
    ledger = run = original = None
    if data_dir is not None:
        import mwmanager_journal
        candidate = mwmanager_journal.Journal(Path(MANAGER_DIR) / "journal")
        if candidate.has_baseline():
            ledger = candidate
            original = ledger.preserve_original(data_dir.parent, target)
            run = ledger.start_run("mwmanager.py", {"operation": "replace asset", "path": str(target)})
    result = _upstream_replace_asset_in_place(path, data)
    if ledger:
        ledger.record(run["run"], mwmanager_journal.rel_of(data_dir.parent, target),
                      "replace" if original["kind"] != "absent" else "create",
                      mwmanager_journal.sha256_file(target), original, tag="asset-repair")
        backup = _ASSET_BACKUP_OF.get(os.path.normcase(os.path.abspath(path)))
        baseline = {mwmanager_journal.key(rel) for rel in ledger.baseline().get("files", {})}
        for artifact in (backup, backup + REPAIRED_SIDECAR if backup else None):
            if not artifact or not os.path.isfile(artifact):
                continue
            try:
                rel = mwmanager_journal.rel_of(data_dir.parent, Path(artifact).resolve())
            except ValueError:
                continue
            if mwmanager_journal.key(rel) not in baseline and not ledger.first_original(rel):
                ledger.record(run["run"], rel, "create", mwmanager_journal.sha256_file(artifact),
                              {"kind": "absent"}, tag="asset-repair-backup")
        ledger.finish_run(run)
    return result


def state_names(manager_dir):
    """Every folder and record path MWManager keeps under `manager_dir` (the MWManager folder). Upstream joins
    these to the game folder; as absolute paths they land here instead."""
    out = os.path.join(manager_dir, "outputs")
    own = lambda name: os.path.join(manager_dir, name)               # noqa: E731
    names = {
        "MANAGER_DIR": manager_dir, "OUTPUT_DIR": out, "BACKUP_DIR": own("backups"), "TOOLS_DIR": own("tools"),
        "STAGING_DIR": own("staging"), "DISABLED_PLUGINS_DIR": own("disabled_plugins"),
        "CORE_HASH_MANIFEST": own("mwmanager_core_hashes.json"),
        # records the user or the GUI edits, in the MWManager folder
        "LOAD_ORDER_FILE": own("mwmanager_sort_order.txt"), "RULES_FILE": own("mwmanager_rules.txt"),
        "DATES_UNDO_FILE": own("mwmanager_loadorder_undo.txt"), "MERGE_RECORD": own("mwmanager_merged.txt"),
        "MERGE_REBUILD_RECORD": own("mwmanager_merge_rebuild.txt"), "DISABLED_RECORD": own("mwmanager_disabled.txt"),
        "CONFLICT_BLACKLIST_FILE": own("mwmanager_conflict_blacklist.txt"),
        "CONFLICT_ANSWERS_FILE": own("mwmanager_conflict_answers.json"),
        "CELL_WINNERS_FILE": own("mwmanager_cell_winners.txt"),
        "ADDED_MASTERS_RECORD": own("mwmanager_added_masters.json"),
        # what runs write: reports and the lists undo reads
        "GENERATED_RECORD": os.path.join(out, "mwmanager_generated_files.txt"),
        "REDUCED_RECORD": os.path.join(out, "mwmanager_reduced_textures.txt"),
        "NEW_CONFLICTS_FILE": os.path.join(out, "mwmanager_new_conflicts.txt"),
        "SHORT_MESH_RECORD": os.path.join(out, "mwmanager_short_meshes.txt"),
        "REDUNDANT_RECORD": os.path.join(out, "mwmanager_redundant.txt"),
        "MISSING_NORMALS_RECORD": os.path.join(out, "mwmanager_missing_normal_maps.txt"),
        "FULLSIZE_DIR": os.path.join(manager_dir, "backups", "fullsize_textures"),
    }
    names["LOAD_ORDER_PATH"] = names["LOAD_ORDER_FILE"]
    names["IGNORED_NORMALS_RECORD"] = names["MISSING_NORMALS_RECORD"]
    return names


def module_state_names(manager_dir):
    """The same for upstream modules outside the scanner namespace: {module: {name: path}}."""
    own = lambda name: os.path.join(manager_dir, name)               # noqa: E731
    return {
        "mwdialog_mastercheck": {"CACHE_FILE": own("mwmanager_mastercheck_cache.json"),
                                 "SIZES_RECORD": own("mwmanager_master_sizes.json")},
        "mwdialog_longpaths": {"RECORD": os.path.join(manager_dir, "outputs", "mwmanager_longpaths.json")},
        "mwdialog_assets": {"CACHE_DB": os.path.join(manager_dir, "outputs", "mwmanager_asset_cache.sqlite"),
                            "CACHE_FILE": os.path.join(manager_dir, "outputs", "mwmanager_asset_cache.json"),
                            "UNUSED_RECORD": os.path.join(manager_dir, "outputs", "mwmanager_unused_assets.txt"),
                            "MIP_EXEMPT_RECORD": own("mwmanager_mip_exempt.txt")},
        "mwdialog_luafix": {"JOURNAL": os.path.join(manager_dir, "outputs", "mwmanager_luafix_journal.jsonl"),
                            "BACKUP_ROOT": os.path.join(manager_dir, "backups", "luafix"),
                            "PLAN_FILE": os.path.join(manager_dir, "outputs", "mwmanager_luafix_plan.txt")},
    }


def tidy_state_names(manager_dir):
    """vortex_tidy's folders and records for MWManager: {name: path} (set by compose_tidy / use_manager_dir)."""
    out = os.path.join(manager_dir, "outputs")
    return {
        "HERE": manager_dir,
        "OUTPUT_DIR": out,
        "STAGING": os.path.join(manager_dir, "staging", "Data Files"),
        "JOURNAL": os.path.join(out, "mwmanager_vortex_tidy_journal.jsonl"),
        "PLAN": os.path.join(out, "mwmanager_vortex_tidy_plan.txt"),
        "AUDIT_ROOT": os.path.join(manager_dir, "backups", "tidy"),
        "BLACKLIST_FILE": os.path.join(manager_dir, "mwmanager_vortex_tidy_blacklist.txt"),
        "DATA_FILES_PLAN": os.path.join(out, "mwmanager_data_files_tidy_plan.txt"),
        "TIDY_BACKUP_CACHE": os.path.join(out, "mwmanager_tidy_backup_cache.json"),
    }


def hook_upstream_modules():
    """Small wraps of upstream functions outside the scanner namespace (done once by compose())."""
    import mwdialog_mastercheck as mc
    if not getattr(mc.save_cache, "_mwmanager", False):
        upstream_save = mc.save_cache

        def save_cache(path, cache, bases):
            """Upstream's save; paths in the MWManager folder (its fix log) are expected outside the game,
            staging and download folders, so they are not reported."""
            outside = upstream_save(path, cache, bases) or []
            own = os.path.normcase(os.path.abspath(MANAGER_DIR)) + os.sep
            return [p for p in outside if not os.path.normcase(os.path.abspath(p)).startswith(own)]
        save_cache._mwmanager = True
        mc.save_cache = save_cache

globals().update(state_names(MANAGER_DIR))


def manager_file(name):
    return os.path.join(MANAGER_DIR, name)


def output_file(name):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.join(OUTPUT_DIR, name)


# MWManager's own generated plugins and markers
PATCH_NAME = "mwmanager - ESM fixes.esp"
PLACE_PATCH_NAME = "mwmanager PlaceholderCell ESP Patch.esp"
CELL_WINNER_PATCH = "mwmanager Cell Fixes.esp"
PERFORMER_PATCH = "mwmanager Performers Patch.esp"
AUTO_MERGE_NAME = "mwmanager AutoMerge.esp"
MULTI_PATCH_NAME = "mwmanager Multipatch.esp"
OUR_PATCHES = (PATCH_NAME, PLACE_PATCH_NAME, MULTI_PATCH_NAME, CELL_WINNER_PATCH, PERFORMER_PATCH)
_OURS_LOWER = None                              # rebuilt from OUR_PATCHES on first use
XE_SKY_PLUGIN = "XE Sky Variations.esp"
PLACEHOLDER_CELL = "mwmanager placeholders"
EARLY_CELL = "mwmanager early placeholders"
FIX_TAG = "; [mwmanager] "
PROMOTED_START = ";; ==== MWManager content rules promoted to masters (written by mwmanager --sort; don't edit) ===="
# Stand-ins shipped in the MWManager folder
DUMMY_NIF = "mwmanager_dummy.nif"
DUMMY_KF = "mwmanager_dummy.kf"
DUMMY_MARKER_NIF = "mwmanager_dummy_marker.nif"
DUMMY_ANIM_NIF = "mwmanager_dummy_anim.nif"
DUMMY_ICON_DDS = "mwmanager_dummy_icon.dds"
DUMMY_BIK = "mwmanager_dummy.bik"



def backup_file(*parts):
    path = os.path.join(BACKUP_DIR, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def asset_backup_file(category, rel, data_dir=None, plugin=None):
    """Choose a collision-safe Off path beside the owning staged mod when possible."""
    if data_dir:
        path = plugin_off_backup_path(data_dir, rel, category, plugin)
    else:
        path = os.path.join(BACKUP_DIR, "assets", category, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        stem, ext = os.path.splitext(path)
        n = 2
        while os.path.exists(f"{stem}.mwmanager-{n}{ext}"):
            n += 1
        path = f"{stem}.mwmanager-{n}{ext}"
    return path


def read_load_order(args):
    """The active plugins, from mwmanager_sort_order.txt. The first time, it is made from Morrowind.ini."""
    # An explicit list is authoritative for this invocation (notably --init --full).
    if getattr(args, "plugins", None):
        return list(args.plugins)
    game_dir = os.path.dirname(os.path.abspath(args.data_dir))
    path = LOAD_ORDER_PATH
    if os.path.isfile(path):
        out = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith(";"):
                    out.append(s)
        return out
    ini = default_ini(args)
    if not os.path.isfile(ini):
        return None
    names = ini_game_files(ini)
    write_load_order(game_dir, names, "made from Morrowind.ini")
    print(f"Created {LOAD_ORDER_FILE} from Morrowind.ini's [Game Files] ({len(names)} plugins). From now on this "
          f"file is the load order; Morrowind.ini only gets it when --play starts the game.", file=sys.stderr)
    return names


def write_load_order(game_dir, order, note=""):
    """Save the plugin list (the previous one is kept as .bak). Returns the backup's name."""
    LOAD_ORDER_WRITTEN.add(os.path.abspath(game_dir))
    path = LOAD_ORDER_PATH
    bak = backup_file("state", LOAD_ORDER_FILE + ".bak")
    if os.path.isfile(path):
        shutil.copy2(path, bak)
    lines = [f"; {LOAD_ORDER_FILE}: manager-owned working load order, separate from Morrowind.ini.",
             f"; Last changed {time.strftime('%Y-%m-%d %H:%M')}" + (f" by {note}" if note else "") + ". One plugin per line.",
             ""] + list(order)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return os.path.basename(bak)


def off_dest(mod, feature, plugin, rel):
    """Return Off/<plugin>/<feature>/<original path> for a staged mod or deployed asset owner."""
    if plugin:
        owner = os.path.splitext(os.path.basename(plugin))[0]
    else:
        own = sorted(e for e in os.listdir(mod) if e.lower().endswith((".esp", ".esm"))) if os.path.isdir(mod) else []
        owner = os.path.splitext(own[0])[0] if own else \
            re.split(r"-\d+-|\s+\d{4,}\s", os.path.basename(mod.rstrip("\\/")))[0].strip()
    return os.path.join(mod, "Off", owner, feature, rel)


# the Off\\ folder name for a file no plugin owns: shipped assets, or whatever was in Data Files when the user
# kept an existing install
ORIGINAL_INSTALL = "Original Install"


def backup_asset_original(data_dir, rel, plugin=None, feature="repair"):
    """Keep one pre-repair original under Off/<plugin>/<feature>/<original relative path>."""
    shipped = shipped_game_file(data_dir, rel)
    mod, owner_plugin = (None, None) if shipped else asset_owner_folder(data_dir, rel)
    owner = ORIGINAL_INSTALL if shipped else (plugin or owner_plugin or ORIGINAL_INSTALL)
    if owner.lower().endswith((".esp", ".esm", ".omwaddon")):
        owner = os.path.splitext(os.path.basename(owner))[0]
    root = backup_root_for(mod, data_dir, rel)
    off = os.path.join(root, "Off")
    current = os.path.join(data_dir, rel)
    # Reuse an original already retained by an earlier feature/pass; keep the first untouched bytes.
    try:
        owner_dir = os.path.join(off, owner)
        if os.path.isdir(owner_dir):
            for kept_feature in os.listdir(owner_dir):
                feature_dir = os.path.join(owner_dir, kept_feature)
                existing = os.path.join(feature_dir, rel)
                if os.path.exists(existing):
                    return existing
        # Read legacy Off/<feature>/<plugin>/ layouts so a repair never backs up
        # a second copy of the same original during migration.
        for category in os.listdir(off):
            category_dir = os.path.join(off, category)
            if not os.path.isdir(category_dir):
                continue
            for plugin_dir in os.listdir(category_dir):
                if plugin_dir.casefold() == owner.casefold():
                    existing = os.path.join(category_dir, plugin_dir, rel)
                    if os.path.exists(existing):
                        return existing
        legacy = os.path.join(off, owner, rel)
        if os.path.exists(legacy):
            return legacy
    except OSError:
        pass
    dest = off_dest(root, feature, owner, rel)
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(current, dest)
        _ASSET_BACKUP_OF[os.path.normcase(os.path.abspath(current))] = dest
        return dest
    return keep_new_original(dest, current)


def _is_under(path, base):
    path, base = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(base))
    return path == base or path.startswith(base.rstrip("\\/") + os.sep)


# Top-level Data Files folders the game itself ships loose files in (GOG/Steam Morrowind GOTY).
SHIPPED_ASSET_FOLDERS = frozenset({"bookart", "fonts", "icons", "meshes", "music", "sound", "splash",
                                   "textures", "video"})


def shipped_game_file(data_dir, rel, mod=None):
    """True for a file the game itself shipped: the core ESMs/BSAs, the official add-on ESPs, or a loose file
    in a shipped asset folder that no staged mod deployed and MWManager didn't generate."""
    if mod:
        return False
    rel = str(rel).replace("/", os.sep).replace("\\", os.sep)
    name = os.path.basename(rel).casefold()
    if name in {n.casefold() for n in (*CORE_GAME_ESMS, *OFFICIAL_BASE_GAME_ESPS, *CORE_GAME_BSAS)}:
        return True
    parts = [p for p in rel.split(os.sep) if p]
    if (len(parts) < 2 or parts[0].casefold() not in SHIPPED_ASSET_FOLDERS or "mwmanager" in name
            or name.endswith((".esp", ".esm", ".omwaddon", ".bsa"))):
        return False
    generated = read_generated_record(output_file(GENERATED_RECORD))
    return not ({rel.lower(), rel.replace(os.sep, "\\").lower(), rel.replace(os.sep, "/").lower()} & set(generated))


def backup_root_for(mod, data_dir, rel=None):
    """Where Off/ backups go: the owning staged mod; Data Files itself for the game's own shipped files (the
    game install's Off folder is for those); otherwise MWManager's own backups folder."""
    if mod:
        return mod
    if data_dir and _is_under(data_dir, MANAGER_DIR):
        return data_dir
    if data_dir and rel is not None and shipped_game_file(data_dir, rel):
        return data_dir
    return os.path.join(BACKUP_DIR, "Data Files")


def plugin_off_backup_path(data_dir, rel, feature, plugin=None, unique=True):
    """Return <mod>/Off/<plugin>/<feature>/<original path>, using the Vortex hard link owner."""
    shipped = shipped_game_file(data_dir, rel)
    mod, owner_plugin = (None, None) if shipped else asset_owner_folder(data_dir, rel)
    owner = ORIGINAL_INSTALL if shipped else (plugin or owner_plugin or ORIGINAL_INSTALL)
    if owner.lower().endswith((".esp", ".esm", ".omwaddon")):
        owner = os.path.splitext(owner)[0]
    root = backup_root_for(mod, data_dir, rel)
    path = os.path.join(root, "Off", owner, str(feature), rel)
    if unique and os.path.exists(path):
        stem, ext = os.path.splitext(path)
        n = 2
        while os.path.exists(f"{stem}.mwmanager-{n}{ext}"):
            n += 1
        path = f"{stem}.mwmanager-{n}{ext}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def reduce_textures(data_dir, game_dir, items, log):
    """Shrink each texture to TEXTURE_MAX_EDGE by dropping its largest mipmap levels. A Vortex-deployed
    file (a hard link) is replaced by a new file, so the staging folder keeps the full-size one; any
    other file is moved to mwmanager_fullsize_textures first. Recorded for --restore-textures."""
    done, saved = [], 0
    rec_path = output_file(REDUCED_RECORD)
    for full, _rel, _w, _h, size in items:
        rel = os.path.relpath(full, data_dir)        # under Data Files (Textures\\...), as the record keeps it
        try:
            with open(full, "rb") as f:
                data = f.read()
            new, dims = dds_reduce(data)
            if new is None:
                continue
            links = hard_link_names(full) if os.stat(full).st_nlink > 1 else []
            if links:
                kind, keep = "link", links[0]
            else:
                kind, keep = "backup", asset_backup_file("texture-reduction", rel, data_dir=data_dir)
                os.makedirs(os.path.dirname(keep), exist_ok=True)
                shutil.move(full, keep)
            tmp = full + ".mwdtmp"
            with open(tmp, "wb") as f:
                f.write(new)
            os.replace(tmp, full)                    # a new file: a hard link's other copy is untouched
            done.append((rel, kind, keep))
            saved += len(data) - len(new)
            log.append(("Data Files", f"texture '{rel}' was {dims[0]}x{dims[1]}",
                        f"reduced to {dims[2]}x{dims[3]} (original kept in {'Vortex staging' if kind == 'link' else FULLSIZE_DIR})"))
        except (OSError, struct.error) as e:
            log.append(("Data Files", f"texture '{rel}'", f"NOT reduced ({e})"))
    if done:
        new_rec = not os.path.exists(rec_path)
        with open(rec_path, "a", encoding="utf-8") as f:
            if new_rec:
                f.write("; Textures mwmanager.py --fix reduced to 2048. One per line: path under Data Files,\n"
                        "; 'link' (the original is the Vortex staging file named) or 'backup' (moved there).\n"
                        "; --restore-textures puts the originals back.\n")
            bases = record_bases(game_dir)
            for rel, kind, keep in done:
                f.write(f"{rel}\t{kind}\t{MP.store_path(keep, bases)}\n")
    return len(done), saved


def stand_in_bytes(rel):
    ext = os.path.splitext(rel)[1].lower()
    if ext == ".kf":                        # an animation: the game's smallest one (a few frames, harmless)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), DUMMY_KF), "rb") as f:
            return f.read()
    if ext == ".nif":                       # a missing model: a small coin, not the engine's huge "!" marker
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), DUMMY_NIF), "rb") as f:
            return f.read()
    rel_windows = rel.replace("/", "\\")
    if ext == ".bik":
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), DUMMY_BIK), "rb") as f:
            return f.read()
    if ext == ".dds" and rel_windows.lower().startswith("icons\\"):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), DUMMY_ICON_DDS), "rb") as f:
            return f.read()
    if ext == ".dds":
        return placeholder_dds(rel)
    return silent_audio(ext or ".wav")


def silent_audio(ext):
    """A short silent sound: a WAV (22,050 Hz mono, 16-bit, 0.2 s) or about two seconds of silent MP3."""
    if ext.lower() == ".mp3":
        frame = b"\xff\xfb\x90\x64" + bytes(413)          # 128 kbit/s, 44.1 kHz: 417-byte frames
        return frame * 77                              # 77 x 1,152 samples at 44.1 kHz ~= 2.01 seconds
    rate, n = 22050, 4410
    data = bytes(2 * n)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data)


def write_generated(data_dir, game_dir, items, log):
    """Create stand-ins and record them for both --remove-generated and install rollback."""
    rec_path = output_file(GENERATED_RECORD)
    made = []
    owner = {}
    pending = []
    for item in items:
        rel, _why, source = (item + (None,))[:3]
        full = os.path.join(data_dir, rel)
        if not os.path.exists(full) and not (source and os.sep + ".orphaned" + os.sep in source.lower()):
            pending.append((rel, full))
    install_ledger = ledger_run = None
    if pending:
        import mwmanager_journal
        install_ledger = mwmanager_journal.Journal(Path(MANAGER_DIR) / "journal")
        if not install_ledger.has_baseline():
            raise RuntimeError("generated files require an install baseline so rollback can remove them")
        baseline = {mwmanager_journal.key(rel) for rel in install_ledger.baseline().get("files", {})}
        for _rel, full in pending:
            game_rel = mwmanager_journal.rel_of(Path(game_dir), Path(full))
            if mwmanager_journal.key(game_rel) in baseline:
                raise RuntimeError(f"refusing to replace missing baseline file with a generated stand-in: {game_rel}")
        ledger_run = install_ledger.start_run("mwmanager.py", {"operation": "generate missing assets"})
    for item in items:
        rel, why, source = (item + (None,))[:3]
        owner[rel.lower()] = item[3] if len(item) > 3 and item[3] else ""
        full = os.path.join(data_dir, rel)
        if os.path.exists(full):
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if source and os.sep + ".orphaned" + os.sep in source.lower():
            os.replace(source, full)                       # a move back, not a new file: not recorded
            log.append(("Data Files", f"'{rel}' ({why}) had been moved to .Orphaned", "moved it back"))
            continue
        if source and os.path.basename(source).lower().startswith("mwmanager_dummy"):
            shutil.copy2(source, full)                     # a stand-in model under the name the record expects
            log.append(("Data Files", f"'{rel}' is needed by {why} but doesn't exist",
                        f"put a copy of {os.path.basename(source)} there (the plugin is unchanged)"))
        elif source:
            shutil.copy2(source, full)
            log.append(("Data Files", f"'{rel}' ({why}) wasn't deployed", f"copied it from {source}"))
        else:
            with open(full, "wb") as f:
                f.write(stand_in_bytes(rel))
            what = ("a placeholder model (coin)" if rel.lower().endswith(".nif") else
                    "a placeholder icon" if rel.lower().replace("/", "\\").startswith("icons\\") else
                    "a placeholder texture" if rel.lower().endswith(".dds") else "a silent stand-in")
            log.append(("Data Files", f"'{rel}' is needed by {why} but doesn't exist", f"created {what}"))
        made.append(rel)
    if made:
        import mwmanager_journal
        for rel in made:
            full = Path(data_dir) / rel
            install_ledger.record(
                ledger_run["run"], mwmanager_journal.rel_of(Path(game_dir), full), "create",
                mwmanager_journal.sha256_file(full), {"kind": "absent"}, tag="generated-asset",
                mod=owner.get(rel.lower()) or None,
            )
        install_ledger.finish_run(ledger_run)
        entries = read_generated_record(rec_path)          # earlier runs' files, with the plugin each was for
        for rel in made:
            try:
                size = str(os.path.getsize(os.path.join(data_dir, rel)))
            except OSError:
                size = ""
            entries[rel.lower()] = (rel, size, owner.get(rel.lower(), ""))
        write_generated_record(rec_path, entries)
    return made


def backup_path(path):
    """Choose <mod>/Off/<plugin>/plugin-repair/<original path>.bak for a rewritten plugin."""
    parent = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)
    if shipped_game_file(parent, name):
        base = backup_file("plugins", "Official Add-Ons", name + ".bak")
    else:
        base = plugin_off_backup_path(parent, name, "plugin-repair", plugin=name, unique=False) + ".bak"
    os.makedirs(os.path.dirname(base), exist_ok=True)
    cand, n = base, 2
    while os.path.exists(cand):
        cand = f"{base}{n}"
        n += 1
    return cand


def backup_order(path):
    """Chronological backup order, with numbered sidecars as a deterministic tie-breaker."""
    m = re.search(r"\.bak(\d*)$", path, re.I)
    ordinal = int(m.group(1) or 1) if m else 0
    try:
        stamp = os.path.getctime(path) if os.name == "nt" else os.path.getmtime(path)
    except OSError:
        stamp = 0
    return stamp, ordinal


def backup_candidates(path):
    """Off-tree plugin backups, older manager backups, and legacy sidecars, oldest first."""
    name = os.path.basename(path)
    central = glob.glob(os.path.join(BACKUP_DIR, "plugins", "**", name + ".bak*"), recursive=True)
    parent = os.path.dirname(os.path.abspath(path))
    owner_root, _owner = asset_owner_folder(parent, name)
    off = []
    # backup_path() writes Off/<plugin>/plugin-repair/...; older runs used Off/plugin-repair/... and the game's
    # Data Files\Off (shipped files' backups, and older runs' others) or MWManager\backups\Data Files
    roots = [backup_root_for(owner_root, parent, name), owner_root or parent]
    if not owner_root:
        roots.append(os.path.join(BACKUP_DIR, "Data Files"))
    for off_root in dict.fromkeys(roots):
        off += glob.glob(os.path.join(off_root, "Off", "**", name + ".bak*"), recursive=True)
    legacy = [path + ".bak"] + [f"{path}.bak{n}" for n in range(2, 100)]
    paths = [p for p in central + off + legacy if os.path.isfile(p)]
    return sorted(set(paths), key=backup_order)


def run_clean_up(args):
    """Delete backups of plugins that are gone, and keep only the most recent backup of the rest."""
    data_dir = args.data_dir
    lookup = index_dir(data_dir)
    groups = defaultdict(list)
    candidates = [p for p in glob.glob(os.path.join(BACKUP_DIR, "plugins", "**", "*"), recursive=True)
                  if os.path.isfile(p)]
    candidates += [bak for plugin_path in lookup.values() for bak in backup_candidates(plugin_path)]
    # Older MWManager releases left backups beside plugins; recognize those until the user cleans them up.
    candidates += [p for p in lookup.values() if os.path.isfile(p)]
    for path in set(candidates):
        m = CLEANUP_BAK_RE.match(os.path.basename(path).lower())
        if m:
            groups[m.group("plugin")].append((int(m.group("n") or 1), path))
    if not groups:
        print("No plugin backups found.")
        return 0
    gone, old = [], []
    kept = {}
    for plugin, baks in groups.items():
        baks.sort(key=lambda item: backup_order(item[1]))
        if plugin not in lookup:
            gone += [p for _n, p in baks]
        else:
            kept[plugin] = baks[-1][1]
            old += [p for _n, p in baks[:-1]]
    size = lambda paths: sum(os.path.getsize(p) for p in paths)
    print(f"{sum(len(b) for b in groups.values())} backup(s) of {len(groups)} plugin(s) in {BACKUP_DIR}")
    if gone:
        print(f"\n  Plugin no longer in Data Files, so all its backups go ({len(gone)}, {size(gone) / 1048576:.1f} MB):")
        for p in sorted(gone, key=str.lower):
            print(f"    - {os.path.basename(p)}")
    if old:
        print(f"\n  Older backups of plugins that are still here ({len(old)}, {size(old) / 1048576:.1f} MB); "
              "the most recent one is kept here:")
        by_plugin = defaultdict(list)
        for p in old:
            by_plugin[CLEANUP_BAK_RE.match(os.path.basename(p).lower()).group("plugin")].append(p)
        for plugin in sorted(by_plugin):
            print(f"    - {plugin}: {len(by_plugin[plugin])} older, keeping {os.path.basename(kept[plugin])}")
    doomed = gone + old
    if not doomed:
        print("  Nothing to clean up: each plugin has a single backup.")
        return 0
    if args.dry_run:
        print("\nDry run: no files were deleted.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("\nNot running interactively; add --yes to delete them.")
            return 1
        if input(f"\nDelete {len(doomed)} backup file(s)? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing deleted.")
            return 0
    freed, done, lines = 0, 0, []
    for path in doomed:
        try:
            n = os.path.getsize(path)
            os.remove(path)
            freed += n
            done += 1
            lines.append(f"deleted backup {os.path.basename(path)}")
        except OSError as e:
            print(f"  {os.path.basename(p)}: NOT deleted ({e})")
    with MT.open_log(args.fix_log, "a", encoding="utf-8") as f:
        import datetime
        f.write(f"\n==== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} clean-up ====\n")
        f.write("".join(l + "\n" for l in lines))
    print(f"\nDeleted {done} backup file(s), {freed / 1048576:.1f} MB freed. Logged to {args.fix_log}.")
    return 0


def restore_all(data_dir, assume_yes, fix_log):
    """Put every plugin back to its OLDEST backup (the original) and remove all its backups."""
    lookup = index_dir(data_dir)
    groups = defaultdict(list)                     # plugin path -> [(n, backup path)]
    candidates = [p for p in glob.glob(os.path.join(BACKUP_DIR, "plugins", "**", "*"), recursive=True)
                  if os.path.isfile(p)] + [p for p in lookup.values() if os.path.isfile(p)]
    candidates += [bak for plugin_path in lookup.values() for bak in backup_candidates(plugin_path)]
    for bak in set(candidates):
        m = BAK_RE.match(os.path.basename(bak).lower())
        if m and m.group("plugin") in lookup and m.group("plugin") not in [x.lower() for x in OUR_PATCHES]:
            groups[lookup[m.group("plugin")]].append((int(m.group("n") or 1), bak))
    if not groups:
        print("No plugin backups found.")
        return 0
    print("These plugins will be put back to their original (oldest) backup, and all their backups removed:")
    for path, baks in sorted(groups.items(), key=lambda kv: kv[0].lower()):
        baks.sort(key=lambda item: backup_order(item[1]))
        print(f"  {os.path.basename(path)}  <- {os.path.basename(baks[0][1])}"
              + (f"  (also removing {len(baks) - 1} newer backup(s))" if len(baks) > 1 else ""))
    patch = lookup.get(PATCH_NAME.lower())
    if patch:
        print(f"\n'{PATCH_NAME}' is left as it is; the next --fix rebuilds it from scratch.")
    if not assume_yes:
        if not sys.stdin.isatty():
            print("\nNot running interactively; add --yes to restore.")
            return 1
        if input(f"\nRestore {len(groups)} plugin(s)? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 0
    done, lines = 0, []
    for path, baks in groups.items():
        oldest = baks[0][1]
        try:
            st = os.stat(oldest)                   # copy2 kept the plugin's original date on it
            with open(oldest, "rb") as f:
                data = f.read()
            safe_write_bytes(path, data, keep_times=st)     # in place, so hard links (Vortex) stay intact
            with open(path, "rb") as f:
                if f.read() != data:
                    raise OSError("the restored copy doesn't match the backup; backups kept")
            for _n, b in baks:
                os.remove(b)
            done += 1
            lines.append(f"{os.path.basename(path)}: restored from {os.path.basename(oldest)}, {len(baks)} backup(s) removed")
        except OSError as e:
            print(f"  {os.path.basename(path)}: NOT restored ({e})")
    with MT.open_log(fix_log, "a", encoding="utf-8") as f:
        import datetime
        f.write(f"\n==== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} restore-all ====\n")
        f.write("".join(l + "\n" for l in lines))
    print(f"Restored {done} plugin(s) to their originals.")
    return 0


def disabled_plugin_path(data_dir, name):
    """Where a disabled ESP/ESM is kept: MWManager's own folder, so nothing extra stays in the game install."""
    return os.path.join(DISABLED_PLUGINS_DIR, os.path.basename(name))


def legacy_disabled_plugin_path(data_dir, name):
    """Where older MWManager versions kept disabled plugins (read only, so they can still be re-enabled)."""
    return os.path.join(os.path.abspath(data_dir), "Off", "Disabled Plugins", os.path.basename(name))


def find_disabled_plugin(data_dir, name):
    for path in (disabled_plugin_path(data_dir, name), legacy_disabled_plugin_path(data_dir, name)):
        if os.path.isfile(path):
            return path
    return None


def move_plugins_to_disabled(data_dir, names):
    """Move selected plugin files out of Data Files, refusing to overwrite an earlier disabled copy."""
    data_dir = os.path.abspath(data_dir)
    moved = []
    try:
        for name in names:
            filename = os.path.basename(name)
            source = os.path.join(data_dir, filename)
            if not os.path.isfile(source):
                continue
            target = disabled_plugin_path(data_dir, filename)
            earlier = find_disabled_plugin(data_dir, filename)
            if earlier:
                raise FileExistsError(f"A disabled copy already exists: {earlier}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(source, target)
            moved.append((source, target))
    except OSError:
        for source, target in reversed(moved):
            try:
                if os.path.isfile(target) and not os.path.exists(source):
                    os.replace(target, source)
            except OSError:
                pass
        raise
    return moved


def restore_disabled_plugins(data_dir, names):
    """Restore disabled plugin files to Data Files, refusing to overwrite a live plugin."""
    data_dir = os.path.abspath(data_dir)
    restored = []
    try:
        for name in names:
            filename = os.path.basename(name)
            source = find_disabled_plugin(data_dir, filename)
            target = os.path.join(data_dir, filename)
            if not source:
                continue
            if os.path.exists(target):
                raise FileExistsError(f"Cannot re-enable {filename}; a file already exists at {target}")
            os.replace(source, target)
            restored.append((source, target))
    except OSError:
        for source, target in reversed(restored):
            try:
                if os.path.isfile(target) and not os.path.exists(source):
                    os.makedirs(os.path.dirname(source), exist_ok=True)
                    os.replace(target, source)
            except OSError:
                pass
        raise
    return restored


CONFLICT_BLACKLIST_DEFAULT = """; mwmanager_conflict_blacklist.txt - plugins that never take part in cell conflicts and
; always lose: they are left out of --cell-conflicts (no report, no question), and --sort loads each one
; as early as its masters allow (.esm among the masters, .esp right after the .esm files), so every
; other mod's version wins, and mwcollide hides its object where two sit on top of each other.
; One plugin per line (wildcards * ? allowed). Don't list a compatibility patch: it must load after the
; mods it patches.
OAAB_Saplings.esm
"""
DEFAULT_RULES = f"""; {os.path.basename(RULES_FILE)}: load order rules for mwmanager.py --sort
;
; The same format as mlox rules (mlox_user.txt / mlox_base.txt). Rules here win over mlox's own:
; files are read in this order, and a rule that contradicts an earlier one is skipped.
;
;   [Order]          each plugin listed loads before the next one
;   [NearStart]      these plugins load as early as their masters allow (in this order)
;   [NearEnd]        these plugins load as late as possible; the FIRST one listed loads LAST
;                    (mlox's convention, so the patch that must win goes first)
;   [Exact]          these plugins load one right after the other, in exactly this order, with
;                    nothing in between (exact names, no wildcards). Not applied, with a note, if
;                    masters make it impossible.
;
; One plugin per line; ";" starts a comment. Names are not case-sensitive and may use
; the wildcards * and ? and mlox's <VER> (a version number). A plugin always loads after its
; masters, and every .esm before every .esp, whatever the rules say.

[NearStart]
{PATCH_NAME}

[NearEnd]
{PERFORMER_PATCH}
{PLACE_PATCH_NAME}
{MULTI_PATCH_NAME}
"""
CORE_GAME_ESMS = ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm")
# Shipped standalone plugins from the Morrowind GOTY Data Files folder. Treat them as normal plugins for
# MWManager's repair pass even though they do not live in a Vortex staging folder.
OFFICIAL_BASE_GAME_ESPS = ("adamantiumarmor.esp", "AreaEffectArrows.esp", "bcsounds.esp", "EBQ_Artifact.esp",
                           "entertainers.esp", "LeFemmArmor.esp", "master_index.esp", "Siege at Firemoth.esp")
CORE_GAME_BSAS = ("Morrowind.bsa", "Tribunal.bsa", "Bloodmoon.bsa")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
