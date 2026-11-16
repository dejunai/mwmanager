"""Discover and safely stage downloaded Morrowind major-master archives."""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import urllib.request
import zipfile
import hashlib
import unicodedata
from pathlib import Path

# Extend through mwmanager_core_addons.json as more community masters are identified.
BUILTIN_MASTERS = {
    # Patch for Purists (the unofficial patch) loads right after Bloodmoon.esm, before every other master
    "patch for purists.esm": ("patch for purists",),
    "tamriel_data.esm": ("tamriel data", "tamriel_data"),
    "oaab_data.esm": ("oaab_data", "oaab data"),
    "aatl_data.esm": ("aatl_data", "aatl data"),
    "dr_data.esm": ("dr_data", "dr data"),
    "sound_data.esm": ("sound_data", "sound data"),
    "tr_mainland.esm": ("tamriel rebuilt", "tamrielrebuild"),
    "cyr_main.esm": ("project cyrodiil", "cyr_main", "province cyrodiil"),
    "sky_main.esm": ("skyrim home of the nords", "sky_main", "shotn"),
}
NEXUS_MOD_IDS = {
    "patch for purists.esm": 45096,
    "tamriel_data.esm": 44537,
    "oaab_data.esm": 49042,
    "aatl_data.esm": 50647,
    "dr_data.esm": 51776,
    "sound_data.esm": 58854,
    "tr_mainland.esm": 42145,
    "cyr_main.esm": 44922,
    "sky_main.esm": 44921,
}
NEXUS_GRAPHQL = "https://api.nexusmods.com/v2/graphql"
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
PLUGIN_EXTENSIONS = {".esm", ".esp"}
PLUGIN_OVERRIDE_CONFIG = "mwmanager_plugin_overrides.json"
NON_MASTER_PLUGIN_BLACKLIST = "mwmanager_non_master_plugin_blacklist.json"
LARGE_ARCHIVE_BYTES = 512 * 1024 * 1024


def extraction_status(archive: Path, index: int, total: int) -> str:
    size = archive.stat().st_size
    prefix = f"Extracting archive {index} of {total} to staging: {archive.name}"
    if size >= LARGE_ARCHIVE_BYTES:
        return (f"{prefix} ({size / (1024 ** 3):.1f} GB). Large archives can take several minutes to unpack; "
                "please be patient—MWManager is still working.")
    return prefix


def _quiet_subprocess_options() -> dict:
    """Prevent helper console windows flashing when called by the Tk application."""
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


def _seven_zip(manager_dir: Path) -> str | None:
    candidates = [manager_dir / "tools" / "7zip" / "7z.exe",
                  manager_dir / "tools" / "7z.exe"]
    located = shutil.which("7z")
    if located:
        candidates.append(Path(located))
    candidates.extend(Path(os.environ[env]) / "7-Zip" / "7z.exe"
                      for env in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(env))
    return next((str(p) for p in candidates if p.is_file()), None)


def _members(path: Path, seven_zip: str | None) -> tuple[list[str], str | None]:
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                return [i.filename for i in zf.infolist() if not i.is_dir()], None
        except (OSError, zipfile.BadZipFile) as exc:
            return [], str(exc)
    if not seven_zip:
        return [], "7-Zip is required to inspect this archive format"
    try:
        result = subprocess.run([seven_zip, "l", "-slt", "-ba", str(path)],
                                capture_output=True, text=True, errors="replace", timeout=180,
                                **_quiet_subprocess_options())
        if result.returncode:
            return [], (result.stderr or result.stdout or f"7-Zip exited {result.returncode}").strip()
        return [m.group(1) for line in result.stdout.splitlines()
                if (m := re.match(r"Path = (.*)", line)) and m.group(1) not in (str(path), "")], None
    except (OSError, subprocess.SubprocessError) as exc:
        return [], str(exc)


def _load_non_master_blacklist(manager: Path) -> dict:
    path = manager / NON_MASTER_PLUGIN_BLACKLIST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("plugins", {})
            data.setdefault("archives", {})
            return data
    except (OSError, ValueError):
        pass
    return {"plugins": {}, "archives": {}}


def _save_non_master_blacklist(manager: Path, blacklist: dict) -> None:
    """Atomically persist scan exclusions as soon as a non-master archive is confirmed."""
    manager.mkdir(parents=True, exist_ok=True)
    path = manager / NON_MASTER_PLUGIN_BLACKLIST
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(blacklist, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _archive_esm_masters(archive: Path, plugin_name: str, manager_dir: Path,
                         member_names: list[str]) -> tuple[list[str], str | None]:
    """Read TES3 MAST subrecords for one ESM inside a ZIP/7z/RAR without extracting the archive."""
    matches = [name for name in member_names
               if Path(name.replace("\\", "/")).name.casefold() == plugin_name.casefold()]
    if len(matches) != 1:
        return [], f"expected one {plugin_name} member in the archive, found {len(matches)}"
    member = matches[0]
    try:
        if archive.suffix.casefold() == ".zip":
            with zipfile.ZipFile(archive) as zf, zf.open(member) as stream:
                record = stream.read(16)
                if len(record) != 16:
                    return [], f"truncated TES3 header in {plugin_name}"
                size = struct.unpack_from("<I", record, 4)[0]
                if record[:4] != b"TES3" or size > 16 * 1024 * 1024:
                    return [], f"invalid or oversized TES3 header in {plugin_name}"
                body = stream.read(size)
                if len(body) != size:
                    return [], f"truncated TES3 header body in {plugin_name}"
        else:
            seven_zip = _seven_zip(manager_dir)
            if not seven_zip:
                return [], "7-Zip is required to inspect ESM masters in 7z/RAR archives"
            result = subprocess.run([seven_zip, "x", "-so", str(archive), member],
                                    capture_output=True, timeout=180, **_quiet_subprocess_options())
            if result.returncode:
                detail = result.stderr.decode("utf-8", "replace")[:500]
                return [], f"could not read {plugin_name} from archive: {detail or result.returncode}"
            data = result.stdout
            if len(data) < 16 or data[:4] != b"TES3":
                return [], f"invalid TES3 header in {plugin_name}"
            size = struct.unpack_from("<I", data, 4)[0]
            if size > 16 * 1024 * 1024 or len(data) < 16 + size:
                return [], f"truncated or oversized TES3 header in {plugin_name}"
            body = data[16:16 + size]
    except (OSError, zipfile.BadZipFile, KeyError, subprocess.SubprocessError, struct.error) as exc:
        return [], f"could not inspect {plugin_name}: {exc}"

    masters, offset = [], 0
    while offset < len(body):
        if offset + 8 > len(body):
            return [], f"malformed TES3 subrecord in {plugin_name}"
        tag = body[offset:offset + 4]
        subsize = struct.unpack_from("<I", body, offset + 4)[0]
        offset += 8
        end = offset + subsize
        if end > len(body):
            return [], f"TES3 subrecord exceeds the header in {plugin_name}"
        if tag == b"MAST":
            masters.append(body[offset:end].split(b"\0", 1)[0].decode("cp1252", "replace"))
        offset = end
    return masters, None


def prepare_core_master_review(manager_dir: str | Path, game_dir: str | Path,
                               plan: dict, progress=None) -> dict:
    """Read and validate archives off the Tk thread before presenting choices."""
    manager_dir, game_dir = Path(manager_dir), Path(game_dir)
    data_dir = game_dir / "Data Files"
    staging_dir = manager_dir / "staging"
    masters = configured_master_names(manager_dir)
    archive_by_master = {}
    for item in plan.get("archives", []):
        for name in item.get("core_masters", []):
            archive_by_master[name.casefold()] = item

    installed = {}
    for root in (data_dir, staging_dir):
        if root.is_dir():
            for path in root.rglob("*.esm"):
                if "off" in {part.casefold() for part in path.parts}:
                    continue
                installed.setdefault(path.name.casefold(), path)

    rows = []
    seven_zip = _seven_zip(manager_dir)
    archive_hashes, archive_members = {}, {}
    for index, master in enumerate(masters):
        if progress:
            progress(f"Inspecting master {index + 1} of {len(masters)}: {master}")
        existing = installed.get(master.casefold())
        item = archive_by_master.get(master.casefold())
        if existing:
            # Existing game and staging copies are both inspected before any selected install proceeds.
            try:
                with existing.open("rb") as stream:
                    record = stream.read(16)
                    if len(record) < 16 or record[:4] != b"TES3":
                        raise ValueError("invalid TES3 header")
                    size = struct.unpack_from("<I", record, 4)[0]
                    if size > 16 * 1024 * 1024:
                        raise ValueError("oversized TES3 header")
                    body = stream.read(size)
                if size > 16 * 1024 * 1024 or len(body) != size:
                    raise ValueError("truncated or oversized TES3 header")
                deps, offset = [], 0
                while offset < len(body):
                    if offset + 8 > len(body):
                        raise ValueError("malformed TES3 subrecord")
                    tag = body[offset:offset + 4]
                    subsize = struct.unpack_from("<I", body, offset + 4)[0]
                    offset += 8
                    end = offset + subsize
                    if end > len(body):
                        raise ValueError("TES3 subrecord exceeds the header")
                    if tag == b"MAST":
                        deps.append(body[offset:end].split(b"\0", 1)[0].decode("cp1252", "replace"))
                    offset = end
                already_installed = data_dir in existing.parents
                rows.append({"name": master, "deps": deps, "error": None, "installed": already_installed,
                             "selected": not already_installed, "path": str(existing),
                             "nexus": (item or {}).get("nexus", {}).get(master.casefold(), {}),
                             "page": nexus_page(master)})
            except (OSError, ValueError, struct.error) as exc:
                rows.append({"name": master, "deps": [], "error": f"staged plugin cannot be checked: {exc}",
                             "installed": False, "selected": False, "path": str(existing)})
            continue

        if not item:
            rows.append({"name": master, "deps": [], "error": "no downloaded archive contains this ESM",
                         "installed": False, "selected": False, "path": None,
                         "nexus": {}, "page": nexus_page(master)})
            continue
        if item.get("install_blocked") or item.get("inspection_error"):
            rows.append({"name": master, "deps": [], "error": "archive is blocked or could not be inspected",
                         "installed": False, "selected": False, "path": item.get("archive"),
                         "nexus": item.get("nexus", {}).get(master.casefold(), {}), "page": nexus_page(master)})
            continue
        archive = Path(item.get("archive", ""))
        if not archive.is_file():
            rows.append({"name": master, "deps": [], "error": "download archive is missing",
                         "installed": False, "selected": False, "path": str(archive),
                         "nexus": item.get("nexus", {}).get(master.casefold(), {}), "page": nexus_page(master)})
            continue
        archive_key = str(archive.resolve())
        if item.get("sha256") and archive_key not in archive_hashes:
            archive_hashes[archive_key] = _sha256(archive)
        if item.get("sha256") and archive_hashes[archive_key] != item["sha256"]:
            rows.append({"name": master, "deps": [], "error": "download changed since the archive scan; rescan required",
                         "installed": False, "selected": False, "path": str(archive),
                         "nexus": item.get("nexus", {}).get(master.casefold(), {}), "page": nexus_page(master)})
            continue
        if archive_key not in archive_members:
            archive_members[archive_key] = _members(archive, seven_zip)
        member_names, error = archive_members[archive_key]
        if error:
            rows.append({"name": master, "deps": [], "error": f"archive listing failed: {error}",
                         "installed": False, "selected": False, "path": str(archive),
                         "nexus": item.get("nexus", {}).get(master.casefold(), {}), "page": nexus_page(master)})
            continue
        deps, error = _archive_esm_masters(archive, master, manager_dir, member_names)
        rows.append({"name": master, "deps": deps, "error": error, "installed": False,
                     "selected": error is None, "path": str(archive),
                     "nexus": item.get("nexus", {}).get(master.casefold(), {}), "page": nexus_page(master)})

    index_by_name = {row["name"].casefold(): i for i, row in enumerate(rows)}
    installed_names = {name for name, path in installed.items() if data_dir in path.parents}
    staged_names = {name for name, path in installed.items() if staging_dir in path.parents}

    def select_dependencies(index: int, seen: set[int] | None = None) -> tuple[bool, str | None]:
        """Select every configured prerequisite before enabling a dependent master."""
        seen = set() if seen is None else seen
        if index in seen:
            return True, None
        seen.add(index)
        row = rows[index]
        if row["error"]:
            return False, row["error"]
        for dep in row["deps"]:
            key = dep.casefold()
            dep_index = index_by_name.get(key)
            if dep_index is not None:
                if dep_index >= index:
                    return False, f"{dep} is not earlier in the configured master order"
                required = rows[dep_index]
                if required["error"]:
                    return False, f"{dep} is unavailable: {required['error']}"
                if not required["installed"]:
                    ok, reason = select_dependencies(dep_index, seen)
                    if not ok:
                        return False, reason
                    required["selected"] = True
            elif key not in installed_names and key not in staged_names:
                return False, f"missing master {dep}"
        return True, None

    def required_by_selection() -> set[int]:
        """Return configured, uninstalled masters required by selected masters."""
        required: set[int] = set()

        def add_dependencies(index: int, seen: set[int]) -> None:
            if index in seen:
                return
            seen.add(index)
            for dep in rows[index]["deps"]:
                dep_index = index_by_name.get(dep.casefold())
                if dep_index is None:
                    continue
                if not rows[dep_index]["installed"]:
                    required.add(dep_index)
                add_dependencies(dep_index, seen)

        for index, row in enumerate(rows):
            if row["selected"] and not row["installed"]:
                add_dependencies(index, set())
        return required

    def readiness():
        for i, row in enumerate(rows):
            reasons = []
            if row["error"]:
                reasons.append(row["error"])
            for dep in row["deps"]:
                key = dep.casefold()
                dep_index = index_by_name.get(key)
                if dep_index is not None and dep_index >= i:
                    reasons.append(f"{dep} is not earlier in the configured master order")
                elif dep_index is not None:
                    required = rows[dep_index]
                    if not required["ready"]:
                        reasons.append(f"master {dep} has a missing dependency")
                    elif not required["installed"] and not required["selected"]:
                        reasons.append(f"depends on disabled master {dep}")
                elif key not in installed_names and key not in staged_names:
                    reasons.append(f"missing master {dep}")
            row["reasons"] = list(dict.fromkeys(reasons))
            row["ready"] = not reasons
            if not row["ready"]:
                row["selected"] = False

    readiness()
    return {"rows": rows, "index_by_name": index_by_name, "readiness": readiness,
            "select_dependencies": select_dependencies, "required_by_selection": required_by_selection}


def review_core_master_install(manager_dir: str | Path, game_dir: str | Path,
                               plan: dict, parent=None, *, prepared=None) -> list[str] | str | None:
    """Present already-inspected masters. Standalone callers may prepare inline."""
    prepared = prepared if prepared is not None else prepare_core_master_review(manager_dir, game_dir, plan)
    rows = prepared["rows"]
    index_by_name = prepared["index_by_name"]
    readiness = prepared["readiness"]
    select_dependencies = prepared["select_dependencies"]
    required_by_selection = prepared["required_by_selection"]
    try:
        import tkinter as tk
        from tkinter import ttk
        import mwmanager_dialogs as messagebox
        from mwmanager_theme import COLORS, FONTS, apply_theme, center
        root = tk.Toplevel(parent) if parent is not None else tk.Tk()
        if parent is None:
            apply_theme(root)                       # run on its own: no main window has themed it
        root.title("Review major masters")
        if parent is not None:
            root.transient(parent)
        center(root, parent, 850, 520)
        root.minsize(700, 380)
        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Every configured major master was checked before installation.",
                  font=FONTS["h2"]).pack(anchor="w")
        ttk.Label(outer, text="Masters are installed in the order shown. Selecting a master automatically selects "
                  "its configured prerequisite masters above it; those blue rows stay enabled until every dependent "
                  "master below them is disabled. Double-click a ready row or press Space to include or skip it. "
                  "Nexus versions are shown as Current or Outdated; missing or unavailable dependencies are red. "
                  "This review makes no file changes.",
                  wraplength=810).pack(anchor="w", pady=(4, 10))
        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(list_frame, columns=("use", "status", "requires"), show="tree headings")
        tree.heading("#0", text="Major master")
        tree.heading("use", text="Install")
        tree.heading("status", text="Status")
        tree.heading("requires", text="Missing / required masters")
        tree.column("#0", width=245, stretch=False)
        tree.column("use", width=75, anchor="center", stretch=False)
        tree.column("status", width=175, stretch=False)
        tree.column("requires", width=360)
        tree.tag_configure("ready", foreground=COLORS["green"])
        tree.tag_configure("missing", foreground=COLORS["red"])
        tree.tag_configure("disabled", foreground=COLORS["muted"])
        tree.tag_configure("installed", foreground=COLORS["green"])
        tree.tag_configure("required", foreground=COLORS["blue"])
        tree.tag_configure("outdated", foreground=COLORS["amber"])
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        ttk.Label(outer,
                  text="Blue — prerequisite master required by a later selected master; disable its dependents first.",
                  foreground=COLORS["blue"], wraplength=810).pack(anchor="w", pady=(6, 0))
        item_ids = [tree.insert("", "end", text=row["name"]) for row in rows]

        def refresh():
            readiness()
            required_indices = required_by_selection()
            for i, row in enumerate(rows):
                nexus_status = row.get("nexus", {}).get("status")
                version_status = "Outdated" if nexus_status == "outdated" else \
                                 "Current" if nexus_status == "current" else "Version unknown"
                if row["installed"]:
                    status, tag, use = "Installed; inspected", "installed", "—"
                elif not row["ready"]:
                    status, tag, use = "Missing master", "missing", "—"
                elif i in required_indices:
                    status, tag, use = f"Required — {version_status}", "required", "☑"
                elif row["selected"]:
                    status = f"Ready — {version_status}"
                    tag, use = ("outdated" if nexus_status == "outdated" else "ready"), "☑"
                else:
                    status, tag, use = "Ready (disabled)", "disabled", "☐"
                reasons = "; ".join(row["reasons"]) if row["reasons"] else ", ".join(row["deps"])
                nexus_detail = row.get("nexus", {}).get("detail")
                if nexus_detail:
                    reasons = "; ".join(filter(None, (reasons, nexus_detail)))
                tree.item(item_ids[i], text=row["name"], values=(use, status, reasons), tags=(tag,))

        def toggle(event=None):
            item_id = tree.focus()
            if item_id not in item_ids:
                return
            row_index = item_ids.index(item_id)
            row = rows[row_index]
            if not row["installed"]:
                if row["selected"]:
                    def depends_on(candidate_index, target_index, seen=None):
                        seen = set() if seen is None else seen
                        if candidate_index in seen:
                            return False
                        seen.add(candidate_index)
                        for dependency in rows[candidate_index]["deps"]:
                            dependency_index = index_by_name.get(dependency.casefold())
                            if dependency_index == target_index:
                                return True
                            if dependency_index is not None and depends_on(dependency_index, target_index, seen):
                                return True
                        return False

                    dependents = [candidate["name"] for i, candidate in enumerate(rows)
                                  if i != row_index and candidate["selected"] and not candidate["installed"]
                                  and depends_on(i, row_index)]
                    if dependents:
                        messagebox.showwarning(
                            "Master is still required",
                            f"Cannot disable {row['name']} while these enabled masters depend on it:\n\n"
                            + "\n".join(dependents)
                            + "\n\nDisable the dependent masters first, then try again.", parent=root)
                        return
                    row["selected"] = False
                else:
                    row["selected"] = True
                    ok, reason = select_dependencies(row_index)
                    if not ok:
                        row["selected"] = False
                        messagebox.showwarning(
                            "Cannot select master",
                            f"{row['name']} cannot be selected because a required master is unavailable:\n\n{reason}",
                            parent=root)
                refresh()

        tree.bind("<Double-1>", toggle)
        tree.bind("<space>", toggle)
        refresh()
        result = {"selected": None}
        buttons = ttk.Frame(outer)
        buttons.pack(side="bottom", fill="x", pady=(10, 0))

        def accept():
            readiness()
            selected = [row["name"] for row in rows if row["selected"] and not row["installed"]]
            if any(row["selected"] and not row["ready"] for row in rows):
                messagebox.showerror("Cannot continue", "A selected master has a missing dependency.", parent=root)
                return
            result["selected"] = selected
            root.destroy()

        def nexus_action():
            item_id = tree.focus()
            if item_id not in item_ids:
                return
            row = rows[item_ids.index(item_id)]
            if not row.get("page"):
                return
            try:
                import mwnexus_browser
                mwnexus_browser.open_page(row["page"], download_dir=plan.get("downloads_dir"))
                messagebox.showinfo(
                    "Download in the MWManager browser",
                    f"Download {row['name']} into:\n\n{plan.get('downloads_dir')}\n\n"
                    "Complete any Nexus security check, finish the download, close the browser, then click OK. "
                    "MWManager will rescan the archive folder.", parent=root)
                mwnexus_browser.close_browser()
            except (OSError, RuntimeError, ValueError) as exc:
                messagebox.showerror("Could not open Nexus", str(exc), parent=root)
                return
            result["selected"] = "rescan"
            root.destroy()

        action_button = ttk.Button(buttons, command=nexus_action)

        def update_action(_event=None):
            item_id = tree.focus()
            row = rows[item_ids.index(item_id)] if item_id in item_ids else None
            status = row.get("nexus", {}).get("status") if row else None
            if not row or row["installed"] or not row.get("page") or status == "current":
                action_button.configure(text="Download")
                action_button.state(["disabled"])
                return
            label = "[Update]" if status == "outdated" else \
                    "Download" if row.get("path") is None else "Check Nexus"
            action_button.configure(text=label)
            action_button.state(["!disabled"])

        tree.bind("<<TreeviewSelect>>", update_action, add="+")
        action_button.pack(side="left")
        update_action()
        ttk.Button(buttons, text="Confirm selected", command=accept).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Abort", command=root.destroy).pack(side="right")
        root.update_idletasks()
        # Position after Tk has measured the window; a bare geometry string
        # leaves Toplevels at the screen origin on some Windows configurations.
        width, height = root.winfo_width(), root.winfo_height()
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        y = max(0, (root.winfo_screenheight() - height) // 2)
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.update_idletasks()
        root.lift()
        root.attributes("-topmost", True)
        # Reapply after mapping: Windows can reset an owned Toplevel's position
        # when it becomes visible or changes topmost state.
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.after(300, lambda: root.attributes("-topmost", False))
        root.focus_force()
        if parent is not None:
            root.grab_set()
        if parent is not None:
            # The app already owns Tk's main loop. A nested mainloop would keep running
            # after this Toplevel closes because the main MWManager window still exists,
            # preventing the caller from starting the staging progress window.
            parent.wait_window(root)
        else:
            root.mainloop()
        return result["selected"]
    except Exception as exc:
        print(f"Could not open the major-master review window: {exc}")
        if not __import__("sys").stdin.isatty():
            return None
        print("Major-master preflight:")
        for row in rows:
            state = "READY" if row["ready"] else "MISSING: " + "; ".join(row["reasons"])
            print(f"  {row['name']}: {state}")
        print("Interactive selection requires the Tk GUI; aborting safely.")
        return None


_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10))}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _archive_path_key(name: str) -> str:
    """Windows-equivalent path key for collisions after extraction to the game staging folder."""
    parts = name.replace("\\", "/").split("/")
    if parts and parts[0].casefold() == "data files":
        parts = parts[1:]  # tidy flattens this common wrapper before staging
    return "/".join(unicodedata.normalize("NFKC", part).rstrip(" .").casefold() for part in parts)


def _member_preflight(names: list[str]) -> tuple[list[str], list[str]]:
    """Return unsafe path findings and duplicate/case-folded Windows path collisions."""
    unsafe, collisions = [], []
    seen: dict[str, str] = {}
    for raw in names:
        name = raw.replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        components = name.split("/")
        reason = None
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            reason = "absolute or drive-qualified path"
        elif any(part in (".", "..") for part in components):
            reason = "relative traversal component"
        elif any(part.casefold().startswith((".mwmanager", ".extract-", ".replaced-")) for part in components):
            reason = "reserved MWManager workflow metadata"
        elif any(not part or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                 or part.endswith((" ", ".")) for part in components):
            reason = "invalid Windows filename component"
        elif any(part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED for part in components):
            reason = "reserved Windows device name"
        elif len(components) > 32:
            reason = "excessively deep path"
        if reason:
            unsafe.append(f"{raw!r}: {reason}")
            continue
        key = _archive_path_key(name)
        previous = seen.get(key)
        if previous is not None:
            if previous != raw:
                collisions.append(f"{previous!r} collides with {raw!r} on Windows")
            else:
                collisions.append(f"duplicate archive entry {raw!r}")
        else:
            seen[key] = raw
    return unsafe, collisions


def _master_asset_overlaps(items: list[dict]) -> list[dict]:
    """Find candidate asset paths supplied by more than one selected major-master archive."""
    owners: dict[str, list[tuple[str, str]]] = {}
    for item in items:
        if not item.get("core_masters") or item.get("inspection_error") or item.get("install_blocked"):
            continue
        archive = Path(item["archive"]).name
        for name in item.get("_member_names", []):
            normalized = name.replace("\\", "/")
            parts = normalized.split("/")
            if parts and parts[0].casefold() == "data files":
                parts = parts[1:]
            if not parts or parts[0].casefold() not in {
                    "meshes", "textures", "icons", "sound", "music", "bookart", "splash", "fonts",
                    "video", "distantland", "mwse", "mge3", "shaders", "grass", "mcpatch"}:
                continue
            if Path(parts[-1]).suffix.casefold() in PLUGIN_EXTENSIONS | {".bsa"}:
                continue
            key = _archive_path_key("/".join(parts))
            if key:
                owners.setdefault(key, []).append((archive, name))
    return [{"normalized_path": key, "sources": [{"archive": archive, "path": name}
             for archive, name in values]}
            for key, values in sorted(owners.items()) if len({a for a, _ in values}) > 1]


def _archive_version(path: Path) -> tuple[int, ...] | None:
    """Read the version component from common Vortex/Nexus archive names."""
    stem = path.stem
    mod_id = re.search(r"(?<!\d)\d{4,7}(?!\d)", stem)
    if not mod_id:
        return None
    tail = stem[mod_id.end():].lstrip(" _-")
    # Nexus names commonly end in an upload epoch; Vortex's newer names use an ISO upload time.
    tail = re.sub(r"(?:^|[-_ ])\d{10,}(?:\s*\(\d+\))?$", "", tail)
    tail = re.sub(r"(?:^|\s)\d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z(?:\s.*)?$", "", tail)
    match = re.match(r"v?(\d+(?:[._-]\d+)*(?:[a-zA-Z]\d*)?)", tail, re.I)
    if not match:
        return None
    return tuple(int(n) for n in re.findall(r"\d+", match.group(1)))


def _archive_rank(path: Path) -> tuple:
    """Higher version first; use embedded upload time and file mtime to break ties."""
    version = _archive_version(path)
    version_key = tuple((version or ())[:8]) + (0,) * (8 - len(version or ()))
    stem = path.stem
    epoch = re.search(r"(?:^|[-_ ])(\d{10,})$", stem)
    upload_key = int(epoch.group(1)) if epoch else 0
    if not upload_key:
        iso = re.search(r"(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})Z", stem)
        if iso:
            upload_key = int("".join(iso.groups()))
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return (version is not None, version_key, upload_key, mtime, path.name.casefold())


def nexus_page(master: str) -> str | None:
    mod_id = NEXUS_MOD_IDS.get(master.casefold())
    return f"https://www.nexusmods.com/morrowind/mods/{mod_id}?tab=files" if mod_id else None


def _nexus_query(document: str, variables: dict, timeout: float = 8) -> dict:
    request = urllib.request.Request(
        NEXUS_GRAPHQL,
        data=json.dumps({"query": document, "variables": variables}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Application-Name": "MWManager",
                 "Application-Version": "0.1", "User-Agent": "MWManager/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("errors"):
        raise ValueError(payload["errors"][0].get("message", "Nexus query failed"))
    return payload.get("data") or {}


def _files_page_version(text: str) -> str | None:
    """Read the displayed mod version from the Files page header, before file-card versions and changelogs."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text).splitlines()]
    for index, line in enumerate(lines[:-1]):
        if line.casefold() == "version":
            match = re.fullmatch(r"v?(\d+(?:[._-]\d+)+(?:[a-z]\d*)?)", lines[index + 1], re.I)
            if match:
                return match.group(1)
    match = re.search(r"(?:^|\n)\s*Version\s+v?(\d+(?:[._-]\d+)+(?:[a-z]\d*)?)\s*(?:\n|$)",
                      str(text), re.I)
    return match.group(1) if match else None


def _scrape_nexus_versions(found: set[str], progress_callback, timeout: float) -> dict[str, str]:
    """Best-effort Files-page versions; use the visible browser only when Nexus blocks ordinary requests."""
    from mwnexus_parse import html_to_text, is_challenge
    versions, blocked = {}, []
    for master in sorted(found):
        mod_id = NEXUS_MOD_IDS[master]
        url = f"https://www.nexusmods.com/morrowind/mods/{mod_id}?tab=files"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "MWManager/0.1", "Accept": "text/html"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                markup = response.read(4 * 1024 * 1024).decode("utf-8", "replace")
            text = html_to_text(markup)
            if is_challenge("", text):
                raise RuntimeError("Nexus security check")
            version = _files_page_version(text)
            if not version:
                raise RuntimeError("displayed version not found")
            versions[master] = version
            progress_callback(f"Nexus Files page: {master} version {version}")
        except Exception as exc:
            blocked.append((master, mod_id, url, str(exc)))

    ask = getattr(progress_callback, "ask_yes_no", None)
    if not blocked or not ask:
        return versions
    import mwnexus_browser
    first_url = blocked[0][2]
    try:
        mwnexus_browser.open_page(first_url)
    except (OSError, RuntimeError, ValueError) as exc:
        progress_callback(f"Nexus browser unavailable; using API fallback ({exc})")
        return versions
    try:
        if not ask(
                "Continue the Nexus version check",
                "The Nexus page opened successfully. Nexus did not return readable version data to MWManager's "
                "background checker, so the signed-in browser session is needed instead.\n\n"
                "Complete any security check and wait for the Files page to load, then choose Yes. Keep the browser "
                "open: MWManager will inspect each relevant Files page, show progress, and close it when finished.\n\n"
                "Choose No to skip the browser check and use the Nexus API result instead."):
            return versions
        total = len(blocked)
        for index, (master, mod_id, url, _error) in enumerate(blocked, 1):
            try:
                progress_callback(f"Reading Nexus Files page {index} of {total}: {master}")
                page = mwnexus_browser.capture_page(str(mod_id), url=url)
                version = _files_page_version(page.get("text", ""))
                if version:
                    versions[master] = version
                    progress_callback(f"Nexus Files page: {master} version {version}")
            except (OSError, RuntimeError, ValueError) as exc:
                progress_callback(f"Could not read {master}'s Nexus Files page; using API fallback ({exc})")
    finally:
        mwnexus_browser.close_browser()
    return versions


def verify_nexus_versions(plan: dict, progress_callback=None, timeout: float = 8) -> dict:
    """Annotate found major-master archives with latest Nexus primary-file status."""
    progress_callback = progress_callback or (lambda _message: None)
    found = {master.casefold() for item in plan.get("archives", [])
             for master in item.get("core_masters", []) if master.casefold() in NEXUS_MOD_IDS}
    if not found:
        return plan
    page_versions = {}
    verified_at = _stage_time()
    query_error = None
    try:
        progress_callback(f"Checking {len(found)} found major-master archive(s) against Nexus…")
        game = _nexus_query('query { game(domainName: "morrowind") { id } }', {}, timeout)
        game_id = str(game["game"]["id"])
        aliases = " ".join(
            f'v{i}: mod(modId: "{NEXUS_MOD_IDS[name]}", gameId: $gameId) {{ version }} '
            f'm{i}: modFiles(modId: "{NEXUS_MOD_IDS[name]}", gameId: $gameId) '
            '{ name version date primary category }'
            for i, name in enumerate(sorted(found)))
        data = _nexus_query(f"query($gameId: ID!) {{ {aliases} }}", {"gameId": game_id}, timeout)
        latest_by_master = {}
        for i, master in enumerate(sorted(found)):
            files = data.get(f"m{i}") or []
            mod_version = str((data.get(f"v{i}") or {}).get("version") or "").strip()
            candidates = [item for item in files if item.get("primary")]
            if not candidates:
                candidates = [item for item in files if str(item.get("category", "")).upper() == "MAIN"]
            if files or mod_version:
                latest = dict(max(candidates or files, key=lambda item: int(item.get("date") or 0))) if files else {}
                latest["version"] = mod_version or latest.get("version")
                latest_by_master[master] = latest
    except Exception as exc:
        latest_by_master = {}
        query_error = f"Nexus API unavailable ({exc.__class__.__name__}: {exc})"

    missing = {master for master in found if master not in latest_by_master}
    if missing:
        progress_callback(f"Nexus API did not return {len(missing)} version(s); checking Files-page HTML…")
        page_versions = _scrape_nexus_versions(missing, progress_callback, timeout)

    for item in plan.get("archives", []):
        archive = Path(item["archive"])
        local_version = _archive_version(archive)
        epoch = re.search(r"(?:^|[-_ ])(\d{10,})(?:\s*\(\d+\))?$", archive.stem)
        statuses = {}
        for master in item.get("core_masters", []):
            latest = latest_by_master.get(master.casefold())
            page_version = page_versions.get(master.casefold())
            if not latest and not page_version:
                statuses[master.casefold()] = {"status": "unknown", "detail": query_error or
                                                "Nexus Files page did not expose a version",
                                                "page": nexus_page(master)}
                continue
            latest_text = page_version or str(latest.get("version") or latest.get("name") or "unknown")
            latest_version = tuple(int(value) for value in re.findall(r"\d+", latest_text))
            api_version = tuple(int(value) for value in re.findall(
                r"\d+", str((latest or {}).get("version") or "")))
            latest_date = int((latest or {}).get("date") or 0)
            if epoch and latest_date and (not page_version or not api_version or latest_version == api_version):
                status = "current" if int(epoch.group(1)) >= latest_date else "outdated"
            elif local_version and latest_version:
                status = "current" if local_version >= latest_version else "outdated"
            else:
                status = "unknown"
            detail = (f"Nexus latest: {latest_text}"
                      + (f"; archive version: {'.'.join(map(str, local_version))}" if local_version
                         else "; archive filename has no comparable version"))
            statuses[master.casefold()] = {"status": status, "latest": latest_text,
                                           "detail": detail, "page": nexus_page(master),
                                           "verified_at": verified_at}
        item["nexus"] = statuses
    return plan


def _stage_plugin_identity(item: dict, selected_masters: list[str]) -> dict:
    primary = selected_masters[0] if selected_masters else ""
    nexus = item.get("nexus", {}).get(primary.casefold(), {}) if primary else {}
    exact_current = nexus.get("latest") if nexus.get("status") == "current" else None
    return {
        "PLUGIN_NAME": Path(primary).stem if primary else Path(item["archive"]).stem,
        "PLUGIN_VERSION": str(exact_current or item.get("filename_version") or "unknown"),
        "NEXUS_VERSION": str(nexus.get("latest") or "unknown"),
        "NEXUS_VERSION_LAST_VERIFIED": nexus.get("verified_at"),
        "NEXUS_MOD_PAGE": nexus.get("page"),
    }


def _config_masters(manager_dir: Path) -> dict[str, tuple[str, ...]]:
    """The editable JSON is authoritative; the hardcoded list seeds it and is a fallback."""
    path = manager_dir / "mwmanager_core_addons.json"
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(user, dict):
            raise ValueError("expected a JSON object mapping master filenames to archive-name keywords")
        return {str(name).lower(): tuple(str(t).casefold() for t in
                ([tokens] if isinstance(tokens, str) else tokens)) for name, tokens in user.items()}
    except FileNotFoundError:
        return dict(BUILTIN_MASTERS)


def configured_master_names(manager_dir: str | os.PathLike) -> list[str]:
    """Configured major masters in user-defined load-order priority."""
    return list(_config_masters(Path(manager_dir)))


def edit_master_config(manager_dir: str | os.PathLike, *, add: str | None = None,
                       keywords: list[str] | None = None, remove: str | None = None) -> dict:
    """Add or remove a major master, preserving the configured priority order."""
    manager = Path(manager_dir)
    path = manager / "mwmanager_core_addons.json"
    current = _config_masters(manager)
    if add:
        name = Path(add).name
        if Path(name).suffix.lower() != ".esm":
            raise ValueError("major master filename must end in .esm")
        tokens = keywords or [Path(name).stem.replace("_", " ").casefold()]
        if name.lower() not in current:
            current[name.lower()] = tuple(t.casefold() for t in tokens)
    if remove:
        current.pop(Path(remove).name.lower(), None)
    manager.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({name: list(tokens) for name, tokens in current.items()},
                               indent=2) + "\n", encoding="utf-8")
    return current


def scan_downloads(downloads_dir: str | os.PathLike, manager_dir: str | os.PathLike,
                   output_dir: str | os.PathLike, *, dry_run: bool = False,
                   progress_callback=None) -> dict:
    downloads = Path(downloads_dir)
    manager = Path(manager_dir)
    output = Path(output_dir)
    masters = _config_masters(manager)
    master_names = {name.casefold() for name in masters}
    blacklist = _load_non_master_blacklist(manager)
    if not dry_run and not (manager / NON_MASTER_PLUGIN_BLACKLIST).exists():
        _save_non_master_blacklist(manager, blacklist)
    skipped_blacklisted = []
    newly_blacklisted_plugins = []
    newly_blacklisted_archives = []
    keywords = {token for tokens in masters.values() for token in tokens}
    seven_zip = _seven_zip(manager)
    print(f"\nCore add-on download scan: {downloads}")
    if not downloads.is_dir():
        print("  Downloads folder not found; set it in MWManager settings or pass --downloads-dir.")
        return {"downloads_dir": str(downloads), "archives": [], "missing_directory": True}
    found = []
    try:
        matching_candidates = sorted((p for p in downloads.iterdir()
                                       if p.is_file() and p.suffix.lower() in ARCHIVE_EXTENSIONS
                                       and any(k in p.name.casefold() for k in keywords)),
                                     key=lambda p: p.name.casefold())
    except OSError as exc:
        print(f"  Could not scan downloads: {exc}")
        return {"downloads_dir": str(downloads), "archives": [], "error": str(exc)}
    candidates = []
    for archive in matching_candidates:
        try:
            archive_stat = archive.stat()
            archive_key = str(archive.resolve()).casefold()
            cached = blacklist["archives"].get(archive_key, {})
            cached_plugins = {str(name).casefold() for name in cached.get("plugins", [])}
            if (cached.get("size_bytes") == archive_stat.st_size
                    and cached.get("mtime_ns") == archive_stat.st_mtime_ns
                    and not cached_plugins.intersection(master_names)):
                skipped_blacklisted.append(archive.name)
                continue
        except OSError:
            pass
        candidates.append(archive)
    if progress_callback:
        progress_callback(f"Found {len(matching_candidates)} archive name match(es); "
                          f"skipping {len(skipped_blacklisted)} cached non-master archive(s), "
                          f"inspecting {len(candidates)} remaining.")
    for index, archive in enumerate(candidates, 1):
        try:
            archive_stat = archive.stat()
            archive_key = str(archive.resolve()).casefold()
        except OSError:
            archive_stat = None
            archive_key = str(archive).casefold()
        if progress_callback:
            progress_callback(f"Inspecting archive {index} of {len(candidates)}: {archive.name}")
        names, error = _members(archive, seven_zip)
        plugins = sorted({Path(n.replace("\\", "/")).name for n in names
                          if Path(n.replace("\\", "/")).suffix.lower() in PLUGIN_EXTENSIONS}, key=str.casefold)
        bsas = sorted({Path(n.replace("\\", "/")).name for n in names
                       if Path(n.replace("\\", "/")).suffix.lower() == ".bsa"}, key=str.casefold)
        included = [p for p in plugins if p.lower() in masters]
        matched = [m for m in included]
        if not matched and not error and archive_stat is not None:
            # A complete plugin listing with no configured master is decisive. Cache the
            # archive plus every plugin name so future master-list additions invalidate the skip.
            non_master_plugins = [name for name in plugins if name.casefold() not in master_names]
            for name in non_master_plugins:
                key = name.casefold()
                previous = blacklist["plugins"].get(key, {})
                sources = set(previous.get("archives", []))
                sources.add(archive.name)
                blacklist["plugins"][key] = {
                    "filename": name,
                    "classification": "not in configured major-master list",
                    "archives": sorted(sources, key=str.casefold),
                }
            blacklist["archives"][archive_key] = {
                "archive": str(archive.resolve()),
                "size_bytes": archive_stat.st_size,
                "mtime_ns": archive_stat.st_mtime_ns,
                "plugins": plugins,
            }
            newly_blacklisted_plugins.extend(non_master_plugins)
            newly_blacklisted_archives.append(archive.name)
            if not dry_run:
                # Persist now, before hashing or inspecting later candidates, so interruption
                # cannot discard confirmed non-master findings.
                _save_non_master_blacklist(manager, blacklist)
        version = _archive_version(archive)
        if progress_callback:
            progress_callback(f"Checking archive fingerprint {index} of {len(candidates)}: {archive.name}")
        try:
            stat = archive.stat()
            fingerprint = _sha256(archive)
            size_bytes = stat.st_size
        except OSError as exc:
            fingerprint, size_bytes = None, None
            error = error or f"could not fingerprint archive: {exc}"
        unsafe, collisions = _member_preflight(names)
        record = {"archive": str(archive), "plugins": plugins, "bsas": bsas,
                  "core_masters": matched, "inspection_error": error,
                  "filename_version": ".".join(map(str, version)) if version else None,
                  "sha256": fingerprint, "size_bytes": size_bytes,
                  "member_count": len(names), "unsafe_paths": unsafe,
                  "windows_path_collisions": collisions,
                  "install_blocked": bool(unsafe or collisions or error),
                  "_member_names": names}
        found.append(record)

    # Keep only the latest archive for each exact plugin filename. A multi-plugin archive stays
    # selected if it is the newest package for at least one plugin it contains.
    winners: dict[str, dict] = {}
    for item in found:
        for plugin in item["plugins"]:
            key = plugin.casefold()
            current = winners.get(key)
            archive = Path(item["archive"])
            if current is None or _archive_rank(archive) > _archive_rank(Path(current["archive"])):
                winners[key] = item
    selected_paths = {item["archive"] for item in winners.values()}
    selected = [item for item in found if not item["plugins"] or item["archive"] in selected_paths]
    superseded = [item for item in found if item["plugins"] and item["archive"] not in selected_paths]
    for item in superseded:
        item["ignored_as_older_version"] = True
        item["superseded_by"] = sorted({plugin for plugin in item["plugins"]
                                       if winners[plugin.casefold()]["archive"] != item["archive"]},
                                      key=str.casefold)
    for item in selected:
        item["latest_plugins"] = sorted({plugin for plugin in item["plugins"]
                                        if winners[plugin.casefold()]["archive"] == item["archive"]},
                                       key=str.casefold)
        item["superseded_plugins"] = sorted({plugin for plugin in item["plugins"]
                                            if winners[plugin.casefold()]["archive"] != item["archive"]},
                                           key=str.casefold)
        archive = Path(item["archive"])
        matched = item["core_masters"]
        if matched or item["inspection_error"] or item["install_blocked"]:
            state = ", ".join(matched) if matched else (f"not inspected: {item['inspection_error']}"
                                                        if item["inspection_error"] else "no listed master")
            if item["install_blocked"]:
                state = "BLOCKED - " + state
            if item["filename_version"]:
                state += f" (version {item['filename_version']})"
            print(f"  {archive.name}: {state}")
            if item["plugins"]:
                print("    plugins: " + ", ".join(item["plugins"]))
            print(f"    archive SHA-256: {item['sha256'] or 'unavailable'} ({item['size_bytes'] or 0:,} bytes)")
            for problem in item["unsafe_paths"]:
                print(f"    BLOCKED unsafe archive path: {problem}")
            for problem in item["windows_path_collisions"]:
                print(f"    BLOCKED Windows path collision: {problem}")
            if item["bsas"]:
                print("    companion BSA(s) to register after installation: " + ", ".join(item["bsas"]))
    for item in superseded:
        winner_names = sorted({Path(winners[p.casefold()]["archive"]).name for p in item["plugins"]
                               if winners[p.casefold()]["archive"] != item["archive"]}, key=str.casefold)
        print(f"  Ignoring older plugin archive: {Path(item['archive']).name} (superseded by "
              f"{', '.join(winner_names)})")

    # The JSON insertion order is the user's major-master priority and is also the future install order.
    master_priority = {master.casefold(): index for index, master in enumerate(masters)}
    identified = [master for master in masters if master.casefold() in winners]
    identified_keys = {m.casefold() for m in identified}
    missing = [m for m in masters if m.casefold() not in identified_keys]
    blocked_masters = sorted({master for item in selected if item["install_blocked"]
                              for master in item["core_masters"]}, key=str.casefold)
    print(f"  Candidate archives inspected: {len(candidates)}; older plugin archives ignored: {len(superseded)}; "
          f"selected archives: {len(selected)}; core masters found: "
          f"{', '.join(identified) if identified else 'none'}")
    non_master = [Path(item["archive"]).name for item in selected
                  if not item["core_masters"] and not item["inspection_error"]]
    if non_master:
        print(f"  Related asset/patch archives also found: {len(non_master)} (kept in the plan for review)")
    if skipped_blacklisted:
        print(f"  Previously blacklisted non-master archives skipped: {len(skipped_blacklisted)}")
    if newly_blacklisted_plugins:
        names = sorted(set(newly_blacklisted_plugins), key=str.casefold)
        print("  Non-master plugins added to blacklist: " + ", ".join(names))
    if missing:
        print("  Core masters not found in inspected downloads: " + ", ".join(missing))
    if any(p.suffix.lower() in {".7z", ".rar"} for p in candidates) and not seven_zip:
        print("  Install 7-Zip or place 7z.exe in MWManager\\tools to inspect 7z/RAR contents.")
    asset_overlaps = _master_asset_overlaps(selected)
    for item in selected + superseded:
        item.pop("_member_names", None)
    if asset_overlaps:
        print(f"  Potential asset overwrites between selected core-master archives: {len(asset_overlaps)}")
        for overlap in asset_overlaps[:25]:
            print(f"    {overlap['normalized_path']}: " + "; ".join(
                f"{source['archive']} ({source['path']})" for source in overlap["sources"]))
        if len(asset_overlaps) > 25:
            print(f"    ... and {len(asset_overlaps) - 25} more")
    selected.sort(key=lambda item: (
        min((master_priority[m.casefold()] for m in item["core_masters"]), default=len(masters)),
        Path(item["archive"]).name.casefold()))
    result = {"downloads_dir": str(downloads), "archives": selected,
              "superseded_archives": superseded,
              "core_masters_found": identified, "core_masters_missing": missing,
              "ordered_install_masters": identified,
              "core_masters_blocked_for_review": blocked_masters,
              "master_archive_asset_overlaps": asset_overlaps,
              "install_order_policy": "use only the latest downloaded archive for each exact plugin filename; install selected core masters before dependent plugins",
              "archive_safety_policy": "archives that cannot be inspected, contain unsafe/traversal paths, or contain Windows path collisions are marked BLOCKED for review; SHA-256 is recorded for provenance",
              "non_master_blacklist": {"plugins_added": sorted(set(newly_blacklisted_plugins), key=str.casefold),
                                       "archives_added": sorted(set(newly_blacklisted_archives), key=str.casefold),
                                       "archives_skipped": skipped_blacklisted,
                                       "candidate_archives": len(matching_candidates),
                                       "archives_inspected": len(candidates)},
              "bsa_policy": "register companion BSAs in Morrowind.ini after install; inspect for vanilla-unsupported folders and repack when needed"}
    if not dry_run:
        output.mkdir(parents=True, exist_ok=True)
        target = output / "mwmanager_core_addon_plan.json"
        target.write_text(json.dumps(result, indent=2), encoding="utf-8")
        report = output / "mwmanager_core_addon_plan.txt"
        lines = ["MWManager core add-on download plan", f"Downloads: {downloads}",
                 "", "Core masters found:"]
        for master in identified:
            packages = [Path(item["archive"]).name for item in selected
                        if master.casefold() in {m.casefold() for m in item["core_masters"]}]
            lines.append(f"  {master}: " + "; ".join(packages))
        lines += ["", "Selected archive candidates:"]
        for item in selected:
            lines.append(f"  {Path(item['archive']).name}")
            if item["plugins"]:
                lines.append("    plugins: " + ", ".join(item["plugins"]))
                lines.append("    latest plugin copies: " + ", ".join(item["latest_plugins"]))
                if item["superseded_plugins"]:
                    lines.append("    older plugin copies to ignore: " + ", ".join(item["superseded_plugins"]))
            if item["bsas"]:
                lines.append("    BSA: " + ", ".join(item["bsas"]) +
                             " (inspect contents; remove vanilla-unsupported folders with BSAPack before registering)")
            lines.append(f"    SHA-256: {item['sha256'] or 'unavailable'}")
            lines.append(f"    member count: {item['member_count']}")
            if item["unsafe_paths"]:
                lines.append("    BLOCKED unsafe paths:")
                lines.extend("      " + problem for problem in item["unsafe_paths"])
            if item["windows_path_collisions"]:
                lines.append("    BLOCKED Windows path collisions:")
                lines.extend("      " + problem for problem in item["windows_path_collisions"])
            if item["inspection_error"]:
                lines.append("    inspection issue: " + item["inspection_error"])
        lines += ["", "Major masters blocked from installation pending review:"]
        lines.extend("  " + master for master in blocked_masters)
        lines += ["", "Non-master plugin blacklist:"]
        lines.append(f"  Candidate archives: {len(matching_candidates)}; inspected: {len(candidates)}; "
                     f"cached non-master archives skipped: {len(skipped_blacklisted)}")
        lines.extend("  Added plugin: " + name for name in sorted(set(newly_blacklisted_plugins), key=str.casefold))
        lines.extend("  Skipped cached archive: " + name for name in skipped_blacklisted)
        lines += ["", "Ignored older plugin archives:"]
        for item in superseded:
            lines.append(f"  {Path(item['archive']).name} (superseded plugin(s): "
                         + ", ".join(item["superseded_by"]) + ")")
        lines += ["", "Potential asset overlaps between selected core-master archives:"]
        for overlap in asset_overlaps:
            lines.append(f"  {overlap['normalized_path']}: " + "; ".join(
                f"{source['archive']} ({source['path']})" for source in overlap["sources"]))
        lines += ["", "Install policy: selected core masters and their required assets first; dependent plugins afterwards.",
                  "Archive safety: do not install packages marked BLOCKED until their unsafe paths or Windows path collisions are reviewed.",
                  "BSA policy: inspect archive contents; BSAs containing Sound, Fonts, Splash, Music, or other unsupported folders must be unpacked and repacked with those folders removed before registration."]
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  Plans saved to {target} and {report}")
    else:
        print("  Dry run: plan not written.")
    return result


def stage_core_master_archives(plan: dict, selected_masters: list[str], manager_dir: str | Path,
                               game_dir: str | Path, *, progress_callback=None,
                               clear_staging: bool = True) -> list[Path]:
    from workflow_safety import mutation_lock
    with mutation_lock(manager_dir, game_dir):
        return _stage_core_master_archives(plan, selected_masters, manager_dir, game_dir,
                                           progress_callback=progress_callback, clear_staging=clear_staging)


def _stage_core_master_archives(plan, selected_masters, manager_dir, game_dir, *,
                                progress_callback=None, clear_staging=True):
    """Extract the selected masters' latest archives into MWManager staging only.

    Nothing is copied or linked into the live game's Data Files directory here.
    Each archive gets an isolated folder under staging/major_masters.
    """
    manager, game = Path(manager_dir).resolve(), Path(game_dir).resolve()
    staging = manager / "staging" / "major_masters"
    if os.path.splitdrive(str(staging))[0].casefold() != os.path.splitdrive(str(game))[0].casefold():
        raise ValueError(f"Staging must be on the same drive as the game for eventual hardlinks: {staging}")
    staging.mkdir(parents=True, exist_ok=True)
    resolved_staging = staging.resolve()
    expected_parent = (manager / "staging").resolve()
    if staging.is_symlink() or resolved_staging.parent != expected_parent:
        raise ValueError(f"Refusing to clear an unexpected major-master staging path: {resolved_staging}")
    if clear_staging:
        from major_master_deploy import assert_package_disposable
        configured = [p.name for p in staging.rglob("*") if p.suffix.casefold() == ".esm"]
        for previous in staging.iterdir():
            if previous.is_dir() and not previous.name.startswith("."):
                assert_package_disposable(previous, staging, game / "Data Files", manager, configured)
        _preserve_only_major_master_fomod_selections(manager, staging)
    seven_zip = _seven_zip(manager)
    by_master = {name.casefold(): item for item in plan.get("archives", [])
                 for name in item.get("core_masters", [])}
    archives: dict[str, dict] = {}
    for master in selected_masters:
        item = by_master.get(master.casefold())
        if not item:
            raise ValueError(f"No latest inspected archive was selected for {master}.")
        archives[str(Path(item["archive"]).resolve()).casefold()] = item

    staged: list[Path] = []
    selected_priority = {name.casefold(): i for i, name in enumerate(selected_masters)}
    ordered_archives = sorted(archives.values(), key=lambda x: min(
            (selected_priority[name.casefold()] for name in item.get("core_masters", [])
             if name.casefold() in selected_priority), default=len(selected_masters)))
    # A Stage action starts fresh once. Subsequent selected archives in the same
    # action must accumulate beside the first so dependencies and options can see them.
    if clear_staging:
        if progress_callback:
            progress_callback("Clearing previous major-master staging before fresh extraction")
        for previous in staging.iterdir():
            if previous.is_dir() and not previous.is_symlink():
                shutil.rmtree(previous, onerror=_rmtree_clear_readonly)
            else:
                try:
                    os.chmod(previous, 0o666)
                except OSError:
                    pass
                previous.unlink()
    for index, item in enumerate(ordered_archives, 1):
        archive = Path(item["archive"]).resolve()
        if progress_callback:
            progress_callback(f"Preparing archive {index} of {len(ordered_archives)}: {archive.name}")
        if item.get("install_blocked") or item.get("inspection_error"):
            raise ValueError(f"Archive failed safety inspection: {archive.name}")
        if not archive.is_file() or (item.get("sha256") and _sha256(archive) != item["sha256"]):
            raise ValueError(f"Archive is missing or changed since scan; scan again: {archive.name}")
        names, error = _members(archive, seven_zip)
        if error:
            raise ValueError(f"Could not list {archive.name}: {error}")
        unsafe, collisions = _member_preflight(names)
        if unsafe or collisions:
            raise ValueError(f"Archive now contains unsafe or colliding paths: {archive.name}")

        safe_stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", archive.stem).strip(" .") or "major-master"
        destination = staging / safe_stem
        old_marker = _read_staged_archive_marker(destination)
        if old_marker and (old_marker.get("archive", "").casefold() != archive.name.casefold() or
                           (old_marker.get("archive_path") and
                            os.path.normcase(old_marker["archive_path"]) != os.path.normcase(str(archive)))):
            destination = staging / (safe_stem + "-" + hashlib.sha256(str(archive).encode()).hexdigest()[:12])
            old_marker = _read_staged_archive_marker(destination)
        package_masters = [name for name in selected_masters
                           if name.casefold() in {m.casefold() for m in item.get("core_masters", [])}]
        archive_hash = item.get("sha256") or _sha256(archive)
        prior = package_steps(destination) if destination.exists() else None
        same_identity = (old_marker.get("sha256") == archive_hash
                         and old_marker.get("processing_version") == PACKAGE_PROCESSING_VERSION
                         and {n.casefold() for n in old_marker.get("selected_masters", [])}
                         == {n.casefold() for n in package_masters})
        if prior and prior["extracted"]["completed_at"] and same_identity:
            # extracted by an earlier, interrupted run: the caller finishes its remaining steps
            staged.append(destination)
            if progress_callback:
                progress_callback(f"Archive {index} of {len(ordered_archives)} is already extracted: {archive.name}")
            continue
        published = destination
        if destination.exists():
            from major_master_deploy import assert_package_disposable
            configured = [p.name for p in staging.rglob("*") if p.suffix.casefold() == ".esm"]
            assert_package_disposable(destination, staging, game / "Data Files", manager, configured)
        import uuid
        destination = staging / (".extract-" + uuid.uuid4().hex)
        destination.mkdir(parents=True)
        # the marker is written first, so an extraction that is interrupted is on record and counted
        steps = {step: _step_blank() for step in PACKAGE_STEPS}
        if prior:
            steps["extracted"]["attempts"] = prior["extracted"]["attempts"]
        base_marker = {
            **_stage_plugin_identity(item, package_masters),
            "archive": archive.name, "sha256": archive_hash, "archive_path": str(archive),
            "processing_version": PACKAGE_PROCESSING_VERSION,
            "selected_masters": package_masters,
        }
        _write_stage_marker(destination, {**base_marker, "steps": steps})
        begin_package_step(destination, "extracted")
        try:
            if progress_callback:
                progress_callback(extraction_status(archive, index, len(ordered_archives)))
            if archive.suffix.casefold() == ".zip":
                with zipfile.ZipFile(archive) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        # Unix symlink entries are not valid game assets and must not be materialized.
                        if (info.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError(f"Archive contains a symbolic link: {info.filename}")
                        parts = info.filename.replace("\\", "/").split("/")
                        target = destination.joinpath(*parts).resolve()
                        if destination not in target.parents:
                            raise ValueError(f"Archive path escapes staging: {info.filename}")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as source, target.open("wb") as sink:
                            shutil.copyfileobj(source, sink, length=1024 * 1024)
            else:
                if not seven_zip:
                    raise ValueError("7-Zip is required to extract this archive.")
                import managed_processes
                result = managed_processes.run([seven_zip, "x", str(archive), f"-o{destination}", "-y"],
                                        progress=progress_callback, text=True, errors="replace", timeout=7200,
                                        **_quiet_subprocess_options())
                if result.returncode:
                    raise RuntimeError((result.stderr or result.stdout or
                                         f"7-Zip exited with code {result.returncode}")[-2000:])
            complete_package_step(destination, "extracted")
            if published.exists():
                # Keep the old package until the replacement is fully extracted.
                stale = staging / (".replaced-" + uuid.uuid4().hex)
                os.replace(published, stale)
                try:
                    os.replace(destination, published)
                except BaseException:
                    os.replace(stale, published)
                    raise
                shutil.rmtree(stale, onerror=_rmtree_clear_readonly)
            else:
                os.replace(destination, published)
            staged.append(published)
            if progress_callback:
                progress_callback(f"Staged archive {index} of {len(ordered_archives)}: {archive.name}")
        except Exception as exc:
            # nothing half-extracted is left behind; only the marker stays, with the attempt and its error
            failed = (package_steps(destination) or steps)["extracted"]
            failed.update(completed_at=None, last_error=str(exc)[:2000])
            shutil.rmtree(destination, ignore_errors=True)
            try:
                if not published.exists():
                    published.mkdir(parents=True, exist_ok=True)
                    _write_stage_marker(published, {**base_marker, "steps": {**steps, "extracted": failed}})
            except OSError:
                pass
            raise
    return staged


# What a staged package goes through, in order. Its marker (.mwmanager-stage.json) records each step the way the
# setup step file does - when it started, when it completed, how many attempts, the last error - so an interrupted
# Stage run is picked up at the package and step where it stopped.
PACKAGE_STEPS = ("extracted", "tidied", "bsa_checked")
PACKAGE_PROCESSING_VERSION = 1
PACKAGE_STEP_LABELS = {"extracted": "extracting the archive", "tidied": "Tidy and the option installer",
                       "bsa_checked": "the BSA check"}


def _stage_time() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _step_blank() -> dict:
    return {"started_at": None, "completed_at": None, "attempts": 0, "last_error": None, "outcome": None}


def _write_stage_marker(package: Path, data: dict) -> None:
    marker = Path(package) / ".mwmanager-stage.json"
    partial = marker.with_name(marker.name + ".tmp")
    partial.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, marker)


def package_steps(package: str | Path) -> dict | None:
    """{step: its record} for a staged package, or None when the folder has no marker at all.

    A marker from before steps were recorded was only written once extraction had finished, so it counts as
    extracted and nothing else."""
    package = Path(package)
    data = _read_staged_archive_marker(package)
    if not data:
        return None
    raw = data.get("steps")
    steps = {step: _step_blank() for step in PACKAGE_STEPS}
    if isinstance(raw, dict):
        for step in PACKAGE_STEPS:
            if isinstance(raw.get(step), dict):
                steps[step].update({key: raw[step].get(key, value) for key, value in _step_blank().items()})
    else:
        try:
            from datetime import datetime
            when = datetime.fromtimestamp((package / ".mwmanager-stage.json").stat().st_mtime).astimezone() \
                .isoformat(timespec="seconds")
        except OSError:
            when = "before step records"
        steps["extracted"].update(started_at=when, completed_at=when, attempts=1)
    return steps


def _update_package_step(package: str | Path, step: str, change) -> dict:
    if step not in PACKAGE_STEPS:
        raise ValueError(f"Unknown package step: {step}")
    package = Path(package)
    data = _read_staged_archive_marker(package)
    steps = package_steps(package)
    if not data or steps is None:
        raise ValueError(f"Not a staged package (no marker): {package}")
    change(steps[step])
    data["steps"] = steps
    _write_stage_marker(package, data)
    return steps[step]


def begin_package_step(package: str | Path, step: str) -> dict:
    return _update_package_step(package, step, lambda entry: entry.update(
        started_at=_stage_time(), completed_at=None, attempts=int(entry.get("attempts") or 0) + 1, last_error=None))


def complete_package_step(package: str | Path, step: str, *, outcome="completed") -> dict:
    def change(entry):
        entry.update(completed_at=_stage_time(), last_error=None, outcome=outcome)
        entry["started_at"] = entry.get("started_at") or entry["completed_at"]
        entry["attempts"] = max(int(entry.get("attempts") or 0), 1)
    return _update_package_step(package, step, change)


def fail_package_step(package: str | Path, step: str, error) -> dict:
    return _update_package_step(package, step, lambda entry: entry.update(last_error=str(error)[:2000]))


IMAGE_ASSET_DIRS = {"textures", "bookart", "icons", "splash"}     # where the game looks for images


def lua_texts(root):
    """Every Lua file of a package, lowercased and joined: enough to ask whether a script names a file."""
    chunks = []
    for dp, ds, fs in os.walk(root):
        ds[:] = [d for d in ds if d.casefold() not in {"off", "bugreports"}]
        for fn in fs:
            if fn.lower().endswith(".lua"):
                try:
                    with open(os.path.join(dp, fn), encoding="utf-8", errors="replace") as handle:
                        chunks.append(handle.read().casefold())
                except OSError:
                    pass
    return "\n".join(chunks)


def lua_used_images(root):
    """The PNG/JPEG files under the image folders that a Lua script names: they stay loose, so a BSA packer must
    leave them out. Paths relative to the package."""
    from major_master_deploy import _data_relative, SKIP_FOLDERS
    root = Path(root)
    text, used = None, []
    for dp, ds, fs in os.walk(root):
        ds[:] = [d for d in ds if d.casefold() not in SKIP_FOLDERS and not d.startswith(".")]
        rel = _data_relative(root, Path(dp))
        if not rel.parts or rel.parts[0].casefold() not in IMAGE_ASSET_DIRS:
            continue
        for fn in fs:
            if fn.lower().endswith((".png", ".jpg", ".jpeg")):
                text = lua_texts(root) if text is None else text
                if fn.casefold() in text:
                    used.append(os.path.relpath(os.path.join(dp, fn), root))
    return sorted(used, key=str.casefold)


def stamped_backup_path(path: str | Path, when: float | None = None) -> Path:
    """Where to keep a file that is about to be replaced: its name with the time appended
    (rock.dds -> rock.dds.20261116-101500), so a later backup never overwrites an earlier one.
    The caller records the returned path; nothing looks backups up by name."""
    import time
    path = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    candidate = path.with_name(f"{path.name}.{stamp}")
    n = 2
    while candidate.exists():                                   # two backups in the same second
        candidate = path.with_name(f"{path.name}.{stamp}-{n}")
        n += 1
    return candidate


def reset_partial_package(package: str | Path, *, manager_dir=None, game_dir=None, configured=()) -> None:
    if manager_dir is not None:
        from workflow_safety import mutation_lock
        from major_master_deploy import assert_package_disposable
        with mutation_lock(manager_dir, game_dir):
            assert_package_disposable(package, Path(package).parent, Path(game_dir) / "Data Files",
                                      manager_dir, list(configured))
            _reset_partial_package(package)
        return
    if Path(package).parent.name == "major_masters":
        raise ValueError("Resetting managed staging requires manager_dir and game_dir for ownership checks.")
    _reset_partial_package(package)


def _reset_partial_package(package):
    """Empty a package whose extraction never finished. Its marker stays, so the attempt count carries over to
    the next extraction; a folder that never had a marker is removed."""
    package = Path(package)
    data, steps = _read_staged_archive_marker(package), package_steps(package)
    shutil.rmtree(package, onerror=_rmtree_clear_readonly)
    if data and steps is not None:
        package.mkdir(parents=True, exist_ok=True)
        _write_stage_marker(package, {**data, "steps": steps})


def unfinished_packages(staging: str | Path) -> list[tuple[Path, list[str]]]:
    """(package, the steps it still needs, in order) for every staged package that is not finished.
    A folder with no marker needs every step."""
    staging = Path(staging)
    found = []
    if not staging.is_dir():
        return found
    for package in sorted((p for p in staging.iterdir() if p.is_dir() and not p.is_symlink()
                           and not p.name.startswith(".")),
                          key=lambda p: p.name.casefold()):
        steps = package_steps(package)
        missing = [step for step in PACKAGE_STEPS if steps is None or not steps[step]["completed_at"]]
        if missing:
            found.append((package, missing))
    return found


def _preserve_only_major_master_fomod_selections(manager: Path, staging: Path) -> None:
    """Keep only saved option keys for this staging set; discard runtime activation state."""
    journal = manager / "outputs" / "mwmanager_compatibility_journal.json"
    try:
        state = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    stage_root = staging.resolve()
    saved_selections = dict(state.get("saved_selections", {}))
    for old_key, entry in state.get("mods", {}).items():
        if not isinstance(entry, dict) or "selected" not in entry:
            continue
        raw_path = entry.get("mod_dir") or old_key
        try:
            mod_dir = Path(raw_path).resolve()
            relative = mod_dir.relative_to(stage_root)
        except (OSError, ValueError):
            continue
        # Each extracted archive occupies one direct child of major_masters.
        if len(relative.parts) != 1:
            continue
        marker = _read_staged_archive_marker(mod_dir)
        masters = sorted(Path(name).name.casefold() for name in marker.get("selected_masters", []) if name)
        identity = "masters:" + "|".join(masters) if masters else \
                   "sha256:" + str(marker.get("sha256", "")).casefold() if marker.get("sha256") else \
                   "package:" + mod_dir.name.casefold()
        saved_selections[identity] = sorted(set(entry["selected"]))
    cleaned = {"version": 1, "known_installed_plugins": [], "mods": {},
               "saved_selections": saved_selections}
    journal.parent.mkdir(parents=True, exist_ok=True)
    temp = journal.with_suffix(journal.suffix + ".tmp")
    temp.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, journal)


def staged_source_archive(package: str | Path) -> Path | None:
    """The archive a staged package came from: its marker keeps the file name, the folder is mwmanager.ini's
    [Paths] archive_downloads_dir (an absolute name in an older marker is used as it is). None when it is gone."""
    import configparser
    name = str(_read_staged_archive_marker(Path(package)).get("archive") or "").strip()
    if not name:
        return None
    candidate = Path(name)
    if not candidate.is_absolute():
        ini = configparser.ConfigParser(interpolation=None)
        try:
            ini.read(Path(__file__).resolve().with_name("mwmanager.ini"), encoding="utf-8")
        except (OSError, configparser.Error):
            return None
        paths = ini["Paths"] if ini.has_section("Paths") else {}
        folder = next((str(paths.get(key, "")).strip() for key in ("archive_downloads_dir", "downloads_dir")
                       if str(paths.get(key, "")).strip()), "")
        if not folder:
            return None
        candidate = Path(folder) / candidate.name
    return candidate if candidate.is_file() else None


def _read_staged_archive_marker(destination: Path) -> dict:
    """Read the archive marker, including copies moved by older tidy versions."""
    marker = destination / ".mwmanager-stage.json"
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        pass
    def onerror(_exc):
        return
    for base, _dirs, files in os.walk(destination, onerror=onerror):
        for name in files:
            if name.casefold() != ".mwmanager-stage.json":
                continue
            try:
                value = json.loads((Path(base) / name).read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            except (OSError, ValueError):
                continue
    return {}


def _rmtree_clear_readonly(func, path, _exc_info):
    """Retry removal of archive-created read-only paths on Windows."""
    if os.name == "nt":
        import ctypes
        get_attrs = ctypes.windll.kernel32.GetFileAttributesW
        set_attrs = ctypes.windll.kernel32.SetFileAttributesW
        get_attrs.argtypes = [ctypes.c_wchar_p]
        get_attrs.restype = ctypes.c_uint32
        set_attrs.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        set_attrs.restype = ctypes.c_int
        attrs = get_attrs(str(path))
        if attrs != 0xFFFFFFFF:
            set_attrs(str(path), attrs & ~0x1)
    else:
        os.chmod(path, 0o700)
    func(path)


def apply_plugin_replacement_overrides(trigger_plugin: str, data_dir: str | Path,
                                       manager_dir: str | Path, *, dry_run: bool = False) -> list[str]:
    """Move known replaced official add-ons into MWManager/Off when a replacement deploys.

    The move is deliberately hash-gated so an unrelated/user-modified file with the same
    filename is never relocated automatically. The deployment workflow should call this
    after the replacement master has been deployed and before enabling its plugin set.
    """
    manager, data = Path(manager_dir), Path(data_dir)
    config_path = manager / PLUGIN_OVERRIDE_CONFIG
    try:
        rules = json.loads(config_path.read_text(encoding="utf-8")).get("rules", [])
    except (OSError, ValueError, AttributeError):
        return [f"Override rule file unavailable or invalid: {config_path}"]

    messages = []
    journal = run = None
    trigger = Path(trigger_plugin).name.casefold()
    for rule in rules:
        if Path(str(rule.get("trigger_master", ""))).name.casefold() != trigger:
            continue
        filename = Path(str(rule.get("replaced_plugin", ""))).name
        if not filename or filename in (".", ".."):
            messages.append("Skipped an override rule with an invalid plugin filename.")
            continue
        source = data / filename
        if not source.is_file():
            continue
        actual_hash = _sha256(source)
        expected_hash = str(rule.get("expected_original_sha256", "")).casefold()
        if not expected_hash or actual_hash.casefold() != expected_hash:
            messages.append(f"Conflict: {filename} exists but does not match the recorded official copy; left in Data Files.")
            continue

        relative_off = Path(str(rule.get("off_path", "Off/Official Add-Ons")))
        if relative_off.is_absolute() or ".." in relative_off.parts:
            messages.append(f"Skipped unsafe Off destination in rule for {filename}.")
            continue
        destination = manager / relative_off / filename
        if destination.exists():
            if _sha256(destination) == actual_hash:
                messages.append(f"Already preserved at {destination}; source left in place for rollback safety.")
                continue
            messages.append(f"Conflict: {destination} already exists with different contents; left source untouched.")
            continue
        messages.append(f"Move {source} -> {destination}" if dry_run else f"Moved {filename} to {destination}")
        if not dry_run:
            if journal is None:
                import mwmanager_journal
                journal = mwmanager_journal.Journal(manager / "journal")
                if not journal.has_baseline():
                    messages[-1] = f"Skipped {filename}: no install baseline exists, so the original cannot be journaled."
                    journal = None
                    continue
                run = journal.start_run("core_addons.py", {"trigger_plugin": trigger_plugin})
            journal.move_original(data.parent, source, destination, run["run"], tag="official-add-on-plugin")
    if run is not None:
        journal.finish_run(run)
    return messages
