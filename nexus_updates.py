"""User-approved Nexus update checks for Vortex-staged Morrowind mods.

This module only checks a mod when its Vortex meta.ini supplies Nexus IDs. Downloads are
started only after the user chooses Update in the dialog. The API key stays on this machine.
"""
from __future__ import annotations

import configparser
import ctypes
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
import tkinter as tk

APP_NAME = "MWManager"
APP_VERSION = "0.1.0"
API_ROOT = "https://api.nexusmods.com/v1"
GAME_DOMAIN = "morrowind"
MAX_CHANGELOG_CHARS = 16000
_SESSION_KEY = ""
_KEY_PROMPTED = False


def _key_file(manager_dir: Path) -> Path:
    return manager_dir / "outputs" / "nexus_api_key.dpapi"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise OSError("Windows DPAPI is unavailable")
    source_buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), source_buf)
    result = _Blob()
    if protect:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "MWManager Nexus API key",
                                                   None, None, None, 0x1, ctypes.byref(result))
    else:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None,
                                                       None, 0x1, ctypes.byref(result))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _read_key(manager_dir: Path) -> str:
    env_key = os.environ.get("MWMANAGER_NEXUS_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return _dpapi(_key_file(manager_dir).read_bytes(), protect=False).decode("utf-8").strip()
    except (OSError, UnicodeError, ValueError):
        return ""


def _ask_key(manager_dir: Path) -> str:
    global _SESSION_KEY, _KEY_PROMPTED
    _KEY_PROMPTED = True
    key = simpledialog.askstring(
        "Nexus Mods API key",
        "MWManager can check this Nexus-linked mod for updates. Enter your personal Nexus API key. "
        "It will be encrypted for this Windows account and used only when you start a mod install.",
        show="*",
    )
    key = (key or "").strip()
    if not key:
        return ""
    _SESSION_KEY = key
    try:
        target = _key_file(manager_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_dpapi(key.encode("utf-8"), protect=True))
    except (OSError, ValueError):
        messagebox.showwarning("API key not saved", "The key will be used for this check only.")
    return key


def _metadata(mod_dir: Path) -> dict | None:
    candidates = [mod_dir / "meta.ini"]
    candidates.extend(p for p in mod_dir.glob("*.meta.ini") if p.is_file())
    for path in candidates:
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(path, encoding="utf-8-sig")
        except (OSError, configparser.Error, UnicodeError):
            continue
        general = parser["General"] if parser.has_section("General") else parser.defaults()
        mod_id = str(general.get("modid", general.get("nexusid", ""))).strip().strip('"')
        if not mod_id.isdigit():
            source = general.get("source", "")
            mod_id = (re.search(r"/mods/(\d+)", source) or re.search(r"modid[=:](\d+)", source, re.I))
            mod_id = mod_id.group(1) if mod_id else ""
        if not mod_id:
            continue
        file_ids = set()
        direct = str(general.get("fileid", "")).strip().strip('"')
        if direct.isdigit():
            file_ids.add(direct)
        if parser.has_section("installedFiles"):
            for name, value in parser.items("installedFiles"):
                if name.casefold().endswith("fileid") and value.strip().strip('"').isdigit():
                    file_ids.add(value.strip().strip('"'))
        version = str(general.get("version", "")).strip().strip('"')
        name = str(general.get("name", mod_dir.name)).strip().strip('"') or mod_dir.name
        return {"mod_id": mod_id, "file_ids": file_ids, "version": version, "name": name}
    return None


def _api_json(url: str, key: str, *, timeout: int = 25):
    class NexusRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            parsed = urllib.parse.urlsplit(newurl)
            if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "api.nexusmods.com":
                raise urllib.error.HTTPError(newurl, code, "Refused cross-host Nexus API redirect", headers, fp)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    request = urllib.request.Request(url, headers={
        "apikey": key,
        "Application-Name": APP_NAME,
        "Application-Version": APP_VERSION,
        "Accept": "application/json",
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
    })
    opener = urllib.request.build_opener(NexusRedirect())
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _plain_text(value) -> str:
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value)
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _timestamp(item: dict) -> float:
    for key in ("uploaded_timestamp", "uploaded_time", "uploaded_at"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).timestamp()
            except ValueError:
                continue
    return 0.0


def _mod_files(mod_id: str, key: str) -> list[dict]:
    data = _api_json(f"{API_ROOT}/games/{GAME_DOMAIN}/mods/{mod_id}/files.json", key)
    return data if isinstance(data, list) else data.get("files", [])


def _changelog(mod_id: str, key: str, candidate: dict) -> str:
    try:
        data = _api_json(f"{API_ROOT}/games/{GAME_DOMAIN}/mods/{mod_id}/changelogs.json", key)
    except (OSError, ValueError, urllib.error.URLError):
        return _plain_text(candidate.get("description", candidate.get("changelog", "")))[:MAX_CHANGELOG_CHARS]
    entries = data if isinstance(data, list) else data.get("changelogs", [])
    file_id = str(candidate.get("file_id", candidate.get("fileid", "")))
    version = str(candidate.get("version", ""))
    matched = []
    for entry in entries:
        entry_id = str(entry.get("file_id", entry.get("fileid", "")))
        entry_version = str(entry.get("version", ""))
        if (file_id and entry_id == file_id) or (version and entry_version == version):
            matched.append(entry)
    if not matched:
        matched = [candidate] if candidate.get("description") or candidate.get("changelog") else []
    lines = []
    for item in matched:
        text = _plain_text(item.get("changelog", item.get("description", item.get("text", ""))))
        if text:
            lines.append(text)
    return "\n\n".join(lines)[:MAX_CHANGELOG_CHARS]


def _download_url(mod_id: str, file_id: str, key: str) -> str:
    data = _api_json(f"{API_ROOT}/games/{GAME_DOMAIN}/mods/{mod_id}/files/{file_id}/download_link.json", key)
    links = data if isinstance(data, list) else data.get("download_links", data.get("links", []))
    if isinstance(links, dict):
        links = [links]
    for entry in links:
        url = entry.get("URI", entry.get("uri", entry.get("url", "")))
        if str(url).startswith("https://"):
            return str(url)
    raise ValueError("Nexus returned no authorized HTTPS download link for this file")


def _safe_filename(value: str, mod_id: str, file_id: str) -> str:
    value = urllib.parse.unquote(Path(value.replace("\\", "/")).name)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not value or value in {".", ".."}:
        return f"MWManager-update-{mod_id}-{file_id}.zip"
    return value


def _save_download(url: str, filename: str, downloads_dir: Path) -> Path:
    downloads_dir.mkdir(parents=True, exist_ok=True)
    destination = downloads_dir / filename
    suffix = 1
    while destination.exists():
        suffix += 1
        marker = " (MWManager update)" if suffix == 2 else f" (MWManager update {suffix})"
        destination = destination.with_name(f"{Path(filename).stem}{marker}{Path(filename).suffix}")
    temporary = destination.with_name(destination.name + ".mwmanager-download")
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            if response.geturl().split(":", 1)[0].casefold() != "https":
                raise ValueError("Nexus download redirected to a non-HTTPS URL")
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
        if temporary.stat().st_size == 0:
            raise ValueError("Nexus returned an empty download")
        os.replace(temporary, destination)
        return destination
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _existing_download(item: dict, downloads_dir: Path, mod_id: str) -> Path | None:
    filename = str(item.get("file_name", item.get("name", "")))
    expected = Path(filename.replace("\\", "/")).name.casefold()
    file_id = _file_id(item)
    try:
        files = [path for path in downloads_dir.iterdir() if path.is_file()]
    except OSError:
        return None
    for path in files:
        if expected and path.name.casefold() == expected:
            return path
    for path in files:
        tokens = re.findall(r"(?<!\d)\d{5,}(?!\d)", path.name)
        if file_id and file_id in tokens and mod_id in tokens:
            return path
    return None


def _offer(mod: dict, candidate: dict, changelog: str,
           downloads_dir: Path, api_key: str) -> bool:
    """Show details and return True after an approved update was downloaded."""
    root = tk.Tk()
    root.withdraw()
    try:
        dialog = tk.Toplevel(root)
        dialog.title(f"MWManager update — {mod['name']}")
        dialog.geometry("680x500" if changelog else "560x190")
        dialog.transient(root)
        dialog.grab_set()
        title = candidate.get("name") or candidate.get("file_name") or "Latest Nexus file"
        version = candidate.get("version", "")
        ttk.Label(dialog, text=f"Update available for {mod['name']}",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        ttk.Label(dialog, text=f"Current: {mod.get('version') or ', '.join(sorted(mod['file_ids'])) or 'unknown'}    "
                  f"Available: {version or title}", wraplength=640).pack(anchor="w", padx=12, pady=3)
        if changelog:
            ttk.Label(dialog, text="Changelog").pack(anchor="w", padx=12, pady=(8, 2))
            text = tk.Text(dialog, wrap="word", height=17)
            text.pack(fill="both", expand=True, padx=12, pady=(0, 8))
            text.insert("1.0", changelog)
            text.configure(state="disabled")
        result = {"downloaded": False}
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=(0, 12))

        def update():
            try:
                local = candidate.get("_mwmanager_local_path")
                if local and Path(local).is_file():
                    result["downloaded"] = True
                    messagebox.showinfo("Update already downloaded", f"Found the update in:\n{local}\n\n"
                                        "Re-stage this archive before continuing with the old package.", parent=dialog)
                    dialog.destroy()
                    return
                file_id = str(candidate.get("file_id", candidate.get("fileid", "")))
                url = _download_url(mod["mod_id"], file_id, api_key)
                name = _safe_filename(str(candidate.get("file_name", candidate.get("name", ""))),
                                      mod["mod_id"], file_id)
                path = _save_download(url, name, downloads_dir)
                result["downloaded"] = True
                messagebox.showinfo("Update downloaded", f"Saved to:\n{path}\n\n"
                                    "Re-stage this archive before continuing with the old package.", parent=dialog)
                dialog.destroy()
            except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
                messagebox.showerror("Update download failed", str(exc), parent=dialog)

        ttk.Button(buttons, text="Ignore", command=dialog.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Download update", command=update).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        root.wait_window(dialog)
        return result["downloaded"]
    finally:
        root.destroy()


def _file_id(item: dict) -> str:
    value = item.get("file_id", item.get("fileid", ""))
    return str(value) if str(value).isdigit() else ""


def _version_key(value: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:[._-]\d+)+", value or "")
    return tuple(int(part) for part in re.findall(r"\d+", match.group(0))) if match else ()


def _targeted_patch_files(files: list[dict], target_version: str, downloads_dir: Path,
                          mod_id: str) -> list[dict]:
    target = _version_key(target_version)
    if not target:
        return []
    matched = []
    patch_terms = re.compile(r"\b(?:patch(?:es)?|hot[ -]?fix(?:es)?|bug[ -]?fix(?:es)?|fix(?:es)?)\b", re.I)
    version_pattern = re.compile(r"\d+(?:[._-]\d+)+")
    for item in files:
        category = str(item.get("category_name", "")).casefold()
        file_id = _file_id(item)
        if not file_id or category not in {"optional", "update"}:
            continue
        name = str(item.get("file_name", item.get("name", "")))
        description = _plain_text(item.get("description", item.get("changelog", "")))
        text = f"{name} {item.get('version', '')} {description}"
        if not patch_terms.search(text):
            continue
        version_mentions = {_version_key(match.group(0)) for match in version_pattern.finditer(text)}
        if target not in version_mentions:
            continue
        item = dict(item)
        item["_mwmanager_description"] = description
        local = _existing_download(item, downloads_dir, mod_id)
        if local:
            item["_mwmanager_local_path"] = str(local)
        matched.append(item)
    return sorted(matched, key=lambda item: (_timestamp(item), str(item.get("file_name", "")).casefold()))


def _download_selected_files(mod: dict, items: list[dict], downloads_dir: Path, api_key: str) -> list[Path]:
    downloaded = []
    for item in items:
        local = item.get("_mwmanager_local_path")
        if local and Path(local).is_file():
            downloaded.append(Path(local))
            continue
        file_id = _file_id(item)
        url = _download_url(mod["mod_id"], file_id, api_key)
        name = _safe_filename(str(item.get("file_name", item.get("name", ""))), mod["mod_id"], file_id)
        downloaded.append(_save_download(url, name, downloads_dir))
    return downloaded


def _offer_targeted_patches(mod: dict, files: list[dict], target_version: str,
                            downloads_dir: Path, api_key: str) -> bool:
    candidates = _targeted_patch_files(files, target_version, downloads_dir, mod["mod_id"])
    if not candidates:
        return False
    root = tk.Tk()
    root.withdraw()
    try:
        dialog = tk.Toplevel(root)
        dialog.title(f"Compatible patches — {mod['name']}")
        dialog.geometry("740x440")
        dialog.transient(root)
        dialog.grab_set()
        ttk.Label(dialog, text=f"Nexus lists optional patches or hotfixes for {mod['name']} {target_version}.",
                  wraplength=700, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 8))
        ttk.Label(dialog, text="Select any files to include with this version. MWManager will download them into the Vortex downloads folder.",
                  wraplength=700).pack(anchor="w", padx=12, pady=(0, 8))
        body = ttk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=12)
        canvas = tk.Canvas(body, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        variables = []
        for item in candidates:
            variable = tk.BooleanVar(value=True)
            variables.append(variable)
            name = str(item.get("file_name", item.get("name", "Nexus optional file")))
            desc = item.get("_mwmanager_description", "")
            state = "already downloaded" if item.get("_mwmanager_local_path") else "available to download"
            ttk.Checkbutton(inner, text=f"{name}  ({item.get('version', 'version not listed')}; {state})",
                            variable=variable).pack(anchor="w", pady=(4, 0))
            if desc:
                ttk.Label(inner, text=desc[:1200], wraplength=670).pack(anchor="w", padx=(24, 0), pady=(0, 4))
        result = {"downloaded": False}
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=12)

        def include():
            selected = [item for item, variable in zip(candidates, variables) if variable.get()]
            if not selected:
                dialog.destroy()
                return
            try:
                paths = _download_selected_files(mod, selected, downloads_dir, api_key)
                result["downloaded"] = bool(paths)
                messagebox.showinfo("Optional files ready", "Available in the Vortex downloads folder:\n\n"
                                    + "\n".join(str(path) for path in paths)
                                    + "\n\nRe-stage these files with the mod before continuing.", parent=dialog)
                dialog.destroy()
            except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
                messagebox.showerror("Optional file download failed", str(exc), parent=dialog)

        ttk.Button(buttons, text="Ignore", command=dialog.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Include selected", command=include).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        root.wait_window(dialog)
        return result["downloaded"]
    finally:
        root.destroy()


def check_and_offer_update(mod_dir: str | os.PathLike, *, manager_dir: str | os.PathLike,
                           downloads_dir: str | os.PathLike | None = None) -> bool:
    """Check a staged Vortex mod before processing; return True if user downloaded an update.

    No API key or Nexus identity means no network lookup. A downloaded update is left in the
    configured Vortex downloads folder; the stale extracted package must not be used afterward.
    """
    global _SESSION_KEY
    mod_dir, manager_dir = Path(mod_dir), Path(manager_dir)
    mod = _metadata(mod_dir)
    if not mod:
        return False
    key = _SESSION_KEY or _read_key(manager_dir)
    if not key:
        if _KEY_PROMPTED:
            return False
        key = _ask_key(manager_dir)
    if not key:
        return False
    if downloads_dir is None:
        settings_path = manager_dir / "mwmanager_gui_settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        downloads_dir = settings.get("downloads_dir") or os.environ.get("MWMANAGER_DOWNLOADS_DIR")
        if not downloads_dir:
            try:
                import mwdialog_paths
                downloads_dir = mwdialog_paths.resolve_downloads(required=False)
            except Exception:
                downloads_dir = None
        if not downloads_dir:
            print(f"  Nexus update skipped for {mod['name']}: no Vortex download folder is set "
                  f"(set it in Options, or MWMANAGER_DOWNLOADS_DIR).")
            return False
    downloads_dir = Path(downloads_dir)
    try:
        files = _mod_files(mod["mod_id"], key)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"  Nexus update check skipped for {mod['name']}: {exc}")
        return False
    main_files = [item for item in files if str(item.get("category_name", "")).casefold() == "main"
                  and _file_id(item)]
    if not main_files:
        main_files = [item for item in files if _file_id(item)]
    if not main_files:
        return False
    candidate = max(main_files, key=_timestamp)
    file_id = _file_id(candidate)
    candidate_version = str(candidate.get("version", "")).strip()
    update_available = bool(file_id and file_id not in mod["file_ids"])
    if not mod["file_ids"]:
        current_parts = _version_key(mod["version"])
        candidate_parts = _version_key(candidate_version)
        update_available = bool(current_parts and candidate_parts and candidate_parts > current_parts)
    updated = False
    target_version = mod["version"]
    if update_available:
        local = _existing_download(candidate, downloads_dir, mod["mod_id"])
        if local:
            candidate = dict(candidate)
            candidate["_mwmanager_local_path"] = str(local)
        changelog = _changelog(mod["mod_id"], key, candidate)
        updated = _offer(mod, candidate, changelog, downloads_dir, key)
        if updated:
            target_version = candidate_version or mod["version"]
    patches_added = _offer_targeted_patches(mod, files, target_version, downloads_dir, key) \
        if target_version else False
    return updated or patches_added
