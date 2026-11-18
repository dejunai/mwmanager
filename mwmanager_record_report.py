"""Human-readable record conflicts derived from MWManager's catalog."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path


def load_conflicts(database: Path, *, limit: int = 5000,
                   active_plugins=None) -> dict:
    database = Path(database).resolve()
    if not database.is_file():
        return {"database": str(database), "conflicts": [], "identical": 0,
                "records_indexed": 0, "note": "Catalog database is missing."}
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """SELECT record_tag, editor_id_norm, editor_id, source_file, body_sha256,
                          load_order
                   FROM esm_record
                   WHERE editor_id_norm IS NOT NULL
                   ORDER BY record_tag, editor_id_norm, load_order, source_file"""
            ).fetchall()
        except sqlite3.OperationalError as exc:
            return {"database": str(database), "conflicts": [], "identical": 0,
                    "records_indexed": 0, "note": f"Catalog is unavailable: {exc}"}
    grouped = defaultdict(list)
    active_order = ({str(name).casefold(): index
                     for index, name in enumerate(active_plugins)}
                    if active_plugins is not None else None)
    for row in rows:
        if active_order is not None and str(row["source_file"]).casefold() not in active_order:
            continue
        version = dict(row)
        if active_order is not None:
            version["load_order"] = active_order[str(row["source_file"]).casefold()]
        grouped[(row["record_tag"], row["editor_id_norm"])].append(version)
    conflicts, identical = [], 0
    for (tag, normalized), versions in grouped.items():
        versions.sort(key=lambda value: (value["load_order"], str(value["source_file"]).casefold()))
        if len(versions) < 2:
            continue
        distinct = {bytes(value) for value in (row["body_sha256"] for row in versions)}
        if len(distinct) == 1:
            identical += 1
            continue
        conflicts.append({
            "tag": tag,
            "id": versions[-1].get("editor_id") or normalized,
            "normalized_id": normalized,
            "version_count": len(versions),
            "distinct_versions": len(distinct),
            "versions": [
                {"plugin": row["source_file"], "load_order": row["load_order"],
                 "body_sha256": bytes(row["body_sha256"]).hex()}
                for row in versions
            ],
            "winner": versions[-1]["source_file"],
        })
    conflicts.sort(key=lambda value: (
        -value["version_count"],
        -value["distinct_versions"],
        value["tag"],
        str(value["id"]).casefold(),
    ))
    return {
        "database": str(database),
        "conflicts": conflicts[:max(1, int(limit))],
        "total_conflicts": len(conflicts),
        "identical": identical,
        "records_indexed": len(rows),
        "records_active": sum(len(values) for values in grouped.values()),
        "note": None,
    }


def render(report: dict) -> list[str]:
    lines = [
        f"Indexed records: {report.get('records_indexed', 0):,} "
        f"({report.get('records_active', report.get('records_indexed', 0)):,} active)",
        f"Conflicting record IDs: {report.get('total_conflicts', len(report.get('conflicts', []))):,}",
        f"Identical multi-plugin records: {report.get('identical', 0):,}",
    ]
    if report.get("note"):
        lines.append(report["note"])
    for item in report.get("conflicts", []):
        lines.append("")
        lines.append(f"{item['tag']} {item['id']} — winner: {item['winner']}")
        for version in item["versions"]:
            lines.append(f"  [{version['load_order']}] {version['plugin']} "
                         f"{version['body_sha256'][:12]}")
    hidden = report.get("total_conflicts", 0) - len(report.get("conflicts", []))
    if hidden > 0:
        lines.append(f"... {hidden:,} additional conflict(s) omitted.")
    return lines
