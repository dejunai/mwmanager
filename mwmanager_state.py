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
import errno
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

import mwdialog_offlayout as OL

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
        "AUTO_MERGE_BLACKLIST": own("mwmanager_auto-merge.blacklist.txt"),
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
        "DOOR_OWNERS_LIST": os.path.join(out, "mwmanager_door_owners.txt"),   # per --fix: owners changed / left
        "DOUBLED_DELETED_FILE": os.path.join(out, "mwmanager_doubled_deleted.txt"),
        "PATHGRID_FIXES_FILE": os.path.join(out, "mwmanager_pathgrid_fixes.txt"),
        "PATHGRID_CACHE_FILE": os.path.join(out, "mwmanager_pathgrid_cache.json"),
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
        "mwdialog_backup": {"ROOT": own("backups")},
        "mwdialog_mastercheck": {"CACHE_FILE": own("mwmanager_mastercheck_cache.json"),
                                 "SIZES_RECORD": own("mwmanager_master_sizes.json"),
                                 "FACTS_FILE": own("mwmanager_mastercheck_facts.json")},
        "mwdialog_longpaths": {"RECORD": os.path.join(manager_dir, "outputs", "mwmanager_longpaths.json")},
        "mwdialog_assets": {"CACHE_DB": os.path.join(manager_dir, "outputs", "mwmanager_asset_cache.sqlite"),
                            "CACHE_FILE": os.path.join(manager_dir, "outputs", "mwmanager_asset_cache.json"),
                            "UNUSED_RECORD": os.path.join(manager_dir, "outputs", "mwmanager_unused_assets.txt"),
                            "MIP_EXEMPT_RECORD": own("mwmanager_mip_exempt.txt")},
        "mwdialog_luafix": {"JOURNAL": os.path.join(manager_dir, "outputs", "mwmanager_luafix_journal.jsonl"),
                            "BACKUP_ROOT": os.path.join(manager_dir, "backups", "luafix"),
                            "PLAN_FILE": os.path.join(manager_dir, "outputs", "mwmanager_luafix_plan.txt")},
        "mwdialog_cellmods": {"OBJECT_CACHE_FILE": os.path.join(manager_dir, "outputs", "mwmanager_object_cache.json")},
        "mwdialog_mergeplan": {"PLAN_TXT": os.path.join(manager_dir, "outputs", "mwmanager_merge_plan.txt"),
                               "PLAN_JSON": os.path.join(manager_dir, "outputs", "mwmanager_merge_plan.json")},
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


def asset_limit(key):
    """mwmanager.ini [Staging] reduce_textures_2048 / reduce_splash_2048 (set in Options; default true)."""
    import configparser
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read(manager_file("mwmanager.ini"), encoding="utf-8")
        return ini.getboolean("Staging", key, fallback=True)
    except (OSError, ValueError, configparser.Error):
        return True


if not asset_limit("reduce_textures_2048"):
    TEXTURE_MAX_EDGE = 1 << 30          # no texture is reduced; spread to the upstream modules by the layer loader


def output_file(name):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.join(OUTPUT_DIR, name)


def cell_fixes_report_path(_game_dir):
    return output_file("mwmanager_cell_fixes.txt")


# Newer upstream files go through mwdialog_paths.state_path, which (outside conflict-tools\) means the game
# folder. MWManager keeps them out of the game install, with its own records and reports. The older upstream
# records already go through MWManager's own readers and writers, so only these names are routed.
_STATE_ROUTES = {"mwdialog_soundfix_report.txt": output_file, "mwdialog_patch_inputs.json": manager_file}
if not getattr(MP.state_path, "mwmanager_routed", False):
    _upstream_state_path = MP.state_path

    def _routed_state_path(base, name):
        route = None if os.path.isabs(name) else _STATE_ROUTES.get(name.lower())
        return route(name) if route else _upstream_state_path(base, name)

    _routed_state_path.mwmanager_routed = True
    MP.state_path = _routed_state_path


def doubled_rules_path(_game_dir):
    return manager_file(RULES_FILE)


def dates_undo_path(_game_dir):
    return manager_file(DATES_UNDO_FILE)


# MWManager's own generated plugins and markers
PATCH_NAME = "mwmanager - ESM fixes.esp"
PLACE_PATCH_NAME = "mwdialog_check PlaceholderCell ESP Patch.esp"
CELL_WINNER_PATCH = "mwdialog_check Cell Fixes.esp"
PERFORMER_PATCH = "mwmanager Performers Patch.esp"
AUTO_MERGE_NAME = "mwdialog_check AutoMerge.esp"
MULTI_PATCH_NAME = "mwdialog_check Multipatch.esp"
OUR_PATCHES = (PATCH_NAME, PLACE_PATCH_NAME, MULTI_PATCH_NAME, CELL_WINNER_PATCH, PERFORMER_PATCH)
_OURS_LOWER = None                              # rebuilt from OUR_PATCHES on first use
XE_SKY_PLUGIN = "XE Sky Variations.esp"
ORDER_TAIL_EXTRA = (XE_SKY_PLUGIN,)
CELL_FIXES_REPORT = "mwmanager_cell_fixes.txt"
CELL_PATCH_AUTHOR = "mwmanager"
CELL_PATCH_DESCRIPTION = "Rebuilt interiors: the chosen mod's version wins (rebuilt by mwmanager --fix)"
DUMMY_SOURCE_PREFIX = "mwmanager_dummy"
TEXT_KEY_BACKUP_FEATURE = "fixed text keys"
PLACE_PATCH_DESCRIPTION = "generated by mwmanager.py. Load this LAST."
REPAIR_SOUND_BACKUP_FEATURE = "converted sounds"
PLACEHOLDER_CELL = "mwmanager placeholders"
EARLY_CELL = "mwmanager early placeholders"
FIX_TAG = "; [mwmanager] "
PROMOTED_START = ";; ==== MWManager content rules promoted to masters (written by mwmanager --sort; don't edit) ===="
# Stand-ins: the game's own files, never shipped. Upstream's dummy_path copies each out of the player's
# Morrowind.bsa into "dummy assets" the first time it is needed (icons and textures are made in code)
DUMMY_NIF = "mwmanager_dummy.nif"
DUMMY_KF = "mwmanager_dummy.kf"
DUMMY_MARKER_NIF = "mwmanager_dummy_marker.nif"
DUMMY_ANIM_NIF = "mwmanager_dummy_anim.nif"
DUMMY_SOURCES = {DUMMY_NIF: "meshes\\m\\misc_dwrv_coin00.nif", DUMMY_KF: "meshes\\i\\xin_dagoth_bridge00.kf",
                 DUMMY_MARKER_NIF: "meshes\\editormarker.nif", DUMMY_ANIM_NIF: "meshes\\base_anim.nif"}

_upstream_compute_order = compute_order


def compute_order(plugins, rule_sets, current_order):
    """Keep the generated ESP tail deterministic; absent files are simply omitted."""
    order, notes = _upstream_compute_order(plugins, rule_sets, current_order)
    by_name = {name.casefold(): name for name in order}
    tail = [by_name[name.casefold()] for name in (
        XE_SKY_PLUGIN, AUTO_MERGE_NAME, CELL_WINNER_PATCH, PLACE_PATCH_NAME, MULTI_PATCH_NAME
    ) if name.casefold() in by_name]
    if tail:
        order = [name for name in order if name not in tail] + tail
    return order, notes



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
    if getattr(args, "dry_run", False):              # a dry run changes nothing: Morrowind.ini's list, not saved
        return names
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
    with atomic_open(path, encoding="utf-8") as f:          # a crash mid-write never empties it
        f.write("\n".join(lines) + "\n")
    return os.path.basename(bak)


def _off_reason(feature):
    """The canonical Off reason for `feature`, or the name itself when it is not in the layout list."""
    try:
        return OL.reason_for(feature)
    except ValueError:
        # Conflict Tools accepted this name; the next sync adds it to REASONS and LEGACY.
        if (feature or "").strip().lower() in {"texture-reduction", "reduced textures"}:
            return "reduced textures"
        return feature


# Older reason-folder spellings still read until (and after) they appear in mwdialog_offlayout.LEGACY.
_LEGACY_REASON_FOLDERS = {
    "reduced textures": ("texture-reduction",),
    "executables": ("Executables",),
    "redundant splash sources": ("Splash",),
    "duplicate omwaddon": ("OpenMW-addon-duplicate",),
    "packaging metadata": ("Packaging Metadata",),
}


def _existing_off_copy(root, owner, rel, reason):
    """An original already kept for `rel` under this reason or an older spelling of it, or None."""
    reason = _off_reason(reason)
    for name in (reason,) + _LEGACY_REASON_FOLDERS.get(reason, ()):
        path = os.path.join(root, "Off", owner, name, rel)
        if os.path.exists(path):
            return path
    return None


# Data Files\\Off uses these fixed owner names; never let owner_name scan Data Files for a plugin.
DATA_FILES_OFF_OWNERS = frozenset({
    "found at setup", "original install", "official add-ons", "disabled plugins",
})


def _looks_like_data_files(path):
    """True for a Data Files folder (or the manager's Data Files backup root): Off there is not a staged mod's."""
    if not path:
        return False
    base = os.path.basename(str(path).rstrip("\\/")).casefold()
    return base == "data files" or os.path.isfile(os.path.join(path, "Morrowind.esm"))


def off_dest(mod, feature, plugin, rel):
    """Return Off/<plugin>/<reason>/<original path> for a staged mod or a Data Files Off owner."""
    reason = _off_reason(feature)
    hint = os.path.splitext(os.path.basename(plugin))[0] if plugin else None
    if hint and hint.casefold() in DATA_FILES_OFF_OWNERS:
        return os.path.join(mod, "Off", hint, reason, rel) if rel else os.path.join(mod, "Off", hint, reason)
    if _looks_like_data_files(mod):
        owner = hint or "Found at Setup"
        return os.path.join(mod, "Off", owner, reason, rel) if rel else os.path.join(mod, "Off", owner, reason)
    fallback = re.split(r"-\d+-|\s+\d{4,}\s", os.path.basename(str(mod).rstrip("\\/")))[0].strip()
    return OL.off_path(mod, reason, rel or "", hint=plugin, fallback=fallback)


# the Off\\ folder name for a file no plugin owns: shipped assets, or whatever was in Data Files when the user
# kept an existing install
ORIGINAL_INSTALL = "Found at Setup"
LEGACY_ORIGINAL_INSTALL = "Original Install"     # its earlier name; files kept there are still found


def backup_asset_original(data_dir, rel, plugin=None, feature="repaired originals"):
    """Keep one pre-repair original under Off/<plugin>/<reason>/<original relative path>."""
    # the owner first: a file a staged mod deployed (a hard link) is that mod's, whatever folder it is in
    mod, owner_plugin = asset_owner_folder(data_dir, rel)
    shipped = shipped_game_file(data_dir, rel, mod)
    owner = ORIGINAL_INSTALL if shipped else (plugin or owner_plugin or ORIGINAL_INSTALL)
    if owner.lower().endswith((".esp", ".esm", ".omwaddon")):
        owner = os.path.splitext(os.path.basename(owner))[0]
    root = backup_root_for(mod, data_dir, rel)
    off = os.path.join(root, "Off")
    current = os.path.join(data_dir, rel)
    # An original retained by an earlier feature/pass stays the first; an updated file is kept beside it.
    kept = (OL.find_original(root, rel) if mod and not _looks_like_data_files(root)
            else _kept_original(off, owner, rel))
    if kept:
        return keep_new_original(kept, current)
    dest = off_dest(root, _off_reason(feature), owner, rel)
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(current, dest)
        _ASSET_BACKUP_OF[os.path.normcase(os.path.abspath(current))] = dest
        return dest
    return keep_new_original(dest, current)


def _kept_original(off, owner, rel):
    """The original an earlier feature, pass or layout kept of `rel`, or None."""
    try:
        for owner_name in [owner] + ([LEGACY_ORIGINAL_INSTALL] if owner == ORIGINAL_INSTALL else []):
            owner_dir = os.path.join(off, owner_name)
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
    return None


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
    """Return <mod>/Off/<plugin>/<feature>/<original path>, using the hard link owner. A file no mod deployed
    (in Data Files when MWManager first set up the game) goes under Off/Found at Setup/."""
    mod, owner_plugin = asset_owner_folder(data_dir, rel)
    shipped = shipped_game_file(data_dir, rel, mod)
    owner = ORIGINAL_INSTALL if shipped else (plugin or owner_plugin or ORIGINAL_INSTALL)
    if owner.lower().endswith((".esp", ".esm", ".omwaddon")):
        owner = os.path.splitext(owner)[0]
    root = backup_root_for(mod, data_dir, rel)
    path = off_dest(root, feature, owner, rel)
    if not os.path.exists(path):
        kept = _existing_off_copy(root, owner, rel, feature)
        if kept:
            path = kept
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
                kind, keep = "backup", asset_backup_file("reduced textures", rel, data_dir=data_dir)
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


def silent_audio(ext):
    """A short silent sound: a WAV (22,050 Hz mono, 16-bit, 0.2 s) or about two seconds of silent MP3."""
    if ext.lower() == ".mp3":
        frame = b"\xff\xfb\x90\x64" + bytes(413)          # 128 kbit/s, 44.1 kHz: 417-byte frames
        return frame * 77                              # 77 x 1,152 samples at 44.1 kHz ~= 2.01 seconds
    rate, n = 22050, 4410
    data = bytes(2 * n)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data)


def _in_unused_assets(path):
    """True for a file under an Off folder's "unused assets" reason folder, in any spelling the layout has used
    (Off\\<plugin>\\unused assets\\, Off\\<plugin>\\Unused assets\\, Off\\unused\\...)."""
    parts = path.casefold().split(os.sep)
    if "off" not in parts:
        return False
    for part in parts[parts.index("off") + 1:-1]:
        try:
            if OL.reason_for(part) == "unused assets":
                return True
        except ValueError:
            continue
    return False


def _kept_asset_finder(data_dir):
    """find(rel) -> a real copy of Data Files\\<rel> kept somewhere MWManager put files: Data Files\\Off (unused,
    replaced or packed originals) or MWManager's staging (mods' own folders). Unused-asset originals come first,
    then the rest of Off, then staging. Matched case-insensitively on the path's tail; indexed on first use."""
    roots = [os.path.join(data_dir, "Off"), os.path.join(MANAGER_DIR, "staging")]
    index = None

    def build():
        found = {}
        for rank, root in enumerate(roots):
            for base, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if not d.startswith(".mwmanager-pack-work")]
                for name in files:
                    path = os.path.join(base, name)
                    unused = _in_unused_assets(path)
                    found.setdefault(name.casefold(), []).append((0 if unused else rank + 1, path))
        return found

    def find(rel):
        nonlocal index
        if index is None:
            index = build()
        tail = os.sep + os.path.normpath(rel).casefold()
        hits = sorted(c for c in index.get(os.path.basename(rel).casefold(), [])
                      if c[1].casefold().endswith(tail))
        return hits[0] if hits else None
    return find


def _journal_moves_back(game_dir, moves):
    """Originals moved back from Data Files\\Off into place: both paths go into the install ledger, so the
    deployed state that verify expects has the file in place and its Off copy gone. (Rollback needs nothing
    more: the file in place is the original the earlier move kept.)"""
    if not moves:
        return
    import mwmanager_journal
    ledger = mwmanager_journal.Journal(Path(MANAGER_DIR) / "journal")
    if not ledger.has_baseline():
        return
    game = Path(game_dir).resolve()
    run = ledger.start_run("mwmanager.py", {"operation": "move originals back from Off"})
    try:
        for source, target in moves:
            target = Path(target).resolve()
            try:
                source_rel = mwmanager_journal.rel_of(game, Path(source).resolve())
                target_rel = mwmanager_journal.rel_of(game, target)
            except ValueError:
                continue
            ledger.record(run["run"], source_rel, "move", None, None, tag="asset-moved-back")
            ledger.record(run["run"], target_rel, "create", mwmanager_journal.sha256_file(target), None,
                          tag="asset-moved-back")
    finally:
        ledger.finish_run(run)


def write_generated(data_dir, game_dir, items, log):
    """Put missing files in place: the real file kept in Off or staging when there is one, else a stand-in.
    Records them for both --remove-generated and install rollback; never stops --full."""
    rec_path = output_file(GENERATED_RECORD)
    made = []
    owner = {}
    pending = []
    find = _kept_asset_finder(data_dir)
    resolved = []
    moved_back = []
    for item in items:
        rel, why, source = (item + (None,))[:3]
        if not source and not os.path.exists(os.path.join(data_dir, rel)):
            hit = find(rel)
            if hit and hit[0] == 0:                          # an original the unused-asset purge moved aside
                full = os.path.join(data_dir, rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                os.replace(hit[1], full)                     # undoes a journaled move; rollback sees it in place
                moved_back.append((hit[1], full))
                log.append(("Data Files", f"'{rel}' is needed again", f"moved the original back from {hit[1]}"))
                continue
            if hit:
                item = (rel, why, hit[1]) + tuple(item[3:])  # copied below, recorded like any generated file
        resolved.append(item)
    _journal_moves_back(game_dir, moved_back)
    items = resolved
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
            raise RuntimeError("generated files require a game baseline so rollback can remove them")
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
    """Choose <mod>/Off/<plugin>/plugin repairs/<original path>.bak for a rewritten plugin."""
    parent = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)
    if shipped_game_file(parent, name):
        base = backup_file("plugins", "Official Add-Ons", name + ".bak")
    else:
        base = plugin_off_backup_path(parent, name, "plugin repairs", plugin=name, unique=False) + ".bak"
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
    # backup_path() writes Off/<plugin>/plugin repairs/...; older runs used plugin-repair, Off/plugin-repair/,
    # and the game's Data Files\Off (shipped files' backups, and older runs' others) or MWManager\backups\Data Files
    roots = [backup_root_for(owner_root, parent, name), owner_root or parent]
    if not owner_root:
        roots.append(os.path.join(BACKUP_DIR, "Data Files"))
    for off_root in dict.fromkeys(roots):
        off += glob.glob(os.path.join(off_root, "Off", "**", name + ".bak*"), recursive=True)
    legacy = [path + ".bak"] + [f"{path}.bak{n}" for n in range(2, 100)]
    paths = [p for p in central + off + legacy if not p.endswith(REPAIRED_SIDECAR) and os.path.isfile(p)]
    return sorted(set(paths), key=backup_order)


def run_clean_up(args):
    """Delete the backups between each plugin's original (the oldest) and its newest. A plugin that isn't in
    Data Files keeps all of its backups: before a deployment that can be every plugin."""
    doomed = []
    for plugin, path in sorted(index_dir(args.data_dir).items()):
        baks = [b for b in backup_candidates(path)            # oldest first
                if (m := CLEANUP_BAK_RE.match(os.path.basename(b).lower())) and m.group("plugin") == plugin]
        if len(baks) > 2:
            print(f"    - {plugin}: {len(baks) - 2} in between; keeping the oldest and the newest")
            doomed += baks[1:-1]
    if not doomed:
        print("  Nothing to clean up: no plugin has backups between its oldest and its newest.")
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
            print(f"  {os.path.basename(path)}: NOT deleted ({e})")
    with MT.open_log(args.fix_log, "a", encoding="utf-8") as f:
        import datetime
        f.write(f"\n==== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} clean-up ====\n")
        f.write("".join(l + "\n" for l in lines))
    print(f"\nDeleted {done} backup file(s), {freed / 1048576:.1f} MB freed. Logged to {args.fix_log}.")
    return 0


def restore_all(data_dir, assume_yes, fix_log):
    """Put every plugin back to its OLDEST backup (the original). What is replaced is backed up first and no
    backup is removed: the file there may be a newer version of the mod."""
    lookup = index_dir(data_dir)
    ours = {x.lower() for x in OUR_PATCHES}
    groups = {}                                    # plugin path -> its backups, oldest first
    for plugin, path in lookup.items():
        if plugin in ours:
            continue
        baks = [b for b in backup_candidates(path)
                if (m := BAK_RE.match(os.path.basename(b).lower())) and m.group("plugin") == plugin]
        if baks:
            groups[path] = baks
    if not groups:
        print("No plugin backups found.")
        return 0
    print("These plugins will be put back to their original (oldest) backup; what is there now is backed up first:")
    for path, baks in sorted(groups.items(), key=lambda kv: kv[0].lower()):
        print(f"  {os.path.basename(path)}  <- {os.path.basename(baks[0])}")
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
        oldest = baks[0]
        try:
            st = os.stat(oldest)                   # copy2 kept the plugin's original date on it
            with open(oldest, "rb") as f:
                data = f.read()
            with open(path, "rb") as f:
                now = f.read()
            if now == data:
                continue
            bak = backup_path(path)
            shutil.copy2(path, bak)
            safe_write_bytes(path, data, keep_times=st)     # in place, so hard links (Vortex) stay intact
            with open(path, "rb") as f:
                if f.read() != data:
                    raise OSError("the restored copy doesn't match the backup")
            done += 1
            lines.append(f"{os.path.basename(path)}: restored from {os.path.basename(oldest)} "
                         f"(what was there: {os.path.basename(bak)})")
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
    replaced_addon = os.path.join(os.path.abspath(data_dir), "Off", "Official Add-Ons", os.path.basename(name))
    for path in (disabled_plugin_path(data_dir, name), legacy_disabled_plugin_path(data_dir, name), replaced_addon):
        if os.path.isfile(path):
            return path
    return None


def move_disabled_plugin_file(source, target):
    try:
        os.replace(source, target)
        return
    except OSError as error:
        if error.errno != errno.EXDEV and getattr(error, "winerror", None) != 17:
            raise
    created = complete = False
    try:
        with open(source, "rb") as src, open(target, "xb") as dst:
            created = True
            shutil.copyfileobj(src, dst)
        shutil.copystat(source, target)
        complete = True
    finally:
        if created and not complete:
            os.unlink(target)
    try:
        os.unlink(source)
    except OSError:
        os.unlink(target)
        raise


def rollback_disabled_plugin_moves(moved):
    errors = []
    for source, target in reversed(moved):
        try:
            if os.path.exists(source):
                raise FileExistsError(f"Cannot restore {source}: a file already exists")
            move_disabled_plugin_file(target, source)
        except OSError as error:
            errors.append(f"{target} -> {source}: {error}")
    if errors:
        raise OSError("Could not roll back plugin moves: " + "; ".join(errors))


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
            move_disabled_plugin_file(source, target)
            moved.append((source, target))
    except OSError:
        rollback_disabled_plugin_moves(moved)
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
            move_disabled_plugin_file(source, target)
            restored.append((source, target))
    except OSError:
        rollback_disabled_plugin_moves(restored)
        raise
    return restored


# Upstream builds no plugin into the always-loses list any more (CONFLICT_BLACKLIST_DEFAULT and
# CONFLICT_BLACKLIST_BUILTIN are gone, and so is the shipped starter file): the list is only what the user's
# own file holds, empty to begin with. Its path is CONFLICT_BLACKLIST_FILE (mwmanager_conflict_blacklist.txt);
# its header text comes from upstream's conflict_blacklist_header(path).
DEFAULT_RULES = f"""; {os.path.basename(RULES_FILE)}: your load order rules for mwmanager.py --sort
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
;
; With no rules, the current install order is preserved wherever masters and mlox allow it.
; MWManager's ESM fix and generated final patches are positioned by built-in safety rules.
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
