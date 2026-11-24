"""MWManager layer: the scanner functions MWManager runs differently from upstream mwdialog_check.

Runs inside the upstream mwdialog_check namespace after mwmanager_state.py (see mwmanager_upstream.py). Each
definition here is upstream's, merged with MWManager's changes: the MWManager load order file is the load order,
--init's staged passes, --full keeping generated patches in place, plugins turned off are moved to
MWManager\\disabled_plugins, mlox rules kept in the MWManager folder, sounds converted in a temp folder, MGE XE's
sky plugin kept before the generated tail. Everything else comes from upstream unchanged.
"""
from mwdialog_check import *  # noqa: F401,F403  (names resolve in mwdialog_check when composed)


def move_doubled_plugins(args, names):
    return move_plugins_to_disabled(args.data_dir, names)


def doubled_turnoff_message(count, moved, backup):
    return (f"  Turned off {count} plugin(s) (old list saved as {backup}); moved {len(moved)} to "
            f"MWManager\\disabled_plugins and listed in {DISABLED_RECORD}.")


def conflict_rule_backup_path(_rules_path):
    return backup_file("state", f"mwmanager_rules-{time.strftime('%Y%m%d-%H%M%S')}.bak")


def reenable_candidate_path(data_dir, name, lookup):
    return lookup.get(name.lower()) or find_disabled_plugin(data_dir, name)


def restore_reenabled_plugins(data_dir, names):
    lookup = index_dir(data_dir)
    return restore_disabled_plugins(data_dir, [n for n in names if n.lower() not in lookup])


def rollback_conflict_moves(moved):
    rollback_disabled_plugin_moves(moved)


def rollback_reenabled_moves(restored):
    rollback_disabled_plugin_moves(restored)


def conflict_choice(index, total, kind, a, b, conflict, labels, options, default,
                    off_plans, blockers, active_names, lua_named):
    if os.environ.get("MWMANAGER_CONFLICT_GUI") != "1":
        return input("  > ").strip().lower()
    from mwmanager_workflow import conflict_answer

    details = f"{a} vs {b}\n\n"
    evidence = conflict.get("incompatible")
    if evidence:
        details += (f"Moved land leaves {', '.join(sorted(evidence['evidence']))} at the old location "
                    f"in {len(evidence['cells'])} cell(s). Load order cannot fix this; one side must be "
                    "turned off.\n\n")
    else:
        details += f"{b} loads later and currently wins where the plugins conflict.\n\n"
    affected = list(dict.fromkeys(
        str(labels.get(item[0], item[0][1]))
        for field in ("place", "refs", "settings") for item in conflict[field]))
    if affected:
        details += ("Affected cells:\n" + "\n".join(affected[:20]) +
                    (f"\n... and {len(affected) - 20} more" if len(affected) > 20 else "") + "\n\n")
    if conflict.get("doubled"):
        details += f"{conflict['dups']} identical objects are placed twice.\n\n"
    rebuilt = [item[0] for item in conflict["place"] if item[6] and item[0][0] == "i"]
    if rebuilt and not evidence:
        details += ("Both rebuilt these interiors:\n" +
                    "\n".join(str(labels.get(cell, cell[1])) for cell in rebuilt) + "\n\n")
    if blockers:
        details += "Unavailable choices:\n" + "\n".join(blockers.values()) + "\n\n"
    for key, plan in off_plans.items():
        names = [active_names.get(name, name) for name in plan]
        details += f"If you {options[key][3]}:\n" + "\n".join(names) + "\n\n"
        lua_affected = [active_names[name] for name in plan if name in lua_named and name in active_names]
        if lua_affected:
            details += ("MWSE Lua or metadata also names these plugins; review those mods separately:\n" +
                        "\n".join(lua_affected) + "\n\n")
    if not evidence:
        details += ("Ordering changes who wins; interior choices rebuild the affected cells. "
                    "Disabling a plugin removes it from the active load order.\n\n")
    details += ("Choose Skip to leave this pair unchanged, or Finish review to keep decisions already made.")
    return conflict_answer(
        f"Cell conflict {index} of {total}: {kind}", details,
        [(value[3], key) for key, value in options.items()] +
        [("Skip this pair", "s"), ("Finish review", "q")], default or "s")


def repair_sound_ffmpeg():
    bundled = os.path.join(TOOLS_DIR, "ffmpeg.exe")
    return bundled if os.path.isfile(bundled) else shutil.which("ffmpeg")


def repair_sound_temp_path(_game_dir, rel):
    return os.path.join(tempfile.gettempdir(),
                        f"mwmanager_sound_{os.getpid()}" + os.path.splitext(rel)[1])


def repair_sound_missing_message():
    return "  ffmpeg isn't installed in MWManager\\tools or on PATH; install it and run this again."


def rules_sources(args, game_dir):
    sources = [manager_file(RULES_FILE)]
    if not getattr(args, "no_mlox_rules", False):
        if not ensure_mlox_rules(game_dir, dry_run=getattr(args, "dry_run", False),
                                 yes=getattr(args, "yes", False),
                                 disabled=getattr(args, "no_mlox_rules", False)):
            if not getattr(args, "dry_run", False):
                args.no_mlox_rules = True
        if not getattr(args, "no_mlox_rules", False):
            sources += [manager_file(os.path.join("mlox", "mlox_user.txt")),
                        manager_file(os.path.join("mlox", "mlox_base.txt"))]
    return sources


def write_ghost_statics(game_dir, data_dir, plugins):
    import mge_ghosts
    mge_ghosts.write(game_dir, data_dir, plugins, manager_dir=MANAGER_DIR)


def cell_conflicts_interactive(args):
    return ((os.environ.get("MWMANAGER_CONFLICT_GUI") == "1" or
             (sys.stdin.isatty() and sys.stdout.isatty()))
            and not args.dry_run and not getattr(args, "no_interactive", False))




def save_pending_cell_conflicts(args, ranked):
    if not getattr(args, "no_interactive", False) or args.dry_run:
        return
    answered = read_conflict_answers(os.path.dirname(os.path.abspath(args.data_dir)))
    pending = ["|".join(sorted(pair)) for pair, conflict in ranked
               if conflict.get("incompatible") or "|".join(sorted(pair)) not in answered]
    try:
        with open(output_file(NEW_CONFLICTS_FILE), "w", encoding="utf-8") as candidate_file:
            candidate_file.write("\n".join(pending))
    except OSError as exc:
        print(f"  Could not save unresolved cell-conflict candidates: {exc}")


def off_label(kind):
    return {
        "unused": "unused or empty file(s) to move to Off\\<plugin>\\Unused assets\\ (--restore-unused)",
        "broken": "broken file(s) to keep in Off\\<plugin>\\Damaged assets\\, a stand-in put in their place",
        "orphan": "orphaned animation(s) to move to Off\\<plugin>\\Unused assets\\",
    }[kind]


def major_master_patch_records(paths):
    return major_master_conflict_overrides(paths)


def activate_generated_patches(args, plugins, patch_path, place_built, game_dir):
    if IN_FULL or getattr(args, "init_scope", False) or not (patch_path or place_built[0]):
        return
    order_args = copy.copy(args)
    order_args.plugins = None
    current = list(read_load_order(order_args) or [name for name, _path in plugins])
    current = [name for name in current
               if name.lower() not in {PATCH_NAME.lower(), PLACE_PATCH_NAME.lower()}]
    if patch_path:
        pos = 0
        while pos < len(current) and current[pos].lower().endswith(".esm"):
            pos += 1
        current.insert(pos, PATCH_NAME)
    if place_built[0]:
        multi_at = next((i for i, name in enumerate(current)
                         if name.lower() == MULTI_PATCH_NAME.lower()), len(current))
        current.insert(multi_at, PLACE_PATCH_NAME)
    write_load_order(game_dir, current, "--fix generated patches")
    print(f"\nGenerated patch order saved to {LOAD_ORDER_FILE}; Morrowind.ini will be synced when this run finishes.")


def _journal_mwse_data(args, filename, write):
    """Run an upstream writer of Data Files\\MWSE\\config\\<filename> and journal the file, so rollback can
    explain and remove it (mwcollide_data.json, mwstranded_data.json: rebuilt by every --full)."""
    import mwmanager_journal
    game_dir = Path(args.data_dir).resolve().parent
    target = Path(args.data_dir).resolve() / "MWSE" / "config" / filename
    ledger = mwmanager_journal.Journal(Path(MANAGER_DIR) / "journal")
    if not ledger.has_baseline():
        return write()
    original = ledger.preserve_original(game_dir, target)
    run = ledger.start_run("mwmanager.py", {"operation": "write MWSE data", "path": filename})
    try:
        result = write()
    finally:
        if target.is_file():
            ledger.record(run["run"], mwmanager_journal.rel_of(game_dir, target),
                          "replace" if original["kind"] != "absent" else "create",
                          mwmanager_journal.sha256_file(target), original, tag="mwse-data")
        ledger.finish_run(run)
    return result


_upstream_write_collide_data = write_collide_data
_upstream_write_stranded_data = write_stranded_data


def write_collide_data(args, *rest):
    return _journal_mwse_data(args, COLLIDE_DATA, lambda: _upstream_write_collide_data(args, *rest))


def write_stranded_data(args, *rest):
    return _journal_mwse_data(args, STRANDED_DATA, lambda: _upstream_write_stranded_data(args, *rest))


_upstream_write_plugin_bytes = write_plugin_bytes


def write_plugin_bytes(path, data, keep_times=None):
    """Journal every generated or repaired Data Files plugin at the shared writer boundary."""
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
            run = ledger.start_run("mwmanager.py", {"operation": "write plugin", "path": target.name})
    result = _upstream_write_plugin_bytes(path, data, keep_times=keep_times)
    if ledger:
        ledger.record(run["run"], mwmanager_journal.rel_of(data_dir.parent, target),
                      "replace" if original["kind"] != "absent" else "create",
                      mwmanager_journal.sha256_file(target), original, tag="plugin-write")
        ledger.finish_run(run)
    return result


class World(World):
    """Upstream World with MWManager's changes: missing voice files and shared responses handled through patch
    records during --full."""

    def _stand_in(self, tag, rid, win, msg):
        """A winning record names a file the game can't load: point it at a working one of the same
        kind (the closest name, else the most common), so the object still works. Statics and
        activators get the invisible editor marker; lights and NPCs simply lose the model; a sound
        record keeps its name and gets a silent file created there."""
        import difflib
        A = self.assets
        pool = self._fallback_pool()
        st = self.subtypes.get((tag, rid))
        new, notes = {}, []
        vers = self.models[(tag, rid)]
        for (sub, kind), v in win[2].items():
            prob = A.field_problem(kind, v)
            if not A.fatal(prob):
                continue
            real = not A.placeholder(v) and not (kind == "icon" and v.lower().endswith(".nif"))
            top0 = {"mesh": "meshes", "icon": "icons", "texture": "textures", "sound": "sound"}[kind]
            orphan = A.find_orphaned(top0, A.rel(v, top0), any_ext=kind != "mesh") if real else None
            if orphan:                                    # 0. moved away by the orphan isolator
                rel = os.path.join(top0.capitalize(), A.rel(v, top0))
                if kind != "mesh":
                    rel = os.path.splitext(rel)[0] + os.path.splitext(orphan)[1]
                self.generated.append((rel, f"{tag} '{win[4]}'", orphan, win[1]))
                notes.append("move it back from .Orphaned (TextureAudit's orphan isolator moved it)")
                continue
            if real and self.vortex is not None:          # 1. the real file, never deployed
                top = {"mesh": "meshes", "icon": "icons", "texture": "textures", "sound": "sound"}[kind]
                hit = self.vortex.find(top, A.rel(v, top), any_ext=kind != "mesh")
                if hit:
                    src_path, mod = hit
                    rel = os.path.join(top.capitalize(), A.rel(v, top))
                    if kind != "mesh" and os.path.splitext(src_path)[1].lower() != os.path.splitext(rel)[1].lower():
                        rel = os.path.splitext(rel)[0] + os.path.splitext(src_path)[1].lower()
                    self.generated.append((rel, f"{tag} '{win[4]}' from Vortex mod '{mod}'", src_path, win[1]))
                    notes.append(f"copy '{os.path.relpath(src_path, self.vortex.root)}' (in Vortex, never deployed) "
                                 f"into Data Files")
                    continue
            if kind == "mesh" and A.placeholder(v) and sub == "MODL" and tag not in ("NPC_", "LIGH"):
                new[sub] = PLACEHOLDER_COIN          # unfinished object that is placed or used: shown as a coin
                notes.append(f"give it the coin model {PLACEHOLDER_COIN} (its model was never set)")
                continue
            if real and kind in ("mesh", "icon"):
                # 2. a stand-in FILE at the exact path the record names; the plugin is not changed, so the
                # real file (a mod installed later) simply replaces it
                if kind == "icon":
                    rel = os.path.join("Icons", os.path.splitext(A.rel(v, "icons"))[0] + ".dds")
                    src, what = None, "a placeholder icon"
                elif v.lower().endswith(".nif"):
                    rel = os.path.join("Meshes", A.rel(v, "meshes"))
                    dummy = (DUMMY_MARKER_NIF if tag in ("STAT", "ACTI", "LIGH") else
                             DUMMY_ANIM_NIF if tag in ("NPC_", "CREA") and sub == "MODL" and tag == "NPC_" else DUMMY_NIF)
                    try:
                        src = dummy_path(dummy)          # copied out of Morrowind.bsa when first needed
                    except OSError:
                        src = os.path.join(DUMMY_DIR, dummy)     # not there: no stand-in (checked below)
                    what = {DUMMY_MARKER_NIF: "the invisible editor marker", DUMMY_ANIM_NIF: "the default skeleton",
                            DUMMY_NIF: "a small coin"}[dummy]
                else:
                    rel = None
                if rel and (src is None or os.path.exists(src)):
                    self.generated.append((rel, f"{tag} '{win[4]}'", src, win[1]))
                    notes.append(f"put {what} at '{v}' (the record is left as it is)")
                    continue
            older = next((w for w in reversed(vers[:-1]) if not w[3] and w[2].get((sub, kind))
                          and A.field_problem(kind, w[2][(sub, kind)]) is None), None)
            if older:                                     # 2. what an earlier version of the record uses
                new[sub] = older[2][(sub, kind)]
                notes.append(f"use '{new[sub]}', the {A.KIND_NAMES[kind]} {older[1]} gives it")
                continue
            if real and kind != "icon":
                self.missing_by_plugin[win[1]] += 1
            if kind == "icon" and real:                   # 3. a dummy icon at the path the record names
                self.generated.append((os.path.join("Icons", os.path.splitext(A.rel(v, "icons"))[0] + ".dds"),
                                       f"{tag} '{win[4]}'", None, win[1]))
                notes.append(f"create a placeholder icon at '{v}'")
                continue
            if tag == "SOUN" and kind == "sound" and not A.placeholder(v):
                self.generated.append((os.path.join("Sound", A.rel(v, "sound")), f"sound record '{win[4]}'", None, win[1]))
                notes.append(f"create a silent '{v}'")
                continue
            if sub == "MODL" and tag in ("LIGH", "NPC_"):
                new[sub] = None
                notes.append("remove the model (a light works without one)" if tag == "LIGH"
                             else "remove the model (the NPC uses the default skeleton)")
                continue
            if sub == "MODL" and tag in ("STAT", "ACTI") and A.mesh_exists("EditorMarker.nif"):
                new[sub] = "EditorMarker.nif"
                notes.append("use the invisible editor marker as its model")
                continue
            cands = pool.get((tag, st, sub)) or pool.get((tag, None, sub))
            if not cands:
                if kind == "mesh" and real and v.lower().endswith(".nif"):   # a small coin at that path
                    self.generated.append((os.path.join("Meshes", A.rel(v, "meshes")), f"{tag} '{win[4]}'",
                                           None, win[1]))
                    notes.append(f"create a placeholder model (a small coin) at '{v}'")
                continue
            base = os.path.basename(v.replace("\\", "/")).lower()
            names = {os.path.basename(c.replace("\\", "/")).lower(): c for c in cands}
            close = difflib.get_close_matches(base, list(names), n=1, cutoff=0.6) if not A.placeholder(v) else []
            plain = pool.get((tag, st, sub, "vanilla"))
            pick = names[close[0]] if close else (plain or cands).most_common(1)[0][0]
            new[sub] = pick
            notes.append(f"use '{pick}' as its {A.KIND_NAMES[kind]}")
        if not new and not notes:
            self.r.add("WARN", win[1], "", win[4], msg + " (no stand-in of that kind found)")
            return
        self.r.add("ERROR", win[1], "", win[4], msg)
        if new:
            self.item_fixes.append({"plugin": win[1], "tag": tag, "id": win[4], "set": new, "npco": set(),
                                    "msgs": [(msg, "; ".join(notes))]})

    def _check_script_assets(self):
        """Files scripts play: StreamMusic (Music), Say (Sound), PlayBink (Video). Found only when the
        line runs, so a missing one is a WARN on the script; scripts are never changed."""
        kinds = {"streammusic": "music", "playbink": "video", "say": "sound"}
        for sid, (plugin, sname, text) in self.script_src.items():
            seen = set()
            for line in text.splitlines():
                code = line.split(";", 1)[0]
                for m in self.SCRIPT_FILE_RE.finditer(code):
                    kind = kinds[m.group(1).lower()]
                    path = m.group(2).strip()
                    if (kind, path.lower()) in seen:
                        continue
                    seen.add((kind, path.lower()))
                    prob = self.assets.field_problem(kind, path)
                    if not prob:
                        if kind == "sound" and SOUND_ROOTS:
                            bad = sound_file_problem(os.path.join("sound", path.strip().replace("/", os.sep)))
                            if bad:
                                self.r.add("ERROR", plugin, "", sname,
                                           f"script plays 'Sound\\{path}', which {bad}: the game can freeze or crash "
                                           f"when that line runs (scripts aren't changed: repair or replace the file)")
                        continue
                    if kind in ("sound", "music") and not self.assets.placeholder(path):
                        top = "Sound" if kind == "sound" else "Music"
                        rel = os.path.join(top, self.assets.rel(path, top.lower()))
                        hit = self.vortex.find(top.lower(), self.assets.rel(path, top.lower()), any_ext=True) \
                            if self.vortex is not None else None
                        if hit:
                            if os.path.splitext(hit[0])[1].lower() != os.path.splitext(rel)[1].lower():
                                rel = os.path.splitext(rel)[0] + os.path.splitext(hit[0])[1].lower()
                            self.generated.append((rel, f"script {sname} ({plugin}), from Vortex mod '{hit[1]}'", hit[0], plugin))
                            what = f"--fix copies it from Vortex mod '{hit[1]}', where it was never deployed"
                        else:
                            self.generated.append((rel, f"script {sname} ({plugin})", None, plugin))
                            self.missing_by_plugin[plugin] += 1
                            what = "--fix creates a silent stand-in there"
                        self.r.add("WARN", plugin, f"script {sname}", "", f"{m.group(1)}: {prob} ({what})")
                    elif kind == "video":
                        hit = self.vortex.find("video", self.assets.rel(path, "video")) if self.vortex is not None else None
                        if hit:
                            self.generated.append((os.path.join("Video", self.assets.rel(path, "video")),
                                                   f"script {sname} ({plugin}), from Vortex mod '{hit[1]}'", hit[0], plugin))
                            self.r.add("WARN", plugin, f"script {sname}", "",
                                       f"{m.group(1)}: {prob} (--fix copies it from Vortex mod '{hit[1]}')")
                        else:
                            self.missing_files.append((os.path.join("Video", self.assets.rel(path, "video")),
                                                       f"script {sname}", plugin))
                            self.missing_by_plugin[plugin] += 1
                            self.r.add("INFO", plugin, f"script {sname}", "",
                                       f"{m.group(1)}: {prob} (the game skips the video)")
                    else:
                        self.r.add("WARN", plugin, f"script {sname}", "",
                                   f"{m.group(1)}: {prob} (the game skips it when this line runs)")

    def _check_info(self, info, t, quest_stages, sound_files):
        r = self.r

        shared = len(set(t.defined_by.get((info.id or "").lower(), []))) > 1

        def add(sev, msg, op=None, desc=None, *, related_plugins=()):
            if op is not None and op[0] == "delete" and op != ("delete", "local") and shared and not IN_FULL:
                # A direct edit could break the response chain shared with another plugin. During --full,
                # the fix is emitted as a later patch record with a DELE marker, preserving the chain.
                if sev in ("ERROR", "WARN"):
                    sev = "INFO"                 # nothing to do about it: it never plays, and stays harmless
                msg += " (left in place outside --full: other plugins have this response too)"
                op = desc = None
            r.add(sev, info.source, t.name, info.id, msg, fix=(info.off, op, desc) if op else None,
                  related_plugins=related_plugins)

        DELETE = ("delete",)
        never = "deleted response: its filter names something that doesn't exist, so it can never play"

        # speaker filters
        if info.actor and not self._exists(ACTOR_TAGS, info.actor):
            add("ERROR", f"speaker ID (ONAM) '{info.actor}' is not an NPC or creature", DELETE, never,
                related_plugins=self._bug_sources(info.actor))
        if info.race and not self._exists({"RACE"}, info.race):
            add("ERROR", f"race filter '{info.race}' does not exist", DELETE, never,
                related_plugins=self._bug_sources(info.race))
        if info.cls and not self._exists({"CLAS"}, info.cls):
            add("ERROR", f"class filter '{info.cls}' does not exist", DELETE, never,
                related_plugins=self._bug_sources(info.cls))
        if info.faction and info.faction.upper() != "FFFF" and not self._exists({"FACT"}, info.faction):
            add("ERROR", f"faction filter '{info.faction}' does not exist", DELETE, never,
                related_plugins=self._bug_sources(info.faction))
        if info.pcfaction and info.pcfaction.upper() != "FFFF" and not self._exists({"FACT"}, info.pcfaction):
            add("ERROR", f"PC faction filter '{info.pcfaction}' does not exist", DELETE, never,
                related_plugins=self._bug_sources(info.pcfaction))
        if info.cell and info.cell.lower() not in self.cell_names:
            if info.cell.lower() in self.region_names:
                add("INFO", f"cell filter '{info.cell}' is a region name, not a cell; "
                            f"the game may warn 'Cell not found' at load")
            else:
                add("WARN", f"cell filter '{info.cell}' does not name an existing cell; "
                            f"the game warns 'Cell not found' at load")
        if info.rank != -1 and not info.faction:
            add("INFO", f"rank filter {info.rank} is set but there is no faction filter (checks the speaker's own faction)")
        if info.sex not in (-1, 0, 1):
            add("ERROR", f"sex filter has invalid value {info.sex}")
        if t.dtype != JOURNAL and not (0 <= info.disp <= 100):
            add("WARN", f"disposition filter {info.disp} is outside 0-100")
        if t.dtype != JOURNAL and (info.qstn or info.qstf or info.qstr):
            for flag, on in (("QSTN", info.qstn), ("QSTF", info.qstf), ("QSTR", info.qstr)):
                if on:
                    add("ERROR", f"quest flag {flag} set on a non-journal response",
                        ("drop_sub", flag), f"removed the {flag} quest flag (it does nothing outside a journal)")

        # conditions
        for raw, value in info.conditions:
            self._check_condition(info, raw, value, add)

        # result script
        if info.result:
            self._check_result(info.result, quest_stages, add)

        # voice / sound file
        if info.sound and sound_files is not None:
            p = ("sound/" + info.sound.replace("\\", "/")).lower()
            if p not in sound_files:
                rel = os.path.join("Sound", info.sound.replace("\\", os.sep).replace("/", os.sep))
                self.generated.append((rel, f"dialogue voice in [{t.name}] ({info.source})", None, info.source))
                add("ERROR", f"sound file 'Sound\\{info.sound}' not found in Data Files or any loaded BSA",
                    None)
            else:
                bad = sound_file_problem(p.replace("/", os.sep))
                if bad:
                    add("ERROR", f"sound file 'Sound\\{info.sound}' {bad}: the game can freeze or crash playing it",
                        ("drop_sub", "SNAM"), "stopped it using the broken sound file; the text still shows")

        if not info.text.strip() and t.dtype in (0, 2, 3) and not info.result.strip():
            add("INFO", "response has no text and no result script")
        if t.dtype == JOURNAL and info.qstn and not info.text.strip():
            add("ERROR", "the quest's name entry has no text (the game logs 'QuestName info in Journal ... has no "
                         "name string', and the quest has no title in the journal)",
                ("set_text", t.name), f"gave the quest its topic's name, '{t.name}'")


def resolve(args, report):
    """Return (list of (name, path) in load order, list of BSA paths)."""
    data_dirs = [args.data_dir]
    plugin_names, bsa_names = None, None
    preserve_order = False

    if args.plugins:
        plugin_names, preserve_order = list(args.plugins), True
        if getattr(args, "init_scope", False):
            bsa_names = ["Morrowind.bsa"]
    elif args.openmw_cfg:
        plugin_names, bsa_names, preserve_order = [], [], True
        with open(args.openmw_cfg, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if "=" not in s or s.startswith("#"):
                    continue
                k, v = (x.strip() for x in s.split("=", 1))
                v = v.strip('"')
                if k == "content":
                    plugin_names.append(v)
                elif k == "data":
                    data_dirs.append(os.path.expandvars(os.path.expanduser(v)))
                elif k == "fallback-archive":
                    bsa_names.append(v)
        if "morrowind.bsa" not in (b.lower() for b in bsa_names):
            bsa_names.insert(0, "Morrowind.bsa")
    else:
        ini = default_ini(args)
        plugin_names = read_load_order(args)
        if plugin_names is not None:
            sec = read_ini_sections(ini) if os.path.isfile(ini) else {}
            bsa_names = ["Morrowind.bsa"] + [v for k, v in sec.get("archives", []) if k.lower().startswith("archive")]
            print(f"Load order from {LOAD_ORDER_FILE}", file=sys.stderr)

    lookup = {}
    for d in reversed(data_dirs):   # later data= folders win in OpenMW; first dir is primary
        lookup.update(index_dir(d))

    if plugin_names is None:
        plugin_paths = [p for n, p in lookup.items() if n.endswith((".esm", ".esp", ".omwaddon", ".omwgame"))]
        bsa_paths = [p for n, p in lookup.items() if n.endswith(".bsa")]
    else:
        plugin_paths, bsa_paths = [], []
        total, _cut = game_files_overflow(plugin_names)
        for i, n in enumerate(plugin_names):
            p = lookup.get(n.lower())
            if p:
                plugin_paths.append(p)
                continue
            longer = [k for k in lookup if k.startswith(n.lower()) and k.endswith((".esp", ".esm"))]
            if i == len(plugin_names) - 1 and total >= INI_SECTION_LIMIT - 64 and len(longer) == 1:
                report.add("ERROR", n, "", "",
                           f"listed in the load order, but the name is cut off (the file is "
                           f"'{os.path.basename(lookup[longer[0]])}'): the [Game Files] list reached Windows' "
                           f"{INI_SECTION_LIMIT:,}-character ini section limit, so Vortex cut it here and dropped "
                           f"every plugin after it. Use fewer plugins (--auto-merge), then --add-all-esp")
            else:
                report.add("ERROR", n, "", "", "listed in the load order but not found in Data Files")
        for n in bsa_names or []:
            p = lookup.get(n.lower())
            if p:
                bsa_paths.append(p)

    if not preserve_order:
        plugin_paths = sort_vanilla(plugin_paths)
    return [(os.path.basename(p), p) for p in plugin_paths], bsa_paths, data_dirs




















def content_rules(plugins):
    """Patches load after the mod they patch, judged by content: when most of the records an .esp
    changes (leaving out anything a master .esm defines) are records another, much bigger .esp adds,
    the smaller one is a patch for the bigger one and must load after it. Returns rules in the [Order] form: [("order", [mod, patch], why)]."""
    owners = defaultdict(list)                 # record id -> [plugin index]
    counts = []
    ours = {x.lower() for x in OUR_PATCHES} | {AUTO_MERGE_NAME.lower()} | {
        k.lower() for k in read_merge_record(os.path.dirname(os.path.dirname(os.path.abspath(plugins[0][1]))))
    } if plugins else set()
    esps = [(n, p) for n, p in plugins if n.lower().endswith(".esp") and n.lower() not in ours]
    # Records a master (.esm) defines are overridden by many unrelated mods (two NPC replacers both
    # change vanilla NPCs); only records an .esp itself adds say "this one patches that one".
    esm_ids = set()
    esm_plugins = [(n, p) for n, p in plugins if n.lower().endswith(".esm")]
    for index, (n, p) in enumerate(esm_plugins, 1):
        print(f"MWMANAGER-STAGE: Sorting — indexing master {index}/{len(esm_plugins)}: {n}",
              file=sys.stderr, flush=True)
        try:
            with open(p, "rb") as f:
                data = f.read()
            for off, tag, flags, body in iter_records(data):
                if tag not in CONTENT_SKIP_TAGS:
                    rid = record_id(tag, body)
                    if rid is not None:
                        esm_ids.add(rid)
        except (OSError, FormatError):
            pass
    for i, (n, p) in enumerate(esps):
        print(f"MWMANAGER-STAGE: Sorting — indexing plugin {i + 1}/{len(esps)}: {n}",
              file=sys.stderr, flush=True)
        ids = set()
        try:
            with open(p, "rb") as f:
                data = f.read()
            for off, tag, flags, body in iter_records(data):
                if tag in CONTENT_SKIP_TAGS:
                    continue
                rid = record_id(tag, body)
                if rid is not None and rid not in esm_ids:
                    ids.add(rid)
        except (OSError, FormatError):
            pass
        counts.append(len(ids))
        for rid in ids:
            owners[rid].append(i)
    shared = Counter()
    for rid, who in owners.items():
        if 1 < len(who) <= 6:                  # records many plugins touch say nothing about pairs
            for a in who:
                for b in who:
                    if a != b:
                        shared[(a, b)] += 1
    rules = []
    masters = {}
    for a_b, k in shared.items():
        x, y = a_b                            # x shares k records with y
        if counts[x] and k >= max(1, 0.5 * counts[x]) and k < 0.5 * counts[y] and counts[y] > 2 * counts[x]:
            xn, yn = esps[x][0], esps[y][0]
            if xn not in masters:
                try:
                    masters[xn] = {m.lower() for m in read_header_masters(esps[x][1])}
                except (OSError, FormatError):
                    masters[xn] = set()
            if yn.lower() in masters[xn]:
                continue                       # already a master: the hard rule covers it
            rules.append(("order", [yn, xn], f"content: {k} of {xn}'s {counts[x]} records are {yn}'s"))
    return rules






def full_check(args, game_dir, stage):
    """The plain scan, like double-clicking the batch file: report to outputs/mwmanager_report.txt. If it has any
    ERROR, open it and say so. Returns the number of errors."""
    report_path = output_file("mwmanager_report.txt")
    print(f"\n--full: checking for errors after {stage} (report: outputs\\mwmanager_report.txt)...")
    import contextlib
    check_plugins = list(getattr(args, "plugins", None) or read_load_order(args) or [])
    lookup = index_dir(args.data_dir)
    # --fix stores master repairs in this generated patch. Validate the effective load order with
    # that patch immediately after the ESMs, even before the user activates it in the game launcher.
    if PATCH_NAME.lower() in lookup and PATCH_NAME.lower() not in {n.lower() for n in check_plugins}:
        pos = 0
        while pos < len(check_plugins) and check_plugins[pos].lower().endswith(".esm"):
            pos += 1
        check_plugins.insert(pos, PATCH_NAME)
    scan_args = [args.data_dir]
    if check_plugins:
        scan_args += ["--plugins", *check_plugins]
    if args.ini:
        scan_args += ["--ini", args.ini]
    with open(report_path, "w", encoding="utf-8", errors="replace") as f, contextlib.redirect_stdout(f):
        main(scan_args)
    errors = 0
    with open(report_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.lstrip().startswith("ERROR"):
                errors += 1
    if errors:
        print(f"--full: {errors} error(s) left after {stage}; stopping here so nothing builds on them.")
        if os.environ.get("MWMANAGER_GUI") == "1":
            print(f"MWManager will display {report_path} in its report viewer.")
        else:
            try:
                subprocess.Popen(["notepad", report_path])
            except OSError:
                print(f"Open {report_path} to see them.")
    else:
        print("--full: no errors.")
    return errors


def run_full(args):
    """--full, from scratch:
        turn back on what earlier runs turned off (and drop their conflict rules and asset stand-ins),
        undo the auto-merge, turn off redundant plugins (asking in a console; --no-redundant skips it; the
        choices, in mwdialog_redundant.txt, are kept), report cell conflicts, sort, set the old Multipatch aside, clean and fix with
        the asset check, sort again;
        stop and open the report if any error is left; otherwise auto-merge, sort, --all, sort again;
        stop and open the report if any error is left; otherwise offer the interactive --cell-conflicts.
    It doesn't start at all while a plugin's master is missing from Data Files."""
    data_dir = os.path.abspath(args.data_dir)
    game_dir = os.path.dirname(data_dir)
    if not getattr(args, "init", False):
        audit_core_files(data_dir, game_dir, db_path=getattr(args, "catalog_db", None))
        warn_mcp_beta(game_dir, show_ui=not args.dry_run)
        offer_mwse_update(game_dir, dry_run=args.dry_run, yes=args.yes)
    set_title("mwmanager --full - checking masters")
    missing = full_missing_masters(args, game_dir)
    if missing:
        set_title("mwmanager --full - stopped: missing masters")
        print("\n--full: ERROR - plugins whose masters aren't in Data Files. Install the masters, or take these "
              "plugins out of the load order, then run --full again:\n")
        for n, mname in missing:
            print(f"  - {n}  needs  {mname}")
        return 2
    tr_mainland_deployed = any(path.name.casefold() == "tr_mainland.esm"
                               for path in Path(data_dir).glob("*.esm"))
    if tr_mainland_deployed and any(Path(name).name.casefold() == "tr_mainland.esm"
                                    for name in read_load_order(args)):
        from core_addons import apply_plugin_replacement_overrides
        for note in apply_plugin_replacement_overrides(
                "TR_Mainland.esm", data_dir, MANAGER_DIR, dry_run=args.dry_run):
            print(f"--full: official add-on override: {note}")
    # your remembered --cell-conflicts answers are replayed by default (the rebuilt-interior winners and the
    # [Order] rules come back before --fix builds the Cell Fixes patch); --fresh-answers starts clean
    if not getattr(args, "fresh_answers", False) and not getattr(args, "use_answers", False) \
            and read_conflict_answers(game_dir):
        args.use_answers = True
        print(f"--full: replaying your {len(read_conflict_answers(game_dir))} remembered conflict answer(s) from "
              f"{CONFLICT_ANSWERS_FILE} (--fresh-answers to start without them)")
    common = ["--fix-log", args.fix_log, "--encoding", args.encoding, "--no-interactive", "--yes",
              "--generate-missing-assets"]
    if getattr(args, "init_scope", False):
        # Keep every internal full-pass subprocess scoped to the --init plugin list and base-game archive.
        common += ["--init-scope", "--plugins", *args.plugins]
    for flag in ("dry_run", "no_sounds", "no_mlox_rules", "no_content_order", "no_vortex", "no_doubled",
                 "no_performers", "no_placeholders", "no_merge_objects"):
        if getattr(args, flag, False):
            common.append("--" + flag.replace("_", "-"))
    if getattr(args, "collide_radius", COLLIDE_ANY_RADIUS) != COLLIDE_ANY_RADIUS:
        common += ["--collide-radius", f"{args.collide_radius:g}"]
    if args.ini:
        common += ["--ini", args.ini]
    if args.vortex_dir:
        common += ["--vortex-dir", args.vortex_dir]
    first = []
    if read_merge_record(game_dir):
        first.append(("undo the auto-merge", ["--unmerge", "--no-follow-up"]))
    first.append(("turn back on what earlier runs turned off", None))
    if args.add_all_esp:
        first.append(("add every plugin in Data Files", ["--add-all-esp"]))
    if not getattr(args, "no_redundant", False):
        # early: once the plugin list is whole (merges undone, masters checked), before anything reads the
        # plugins - a plugin turned off here is not cleaned, fixed, merged, patched or asset-checked
        first.append(full_redundant_step(args, common))
    first += [
        full_conflict_step(args, common),
        ("sort", ["--sort", "--no-follow-up"]),
        # The Multipatch is built once, after the auto-merge (which ignores this tool's patches): before it,
        # the old one is set aside and only clean + fix (+ the asset check) run on the unmerged order.
        ("set the old Multipatch aside (rebuilt after the auto-merge)", "multipatch_aside"),
        ("clean and fix with the asset check (no Multipatch yet)", ["--clean", "--fix", "--asset-check"]),
        # the fixes can change the plugin list (a plugin turned back on or off): sort again, without the
        # follow-up (it would rebuild the patches, which the second half does)
        ("sort again (after the fixes)", ["--sort", "--no-follow-up"]),
    ]
    second = [
        ("auto-merge", ["--auto-merge", "--no-follow-up"]),
        ("sort (merged)", ["--sort", "--no-follow-up"]),
        ("--all (merged)", ["--all"]),
        ("sort again (after --all's changes)", ["--sort"]),
    ]
    if args.dry_run:
        print("--full --dry-run: each step only shows its plan, so later steps see the load order as it is now.")
    global IN_FULL
    IN_FULL = True
    done, stopped = [], None
    total = len(first) + len(second)
    try:
        for phase, steps in ((1, first), (2, second)):
            for what, extra in steps:
                n = len(done) + 1
                set_title(f"mwmanager --full - step {n}/{total}: {what}")
                print("\n" + "#" * 70 + f"\n# --full step {n}/{total}: {what}"
                      + (f"\n#   mwmanager {' '.join(extra)}" if isinstance(extra, list) else "")
                      + "\n" + "#" * 70)
                start = time.time()
                try:
                    if extra is None:
                        rc = full_reset(args, game_dir)
                    elif extra == "multipatch_aside":
                        rc = multipatch_aside(args, game_dir)
                    elif extra and extra[0] in ("--redundant", "--redundant-auto", "--cell-conflicts"):
                        rc = main([args.data_dir] + extra)       # carries its own copy of the common flags
                    else:
                        rc = main([args.data_dir] + extra + common)
                except KeyboardInterrupt:
                    stopped = "stopped (Ctrl+C)"
                    break
                rc = rc or 0
                done.append((what, rc, time.time() - start))
                if rc not in (0, 1):
                    stopped = f"'{what}' failed (exit {rc})"
                    break
            if stopped:
                break
            if phase == 2 and not args.dry_run:
                # The ESM repair patch must be active in MWManager's independent load order for the
                # repaired records to take effect when the user launches through MWManager.
                order_args = copy.copy(args)
                order_args.plugins = None
                working_order = list(read_load_order(order_args) or [])
                if PATCH_NAME.lower() in index_dir(args.data_dir):
                    working_order = [n for n in working_order if n.lower() != PATCH_NAME.lower()]
                    pos = 0
                    while pos < len(working_order) and working_order[pos].lower().endswith(".esm"):
                        pos += 1
                    working_order.insert(pos, PATCH_NAME)
                    write_load_order(game_dir, working_order, "mwmanager --full (ESM repair patch)")
                    print(f"--full: added {PATCH_NAME} after the core masters in {LOAD_ORDER_FILE}")
            if not args.dry_run:
                set_title(f"mwmanager --full - checking for errors ({'before' if phase == 1 else 'after'} the auto-merge)")
                errs = full_check(args, game_dir, "the unmerged load order" if phase == 1 else "the auto-merge")
                if errs:
                    stopped = f"{errs} error(s) after {'the unmerged load order' if phase == 1 else 'the auto-merge'}"
                    break
    finally:
        IN_FULL = False
    set_title("mwmanager --full - " + (f"stopped: {stopped}" if stopped else "finished"))
    print("\n" + "=" * 70 + "\n--full summary\n" + "=" * 70)
    for what, rc, secs in done:
        state = "ok" if rc == 0 else "issues reported" if rc == 1 else str(rc)
        print(f"  {what:62} {state:16} {secs:6.0f}s")
    try:
        rec = read_merge_record(game_dir)
        total = sum(len(v) for v in rec.values())
        if rec:
            print("\n  Auto-merge: " + "; ".join(f"{len(v)} plugin(s) merged into {k}" for k, v in rec.items())
                  + (f" ({total} in all)" if len(rec) > 1 else ""))
        cur = read_load_order(args) or []
        print(f"  Active plugins now: {len(cur)}" + (f" ({len(cur) + total - len(rec)} before merging)" if rec else ""))
    except (OSError, ValueError):
        pass
    if stopped:
        print(f"\n--full stopped: {stopped}. Fix what the report shows, then run --full again.")
        return 2
    if args.dry_run:
        return 0
    print("\n--full finished with no errors.")
    try:
        new_pairs = [l for l in open(output_file(NEW_CONFLICTS_FILE), encoding="utf-8").read().split("\n") if l]
    except OSError:
        new_pairs = []
    if new_pairs:
        print(f"\n{len(new_pairs)} new conflict(s) have no remembered answer yet: "
              + ", ".join(p_.replace("|", " vs ") for p_ in new_pairs[:5]) + (" ..." if len(new_pairs) > 5 else ""))
        if not args.yes and sys.stdin.isatty():
            try:
                ans = input("Answer them now (only the new ones are asked)? [Y/n] ").strip().lower()
            except EOFError:
                ans = "n"
            if ans in ("", "y", "yes"):
                set_title("mwmanager --full - the new conflicts")
                main([args.data_dir, "--cell-conflicts", "--only-new"] + (["--ini", args.ini] if args.ini else []))
                new_pairs = None                         # answered: don't offer the full session too
        else:
            print("Answer them with:  mwmanager --cell-conflicts --only-new")
    if new_pairs is not None and not args.yes and sys.stdin.isatty():
        try:
            ans = input("Run --cell-conflicts now, to choose rules or turn plugins off conflict by conflict? [y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() in ("y", "yes"):
            set_title("mwmanager --full - interactive --cell-conflicts")
            main([args.data_dir, "--cell-conflicts"] + (["--ini", args.ini] if args.ini else []))
    if args.play:
        print("\n--full: starting the game")
        return main([args.data_dir, "--play"] + (["--play-anyway"] if args.play_anyway else [])
                    + (["--ini", args.ini] if args.ini else []))
    return 0


def _main(argv=None):
    global ENC
    ap = MT.parser(description="Scan a Morrowind Data Files folder for dialogue errors.")
    ap.add_argument("data_dir", help='path to the "Data Files" folder')
    ap.add_argument("--downloads-dir", default=os.environ.get("MWMANAGER_DOWNLOADS_DIR"), help="Vortex downloads folder scanned during --init for core community masters (default: Vortex's own download folder)")
    ap.add_argument("--core-master-list", action="store_true", help="list the editable major-master priority list")
    ap.add_argument("--core-master-add", metavar="FILE.esm", help="add a major master to the editable priority list")
    ap.add_argument("--core-master-keywords", nargs="+", metavar="WORD", help="download-name keywords for --core-master-add")
    ap.add_argument("--core-master-remove", metavar="FILE.esm", help="remove a major master from the editable priority list")
    ap.add_argument("--plugins", nargs="+", metavar="FILE", help="explicit load order (file names in Data Files)")
    ap.add_argument("--init", action="store_true",
                    help="initialize core ESMs first, process shipped ESPs, then required XE Sky Variations, refreshing the catalog at each stage before major masters (texconv/FFmpeg in MWManager\\tools or PATH)")
    ap.add_argument("--catalog-db", metavar="PATH", help="with --init: SQLite catalog path (defaults to the manager project folder)")
    ap.add_argument("--init-scope", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--ini", help="Morrowind.ini path (used for archives and --init canonical registrations)")
    ap.add_argument("--openmw-cfg", help="deprecated; OpenMW installations are not supported")
    ap.add_argument("--only", nargs="+", metavar="FILE", help="report (and fix) only issues coming from these plugins")
    ap.add_argument("--min-severity", choices=SEVERITIES, default="WARN", help="lowest severity to report (default WARN; use INFO to also see harmless notes)")
    ap.add_argument("--no-sounds", action="store_true", help="skip checking that voice/sound files exist")
    ap.add_argument("--csv", metavar="PATH", help="also write the report to a CSV file")
    ap.add_argument("--encoding", default="cp1252", help="text encoding of plugin strings (cp1252 for English, cp1251 Russian, cp1250 Polish)")
    fx = ap.add_argument_group("fixing (ERRORs only; plugin backups go to the MWManager backups folder; .esm files are never changed)")
    fx.add_argument("--fix", action="store_true",
                    help="after the report, show a fix plan and apply it after you confirm; .esp files are fixed "
                         "in place, masters (.esm) through the patch plugin '" + PATCH_NAME + "'")
    fx.add_argument("--dry-run", action="store_true", help="show planned actions without changing files (including --init and --full)")
    fx.add_argument("--yes", action="store_true", help="with --fix: apply without asking")
    fx.add_argument("--generate-missing-assets", action="store_true", help=argparse.SUPPRESS)
    fx.add_argument("--patch-esm", action="store_true",
                    help="same as --fix (kept for older batch notes)")
    fx.add_argument("--restore", nargs="+", metavar="FILE", help="undo: put each plugin's newest MWManager backup back (keeps the file date) and exit")
    tc = ap.add_argument_group("tes3cmd features (in place, .bak first, file dates kept)")
    tc.add_argument("--multipatch", action="store_true",
                    help=f"build '{MULTI_PATCH_NAME}' like 'tes3cmd multipatch': merged leveled lists, "
                         f"fog-bugged cells, reverted cell names, plus objects several mods change merged field "
                         f"by field (load it last)")
    tc.add_argument("--disable-mismatched", action="store_true",
                    help="take plugins made for a different master than the installed one (mwdialog_mastercheck: "
                         "references that don't resolve, original header size) out of the load order")
    tc.add_argument("--mismatch-threshold", type=float, default=None, metavar="0-1",
                    help="confidence from which a master counts as a different mod (default 0.75)")
    tc.add_argument("--vortex-downloads", "--downloads", default=None, dest="vortex_downloads", metavar="DIR",
                    help="Vortex download folder, to read a plugin's original header from its download archive "
                         "when --fix has rewritten it (default: Vortex's own setting)")
    tc.add_argument("--no-archive-lookup", action="store_true", help="don't open download archives for the master check")
    tc.add_argument("--no-master-guard", action="store_true", help="skip the master mismatch check")
    tc.add_argument("--longpaths-undo", action="store_true",
                    help="put back every file --fix or vortex_tidy moved to a short path (mwdialog_longpaths.json) "
                         "and the paths in plugins and models; the next --fix rebuilds the ESM patch without them")
    tc.add_argument("--restore-unused", action="store_true",
                    help="put back the unused textures/sounds --asset-check --fix moved to Off\\unused\\")
    tc.add_argument("--repair-sounds", action="store_true",
                    help="re-save sound files the active plugins play that the game can hang or crash on (24/32-bit "
                         "or odd-codec WAVs, MP3s with damaged or huge tags), same names, no plugin changed; needs "
                         "ffmpeg; originals go to <mod folder>\\Off\\<plugin>\\<path>")
    tc.add_argument("--no-merge-objects", action="store_true",
                    help="with --multipatch: don't merge objects several mods change (by default the Multipatch "
                         "combines what each changed in an NPC, creature, item or spell, like TES3Merge)")
    tc.add_argument("--all", action="store_true", help="--clean, then --fix, then --multipatch")
    tc.add_argument("--full", action="store_true",
                    help="everything, from scratch. Aborts first if a plugin's master isn't in Data Files. Then: "
                         "turn back on what earlier runs turned off (and drop their conflict rules and asset "
                         "stand-ins), --unmerge, --cell-conflicts (report), --sort, --all --asset-check; if any "
                         "ERROR is left, stop and open outputs\\mwmanager_report.txt; else --auto-merge, --sort, --all; if "
                         "any ERROR is left, stop and open the report; else offer the interactive "
                         "--cell-conflicts. --yes: no questions; --add-all-esp: add every plugin first; --play: "
                         "start the game at the end")
    tc.add_argument("--header", nargs="+", metavar="FILE",
                    help="show a plugin's Author and Description, or set them with --set-author/--set-description")
    tc.add_argument("--set-author", metavar="TEXT", help="with --header: new Author ('+text' appends)")
    tc.add_argument("--set-description", metavar="TEXT",
                    help="with --header: new Description ('+text' appends; \\n starts a new line)")
    lo = ap.add_argument_group("load order (sets plugin dates; masters first, then mlox-style rules)")
    lo.add_argument("--sort", action="store_true",
                    help=f"sort the active plugins: masters before the plugins using them, then the rules in "
                         f"{RULES_FILE} and mlox's mlox_user.txt/mlox_base.txt; dates are set one day apart")
    lo.add_argument("--no-mlox-rules", action="store_true",
                    help=f"with --init/--sort: don't import or use community mlox rules; use only {RULES_FILE}")
    lo.add_argument("--update-rules", action="store_true",
                    help="remove the steps of mlox's rules (Morrowind\\mlox\\mlox_user.txt, mlox_base.txt) that the "
                         "sort skips because a master, your rules, a context rule or an earlier mlox rule says "
                         "otherwise; --dry-run shows the plan and the reason for each. Your own rules file is "
                         "only reported, never edited")
    lo.add_argument("--bug-reports", action="store_true",
                    help="write <plugin>.bugreport.txt for plugins with problems of their own (also done by "
                         "--fix and --asset-check)")
    lo.add_argument("--no-bug-reports", action="store_true", help="don't write <plugin>.bugreport.txt files")
    lo.add_argument("--no-add-masters", action="store_true",
                    help="--sort: don't add a mod as a master to the patch that changes its records (keep "
                         "only the [Context] order rule)")
    lo.add_argument("--no-content-order", action="store_true",
                    help="with --sort: don't add the automatic patch-after-mod rules (a small .esp most of whose "
                         "records are another .esp's loads after it)")
    lo.add_argument("--restore-dates", action="store_true", help="undo the last --sort (put the old dates back)")
    lo.add_argument("--add-all-esp", action="store_true",
                    help=f"activate every .esm/.esp in Data Files in {LOAD_ORDER_FILE} (masters first, then by date; "
                         "MGE XE grass plugins left out; the old ini is backed up). Use --dry-run to preview")
    mg = ap.add_argument_group("merging")
    mg.add_argument("--merge", nargs="+", metavar=("OUT.esp", "INPUT"),
                    help="merge plugins into one: --merge \"Merged.esp\" FILE_OR_FOLDER... (written beside the "
                         "inputs' originals - for files deployed into Data Files, their Vortex mod folder - unless "
                         "the name includes a path). Inputs whose "
                         "masters aren't active, or whose dialogue refers to things no installed mod defines, "
                         "are left out (their target mod isn't installed). Later inputs win conflicts")
    mg.add_argument("--auto-merge", action="store_true",
                    help=f"find active .esp plugins with no cell/land/pathgrid edits that can move "
                         f"together without changing what wins against any other plugin, merge them into one "
                         f"('{AUTO_MERGE_NAME}', or --merge-name), and deactivate them (recorded in {MERGE_RECORD}; "
                         f"--add-all-esp leaves them out). Plugins listed one per line in "
                         f"mwmanager_auto-merge.blacklist.txt are never merged. Use --dry-run first")
    mg.add_argument("--merge-name", metavar="NAME.esp", help="with --auto-merge: the merged plugin's name")
    mg.add_argument("--merge-plan", action="store_true",
                    help="recommend merges --auto-merge doesn't make (plugins with cells, land or "
                         "pathgrids), grouped by kind; read-only: writes outputs\\mwmanager_merge_plan.txt and "
                         ".json. Merges that place objects in cells need a new game")
    mg.add_argument("--plan-size", type=int, default=8, metavar="N",
                    help="with --merge-plan: at most N plugins per merge (small patches aren't capped)")
    mg.add_argument("--no-follow-up", "--no-follow-fix", dest="no_follow_fix", action="store_true",
                    help="don't run the usual follow-ups: --fix --multipatch after --auto-merge or a --sort "
                         "that changed the order, --add-all-esp --sort after --unmerge, and --fix's own re-scans "
                         "for follow-on fixes (one final check still runs)")
    mg.add_argument("--unmerge", nargs="*", metavar="NAME.esp",
                    help="undo an --auto-merge: reactivate the plugins it replaced and remove the merged plugin "
                         "(all recorded merges if no name is given)")
    mg.add_argument("--merge-all", action="store_true", help="with --merge: include every input anyway")
    mg.add_argument("--merge-with", nargs="+", metavar="PLUGIN",
                    help="with --merge: plugins (not merged) that count as installed when checking the inputs, "
                         "e.g. the base mod the patches are for, if it isn't active yet")
    gm = ap.add_argument_group("game")
    gm.add_argument("--play", action="store_true",
                    help="start Morrowind.exe directly, skipping the Morrowind Launcher (after any other steps "
                         "given, e.g. --sort --play). Sets SteamAppId so Steam doesn't redirect to the Launcher")
    gm.add_argument("--steam-appid", default=STEAM_APPID, help=f"with --play: Steam app id (default {STEAM_APPID})")
    gm.add_argument("--play-anyway", action="store_true",
                    help="with --play: start the game even if a plugin's master is missing or loads after it")
    gm.add_argument("--exe", default="Morrowind.exe", help="with --play: the game executable (default Morrowind.exe)")
    tc.add_argument("--cell-conflicts", action="store_true",
                    help="report plugins that don't know about each other but build in the same spot, change the "
                         "same placed objects, or change the same cell settings; in a console it then asks, "
                         "conflict by conflict: an [Order] rule, turn one off, or for an interior both rebuilt, "
                         "whose version of the place wins (" + CELL_WINNERS_FILE + "; --fix builds "
                         + CELL_WINNER_PATCH + ")")
    tc.add_argument("--only-new", action="store_true",
                    help="with --cell-conflicts: replay every remembered answer and ask only about the conflicts "
                         "that have none yet")
    tc.add_argument("--only-incompatible", action="store_true",
                    help="with --cell-conflicts: ask only about incompatible plugins that require a choice")
    tc.add_argument("--defer-follow-up", action="store_true",
                    help="with --cell-conflicts: save answers without launching a follow-up; the calling workflow "
                         "will sort and rebuild patches")
    tc.add_argument("--fresh-answers", action="store_true",
                    help="with --full: don't replay the answers remembered in " + CONFLICT_ANSWERS_FILE +
                         " (by default --full replays them, so your rebuilt-interior winners and [Order] choices "
                         "stand); the conflicts are only reported and you are asked at the end")
    tc.add_argument("--use-answers", action="store_true",
                    help="with --cell-conflicts: no questions, take every answer remembered in "
                         + CONFLICT_ANSWERS_FILE + " (a pair with none is skipped), then run what it needs "
                         "(--sort, and --fix --multipatch if a plugin is turned off or a place's winner chosen). "
                         "With --full: keeps " + CELL_WINNERS_FILE + " and replays the answers in its conflict step")
    tc.add_argument("--conflict-min", type=int, default=5, metavar="N",
                    help="with --cell-conflicts: a plugin must add at least N new objects to a cell to count (default 5)")
    tc.add_argument("--conflict-top", type=int, default=40, metavar="N",
                    help="with --cell-conflicts: print the N most serious pairs in the summary (default 40); "
                         "the questions still go through every pair")
    tc.add_argument("--conflict-all", action="store_true", help="with --cell-conflicts: show every pair")
    tc.add_argument("--conflict-all-places", action="store_true",
                    help="--cell-conflicts: also list pairs that only place objects near each other "
                         "(left to mwcollide, not asked about)")
    tc.add_argument("--collide-radius", type=float, default=COLLIDE_ANY_RADIUS, metavar="UNITS",
                    help="with --cell-conflicts: for the mwcollide MWSE mod, hide anything of the earlier of two "
                         f"unrelated mods within this many units of something of the later one's (default "
                         f"{COLLIDE_ANY_RADIUS:g}; 0: only the same object or model, within {COLLIDE_RADIUS:g})")
    tc.add_argument("--no-interactive", action="store_true",
                    help="with --cell-conflicts: only report, don't ask what to do about each conflict "
                         "(also the case when the output goes to a file)")
    tc.add_argument("--vortex-dir", metavar="PATH",
                    help="Vortex's mod staging folder, searched for files that exist there but were never deployed "
                         "(default: Vortex's own setting)")
    tc.add_argument("--no-vortex", action="store_true", help="don't search the Vortex mod folders")
    tc.add_argument("--restore-textures", action="store_true",
                    help=f"put back the full-size textures --asset-check --fix reduced to {TEXTURE_MAX_EDGE} (see {REDUCED_RECORD})")
    tc.add_argument("--remove-generated", action="store_true",
                    help=f"delete the silent stand-in sound/music files --fix created (see {GENERATED_RECORD})")
    tc.add_argument("--asset-check", action="store_true",
                    help="also check the files records, scripts and Morrowind.ini name (models and their textures, "
                         "icons, sounds, music, videos, fonts), search Vortex mod folders and .Orphaned for missing "
                         "ones, and with --fix put them back or use stand-ins (adds a few minutes)")
    tc.add_argument("--no-models", action="store_true", help=argparse.SUPPRESS)   # the old opt-out; now the default
    tc.add_argument("--performers-patch", action="store_true",
                    help="with --fix: delete doubled bards with mwmanager Performers Patch.esp, instead of "
                         "leaving them to the mwperformers MWSE mod (which hides them in the game)")
    tc.add_argument("--no-performers", action="store_true",
                    help="with --fix: don't remove doubled bards (two plugins' performers in one inn)")
    tc.add_argument("--no-doubled", action="store_true",
                    help="with --fix: don't look for (or turn off) plugins that place the same objects twice")
    tc.add_argument("--reenable", nargs="*", metavar="NAME",
                    help=f"turn back on plugins --fix turned off as doubled (all of them with no name; see {DISABLED_RECORD})")
    tc.add_argument("--redundant", action="store_true",
                    help="find plugins doing the same job as another (nothing of theirs would be missed, or two "
                         "versions of one mod), describe each and how they overlap, and ask which to keep "
                         f"(report only when not in a console, or with --no-interactive; remembered in {REDUNDANT_RECORD})")
    tc.add_argument("--redundant-auto", action="store_true",
                    help="--redundant, turning off the high-confidence ones (an .esp that changes nothing in the "
                         "game and that nothing needs) without asking; two versions of one mod are still only asked about")
    tc.add_argument("--no-redundant", action="store_true",
                    help="with --full: skip the redundant-plugins step (by default it runs early, so plugins doing "
                         "nothing another doesn't already do are turned off before anything else reads them)")
    tc.add_argument("--redundant-undo", nargs="*", metavar="NAME",
                    help="turn back on plugins --redundant turned off (all of them, and forget the keep-both "
                         "choices, with no name)")
    tc.add_argument("--trace", nargs="+", metavar="ID",
                    help="follow object IDs through the load order: who defines, places, deletes or scripts them")
    tc.add_argument("--clean", action="store_true",
                    help="clean .esp plugins like 'tes3cmd clean': duplicate object instances, redundant AMBI/WHGT, "
                         "records duplicated from masters, Evil GMSTs, junk cells")
    fx.add_argument("--no-placeholders", action="store_true",
                    help="with --fix: don't add stand-in gold coins for objects that scripts name but no mod defines")
    fx.add_argument("--restore-all", action="store_true",
                    help="undo everything: put every plugin back to its oldest .bak (the original), back up what it replaces, and exit")
    fx.add_argument("--clean-up", action="store_true",
                    help="delete the .bak backups between each installed plugin's oldest and newest "
                         "(use --dry-run to preview)")
    fx.add_argument("--fix-log", default=os.path.join(OUTPUT_DIR, "mwmanager_fixes.log"), help="where to append a record of fixes (default: MWManager outputs folder)")
    import mwmanager_journal
    jg = ap.add_argument_group("install journal (the game folder is the parent of data_dir; nothing is changed)")
    mwmanager_journal.add_arguments(jg)
    args = ap.parse_args(argv)
    if getattr(args, "data_dir", None):        # another game's answers and rules are set aside first
        import mwmanager_gamestate
        switched = mwmanager_gamestate.use_game(MANAGER_DIR, os.path.dirname(os.path.abspath(args.data_dir)))
        if switched:
            print(switched)
    if args.capture_baseline or args.verify or args.rollback or args.mod_rollback:
        from pathlib import Path as _Path
        game_dir = _Path(os.path.abspath(args.data_dir)).parent
        return mwmanager_journal.run_cli(game_dir, args, mwmanager_journal.Journal())
    if args.core_master_list or args.core_master_add or args.core_master_remove:
        from core_addons import configured_master_names, edit_master_config
        manager_dir = os.path.dirname(os.path.abspath(__file__))
        if args.core_master_add or args.core_master_remove:
            try:
                edit_master_config(manager_dir, add=args.core_master_add,
                                   keywords=args.core_master_keywords, remove=args.core_master_remove)
            except (OSError, ValueError) as exc:
                print(f"Could not update major-master list: {exc}", file=sys.stderr)
                return 2
        print("Major masters in load-order priority:")
        for i, name in enumerate(configured_master_names(manager_dir), 1):
            print(f"  {i}. {name}")
        print(f"Editable file: {os.path.join(manager_dir, 'mwmanager_core_addons.json')}")
        return 0
    if not os.path.isabs(args.fix_log):
        args.fix_log = os.path.join(OUTPUT_DIR, args.fix_log)
    os.makedirs(os.path.dirname(os.path.abspath(args.fix_log)), exist_ok=True)
    if args.openmw_cfg:
        print("OpenMW is not a supported target. Point mwmanager at the vanilla Morrowind Data Files folder and omit --openmw-cfg.", file=sys.stderr)
        return 2
    ENC = args.encoding
    mwdialog_tes3.ENC = ENC                   # dec/enc/cstr live there
    try:
        _ensure_vortex_paths(args)
    except (MP.VortexPathError, MP.ToolNotFoundError) as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.init:
        # Keep the user's independent full load order across catalog initialization, which resets its
        # bootstrap order to the three canonical ESMs. An explicit --plugins list is authoritative.
        prior_order = list(args.plugins or [])
        if not prior_order and os.path.isfile(LOAD_ORDER_PATH):
            with open(LOAD_ORDER_PATH, "r", encoding="utf-8", errors="replace") as stream:
                prior_order = [line.strip() for line in stream
                               if line.strip() and not line.lstrip().startswith(";")]
        rc, initial_plugins = initialize_manager(args)
        if rc:
            return rc
        if install_mwse_lua_guards(args):
            return 2
        lint_installed_core_mwse(os.path.dirname(os.path.abspath(args.data_dir)), dry_run=args.dry_run)
        rc = post_init_data_files_maintenance(args)
        if rc:
            return rc
        download_plan = None
        try:
            from core_addons import scan_downloads
            if not args.downloads_dir:
                import mwdialog_paths
                args.downloads_dir = mwdialog_paths.resolve_downloads(required=True)
            download_plan = scan_downloads(args.downloads_dir, os.path.dirname(os.path.abspath(__file__)), OUTPUT_DIR,
                                           dry_run=args.dry_run)
        except Exception as exc:
            print(f"--init: core add-on download scan failed: {exc}", file=sys.stderr)
        lookup = index_dir(os.path.abspath(args.data_dir))
        shipped_esps = [os.path.basename(lookup[name.lower()])
                        for name in OFFICIAL_BASE_GAME_ESPS if name.lower() in lookup]
        from core_addons import configured_master_names
        configured_masters = [os.path.basename(lookup[name.lower()])
                              for name in configured_master_names(os.path.dirname(os.path.abspath(__file__)))
                              if name.lower() in lookup]
        args.init_scope = True
        args.full = True
        args.yes = True
        def unique_plugins(names):
            result, seen = [], set()
            for name in names:
                actual = os.path.basename(lookup[name.lower()]) if name.lower() in lookup else name
                if actual.lower() not in seen:
                    result.append(actual)
                    seen.add(actual.lower())
            return result

        # Keep initialization deliberately staged: core masters establish the base database and repairs;
        # shipped ESPs follow; MGE XE's required sky plugin is processed only in the final vanilla pass.
        xe = lookup.get("xe sky variations.esp")
        if not xe:
            print("--init: required MGE XE plugin XE Sky Variations.esp was not found in Data Files. "
                  "Install MGE XE and rerun --init.", file=sys.stderr)
            return 2
        stages = [("canonical ESMs", unique_plugins(initial_plugins))]
        vanilla_with_addons = unique_plugins([*initial_plugins, *shipped_esps])
        stages.append(("shipped ESP add-ons", vanilla_with_addons))
        with_xe = unique_plugins([*vanilla_with_addons, os.path.basename(xe)])
        stages.append(("MGE XE Sky Variations", with_xe))
        for label, plugins in stages:
            args.plugins = plugins
            print(f"\n--init: processing {label} ({len(plugins)} plugin(s)) with --full --yes")
            rc = run_full(args)
            if rc:
                return rc
            rc = refresh_init_catalog(args, plugins)
            if rc:
                return rc

        # Existing configured masters are ordered by the editable JSON. Downloaded archives are currently
        # inventoried and safety-checked, but this build has no archive-to-staging installer yet.
        # Do not silently claim those archives were installed; the later install phase must precede dependent mods.
        missing_installed = [name for name in configured_master_names(os.path.dirname(os.path.abspath(__file__)))
                             if name.lower() not in lookup]
        if missing_installed:
            from core_addons import review_core_master_install
            selected_masters = review_core_master_install(os.path.dirname(os.path.abspath(__file__)),
                                                          os.path.dirname(os.path.abspath(args.data_dir)),
                                                          download_plan or {})
            if selected_masters is None:
                print("--init: major-master review aborted; no masters were installed or processed.", file=sys.stderr)
                return 1
            if selected_masters:
                print("--init: preflight passed for selected major masters: " + ", ".join(selected_masters))
                print("--init: automatic archive installation is not implemented yet. No masters were installed "
                      "or dependent plugins processed. The staging folder is ready at " + STAGING_DIR,
                      file=sys.stderr)
                return 2
            else:
                print("--init: no uninstalled major masters selected; continuing with installed plugins only.")

        # Once installed, major masters run before all remaining plugins. Validate their own ESM dependencies
        # through --full's missing-master preflight before any dependent plugin is scanned.
        remaining = unique_plugins([*initial_plugins, *configured_masters, *prior_order, *shipped_esps,
                                    os.path.basename(xe)])
        if remaining != with_xe:
            args.plugins = remaining
            print(f"\n--init: processing installed major masters and remaining load order ({len(remaining)} plugin(s))")
            rc = run_full(args)
            if rc:
                return rc
            rc = refresh_init_catalog(args, remaining)
            if rc:
                return rc
        if not args.dry_run:
            write_load_order(os.path.dirname(os.path.abspath(args.data_dir)), remaining,
                             "mwmanager --init staged initialization")
        print("--init: core, shipped add-on, and XE Sky Variations passes completed; catalog refreshed.")
    if MAIN_DEPTH == 1:
        global TOP_ARGS
        TOP_ARGS = args
        if getattr(args, "data_dir", None) and os.path.isdir(args.data_dir) and not getattr(args, "dry_run", False):
            try:
                drop_vanished_added_masters(args)
            except (OSError, UnicodeError, ValueError) as e:
                print(f"(could not check {ADDED_MASTERS_RECORD}: {e})", file=sys.stderr)
        if (getattr(args, "data_dir", None) and os.path.isdir(args.data_dir)
                and args.reenable is None and not args.full):
            try:
                release_lost_winners(args)
            except (OSError, UnicodeError) as e:
                print(f"(could not check {DISABLED_RECORD}: {e})", file=sys.stderr)
            if not args.fix and not args.dry_run:
                try:
                    release_lost_doubles(args)
                except (OSError, UnicodeError, FormatError) as e:
                    print(f"(could not check {DOUBLED_DELETED_FILE}: {e})", file=sys.stderr)
        if getattr(args, "data_dir", None) and os.path.isdir(args.data_dir) and args.redundant_undo is None:
            try:
                release_redundant(args)
            except (OSError, UnicodeError) as e:
                print(f"(could not check {REDUNDANT_RECORD}: {e})", file=sys.stderr)
        if getattr(args, "data_dir", None) and os.path.isdir(args.data_dir) and not (args.full or args.auto_merge):
            pend = read_merge_rebuild(os.path.dirname(os.path.abspath(args.data_dir)))
            if pend:
                print(f"Note: {', '.join(pend)} still contain{'s' if len(pend) == 1 else ''} plugin(s) --redundant "
                      f"turned off ({', '.join(i for v in pend.values() for i in v)}); --auto-merge or --full "
                      f"rebuilds without them.", file=sys.stderr)
    if args.all:
        args.clean = args.fix = args.multipatch = True
    if args.play_anyway:
        args.play = True
    if args.full:
        return run_full(args)
    if getattr(args, "longpaths_undo", False):
        dd = os.path.abspath(args.data_dir)
        return longpaths_module().undo(os.path.dirname(dd), dd, sys.modules[__name__])
    if getattr(args, "restore_unused", False):
        dd = os.path.abspath(args.data_dir)
        return asset_health_module().restore_unused(os.path.dirname(dd), dd)
    if getattr(args, "repair_sounds", False):
        return run_repair_sounds(args)
    if not REPAIRING_INI and os.path.isdir(args.data_dir) and not (args.add_all_esp and args.sort) \
            and not (args.header or args.restore or args.restore_all or args.restore_dates):
        cut = truncated_game_files(args)
        if cut:
            last, full, count = cut
            print("=" * 70)
            print(f"Morrowind.ini's plugin list was cut off: it stops after {count} entries at '{last}'"
                  + (f" (the file is '{full}')" if full else "") + ".")
            print(f"The list reached Windows' {INI_SECTION_LIMIT:,}-character ini section limit and Vortex dropped "
                  f"every plugin after that point.")
            if args.dry_run:
                print("--dry-run: not repairing it. Without --dry-run this runs --add-all-esp --sort --yes first.")
                print("=" * 70)
            else:
                print("Repairing it first: --add-all-esp --sort --yes")
                print("=" * 70)
                argv2 = [args.data_dir, "--add-all-esp", "--sort", "--yes",
                         "--fix-log", args.fix_log, "--encoding", args.encoding]
                if args.no_mlox_rules:
                    argv2.append("--no-mlox-rules")
                if args.no_content_order:
                    argv2.append("--no-content-order")
                if args.ini:
                    argv2 += ["--ini", args.ini]
                REPAIRING_INI.append(True)
                try:
                    rc = main(argv2)
                finally:
                    REPAIRING_INI.clear()
                if rc:
                    print(f"(The repair's own checks ended with code {rc}; carrying on.)")
                print("\n" + "=" * 70 + "\nLoad order repaired; now running what was asked.\n" + "=" * 70)
    if not REPAIRING_INI and os.path.isdir(args.data_dir) and not args.sort \
            and not (args.header or args.restore or args.restore_all or args.restore_dates or args.add_all_esp):
        split = split_exact_groups(args)
        if split:
            print("=" * 70)
            for src, members, between, fresh in split:
                print(f"[Exact] group at {src} has come apart in the load order: {' / '.join(members)}")
                for m, t in fresh:
                    print(f"  {m} has the date {time.strftime('%Y-%m-%d %H:%M', time.localtime(t))}: it was "
                          f"saved or replaced since the last --sort, so it now loads in the wrong place")
                if between:
                    shown = ", ".join(n for n, _t in between[:4]) + (f" and {len(between) - 4} more" if len(between) > 4 else "")
                    print(f"  loading in between now: {shown}")
            if args.dry_run:
                print("--dry-run: not re-sorting. Without --dry-run this runs --sort --yes first.")
                print("=" * 70)
            else:
                print("Putting it back together first: --sort --yes")
                print("=" * 70)
                argv2 = [args.data_dir, "--sort", "--yes", "--fix-log", args.fix_log, "--encoding", args.encoding]
                if args.no_mlox_rules:
                    argv2.append("--no-mlox-rules")
                if args.no_content_order:
                    argv2.append("--no-content-order")
                if args.ini:
                    argv2 += ["--ini", args.ini]
                REPAIRING_INI.append(True)
                try:
                    rc = main(argv2)
                finally:
                    REPAIRING_INI.clear()
                if rc:
                    print(f"(The re-sort's own checks ended with code {rc}; carrying on.)")
                print("\n" + "=" * 70 + "\nLoad order re-sorted; now running what was asked.\n" + "=" * 70)
    if args.play:
        actions = (args.sort, args.add_all_esp, args.clean, args.fix, args.multipatch, args.patch_esm,
                   args.header, args.trace, args.restore, args.restore_all, args.restore_dates, args.csv,
                   args.clean_up, args.merge, args.auto_merge, args.merge_plan, args.unmerge is not None, args.cell_conflicts, args.reenable is not None, args.remove_generated,
                   args.redundant, args.redundant_auto, args.redundant_undo is not None, args.restore_textures,
                   args.update_rules)
        if not any(actions):                 # --play on its own: just start the game
            GAME_STARTED.append(True)
            return launch_game(args)
        elif args.dry_run:
            print("--play: skipped because of --dry-run.")
        else:
            PLAY_REQUEST.append(args)        # otherwise start it after the other steps

    if not os.path.isdir(args.data_dir):
        ap.error(f"not a folder: {args.data_dir}")

    if args.header:
        return run_header(args)
    if args.update_rules:
        rc = run_update_rules(args)
        if rc or not (args.sort or args.clean or args.fix or args.multipatch):
            return rc
    if args.restore_dates:
        return restore_dates(args)
    if args.add_all_esp:
        rc = run_add_all_esp(args)
        if rc or args.dry_run or not (args.sort or args.clean or args.fix or args.multipatch):
            return rc
    if args.clean_up:
        rc = run_clean_up(args)
        if rc or not (args.sort or args.clean or args.fix or args.multipatch):
            return rc
    if args.reenable is not None:
        return run_reenable(args)
    if args.redundant_undo is not None:
        return run_redundant_undo(args)
    if args.redundant or args.redundant_auto:
        return run_redundant(args)
    if args.remove_generated:
        return run_remove_generated(args)
    if args.restore_textures:
        return run_restore_textures(args)
    if args.restore_all:
        return restore_all(args.data_dir, args.yes, args.fix_log)
    if args.restore:
        return restore_backups(args.restore, args.data_dir)

    report = Report()
    plugins, bsas, data_dirs = resolve(args, report)
    if args.merge_plan:
        import mwdialog_mergeplan as MPL
        data_dir = os.path.abspath(args.data_dir)
        print(f"Reading {len(plugins)} plugins...", file=sys.stderr)
        plan = MPL.recommend(plugins, data_dir, os.path.dirname(data_dir), max(2, args.plan_size),
                             log=lambda s: print(s, file=sys.stderr))
        path = MPL.write_plan(plan, OUTPUT_DIR, len(plugins))
        merged = sum(len(g["inputs"]) for g in plan["groups"])
        print(f"Merge plan: {merged} plugin(s) in {len(plan['groups'])} merge(s) would free "
              f"{merged - len(plan['groups'])} slot(s). Report: {path}")
        return 0
    if args.unmerge is not None:
        rc = run_unmerge(args, plugins)
        if rc == 0 and not args.dry_run and not args.no_follow_fix:
            # the merged plugins are back: activate everything and put it in rule order
            print("\n" + "=" * 70 + "\nFOLLOW-UP: --add-all-esp --sort\n" + "=" * 70)
            argv2 = [args.data_dir, "--add-all-esp", "--sort", "--fix-log", args.fix_log, "--encoding", args.encoding]
            argv2.append("--yes")
            if args.no_mlox_rules:
                argv2.append("--no-mlox-rules")
            if args.no_content_order:
                argv2.append("--no-content-order")
            if args.ini:
                argv2 += ["--ini", args.ini]
            return main(argv2)
        return rc
    if not args.dry_run and not args.cell_conflicts and (
            args.auto_merge or args.merge or args.fix or args.patch_esm or args.multipatch):
        rc = build_compatibility_guard(args, plugins)
        if rc:
            return rc
        plugins, bsas, data_dirs = resolve(args, report)
    if args.auto_merge:
        rc = run_auto_merge(args, plugins, bsas, data_dirs)
        if rc == 0 and getattr(args, "merged_now", False) and not args.no_follow_fix:
            # the load order changed: rebuild the patches and fix what the new order exposes
            print("\n" + "=" * 70 + "\nFOLLOW-UP: --fix --multipatch with the merged load order\n" + "=" * 70)
            argv2 = [args.data_dir, "--fix", "--multipatch", "--fix-log", args.fix_log, "--encoding", args.encoding]
            argv2.append("--yes")
            if args.no_sounds:
                argv2.append("--no-sounds")
            if args.ini:
                argv2 += ["--ini", args.ini]
            return main(argv2)
        return rc
    if args.merge:
        if len(args.merge) < 2:
            ap.error("--merge needs the merged plugin's name and at least one input file or folder")
        return run_merge(args, plugins, bsas, data_dirs)
    if args.cell_conflicts:
        return run_cell_conflicts(args, plugins)
    if args.fix and fix_omwaddon_masters(args) and not args.dry_run:
        if not args.sort and not IN_FULL:                    # their masters may load after them: sort
            print("\n" + "=" * 70 + "\nSORT: plugin headers now name different masters\n" + "=" * 70)
            run_sort(args, resolve(args, Report())[0])
            if getattr(args, "sort_changed", False):
                rc = build_compatibility_guard(args, resolve(args, Report())[0])
                if rc:
                    return rc
        plugins, bsas, data_dirs = resolve(args, report)     # those plugins now find their masters
    if args.sort:
        # Sorting persists the intended order; validate it before any record or patch repairs run.
        run_sort(args, resolve(args, Report())[0])
        if getattr(args, "sort_changed", False) and not args.dry_run and (
                args.fix or args.patch_esm or args.multipatch or not args.no_follow_fix):
            rc = build_compatibility_guard(args, resolve(args, Report())[0])
            if rc:
                return rc
        if getattr(args, "sort_changed", False) and not args.no_follow_fix and not (args.fix and args.multipatch):
            args.fix = args.multipatch = True
        if not (args.clean or args.fix or args.multipatch):
            return 0
        plugins, bsas, data_dirs = resolve(args, report)
        if (args.fix or args.patch_esm or args.multipatch) and not hasattr(args, "esm_patch_pos"):
            args.esm_patch_pos = sum(1 for n, _p in plugins if n.lower().endswith(".esm"))
        plugins = fix_scan_plugins(args, plugins) \
            if (args.fix or args.patch_esm or args.multipatch) else plugins
    if args.fix:
        fix_ref_groups(args, plugins)
        fix_cell_layouts(args, plugins)
    if args.fix and fix_long_names(args, plugins, bsas) and not args.dry_run:
        plugins, bsas, data_dirs = resolve(args, report)
    if args.fix and not args.no_doubled:
        gone = {n.lower() for n in fix_doubled(args, plugins)}
        if gone:
            plugins = [(n, p) for n, p in plugins if n.lower() not in gone]
            args._cell_ctx = None
    if args.fix and args.performers_patch and not args.no_performers:
        fix_performers(args, plugins)
    elif args.fix and not args.dry_run:
        drop_performers_patch(args)       # the mwperformers MWSE mod decides in the game now
    if args.fix and args.sort and getattr(args, "sort_changed", False) and not args.dry_run:
        args._cell_ctx = None
        plugins, _bsas, _data_dirs = resolve(args, Report())
        rc = build_compatibility_guard(args, plugins)
        if rc:
            return rc
        plugins, bsas, data_dirs = resolve(args, report)
    if args.fix:
        fix_cell_winners(args, plugins, report)
    loaded_patches = {n.lower() for n, _p in plugins} & {x.lower() for x in OUR_PATCHES}
    if args.fix or args.patch_esm or args.multipatch:   # rebuilt from scratch: leave them out of the scan
        names = [n.lower() for n, _p in plugins]
        if PATCH_NAME.lower() in names:            # where the ESM patch really loads
            args.esm_patch_pos = sum(1 for n in names[:names.index(PATCH_NAME.lower())]
                                     if n not in [x.lower() for x in OUR_PATCHES])
        else:                                      # new: right after the masters
            args.esm_patch_pos = sum(1 for n in names if n.endswith(".esm"))
        plugins = fix_scan_plugins(args, plugins)
    if not plugins:
        print("No plugins found.", file=sys.stderr)
        return 2

    only = {o.lower() for o in args.only} if args.only else None
    if args.clean:
        run_clean(args, plugins, only)        # cleaned files are what the checks below see

    world = scan(args, plugins, bsas, data_dirs, report)
    if args.trace:
        print("\n" + "=" * 70 + "\nTRACE (load position, plugin: what it does)\n" + "=" * 70)
        for t in sorted(world.trace):
            lines = [x for tt, x in world.trace_log if tt == t]
            print(f"\n{t}:" + ("" if lines else "  not mentioned by any loaded plugin"))
            for x in lines:
                print("  " + x)
            print(f"  -> at the end: {'defined as ' + world.all_ids[t] if t in world.all_ids else 'NOT defined'}, "
                  f"{max(world.placed.get(t, 0), 0)} placed copy/copies")
        print()

    limit = SEV_RANK[args.min_severity]
    hidden = set()      # (plugin, issue label) for master issues the existing patch already fixes
    if args.fix or args.patch_esm or loaded_patches:
        covered = set()
        for pn in (PATCH_NAME, PLACE_PATCH_NAME):       # the patches that fix issues (the others merge or move)
            covered |= patch_contents(os.path.join(args.data_dir, pn))
        for sev, plugin, topic, info, msg in report.issues:
            if covered_by_patches((sev, plugin, topic, info, msg), covered):
                hidden.add((plugin, issue_label(topic, info, msg)))
    issues = [i for i in report.issues
              if SEV_RANK[i[0]] <= limit and (only is None or i[1].lower() in only)
              and not is_our_patch(i[1])
              and (i[1], issue_label(i[2], i[3], i[4])) not in hidden]
    order = {n.lower(): k for k, (n, _p) in enumerate(plugins)}
    issues.sort(key=lambda i: (order.get(i[1].lower(), -1), i[1].lower(), i[2].lower(), SEV_RANK[i[0]], i[3]))

    counts = defaultdict(int)
    last_plugin = last_topic = None
    for sev, plugin, topic, info, msg in issues:
        counts[sev] += 1
        if plugin != last_plugin:
            print(f"\n=== {plugin or '(load order)'} ===")
            last_plugin, last_topic = plugin, None
        if topic != last_topic:
            print(f"  [{topic}]" if topic else "  [file]")
            last_topic = topic
        loc = f" {info}:" if info else ""
        print(f"    {sev:<5}{loc} {msg}")

    print(f"\nLoaded {len(plugins)} plugin(s), {len(world.topics)} topic(s), "
          f"{sum(len(t.infos) for t in world.topics.values())} response(s).")
    print("Found " + ", ".join(f"{counts[s]} {s}" for s in SEVERITIES) + ".")
    if world.persist_mismatch and SEV_RANK["INFO"] > limit:
        print("\nReferences filed as persistent/temporary differently from their master "
              "(left as the mods made them; see them with --min-severity INFO):")
        for plugin in sorted(world.persist_mismatch, key=lambda n: order.get(n.lower(), 0)):
            if only is None or plugin.lower() in only:
                print(f"  {plugin}: {world.persist_mismatch[plugin]}")
    if hidden:
        print(f"({len(hidden)} issue(s) in masters are already fixed by '{PATCH_NAME}' and not listed; "
              f"they are carried over when the patch is rebuilt.)")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["severity", "plugin", "topic", "response_id", "message"])
            w.writerows(issues)
        print(f"CSV written to {args.csv}")

    if (args.fix or args.asset_check or getattr(args, "bug_reports", False)) and not args.dry_run \
            and not getattr(args, "no_bug_reports", False) and only is None:
        try:
            write_bug_reports(args, plugins, report, world)
        except Exception as e:                       # a report is a courtesy: never stop the run for it
            print(f"(bug reports not written: {e})", file=sys.stderr)
    if args.fix or args.patch_esm:
        run_fix(args, plugins, bsas, data_dirs, report, world, only, hidden)
    if args.multipatch:
        run_multipatch(args, plugins, world)

    return 1 if counts["ERROR"] else 0


_upstream_write_bug_reports = write_bug_reports


def write_bug_reports(args, plugins, report, world):
    return _upstream_write_bug_reports(args, plugins, report, world, section="mwmanager")


def main(argv=None):
    MT.install()
    global MAIN_DEPTH
    MAIN_DEPTH += 1
    finished = False
    try:
        rc = _main(argv)
        finished = True
        return rc
    finally:
        MAIN_DEPTH -= 1
        if MAIN_DEPTH == 0:
            if finished:
                # only a run that ended normally writes its load order into Morrowind.ini; after a crash or
                # Ctrl-C the list may be half done (mwdialog_load_order.txt keeps it; --play writes it in)
                try:
                    apply_load_order_to_ini(TOP_ARGS)
                except (OSError, UnicodeError) as e:
                    print(f"\nCould not update Morrowind.ini's [Game Files]: {e}", file=sys.stderr)
            elif TOP_ARGS is not None and LOAD_ORDER_WRITTEN:
                print(f"\nThe run stopped early: Morrowind.ini's [Game Files] was NOT updated from "
                      f"{LOAD_ORDER_FILE}.", file=sys.stderr)
            LOAD_ORDER_WRITTEN.clear()


def _archive_downloads_setting():
    """The archive folder the GUI saved in mwmanager.ini (archive_downloads_dir, or the older downloads_dir)."""
    import configparser
    ini = configparser.ConfigParser()
    try:
        ini.read(manager_file("mwmanager.ini"), encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    for key in ("archive_downloads_dir", "downloads_dir"):
        value = ini.get("Paths", key, fallback="").strip()
        if value and os.path.isdir(value):
            return value
    return None


def _ensure_vortex_paths(args):
    """MWManager deploys from its own staging folder, so Vortex is optional here (upstream requires it unless
    --no-vortex). Staging: --vortex-dir, else the folder the deployed plugins are hard links into, else none.
    Downloads (the master guard's archive lookup): --vortex-downloads, else the archive folder set in the GUI,
    else Vortex's when it is installed, else none. Never an error."""
    if getattr(args, "no_vortex", False):
        args.vortex_dir = None
    elif not getattr(args, "vortex_dir", None):
        try:
            args.vortex_dir = find_vortex_root(os.path.abspath(args.data_dir))
        except (OSError, AttributeError, ValueError):
            args.vortex_dir = None
    if getattr(args, "no_archive_lookup", False) or getattr(args, "no_master_guard", False):
        return
    if not getattr(args, "vortex_downloads", None):
        args.vortex_downloads = _archive_downloads_setting()
    if not args.vortex_downloads:
        try:
            args.vortex_downloads = MP.resolve_downloads(required=False)
        except Exception:                             # Vortex not installed, unreadable settings...
            args.vortex_downloads = None
