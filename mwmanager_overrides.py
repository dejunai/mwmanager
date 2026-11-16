"""MWManager layer: the scanner functions MWManager runs differently from upstream mwdialog_check.

Runs inside the upstream mwdialog_check namespace after mwmanager_state.py (see mwmanager_upstream.py). Each
definition here is upstream's, merged with MWManager's changes: the MWManager load order file is the load order,
--init's staged passes, --full keeping generated patches in place, plugins turned off are moved to
MWManager\\disabled_plugins, mlox rules kept in the MWManager folder, sounds converted in a temp folder, MGE XE's
sky plugin kept before the generated tail. Everything else comes from upstream unchanged.
"""
from mwdialog_check import *  # noqa: F401,F403  (names resolve in mwdialog_check when composed)


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
    """Upstream World with MWManager's changes: placeholder Bink videos for missing ones, missing voice files
    and shared responses handled through patch records during --full."""

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
                here = os.path.dirname(os.path.abspath(__file__))
                if kind == "icon":
                    rel = os.path.join("Icons", os.path.splitext(A.rel(v, "icons"))[0] + ".dds")
                    src, what = None, "a placeholder icon"
                elif v.lower().endswith(".nif"):
                    rel = os.path.join("Meshes", A.rel(v, "meshes"))
                    dummy = (DUMMY_MARKER_NIF if tag in ("STAT", "ACTI", "LIGH") else
                             DUMMY_ANIM_NIF if tag in ("NPC_", "CREA") and sub == "MODL" and tag == "NPC_" else DUMMY_NIF)
                    src = os.path.join(here, dummy)
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
            if kind == "video" and real:
                self.generated.append((os.path.join("Video", A.rel(v, "video")),
                                       f"video reference in {tag} '{win[4]}'", None, win[1]))
                notes.append(f"create a placeholder Bink video at '{v}'")
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
                            rel = os.path.join("Video", self.assets.rel(path, "video"))
                            self.generated.append((rel, f"script {sname} ({plugin})", None, plugin))
                            self.missing_by_plugin[plugin] += 1
                            self.r.add("WARN", plugin, f"script {sname}", "",
                                       f"{m.group(1)}: {prob} (--fix creates a placeholder Bink video)")
                    else:
                        self.r.add("WARN", plugin, f"script {sname}", "",
                                   f"{m.group(1)}: {prob} (the game skips it when this line runs)")

    def _check_info(self, info, t, quest_stages, sound_files):
        r = self.r

        shared = len(set(t.defined_by.get((info.id or "").lower(), []))) > 1

        def add(sev, msg, op=None, desc=None):
            if op is not None and op[0] == "delete" and op != ("delete", "local") and shared and not IN_FULL:
                # A direct edit could break the response chain shared with another plugin. During --full,
                # the fix is emitted as a later patch record with a DELE marker, preserving the chain.
                if sev in ("ERROR", "WARN"):
                    sev = "INFO"                 # nothing to do about it: it never plays, and stays harmless
                msg += " (left in place outside --full: other plugins have this response too)"
                op = desc = None
            r.add(sev, info.source, t.name, info.id, msg, fix=(info.off, op, desc) if op else None)

        DELETE = ("delete",)
        never = "deleted response: its filter names something that doesn't exist, so it can never play"

        # speaker filters
        if info.actor and not self._exists(ACTOR_TAGS, info.actor):
            add("ERROR", f"speaker ID (ONAM) '{info.actor}' is not an NPC or creature", DELETE, never)
        if info.race and not self._exists({"RACE"}, info.race):
            add("ERROR", f"race filter '{info.race}' does not exist", DELETE, never)
        if info.cls and not self._exists({"CLAS"}, info.cls):
            add("ERROR", f"class filter '{info.cls}' does not exist", DELETE, never)
        if info.faction and info.faction.upper() != "FFFF" and not self._exists({"FACT"}, info.faction):
            add("ERROR", f"faction filter '{info.faction}' does not exist", DELETE, never)
        if info.pcfaction and info.pcfaction.upper() != "FFFF" and not self._exists({"FACT"}, info.pcfaction):
            add("ERROR", f"PC faction filter '{info.pcfaction}' does not exist", DELETE, never)
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


def run_repair_sounds(args):
    """--repair-sounds: sound files that plugins play (dialogue, sound records, scripts) and the game can hang
    or crash on, re-saved with the same name: WAVs as 16-bit PCM, MP3s without their damaged or oversized
    tag. Needs ffmpeg. Originals go to <mod folder>\\Off\\<plugin>\\<path> first; no plugin is changed."""
    data_dir = os.path.abspath(args.data_dir)
    game_dir = os.path.dirname(data_dir)
    print("=" * 70 + "\nREPAIR SOUNDS\n" + "=" * 70)
    ffmpeg = os.path.join(TOOLS_DIR, "ffmpeg.exe")
    if not os.path.isfile(ffmpeg):
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("  ffmpeg isn't installed in MWManager\tools or on PATH; install it and run this again.")
        return 2
    SOUND_ROOTS[:] = [data_dir]
    names = read_load_order(args) or []
    used = defaultdict(set)
    for n in names:
        p = os.path.join(data_dir, n)
        try:
            data = open(p, "rb").read()
        except OSError:
            continue
        for _off, tag, _fl, b in iter_records(data):
            if tag == "INFO":
                v = first_sub(b, b"SNAM")
            elif tag == "SOUN":
                v = first_sub(b, b"FNAM")
            elif tag == "SCPT":
                for x in SAY_SOUND_RE.findall((first_sub(b, b"SCTX") or b"").decode(ENC, MW_ERRORS)):
                    used[os.path.join("Sound", x.strip().replace("/", os.sep))].add(n)
                continue
            else:
                continue
            if v:
                used[os.path.join("Sound", cstr(v).strip().replace("/", os.sep))].add(n)
    print(f"  {len(used)} sound file(s) named by the active plugins; checking them...", file=sys.stderr)
    fixed, failed = [], []
    for rel, who in sorted(used.items()):
        full = os.path.join(data_dir, rel)
        if not os.path.isfile(full):
            continue                                 # in a BSA, or missing (--asset-check reports those)
        prob = sound_file_problem(rel.lower())
        if not prob:
            continue
        if args.dry_run:
            print(f"  would repair {rel}: it {prob}")
            continue
        tmp = os.path.join(tempfile.gettempdir(), f"mwmanager_sound_{os.getpid()}" + os.path.splitext(rel)[1])
        if rel.lower().endswith(".mp3"):
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", full, "-map_metadata", "-1",
                   "-id3v2_version", "0", "-write_xing", "0", "-c:a", "copy", tmp]
        else:
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", full, "-c:a", "pcm_s16le",
                   "-map_metadata", "-1", "-fflags", "+bitexact", tmp]
        try:
            ok = subprocess.run(cmd, capture_output=True).returncode == 0 and os.path.getsize(tmp) >= 64
        except OSError:
            ok = False
        if not ok:
            failed.append(rel)
            print(f"  FAILED {rel} ({prob}): ffmpeg couldn't re-save it")
            continue
        backup = backup_asset_original(data_dir, rel, plugin=sorted(who)[0], feature="Reprocessed sounds")
        replace_asset_in_place(full, open(tmp, "rb").read())
        os.remove(tmp)
        _SOUND_PROBLEMS.pop(rel.lower(), None)
        fixed.append((rel, prob, backup))
        print(f"  repaired {rel}: it {prob}\n      original kept as {backup}")
    if fixed and not args.dry_run:
        with MT.open_log(os.path.join(game_dir, args.fix_log), "a", encoding="utf-8") as lf:
            for rel, prob, backup in fixed:
                lf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {rel}: it {prob} -> re-saved under the same name; "
                         f"original kept as {backup}\n")
    print(f"\n  {len(fixed)} repaired, {len(failed)} couldn't be." if fixed or failed else "  Nothing to repair.")
    return 1 if failed else 0


def run_fix(args, plugins, bsas, data_dirs, report, world, only, hidden=frozenset()):
    esp_plans, esm_plans, skipped, manual = plan_fixes(report, world, plugins, only, True)
    print("\n" + "=" * 70)
    print("FIX PLAN (ERRORs only)")
    print("=" * 70)
    for plugin, why in skipped.items():
        print(f"  SKIP {plugin}: {why}")

    # stand-in gold coins for objects that scripts name but no loaded mod defines, and a hidden
    # cell with a placed copy of every object a script acts on ("X->") that nothing places
    placeholders, placements = {}, {}
    if not args.no_placeholders:
        for o, v in sorted(world.missing_objects.items()):
            if only is not None and not any(pl.lower() in only for pl, _s in v["used_by"]):
                continue
            if world.gold_template is not None and len(v["id"]) <= 31:
                placeholders[o] = v
                if v["arrow"]:
                    placements[o] = dict(v, tag="MISC", why="placeholder coin")
        for o, v in sorted(world.unplaced.items()):
            if only is None or any(pl.lower() in only for pl, _s in v["used_by"]):
                placements[o] = v
    early = {o: v for o, v in world.early_needed.items()
             if only is None or any(p.lower() in only for p in v[2])}
    early_placed = {o: v for o, v in world.early_placed.items()
                    if only is None or any(p.lower() in only for p in v["users"])}
    # scripts / enchantments / inventory items that point at nothing (stale overrides)
    item_esp, item_esm = {}, {}
    for fx in world.item_fixes:
        pl = fx["plugin"]
        if only is not None and pl.lower() not in only:
            continue
        if pl.lower() in world.unfixable:
            if pl not in skipped:
                print(f"  SKIP {pl}: {world.unfixable[pl.lower()]}")
                skipped[pl] = world.unfixable[pl.lower()]
            continue
        (item_esp if pl.lower().endswith(FIXABLE_EXT) else item_esm).setdefault(pl, []).append(fx)
    generated = list({g[0].lower(): g for g in world.generated}.values()) if (only is None) else []
    shrink = list(getattr(world, "texture_reductions", [])) if only is None else []
    keyfix = list(getattr(world, "text_key_fixes", [])) if only is None else []
    health = list(getattr(world, "asset_health", [])) if only is None else []
    lp_held = getattr(world, "longpath_plan", None)
    if not asset_repair_enabled(args):
        repair_generated = bool(getattr(args, "generate_missing_assets", False))
        held = (0 if repair_generated else len(generated)) + len(shrink) + len(keyfix) + len(health) + len(lp_held or [])
        if not repair_generated:
            generated = []
        shrink, keyfix, health = [], [], []
        if lp_held:
            world.longpath_plan, world.longpath_nifs = [], []
        if held:
            print(f"  Asset repairs are off: {held:,} asset file change(s) left out of this fix (turn them on with "
                  f"--asset-repair, or [Repair] asset_repair = true in mwmanager.ini).")
    if not esp_plans and not esm_plans and not placeholders and not placements and not early and not early_placed \
            and not item_esp and not item_esm and not generated and not shrink and not keyfix and not health \
            and not (only is None and getattr(world, "longpath_plan", None)):
        print("  Nothing that can be fixed automatically.")
        return
    total = print_plan("", esp_plans)
    total += print_plan(f"   (fixed through {PATCH_NAME})", esm_plans, hidden)
    if item_esp or item_esm:
        print("\n  Scripts, enchantments, inventory items and cell regions that point at nothing (usually an override made "
              "for an older version of its master):")
        in_patch = set()
        try:
            with open(os.path.join(args.data_dir, PATCH_NAME), "rb") as f:
                pdata = f.read()
            for _o, ptag, _f, pbody in iter_records(pdata):
                pn = first_sub(pbody, b"NAME") if ptag in REF_TAGS else None
                if pn is not None:
                    in_patch.add((ptag, cstr(pn).lower()))
        except (OSError, FormatError):
            pass
        for group, title in ((item_esp, ""), (item_esm, f"   (fixed through {PATCH_NAME})")):
            for plugin, fixes in group.items():
                carried = [fx for fx in fixes if group is item_esm and (fx["tag"], fx["id"].lower()) in in_patch]
                if carried and len(carried) == len(fixes):
                    print(f"\n  {plugin}{title}: {len(carried)} record(s) already in the patch, carried over")
                    total += sum(len(fx["msgs"]) for fx in carried)
                    continue
                print(f"\n  {plugin}{title}")
                for fx in fixes:
                    for msg, action in fx["msgs"]:
                        print(f"    - {msg}\n        -> {action}")
                        total += 1
    if placeholders:
        place_has = patch_contents(os.path.join(args.data_dir, PLACE_PATCH_NAME))
        already = {o for o in placeholders if ("script-object", o) in place_has}
        new = [v for o, v in placeholders.items() if o not in already]
        print(f"\n  Placeholder objects (a single gold coin each, added to {PLACE_PATCH_NAME})"
              + (f": {len(already)} already in the patch, carried over" if already else ""))
        for v in new:
            users = ", ".join(sorted({f"{pl}: {sc}" for pl, sc in v["used_by"]}))
            print(f"    - {v['id']}  (named by {users})")
        total += len(placeholders)
    if early:
        have = patch_contents(os.path.join(args.data_dir, PATCH_NAME))
        already = {o for o in early if ("early-object", o) in have}
        print(f"\n  Defined early in {PATCH_NAME} (a copy of the real definition, so plugins that place or "
              f"script these find them when they load)" + (f": {len(already)} already in the patch, carried over"
                                                    if already else ""))
        for o, (src, tag, users) in early.items():
            if o not in already:
                print(f"    - {o}  ({tag} from {src}; needed by {', '.join(sorted(set(users)))})")
        total += len(early)
    if early_placed:
        have = patch_contents(os.path.join(args.data_dir, PATCH_NAME))
        already = {o for o in early_placed if ("early-placed", o) in have}
        print(f"\n  Placed early in the hidden cell '{EARLY_CELL}' ({PATCH_NAME}), so scripts that load before "
              f"the real copy find one" + (f": {len(already)} already in the patch, carried over" if already else ""))
        for o, v in early_placed.items():
            if o not in already:
                print(f"    - {v['id']}  (first placed by {v['by']}; acted on by scripts in "
                      f"{', '.join(sorted(set(v['users'])))})")
        total += len(early_placed)
    if placements:
        covered_now = patch_contents(os.path.join(args.data_dir, PLACE_PATCH_NAME))
        already = {o for o in placements if ("placed-object", o) in covered_now}
        print(f"\n  Placed in the hidden cell '{PLACEHOLDER_CELL}' (scripts act on these directly)"
              + (f": {len(already)} already in the patch, carried over" if already else ""))
        for o, v in placements.items():
            if o not in already:
                users = ", ".join(sorted({pl for pl, _s in v["used_by"]}))
                print(f"    - {v['id']}  ({v['why']}; used by {users})")
        total += len(placements)
    manual = {p: [x for x in m if (p, x) not in hidden
                  and not ("isn't defined yet" in x and any(f"'{o}'" in x.lower() for o in early))]
              for p, m in manual.items()}
    manual = {p: m for p, m in manual.items() if m}
    if manual:
        print("\n  Left for you to fix by hand:")
        for plugin, items in manual.items():
            for label in items:
                print(f"    - {plugin} {label}")

    if shrink:
        tbytes = sum(t[4] for t in shrink)
        print(f"\n  Textures to reduce to {TEXTURE_MAX_EDGE} on their longest edge ({len(shrink)}, {tbytes / 2**30:.1f} GB "
              f"now, about {tbytes * 0.75 / 2**30:.1f} GB less after; the smaller copies inside each file are used, "
              f"originals kept, --restore-textures undoes it):")
        for full, rel, w, h, size in sorted(shrink, key=lambda t: -t[4])[:10]:
            print(f"    - {rel}   {w}x{h}, {size / 2**20:.0f} MB")
        if len(shrink) > 10:
            print(f"    ... and {len(shrink) - 10} more")
        total += len(shrink)
    if generated:
        print(f"\n  Files to put in Data Files ({len(generated)}): copies of files found in Vortex mod folders but never "
              f"deployed, silent stand-ins for sounds and music, and placeholder textures for models whose textures "
              f"no mod has (listed in {GENERATED_RECORD}; --remove-generated deletes them):")
        for item in generated[:12]:
            rel, why, source = (item + (None,))[:3]
            kind = (("back from .Orphaned" if ".orphaned" in source.lower() else
                     "stand-in model, " + os.path.basename(source) if os.path.basename(source).lower().startswith(
                         "mwmanager_dummy") else "copy of " + source) if source
                    else ("placeholder texture" if rel.lower().endswith(".dds") else "silent"))
            print(f"    + {rel}   ({kind}; {why})")
        if len(generated) > 12:
            print(f"    ... and {len(generated) - 12} more")
        total += len(generated)
    if keyfix:
        print(f"\n  Models/animations whose text keys get Windows line endings ({len(keyfix)}, originals kept):")
        for full, rel, bad in keyfix[:10]:
            print(f"    - {rel}   ({bad} key(s))")
        total += len(keyfix)
    if health:
        from collections import Counter as _C
        kinds = _C(k for k, *_x in health)
        names = {"tex": "texture(s) to convert (DXT / mipmaps / power-of-two size, texconv)",
                 "abs": "model(s) whose absolute texture paths become relative",
                 "snd": "sound(s) to convert (mono / 44.1 kHz, ffmpeg)",
                 "unused": "unused or empty file(s) to move to Off\\<plugin>\\Unused assets\\ (--restore-unused)",
                 "broken": "broken file(s) to keep in Off\\<plugin>\\Damaged assets\\, a stand-in put in their place",
                 "orphan": "orphaned animation(s) to move to Off\\<plugin>\\Unused assets\\"}
        print("\n  Asset health:")
        for k, n in kinds.items():
            print(f"    {n:6d} {names[k]}")
            for kind, full, rel, job in [h for h in health if h[0] == k][:5]:
                print(f"           e.g. {rel}")
        total += len(health)
    lp_plan = getattr(world, "longpath_plan", []) if only is None else []
    if lp_plan:
        print(f"\n  Paths too long for Windows: files to move to a short path ({len(lp_plan)}; the records and "
              f"models that name them follow; listed in mwdialog_longpaths.json, --longpaths-undo puts them back):")
        for line in longpaths_module().plan_lines(world)[:15]:
            print(line)
        total += len(lp_plan)
    if args.dry_run:
        print("\nDry run: no files were changed.")
        return
    if not args.yes:
        if not sys.stdin.isatty():
            print("\nNot running interactively; add --yes to apply these fixes.")
            return
        parts = []
        if esp_plans:
            parts.append(f"{len(esp_plans)} plugin(s), each backed up first")
        if item_esp and not esp_plans:
            parts.append(f"{len(item_esp)} plugin(s), each backed up first")
        elif item_esp:
            parts[0] = f"{len(set(esp_plans) | set(item_esp))} plugin(s), each backed up first"
        if esm_plans or placeholders or placements or early or early_placed or item_esm:
            parts.append("the patch plugin")
        ans = input(f"\nApply {total} fix(es) to {' and '.join(parts)}? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("No changes made.")
            return

    paths = {n: p for n, p in plugins}
    log = []
    for plugin, plan in esp_plans.items():
        try:
            n = apply_plan(paths[plugin], plugin, plan, world, log)
            print(f"  {plugin}: {n} change(s) written")
        except Exception as e:                       # one plugin's problem never stops the others halfway
            print(f"  {plugin}: NOT changed ({type(e).__name__}: {e})")

    if lp_plan or getattr(world, "longpath_nifs", None):
        failed = longpaths_module().apply(world, sys.modules[__name__], os.path.abspath(args.data_dir), log)
        if failed:                                    # a file that didn't move keeps its records' old path
            for group in (item_esp, item_esm):
                for pl in list(group):
                    group[pl] = [fx for fx in group[pl] if fx.get("longpath") not in failed]
                    if not group[pl]:
                        del group[pl]
        print(f"  Data Files: {len(lp_plan) - len(failed)} file(s) moved to a short path"
              + (f", {len(failed)} could not be" if failed else ""))
    for plugin, fixes in item_esp.items():
        try:
            n = apply_item_fixes(paths[plugin], plugin, fixes, log, backup=plugin not in esp_plans)
            print(f"  {plugin}: {n} record(s) corrected")
        except Exception as e:                       # one plugin's problem never stops the others halfway
            print(f"  {plugin}: NOT changed ({type(e).__name__}: {e})")
    if generated:
        made = write_generated(os.path.abspath(args.data_dir), os.path.dirname(os.path.abspath(args.data_dir)),
                               generated, log)
        print(f"  Data Files: {len(made)} silent stand-in file(s) created")
    if shrink:
        n, saved = reduce_textures(os.path.abspath(args.data_dir), os.path.dirname(os.path.abspath(args.data_dir)),
                                   shrink, log)
        print(f"  Data Files: {n} texture(s) reduced to {TEXTURE_MAX_EDGE}, {saved / 2**30:.1f} GB smaller. Vortex may "
              f"report them as changed outside Vortex: keep the changes, or choose Revert to get the full size back "
              f"(don't save them into the mod, or the staging folder loses its full-size copies).")

    if keyfix:
        dd = os.path.abspath(args.data_dir)
        done = 0
        for full, rel, bad in keyfix:
            try:
                with open(full, "rb") as f:
                    data = f.read()
                n, new = nif_text_keys_lf(data)
                if not new:
                    continue
                bak = backup_asset_original(dd, rel, feature="Reprocessed Unix line joins")
                replace_asset_in_place(full, new)
                done += 1
                log.append(("Data Files", f"'{rel}': {n} animation text key(s) with Unix line endings",
                            f"rewritten with Windows line endings (original: {bak})"))
            except OSError as e:
                print(f"  {rel}: NOT changed ({e})")
        print(f"  Data Files: {done} mesh/animation file(s) had their text keys rewritten with Windows line endings")

    if health:
        dd = os.path.abspath(args.data_dir)
        print("  Data Files: " + asset_health_module().apply(health, dd, os.path.dirname(dd), sys.modules[__name__],
                                                             log, dry=args.dry_run))

    patch_path = os.path.join(args.data_dir, PATCH_NAME)
    place_path = os.path.join(args.data_dir, PLACE_PATCH_NAME)
    patch_log = []                     # ESM patch: rebuilt (and re-logged) after follow-on fixes
    place_log = []
    patch_built = [False]
    place_built = [False]

    def write_dated(path, *a, **kw):
        """Write a patch; a brand-new one gets the day after the newest plugin (date-based load order)."""
        is_new = not os.path.exists(path)
        result = write_patch(path, *a, **kw)
        if is_new:
            when = next_free_date(args.data_dir, os.path.basename(path))
            if when:
                os.utime(path, (when, when))
        return result

    def build_patch():
        """(Re)build the ESM patch from every master fix planned so far. It loads right after the masters."""
        patch_log.clear()
        entries = []
        for name, path in plugins:                      # load order keeps the patch tidy
            if name in esm_plans:
                apply_plan(path, name, esm_plans[name], world, patch_log, collect=entries)
        paths_l = {n.lower(): p for n, p in plugins}
        conflict_records, conflict_winners = major_master_conflict_overrides(paths_l)
        patch_sources = dict(esm_plans)
        for winner in conflict_winners:
            patch_sources.setdefault(winner, {})
        masters = master_closure(world, plugins, patch_sources)
        extra = list(conflict_records)
        for winner in conflict_winners:
            patch_log.append((PATCH_NAME, f"major-master record conflicts resolved in favor of {winner}",
                              "copied the selected record into the ESM patch"))
        for name, fixes in item_esm.items():          # corrected copies of masters' records
            apply_item_fixes(paths_l[name.lower()], name, fixes, patch_log, collect=extra)
        for o, (src, tag, users) in early.items():
            raw = extract_record(paths_l[src.lower()], tag, o) if src.lower() in paths_l else None
            if raw:
                extra.append(raw)
                patch_log.append((PATCH_NAME, f"'{o}' is placed by {', '.join(sorted(set(users)))} before "
                                              f"{src} defines it", "copied its definition into the patch"))
        if early_placed:
            extra.append(placeholder_cell([v["id"] for v in early_placed.values()], EARLY_CELL))
            for v in early_placed.values():
                patch_log.append((PATCH_NAME, f"scripts in {', '.join(sorted(set(v['users'])))} act on '{v['id']}' "
                                              f"before {v['by']} places it",
                                  f"placed a hidden copy in '{EARLY_CELL}'"))
        topics, recs = write_dated(patch_path, entries, masters, patch_log, backup=not patch_built[0],
                                   extra=extra)
        patch_built[0] = True
        print(f"  {PATCH_NAME}: written with {recs} response override(s) in {topics} topic(s)"
              + (f", {len(early)} object(s) defined early" if early else "")
              + (f", {len(early_placed)} placed early" if early_placed else "")
              + (f"; masters: {', '.join(n for n, _ in masters)}" if masters else ""))
        patch_log.append((PATCH_NAME, "", f"written ({recs} responses in {topics} topics)"))

    def build_place():
        """(Re)build the placeholder patch: coins and the hidden cell. It loads last."""
        extra = [r for r in (placeholder_record(world, v["id"]) for v in placeholders.values()) if r]
        if placements:
            extra.append(placeholder_cell([v["id"] for v in placements.values()]))
        write_dated(place_path, [], [], place_log, backup=not place_built[0], extra=extra,
                    description="Placeholder objects and hidden cell for missing script objects, "
                                "generated by mwmanager.py. Load this LAST.")
        place_built[0] = True
        print(f"  {PLACE_PATCH_NAME}: written with {len(placeholders)} placeholder object(s) and "
              f"{len(placements)} hidden placed object(s)")
        for v in placeholders.values():
            place_log.append((PLACE_PATCH_NAME, f"script object '{v['id']}' is not defined by any loaded mod",
                              "added a placeholder gold coin with that ID"))
        for v in placements.values():
            place_log.append((PLACE_PATCH_NAME, f"scripts act on '{v['id']}' but no copy is placed ({v['why']})",
                              f"placed one in the hidden cell '{PLACEHOLDER_CELL}'"))

    conflict_records_for_patch, _conflict_winners = major_master_conflict_overrides(
        {name.lower(): path for name, path in plugins})
    if esm_plans or early or early_placed or item_esm or os.path.exists(patch_path) or conflict_records_for_patch:
        build_patch()                      # also clears out what older versions put in it
    if placeholders or placements or os.path.exists(place_path):
        build_place()

    def rescan_list():
        """The load order as the game will see it: ESM patch where it loads, placeholder patch last."""
        order = list(plugins)
        if patch_built[0]:
            pos = min(getattr(args, "esm_patch_pos", len(order)), len(order))
            order.insert(pos, (PATCH_NAME, patch_path))
        if place_built[0]:
            order.append((PLACE_PATCH_NAME, place_path))
        return order

    backed_up = set(esp_plans) | set(item_esp)
    fresh = None                       # a scan that already reflects every change made
    # The re-scans below look at what the fixes changed in the plugins; the files on disk (models,
    # textures, sounds) don't change between them, so the asset check (minutes) isn't repeated.
    rescan_args = copy.copy(args)
    rescan_args.asset_check = False
    if getattr(args, "asset_check", False):
        print("\n(Re-scans after the fixes skip the asset check: the files on disk haven't changed.)")
    for _ in range(0 if getattr(args, "no_follow_fix", False) else 4):
        # Fixes can expose new ones, e.g. a plugin or master linking to a response that was just deleted.
        report2 = Report()
        world2 = scan(rescan_args, rescan_list(), bsas, data_dirs, report2)
        follow, follow_esm, _skip, _man = plan_fixes(report2, world2, rescan_list(), only, True)
        follow.pop(PATCH_NAME, None)
        follow.pop(PLACE_PATCH_NAME, None)
        for plugin in list(follow_esm):        # already in the patch (e.g. a master's nameless topic)
            for off in list(follow_esm[plugin]):
                have = esm_plans.get(plugin, {}).get(off, [])
                follow_esm[plugin][off] = [x for x in follow_esm[plugin][off] if x not in have]
                if not follow_esm[plugin][off]:
                    del follow_esm[plugin][off]
            if not follow_esm[plugin]:
                del follow_esm[plugin]
        for plugin, off, op, desc, msg, topic, info_id in report2.fixes:
            if plugin != PATCH_NAME or not patch_supports(op):
                continue
            t = world.topics.get(topic.lower())
            owner = t.infos.get(info_id.lower()) if t and info_id else None
            if owner is None or owner.off is None or owner.source not in dict(plugins) \
                    or owner.source.lower().endswith(FIXABLE_EXT):
                continue
            items = follow_esm.setdefault(owner.source, defaultdict(list))[owner.off]
            if (op, desc, msg) not in items:
                items.append((op, desc, msg))
        if not follow and not follow_esm:
            fresh = report2
            break
        print("\nFollow-on fixes made possible by the changes above:")
        n = print_plan("", follow) + print_plan(f"   (fixed through {PATCH_NAME})", follow_esm)
        if not args.yes:
            if not sys.stdin.isatty() or input(f"Apply these {n} follow-on fix(es)? [y/N] ").strip().lower() not in ("y", "yes"):
                break
        for plugin, plan in follow.items():
            try:
                k = apply_plan(paths[plugin], plugin, plan, world, log, backup=plugin not in backed_up)
                backed_up.add(plugin)
                esp_plans.setdefault(plugin, {})
                print(f"  {plugin}: {k} change(s) written")
            except (OSError, FormatError) as e:
                print(f"  {plugin}: NOT changed ({e})")
        if follow_esm:
            for plugin, plan in follow_esm.items():
                dest = esm_plans.setdefault(plugin, defaultdict(list))
                for off, items in plan.items():
                    for item in items:
                        if item not in dest[off]:
                            dest[off].append(item)
            build_patch()
    log.extend(patch_log)
    log.extend(place_log)
    patch_path = patch_path if patch_built[0] else None

    with MT.open_log(args.fix_log, "a", encoding="utf-8") as f:
        import datetime
        f.write(f"\n==== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ====\n")
        for plugin, msg, desc in log:
            f.write(f"{plugin}: {msg + ' -> ' if msg else ''}{desc}\n")
    print(f"Fix log appended to {args.fix_log}")

    if fresh is not None:
        report2 = fresh
        print("\nFinal check" + (" (patches in their load-order positions):" if patch_path else ":"))
    else:
        print("\nRe-scanning" + (" with the patches in their load-order positions..." if patch_path else "..."))
        report2 = Report()
        scan(rescan_args, rescan_list(), bsas, data_dirs, report2)
    covered = set()
    for pp in (patch_path, place_path if place_built[0] else None):
        if pp:
            covered |= patch_contents(pp)
    report2.issues = [i for i in report2.issues if not covered_by_patches(i, covered)]
    report2.fixes = [f for f in report2.fixes
                     if f[0].lower().endswith(FIXABLE_EXT) or (f[5].lower(), f[6].lower()) not in covered]
    for plugin in list(dict.fromkeys(list(esp_plans) + list(item_esp) + list(esm_plans) + list(item_esm))) \
            + ([PATCH_NAME] if patch_path else []) \
            + ([PLACE_PATCH_NAME] if place_built[0] else []):
        left = sum(1 for i in report2.issues if i[0] == "ERROR" and i[1] == plugin)
        print(f"  {plugin}: {left} ERROR(s) remaining")
    done = set(esp_plans) | set(esm_plans) | set(item_esp) | set(item_esm) | set(OUR_PATCHES)
    others = sum(1 for i in report2.issues if i[0] == "ERROR" and i[1] not in done)
    print(f"  other plugins: {others} ERROR(s)")
    more = sum(1 for f in report2.fixes if not is_our_patch(f[0])
               and (only is None or f[0].lower() in only) and f[0].lower() not in world.unfixable)
    if more:
        print(f"\n{more} more fix(es) became possible because of these changes "
              f"(e.g. links to responses that were just deleted). Run --fix again to apply them.")
    # Keep generated repair patches active in MWManager's authoritative order. --full manages
    # these positions itself as it sorts between its internal passes.
    if not IN_FULL and not getattr(args, "init_scope", False) and (patch_path or place_built[0]):
        game_dir = os.path.dirname(os.path.abspath(args.data_dir))
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
            # Multipatch is the final plugin when present; PlaceholderCell precedes it.
            multi_at = next((i for i, name in enumerate(current)
                             if name.lower() == MULTI_PATCH_NAME.lower()), len(current))
            current.insert(multi_at, PLACE_PATCH_NAME)
        write_load_order(game_dir, current, "--fix generated patches")
        print(f"\nGenerated patch order saved to {LOAD_ORDER_FILE}; Morrowind.ini will be synced when this run finishes.")


def run_cell_conflicts(args, plugins, collect=False):
    """--cell-conflicts: plugins that don't know about each other (neither is a master of the other)
    but work on the same cells: both add many new objects in the same spot, both change the same
    placed objects differently, or both change the same cell settings. Report only."""
    if not collect:
        print("\n" + "=" * 70 + "\nCELL CONFLICTS\n" + "=" * 70)
    min_new = getattr(args, "conflict_min", 5)
    skip = {x.lower() for x in OUR_PATCHES} | {AUTO_MERGE_NAME.lower(), CELL_WINNER_PATCH.lower()}
    black = read_conflict_blacklist(os.path.dirname(os.path.abspath(args.data_dir)))
    left_out = [n for n, _p in plugins if conflict_blacklisted(n, black)]
    skip |= {n.lower() for n in left_out}
    if left_out and not collect:
        print(f"  left out ({CONFLICT_BLACKLIST_FILE}: they always lose): " + ", ".join(left_out))
    order = [(n, p) for n, p in plugins if n.lower() not in skip]
    pos = {n.lower(): i for i, (n, _p) in enumerate(order)}
    name_of = {n.lower(): n for n, _p in order}
    masters = {}
    for n, p in order:
        try:
            masters[n.lower()] = [m.lower() for m in read_header_masters(p)]
        except (OSError, FormatError):
            masters[n.lower()] = []
    closure = {}

    def deps(n):
        if n not in closure:
            closure[n] = set()
            seen, stack = set(), list(masters.get(n, []))
            while stack:
                x = stack.pop()
                if x not in seen:
                    seen.add(x)
                    stack += masters.get(x, [])
            closure[n] = seen
        return closure[n]

    related = lambda a, b: a in deps(b) or b in deps(a)
    labels = {}
    new_objs = defaultdict(dict)          # cell key -> plugin -> [(id, x, y, z)]
    settings = defaultdict(list)          # cell key -> [(plugin, {field: value})]
    edits = defaultdict(list)             # (owner, index) -> [(plugin, digest, deleted, object, cell key)]
    cells_of = defaultdict(set)           # plugin -> cells it has a record for (patches are judged by these)
    own_pos = defaultdict(dict)           # interior -> (owner, index) -> (object, x, y, z, owner)
    deletions = defaultdict(lambda: defaultdict(set))   # interior -> plugin -> master references it deletes
    door_final = {}                       # door ref -> (plugin index, cell key, (x,y,z), dest cell, (x,y,z,rz), deleted)
    moved = {}                            # (interior, ref) -> (plugin index, x, y, z): the last plugin to place it

    # interiors where you chose whose version wins (CELL_WINNERS_FILE): the Cell Fixes patch takes the
    # loser's work there out, so it doesn't count against anyone in that place
    game_dir = os.path.dirname(os.path.abspath(args.data_dir))
    lost_in = {("i", k): l.lower() for k, (_c, w, l) in read_cell_winners(game_dir).items()
               if w.lower() in pos and l.lower() in pos}

    def read(n, p, keys=None, own_digests=None):
        try:
            data = open(p, "rb").read()
        except OSError:
            return
        mast = masters.get(n.lower(), [])
        esm = n.lower().endswith(".esm")
        for off, tag, flags, body in iter_records(data):
            if tag != "CELL":
                continue
            key, label = cell_key(body)
            if key is None or (keys is not None and key not in keys and own_digests is None):
                continue
            head, pers, _n0, temp = split_cell(body)
            if own_digests is None and lost_in.get(key) == n.lower():
                cells_of[n.lower()].add(key)
                continue                         # its version of this place is set aside
            if own_digests is None:
                cells_of[n.lower()].add(key)
                labels.setdefault(key, label)
                st = _cell_settings(head)
                if st:
                    settings[key].append((n.lower(), st))
            pts = []
            for block in pers + temp:
                bk = block_key(block, mast, n.lower())
                if bk is None:
                    continue
                if own_digests is not None:
                    if bk in own_digests:
                        own_digests[bk] = block_digest(block)
                    continue
                oid = next((cstr(d) for s, d in block if s == "NAME"), "")
                deleted = any(s == "DELE" for s, _d in block)
                # the last version of each door's destination (.esm files are read after the .esp files,
                # so the load order position decides, not the reading order)
                dt = next((d for s, d in block if s == "DODT" and len(d) >= 24), None)
                prev = door_final.get(bk)
                if (dt is not None or (deleted and prev)) and (prev is None or prev[0] <= pos[n.lower()]):
                    if dt is None:
                        door_final[bk] = (pos[n.lower()],) + prev[1:5] + (True,)
                    else:
                        dn = next((cstr(d).lower() for s, d in block if s == "DNAM"), "")
                        dd = next((d for s, d in block if s == "DATA" and len(d) >= 24), None)
                        to = struct.unpack_from("<6f", dt)
                        door_final[bk] = (pos[n.lower()], key, struct.unpack_from("<3f", dd) if dd else None,
                                          dn, (to[0], to[1], to[2], to[5]), deleted)
                if bk[0] == n.lower():
                    if deleted:
                        continue
                    dd = next((d for s, d in block if s == "DATA" and len(d) >= 24), None)
                    if dd is not None:
                        x, y, z = struct.unpack_from("<3f", dd)
                        pts.append((oid, x, y, z))
                        if key[0] == "i":
                            own_pos[key][bk] = (oid, x, y, z, n.lower())
                else:
                    if deleted and key[0] == "i":
                        deletions[key][n.lower()].add(bk)
                    elif key[0] == "i":            # moved by a later plugin (a patch): where it ends up
                        dd = next((d for s, d in block if s == "DATA" and len(d) >= 24), None)
                        if dd is not None and moved.get((key, bk), (-1,))[0] < pos[n.lower()]:
                            moved[(key, bk)] = (pos[n.lower()],) + struct.unpack_from("<3f", dd)
                    if not esm:
                        edits[bk].append((n.lower(), block_digest(block), deleted, oid, key))
            if pts:
                new_objs[key][n.lower()] = pts

    print(f"  Reading cells in {len(order)} plugins...", file=sys.stderr)
    for n, p in order:                     # the .esp files first: they decide which cells matter
        if not n.lower().endswith(".esm"):
            read(n, p)
    esp_cells = set(new_objs) | set(settings)
    for n, p in order:
        if n.lower().endswith(".esm"):
            read(n, p, keys=esp_cells)
    for (key, bk), (_pi, x, y, z) in moved.items():
        v = own_pos.get(key, {}).get(bk)
        if v is not None and pos.get(v[4], -1) < _pi:
            own_pos[key][bk] = (v[0], x, y, z, v[4])
    # the master's own version of each contested reference, so unchanged ("dirty") copies don't count
    contested = {bk: v for bk, v in edits.items() if len({d for _p, d, _x, _o, _k in v}) > 1
                 and len({p for p, *_r in v}) > 1}
    by_owner = defaultdict(dict)
    for bk in contested:
        by_owner[bk[0]][bk] = None
    for owner, want in by_owner.items():
        p = dict((n.lower(), pp) for n, pp in order).get(owner)
        if p:
            read(name_of[owner], p, keys=set(), own_digests=want)

    pairs = defaultdict(lambda: {"place": [], "refs": [], "settings": [], "score": 0})

    def pair_key(a, b):
        return (a, b) if pos[a] < pos[b] else (b, a)

    # 1. new objects in the same spot
    for key, per in new_objs.items():
        big = [(pl, pts) for pl, pts in per.items() if len(pts) >= min_new]
        if len(big) < 2:
            continue
        radius = 256.0 if key[0] == "e" else 128.0
        for i in range(len(big)):
            for j in range(i + 1, len(big)):
                (a, pa), (b, pb) = big[i], big[j]
                if related(a, b):
                    continue
                grid = defaultdict(list)
                for o in pb:
                    grid[(int(o[1] // radius), int(o[2] // radius))].append(o)
                close, dup, example = 0, 0, None
                for o in pa:
                    gx, gy = int(o[1] // radius), int(o[2] // radius)
                    near = [q for dx in (-1, 0, 1) for dy in (-1, 0, 1) for q in grid.get((gx + dx, gy + dy), ())
                            if (q[1] - o[1]) ** 2 + (q[2] - o[2]) ** 2 <= radius * radius and abs(q[3] - o[3]) <= radius]
                    if near:
                        close += 1
                        example = example or (o[0], near[0][0])
                        if any(q[0].lower() == o[0].lower() and (q[1] - o[1]) ** 2 + (q[2] - o[2]) ** 2 < 64 for q in near):
                            dup += 1
                frac = close / min(len(pa), len(pb))
                if close < 3:
                    continue
                major = close >= 10 and frac >= 0.25
                pk = pair_key(a, b)
                if dup:
                    example = next(((o[0], o[0]) for o in pa for q in grid.get((int(o[1] // radius), int(o[2] // radius)), ())
                                    if q[0].lower() == o[0].lower() and (q[1] - o[1]) ** 2 + (q[2] - o[2]) ** 2 < 64), example)
                pairs[pk]["place"].append((key, len(per[pk[0]]), len(per[pk[1]]), close, dup, example, major))
                pairs[pk]["score"] += (100 if major else 10) + close + 5 * dup
    # 2. the same placed objects changed differently
    master_dig = {}
    for owner, want in by_owner.items():
        master_dig.update(want)
    for bk, v in contested.items():
        md = master_dig.get(bk)
        real = [(p, d, x, o, k) for p, d, x, o, k in v if d != md]
        for i in range(len(real)):
            for j in range(i + 1, len(real)):
                a, b = real[i], real[j]
                if a[0] == b[0] or a[1] == b[1] or related(a[0], b[0]):
                    continue
                pk = pair_key(a[0], b[0])
                pairs[pk]["refs"].append((a[4], a[3] or b[3], a[2] or b[2]))
                pairs[pk]["score"] += 2
    # 3. cell settings
    for key, hist in settings.items():
        base = {}
        for pl, st in hist:
            if pl.endswith(".esm"):
                base.update(st)
        for field in CELL_SETTING_NAMES:
            ch = [(pl, st[field]) for pl, st in hist if not pl.endswith(".esm") and field in st
                  and st[field] != base.get(field)]
            for i in range(len(ch)):
                for j in range(i + 1, len(ch)):
                    (a, va), (b, vb) = ch[i], ch[j]
                    if a == b or va == vb or related(a, b):
                        continue
                    pk = pair_key(a, b)
                    later_v, earlier_v = (vb, va) if pos[b] > pos[a] else (va, vb)
                    pairs[pk]["settings"].append((key, field, earlier_v, later_v))
                    pairs[pk]["score"] += 3
    if args.only:
        only = {o.lower() for o in args.only}
        pairs = {k: v for k, v in pairs.items() if k[0] in only or k[1] in only}
    if not pairs and not collect:
        print("  No conflicts found between plugins that don't know about each other.")
        return 0

    def cell_name(key):
        lab = (labels.get(key) or "").strip()
        if key[0] == "i":
            return f"'{lab}'"
        return f"{lab}" if lab and not lab.startswith("(") else f"Wilderness ({key[1]}, {key[2]})"

    total_new = Counter()
    for per in new_objs.values():
        for pl, pts in per.items():
            total_new[pl] += len(pts)
    for (a, b), c in pairs.items():                 # nearly everything placed twice: two versions of one mod
        dups = sum(p[4] for p in c["place"])
        c["doubled"] = dups >= 20 and dups >= 0.6 * min(total_new[a], total_new[b])
        c["dups"] = dups
        if c["doubled"]:
            c["score"] += 100000
    # a patch for both - a plugin with both as masters that has records for every cell where they conflict -
    # has already decided those cells: the conflict is void. One that covers only some cells is mentioned.
    patched = {}
    active = list(pos)
    for (a, b), c in list(pairs.items()):
        cells = {t[0] for t in c["place"]} | {t[0] for t in c["refs"]} | {t[0] for t in c["settings"]}
        best, best_n = [], 0
        for pl in active:
            if pl in (a, b) or not ({a, b} <= deps(pl)):
                continue
            n = len(cells & cells_of.get(pl, set()))
            if n == len(cells):
                best.append(pl)
            elif n > best_n:
                best_n, partial = n, pl
        if best:
            patched[(a, b)] = (c, best)
            del pairs[(a, b)]
        elif best_n:
            c["patched_some"] = (partial, best_n, len(cells))
    if collect:
        return {"pairs": pairs, "name_of": name_of, "total_new": total_new, "pos": pos, "masters": masters}
    if not args.dry_run:
        try:
            write_collide_data(args, order, new_objs, labels, pos, name_of, deps, cells_of)
        except (OSError, ValueError) as e:
            print(f"  (could not write {COLLIDE_DATA}: {e})", file=sys.stderr)
    doors_at, arrivals = defaultdict(list), defaultdict(list)
    for bk, (_pi, ckey, at, dest, to, dele) in door_final.items():
        if dele:
            continue
        arrivals[dest].append(to)
        doors_at[ckey].append((at, dest, to))
    lost = []
    report_rearranged(order, own_pos, deletions, new_objs, labels, pos, name_of, deps, cells_of,
                      doors_at, arrivals, lost)
    if not args.dry_run:
        try:
            write_stranded_data(args, order, lost)
        except (OSError, ValueError) as e:
            print(f"  (could not write {STRANDED_DATA}: {e})", file=sys.stderr)
    if patched:
        print(f"  {len(patched)} pair(s) already handled by a patch (a plugin made for both, covering every cell "
              f"where they conflict); not counted below:")
        for (a, b), (c, pls) in sorted(patched.items(), key=lambda kv: -kv[1][0]["score"]):
            print(f"    {name_of[a]}  vs  {name_of[b]}: patched by {', '.join(name_of[x] for x in pls)}")
        if not pairs:
            print("  No other conflicts.")
            return 0
    # A small number of adjacent objects are handled by mwcollide, which hides only the actual overlap.
    # Keep these out of the main conflict report unless the user explicitly asks to see them.
    def collide_only(conflict):
        return (not conflict["refs"] and not conflict["settings"] and not conflict.get("doubled")
                and conflict["place"] and not any(place[6] for place in conflict["place"]))

    by_collide = {key: conflict for key, conflict in pairs.items() if collide_only(conflict)}
    if by_collide:
        pairs = {key: conflict for key, conflict in pairs.items() if key not in by_collide}
        print(f"  {len(by_collide)} pair(s) only put a few objects near each other: mwcollide hides the ones that "
              "overlap as the cell loads; not counted below (--conflict-all-places lists them)")
        if getattr(args, "conflict_all_places", False):
            for (a, b), conflict in sorted(by_collide.items(), key=lambda item: -item[1]["score"]):
                print(f"    {name_of[a]}  vs  {name_of[b]}: {sum(place[3] for place in conflict['place'])} object(s) "
                      f"near each other in {len(conflict['place'])} cell(s)")
        if not pairs:
            print("  No other conflicts.")
            return 0
    ranked = sorted(pairs.items(), key=lambda kv: -kv[1]["score"])
    majors = [kv for kv in ranked if any(p[6] for p in kv[1]["place"]) or len(kv[1]["refs"]) >= 20]
    doubled = [kv for kv in ranked if kv[1]["doubled"]]
    print(f"  {len(pairs)} pair(s) of plugins conflict; {len(majors)} major"
          + (f", {len(doubled)} of them DOUBLED (the same objects placed twice)" if doubled else "")
          + ". Most serious first.")
    print("  (pairs where one plugin is a master of the other are left out: one was made with the other in view)")
    shown = ranked if args.conflict_all else ranked[:args.conflict_top]
    remembered = read_conflict_answers(os.path.dirname(os.path.abspath(args.data_dir)))
    _rp = os.path.join(os.path.dirname(os.path.abspath(args.data_dir)), RULES_FILE)
    try:                                           # every [Order]/[Exact]/[Near*] rule in the rules file
        _rt = open(_rp, encoding="cp1252", errors="replace").read() if os.path.exists(_rp) else ""
        _rules = ordering_rules(parse_rules(_rp, _rt)[0])
    except Exception:
        _rules = []

    def ruled_first(a, b):
        """Return the plugin loaded first by a user [Order] or [Exact] rule, if any."""
        for rule in _rules:
            if a in rule and b in rule:
                return a if rule.index(a) < rule.index(b) else b
        return None

    def answer_text(a, b):
        r_ = remembered.get("|".join(sorted((a, b))))
        if not r_ or r_.get("do") == "skip":
            first = ruled_first(a, b)
            if first:
                second = b if first == a else a
                return (f"decided by an [Order] rule in {RULES_FILE}: {name_of.get(second, second)} loads after "
                        f"{name_of.get(first, first)}, so its version wins; not asked")
            if a.lower().endswith(".esm") != b.lower().endswith(".esm"):
                return ("not asked: an .esm always loads before an .esp, so the .esp's version wins and no "
                        "order can change it (turn one off, or a patch that moves one, if it looks wrong)")
            return "no answer yet (you'll be asked)"
        who, when = r_.get("plugin", ""), r_.get("when", "")
        other = next((x for x in r_.get("pair", []) if x.lower() != who.lower()), "the other")
        what = {"order": f"{other} wins ([Order] rule: {who} loads first)",
                "cell": f"{who}'s version of the rebuilt interior(s) wins (Cell Fixes)",
                "off": f"turn off {who}",
                "skip": "skipped (left as it is)"}.get(r_.get("do"), str(r_.get("do")))
        return what + (f"  [{when}]" if when else "")

    for (a, b), c in shown:
        major = any(p[6] for p in c["place"]) or len(c["refs"]) >= 20
        kind = "DOUBLED" if c["doubled"] else "MAJOR" if major else "minor"
        print(f"\n=== {kind}: {name_of[a]}  vs  {name_of[b]}")
        print(f"    your answer: {answer_text(a, b)}")
        if c["doubled"]:
            print(f"    {c['dups']} identical objects are placed twice in the same spot (of {total_new[a]} and "
                  f"{total_new[b]} new objects): these look like two versions of the same mod, or one already "
                  f"contains the other. Keep only one of them active")
        print(f"    {name_of[b]} loads later, so where they disagree its version wins")
        if c.get("patched_some"):
            pl, k, t = c["patched_some"]
            print(f"    {name_of[pl]} patches the two of them, but only in {k} of the {t} cell(s) where they conflict")
        if c["place"]:
            places = sorted(c["place"], key=lambda t: -t[3])
            print(f"    builds in the same spot in {len(places)} cell(s):")
            for key, na, nb, close, dup, ex, mj in places[:6]:
                extra = f"; {dup} identical object(s) placed twice" if dup else ""
                print(f"      {cell_name(key)}: adds {na} vs {nb} new objects, {close} of them within "
                      f"{'256' if key[0] == 'e' else '128'} units of the other's{extra}"
                      + (f" (e.g. '{ex[0]}' next to '{ex[1]}')" if ex else ""))
            if len(places) > 6:
                print(f"      ... and {len(places) - 6} more cell(s)")
        if c["refs"]:
            cells = Counter(k for k, _o, _d in c["refs"])
            dels = sum(1 for _k, _o, d in c["refs"] if d)
            print(f"    both change {len(c['refs'])} of the same placed objects differently"
                  + (f" ({dels} deleted by one of them)" if dels else "") + ", in " +
                  ", ".join(f"{cell_name(k)} ({n})" for k, n in cells.most_common(4))
                  + (f" and {len(cells) - 4} more cell(s)" if len(cells) > 4 else ""))
        if c["settings"]:
            print("    both change the same cell settings:")
            for key, field, ev, lv in c["settings"][:5]:
                print(f"      {cell_name(key)} {CELL_SETTING_NAMES[field]}: {_show_setting(field, ev)}  ->  "
                      f"{_show_setting(field, lv)} (wins)")
            if len(c["settings"]) > 5:
                print(f"      ... and {len(c['settings']) - 5} more")
        if c["doubled"]:
            pass
        elif c["place"]:
            print("    -> the two build over each other: expect overlapping or floating objects. Usually only "
                  "one of them should be used there, or a compatibility patch that moves one")
        elif c["refs"] or c["settings"]:
            print(f"    -> if {name_of[a]}'s changes should win, load it after {name_of[b]} "
                  f"(an [Order] rule); if both matter, a patch that combines them is needed")
    if len(ranked) > len(shown):
        print(f"\n  ... and {len(ranked) - len(shown)} smaller conflict(s) (--conflict-all lists them all)")
    interactive = ((os.environ.get("MWMANAGER_CONFLICT_GUI") == "1" or
                    (sys.stdin.isatty() and sys.stdout.isatty())) and not args.dry_run
                   and not getattr(args, "no_interactive", False))
    if interactive or (getattr(args, "use_answers", False) and not args.dry_run):
        # every pair is asked (or replayed), not only the ones printed above: --conflict-top limits the summary
        return conflict_session(args, ranked, name_of, masters, total_new, plugins, labels)
    if getattr(args, "no_interactive", False) and not args.dry_run:
        # Preserve unanswered candidates for the GUI's post-generation review step. Always overwrite
        # this file, including when there are no candidates, so a previous run cannot trigger stale prompts.
        answered = read_conflict_answers(os.path.dirname(os.path.abspath(args.data_dir)))
        pending = ["|".join(sorted(pair)) for pair, _conflict in ranked if "|".join(sorted(pair)) not in answered]
        try:
            with open(output_file(NEW_CONFLICTS_FILE), "w", encoding="utf-8") as candidate_file:
                candidate_file.write("\n".join(pending))
        except OSError as exc:
            print(f"  Could not save unresolved cell-conflict candidates: {exc}")
    return 0


def conflict_session(args, ranked, name_of, masters, total_new, plugins=(), labels=None):
    """--cell-conflicts in a console: go through the conflicts, most serious first, and for each one
    add an [Order] rule (either way) or turn one of the two plugins off. Offers --sort at the end."""
    game_dir = os.path.dirname(os.path.abspath(args.data_dir))
    rules_path = manager_file(RULES_FILE)
    needed = {m for ms in masters.values() for m in ms}            # masters of something active
    # what earlier runs of this session added is replaced, not added to: this run's answers are the ones that count
    text = open(rules_path, encoding="cp1252", errors="replace").read() if os.path.exists(rules_path) else ""
    own_text, earlier = split_conflict_rules(text)
    existing = ordering_rules(parse_rules(rules_path, own_text)[0])
    earlier_l = {(a.lower(), b.lower()) for a, b in earlier}
    if earlier and not args.dry_run:
        # wiped before anything is read or asked: this session starts from your own rules only
        shutil.copy2(rules_path, backup_file("state", f"mwmanager_rules-{time.strftime('%Y%m%d-%H%M%S')}.bak"))
        with open(rules_path, "w", encoding="cp1252", errors="replace", newline="\r\n") as f:
            f.write(own_text)
        print(f"\nRemoved the {len(earlier)} [Order] rule(s) earlier --cell-conflicts runs added to {RULES_FILE} "
              f"(backed up first); a pair an earlier run decided shows what it chose.")

    def has_rule(first, second):
        return any(first in r and second in r and r.index(first) < r.index(second) for r in existing)

    labels = labels or {}
    winners = read_cell_winners(game_dir)
    new_winners = {}

    def rebuilt(c):
        """The interiors both rebuilt (major: much of each built over the other)."""
        return [p[0] for p in c["place"] if p[6] and p[0][0] == "i"]

    def chosen(a, b, c):
        cs = rebuilt(c)
        return bool(cs) and all(k[1] in winners and {winners[k[1]][1].lower(), winners[k[1]][2].lower()} == {a, b}
                                for k in cs)

    new_rules, turned_off = [], []
    answers = read_conflict_answers(game_dir)        # last time's answers: the defaults
    answered = {}
    use_all = bool(getattr(args, "use_answers", False))     # --use-answers: last time's answer for every pair
    only_new = bool(getattr(args, "only_new", False))       # --only-new: replay answered pairs, ask the new ones
    unanswered = []                                         # pairs with no answer yet (left for you)
    # An .esm always loads before every .esp, so an [Order] rule can't change who wins between them:
    # list those pairs as warnings and only ask about the rest.
    # (one that both rebuilt an interior is still asked: whose version of the place wins doesn't depend on order)
    mixed = lambda k: k[0].endswith(".esm") != k[1].endswith(".esm")
    esm_vs_esp = [(k, c) for k, c in ranked if mixed(k) and not rebuilt(c)]
    if esm_vs_esp:
        print("\n" + "=" * 70 + f"\nWARNINGS: {len(esm_vs_esp)} conflict(s) between an .esm and an .esp"
              "\n(an .esm always loads first, so the .esp always wins; no [Order] rule can change that)\n" + "=" * 70)
        for (a, b), c in esm_vs_esp:
            esm, esp = (name_of[a], name_of[b]) if a.endswith(".esm") else (name_of[b], name_of[a])
            parts = []
            if c["place"]:
                parts.append(f"same spot in {len(c['place'])} cell(s)")
            if c["refs"]:
                parts.append(f"{len(c['refs'])} same objects")
            if c["settings"]:
                parts.append(f"{len(c['settings'])} cell setting(s)")
            extra = f"; {c['dups']} objects placed twice" if c.get("doubled") else ""
            print(f"  WARN {esp} overrides {esm}: {', '.join(parts)}{extra}")
        ranked = [(k, c) for k, c in ranked if not mixed(k) or rebuilt(c)]
    # interiors where you already chose whose version wins: decided
    picked = [(k, c) for k, c in ranked if chosen(k[0], k[1], c)]
    if picked:
        print("\n" + "=" * 70 + f"\nREBUILT INTERIORS ALREADY CHOSEN: {len(picked)} conflict(s) (not asked; "
              f"{CELL_WINNERS_FILE})\n" + "=" * 70)
        for (a, b), c in picked:
            for k in rebuilt(c):
                cell, w, _l = winners[k[1]]
                print(f"  WARN '{cell}': {w}'s version wins ({name_of[a]} vs {name_of[b]})")
        picked_keys = {k for k, _c in picked}
        ranked = [(k, c) for k, c in ranked if k not in picked_keys]
    # pairs your own rules already order: decided, so only a warning
    ruled = [(k, c) for k, c in ranked if has_rule(k[0], k[1]) or has_rule(k[1], k[0])]
    if ruled:
        print("\n" + "=" * 70 + f"\nALREADY DECIDED BY YOUR RULES: {len(ruled)} conflict(s) (not asked)\n" + "=" * 70)
        for (a, b), c in ruled:
            first, second = (a, b) if has_rule(a, b) else (b, a)
            extra = f"; {c['dups']} objects placed twice" if c.get("doubled") else ""
            print(f"  WARN {name_of[second]} wins over {name_of[first]} where they conflict (your [Order] rule){extra}")
        ruled_keys = {k for k, _c in ruled}
        ranked = [(k, c) for k, c in ranked if k not in ruled_keys]
    if not ranked:
        print("\nNo conflicts left to decide.")
        return 0
    print("\n" + "=" * 70 + "\nCHOOSE WHAT TO DO (most serious first; Enter takes last time's answer, or skips; "
          "s skips, a takes last time's answers for the rest, q stops)\n" + "=" * 70)
    for i, ((a, b), c) in enumerate(ranked, 1):
        if a in {x.lower() for x, _w in turned_off} or b in {x.lower() for x, _w in turned_off}:
            continue
        A, B = name_of[a], name_of[b]
        kind = "DOUBLED" if c.get("doubled") else "MAJOR" if (any(p[6] for p in c["place"]) or len(c["refs"]) >= 20) else "minor"
        parts = []
        if c["place"]:
            parts.append(f"build in the same spot in {len(c['place'])} cell(s)")
        if c["refs"]:
            parts.append(f"change {len(c['refs'])} of the same objects")
        if c["settings"]:
            parts.append(f"change {len(c['settings'])} cell setting(s)")
        print(f"\n[{i}/{len(ranked)}] {kind}: {A}  vs  {B}")
        print(f"    they {', '.join(parts)}; {B} loads later, so its version wins now")
        if c.get("patched_some"):
            pl, k, t = c["patched_some"]
            print(f"    ({name_of.get(pl, pl)} patches the two of them, but only in {k} of these {t} cell(s))")
        if c.get("doubled"):
            print(f"    {c['dups']} identical objects are placed twice: two versions of one mod, keep only one")
        if (a, b) in earlier_l:
            print(f"    (an earlier run chose: {B} after {A})")
        elif (b, a) in earlier_l:
            print(f"    (an earlier run chose: {A} after {B})")
        if mixed((a, b)):
            opts = {}
            print("    (an .esm always loads before an .esp, so no [Order] rule changes who wins elsewhere)")
        else:
            opts = {"1": ("order", A, B, f"keep {B} winning"), "2": ("order", B, A, f"make {A} win")}
            print(f"  [1] keep  {B} winning   (adds [Order] {A} > {B})")
            print(f"  [2] make  {A} win   (adds [Order] {B} > {A})")
        rb = rebuilt(c)
        if rb:
            names = ", ".join(f"'{labels.get(k, k[1])}'" for k in rb[:4]) + (f" and {len(rb) - 4} more" if len(rb) > 4 else "")
            print(f"    both rebuilt {names}")
            opts["5"] = ("cell", B, A, f"{B}'s version of the rebuilt interior(s)")
            opts["6"] = ("cell", A, B, f"{A}'s version of the rebuilt interior(s)")
            print(f"  [5] use {B}'s version of {'that place' if len(rb) == 1 else 'those places'}: {A}'s objects "
                  f"there are removed and its doors in arrive in {B}'s layout")
            print(f"  [6] use {A}'s version of {'that place' if len(rb) == 1 else 'those places'} (the same, the "
                  f"other way round)")
        for key, n in (("3", A), ("4", B)):
            hit = relied_on(plugins, n, [x for x, _w in turned_off]) if plugins and n.lower() not in needed else {}
            if n.lower() in needed:
                print(f"  [{key}] (can't turn off {n}: another active plugin needs it as a master)")
            elif hit:
                print(f"  [{key}] (can't turn off {n}: {relied_on_text(hit, name_of)})")
            else:
                opts[key] = ("off", n, A if n == B else B, f"turn off {n}")
                print(f"  [{key}] turn off {n}")
        pair_key = "|".join(sorted((a, b)))
        last = answers.get(pair_key) or {}
        default = ""
        if last.get("do") == "skip":
            default = "s"
        elif last.get("do") and last.get("plugin"):
            want = last["plugin"].lower()
            default = next((k for k, (w, x, _y, _l) in opts.items()
                            if w == last["do"] and x.lower() == want), "")
        if default:
            shown = "skip" if default == "s" else f"[{default}] {opts[default][3]}"
            print(f"  [Enter] last time: {shown}   [s] skip   [a] last time's answers for the rest   [q] stop")
        else:
            print("  [Enter] skip   [a] last time's answers for the rest   [q] stop"
                  + ("   (last time's answer no longer fits)" if last else ""))
        if (use_all or only_new) and not last:
            unanswered.append(pair_key)
            if use_all:
                print("  > (new conflict: no answer yet; left for you)")
                continue
        if use_all or (only_new and last):
            ans = default or "s"
            print(f"  > {ans}  (last time's answer)")
        else:
            try:
                if os.environ.get("MWMANAGER_CONFLICT_GUI") == "1":
                    from mwmanager_workflow import conflict_answer
                    details = (f"{A} vs {B}\n\nThey {', '.join(parts)}.\n"
                               f"{B} loads later and currently wins.\n\n")
                    affected = list(dict.fromkeys(
                        str(labels.get(item[0], item[0][1]))
                        for field in ("place", "refs", "settings") for item in c[field]))
                    if affected:
                        details += ("Affected cells:\n" + "\n".join(affected[:20]) +
                                    (f"\n... and {len(affected) - 20} more" if len(affected) > 20 else "") + "\n\n")
                    if c.get("doubled"):
                        details += f"{c['dups']} identical objects are placed twice. Usually keep only one version.\n\n"
                    if rb:
                        details += ("Both rebuilt these interiors:\n" +
                                    "\n".join(str(labels.get(k, k[1])) for k in rb) + "\n\n")
                    details += ("Ordering changes who wins. Interior choices rebuild the affected cells. "
                                "Disabling a plugin removes it from the active load order; required plugins "
                                "cannot be disabled.\n\nChoose Skip to leave this pair unchanged, "
                                "or Finish review to keep decisions already made.")
                    ans = conflict_answer(
                        f"Cell conflict {i} of {len(ranked)}: {kind}", details,
                        [(value[3], key) for key, value in opts.items()] +
                        [("Skip this pair", "s"), ("Finish review", "q")], default or "s")
                else:
                    ans = input("  > ").strip().lower()
            except EOFError:
                break
            if ans == "q":
                break
            if ans == "a":
                # last time's answers for every pair that has one; a new pair is still asked
                if last:
                    ans = default or "s"
                    print("    (taking last time's answers from here on; new conflicts are still asked)")
                else:
                    print("    (taking last time's answers from here on; new conflicts like this one are still "
                          "asked)")
                    valid = set(opts) | {"", "s", "a", "q"}
                    try:
                        ans = input("  > ").strip().lower()
                        while ans not in valid:
                            print(f"    '{ans}' isn't one of the choices; try again")
                            ans = input("  > ").strip().lower()
                    except EOFError:
                        break
                    if ans == "q":
                        break
                    if ans in ("", "a"):
                        ans = default or "s"
            elif ans == "":
                ans = default or "s"
        if ans not in opts:
            if ans == "s":
                answered[pair_key] = {"do": "skip", "plugin": "", "pair": [A, B], "when": time.strftime("%Y-%m-%d %H:%M")}
            continue
        what, x, y, label = opts[ans]
        answered[pair_key] = {"do": what, "plugin": x, "pair": [A, B], "when": time.strftime("%Y-%m-%d %H:%M")}
        if what == "cell":
            for k in rebuilt(c):
                new_winners[k[1]] = (labels.get(k, k[1]), x, y)
            print(f"    -> {label} ({x} wins, built by --fix as {CELL_WINNER_PATCH})")
            continue
        if what == "order":
            new_rules.append((x, y, label))
            existing.append([x.lower(), y.lower()])
            print(f"    -> [Order] {x} > {y}")
        else:
            turned_off.append((x, f"turned off by --cell-conflicts (conflicts with {y})"))
            print(f"    -> {x} will be turned off")
    if not args.dry_run:                             # what is still unanswered, for --full's last question
        try:
            with open(output_file(NEW_CONFLICTS_FILE), "w", encoding="utf-8") as _nf:
                _nf.write("\n".join(p_ for p_ in unanswered if p_ not in answered))
        except OSError:
            pass
    if answered and not args.dry_run:
        answers.update(answered)
        write_conflict_answers(game_dir, answers)
        print(f"\nRemembered {len(answered)} answer(s) in {CONFLICT_ANSWERS_FILE}: next time they're the default.")
    if new_winners and not args.dry_run:
        winners.update(new_winners)
        write_cell_winners(game_dir, winners)
        print(f"\nRecorded {len(new_winners)} rebuilt interior choice(s) in {CELL_WINNERS_FILE}.")
    if not new_rules and not turned_off and not new_winners:
        print("\nNo new rules and no plugins turned off." + (" (Earlier runs' conflict rules were removed; run --sort so the order follows your own rules.)" if earlier and not args.dry_run else ""))
        return 0
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if new_rules:
        if os.path.exists(rules_path):
            shutil.copy2(rules_path, backup_file("state", f"mwmanager_rules-{time.strftime('%Y%m%d-%H%M%S')}.bak"))
        with open(rules_path, "w", encoding="cp1252", errors="replace", newline="\r\n") as f:
            f.write(own_text.rstrip("\n") + "\n" if own_text.strip() else "")
            f.write(f"\n{CONFLICT_RULES_HEADER} {stamp} ----\n")
            for x, y, label in new_rules:
                f.write(f"\n[Order]\n{x}\n{y}\n; {label} where they conflict\n")
        print(f"\nWrote {len(new_rules)} [Order] rule(s) to {RULES_FILE}"
              + (f", replacing the {len(earlier)} from earlier --cell-conflicts runs" if earlier else "")
              + " (old file backed up).")
    if turned_off:
        gone = {n.lower() for n, _w in turned_off}
        cur = read_load_order(args) or []
        set_game_files(default_ini(args), [n for n in cur if n.lower() not in gone], "--cell-conflicts")
        moved = move_plugins_to_disabled(args.data_dir, [n for n, _w in turned_off])
        rec = read_disabled(game_dir)
        for n, w in turned_off:
            rec[n] = w
        write_disabled(game_dir, rec)
        print(f"Turned off {len(turned_off)} plugin(s): {', '.join(n for n, _w in turned_off)} "
              f"({len(moved)} moved to MWManager\\disabled_plugins; listed in {DISABLED_RECORD}).")
    if IN_FULL:
        print("(--full sorts next, so the changes take effect.)")
        return 0
    if getattr(args, "defer_follow_up", False):
        print("(follow-up deferred; the calling workflow will sort and rebuild patches.)")
        return 0
    what = "--sort --fix --multipatch" if (turned_off or new_winners) else "--sort"
    if use_all and getattr(args, "use_answers", False):
        ans = "y"
        print(f"\nRunning {what} so the changes take effect (--use-answers).")
    else:
        try:
            ans = input(f"\nRun {what} now so the changes take effect? [Y/n] ").strip().lower()
        except EOFError:
            ans = "n"
    if ans in ("", "y", "yes"):
        argv2 = [args.data_dir, "--sort", "--yes", "--fix-log", args.fix_log, "--encoding", args.encoding]
        if turned_off or new_winners:
            # a plugin gone leaves the others' order as it was, but the multipatch's merged lists and
            # the fix patch were built with it: rebuild them, or they still name its objects
            argv2 += ["--fix", "--multipatch"]
        if args.ini:
            argv2 += ["--ini", args.ini]
        return main(argv2)
    print(f"Run mwmanager.bat {what} when you're ready.")
    return 0


def fix_cell_winners(args, plugins):
    """--fix: for each interior in CELL_WINNERS_FILE, the winning plugin's version of the place is used as if
    the losing plugin had never touched it, through CELL_WINNER_PATCH (rebuilt each time; no plugin changed):
      - the loser's own objects there are deleted (not its people and creatures, nor objects its scripts or
        dialogue name: those are kept and listed, as a quest may need them)
      - the objects the loser changed or deleted there get the version they'd have without it back
      - the cell's lighting/water/flags and its pathgrid are the ones they'd be without the loser
      - every door into the place (any plugin's, anywhere) that the loser set up is pointed at an arrival
        spot of the winner's layout: the one used from the same place if there is one, else the main one"""
    data_dir = os.path.abspath(args.data_dir)
    game_dir = os.path.dirname(data_dir)
    print("\n" + "=" * 70 + "\nCELL FIXES: rebuilt interiors, door arrivals, objects on reshaped land\n" + "=" * 70)
    entries = read_cell_winners(game_dir)
    out_path = os.path.join(data_dir, CELL_WINNER_PATCH)
    ours = {x.lower() for x in OUR_PATCHES} | {CELL_WINNER_PATCH.lower(), PERFORMER_PATCH.lower()}
    order = [(n, p) for n, p in plugins if n.lower() not in ours]
    pos = {n.lower(): i for i, (n, _p) in enumerate(order)}
    name_of = {n.lower(): n for n, _p in order}
    cells = {}
    for k, (cell, w, l) in entries.items():
        if w.lower() not in pos or l.lower() not in pos:
            print(f"  '{cell}': {w if w.lower() not in pos else l} isn't active, so there's nothing to choose; skipped")
            continue
        cells[k] = (cell, w.lower(), l.lower())

    def drop_patch():
        cur = read_load_order(args) or []
        if os.path.exists(out_path) and not args.dry_run:
            os.replace(out_path, backup_path(out_path))
            set_game_files(args.ini or os.path.join(game_dir, "Morrowind.ini"),
                           [n for n in cur if n.lower() != CELL_WINNER_PATCH.lower()], "--fix (cell winners)")
            print(f"  {CELL_WINNER_PATCH} no longer needed: removed.")
    if not cells:
        print("  Rebuilt interiors: none chosen (--cell-conflicts offers it for interiors two mods both rebuilt).")
    losers = {l for _c, _w, l in cells.values()}
    masters_of = {}                              # plugin -> its masters (lower)
    ipos = {}                                    # (interior, ref key) -> (plugin, x, y, z, deleted): latest version
    iorig = {}                                   # (interior, ref key) -> (x, y, z, z-rotation, id): its owner's version
    int_recs = defaultdict(list)                 # interior -> [(plugin index, offset)] of its CELL records
    item_ids = set()                             # things that get picked up and moved about: never anchors
    interiors = set()
    land_vers = defaultdict(list)                # (x, y) -> [(plugin index, VHGT digest, offset)] in load order
    ext_recs = defaultdict(list)                 # (x, y) -> [(plugin index, offset)] of exterior CELL records
    print("  Reading every plugin's cells, doors and pathgrids...", file=sys.stderr)
    actor_ids, cont_ids, loser_text = set(), set(), defaultdict(list)
    heads = {}                                   # cell key -> latest head
    xheads = defaultdict(list)                   # chosen interior -> [(plugin, head)] in load order
    xver = defaultdict(list)                     # (interior, ref key) -> [(plugin, group, block)]
    xpgrd = defaultdict(list)                    # interior -> [(plugin, raw PGRD record)]
    door_hist = defaultdict(list)                # ref key -> [(plugin, group, block, cell key)] of doors into them
    for pi, (n, p) in enumerate(order):
        low = n.lower()
        try:
            data = open(p, "rb").read()
            ms = [x.lower() for x in plugin_masters(data)]
            recs = list(iter_records(data))
        except (OSError, FormatError):
            continue
        masters_of[low] = ms
        for off, tag, fl, body in recs:
            if tag == "LAND":
                iv, vh = first_sub(body, b"INTV"), first_sub(body, b"VHGT")
                if iv and vh and len(iv) >= 8 and len(vh) >= 4 + 65 * 65:
                    land_vers[struct.unpack_from("<ii", iv)].append((pi, hashlib.md5(vh[:4 + 65 * 65]).digest(), off))
                continue
            if tag in ("MISC", "BOOK", "WEAP", "ARMO", "CLOT", "ALCH", "INGR", "APPA", "LOCK", "PROB", "REPA",
                       "LEVI"):
                nm = first_sub(body, b"NAME")
                if nm:
                    item_ids.add(cstr(nm).lower())
            if tag in ("NPC_", "CREA", "LEVC", "CONT"):
                nm = first_sub(body, b"NAME")
                if nm:
                    (cont_ids if tag == "CONT" else actor_ids).add(cstr(nm).lower())
            elif tag == "SCPT" and low in losers:
                loser_text[low].append((first_sub(body, b"SCTX") or b"").lower())
            elif tag == "INFO" and low in losers:
                loser_text[low].append((first_sub(body, b"BNAM") or b"").lower())
            elif tag == "PGRD":
                nm = cstr(first_sub(body, b"NAME") or b"").lower()
                if nm in cells:
                    xpgrd[nm].append((low, b"PGRD" + struct.pack("<III", len(body), 0, fl) + body))
            elif tag == "CELL":
                key, _label = cell_key(body)
                if key is None:
                    continue
                head, pers, _n0, temp = split_cell(body, off)
                heads[key] = head
                if key[0] == "e":
                    ext_recs[(key[1], key[2])].append((pi, off))
                else:
                    interiors.add(key[1])
                    int_recs[key[1]].append((pi, off))
                inx = key[0] == "i" and key[1] in cells
                if inx:
                    xheads[key[1]].append((low, head))
                for grp, blks in (("p", pers), ("t", temp)):
                    for blk in blks:
                        bk = block_key(blk, ms, low)
                        if bk is None:
                            continue
                        if inx:
                            xver[(key[1], bk)].append((low, grp, blk))
                        if key[0] == "i":
                            dd = next((d for s_, d in blk if s_ == "DATA" and len(d) >= 12), None)
                            dele = any(s_ == "DELE" for s_, _d in blk)
                            if dd is not None or dele:
                                x, y, z = struct.unpack_from("<3f", dd) if dd is not None else (0.0, 0.0, 0.0)
                                ipos[(key[1], bk)] = (low, x, y, z, dele)
                                if bk[0] == low and dd is not None and not dele:
                                    rz = struct.unpack_from("<f", dd, 20)[0] if len(dd) >= 24 else 0.0
                                    oid = next((cstr(d) for s_, d in blk if s_ == "NAME"), "").lower()
                                    iorig[(key[1], bk)] = (x, y, z, rz, oid)
                        if bk in door_hist or any(s_ == "DNAM" for s_, _d in blk):
                            door_hist[bk].append((low, grp, blk, key))

    def has(blk, sub):
        return any(s_ == sub for s_, _d in blk)

    def name_in(blk):
        return next((cstr(d) for s_, d in blk if s_ == "NAME"), "")

    out = defaultdict(dict)                      # cell key -> {(owner, index): (group, block to write)}
    out_heads, pgrds, used = {}, [], set()
    kept_bk = defaultdict(set)                   # interior -> the loser's objects left in

    def put(key, grp, bk, blk):
        out[key][bk] = (grp, blk)
        used.add(bk[0])

    for cl, (cell, w, l) in sorted(cells.items()):
        key = ("i", cl)
        hs = xheads.get(cl, [])
        if not hs:
            print(f"  '{cell}': no plugin has this interior any more; skipped")
            continue
        others = [h for pl, h in hs if pl != l]
        out_heads[key] = others[-1] if others else hs[-1][1]
        removed, kept, restored, gone_doors = [], [], 0, []
        text = b"\n".join(loser_text[l])
        for (c, bk), vers in xver.items():
            if c != cl or vers[-1][0] != l:
                continue                               # a plugin after the loser decides this one
            pl, grp, blk = vers[-1]
            if bk[0] == l:                             # the loser's own object
                if has(blk, "DELE"):
                    continue
                oid = name_in(blk)
                if oid.lower() in actor_ids or (oid and oid.lower().encode(ENC, MW_ERRORS) in text):
                    kept.append(oid)
                    kept_bk[cl].add(bk)
                    continue
                dest = next((cstr(d) for s_, d in blk if s_ == "DNAM"), None)
                if dest:
                    gone_doors.append(dest)
                nm = next((d for s_, d in blk if s_ == "NAME"), oid.encode(ENC, MW_ERRORS) + b"\0")
                put(key, grp, bk, [["FRMR", b""], ["NAME", nm], ["DELE", struct.pack("<I", 0)]])
                removed.append(oid)
            else:                                      # someone else's object the loser changed or deleted
                prev = [v for v in vers if v[0] != l]
                if not prev:
                    continue
                _pl, grp2, blk2 = prev[-1]
                if has(blk2, "DELE"):
                    nm = next((d for s_, d in blk2 if s_ == "NAME"), b"\0")
                    put(key, grp2, bk, [["FRMR", b""], ["NAME", nm], ["DELE", struct.pack("<I", 0)]])
                else:
                    put(key, grp2, bk, [list(x) for x in blk2])
                restored += 1
        pg = xpgrd.get(cl, [])
        if pg and pg[-1][0] == l:
            other_pg = [r for pl, r in pg if pl != l]
            if other_pg:
                pgrds.append(other_pg[-1])
        print(f"  '{cell}': {name_of[w]}'s version wins over {name_of[l]}'s: {len(removed)} of its object(s) "
              f"removed, {restored} changed object(s) put back")
        if kept:
            print(f"      kept (people, creatures, or named by its scripts or dialogue - may stand in the other "
                  f"layout's walls): {', '.join(sorted(set(kept))[:12])}{' ...' if len(set(kept)) > 12 else ''}")
        for dn in sorted(set(gone_doors)):
            print(f"      its door from here to '{dn}' goes too: check that place can still be reached")

    # doors into the chosen interiors
    good = defaultdict(list)                     # interior -> [(DODT, source cell key)]
    final = {}
    for bk, hist in door_hist.items():
        pl, grp, blk, ck = hist[-1]
        if has(blk, "DELE"):
            continue
        dn = next((cstr(d).lower() for s_, d in blk if s_ == "DNAM"), None)
        if dn not in cells or ck == ("i", dn):
            continue
        final[bk] = (pl, grp, blk, ck, dn)
        src = blk if pl != cells[dn][2] else next((h[2] for h in reversed(hist) if h[0] != cells[dn][2]
                                                   and not has(h[2], "DELE")), None)
        if src is not None and next((cstr(d).lower() for s_, d in src if s_ == "DNAM"), None) == dn:
            dodt = next((d for s_, d in src if s_ == "DODT" and len(d) >= 24), None)
            if dodt:
                good[dn].append((dodt, ck))     # the winner's (or the original) arrival spot
    fixed_doors, unfixable = 0, []
    for bk, (pl, grp, blk, ck, dn) in final.items():
        cell, w, l = cells[dn]
        if pl != l:
            continue
        hist = door_hist[bk]
        prev = [h for h in hist if h[0] != l and not has(h[2], "DELE")]
        if bk[0] != l and prev:                  # someone else's door the loser redirected: theirs again
            _p, g2, b2, ck2 = prev[-1]
            put(ck2, g2, bk, [list(x) for x in b2])
            out_heads.setdefault(ck2, heads.get(ck2))
            fixed_doors += 1
            continue
        dodt = next((d for s_, d in blk if s_ == "DODT" and len(d) >= 24), None)
        if dodt is None or not good[dn]:
            unfixable.append((dn, ck))
            continue
        x, y, z = struct.unpack_from("<3f", dodt)
        if any((struct.unpack_from("<3f", g)[0] - x) ** 2 + (struct.unpack_from("<3f", g)[1] - y) ** 2
               + (struct.unpack_from("<3f", g)[2] - z) ** 2 < 256 ** 2 for g, _c in good[dn]):
            continue                             # it already arrives where the winner's doors do
        same = [g for g, c in good[dn] if c == ck]
        target = (same or [g for g, _c in good[dn]])[0]
        nb = [[s_, target[:24] if s_ == "DODT" else d] for s_, d in blk]
        put(ck, grp, bk, nb)
        out_heads.setdefault(ck, heads.get(ck))
        fixed_doors += 1
    if fixed_doors:
        print(f"  {fixed_doors} door(s) into those places now arrive in the winning layout")
    for dn, ck in unfixable:
        print(f"  WARNING: a door of {cells[dn][2]} into '{cells[dn][0]}' (from {ck}) has no arrival spot of the "
              f"winning layout to use; check it in the game")
    # ---- every other door into an interior: one that leads to a place no plugin has, or arrives where
    # nothing stands any more (the place was rebuilt and the door still uses the old layout's spot)
    # what mwstranded moves or hides (--cell-conflicts) isn't part of the place: a door arriving among it
    # arrives in the void of the old layout
    cut_off = defaultdict(set)
    try:
        with open(os.path.join(os.path.abspath(args.data_dir), "MWSE", "config", STRANDED_DATA),
                  encoding="utf-8") as f:
            for cl, c in (json.load(f).get("cells") or {}).items():
                for o in c.get("objects", ()):
                    cut_off[cl.lower()].add(tuple(int(round(v)) for v in o["pos"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    stand, gone_n = defaultdict(list), Counter()
    for (c, _bk), (_pl, x, y, z, dele) in ipos.items():
        if dele:
            gone_n[c] += 1
        elif (int(round(x)), int(round(y)), int(round(z))) in cut_off.get(c, ()):
            gone_n[c] += 1
        else:
            stand[c].append((x, y, z))
    arrivals = defaultdict(list)
    missing_dest = defaultdict(set)
    for bk, hist in door_hist.items():
        pl, grp, blk, ck = hist[-1]
        if has(blk, "DELE"):
            continue
        dn_raw = next((cstr(d) for s_, d in blk if s_ == "DNAM"), None)
        if not dn_raw:
            continue
        dn = dn_raw.lower()
        if dn in cells:
            continue                             # handled above
        if dn not in interiors:
            missing_dest[dn_raw].add(name_of.get(pl, pl))
            continue
        dodt = next((d for s_, d in blk if s_ == "DODT" and len(d) >= 24), None)
        if dodt is not None and ck != ("i", dn):
            arrivals[dn].append((bk, dodt, ck, pl, grp, blk))
    for dn_raw, by in sorted(missing_dest.items()):
        print(f"  WARNING: door(s) of {', '.join(sorted(by))} lead to '{dn_raw}', a place no active plugin has")
    VOID = 600.0
    door_fixed, door_removed = [], []
    for dn, arr in arrivals.items():
        pts = stand.get(dn, [])
        if (gone_n[dn] < 10 and dn not in cut_off) or len(pts) < 20:
            continue                             # not rebuilt: the arrival spots are the original ones
        grid = defaultdict(list)
        for x, y, z in pts:
            grid[(int(x // VOID), int(y // VOID))].append((x, y, z))

        def in_void(d):
            x, y, z = struct.unpack_from("<3f", d)
            gx, gy = int(x // VOID), int(y // VOID)
            return not any((a - x) ** 2 + (b - y) ** 2 + (c - z) ** 2 < VOID ** 2
                           for dx in (-1, 0, 1) for dy in (-1, 0, 1) for a, b, c in grid.get((gx + dx, gy + dy), ()))
        okay = [(d, ck) for _bk, d, ck, *_r in arr if not in_void(d)]
        for bk, d, ck, pl, grp, blk in arr:
            if not in_void(d):
                continue
            if not okay:
                # nowhere safe to send it: remove the door, so nobody walks through it into the void
                nm = next((d for s_, d in blk if s_ == "NAME"), b"")
                put(ck, grp, bk, [["FRMR", b""], ["NAME", nm], ["DELE", struct.pack("<I", 0)]])
                out_heads.setdefault(ck, heads.get(ck))
                door_removed.append(f"{name_of.get(pl, pl)}: door into '{dn}' (from {ck})")
                continue
            same = [g for g, c in okay if c == ck]
            target = (same or [g for g, _c in okay])[0]
            base = out[ck][bk][1] if bk in out.get(ck, {}) else [list(x) for x in blk]
            put(ck, grp, bk, [[s_, target[:24] if s_ == "DODT" else dd] for s_, dd in base])
            out_heads.setdefault(ck, heads.get(ck))
            door_fixed.append(f"{name_of.get(pl, pl)}: door into '{dn}'")
    if door_fixed:
        print(f"  Doors: {len(door_fixed)} arrived in the void of a rebuilt place; now they arrive where another "
              f"door into it does:")
        for t in door_fixed[:15]:
            print(f"      {t}")
        if len(door_fixed) > 15:
            print(f"      ... and {len(door_fixed) - 15} more")
    if door_removed:
        print(f"  Doors: {len(door_removed)} arrived in the void of a rebuilt place with no other way in to send "
              f"them to; removed (in the Cell Fixes patch only):")
        for t in door_removed:
            print(f"      {t}")

    # ---- objects that came with something another mod moved: a mod puts its own objects by an original
    # piece (logs in a fireplace, a candle on a shelf), and another mod it doesn't know about moves that
    # piece across the room. Its objects stay where the piece used to be; they're moved after it.
    closure_all = {}

    def knows(a, b):
        """Plugin a has b among its masters (directly or not)."""
        if a not in closure_all:
            seen, stack = set(), list(masters_of.get(a, []))
            while stack:
                x = stack.pop()
                if x not in seen:
                    seen.add(x)
                    stack += masters_of.get(x, [])
            closure_all[a] = seen
        return b in closure_all[a]
    FOLLOW_MOVED, FOLLOW_NEAR = 128.0, 128.0           # "moved": over this far; "by it": this close (across)
    anchors = defaultdict(list)                        # interior -> [(original x, y, z, rz, final x, y, z, mover)]
    for (c, bk), (pl, x, y, z, dele) in ipos.items():
        if dele or c in cells or pl == bk[0]:
            continue
        o = iorig.get((c, bk))
        if not o or o[4] in item_ids or o[4] in actor_ids:
            continue
        if math.hypot(x - o[0], y - o[1]) < FOLLOW_MOVED:
            continue
        anchors[c].append((o, (x, y, z), pl, bk))
    follow = defaultdict(list)                         # interior -> [(ref key, plugin, offset x/y/z, anchor)]
    for (c, bk), (pl, x, y, z, dele) in ipos.items():
        if dele or c not in anchors or pl != bk[0]:
            continue                                   # only a mod's own objects, where it put them
        o_self = iorig.get((c, bk))
        if not o_self or o_self[4] in actor_ids:
            continue
        best = None
        for o, fin, mover, abk in anchors[c]:
            if pl in (mover, abk[0]) or knows(pl, mover):
                continue                               # made with the move in view
            dn = math.hypot(x - o[0], y - o[1])
            if dn <= FOLLOW_NEAR and abs(z - o[2]) <= 512 and math.hypot(x - fin[0], y - fin[1]) > FOLLOW_NEAR:
                if best is None or dn < best[0]:
                    best = (dn, o, fin, mover)
        if best:
            follow[c].append((bk, pl, best))
    followed = Counter()
    if follow:
        by_plugin = defaultdict(set)
        for c, items in follow.items():
            for bk, pl, _b in items:
                by_plugin[pl].add(c)
        want = {(c, bk): (pl, b) for c, items in follow.items() for bk, pl, b in items}
        for pi, (n, p) in enumerate(order):
            low = n.lower()
            if low not in by_plugin:
                continue
            try:
                data = open(p, "rb").read()
            except OSError:
                continue
            ms = masters_of.get(low, [])
            for c in by_plugin[low]:
                for rpi, off in int_recs.get(c, ()):
                    if rpi != pi:
                        continue
                    size = struct.unpack_from("<I", data, off + 4)[0]
                    head, pers, _n0, temp = split_cell(data[off + 16:off + 16 + size], off)
                    for grp, blks in (("p", pers), ("t", temp)):
                        for blk in blks:
                            bk = block_key(blk, ms, low)
                            hit = want.get((c, bk))
                            if not hit or hit[0] != low:
                                continue
                            _pl, (_dn, o, fin, mover) = hit
                            base = out[("i", c)][bk][1] if bk in out.get(("i", c), {}) else blk
                            dd = next((d for s_, d in base if s_ == "DATA" and len(d) >= 24), None)
                            if dd is None:
                                continue
                            x, y, z, r1, r2, r3 = struct.unpack_from("<6f", dd)
                            nd = struct.pack("<6f", x + fin[0] - o[0], y + fin[1] - o[1], z + fin[2] - o[2],
                                             r1, r2, r3) + dd[24:]
                            put(("i", c), grp, bk, [[s_, nd if s_ == "DATA" else d] for s_, d in base])
                            out_heads.setdefault(("i", c), heads.get(("i", c)))
                            followed[(name_of.get(low, low), name_of.get(mover, mover), c)] += 1
    if followed:
        print(f"  Moved pieces: {sum(followed.values())} object(s) put back with the piece they were placed by, "
              f"which another mod moved:")
        for (who, mover, c), n in sorted(followed.items(), key=lambda kv: -kv[1])[:15]:
            print(f"      - {n} of {who}'s in '{c}' (moved by {mover})")
        if len(followed) > 15:
            print(f"      ... and {len(followed) - 15} more (all listed in mwmanager_cell_fixes.txt)")
        try:
            with open(output_file("mwmanager_cell_fixes.txt"), "w", encoding="utf-8") as lf:
                lf.write("; objects the Cell Fixes patch moves with the piece they were placed by, which another mod "
                         "moved (rewritten by each --fix)\n")
                for (who, mover, c), n in sorted(followed.items(), key=lambda kv: (kv[0][2], -kv[1])):
                    lf.write(f"{c}\t{n} of {who}'s\tmoved by {mover}\n")
        except OSError:
            pass

    # ---- reshaped land: where two plugins change the same land's heights, the last one's land is used, and
    # objects put down on the land another plugin saw float or sink. They move up or down with the land.
    contested = {g for g, v in land_vers.items() if len({d for _i, d, _o in v}) >= 2}
    if contested:
        print(f"  Reading the {len(contested)} exterior cell(s) whose land two or more plugins reshape...",
              file=sys.stderr)
        names = [n.lower() for n, _p in order]
        closure = {}

        def deps(n):
            if n not in closure:
                seen, stack = set(), list(masters_of.get(n, []))
                while stack:
                    x = stack.pop()
                    if x not in seen:
                        seen.add(x)
                        stack += masters_of.get(x, [])
                closure[n] = seen | {n}
            return closure[n]
        need = defaultdict(list)
        for g in contested:
            for pi, _d, off in land_vers[g]:
                need[pi].append(("L", g, off))
            for pi, off in ext_recs.get(g, ()):
                need[pi].append(("C", g, off))
        land_h = {}                              # (plugin index, grid) -> heights
        refs = {}                                # ref key -> (plugin, group, block, cell key): latest version
        for pi in sorted(need):
            try:
                data = open(order[pi][1], "rb").read()
            except OSError:
                continue
            ms = masters_of.get(names[pi], [])
            for kind, g, off in need[pi]:
                size = struct.unpack_from("<I", data, off + 4)[0]
                body = data[off + 16:off + 16 + size]
                if kind == "L":
                    land_h[(pi, g)] = land_heights(first_sub(body, b"VHGT"))
                    continue
                head, pers, _n0, temp = split_cell(body, off)
                for grp, blks in (("p", pers), ("t", temp)):
                    for blk in blks:
                        bk = block_key(blk, ms, names[pi])
                        if bk is not None:
                            refs[bk] = (names[pi], grp, blk, ("e", g[0], g[1]))
        land_digest = {g: {pi: d for pi, d, _o in v} for g, v in land_vers.items() if g in contested}

        def seen_land(pl, g):
            """The land version plugin pl was made against: its own or its masters' latest."""
            dep = deps(pl)
            vs = land_vers[g]
            return next((pi for pi, _d, _o in reversed(vs) if names[pi] in dep), vs[0][0])

        def shift(pl, x, y, z):
            g = (int(math.floor(x / 8192)), int(math.floor(y / 8192)))
            if g not in contested:
                return None
            last = land_vers[g][-1][0]
            saw = seen_land(pl, g)
            if land_digest[g][saw] == land_digest[g][last]:
                return None
            h_old = height_at(land_h.get((saw, g)), g, x, y)
            h_new = height_at(land_h.get((last, g)), g, x, y)
            if h_old is None or h_new is None:
                return None
            dz = h_new - h_old
            if abs(dz) < 16 or abs(z - h_old) > 400:
                return None                      # the land barely moved, or it isn't standing on the land
            return dz
        moved_by = Counter()
        for bk, (pl, grp, blk, ck) in refs.items():
            if has(blk, "DELE"):
                continue
            base = out[ck][bk][1] if bk in out.get(ck, {}) else blk
            dd = next((d for s_, d in base if s_ == "DATA" and len(d) >= 24), None)
            if dd is None:
                continue
            x, y, z, r1, r2, r3 = struct.unpack_from("<6f", dd)
            dz = shift(pl, x, y, z)
            if dz is None:
                continue
            nd = struct.pack("<6f", x, y, z + dz, r1, r2, r3) + dd[24:]
            put(ck, grp, bk, [[s_, nd if s_ == "DATA" else d] for s_, d in base])
            out_heads.setdefault(ck, heads.get(ck))
            g = (int(math.floor(x / 8192)), int(math.floor(y / 8192)))
            moved_by[(name_of.get(pl, pl), order[land_vers[g][-1][0]][0])] += 1
        # doors that arrive outside, on land that moved under them
        arrive_fixed = 0
        for bk, hist in door_hist.items():
            pl, grp, blk, ck = hist[-1]
            if has(blk, "DELE") or any(s_ == "DNAM" for s_, _d in blk):
                continue
            base = out[ck][bk][1] if bk in out.get(ck, {}) else blk
            dodt = next((d for s_, d in base if s_ == "DODT" and len(d) >= 24), None)
            if dodt is None:
                continue
            x, y, z, r1, r2, r3 = struct.unpack_from("<6f", dodt)
            dz = shift(pl, x, y, z)
            if dz is None:
                continue
            nd = struct.pack("<6f", x, y, z + dz, r1, r2, r3)
            put(ck, grp, bk, [[s_, nd if s_ == "DODT" else d] for s_, d in base])
            out_heads.setdefault(ck, heads.get(ck))
            arrive_fixed += 1
        total = sum(moved_by.values())
        print(f"  Reshaped land: {len(contested)} exterior cell(s) whose land two or more plugins change; "
              f"{total} object(s) moved up or down onto the land that's used"
              + (f", {arrive_fixed} door arrival(s) too" if arrive_fixed else ""))
        for (who, land), n in moved_by.most_common(12):
            print(f"      {n:6} of {who}'s, onto {land}'s land")
        if len(moved_by) > 12:
            print(f"      ... and {len(moved_by) - 12} more")

    # people, creatures, containers (and what the loser left in) standing where the winner built nothing - in
    # the void of a layout that isn't there any more - are moved to the place's arrival spot, a little apart
    def data_pos(blk):
        d = next((d for s_, d in blk if s_ == "DATA" and len(d) >= 24), None)
        return struct.unpack_from("<6f", d) if d else None
    for cl, (cell, w, l) in sorted(cells.items()):
        key = ("i", cl)
        spots = [g for g, _c in good.get(cl, [])]
        if not spots:
            continue
        sx, sy, sz, _r1, _r2, sr = struct.unpack_from("<6f", spots[0])
        layout, movers = [], []
        for (c, bk), vers in xver.items():
            if c != cl:
                continue
            if bk in out[key]:
                grp, blk = out[key][bk]
                prov = "patch"
            else:
                prov, grp, blk = vers[-1]
            if has(blk, "DELE"):
                continue
            p6 = data_pos(blk)
            if p6 is None:
                continue
            oid = name_in(blk).lower()
            if oid in actor_ids or oid in cont_ids or bk in kept_bk[cl]:
                movers.append((bk, grp, blk, p6, oid))
            elif prov != l:                      # what still stands there: the winner's, and the original's it kept
                layout.append(p6[:3])
        if len(layout) < 10:
            continue                             # too little of the winner's own to judge what's in the void
        grid = defaultdict(list)
        for x, y, z in layout:
            grid[(int(x // 300), int(y // 300))].append((x, y, z))

        def near(x, y, z):
            gx, gy = int(x // 300), int(y // 300)
            return any((a - x) ** 2 + (b - y) ** 2 + (c - z) ** 2 < 300 ** 2
                       for dx in (-1, 0, 1) for dy in (-1, 0, 1) for a, b, c in grid.get((gx + dx, gy + dy), ()))
        moved = []
        for bk, grp, blk, p6, oid in movers:
            if near(*p6[:3]):
                continue
            i = len(moved)
            ang = i * 0.9
            r = 48 + 24 * (i // 7)
            nd = struct.pack("<6f", sx + r * math.cos(ang), sy + r * math.sin(ang), sz + 8, p6[3], p6[4], p6[5])
            nb = [[s_, nd if s_ == "DATA" else d] for s_, d in blk]
            put(key, grp, bk, nb)
            moved.append(name_in(blk))
        if moved:
            print(f"  '{cell}': {len(moved)} left in the void of the old layout, moved to the entrance: "
                  f"{', '.join(moved[:10])}{' ...' if len(moved) > 10 else ''}")

    if not any(out.values()) and not pgrds:
        print("  Nothing to change.")
        drop_patch()
        return 0
    if args.dry_run:
        print(f"  Dry run: {CELL_WINNER_PATCH} not written.")
        return 0
    lookup = index_dir(data_dir)
    needed = used | {w for _c, w, _l in cells.values()} | losers
    masters = [os.path.basename(lookup[m]) for m in sorted(needed, key=lambda m: pos.get(m, -1)) if m in lookup]
    if len(masters) > 250:
        print(f"  WARNING: the fixes touch objects of {len(masters)} plugins, more than a plugin can name as "
              f"masters (255); the rest are left out")
        masters = masters[:250]
    midx = {m.lower(): i + 1 for i, m in enumerate(masters)}
    recs, count = b"", 0
    for key, refs in out.items():
        if not refs:
            continue
        head = out_heads.get(key) or heads.get(key)
        if head is None:
            continue
        body = b"".join(pack_sub(s_, d) for s_, d in head)
        for grp in ("p", "t"):
            blocks = []
            for (owner, idx), (g, blk) in refs.items():
                if g != grp:
                    continue
                if owner not in midx:
                    continue
                blk[0] = ["FRMR", struct.pack("<I", (midx[owner] << 24) | idx)]
                blocks.append(blk)
            if grp == "t" and blocks:
                body += pack_sub("NAM0", struct.pack("<I", len(blocks)))
            for blk in blocks:
                body += b"".join(pack_sub(s_, d) for s_, d in blk)
        recs += b"CELL" + struct.pack("<III", len(body), 0, 0) + body
        count += 1
    for key, head in out_heads.items():          # head only: the cell's settings without the loser
        if not out.get(key) and head is not None:
            body = b"".join(pack_sub(s_, d) for s_, d in head)
            recs += b"CELL" + struct.pack("<III", len(body), 0, 0) + body
            count += 1
    for r in pgrds:
        recs += r
        count += 1
    desc = "Rebuilt interiors: the chosen mod's version wins (rebuilt by mwmanager --fix)"
    hedr = (struct.pack("<fI", 1.3, 0) + b"mwmanager".ljust(32, b"\0")
            + desc.encode(ENC, MW_ERRORS)[:255].ljust(256, b"\0") + struct.pack("<I", count))
    subs = [pack_sub("HEDR", hedr)]
    for mname in masters:
        subs += [pack_sub("MAST", mname.encode(ENC, MW_ERRORS) + b"\0"),
                 pack_sub("DATA", struct.pack("<Q", os.path.getsize(os.path.join(data_dir, mname))))]
    hb = b"".join(subs)
    if os.path.exists(out_path):
        shutil.copy2(out_path, backup_path(out_path))
    write_plugin_bytes(out_path, b"TES3" + struct.pack("<III", len(hb), 0, 0) + hb + recs)
    when = max(os.path.getmtime(os.path.join(data_dir, m)) for m in masters) + 60
    os.utime(out_path, (when, when))
    cur = read_load_order(args) or []
    if CELL_WINNER_PATCH.lower() not in {n.lower() for n in cur}:
        tail = {PLACE_PATCH_NAME.lower(), MULTI_PATCH_NAME.lower()}
        at = next((i for i, n in enumerate(cur) if n.lower() in tail), len(cur))
        set_game_files(args.ini or os.path.join(game_dir, "Morrowind.ini"), cur[:at] + [CELL_WINNER_PATCH] + cur[at:],
                       "--fix (cell winners)")
    with MT.open_log(os.path.join(game_dir, args.fix_log), "a", encoding="utf-8") as lf:
        lf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {CELL_WINNER_PATCH}: "
                 + "; ".join(f"{c}: {name_of[w]} over {name_of[l]}" for c, w, l in cells.values()) + "\n")
    print(f"  Wrote {CELL_WINNER_PATCH} ({count} record(s)); it loads near the end, before the Multipatch.")
    return 0


def fix_doubled(args, plugins):
    """--fix: turn off plugins that another active plugin already duplicates (the same objects placed
    twice in the same spots: two versions of one mod, or one containing the other). Which one stays:
    never a plugin another active plugin needs as a master goes; else the bigger one stays when it
    contains the other; else the one loading later (its version already wins). Pairs you keep together
    in an [Exact] rule are left alone. Returns the names turned off (or that would be, with --dry-run)."""
    game_dir = os.path.dirname(os.path.abspath(args.data_dir))
    print("\n" + "=" * 70 + "\nDOUBLED PLUGINS (the same objects placed twice)\n" + "=" * 70)
    print("  Looking for plugins that place the same objects twice...", file=sys.stderr)
    ctx = run_cell_conflicts(args, plugins, collect=True)
    pairs, name_of, total, pos, masters = (ctx["pairs"], ctx["name_of"], ctx["total_new"], ctx["pos"],
                                           ctx["masters"])
    exact = [{e.lower() for e in ents} for k, ents, _s in parse_rules(manager_file(RULES_FILE))[0]
             if k == "exact"]
    needed = {m for ms in masters.values() for m in ms}          # masters of something active
    doubled = [(a, b) for (a, b), c in pairs.items() if c["doubled"]]
    kept_exact = [(a, b) for a, b in doubled if any(a in g and b in g for g in exact)]
    doubled = [p for p in doubled if p not in kept_exact]
    if not doubled:
        print("  None." + (f" ({len(kept_exact)} doubled pair(s) left alone: you keep them together in an [Exact] rule)"
                           if kept_exact else ""))
        return []
    parent = {}

    def root(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in doubled:
        parent[root(a)] = root(b)
    groups = defaultdict(list)
    for x in list(parent):
        groups[root(x)].append(x)
    dpairs = {frozenset(p) for p in doubled}
    off = []
    for members in groups.values():
        members.sort(key=lambda n: pos[n])
        keep = max(members, key=lambda n: (n in needed, total[n], pos[n]))
        why_keep = ("other active plugins need it as a master" if keep in needed else
                    "it has the most objects (it contains the other's)" if total[keep] > 1.1 * min(total[m] for m in members if m != keep)
                    else "it loads later, so its version already wins")
        for m in members:
            if m == keep:
                continue
            if m in needed:
                print(f"  {name_of[m]}: doubled with {name_of[keep]}, but other active plugins need it as a master; "
                      f"left on")
                continue
            hit = relied_on(plugins, m, [x for x, _w in off])
            if hit:
                print(f"  {name_of[m]}: doubled with {name_of[keep]}, but {relied_on_text(hit, name_of)}; left on")
                continue
            if frozenset((m, keep)) not in dpairs and not any(frozenset((m, k)) in dpairs for k in members if k != m):
                continue
            c = pairs.get((m, keep)) or pairs.get((keep, m)) or {}
            dups = c.get("dups", 0)
            print(f"  turn off {name_of[m]}: {dups or 'most'} of its {total[m]} new objects are already placed, in the "
                  f"same spots, by {name_of[keep]} (kept: {why_keep})")
            off.append((name_of[m], f"doubled with {name_of[keep]}"))
    for a, b in kept_exact:
        print(f"  left alone: {name_of[a]} + {name_of[b]} (you keep them together in an [Exact] rule)")
    if not off:
        return []
    if args.dry_run:
        print("  Dry run: nothing turned off.")
        return [n for n, _w in off]
    if not args.yes and (not sys.stdin.isatty() or input(f"\nTurn off {len(off)} doubled plugin(s)? [y/N] ")
                         .strip().lower() not in ("y", "yes")):
        print("  Nothing turned off.")
        return []
    ini = args.ini or os.path.join(game_dir, "Morrowind.ini")
    gone = {n.lower() for n, _w in off}
    cur = read_load_order(args) or []
    backup = set_game_files(ini, [n for n in cur if n.lower() not in gone], "--fix (doubled plugins)")
    moved = move_plugins_to_disabled(args.data_dir, [n for n, _w in off])
    rec = read_disabled(game_dir)
    for n, w in off:
        rec[n] = w
    write_disabled(game_dir, rec)
    print(f"  Turned off {len(off)} plugin(s) (old list saved as {backup}); moved {len(moved)} to "
          f"MWManager\\disabled_plugins and listed in {DISABLED_RECORD}.")
    return [n for n, _w in off]


def release_lost_winners(args):
    """Each plugin in mwmanager_disabled.txt was turned off in favour of another (the "winner": doubled with
    it, or conflicting with it). When that winner is gone - not in Data Files, or no longer active (in the
    load order or inside an active merge) - the plugin that lost is turned back on. Runs at the start of
    every run that isn't a dry run."""
    if getattr(args, "plugins", None) or getattr(args, "openmw_cfg", None) or args.dry_run:
        return 0
    data_dir = os.path.abspath(args.data_dir)
    game_dir = os.path.dirname(data_dir)
    rec = read_disabled(game_dir)
    if not rec:
        return 0
    lookup = index_dir(data_dir)
    disabled_lookup = {name.casefold(): find_disabled_plugin(data_dir, name)
                       for name in rec if find_disabled_plugin(data_dir, name)}
    active = {n.lower() for n in (read_load_order(args) or [])} | merged_away(game_dir, data_dir)
    lost = []
    for loser, why in rec.items():
        m = WINNER_RE.search(why)
        if not m:
            continue
        winner = m.group("w").strip()
        if winner.lower() not in lookup:
            lost.append((loser, winner, "is no longer in Data Files"))
        elif winner.lower() not in active:
            lost.append((loser, winner, "is no longer in the load order"))
    lost = [x for x in lost if x[0].lower() in lookup or x[0].lower() in disabled_lookup]
    if not lost:
        return 0
    print("\nPlugins turned off in favour of another that is gone:")
    for loser, winner, how in lost:
        print(f"  {loser}: turned off for {winner}, which {how}; turning {loser} back on")
    a2 = copy.copy(args)
    a2.reenable = [l for l, _w, _h in lost]
    a2.dry_run = False
    run_reenable(a2)
    return len(lost)


def run_reenable(args):
    """--reenable [NAME...]: turn plugins --fix turned off (doubled) back on."""
    data_dir = os.path.abspath(args.data_dir)
    game_dir = os.path.dirname(data_dir)
    rec = read_disabled(game_dir)
    want = {x.lower() for x in args.reenable} if args.reenable else {n.lower() for n in rec}
    todo = [n for n in rec if n.lower() in want]
    if not todo:
        print("--reenable: none of those were turned off by --fix. Turned off: " + (", ".join(rec) or "none"))
        return 1
    ini = args.ini or os.path.join(game_dir, "Morrowind.ini")
    if not args.dry_run:
        restore_disabled_plugins(data_dir, todo)
    lookup = index_dir(data_dir)
    cur = read_load_order(args) or []
    paths = [lookup[n.lower()] for n in cur if n.lower() in lookup] + \
            [lookup[n.lower()] for n in todo if n.lower() in lookup and n.lower() not in {c.lower() for c in cur}]
    if args.dry_run:
        print("--reenable would turn back on: " + ", ".join(todo))
        return 0
    backup = set_game_files(ini, [os.path.basename(p) for p in sort_vanilla(paths)], "reenable")
    for n in todo:
        del rec[n]
    write_disabled(game_dir, rec)
    print(f"  Turned back on: {', '.join(todo)} (old list saved as {backup}).")
    return 0


def compute_order(plugins, rule_sets, current_order):
    """Stable topological sort. Returns (new order, notes)."""
    names = [n for n, _p in plugins]
    low = {n.lower(): n for n in names}
    pos = {n: i for i, n in enumerate(current_order)}
    esp = {n: n.lower().endswith(FIXABLE_EXT) for n in names}
    succ = defaultdict(set)
    edge_src = {}                                # (a, b) -> what put a before b ("master" or a rule source)
    notes = {"missing_master": [], "cycles": [], "esm_after_esp": 0, "applied": 0, "rules": 0,
             "steps": {}}                        # (rule source, entry index) -> {"applied", "skipped"}

    def path_between(a, b):
        """The chain of edges by which b already follows a: [(x, y, source)], or []."""
        parent, stack = {a: None}, [a]
        while stack:
            x = stack.pop()
            if x == b:
                out = []
                while parent[x] is not None:
                    p = parent[x]
                    out.append((p, x, edge_src.get((p, x), "master")))
                    x = p
                return out[::-1]
            for y in succ[x]:
                if y not in parent:
                    parent[y] = x
                    stack.append(y)
        return []

    def reaches(a, b):
        """Is b reachable from a?"""
        stack, seen = [a], {a}
        while stack:
            x = stack.pop()
            if x == b:
                return True
            for y in succ[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return False

    # hard: masters before the plugins using them
    for n, path in plugins:
        try:
            for m in read_header_masters(path):
                ml = m.lower()
                if ml not in low and ml.endswith(".omwaddon"):     # an OpenMW name for a plugin here as
                    for ext in (".esp", ".esm"):                    # .esp/.esm (--fix renames it in the header)
                        if ml[:-len(".omwaddon")] + ext in low:
                            ml = ml[:-len(".omwaddon")] + ext
                            break
                if ml in low:
                    succ[low[ml]].add(n)
                    edge_src[(low[ml], n)] = "master"
                else:
                    notes["missing_master"].append((n, m))
        except (OSError, FormatError):
            pass

    def expand(entry):
        rx = name_matcher(entry)
        if rx is None:
            return [low[entry.lower()]] if entry.lower() in low else []
        return [n for n in all_names if rx.match(n)]

    all_names = names
    # [Exact]: the listed plugins load one right after the other, in exactly that order. Each group is
    # sorted as a single block; names are taken literally (no wildcards).
    notes["exact"] = []                          # (source, members, problem or None)
    in_group, groups_ok = {}, []

    def reach_set(starts, edges):
        seen, stack = set(starts), list(starts)
        while stack:
            for y in edges[stack.pop()]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return seen

    pred = defaultdict(set)
    for a in list(succ):
        for b in succ[a]:
            pred[b].add(a)
    for rules in rule_sets:
        for kind, entries, src in rules:
            if kind != "exact":
                continue
            members = [low[e.lower()] for e in entries if e.lower() in low]
            missing = [e for e in entries if e.lower() not in low]
            members = list(dict.fromkeys(members))
            problem = None
            if len(members) < 2:
                problem = "fewer than two of its plugins are active"
            elif any(m in in_group for m in members):
                problem = f"{next(m for m in members if m in in_group)} is already in another [Exact] group"
            elif len({esp[m] for m in members}) > 1:
                problem = "it mixes .esm and .esp files, which can't load side by side"
            else:
                idx = {m: i for i, m in enumerate(members)}
                for m in members:
                    for s in succ[m]:
                        if s in idx and idx[s] < idx[m]:
                            problem = f"{m} is a master of {s}, so it can't load after it"
                if problem is None:
                    gset = set(members)
                    down = reach_set([s for m in members for s in succ[m] if s not in gset], succ)
                    up = reach_set([p for m in members for p in pred[m] if p not in gset], pred)
                    between = (down & up) - gset
                    if between:
                        problem = (f"{sorted(between)[0]} must load between two of them (masters)")
            notes["exact"].append((src, members, problem, missing))
            if problem is None:
                for m in members:
                    in_group[m] = members[0]
                groups_ok.append(members)

    if groups_ok:                                # contract each group to its first member
        rep_of = lambda n: in_group.get(n, n)
        new_succ = defaultdict(set)
        for a in list(succ):
            for b in succ[a]:
                ra, rb = rep_of(a), rep_of(b)
                if ra != rb:
                    new_succ[ra].add(rb)
                    edge_src.setdefault((ra, rb), edge_src.get((a, b), "master"))
        succ.clear()
        succ.update(new_succ)
        members_of = {g[0]: g for g in groups_ok}
        for g in groups_ok:
            pos[g[0]] = min(pos.get(m, 0) for m in g)
        all_names = names
        names = [n for n in names if rep_of(n) == n]
    else:
        rep_of = lambda n: n
        members_of = {}
        all_names = names

    near_start, near_end = {}, {}
    for set_no, rules in enumerate(rule_sets):   # highest priority first; the first is your rules file
        for kind, entries, src in rules:
            notes["rules"] += 1
            if kind == "exact":
                continue
            egroups = [(ei, list(dict.fromkeys(rep_of(n) for n in g)))
                       for ei, g in enumerate(expand(e) for e in entries) if g]
            groups = [g for _ei, g in egroups]
            if kind == "nearstart":
                for g in groups:
                    for n in g:
                        near_start.setdefault(n, len(near_start))
            elif kind == "nearend":
                for g in groups:
                    for n in g:
                        near_end.setdefault(n, len(near_end))
            # In your rules file (the first rule set) a [NearStart] / [NearEnd] block is also an [Order] rule
            # for its own plugins, in the order it places them: [NearStart] as listed, [NearEnd] reversed (its
            # first plugin loads last). So a plugin listed there needs no second [Order] entry; contradictions
            # are skipped and reported like any [Order] step, and the plugin still goes to the start / end
            # band. mlox's own files keep mlox's meaning (a list of priorities, not an order).
            if kind in ("nearstart", "nearend") and set_no != 0:
                continue
            pairs = zip(egroups, egroups[1:])
            if kind == "nearend":
                pairs = ((b, a) for a, b in pairs)
            for (ea, a_grp), (eb, b_grp) in pairs:
                step = notes["steps"].setdefault((src, max(ea, eb)), {"applied": 0, "skipped": [], "entries": entries,
                                                                      "kind": kind})
                for a in a_grp:
                    for b in b_grp:
                        if a == b:
                            continue
                        if b in succ[a]:
                            continue                            # already so from elsewhere: changes nothing
                        if esp[a] and not esp[b]:
                            notes["esm_after_esp"] += 1       # an .esp can never load before an .esm
                            step["skipped"].append((a, b, "esm"))
                            continue
                        if reaches(b, a):
                            notes["cycles"].append((a, b, src))
                            step["skipped"].append((a, b, path_between(b, a)))
                            continue
                        succ[a].add(b)
                        edge_src[(a, b)] = src
                        step["applied"] += 1
                        notes["applied"] += 1

    indeg = Counter()
    for a in list(succ):
        for b in succ[a]:
            indeg[b] += 1
    import heapq

    def key(n):
        band = 0 if n in near_start else 2 if n in near_end else 1
        # mlox: in [NearStart] the first listed loads first; in [NearEnd] the first listed loads LAST
        rank = near_start.get(n, 0) if band == 0 else -near_end.get(n, 0) if band == 2 else pos.get(n, 0)
        return (esp[n], band, rank, n.lower())

    ready = [(key(n), n) for n in names if indeg[n] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        _k, n = heapq.heappop(ready)
        order.append(n)
        for b in succ[n]:
            indeg[b] -= 1
            if indeg[b] == 0:
                heapq.heappush(ready, (key(b), b))
    if len(order) != len(names):                 # a cycle among masters themselves: keep the rest as is
        left = [n for n in current_order if n not in set(order) and rep_of(n) == n]
        order += left
        notes["cycles"].append(("(masters)", ", ".join(left[:3]), "plugin headers"))
    order = [m for n in order for m in members_of.get(n, [n])]

    def outside_groups(i):
        """An insertion point that doesn't split an [Exact] group: inside one, move to its start."""
        for g in groups_ok:
            if g[0] in order and g[-1] in order:
                s = order.index(g[0])
                if s < i < s + len(g):
                    return s
        return i

    # Hard rule: the ESM fixes patch is always the first .esp (it only patches masters, and the
    # objects it defines early must exist before any .esp loads).
    pin = low.get(PATCH_NAME.lower())
    if pin in order:
        try:
            ok = all(m.lower().endswith(".esm") for m in read_header_masters(dict(plugins)[pin]))
        except (OSError, FormatError):
            ok = True
        if ok:
            order.remove(pin)
            first_esp = next((i for i, n in enumerate(order) if esp.get(n, n.lower().endswith(".esp"))), len(order))
            order.insert(outside_groups(first_esp), pin)
        else:
            notes.setdefault("pin_failed", True)
    # Hard rule: plugins in the conflict blacklist always lose: each loads as early as its masters allow
    # (an .esm among the .esm files, an .esp right after them / the ESM fixes patch)
    try:
        black = read_conflict_blacklist(os.path.dirname(os.path.dirname(os.path.abspath(plugins[0][1]))), create=False)
    except (IndexError, OSError):
        black = []
    if black:
        paths_ = dict(plugins)
        for n in [x for x in order if conflict_blacklisted(x, black)]:
            try:
                ms = {m.lower() for m in read_header_masters(paths_[n])}
            except (OSError, FormatError, KeyError):
                continue
            order.remove(n)
            is_esp = esp.get(n, n.lower().endswith(".esp"))
            at = 0
            for i, x in enumerate(order):
                if x.lower() in ms or (is_esp and (not esp.get(x, x.lower().endswith(".esp")) or x == pin)):
                    at = i + 1                       # after every master (and, for an .esp, every .esm)
            order.insert(outside_groups(at), n)
            notes.setdefault("always_lose", []).append(n)
    # MGE XE's sky variation plugin is required and is always immediately before MWManager's generated tail.
    # Hard rule: this tool's end patches load last, whatever the rules say: the auto-merge, the performers
    # patch (its deletions then win over every other plugin's edits of those inns), the placeholder patch,
    # then the Multipatch (which must see, and win over, every plugin). One that is in an [Exact] group
    # takes its whole group with it.
    tail = []
    # every auto-merge (the default one and any made with --merge-name), in the order they were made: each
    # was checked against a load order with it at the end
    try:
        merges = list(read_merge_record(os.path.dirname(os.path.dirname(os.path.abspath(plugins[0][1])))))
    except (IndexError, OSError):
        merges = []
    if AUTO_MERGE_NAME not in merges:
        merges.insert(0, AUTO_MERGE_NAME)
    for want in [XE_SKY_PLUGIN] + merges + [PERFORMER_PATCH, CELL_WINNER_PATCH, PLACE_PATCH_NAME, MULTI_PATCH_NAME]:
        n = low.get(want.lower())
        if n not in order or n in tail:
            continue
        grp = next((g for g in groups_ok if n in g), [n])
        tail += [m for m in grp if m in order and m not in tail]
    if tail:
        order = [n for n in order if n not in tail] + tail
    for g in groups_ok:                          # last check: every applied group is still one block
        idx = [order.index(m) for m in g if m in order]
        if idx != list(range(idx[0], idx[0] + len(idx))) if idx else False:
            notes.setdefault("exact_split", []).append(g)
    return order, notes


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


def sort_rule_sources(args, plugins, quiet=False):
    """The rule sets --sort uses, highest priority first: your rules file, the content rules, then
    mlox_user.txt and mlox_base.txt. Returns (rule_sets, {file basename: path})."""
    game_dir = os.path.dirname(os.path.abspath(args.data_dir))
    rules_path = manager_file(RULES_FILE)
    sources = [rules_path]
    if not getattr(args, "no_mlox_rules", False):
        if not ensure_mlox_rules(game_dir, dry_run=getattr(args, "dry_run", False),
                                 yes=getattr(args, "yes", False),
                                 disabled=getattr(args, "no_mlox_rules", False)):
            if not getattr(args, "dry_run", False):
                args.no_mlox_rules = True
        if not getattr(args, "no_mlox_rules", False):
            sources += [manager_file(os.path.join("mlox", "mlox_user.txt")), manager_file(os.path.join("mlox", "mlox_base.txt"))]
    rule_sets, files = [], {}
    for s in sources:
        if os.path.exists(s):
            rule_sets.append(parse_rules(s)[0])
            files[os.path.basename(s)] = s
        elif s == rules_path:
            rule_sets.append(parse_rules(s, DEFAULT_RULES)[0])
        else:
            rule_sets.append([])
    if not getattr(args, "no_content_order", False):
        if not quiet:
            print("  Reading plugins to find patches that must follow the mods they change...", file=sys.stderr)
        rule_sets.insert(1, content_rules(plugins))
    return rule_sets, files


def run_sort(args, plugins):
    """--sort: compute the load order and set file dates to match (one day apart from 2000-01-01)."""
    print("\n" + "=" * 70 + "\nLOAD ORDER\n" + "=" * 70)
    game_dir = os.path.dirname(os.path.abspath(args.data_dir))
    rules_path = manager_file(RULES_FILE)
    if not os.path.exists(rules_path) and not args.dry_run:
        with open(rules_path, "w", encoding="cp1252", newline="\r\n") as f:
            f.write(DEFAULT_RULES)
        print(f"  Created {RULES_FILE} with the default rules (edit it to add your own).")
    sources = [rules_path]
    if not args.no_mlox_rules:
        game_dir = os.path.dirname(os.path.abspath(args.data_dir))
        if not ensure_mlox_rules(game_dir, dry_run=args.dry_run, yes=args.yes,
                                 disabled=args.no_mlox_rules):
            if not args.dry_run:
                args.no_mlox_rules = True
        if not args.no_mlox_rules:
            sources += [manager_file(os.path.join("mlox", "mlox_user.txt")), manager_file(os.path.join("mlox", "mlox_base.txt"))]
    rule_sets, ignored = [], Counter()
    BAD_RULE_ENTRIES.clear()
    for src in sources:
        if os.path.exists(src):
            rules, ign = parse_rules(src)
        elif src == rules_path:                  # dry run before the file exists: use the defaults
            rules, ign = parse_rules(src, DEFAULT_RULES)
        else:
            rules, ign = [], Counter()
        if rules or ign:
            print(f"  {os.path.relpath(src, game_dir)}: {len(rules)} order rule(s)"
                  + (f", {sum(ign.values())} other rule(s) ignored" if ign else ""))
        rule_sets.append(rules)
        ignored += ign
    mine = [b for b in BAD_RULE_ENTRIES if os.path.normcase(b[0]) == os.path.normcase(rules_path)]
    for src, no, entry in BAD_RULE_ENTRIES:
        if (src, no, entry) not in mine:
            print(f"  WARN {os.path.relpath(src, game_dir)} line {no}: '{entry}' isn't a plugin (.esp/.esm); "
                  f"left out of that rule")
    if mine:
        print(f"\n  ERROR {RULES_FILE}: {len(mine)} rule entr{'y is' if len(mine) == 1 else 'ies are'} not a plugin "
              f"(every entry must be an .esp or .esm name, or a wildcard ending in * that can match one):")
        for _src, no, entry in mine:
            print(f"    line {no}: {entry}")
        print(f"  Fix {RULES_FILE} and sort again; nothing was changed.")
        return 2
    added_rec = read_added_masters(game_dir)
    if added_rec and not args.dry_run:
        # a master this tool added, and your own rule now wants the patch BEFORE that mod: take it out again
        paths0 = dict(plugins)
        for patch, mods in list(added_rec.items()):
            for mod in list(mods):
                if patch in paths0 and rule_puts_before(rule_sets[0], patch, mod):
                    try:
                        bak = remove_master(paths0[patch], mod)
                        del mods[mod]
                        print(f"  {patch}: master {mod} (added by an earlier sort) taken out again, because "
                              f"{RULES_FILE} puts {patch} first" + (f" (backup {os.path.basename(bak)})" if bak else ""))
                    except (OSError, FormatError) as e:
                        print(f"  {patch}: could not take out master {mod} ({e}); your rule can't apply")
        write_added_masters(game_dir, added_rec)
    if not args.no_content_order:
        print("  Reading plugins to find patches that must follow the mods they change...", file=sys.stderr)
        crules = content_rules(plugins)
        rule_sets.insert(1, crules)            # after your rules file, before mlox's
        print(f"  content: {len(crules)} patch-after-mod rule(s) found from what the plugins change")
    current = [n for n, _p in plugins]
    order, notes = compute_order(plugins, rule_sets, current)
    if not args.no_content_order and not args.dry_run and os.path.exists(rules_path):
        skipped = {(a.lower(), b.lower()) for a, b, s in notes["cycles"] if str(s).startswith("content:")}
        if write_context_rules(rules_path, crules, skipped):
            print(f"  Listed the {len(crules)} content rule(s) in a [Context] section at the top of {RULES_FILE}.")
    if not args.no_content_order and not getattr(args, "no_add_masters", False):
        # A patch that changes another mod's records without listing it as a master: make it a real master
        # (then the order is a hard rule and the [Context] line goes away on the next sort). .esp only.
        paths_ = dict(plugins)
        pos_ = {n.lower(): i for i, n in enumerate(order)}
        skipped = {(a.lower(), b.lower()) for a, b, s in notes["cycles"] if str(s).startswith("content:")}
        merged = {k.lower() for k in read_merge_record(os.path.dirname(os.path.abspath(args.data_dir)))}
        added = []
        for _k, (mod, patch), why in crules:
            if (mod.lower(), patch.lower()) in skipped or not patch.lower().endswith(FIXABLE_EXT) \
                    or is_our_patch(patch) or patch.lower() in merged or patch.lower() in NOT_A_MOD \
                    or pos_.get(mod.lower(), 10 ** 9) > pos_.get(patch.lower(), -1):
                continue
            if rule_puts_before(rule_sets[0], patch, mod):
                continue                         # your rules want the patch first: no master
            if args.dry_run:
                added.append((patch, mod, None, why))
                continue
            try:
                arch = backup_asset_original(os.path.abspath(args.data_dir), patch, plugin=patch, feature="added masters",
                                             track_new=False)
                bak = add_master(paths_[patch], mod, paths_[mod])
            except (OSError, FormatError, KeyError) as e:
                print(f"  {patch}: could not add master {mod} ({e})")
                continue
            if bak:
                added.append((patch, mod, bak, why))
                added_rec.setdefault(patch, {})[mod] = (time.strftime("%Y-%m-%d %H:%M:%S") + f"; original: {arch}"
                                                        + f"; why: {why}")
        if added:
            print(f"\n  {len(added)} patch(es) now list the mod they change as a master"
                  + (" (dry run: not written)" if args.dry_run else "") + ":")
            for patch, mod, bak, why in added[:15]:
                print(f"    {patch}  +master  {mod}   ({why})")
            if len(added) > 15:
                print(f"    ... and {len(added) - 15} more")
            if not args.dry_run:
                write_added_masters(os.path.dirname(os.path.abspath(args.data_dir)), added_rec)
                with MT.open_log(args.fix_log, "a", encoding="utf-8") as lf:
                    for patch, mod, bak, why in added:
                        lf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {patch}: added master {mod} ({why}); "
                                 f"backup {os.path.basename(bak)}\n")

    for n, m in notes["missing_master"]:
        print(f"  WARNING: {n} needs master '{m}', which is not active")
    for src, members, problem, missing in notes.get("exact", []):
        if problem:
            print(f"  [Exact] at {src} NOT applied: {problem}")
        else:
            print(f"  [Exact] at {src}: {len(members)} plugins kept together in that order")
    for g in notes.get("exact_split", []):
        print(f"  WARNING: [Exact] group starting {g[0]} came apart; please report this (the rest of the sort is fine)")
        if missing:
            print(f"      not active (left out of the group): {', '.join(missing)}")
    if notes["cycles"]:
        print(f"  {len(notes['cycles'])} rule(s) skipped because they contradict an earlier rule or a master:")
        for a, b, src in notes["cycles"][:10]:
            print(f"    {a}  before  {b}   ({src})")
        if len(notes["cycles"]) > 10:
            print(f"    ... and {len(notes['cycles']) - 10} more")
    if notes.get("always_lose"):
        print(f"  {len(notes['always_lose'])} plugin(s) from {CONFLICT_BLACKLIST_FILE} load as early as their masters "
              f"allow (they always lose): " + ", ".join(notes["always_lose"]))
    if notes["esm_after_esp"]:
        print(f"  {notes['esm_after_esp']} rule(s) skipped: they put an .esp before an .esm, which the game never does")

    paths = dict(plugins)
    want = {n: DATE_BASE + 86400 * i for i, n in enumerate(order)}
    changes = [(n, paths[n]) for n in order if int(os.path.getmtime(paths[n])) != want[n]]
    moved = [(current.index(n), i, n) for i, n in enumerate(order) if current.index(n) != i]
    print(f"\n  {len(order)} active plugins; {len(moved)} change position, {len(changes)} need a new date")
    shown = [(a, b, n) for a, b, n in moved if abs(a - b) > 1 or len(moved) <= 40]
    for a, b, n in shown[:60]:
        print(f"    {a + 1:>4} -> {b + 1:<4} {n}")
    if len(shown) > 60:
        print(f"    ... and {len(shown) - 60} more")
    if not changes:
        print("  Load order already matches; nothing to change.")
        sync_ini_order(args, order)
        return
    if args.dry_run:
        print("Dry run: no dates were changed.")
        return
    if not args.yes:
        if not sys.stdin.isatty() or input(f"\nSet new dates on {len(changes)} plugin(s)? The old dates are "
                                           f"saved for --restore-dates. [y/N] ").strip().lower() not in ("y", "yes"):
            print("No dates changed.")
            return
    import datetime
    undo = manager_file(DATES_UNDO_FILE)
    with open(undo, "a", encoding="utf-8") as f:
        f.write(f"==== {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        for n, p in changes:
            st = os.stat(p)
            f.write(f"{n}\t{st.st_atime_ns}\t{st.st_mtime_ns}\n")
    for n, p in changes:
        os.utime(p, (want[n], want[n]))         # hard links share the date, so Vortex's copy follows
    print(f"  Dates set on {len(changes)} plugin(s). Old dates saved in {DATES_UNDO_FILE} "
          f"(undo with --restore-dates).")
    sync_ini_order(args, order)
    # only a different order calls for --fix and --multipatch again; new dates on the same order don't
    args.sort_changed = bool(moved)
    if not moved:
        print("  The order itself is the same (only dates changed), so --fix and --multipatch aren't needed.")


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
    if asset_repair_enabled(args):
        common.append("--asset-repair")
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
    first = [("turn back on what earlier runs turned off", None)]
    if read_merge_record(game_dir):
        first.append(("undo the auto-merge", ["--unmerge", "--no-follow-up"]))
    if args.add_all_esp:
        first.append(("add every plugin in Data Files", ["--add-all-esp"]))
    if not getattr(args, "no_redundant", False):
        # early: once the plugin list is whole (merges undone, masters checked), before anything reads the
        # plugins - a plugin turned off here is not cleaned, fixed, merged, patched or asset-checked
        first.append(full_redundant_step(args, common))
    first += [
        (("cell conflicts (your remembered answers)", ["--cell-conflicts", "--use-answers"])
         if getattr(args, "use_answers", False) else
         ("cell conflicts (report)", ["--cell-conflicts", "--no-interactive"])),
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
                    elif extra and extra[0] in ("--redundant", "--redundant-auto"):
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
    fx.add_argument("--asset-repair", action="store_true", default=None,
                    help="with --fix: also change asset files in Data Files (stand-ins, texture reduction, text "
                         "keys, asset health, short paths). Off by default; [Repair] asset_repair = true in "
                         "mwmanager.ini turns it on for every run")
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
                    help=f"find active .esp plugins with no scripts and no cell/land/pathgrid edits that can move "
                         f"together without changing what wins against any other plugin, merge them into one "
                         f"('{AUTO_MERGE_NAME}', or --merge-name), and deactivate them (recorded in {MERGE_RECORD}; "
                         f"--add-all-esp leaves them out). Use --dry-run first")
    mg.add_argument("--merge-name", metavar="NAME.esp", help="with --auto-merge: the merged plugin's name")
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
                    help="undo everything: put every plugin back to its oldest .bak (the original), remove all its backups, and exit")
    fx.add_argument("--clean-up", action="store_true",
                    help="delete .bak backups of plugins that are no longer in Data Files, and keep only the most "
                         "recent .bak of each remaining plugin (use --dry-run to preview)")
    fx.add_argument("--fix-log", default=os.path.join(OUTPUT_DIR, "mwmanager_fixes.log"), help="where to append a record of fixes (default: MWManager outputs folder)")
    import mwmanager_journal
    jg = ap.add_argument_group("install journal (the game folder is the parent of data_dir; nothing is changed)")
    mwmanager_journal.add_arguments(jg)
    args = ap.parse_args(argv)
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
        if getattr(args, "data_dir", None) and os.path.isdir(args.data_dir) and not args.reenable:
            try:
                release_lost_winners(args)
            except (OSError, UnicodeError) as e:
                print(f"(could not check {DISABLED_RECORD}: {e})", file=sys.stderr)
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
                   args.clean_up, args.merge, args.auto_merge, args.unmerge is not None, args.cell_conflicts, args.reenable is not None, args.remove_generated,
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
    if args.unmerge is not None:
        rc = run_unmerge(args, plugins)
        if rc == 0 and not args.no_follow_fix:
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
        plugins, bsas, data_dirs = resolve(args, report)     # those plugins now find their masters
    if args.fix:
        fix_ref_groups(args, plugins)
    if args.fix and fix_long_names(args, plugins, bsas) and not args.dry_run:
        plugins, bsas, data_dirs = resolve(args, report)
    if args.fix and not args.no_doubled:
        gone = {n.lower() for n in fix_doubled(args, plugins)}
        if gone:
            plugins = [(n, p) for n, p in plugins if n.lower() not in gone]
    if args.fix and args.performers_patch and not args.no_performers:
        fix_performers(args, plugins)
    elif args.fix and not args.dry_run:
        drop_performers_patch(args)       # the mwperformers MWSE mod decides in the game now
    if args.fix:
        fix_cell_winners(args, plugins)
    loaded_patches = {n.lower() for n, _p in plugins} & {x.lower() for x in OUR_PATCHES}
    if args.fix or args.patch_esm or args.multipatch:   # rebuilt from scratch: leave them out of the scan
        names = [n.lower() for n, _p in plugins]
        if PATCH_NAME.lower() in names:            # where the ESM patch really loads
            args.esm_patch_pos = sum(1 for n in names[:names.index(PATCH_NAME.lower())]
                                     if n not in [x.lower() for x in OUR_PATCHES])
        else:                                      # new: right after the masters
            args.esm_patch_pos = sum(1 for n in names if n.endswith(".esm"))
        plugins = [(n, p) for n, p in plugins if n.lower() not in [x.lower() for x in OUR_PATCHES]]
    if not plugins:
        print("No plugins found.", file=sys.stderr)
        return 2

    only = {o.lower() for o in args.only} if args.only else None
    if args.sort:
        run_sort(args, resolve(args, Report())[0])     # every active plugin, our patches included
        if getattr(args, "sort_changed", False) and not args.no_follow_fix and not (args.fix and args.multipatch):
            # a new order can undo what the patches fixed (a later plugin now blanking 'Caldera Priory',
            # leveled lists merged in the old order): rebuild them and fix what the new order exposes
            print("\n" + "=" * 70 + "\nFOLLOW-UP: --fix --multipatch with the new load order\n" + "=" * 70)
            args.fix = args.multipatch = True
        if not (args.clean or args.fix or args.multipatch):
            return 0
        plugins, bsas, data_dirs = resolve(args, report)   # dates changed: read the new order
        if (args.fix or args.patch_esm or args.multipatch) and not hasattr(args, "esm_patch_pos"):
            args.esm_patch_pos = sum(1 for n, _p in plugins if n.lower().endswith(".esm"))
        plugins = [(n, p) for n, p in plugins if n.lower() not in [x.lower() for x in OUR_PATCHES]] \
            if (args.fix or args.patch_esm or args.multipatch) else plugins
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


def write_bug_reports(args, plugins, report, world):
    """<plugin>.bugreport.txt for every plugin with problems of its own (section "mwmanager"): its
    errors and warnings, files it needs that no mod has, and what --fix repairs for it."""
    ah = asset_health_module()
    data_dir = os.path.abspath(args.data_dir)
    names = {n.lower(): (n, p) for n, p in plugins}
    per = defaultdict(lambda: {"ERROR": [], "WARN": [], "repaired": [], "missing": []})
    for sev, plugin, topic, info, msg in report.issues:
        if sev not in ("ERROR", "WARN") or is_our_patch(plugin) or plugin.lower() not in names \
                or NOT_OWN_BUG.search(msg):
            continue
        where = " / ".join(x for x in (f"[{topic}]" if topic else "", info) if x)
        per[plugin.lower()][sev].append(f"  - {where + ': ' if where else ''}{msg}")
    for plugin, off, op, desc, label, topic, info in report.fixes:
        if plugin and plugin.lower() in names and not is_our_patch(plugin):
            per[plugin.lower()]["repaired"].append(f"  - {label}: {desc}")
    for item in getattr(world, "generated", []):
        rel, why = item[0], item[1]
        pl = item[3] if len(item) > 3 else ""
        if pl and pl.lower() in names and not is_our_patch(pl) and not (len(item) > 2 and item[2]):
            per[pl.lower()]["missing"].append(f"  - {rel} (needed by {why}); a stand-in was put in its place")
    # the scan saw every plugin: one that is clean now loses an old section
    written = cleared = 0
    for low, (name, path) in names.items():
        if is_our_patch(name) or low in NOT_A_MOD:
            continue
        d = per.get(low)
        lines = []
        if d:
            for title, key in (("Errors (the game reports these, or the content breaks)", "ERROR"),
                               ("Warnings", "WARN"),
                               ("Files the plugin needs that no mod provides", "missing"),
                               ("Repaired by mwmanager --fix (the author may want to fix these in the plugin)",
                                "repaired")):
                if d[key]:
                    lines.append(f"{title}: {len(d[key])}")
                    lines += sorted(set(d[key]))[:300]
                    lines.append("")
        out = output_file(os.path.join("bugreports", ah.bug_report_name(data_dir, name, sys.modules[__name__])))
        if not lines and not os.path.exists(out):
            continue
        ah.update_bug_report(out, "mwmanager", ah.bug_report_header(path, sys.modules[__name__]), lines)
        written += bool(lines)
        cleared += not lines
    if written or cleared:
        print(f"\nBug reports: {written} plugin(s) have <plugin>.bugreport.txt in their mod folder (or in "
              f"bugreports\\)" + (f"; {cleared} cleared" if cleared else "") + ".")


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
