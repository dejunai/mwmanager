"""MWManager layer: --init, the game catalog, MWSE / MCP / MGE XE checks, mlox rules and the core masters.

MWManager only (none of this is in upstream). Runs inside the upstream mwdialog_check namespace after
mwmanager_overrides.py (see mwmanager_upstream.py).
"""
from mwdialog_check import *  # noqa: F401,F403  (names resolve in mwdialog_check when composed)


def major_master_conflict_overrides(plugin_paths):
    """Read resolved major-master records for the generated ESM fixes patch.

    The review saves each choice under the catalog's label (database.catalog.record_identity): interiors by
    name, exteriors as 'Name (x, y)', LAND as '(x, y)', PGRD as 'name (x, y)', SCPT by its header name,
    SKIL/MGEF by index, the rest by NAME. Records are matched by that same label, not by NAME alone.
    A CELL is copied as its header only (name, flags, region, water, ambient light...): its references
    stay with the plugins that place them, since a copied FRMR would make the patch place every object
    again as its own new reference. Dialogue responses (INFO) need their topic, so they follow load order.
    Choices that cannot be applied are printed, never dropped silently."""
    choices_path = manager_file("mwmanager_major_master_conflicts.json")
    try:
        choices = json.loads(Path(choices_path).read_text(encoding="utf-8")).get("resolutions", [])
    except (OSError, ValueError, TypeError):
        return [], []
    wanted = {}
    for item in choices:
        if not isinstance(item, dict):
            continue
        winner = str(item.get("winner", ""))
        tag = str(item.get("record_tag", ""))
        label = str(item.get("editor_id", ""))
        if len(tag) != 4 or not label.strip():
            continue
        if tag == "INFO":
            print(f"  Major-master choice not applied: INFO {label} ({winner}) - a dialogue response "
                  f"can't be copied without its topic; load order decides it.")
            continue
        path = plugin_paths.get(winner.casefold())
        if not path:
            print(f"  Major-master choice not applied: {tag} {label} - {winner} is not in the load order.")
            continue
        wanted.setdefault(path, []).append((tag, label.casefold(), winner, label))
    records, winners = [], []
    for path, items in wanted.items():
        try:
            found = _mm_records_by_label(path, {(tag, key) for tag, key, _w, _l in items})
        except (OSError, FormatError) as exc:
            found = {}
            print(f"  Major-master choices from {Path(path).name} not applied: {exc}")
        for tag, key, winner, label in items:
            raw = found.get((tag, key))
            if raw is None:
                print(f"  Major-master choice not applied: {tag} {label} - not found in {winner}.")
                continue
            records.append(raw)
            winners.append(winner)
    return records, winners


def _mm_records_by_label(path, keys):
    """{(tag, casefolded label): raw record} for the last record per key in one plugin (CELLs header-only)."""
    from database.catalog import record_identity
    tags = {tag for tag, _key in keys}
    with open(path, "rb") as stream:
        data = stream.read()
    found = {}
    for off, tag, _flags, body in iter_records(data):
        if tag not in tags:
            continue
        identity = record_identity(tag, body)
        if identity is None or (tag, identity[1].casefold()) not in keys:
            continue
        raw = data[off:off + 16 + len(body)]
        found[(tag, identity[1].casefold())] = _mm_cell_header(raw) if tag == "CELL" else raw
    return found


def _mm_cell_header(raw):
    """A CELL record cut down to the subrecords before its first reference (FRMR, MVRF or NAM0)."""
    body = raw[16:]
    head = []
    for name, data in iter_subs(body):
        if name in ("FRMR", "MVRF", "NAM0"):
            break
        head.append(struct.pack("<4sI", name.encode("ascii"), len(data)) + bytes(data))
    out = b"".join(head)
    return raw[:4] + struct.pack("<I", len(out)) + raw[8:16] + out


from mwmanager_mlox import MLOX_MY_RULES, MLOX_RULE_URLS, download_mlox_rules, import_personal_rules

MLOX_SUPPORTED_SECTIONS = {"order", "nearstart", "nearend", "exact"}
MLOX_SECTION_LINE = re.compile(r"^\s*\[([A-Za-z][A-Za-z0-9_]*)\]\s*(?:;.*)?$")


def clean_mlox_rule_file(path):
    """Keep only load-order sections MWManager understands, including its [Exact] extension."""
    try:
        with open(path, "r", encoding=ENC, errors="replace") as stream:
            original = stream.read()
    except OSError as exc:
        return False, f"cannot read {path}: {exc}"

    kept, active = [], True
    for line in original.splitlines(keepends=True):
        header = MLOX_SECTION_LINE.match(line.rstrip("\r\n"))
        if header:
            active = header.group(1).casefold() in MLOX_SUPPORTED_SECTIONS
            if active:
                kept.append(line)
        elif active:
            kept.append(line)
    cleaned = "".join(kept)
    if cleaned == original:
        return False, None

    try:
        if not path.endswith(".mwmanager-original"):
            backup = backup_file("rules", os.path.basename(path) + ".mwmanager-original")
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
        with open(path, "w", encoding=ENC, errors="replace", newline="") as stream:
            stream.write(cleaned)
    except OSError as exc:
        return False, f"cannot clean {path}: {exc}"
    return True, None


def ensure_mlox_rules(game_dir=None, *, dry_run=False, yes=False, disabled=False):
    """Import local game rules once, then offer to fetch missing rules and clean MWManager copies."""
    folder = manager_file("mlox")
    if disabled:
        return False
    os.makedirs(folder, exist_ok=True) if not dry_run else None
    # Import the user's installed rules once. After import, the MWManager copy is authoritative.
    import_marker = os.path.join(folder, ".imported_game_rules.json")
    if game_dir and not os.path.isfile(import_marker):
        game_rules = os.path.join(game_dir, "mlox")
        found = [(name, os.path.join(game_rules, name)) for name in MLOX_RULE_URLS
                 if os.path.isfile(os.path.join(game_rules, name))]
        if found and dry_run:
            print("--dry-run: would copy installed mlox rules from " + os.path.join(game_dir, "mlox"))
        elif found:
            for name, source in found:
                target = os.path.join(folder, name)
                if os.path.isfile(target):
                    digest = hashlib.sha256(os.path.normcase(os.path.abspath(game_dir)).encode("utf-8")).hexdigest()[:12]
                    backup = backup_file("rules", f"{digest}-{name}.pre-game-import")
                    if not os.path.exists(backup):
                        shutil.copy2(target, backup)
                shutil.copy2(source, target)
                print(f"MWManager: copied {os.path.join('mlox', name)} into its managed rules folder")
            with open(import_marker, "w", encoding="utf-8") as stream:
                json.dump({"game_dir": os.path.abspath(game_dir), "files": [name for name, _ in found]}, stream, indent=2)
    if game_dir and not dry_run:
        for name in import_personal_rules(game_dir, folder):
            print(f"MWManager: copied {os.path.join('mlox', name)} into its managed rules folder")

    missing = [name for name in MLOX_RULE_URLS if not os.path.isfile(os.path.join(folder, name))]
    if dry_run:
        if missing:
            if disabled:
                print("--dry-run: mlox rules are disabled; would not download " + ", ".join(missing))
            else:
                print("--dry-run: mlox rules missing; would offer to download " + ", ".join(missing) + " from DanaePlays/mlox-rules")
        return not missing

    if missing and disabled:
        return False

    if missing and not yes:
        use_rules = False
        if sys.stdin.isatty():
            answer = input("MWManager has no mlox rules. Download and clean them now? [y/N] ").strip().casefold()
            use_rules = answer in {"y", "yes"}
        else:
            try:
                import mwmanager_dialogs as messagebox
                import tkinter as tk
                prompt_root = tk.Tk()
                prompt_root.withdraw()
                try:
                    use_rules = messagebox.askyesno(
                        "mlox load-order rules",
                        "No mlox rules were found in the Morrowind folder or MWManager.\n\n"
                        "Download the community rules from GitHub and clean them for MWManager?\n\n"
                        "Choose No to continue without mlox rules. You can enable them later by running --sort without --no-mlox-rules.",
                        parent=prompt_root)
                finally:
                    prompt_root.destroy()
            except Exception:
                use_rules = False
        if not use_rules:
            print("MWManager: continuing without mlox rules; use --no-mlox-rules to make this choice explicit.")
            return False

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        print(f"WARNING: cannot create MWManager mlox rules folder: {exc}", file=sys.stderr)
        return False

    if missing:
        result = download_mlox_rules(folder, names=missing, overwrite=False,
                                    backup_dir=backup_file("rules"))
        if not result["ok"]:
            for error in result["errors"]:
                print(f"WARNING: {error}", file=sys.stderr)
            return False
        for name in result["written"]:
            print(f"MWManager: downloaded missing mlox rules file {name} from {MLOX_RULE_URLS[name]}")

    for name in MLOX_RULE_URLS:
        changed, error = clean_mlox_rule_file(os.path.join(folder, name))
        if error:
            print(f"WARNING: {error}", file=sys.stderr)
            return False
        if changed:
            print(f"MWManager: cleaned {name}; retained [Order], [NearStart], [NearEnd], and [Exact] rules")
    return True


CORE_HASH_FILES = (*CORE_GAME_ESMS, *CORE_GAME_BSAS)
PREREQUISITE_URLS = {
    "texconv": "https://github.com/microsoft/DirectXTex/wiki/Texconv",
    "ffmpeg": "https://ffmpeg.org/download.html",
    "mcp": "https://www.nexusmods.com/morrowind/mods/26348?tab=files",
    "mwse": "https://www.nexusmods.com/morrowind/mods/45468?tab=files",
}


def show_download_interface(items, title="MWManager prerequisites"):
    """Offer browser links in a small Tk window; fall back to printable URLs without a desktop."""
    try:
        import tkinter as tk
        import webbrowser
        from tkinter import ttk
        from mwmanager_theme import apply_theme, center
        root = tk.Tk()
        apply_theme(root)
        root.title(title)
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=(18, 14))
        frame.pack(fill="both", expand=True)
        for heading, detail, url in items:
            ttk.Label(frame, text=heading, style="H3.TLabel", anchor="w").pack(fill="x", pady=(4, 0))
            ttk.Label(frame, text=detail, justify="left", anchor="w", wraplength=540).pack(fill="x", pady=(1, 2))
            ttk.Button(frame, text="Open download page", command=lambda u=url: webbrowser.open(u)).pack(anchor="e", pady=(0, 5))
        ttk.Button(frame, text="Close", command=root.destroy).pack(anchor="e", pady=(6, 0))
        center(root)
        root.mainloop()
    except Exception as exc:
        print(f"(Could not open the prerequisite window: {exc})")
        for heading, _detail, url in items:
            print(f"  {heading}: {url}")


def check_mwse_latest_commit(timeout=10):
    """Return the latest upstream MWSE commit SHA used as a nightly-build freshness proxy."""
    from urllib.request import Request, urlopen
    request = Request("https://api.github.com/repos/MWSE/MWSE/commits/master",
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "MWManager"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    sha = payload.get("sha", "") if isinstance(payload, dict) else ""
    return sha.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", sha) else None


def offer_mwse_update(game_dir, *, dry_run=False, yes=False):
    """Check MWSE freshness ourselves; offer the official updater only when needed or unknown."""
    updater = os.path.join(game_dir, "MWSE-Update.exe")
    if not os.path.isfile(updater):
        print("\nWARNING: MWSE-Update.exe was not found in the Morrowind game folder. "
              "MWSE may be missing or its updater may need to be restored.")
        if dry_run:
            print("--dry-run: would offer the MWSE installation page.")
        else:
            show_download_interface([("MWSE and its updater",
                                      "MGE XE normally includes MWSE-Update.exe. Install/update MWSE, then rerun MWManager.",
                                      PREREQUISITE_URLS["mwse"])], "MWSE updater missing")
        return False

    marker = os.path.join(game_dir, "mwse-version.txt")
    installed = os.path.isfile(marker) and os.path.isfile(os.path.join(game_dir, "MWSE.dll"))
    installed_sha = None
    if installed:
        try:
            candidate = open(marker, encoding="ascii", errors="ignore").read().strip()
            installed_sha = candidate.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", candidate) else None
        except OSError:
            pass
    if not installed:
        print("\nMWSE appears incomplete (version marker or MWSE.dll missing).")
    else:
        print(f"\nMWSE installation found; installed build marker: {installed_sha or 'unrecognized'}.")

    latest_sha = None
    check_error = None
    try:
        latest_sha = check_mwse_latest_commit()
    except Exception as exc:
        check_error = str(exc)
    if latest_sha and installed_sha and latest_sha == installed_sha:
        print("MWSE is current according to the upstream build marker; no update prompt is needed.")
        return True
    if latest_sha and installed_sha:
        print(f"A newer MWSE upstream revision is available ({installed_sha[:12]} -> {latest_sha[:12]}).")
        reason = "A newer MWSE build is available."
    elif not installed:
        reason = "MWSE is missing or incomplete."
    else:
        print(f"WARNING: MWSE update status could not be verified ({check_error or 'unrecognized installed build marker'}).",
              file=sys.stderr)
        reason = "MWSE update status could not be verified. Run its official updater to check for an update?"

    if dry_run:
        print(f"--dry-run: would ask whether to run MWSE-Update.exe. Reason: {reason}")
        return installed
    answer = bool(yes)
    if yes:
        print("--yes: running the official MWSE updater because the installed build is outdated or could not be verified.")
    else:
        try:
            import tkinter as tk
            import mwmanager_dialogs as messagebox
            root = tk.Tk()
            root.withdraw()
            answer = messagebox.askyesno(
                "MWSE update available",
                reason + "\n\nMWManager will run MWSE-Update.exe and wait for it to finish.",
                parent=root)
            root.destroy()
        except Exception:
            try:
                answer = input(reason + " [y/N] ").strip().casefold() in {"y", "yes"}
            except (EOFError, OSError):
                answer = False
    if not answer:
        print("MWSE update skipped; continuing with the installed build.")
        return installed
    try:
        result = subprocess.run([updater], cwd=game_dir, check=False)
    except OSError as exc:
        print(f"WARNING: could not start MWSE-Update.exe: {exc}", file=sys.stderr)
        return False
    if result.returncode:
        print(f"WARNING: MWSE updater exited with code {result.returncode}; continuing.", file=sys.stderr)
        return False
    print("MWSE updater finished; initialization will lint the installed core Lua afterward.")
    return True


def mcp_beta_status(game_dir):
    """Detect MCP Skunk Works from its beta patch definitions in mcpatch/."""
    mcpatch_dir = os.path.join(game_dir, "mcpatch")
    describe_path = os.path.join(mcpatch_dir, "describe.json")
    try:
        with open(describe_path, encoding="utf-8-sig") as stream:
            categories = json.load(stream)
    except (OSError, ValueError) as exc:
        return False, f"MCP patch metadata could not be read from {describe_path}: {exc}"

    beta_ids = {
        patch.get("id")
        for category in categories if isinstance(category, dict)
        and str(category.get("category", "")).strip().casefold() == "beta"
        for patch in category.get("patches", []) if isinstance(patch, dict)
        and isinstance(patch.get("id"), int)
    }
    expected_ids = set(range(180, 196))
    if expected_ids.issubset(beta_ids):
        return True, "mcpatch/ contains the MCP Skunk Works beta patch definitions (IDs 180–195)."
    if beta_ids:
        return False, f"mcpatch/ has a Beta category, but the expected Skunk Works patch set (IDs 180–195) is incomplete. Found IDs: {', '.join(map(str, sorted(beta_ids)))}."
    return False, f"mcpatch/ does not contain the MCP Skunk Works beta patch definitions: {describe_path} has no Beta category."


def warn_mcp_beta(game_dir, *, show_ui=True):
    ok, detail = mcp_beta_status(game_dir)
    if ok:
        print("MCP beta check: " + detail)
        return True
    print("\nWARNING: MCP Skunk Works / MCP 2.5 beta was not detected. " + detail)
    print("MWManager recommends installing the beta over the base MCP. You may continue without it.")
    if show_ui:
        show_download_interface([("MCP Skunk Works beta",
                                  "Install MCP Skunk Works over the base MCP, then rerun initialization.",
                                  PREREQUISITE_URLS["mcp"])], "MCP beta recommended")
    return False


def audit_core_files(data_dir, game_dir, *, initializing=False, dry_run=False, db_path=None):
    """Compare the core ESMs and BSAs with mwmanager_core_hashes.json (shipped with MWManager, never written);
    advise restoring originals but never block the run."""
    import mwmanager_prereqs
    lookup = index_dir(data_dir)
    mismatches = []
    for name in CORE_HASH_FILES:
        path = lookup.get(name.casefold())
        known = mwmanager_prereqs.CORE_FILE_HASHES.get(name, ())
        if not path or not known:
            continue
        actual = sha256_file(path)
        if actual.casefold() not in known:
            mismatches.append((name, known[0], actual))
    if mismatches:
        print("\nWARNING: one or more core game files differ from the original SHA-256 hashes:")
        for name, expected, actual in mismatches:
            print(f"  ALTERED  {name}  expected {expected[:16]}… got {actual[:16]}…")
        print("  MWManager repairs mod content without editing these core files. We recommend restoring the "
              "original files through GOG Galaxy's verify/repair option. You may continue; no action was blocked.")
    return bool(mismatches)


def planned_ini_registrations(ini, section_name, key_prefix, canonical_names):
    """Return INI text with canonical numbered entries first and existing extras retained."""
    raw = open(ini, "rb").read()
    text = raw.decode(ENC, errors="replace")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    section_indexes = []
    current = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip().casefold()
        if current == section_name.casefold():
            section_indexes.append(i)
    if not section_indexes:
        lines.extend(["", f"[{section_name}]"])
        section_indexes = [len(lines) - 1]
    start = section_indexes[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = i
            break
    existing = []
    pattern = re.compile(r"^\s*" + re.escape(key_prefix) + r"\s*(\d+)\s*=\s*(.*?)\s*$", re.I)
    for line in lines[start + 1:end]:
        match = pattern.match(line)
        if match:
            existing.append((int(match.group(1)), match.group(2).strip().strip('"')))
    extras = [value for _index, value in sorted(existing)
              if value.casefold() not in {name.casefold() for name in canonical_names}]
    registered = list(canonical_names) + extras
    retained = [line for line in lines[start + 1:end] if not pattern.match(line)]
    rebuilt = retained + [f"{key_prefix}{i}={name}" for i, name in enumerate(registered)]
    lines = lines[:start + 1] + rebuilt + lines[end:]
    return newline.join(lines) + newline, registered


def plan_core_ini(ini):
    """Ensure all canonical ESMs and BSAs are registered, retaining other registrations."""
    original = open(ini, "rb").read().decode(ENC, errors="replace")
    text, esms = planned_ini_registrations(ini, "Game Files", "GameFile", CORE_GAME_ESMS)
    # Apply the second section to the intermediate text without touching disk.
    temp = tempfile.NamedTemporaryFile(delete=False)
    try:
        temp.write(text.encode(ENC, errors="replace"))
        temp.close()
        text, bsas = planned_ini_registrations(temp.name, "Archives", "Archive ", CORE_GAME_BSAS)
    finally:
        try:
            os.unlink(temp.name)
        except OSError:
            pass
    return original, text, esms, bsas


def lint_installed_core_mwse(game_dir, *, dry_run=False):
    """Lint the Lua shipped with the installed MWSE core (MWSE\\core, MWSE\\lib) and apply its conservative
    repairs. Each original is kept once, first copy wins, in Data Files\\MWSE\\backups\\<path under MWSE>; MWSE
    only runs Lua from MWSE\\core, MWSE\\mods (and the legacy MWSE\\lua), so that folder is never loaded. Mods
    in MWSE\\mods are not touched here: their originals stay in their staged mod."""
    data_dir = os.path.join(os.path.abspath(game_dir), "Data Files")
    mwse_dir = os.path.join(data_dir, "MWSE")
    if not os.path.isdir(mwse_dir):
        print(f"--init: MWSE core Lua folder not found; skipped lint ({mwse_dir})")
        return 0
    manager_dir = os.path.dirname(os.path.abspath(__file__))
    linter = os.path.join(manager_dir, "mwse_lint.py")
    if not os.path.isfile(linter):
        print(f"--init: MWSE Lua linter not found: {linter}", file=sys.stderr)
        return 0
    report = os.path.join(OUTPUT_DIR, "mwmanager_core_mwse_lua_report.json")
    bugreport = os.path.join(OUTPUT_DIR, "mwmanager_core_mwse_bugreport.txt")
    backup_root = os.path.join(mwse_dir, "backups")
    older_root = os.path.join(MANAGER_DIR, "backups", "mwse-core")    # where versions before 2026-11-13 kept them
    if dry_run:
        print(f"--dry-run: would lint and apply conservative repairs to installed MWSE core Lua under {mwse_dir}")
        print(f"--dry-run: originals would be saved under {backup_root}")
        print(f"--dry-run: JSON findings and sendable bugreport would be written under {OUTPUT_DIR}")
        return 0
    command = [sys.executable, linter, "--staging", data_dir, "--report", report, "--bugreport", bugreport,
               "--only", "MWSE", "--exclude", "MWSE/mods", "MWSE/backups", "MWSE/lua",
               "--backup-root", backup_root, "--backup-relative-to", mwse_dir,
               "--prior-backup-root", older_root, "--apply"]
    result = subprocess.run(command, check=False)
    if result.returncode:
        print(f"--init: MWSE core Lua lint returned {result.returncode}; initialization will continue. See {report}",
              file=sys.stderr)
    else:
        print(f"--init: installed MWSE core Lua lint/repair complete; JSON: {report}; bugreport: {bugreport}")
    return 0


# MWManager's earlier Lua packaging under MWSE\\mods: the consolidated MWManager mod (its mwquickfixes tab,
# its off\\legacy-standalone archive) and a standalone mwquickfixes folder. The suite replaces them.
RETIRED_MWSE_MODS = ("MWManager", "mwquickfixes", "mwplainpaperfix")


# reference\<folder>\mods\<name> and \metadata: the suite (copies of the Steam install), then MWManager's own
# mwmanagermenu, which puts the suite's settings pages under one Mod Config Menu entry
LUA_SOURCES = ("mwse-lua-guards", "mwmanager-menu")


def _suite_source():
    """(reference folder, {mod name: its source folder}, [metadata toml files])."""
    reference = Path(os.path.dirname(os.path.abspath(__file__))) / "reference"
    mods, metadata = {}, []
    for folder in LUA_SOURCES:
        root = reference / folder
        if (root / "mods").is_dir():
            mods.update({p.name: p for p in sorted((root / "mods").iterdir()) if (p / "main.lua").is_file()})
        if (root / "metadata").is_dir():
            metadata += sorted((root / "metadata").glob("*-metadata.toml"))
    return reference, mods, metadata


def _unique_archive(root, name):
    archived = root / name
    suffix = 2
    while archived.exists():
        archived = root / f"{name}-{suffix}"
        suffix += 1
    return archived


def _copy_replacing(source_file, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".mwmanager-tmp")
    shutil.copy2(source_file, temporary)
    os.replace(temporary, target)


def install_mwse_lua_guards(args):
    """Install the mw* MWSE Lua suite under MWSE\\mods\\mwsuite and its metadata in Data Files."""
    data_dir = Path(args.data_dir).resolve()
    mwse_dir = data_dir / "MWSE"
    if not mwse_dir.is_dir():
        print(f"--init: MWSE core folder not found; Lua suite installation deferred ({mwse_dir})")
        return 0
    source, mods, metadata = _suite_source()
    names = sorted(mods)
    if not names:
        print(f"--init: MWSE Lua suite source not found: {source}", file=sys.stderr)
        return 2
    target_root = mwse_dir / "mods"
    suite_root = target_root / "mwsuite"
    bootstrap = source / "mwmanager-menu" / "bootstrap" / "main.lua"
    if not bootstrap.is_file():
        print(f"--init: MWSE Lua suite bootstrap not found: {bootstrap}", file=sys.stderr)
        return 2
    # Data Files\\MWSE\\backups\\mods\\<name>: beside MWSE, where MWSE never loads Lua from (it runs every main.lua
    # under MWSE\\mods at any depth, so nothing that is not live may stay there).
    legacy_root = mwse_dir / "backups" / "mods"
    existing = {p.name.casefold(): p for p in target_root.iterdir() if p.is_dir()} if target_root.is_dir() else {}
    retired_names = set(RETIRED_MWSE_MODS) | set(names)
    retired = [existing[n.casefold()] for n in sorted(retired_names) if n.casefold() in existing]
    nested = {p.name.casefold(): p for p in suite_root.iterdir() if p.is_dir()} if suite_root.is_dir() else {}
    deployment = data_dir / "vortex.deployment.json"
    if deployment.is_file():
        try:
            manifest = json.loads(deployment.read_text(encoding="utf-8-sig"))
            staging = Path(manifest["stagingPath"])
            for entry in manifest["files"]:
                rel = Path(entry["relPath"].replace("\\", os.sep))
                parts = rel.parts
                if (len(parts) >= 4 and [p.casefold() for p in parts[:2]] == ["mwse", "mods"]
                        and parts[2].casefold() in {n.casefold() for n in retired_names}
                        and (staging / entry["source"] / rel).is_file()):
                    print(f"--init: Vortex still stages the old Lua suite path: {rel}. "
                          "Move that package to MWSE\\mods\\mwsuite and redeploy before installing.",
                          file=sys.stderr)
                    return 2
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"--init: cannot inspect Vortex deployment manifest {deployment}: {exc}", file=sys.stderr)
            return 2
    if args.dry_run:
        for name in names:
            print(f"--dry-run: would install MWSE Lua suite mod {name} to {suite_root / name}")
        print(f"--dry-run: would install suite bootstrap to {suite_root / 'main.lua'}")
        for toml in metadata:
            print(f"--dry-run: would install {toml.name} to {data_dir}")
        for old in retired:
            print(f"--dry-run: would move {old} to {legacy_root}")
        return 0

    install_ledger = ledger_run = None
    try:
        import mwmanager_journal
        candidate = mwmanager_journal.Journal(Path(MANAGER_DIR) / "journal")
        if candidate.has_baseline():
            install_ledger = candidate
            ledger_run = install_ledger.start_run("mwmanager.py", {"operation": "install MWSE Lua suite"})
    except (ImportError, NameError):
        pass

    def move_file(source_file, destination_file):
        if install_ledger:
            install_ledger.move_original(data_dir.parent, source_file, destination_file,
                                         ledger_run["run"], tag="mwse-lua-suite")
        else:
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_file), str(destination_file))

    def copy_file(source_file, destination_file):
        original = install_ledger.preserve_original(data_dir.parent, destination_file) if install_ledger else None
        _copy_replacing(source_file, destination_file)
        if install_ledger:
            rel = mwmanager_journal.rel_of(data_dir.parent, destination_file)
            install_ledger.record(ledger_run["run"], rel,
                                  "replace" if original["kind"] != "absent" else "create",
                                  mwmanager_journal.sha256_file(destination_file), original,
                                  tag="mwse-lua-suite")

    try:
        for old in retired:
            archived = _unique_archive(legacy_root, old.name)
            for source_file in sorted(path for path in old.rglob("*") if path.is_file()):
                move_file(source_file, archived / source_file.relative_to(old))
            shutil.rmtree(old)
        for name in names:
            mod_source = mods[name]
            files = {p.relative_to(mod_source) for p in mod_source.rglob("*") if p.is_file()}
            destination = nested.get(name.casefold(), suite_root / name)
            # files a suite mod no longer has (older modules, editor backups) leave MWSE\\mods
            stale = sorted(p.relative_to(destination) for p in destination.rglob("*")
                           if p.is_file() and p.relative_to(destination) not in files) if destination.is_dir() else []
            if stale:
                archived = _unique_archive(legacy_root, name)
                for rel in stale:
                    move_file(destination / rel, archived / rel)
            for rel in sorted(files):
                copy_file(mod_source / rel, destination / rel)
        copy_file(bootstrap, suite_root / "main.lua")
        for toml in metadata:
            copy_file(toml, data_dir / toml.name)
    except OSError as exc:
        print(f"--init: could not install the MWSE Lua suite: {exc}", file=sys.stderr)
        return 2
    if install_ledger:
        install_ledger.finish_run(ledger_run)
    print(f"--init: installed the MWSE Lua suite ({len(names)} mods: {', '.join(names)}) under {suite_root}")
    for old in retired:
        print(f"--init: moved MWManager's earlier Lua package {old.name} to {legacy_root}")
    return 0


def initialize_manager(args):
    """Initialize the local catalog and independent load-order list from INI GameFile0..2."""
    data_dir = os.path.abspath(args.data_dir)
    game_dir = os.path.dirname(data_dir)
    if os.path.splitdrive(os.path.abspath(STAGING_DIR))[0].casefold() != os.path.splitdrive(game_dir)[0].casefold():
        print(f"--init: staging must be on the same drive as Morrowind for hardlinks: {STAGING_DIR}", file=sys.stderr)
        return 2, []
    if not args.dry_run:
        try:
            os.makedirs(STAGING_DIR, exist_ok=True)
        except OSError as exc:
            print(f"--init: cannot create staging folder {STAGING_DIR}: {exc}", file=sys.stderr)
            return 2, []
    ini = os.path.abspath(args.ini or os.path.join(game_dir, "Morrowind.ini"))
    if not os.path.isfile(ini):
        print(f"--init: cannot read {ini}", file=sys.stderr)
        return 2, []
    names = list(CORE_GAME_ESMS)
    try:
        original_ini, planned_ini, registered_esms, registered_bsas = plan_core_ini(ini)
    except (OSError, UnicodeError) as exc:
        print(f"--init: cannot plan canonical INI registrations: {exc}", file=sys.stderr)
        return 2, []
    lookup = index_dir(data_dir)
    missing = [name for name in (*CORE_GAME_ESMS, *CORE_GAME_BSAS) if name.lower() not in lookup]
    if missing:
        print("--init: required canonical game files are missing: " + ", ".join(missing), file=sys.stderr)
        return 2, []
    texconv = os.path.join(TOOLS_DIR, "texconv.exe")
    if not os.path.isfile(texconv):
        texconv = shutil.which("texconv")
    if not texconv:
        print("--init: required prerequisite texconv.exe was not found. Put it in MWManager\\tools\\ or add it to PATH.", file=sys.stderr)
        if not args.dry_run:
            show_download_interface([("texconv.exe", "DirectXTex texconv is MIT-licensed; download or install it, then place it in MWManager\\tools.", PREREQUISITE_URLS["texconv"])])
        return 2, []
    ffmpeg = os.path.join(TOOLS_DIR, "ffmpeg.exe")
    if not os.path.isfile(ffmpeg):
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("--init: required prerequisite ffmpeg.exe was not found. Keep it in MWManager\\tools or add it to PATH; "
              "--full uses it to repair WAV and MP3 assets.", file=sys.stderr)
        if not args.dry_run:
            show_download_interface([("FFmpeg", "MWManager does not bundle FFmpeg because Windows builds can include different codec libraries and license terms. Use the official FFmpeg download page to choose a build.", PREREQUISITE_URLS["ffmpeg"])])
        return 2, []
    audit_core_files(data_dir, game_dir, initializing=True, dry_run=args.dry_run,
                     db_path=getattr(args, "catalog_db", None))
    warn_mcp_beta(game_dir, show_ui=not args.dry_run)
    offer_mwse_update(game_dir, dry_run=args.dry_run, yes=args.yes)
    if planned_ini != original_ini:
        if args.dry_run:
            print("--dry-run: would register canonical files in Morrowind.ini:")
            for index, name in enumerate(registered_esms):
                print(f"  GameFile{index}={name}")
            for index, name in enumerate(registered_bsas):
                print(f"  Archive {index}={name}")
        else:
            backup = backup_file("ini", os.path.basename(ini) + ".mwmanager-init.bak")
            shutil.copy2(ini, backup)
            with open(ini, "wb") as stream:
                stream.write(planned_ini.encode(ENC, errors="replace"))
            print(f"--init: registered canonical ESMs and BSAs in {ini} (backup: {backup})")
    else:
        print("--init: all canonical ESMs and BSAs are already registered in Morrowind.ini")

    if not ensure_mlox_rules(game_dir, dry_run=args.dry_run, yes=args.yes,
                             disabled=getattr(args, "no_mlox_rules", False)):
        if not args.dry_run:
            args.no_mlox_rules = True
            detail = ("disabled by request" if getattr(args, "no_mlox_rules", False)
                      else "unavailable; initialization can continue, but sorting will lack those rules")
            print(f"--init: mlox rules {detail}.", file=sys.stderr)

    root = os.path.dirname(os.path.abspath(__file__))
    catalog = os.path.join(root, "database", "catalog.py")
    if not os.path.isfile(catalog):
        print(f"--init: catalog utility not found: {catalog}", file=sys.stderr)
        return 2, []
    command = [sys.executable, catalog, "--game-dir", game_dir, "--ini", ini, "--canonical-core"]
    if getattr(args, "catalog_db", None):
        command += ["--db", args.catalog_db]
    if args.dry_run:
        command.append("--inventory-only")
    print("--init: starting catalog with these ESMs in INI order:")
    for i, name in enumerate(names):
        print(f"  {i}: {name}")
    result = subprocess.run(command, check=False)
    if result.returncode:
        print(f"--init: catalog utility failed with exit code {result.returncode}", file=sys.stderr)
        return result.returncode, []
    if args.dry_run:
        print(f"--dry-run: would initialize/update SQLite catalog at "
              f"{getattr(args, 'catalog_db', None) or os.path.join(root, 'game_catalog.sqlite3')}")
        print(f"--dry-run: would write independent working load order to {LOAD_ORDER_PATH}")
        print("--dry-run: no INI, catalog, or load-order files were changed")
        return 0, names
    write_load_order(game_dir, names, "mwmanager --init")
    print(f"--init: independent working load order written to {LOAD_ORDER_PATH}")
    return 0, names


def post_init_data_files_maintenance(args):
    """Tidy loose documents on first init and migrate official add-on docs on later inits."""
    data_dir = os.path.abspath(args.data_dir)
    game_key = hashlib.sha256(os.path.normcase(data_dir).encode("utf-8", "surrogatepass")).hexdigest()[:16]
    marker = os.path.join(OUTPUT_DIR, f"mwmanager_post_init_tidy_{game_key}.json")
    tidy = os.path.join(MANAGER_DIR, "mwmanager_install.py")
    if not os.path.isfile(tidy):
        print(f"--init: Data Files tidy utility not found: {tidy}", file=sys.stderr)
        return 2
    command = [sys.executable, tidy, "--data-files", data_dir, "--once-marker", marker]
    command.append("--dry-run" if args.dry_run else "--apply")
    result = subprocess.run(command, check=False)
    if result.returncode:
        print(f"--init: Data Files tidy failed with exit code {result.returncode}; it can retry on the next --init.",
              file=sys.stderr)
    return result.returncode


def refresh_init_catalog(args, plugins):
    """Refresh the selected ESM/ESP records without dropping plugins catalogued in earlier init phases."""
    root = os.path.dirname(os.path.abspath(__file__))
    catalog = os.path.join(root, "database", "catalog.py")
    game_dir = os.path.dirname(os.path.abspath(args.data_dir))
    command = [sys.executable, catalog, "--game-dir", game_dir, "--plugin-files", *plugins, "--append"]
    if args.ini:
        command += ["--ini", args.ini]
    if getattr(args, "catalog_db", None):
        command += ["--db", args.catalog_db]
    if args.dry_run:
        command.append("--inventory-only")
    result = subprocess.run(command, check=False)
    if result.returncode:
        print(f"--init: catalog refresh failed with exit code {result.returncode}", file=sys.stderr)
    return result.returncode
