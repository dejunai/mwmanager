"""Copy Tamriel Data's Translation.txt into the tools folder when that master is staged.

Tamriel Data ships the file (Docs\\Translation.txt). The tools use it as a rename
map (old object IDs to current ones) during --fix. It is not bundled or committed:
when a staged package contains the file, it is copied next to the other tools
data, and a sidecar records the source archive's version and hash. A newer
archive (different version or hash) refreshes the copy. If the file's contents
changed, a --full rebuild is queued so generated patches pick up the new IDs.

The file is optional. Missing, unreadable, or unrecognised contents are skipped
with a plain warning; staging and --fix go on.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

FILE_NAME = "Translation.txt"
# Job-window lines with this prefix go only to outputs\\mwmanager_status_detail.log (mwmanager_workflow.UserOutput
# drops them); the window gets one plain line instead, under 88201bb's plain-status rules.
DETAIL_PREFIX = "MWMANAGER-DETAIL: "


def _report(progress_callback, plain: str | None, detail: str | None = None) -> None:
    """Send the full detail to the detail log only, and at most one plain line to the job window."""
    if not progress_callback:
        return
    if detail:
        progress_callback(DETAIL_PREFIX + "Translation.txt: " + detail)
    if plain:
        progress_callback(plain)
META_NAME = "Translation.meta.json"
PENDING_NAME = "mwmanager_translation_rebuild.json"
STAGE_MARKER = ".mwmanager-stage.json"

# Exact names, plus the older "Tamriel Data (HD) - Translation.txt" form.
_NAME_KEYS = frozenset({"translation.txt", "translations.txt"})
_NAME_SUFFIXES = (" - translation.txt", " - translations.txt")

# Translation.txt ships in Tamriel Data, not Tamriel Rebuilt, but a TR update
# often arrives with a new Tamriel Data and should still trigger a refresh scan.
TR_MASTER_KEYS = frozenset({"tr_mainland.esm"})
TD_MASTER_KEYS = frozenset({"tamriel_data.esm"})


def default_tools_dir(script_dir: str | Path | None = None) -> Path:
    """<script folder>\\tools, the same place mwdialog_patchfile.TD_MAP reads."""
    base = Path(script_dir) if script_dir is not None else Path(__file__).resolve().parent
    return base / "tools"


def translation_path(tools_dir: str | Path) -> Path:
    return Path(tools_dir) / FILE_NAME


def meta_path(tools_dir: str | Path) -> Path:
    return Path(tools_dir) / META_NAME


def pending_path(manager_dir: str | Path) -> Path:
    return Path(manager_dir) / PENDING_NAME


def is_translation_filename(name: str) -> bool:
    key = Path(name).name.casefold()
    return key in _NAME_KEYS or any(key.endswith(suffix) for suffix in _NAME_SUFFIXES)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def looks_like_rename_map(path: str | Path) -> bool:
    """At least one 'old:new' line. A real Tamriel Data map is huge; tests use a few lines."""
    try:
        text = Path(path).read_text(encoding="cp1252", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        old, sep, new = line.strip().partition(":")
        if sep and old.strip() and new.strip():
            return True
    return False


def find_translation(package: str | Path) -> Path | None:
    """The Translation.txt inside a staged package, or None.

    Prefers a Docs folder and an exact Translation.txt name. Never leaves the
    package, and ignores the staging marker file.
    """
    root = Path(package)
    if not root.is_dir():
        return None
    root = root.resolve()
    found: list[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Keep the walk inside the package even if a child is a symlink.
            current = Path(dirpath)
            try:
                current.resolve().relative_to(root)
            except ValueError:
                dirnames[:] = []
                continue
            dirnames[:] = [name for name in dirnames if name not in {".", ".."}]
            for name in filenames:
                if name == STAGE_MARKER or not is_translation_filename(name):
                    continue
                path = current / name
                try:
                    resolved = path.resolve()
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    continue
                if resolved.is_file():
                    found.append(resolved)
    except OSError:
        return None
    if not found:
        return None

    def sort_key(path: Path) -> tuple:
        parts = [part.casefold() for part in path.relative_to(root).parts]
        in_docs = any(part == "docs" for part in parts)
        exact = path.name.casefold() == FILE_NAME.casefold()
        return (0 if in_docs else 1, 0 if exact else 1, len(parts), str(path).casefold())

    return sorted(found, key=sort_key)[0]


def read_meta(tools_dir: str | Path) -> dict:
    path = meta_path(tools_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_pending(manager_dir: str | Path) -> dict:
    path = pending_path(manager_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def rebuild_is_pending(manager_dir: str | Path) -> bool:
    pending = read_pending(manager_dir)
    return bool(pending.get("pending") and pending.get("action"))


def clear_rebuild(manager_dir: str | Path) -> None:
    """Forget a queued --full after a successful Generate Patches --full run."""
    path = pending_path(manager_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        try:
            path.write_text(json.dumps({"pending": False}, indent=2) + "\n", encoding="utf-8")
        except OSError:
            return


def pending_alert(manager_dir: str | Path) -> str | None:
    """The alert to show the user, or None when nothing is waiting."""
    pending = read_pending(manager_dir)
    if not pending.get("pending"):
        return None
    text = pending.get("alert")
    return str(text) if text else None


def rebuild_offer_text(manager_dir: str | Path, default: str) -> str:
    """The post-deploy '--full?' prompt, with a Translation.txt reason when one is queued."""
    alert = pending_alert(manager_dir)
    if not alert:
        return default
    return default.rstrip() + "\n\n" + alert


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, indent=2, ensure_ascii=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as handle, open(source, "rb") as src:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        os.replace(tmp, destination)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_marker(package: Path) -> dict:
    try:
        data = json.loads((package / STAGE_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _source_info(package: Path, marker: dict | None = None) -> dict:
    marker = marker if marker is not None else _read_marker(package)
    masters = marker.get("selected_masters") or []
    if not isinstance(masters, list):
        masters = []
    version = (marker.get("PLUGIN_VERSION") or marker.get("filename_version") or
               marker.get("version") or "")
    return {
        "source_archive": str(marker.get("archive") or ""),
        "source_archive_sha256": str(marker.get("sha256") or ""),
        "source_version": str(version),
        "source_package": package.name,
        "source_masters": [str(name) for name in masters],
    }


def _is_tr_master(name: str) -> bool:
    key = Path(name).name.casefold()
    return key in TR_MASTER_KEYS or "tamriel rebuilt" in key.replace("_", " ")


def _is_td_master(name: str) -> bool:
    key = Path(name).name.casefold()
    return key in TD_MASTER_KEYS or key.startswith("tamriel_data")


def _run_mentions_tr(packages: list[Path], source_masters: list[str]) -> bool:
    if any(_is_tr_master(name) for name in source_masters):
        return True
    for package in packages:
        marker = _read_marker(package)
        masters = marker.get("selected_masters") or []
        if isinstance(masters, list) and any(_is_tr_master(str(name)) for name in masters):
            return True
        # Archive name often carries "Tamriel Rebuilt" even before masters are recorded.
        archive = str(marker.get("archive") or package.name)
        if "tamriel rebuilt" in archive.replace("_", " ").casefold():
            return True
    return False


def _source_changed(meta: dict, info: dict) -> bool:
    """True when the staged archive's version or hash is not what the tools copy came from."""
    if not meta:
        return True
    new_hash, old_hash = info.get("source_archive_sha256") or "", meta.get("source_archive_sha256") or ""
    if new_hash and old_hash and new_hash.casefold() != old_hash.casefold():
        return True
    new_ver, old_ver = str(info.get("source_version") or ""), str(meta.get("source_version") or "")
    if new_ver and old_ver and new_ver != old_ver and new_ver != "unknown" and old_ver != "unknown":
        return True
    new_name, old_name = str(info.get("source_archive") or ""), str(meta.get("source_archive") or "")
    if new_name and old_name and new_name.casefold() != old_name.casefold():
        return True
    return not meta


def _alert_text(*, content_changed: bool, info: dict, tr_updated: bool) -> str:
    version = info.get("source_version") or "unknown"
    archive = info.get("source_archive") or info.get("source_package") or "the staged archive"
    if tr_updated:
        lead = ("Tamriel Rebuilt was updated, and Translation.txt from the staged Tamriel Data "
                "has changed." if content_changed else
                "Tamriel Rebuilt was updated. Translation.txt from the staged Tamriel Data was checked.")
    else:
        lead = ("A newer Tamriel Data archive was staged and Translation.txt has changed."
                if content_changed else
                "A newer Tamriel Data archive was staged.")
    if content_changed:
        return (
            f"{lead} This file maps old object IDs to their current Tamriel Data names, and the "
            f"tools' copy has been refreshed from {archive} (version {version}). Generate Patches "
            "needs to run again (--full) so the patches use the new IDs."
        )
    return (
        f"{lead} The tools' copy already matches the file in {archive} (version {version}), so "
        "the patches do not need a rebuild for translations."
    )


def _queue_rebuild(manager_dir: Path, *, info: dict, file_sha256: str, alert: str) -> None:
    _write_json(pending_path(manager_dir), {
        "pending": True,
        "action": "full",
        "reason": "Translation.txt changed",
        "file_sha256": file_sha256,
        "source_archive": info.get("source_archive") or "",
        "source_version": info.get("source_version") or "",
        "source_archive_sha256": info.get("source_archive_sha256") or "",
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "alert": alert,
    })


def copy_from_package(package: str | Path, tools_dir: str | Path, *,
                      manager_dir: str | Path | None = None,
                      all_packages: list[Path] | None = None,
                      progress_callback=None) -> dict:
    """Copy Translation.txt from one staged package into tools/, if it should be refreshed."""
    root = Path(package)
    tools = Path(tools_dir)
    result = {"status": "missing", "package": root.name, "path": None, "content_changed": False,
              "rebuild_queued": False, "alert": None, "warning": None}
    source = find_translation(root)
    if source is None:
        result["warning"] = (
            f"No Translation.txt in {root.name}. It ships with Tamriel Data, in the Docs folder; "
            "the tools will go on without a new copy."
        )
        return result
    if not looks_like_rename_map(source):
        result["status"] = "skipped"
        result["warning"] = (
            f"{source.name} in {root.name} does not look like Tamriel Data's rename map "
            "(each line should be old-id:new-id). The tools' copy was left as it is."
        )
        return result
    try:
        file_hash = sha256_file(source)
    except OSError as exc:
        result["status"] = "error"
        result["warning"] = f"Could not read Translation.txt in {root.name}: {exc}"
        return result
    info = _source_info(root)
    meta = read_meta(tools)
    dest = translation_path(tools)
    existing_hash = ""
    if dest.is_file():
        try:
            existing_hash = sha256_file(dest)
        except OSError:
            existing_hash = ""
    content_changed = (not existing_hash) or existing_hash.casefold() != file_hash.casefold()
    source_changed = _source_changed(meta, info) or content_changed or not dest.is_file()
    result.update(path=str(source), file_sha256=file_hash, **{k: info[k] for k in info})
    if not source_changed and not content_changed:
        result["status"] = "unchanged"
        return result
    if content_changed or not dest.is_file():
        try:
            _atomic_copy(source, dest)
        except OSError as exc:
            result["status"] = "error"
            result["warning"] = (
                f"Could not copy Translation.txt into the tools folder: {exc}. "
                "Staging finished; --fix will go on without a new map."
            )
            return result
    tr_updated = _run_mentions_tr(list(all_packages or [root]), info.get("source_masters") or [])
    new_meta = {
        **info,
        "file_sha256": file_hash,
        "copied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "content_changed": content_changed,
    }
    try:
        _write_json(meta_path(tools), new_meta)
    except OSError as exc:
        result["status"] = "error"
        result["warning"] = f"Copied Translation.txt but could not write its source record: {exc}"
        # The copy itself is in place; keep going.
    alert = _alert_text(content_changed=content_changed, info=info, tr_updated=tr_updated)
    result["alert"] = alert if content_changed else None
    result["content_changed"] = content_changed
    if content_changed and manager_dir is not None:
        try:
            _queue_rebuild(Path(manager_dir), info=info, file_sha256=file_hash, alert=alert)
            result["rebuild_queued"] = True
        except OSError as exc:
            result["warning"] = (
                (result.get("warning") + " " if result.get("warning") else "") +
                f"The tools' copy was updated, but the --full rebuild could not be queued: {exc}."
            )
    if content_changed:
        result["status"] = "updated" if existing_hash else "copied"
    else:
        result["status"] = "unchanged"
    if content_changed:
        _report(
            progress_callback,
            "Updated Translation.txt from the staged Tamriel Data"
            + ("; Generate Patches will rebuild with it" if result.get("rebuild_queued") else "")
            + " (details in the status detail log)",
            f"copied {source} -> {dest}; sha256 {file_hash} (was {existing_hash or 'none'}); "
            f"archive {info.get('source_archive') or root.name}; version {info.get('source_version') or 'unknown'}; "
            f"rebuild queued: {bool(result.get('rebuild_queued'))}"
            + (f"; {result['warning']}" if result.get("warning") else ""))
    return result


def _package_dirs(staging: Path, extra: list[Path] | None = None) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for package in list(extra or []):
        try:
            resolved = Path(package).resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen and resolved.is_dir():
            seen.add(key)
            found.append(resolved)
    try:
        children = list(staging.iterdir())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            resolved = child.resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            found.append(resolved)
    return found


def refresh_from_staging(staging: str | Path, *, tools_dir: str | Path,
                         manager_dir: str | Path | None = None,
                         packages: list[Path] | None = None,
                         progress_callback=None) -> dict:
    """Scan staged major-master packages for Translation.txt and refresh the tools copy.

    Looks at every package in the staging folder, not only the ones extracted in
    this run, so a Tamriel Rebuilt update still picks up Tamriel Data staged
    beside it. Never raises: problems are returned as warnings.
    """
    staging_dir = Path(staging)
    all_packages = _package_dirs(staging_dir, packages)
    summary = {
        "status": "missing",
        "content_changed": False,
        "rebuild_queued": False,
        "alert": None,
        "warning": None,
        "results": [],
    }
    if not all_packages:
        summary["warning"] = "No staged packages were found, so Translation.txt was not updated."
        return summary
    # Prefer a package that actually contains the file; try Tamriel Data first.
    def order(package: Path) -> tuple:
        marker = _read_marker(package)
        masters = [str(name).casefold() for name in (marker.get("selected_masters") or [])]
        has_td = any(_is_td_master(name) for name in masters) or "tamriel data" in package.name.casefold()
        has_file = find_translation(package) is not None
        return (0 if has_file else 1, 0 if has_td else 1, package.name.casefold())

    warning_bits = []
    for package in sorted(all_packages, key=order):
        result = copy_from_package(
            package, tools_dir, manager_dir=manager_dir,
            all_packages=all_packages, progress_callback=progress_callback)
        summary["results"].append(result)
        if result.get("warning") and result.get("status") not in {"missing"}:
            warning_bits.append(result["warning"])
        if result.get("status") in {"copied", "updated", "unchanged"}:
            summary.update(
                status=result["status"],
                content_changed=bool(result.get("content_changed")),
                rebuild_queued=bool(result.get("rebuild_queued")),
                alert=result.get("alert"),
                path=result.get("path"),
                file_sha256=result.get("file_sha256"),
                source_archive=result.get("source_archive"),
                source_version=result.get("source_version"),
            )
            # One tools copy: the first package that has a usable file wins
            # (Tamriel Data preferred by the sort).
            if result["status"] != "unchanged" or not warning_bits:
                summary["warning"] = None
            break
    else:
        # Nothing had a usable file. Mention it once, in plain words.
        had_tr = _run_mentions_tr(all_packages, [])
        if warning_bits:
            pass                           # a file was found but could not be used: say why, below
        elif had_tr:
            summary["warning"] = (
                "Tamriel Rebuilt was staged, but Translation.txt is not in that archive; it ships "
                "with Tamriel Data, in the Docs folder. The tools' existing copy was left as it is."
            )
        else:
            summary["warning"] = (
                "No Translation.txt was found in the staged packages. It ships with Tamriel Data, "
                "in the Docs folder. The tools will go on without a new copy."
            )
    if warning_bits and summary.get("warning") is None:
        summary["warning"] = warning_bits[0]
    if summary.get("warning"):
        if summary["status"] == "error" or any(r.get("status") == "error" for r in summary["results"]):
            _report(progress_callback,
                    "Translation.txt could not be updated; staging finished and Generate Patches can still run "
                    "(details in the status detail log)", summary["warning"])
        elif summary["status"] == "missing" and not warning_bits and not _run_mentions_tr(all_packages, []):
            # Every batch staged before Tamriel Data would say this; detail log only.
            _report(progress_callback, None, summary["warning"])
        else:
            _report(progress_callback, summary["warning"])   # plain already, no paths
    return summary
