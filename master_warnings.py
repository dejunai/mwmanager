"""Warnings shown when a major master is chosen.

The text and any cheap incompatibility rules live in mwmanager_master_warnings.json,
so a new master warning does not need new UI code. Optional lists of already
catalogued plugins come from game_catalog.sqlite3; this module never opens a
plugin file or starts a scan.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

WARNINGS_FILE = "mwmanager_master_warnings.json"
DEFAULT_WARNINGS_PATH = Path(__file__).resolve().parent / WARNINGS_FILE


def warnings_config(path: str | Path | None = None) -> dict[str, dict]:
    """{master filename, casefolded: warning record} from the shipped JSON, or path."""
    source = Path(path) if path is not None else DEFAULT_WARNINGS_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    config = {}
    for name, record in raw.items():
        if not isinstance(name, str) or not isinstance(record, dict) or not record.get("text"):
            continue
        config[name.casefold()] = record
    return config


def master_warning(name: str, config: dict | None = None) -> dict | None:
    """The warning record for a master, or None."""
    records = warnings_config() if config is None else config
    return records.get(Path(name).name.casefold())


def overlapping_plugins(catalog_db: str | Path | None, overlap_of: str, record_tags: list[str],
                        exclude: list[str] | None = None,
                        exclude_name_contains: list[str] | None = None) -> list[str]:
    """Distinct plugins in an existing catalog that share CELL/LAND/PGRD identities with overlap_of.

    Empty when the catalog is missing, unreadable, or has not indexed those files yet.
    """
    if not catalog_db or not overlap_of or not record_tags:
        return []
    path = Path(catalog_db)
    if not path.is_file():
        return []
    tags = [tag for tag in record_tags if isinstance(tag, str) and len(tag) == 4]
    if not tags:
        return []
    placeholders = ",".join("?" * len(tags))
    sql = (
        "SELECT DISTINCT other.source_file "
        "FROM esm_record AS blood "
        "JOIN esm_record AS other "
        "  ON other.record_tag = blood.record_tag "
        " AND other.editor_id_norm = blood.editor_id_norm "
        "WHERE lower(blood.source_file) = lower(?) "
        f"  AND blood.record_tag IN ({placeholders}) "
        "  AND blood.editor_id_norm IS NOT NULL "
        "  AND lower(other.source_file) <> lower(blood.source_file) "
        "ORDER BY other.source_file COLLATE NOCASE"
    )
    try:
        # as_posix() keeps the sqlite file: URI valid on Windows paths.
        # closing() is required: a sqlite connection context manager does not close the handle.
        uri = path.resolve().as_posix()
        with closing(sqlite3.connect(f"file:{uri}?mode=ro", uri=True, timeout=1)) as db:
            rows = db.execute(sql, (Path(overlap_of).name, *tags)).fetchall()
    except sqlite3.Error:
        return []
    skipped = {Path(name).name.casefold() for name in (exclude or [])}
    needles = [needle.casefold() for needle in (exclude_name_contains or []) if needle]
    names = []
    for (source,) in rows:
        if not source:
            continue
        key = Path(source).name.casefold()
        if key in skipped or any(needle in key for needle in needles):
            continue
        names.append(str(source))
    return names


def likely_incompatible_plugins(master: str, *, catalog_db: str | Path | None = None,
                                config: dict | None = None) -> list[str]:
    """Plugins the warning's catalog rule flags, or an empty list without a catalog."""
    record = master_warning(master, config)
    rule = (record or {}).get("likely_incompatible") or {}
    if not record or not isinstance(rule, dict) or not rule.get("overlap_cells_of"):
        return []
    return overlapping_plugins(
        catalog_db, rule["overlap_cells_of"], list(rule.get("record_tags") or ()),
        exclude=list(rule.get("exclude") or ()),
        exclude_name_contains=list(rule.get("exclude_name_contains") or ()))


def warning_text(master: str, *, catalog_db: str | Path | None = None,
                 config: dict | None = None, plugins: list[str] | None = None) -> str | None:
    """Full warning text, with an optional list of likely-incompatible plugins."""
    record = master_warning(master, config)
    if not record:
        return None
    text = str(record["text"]).rstrip()
    if plugins is None:
        plugins = likely_incompatible_plugins(master, catalog_db=catalog_db, config=config)
    if not plugins:
        return text
    heading = str(record.get("incompatible_heading") or
                  "These installed plugins already share cells with this master, and are likely incompatible:")
    limit = 15
    rule = record.get("likely_incompatible") or {}
    if isinstance(rule, dict) and rule.get("list_limit"):
        try:
            limit = max(1, int(rule["list_limit"]))
        except (TypeError, ValueError):
            pass
    shown, extra = plugins[:limit], len(plugins) - min(len(plugins), limit)
    lines = "\n".join(f"- {name}" for name in shown)
    if extra:
        lines += f"\n- and {extra} more"
    return f"{text}\n\n{heading}\n{lines}"


def attach_master_warnings(rows: list[dict], catalog_db: str | Path | None = None,
                           config: dict | None = None) -> None:
    """Set warning and warning_summary on each row that has a configured warning."""
    for row in rows:
        name = row.get("name") or ""
        text = warning_text(name, catalog_db=catalog_db, config=config)
        if not text:
            continue
        row["warning"] = text
        record = master_warning(name, config) or {}
        row["warning_summary"] = str(record.get("summary") or text.split("\n", 1)[0])
