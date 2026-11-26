"""MWManager layer for the asset checker: upstream mwdialog_assets.py with MWManager's settings.

Runs inside the upstream mwdialog_assets namespace (see mwmanager_upstream.py); a name set here replaces
upstream's. What MWManager changes: originals of repaired, broken and unused files go to the owning mod's
Off\\<plugin>\\<reason>\\ (or MWManager's backups, never the game install), texconv and ffmpeg come from
MWManager\\tools, bug reports carry the mod's Nexus page and go to MWManager\\outputs\\bugreports, a damaged Bink
video gets the stand-in, and the cache and records live in the MWManager folder. The checks themselves (mipmap
rules, texture limits, model facts, the SQLite cache) are upstream's.
"""
from mwdialog_assets import *  # noqa: F401,F403  (names resolve in mwdialog_assets when composed)
import os
import re
import shutil
import sys
import tempfile
import time
import hashlib

import mwdialog_offlayout as OL

_scanner = sys.modules["mwdialog_check"]
MANAGER_DIR = _scanner.MANAGER_DIR
OUTPUT_DIR = _scanner.OUTPUT_DIR
TOOLS_DIR = _scanner.TOOLS_DIR
# the kind of repair -> its reason folder (the fixed list in mwdialog_offlayout.REASONS)
FEATURE_OF = {"snd": "converted sounds", "tex": "converted textures", "splash": "converted splash screens",
              "keys": "fixed text keys", "abs": "fixed texture paths", "ascii": "ascii",
              "broken": "broken", "orphan": "orphaned animations", "unused": "unused assets"}
DEFAULT_REASON = "repaired originals"
if not _scanner.asset_limit("reduce_splash_2048"):
    SPLASH_MAX_WIDTH = None             # splashes keep their width; alpha and non-TGA repairs still apply


def broken_dest(root_or_mod, rel, sub="broken", plugin="Found at Setup"):
    """<mod>\\Off\\<plugin>\\<reason>\\<original path> (the owner is mwdialog_offlayout.owner_name's)."""
    return OL.off_path(root_or_mod, sub, rel, hint=plugin)


def _off_dest(m, data_dir, rel, reason):
    """Where a file leaves Data Files to: the owning staged mod's Off\\<plugin>\\<reason>\\<rel>, or (a file no
    staged mod owns) the manager's own backup folder for it."""
    mod, plugin = m.asset_owner_folder(data_dir, rel)
    if not mod:
        return m.plugin_off_backup_path(data_dir, rel, reason, plugin)
    dest = broken_dest(mod, rel, reason, plugin)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):                       # never over an earlier original of the same path
        stem, ext = os.path.splitext(dest)
        number = 1
        while os.path.exists(f"{stem}.mwmanager-{number}{ext}"):
            number += 1
        dest = f"{stem}.mwmanager-{number}{ext}"
    return dest


def apply(fixes, data_dir, game_dir, m, log, dry=False):
    """--fix: repair what check() found. Returns a summary string."""
    texconv = next((p for p in (os.path.join(TOOLS_DIR, "texconv.exe"), shutil.which("texconv")) if p and
                    os.path.exists(p)), None)
    ff = find_ffmpeg()
    done = {"tex": 0, "abs": 0, "snd": 0, "unused": 0, "broken": 0, "orphan": 0}
    skipped = []
    moves = []
    install_ledger = ledger_run = None

    def move_out(full, dest, rel):
        nonlocal install_ledger, ledger_run
        if install_ledger is None:
            import mwmanager_journal
            install_ledger = mwmanager_journal.Journal(os.path.join(MANAGER_DIR, "journal"))
            ledger_run = install_ledger.start_run("mwmanager_install.py", {"operation": "initial asset tidy"})
        data_abs = os.path.normcase(os.path.abspath(data_dir))
        staged = [p for p in m.hard_link_names(full) if not os.path.normcase(p).startswith(data_abs + os.sep)]
        install_ledger.move_original(game_dir, full, dest, ledger_run["run"], tag="original-install-asset")
        # deployed from a staged package and now kept in that package's Off: its copy at the deploy path goes
        # too, or the next deployment links it straight back (--restore-unused puts it back there)
        for path in staged:
            if not OFF_MARKER_RE.search(path) and os.path.isfile(path):
                os.remove(path)

    tmp = os.path.join(tempfile.gettempdir(), f"mwmanager_tmp_assets_{os.getpid()}")
    if fixes and not dry:                          # each file is converted or moved one by one: say so
        print(f"Asset repairs: {len(fixes):,} file(s)...", file=sys.stderr, flush=True)
    last_report = time.monotonic()
    for index, (kind, full, rel, job) in enumerate(fixes, 1):
        if dry:
            done[kind] += 1
            continue
        if time.monotonic() - last_report >= 20:
            print(f"Asset repairs: {index - 1:,} of {len(fixes):,} files...", file=sys.stderr, flush=True)
            last_report = time.monotonic()
        try:
            if kind == "tex":
                if not texconv:
                    skipped.append(f"{rel} (texconv.exe not found)")
                    continue
                os.makedirs(tmp, exist_ok=True)
                args = [texconv, "-nologo", "-y", "-dx9", "-o", tmp]
                if job.get("dxt"):
                    args += ["-f", "BC3_UNORM" if job.get("alpha", True) else "BC1_UNORM"]
                args += ["-m", "0"] if job.get("mips") or job.get("size") or job.get("dxt") else []
                if job.get("size"):
                    args += ["-w", str(job["size"][0]), "-h", str(job["size"][1])]
                res = run_tool(args + [full])
                out = os.path.join(tmp, os.path.basename(full))
                if res.returncode or not os.path.exists(out):
                    skipped.append(f"{rel} (texconv failed: {(res.stdout + res.stderr).strip()[-120:]})")
                    continue
                new = open(out, "rb").read()
                os.remove(out)
            elif kind == "abs":
                n, new = rewrite_absolute(open(full, "rb").read())
                if not new:
                    continue
            elif kind == "snd":
                if not ff:
                    skipped.append(f"{rel} (ffmpeg not found)")
                    continue
                os.makedirs(tmp, exist_ok=True)
                out = os.path.join(tmp, "x" + os.path.splitext(full)[1].lower())
                a = [ff, "-y", "-v", "error", "-i", full]
                if job.get("mono"):
                    a += ["-ac", "1"]
                if job.get("rate"):
                    a += ["-ar", str(job["rate"])]
                a += (["-c:a", "pcm_s16le"] if out.endswith(".wav") else ["-c:a", "libmp3lame", "-q:a", "2",
                                                                         "-id3v2_version", "0"]) + [out]
                res = run_tool(a)
                if res.returncode or not os.path.exists(out):
                    skipped.append(f"{rel} (ffmpeg failed: {res.stderr.strip()[-120:]})")
                    continue
                new = open(out, "rb").read()
                os.remove(out)
            elif kind == "orphan":                     # out of Data Files, into the mod's Off\...\orphaned\
                dest = _off_dest(m, data_dir, rel, FEATURE_OF["orphan"])
                if os.path.exists(full):
                    move_out(full, dest, rel)
                moves.append((rel, dest, job))
                done["unused"] += 1
                log.append(("Data Files", f"'{rel}' ({job})", f"moved to {dest}"))
                continue
            elif kind == "broken":                     # keep the broken file in Off, a stand-in in its place
                dest = _off_dest(m, data_dir, rel, FEATURE_OF["broken"])
                if not os.path.exists(dest):
                    shutil.copy2(full, dest)
                m.replace_asset_in_place(full, m.stand_in_bytes(rel))
                done.setdefault("broken", 0)
                done["broken"] += 1
                log.append(("Data Files", f"'{rel}' ({job})", f"broken file kept in {dest}; stand-in put in its place"))
                continue
            else:                                      # unused / empty: move out of Data Files
                dest = _off_dest(m, data_dir, rel, FEATURE_OF["unused"])
                if os.path.exists(full):
                    move_out(full, dest, rel)
                moves.append((rel, dest, job))
                log.append(("Data Files", f"'{rel}' ({job})", f"moved to {dest}"))
                done["unused"] += 1
                continue
            _mod, owner_plugin = m.asset_owner_folder(data_dir, rel)
            reason = FEATURE_OF.get(kind, DEFAULT_REASON)
            if _mod:
                bak = _backup_in_mod(_mod, rel, os.path.join(data_dir, rel), owner_plugin, m, reason)
            else:
                bak = m.backup_asset_original(data_dir, rel, plugin=owner_plugin, feature=reason)
            m.replace_asset_in_place(full, new)
            done[kind] += 1
            log.append(("Data Files", f"'{rel}' ({kind})", f"repaired (original: {bak})"))
        except OSError as e:
            skipped.append(f"{rel} ({e})")
    if moves:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, UNUSED_RECORD), "a", encoding="utf-8") as f:
            for rel, dest, why in moves:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{rel}\t{dest}\t{why}\n")
    if ledger_run is not None:
        install_ledger.finish_run(ledger_run)
    shutil.rmtree(tmp, ignore_errors=True)
    msg = (f"{done.get('broken', 0)} broken file(s) replaced by stand-ins (originals in each mod's Off tree), "
           f"{done['tex']} texture(s) converted, {done['abs']} model(s) with absolute texture paths fixed, "
           f"{done['snd']} sound(s) converted, {done['unused']} unused/empty file(s) moved to each mod's Off tree")
    if skipped:
        msg += f"; {len(skipped)} skipped, e.g. " + "; ".join(skipped[:3])
    return ("Dry run: would do " if dry else "") + msg
# <mod>\Off\... (any case, either slash): a file these tools took out of a mod
OFF_MARKER_RE = re.compile(r"[\\/]off[\\/]", re.IGNORECASE)


def restore_unused(game_dir, data_dir):
    """--restore-unused: put back every file listed in mwmanager_unused_assets.txt (staging copy back into the
    mod folder; Vortex redeploys it, or it is copied into Data Files when it wasn't Vortex's)."""
    path = os.path.join(OUTPUT_DIR, UNUSED_RECORD)
    if not os.path.exists(path):
        print("Nothing to restore.")
        return 0
    back, failed = 0, []
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rel, dest = parts[1], parts[2]
        if not os.path.exists(dest):
            failed.append(line)
            continue
        try:
            marker = OFF_MARKER_RE.search(dest)
            if marker:
                mod = dest[:marker.start()]
                staged = os.path.join(mod, rel)
                if os.path.exists(staged):
                    raise FileExistsError("a newer file is there")
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                os.replace(dest, staged)
                target = os.path.join(data_dir, rel)
                if not os.path.exists(target):
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    try:
                        os.link(staged, target)
                    except OSError:
                        shutil.copy2(staged, target)
            else:
                target = os.path.join(data_dir, rel)
                if os.path.exists(target):
                    raise FileExistsError("a newer file is there")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(dest, target)
            back += 1
        except OSError as exc:
            failed.append(line)
            print(f"  could not put back '{rel}': {exc}")
    if failed:
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(failed) + "\n")
        os.replace(temp_path, path)
        print(f"{back} file(s) put back; {len(failed)} remain in {path} for another retry.")
        return 1
    os.replace(path, path + ".restored")
    print(f"{back} file(s) put back.")
    return 0


def _backup_in_mod(root, rel, full, plugin_name, m, feature=DEFAULT_REASON):
    """Keep the original at <mod>\\Off\\<plugin>\\<reason>\\<original relative path>, and an update's beside it.
    An original already kept for this path (any reason or older layout, but not a moved-out file) is reused."""
    kept = OL.find_original(root, rel)
    if kept:
        return m.keep_new_original(kept, full)
    dest = OL.off_path(root, feature, rel, hint=plugin_name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        shutil.copy2(full, dest)
        m._ASSET_BACKUP_OF[os.path.normcase(os.path.abspath(full))] = dest
        return dest
    return m.keep_new_original(dest, full)


def find_ffmpeg():
    """ffmpeg in MWManager's tools folder, else on PATH."""
    bundled = os.path.join(TOOLS_DIR, "ffmpeg.exe")
    return bundled if os.path.isfile(bundled) else shutil.which("ffmpeg")


def rename_ascii(root, full, rel, new_rel, log, backup_callback=None):
    """Rename a file (and the folders on its way) to its ASCII name, and change the name in the mod's own
    plugins (Windows-1252, as Morrowind writes them; same length, so changed in place)."""
    new_full = os.path.join(root, new_rel)
    operations = []
    os.makedirs(os.path.dirname(new_full), exist_ok=True)
    if not os.path.exists(new_full):
        os.replace(full, new_full)
        operations.append({"src": full, "dst": new_full})
    elif os.path.exists(full):
        # If this path was renamed earlier, keep the old-named source in Off\<plugin>\ascii\<old path>.
        import mwdialog_check as _manager
        keep = OL.off_path(root, "ascii", rel)
        kept_before = keep if os.path.exists(keep) else OL.find_original(root, rel)   # or an older layout's
        if kept_before:
            # an update's file is a new original; raises if it can't be kept
            _manager.keep_new_original(kept_before, full)
            os.remove(full)
            operations.append({"duplicate": new_full, "restore": full})
            log(f"{rel}: ASCII replacement {new_rel} already exists and the original is already kept; "
                "removed the redundant copy")
        else:
            os.makedirs(os.path.dirname(keep), exist_ok=True)
            os.replace(full, keep)
            operations.append({"src": full, "dst": keep})
            log(f"{rel}: ASCII replacement {new_rel} already exists; moved the original to "
                f"{os.path.relpath(keep, root)}")
    # drop the old folders when emptied
    d = os.path.dirname(full)
    while os.path.normcase(d) != os.path.normcase(root) and os.path.isdir(d) and not os.listdir(d):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)
    # the name as plugins have it: no "Meshes\" etc. in front (or, rarely, with it)
    parts_old, parts_new = rel.replace("/", "\\").split("\\"), new_rel.replace("/", "\\").split("\\")
    pairs = []
    for k in (1, 0):
        o, n = "\\".join(parts_old[k:]), "\\".join(parts_new[k:])
        try:
            ob, nb = o.encode("cp1252"), n.encode("cp1252")
        except UnicodeEncodeError:
            continue
        if ob != nb and len(ob) == len(nb):
            pairs.append((ob.lower(), nb))
    if not pairs:
        return operations
    import mwdialog_check as _m
    for pf in os.listdir(root):
        if not pf.lower().endswith((".esp", ".esm", ".omwaddon")):
            continue
        pp = os.path.join(root, pf)
        try:
            data = open(pp, "rb").read()
            new, hits = plugin_path_rename(data, pairs)
        except (OSError, _m.FormatError) as e:
            log(f"{pf}: not changed ({e})")
            continue
        if hits:
            if backup_callback:
                backup = backup_callback(pp, data)
                if backup:
                    operations.append({"restore": pp, "backup": backup})
            else:
                bak = OL.off_path(root, FEATURE_OF["ascii"], pf + ".bak_ascii", hint=pf)
                os.makedirs(os.path.dirname(bak), exist_ok=True)
                if not os.path.exists(bak):
                    shutil.copy2(pp, bak)
                _m.keep_new_original(bak, pp)
            with open(pp, "r+b") as f:                 # in place: keeps a Vortex hard link
                f.write(bytes(new))
            _m.record_repair(pp, new)
            log(f"{pf}: {hits} name(s) of {rel} changed to {new_rel}")
    return operations


def apply_folder(root, fixes, m, game_dir, plugin_name, change_log=None, progress_callback=None,
                 journal_ops=None):
    """Repair findings in place; optionally append one exact record per successful file change."""
    texconv = next((p for p in (os.path.join(TOOLS_DIR, "texconv.exe"), shutil.which("texconv")) if p and
                    os.path.exists(p)), None)
    ff = find_ffmpeg()
    tmp = os.path.join(tempfile.gettempdir(), f"mwmanager_tmp_assets_{os.getpid()}")
    done, skipped = 0, []
    total = len(fixes)
    for index, (kind, full, rel, job) in enumerate(fixes, 1):
        if progress_callback and (index == 1 or index % 25 == 0):
            progress_callback(f"Applying asset repairs for {plugin_name}: {index:,} of {total:,} files")
        try:
            with open(full, "rb") as source:
                data = source.read()
            new = None
            if kind == "ascii":
                def backup_plugin(path, _data):
                    plugin_rel = os.path.relpath(path, root)
                    return _backup_in_mod(root, plugin_rel, path, plugin_name, m,
                                          FEATURE_OF["ascii"])
                operations = rename_ascii(root, full, rel, job, lambda t: print("  " + t), backup_plugin)
                if journal_ops is not None:
                    journal_ops.extend(operations)
                done += 1
                continue
            if kind == "orphan":
                dest = OL.off_path(root, FEATURE_OF["orphan"], rel, hint=plugin_name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                moved = not os.path.exists(dest)
                if moved:
                    shutil.move(full, dest)
                if change_log is not None:
                    change_log.append({"file": rel, "action": f"moved to Off ({job})" if isinstance(job, str) else
                                       "moved to Off/orphaned animations after checking global asset availability",
                                       "backup": os.path.relpath(dest, root), "cross_plugin": True})
                if journal_ops is not None and moved:
                    journal_ops.append({"src": full, "dst": dest})
                done += 1
                continue
            if kind == "broken":
                dest = _backup_in_mod(root, rel, full, plugin_name, m, FEATURE_OF["broken"])
                m.replace_asset_in_place(full, m.stand_in_bytes(rel))
                if journal_ops is not None:
                    journal_ops.append({"restore": full, "backup": dest})
                if change_log is not None:
                    change_log.append({"file": rel, "action": "replaced damaged asset with MWManager stand-in", "backup": os.path.relpath(dest, root)})
                done += 1
                continue
            if kind == "keys":
                _n, new = m.nif_text_keys_lf(data)
            elif kind == "abs":
                _n, new = rewrite_absolute(data)
            elif kind == "tex":
                if job.get("max") and set(job) == {"max"}:
                    new, _info = m.dds_reduce(data, job["max"])   # just drop the big mip levels
                    if new is None and not texconv:
                        skipped.append(f"{rel} ({_info})")
                        continue
                if new is None:
                    if not texconv:
                        skipped.append(f"{rel} (texconv.exe not found)")
                        continue
                    os.makedirs(tmp, exist_ok=True)
                    a = [texconv, "-nologo", "-y", "-dx9", "-o", tmp, "-m", "1" if job.get("nomips") else "0"]
                    if job.get("convert"):
                        a += ["-ft", job["convert"][1:]] + (["-f", "BC3_UNORM"] if job["convert"] == ".dds" else [])
                    if job.get("dxt"):
                        a += ["-f", "BC3_UNORM" if job.get("alpha", True) else "BC1_UNORM"]
                    w, h = job.get("size") or (None, None)
                    if job.get("max"):
                        info = m.dds_info(data[:148])
                        w0, h0 = (w or info[0]), (h or info[1])
                        k = max(w0, h0) / job["max"]
                        if k > 1:
                            w, h = max(4, int(w0 / k)), max(4, int(h0 / k))
                    if w:
                        a += ["-w", str(w), "-h", str(h)]
                    res = run_tool(a + [full])
                    out = os.path.join(tmp, os.path.splitext(os.path.basename(full))[0] + job["convert"]
                                       if job.get("convert") else os.path.basename(full))
                    if res.returncode or not os.path.exists(out):
                        skipped.append(f"{rel} (texconv: {(res.stdout + res.stderr).strip()[-100:]})")
                        continue
                    new = open(out, "rb").read()
                    os.remove(out)
            elif kind == "snd":
                if job.get("strip") and set(job) == {"strip"}:
                    new = data[job["strip"]:]
                else:
                    if not ff:
                        skipped.append(f"{rel} (ffmpeg not found)")
                        continue
                    os.makedirs(tmp, exist_ok=True)
                    out = os.path.join(tmp, "x" + (".mp3" if job.get("mp3") else
                                                    os.path.splitext(full)[1].lower()))
                    a = [ff, "-y", "-v", "error", "-i", full]
                    if job.get("mono"):
                        a += ["-ac", "1"]
                    if job.get("rate"):
                        a += ["-ar", str(job["rate"])]
                    a += (["-c:a", "pcm_s16le"] if out.endswith(".wav") else
                          ["-c:a", "libmp3lame", "-q:a", "2", "-id3v2_version", "0", "-map_metadata", "-1"]) + [out]
                    res = run_tool(a)
                    if res.returncode or not os.path.exists(out):
                        skipped.append(f"{rel} (ffmpeg: {res.stderr.strip()[-100:]})")
                        continue
                    new = open(out, "rb").read()
                    os.remove(out)
            if kind == "splash":                      # at most 2048 wide, 24-bit RLE TGA; a DDS/BMP... becomes a .tga
                if not texconv and splash_needs_texconv(full):
                    skipped.append(f"{rel} (texconv.exe not found)")
                    continue
                os.makedirs(tmp, exist_ok=True)
                new = splash_tga(full, texconv, tmp)
            if not new:
                continue
            to_tga = kind == "splash" and not full.lower().endswith(".tga")
            if to_tga:
                replacement = os.path.splitext(full)[0] + ".tga"
                if os.path.exists(replacement):
                    skipped.append(f"{rel} (a TGA of that name exists already)")
                    continue
            if kind == "snd" and job.get("mp3"):
                replacement = os.path.splitext(full)[0] + ".mp3"
                if os.path.exists(replacement):
                    skipped.append(f"{rel} (MP3 replacement already exists)")
                    continue
            if kind == "tex" and job.get("convert"):
                replacement = os.path.splitext(full)[0] + job["convert"]
            if kind == "tex" and job.get("keep"):
                # the original stays where it is: only the DDS beside it is new
                partial = replacement + ".mwmanager-tmp"
                with open(partial, "wb") as output:
                    output.write(new)
                os.replace(partial, replacement)
                if journal_ops is not None:
                    journal_ops.append({"generated": replacement, "sha256": hashlib.sha256(new).hexdigest()})
                done += 1
                continue
            backup = _backup_in_mod(root, rel, full, plugin_name, m, FEATURE_OF.get(kind, DEFAULT_REASON))
            if kind == "tex" and job.get("convert") and os.path.exists(replacement):
                # the mod ships its own DDS/TGA of that name, which the game prefers: the PNG only goes to Off
                os.remove(full)
                if journal_ops is not None:
                    journal_ops.append({"restore": full, "backup": backup})
            elif (kind == "snd" and job.get("mp3")) or (kind == "tex" and job.get("convert")) or to_tga:
                partial = replacement + ".mwmanager-tmp"
                with open(partial, "wb") as output:
                    output.write(new)
                os.replace(partial, replacement)
                try:
                    os.remove(full)
                except OSError:
                    os.remove(replacement)
                    raise
                if journal_ops is not None:
                    journal_ops.append({"restore": full, "backup": backup})
                    journal_ops.append({"generated": replacement,
                                        "sha256": hashlib.sha256(new).hexdigest()})
            else:
                m.replace_asset_in_place(full, new)
                if journal_ops is not None:
                    journal_ops.append({"restore": full, "backup": backup})
            if change_log is not None:
                details = {"keys": "normalized NIF text-key line endings", "abs": "rewrote absolute texture paths"}.get(kind)
                if kind == "keys":
                    count, _ = m.nif_text_keys_lf(data)
                    details = f"normalized {count} NIF text-key line ending(s)"
                elif kind == "abs":
                    count, _ = rewrite_absolute(data)
                    details = f"rewrote {count} absolute texture path(s)"
                elif kind == "tex":
                    details = "repaired or converted DDS texture (including requested size/format/mipmap changes)"
                elif kind == "splash":
                    details = f"splash screen made an {splash_target()}"
                elif kind == "snd":
                    details = ("converted WAV to same-stem MP3" if job.get("mp3") else
                               "converted sound to Morrowind-compatible encoding" if not job.get("strip") else
                               "removed leading sound header bytes")
                change_log.append({"file": rel, "action": details or f"repaired {kind} asset", "backup": os.path.relpath(backup, root)})
            done += 1
        except OSError as e:
            skipped.append(f"{rel} ({e})")
    shutil.rmtree(tmp, ignore_errors=True)
    if progress_callback and total:
        progress_callback(f"Asset repairs complete for {plugin_name}: {done:,} repaired, {len(skipped):,} skipped")
    return done, skipped


def vortex_mod_metadata(mod_folder):
    """Read Vortex's own meta.ini for a staged mod's display name, author, and page URL."""
    if not mod_folder or not os.path.isdir(mod_folder):
        return {}
    import configparser
    folder = os.path.abspath(mod_folder)
    wanted_id = nexus_id(folder)
    candidates = []
    root_meta = os.path.join(folder, "meta.ini")
    if os.path.isfile(root_meta):
        candidates.append(root_meta)
    try:
        top_entries = os.listdir(folder)
    except OSError:
        return {}
    extras = next((os.path.join(folder, name) for name in top_entries
                   if name.casefold() == "extras" and os.path.isdir(os.path.join(folder, name))), None)
    if extras:
        for base, dirs, files in os.walk(extras):
            dirs[:] = [d for d in dirs if d.casefold() not in {"off", "bugreports"}]
            if any(name.casefold() == "meta.ini" for name in files):
                candidates.append(os.path.join(base, next(name for name in files if name.casefold() == "meta.ini")))
    scored = []
    for path in candidates:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str.lower
        try:
            parser.read(path, encoding="utf-8-sig")
            section = parser["General"] if parser.has_section("General") else parser.defaults()
            values = {key: section.get(key, "").strip() for key in
                      ("name", "author", "modid", "nexusid", "url", "hascustomurl", "version", "newestversion")}
        except (OSError, configparser.Error, UnicodeError):
            continue
        mod_id = values.get("modid") or values.get("nexusid")
        if not mod_id.isdigit() or int(mod_id) <= 0:
            mod_id = ""
        score = (4 if wanted_id and mod_id == wanted_id else 0) + (2 if mod_id else 0) \
            + bool(values.get("name")) + bool(values.get("author"))
        scored.append((score, values, mod_id))
    if not scored:
        return {}
    _score, values, mod_id = max(scored, key=lambda item: item[0])
    mod_id = mod_id or wanted_id or ""
    custom_url = values.get("url", "")
    if values.get("hascustomurl", "").casefold() in {"true", "1", "yes"} \
            and custom_url.casefold().startswith(("https://", "http://")):
        page = custom_url
    elif mod_id and mod_id.isdigit():
        page = f"https://www.nexusmods.com/morrowind/mods/{mod_id}"
    else:
        page = ""
    return {"name": values.get("name") or os.path.basename(os.path.normpath(folder)),
            "author": values.get("author", ""), "mod_id": mod_id or "", "mod_page": page,
            "version": values.get("newestversion") or values.get("version", "")}


def bug_report_header(plugin_path, m, mod_folder=None):
    """Name, size, date, masters of a plugin (and its Nexus page), for the top of its bug report."""
    name = os.path.basename(plugin_path)
    plugin_author = ""
    plugin_description = ""
    try:
        st = os.stat(plugin_path)
        with open(plugin_path, "rb") as f:
            head = f.read(max(getattr(m, "DESC_OFF", 56) + getattr(m, "DESC_LEN", 256), 65536))
        masters = m.plugin_masters(head)
        try:
            plugin_author = m.cstr(head[m.AUTH_OFF:m.AUTH_OFF + m.AUTH_LEN])
            plugin_description = m.cstr(head[m.DESC_OFF:m.DESC_OFF + m.DESC_LEN])
        except (AttributeError, IndexError):
            pass
        info = f"{st.st_size} bytes, dated {time.strftime('%Y-%m-%d', time.localtime(st.st_mtime))}"
    except Exception:
        masters, info = [], "?"
    if mod_folder is None:
        try:
            mod_folder, _pl = m.asset_owner_folder(os.path.dirname(plugin_path), name)
        except Exception:
            mod_folder = None
    metadata = vortex_mod_metadata(mod_folder)
    nid = metadata.get("mod_id") or (nexus_id(mod_folder) if mod_folder else None)
    lines = [f"Bug report for {name}", f"Plugin: {name} ({info})"]
    if metadata.get("name"):
        lines.append(f"Vortex mod: {metadata['name']}")
    if metadata.get("version"):
        lines.append(f"Vortex version: {metadata['version']}")
    if metadata.get("author"):
        lines.append(f"Vortex mod author: {metadata['author']}")
    if plugin_author:
        lines.append(f"Plugin header author: {plugin_author}")
    if plugin_description:
        lines.append(f"Plugin description: {plugin_description}")
    page = metadata.get("mod_page")
    if not page and nid:
        page = f"https://www.nexusmods.com/morrowind/mods/{nid}"
    if page:
        lines.append(f"Mod page: {page}")
    lines += ["Masters: " + (", ".join(masters) if masters else "none"),
              f"Generated {time.strftime('%Y-%m-%d %H:%M')} by mwmanager / vortex_tidy (Morrowind + MWSE + MGE XE).",
              "Only this plugin's own defects and declared-master compatibility issues are listed, not unrelated mod interactions.",
              "Paths are relative to Data Files."]
    return lines


def nexus_id(folder):
    """The Nexus mod id in a Vortex folder name ('MUSE 2.02-46200-2-02-1593543300' -> '46200'), or None."""
    name = os.path.basename(folder.rstrip("\\/"))
    for pat in (r" (\d{2,7}) \S+ \d{4}-\d\d-\d\dT\d\d-\d\dZ \w+$",       # 'Name 56060 1.0 2026-10-17T08-23Z abc'
                r"-(\d{2,7})-(?:[\w.]+-)*\d{9,11}$",                         # 'Name-46200-2-02-1593543300'
                r"-(\d{2,7})-[\w.\- ()\[\]]+$",                          # 'Name-42009-2-1', '...-0-5-2RC (1) [2]'
                r"-(\d{2,7})-?$"):                                            # 'AnimatedSoulgemsV0_4-25278' 
        mm = re.search(pat, name)
        if mm:
            return mm.group(1)
    return None


def bug_report_name(data_dir, plugin_name, m):
    """Filename for the consolidated MWManager bug report in outputs/bugreports."""
    mod = None
    try:
        mod, _pl = m.asset_owner_folder(data_dir, plugin_name)
    except Exception:
        mod = None
    metadata = vortex_mod_metadata(mod)
    nid = metadata.get("mod_id") or (nexus_id(mod) if mod else None)
    return os.path.splitext(plugin_name)[0] + (f" [Nexus {nid}]" if nid else "") + ".bugreport.txt"


def bug_report_path(data_dir, game_dir, plugin_name, m):
    os.makedirs(os.path.join(OUTPUT_DIR, "bugreports"), exist_ok=True)
    return os.path.join(OUTPUT_DIR, "bugreports", bug_report_name(data_dir, plugin_name, m))


_upstream_check_folder = check_folder


def catalog_has_asset(m, relative_path):
    """Whether the installed-game catalog already owns this Data Files path."""
    import sqlite3
    database = os.path.join(getattr(m, "MANAGER_DIR", os.path.dirname(os.path.abspath(__file__))),
                            "game_catalog.sqlite3")
    normalized = relative_path.replace("\\", "/").strip("/").casefold()
    connection = None
    try:
        connection = sqlite3.connect(database)
        return connection.execute(
            "SELECT 1 FROM bsa_asset WHERE asset_path_norm = ? LIMIT 1", (normalized,)).fetchone() is not None
    except (OSError, sqlite3.Error):
        return False
    finally:
        if connection is not None:
            connection.close()


from core_addons import IMAGE_ASSET_DIRS, lua_texts, lua_used_images  # noqa: E402  (shared with the packers)


def check_folder(root, m, data_dir=None, bsa_meshes=None, stats=None, progress_callback=None):
    """Upstream's check of a mod folder, plus a damaged Bink video (not BIK / KB2) under Video\\, which --fix
    replaces with MWManager's stand-in video (the original kept in the mod's Off\\ tree)."""
    if progress_callback is None and os.environ.get("MWMANAGER_GUI") == "1":
        progress_callback = lambda message: print(f"MWMANAGER-PROGRESS: {message}", flush=True)
    original_walk, checked = _walk, 0

    def reporting_walk(*args, **kwargs):
        nonlocal checked
        for entry in original_walk(*args, **kwargs):
            checked += 1
            if progress_callback and checked % 2000 == 0:
                progress_callback(f"Checked {checked:,} asset files in {os.path.basename(root.rstrip(os.sep))}…")
            yield entry

    if progress_callback:
        progress_callback(f"Checking assets in {os.path.basename(root.rstrip(os.sep))}")
        globals()["_walk"] = reporting_walk
    try:
        issues, fixes = _upstream_check_folder(root, m, data_dir, bsa_meshes, stats)
    finally:
        globals()["_walk"] = original_walk
    # an "orphaned" animation whose path the installed game's BSAs own still has its model there: keep it
    owned = {rel for kind, _full, rel, why in fixes
             if kind == "orphan" and why == "orphaned animation" and catalog_has_asset(m, rel)}
    if owned:
        fixes = [f for f in fixes if not (f[0] == "orphan" and f[2] in owned)]
        issues = [i for i in issues if not (i[1] in owned and i[2].startswith("orphaned animation"))]
    video = os.path.join(root, "Video")
    for dp, _ds, fs in os.walk(video) if os.path.isdir(video) else ():
        for fn in fs:
            if not fn.lower().endswith(".bik"):
                continue
            full = os.path.join(dp, fn)
            try:
                with open(full, "rb") as f:
                    header = f.read(4)
            except OSError:
                continue
            if not header.startswith((b"BIK", b"KB2")):
                rel = os.path.relpath(full, root)
                issues.append(("ERROR", rel, "not a recognized Bink video (damaged or wrong file) -> original kept in "
                                             "the mod Off/broken tree; stand-in video deployed"))
                fixes.append(("broken", full, rel, "damaged Bink video"))
    # PNG/JPEG: neither the engine nor a BSA takes them. Converted like the other texture repairs, to DDS under
    # Textures and TGA under BookArt, Icons and Splash. Only there: elsewhere (Extras, fomod images, docs) nothing
    # would load them. One a Lua script names (MWSE and its MCM read PNG) stays as it is, loose, out of any BSA.
    lua_text = None
    for dp, ds, fs in os.walk(root):
        rel_dir = os.path.relpath(dp, root)
        if rel_dir == ".":
            ds[:] = [d for d in ds if d.casefold() in IMAGE_ASSET_DIRS]
            continue
        dds_stems = {os.path.splitext(fn)[0].casefold() for fn in fs if fn.lower().endswith(".dds")}
        splash = rel_dir.casefold().split(os.sep)[0] == "splash"
        for fn in fs:
            stem, ext = os.path.splitext(fn)
            if splash and ext.lower() == ".tga":
                continue                     # the splash list may be read by name: its TGA stays beside a DDS
            if ext.lower() in (".tga", ".bmp") and stem.casefold() in dds_stems:
                # the engine loads the .dds whenever a .tga/.bmp of that name is asked for: this file is never used
                rel = os.path.relpath(os.path.join(dp, fn), root)
                issues.append(("WARN", rel, f"unused: {stem}.dds beside it is loaded instead -> moved to Off"))
                fixes.append(("orphan", os.path.join(dp, fn), rel, f"{stem}.dds of the same name is loaded instead"))
            if not fn.lower().endswith((".png", ".jpg", ".jpeg")):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, root)
            if lua_text is None:
                lua_text = lua_texts(root)
            if fn.casefold() in lua_text:
                issues.append(("WARN", rel, "image named by a Lua script -> left as it is, kept out of the BSA"))
                continue
            target = ".dds" if rel.casefold().split(os.sep)[0] == "textures" else ".tga"
            issues.append(("ERROR", rel, f"{os.path.splitext(fn)[1]} image: Morrowind and its BSAs cannot use it -> "
                                         f"converted to {target} (original kept in Off)"))
            fixes.append(("tex", full, rel, {"convert": target}))
    if progress_callback:
        progress_callback(f"Asset scan complete: {checked:,} files checked; {len(fixes):,} in need of repairs")
    return issues, fixes
