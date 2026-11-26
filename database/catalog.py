"""Enumerate and index Morrowind's three core ESMs and three core BSAs.

Uses only the Python standard library. The ESM table preserves complete raw
record bodies (including every subrecord); the BSA table stores directory
entries and offsets without extracting or copying the asset contents.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import struct
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "game_catalog.sqlite3"   # MWManager's folder
CORE_ESMS = ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm")
CORE_BSAS = ("Morrowind.bsa", "Tribunal.bsa", "Bloodmoon.bsa")


def is_grass_plugin(path: Path) -> bool:
    """An MGE XE grass plugin (vortex_tidy's content test). It is read by MGE XE's distant land generator from
    Data Files\\Grass, never loaded by the game, so it is never catalogued and never reaches a conflict."""
    if path.suffix.casefold() != ".esp":
        return False
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from vortex_tidy import is_grass_plugin as test
    except ImportError:
        return False
    try:
        return bool(test(str(path)))
    except (OSError, ValueError, struct.error):
        return False


def sha256_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def find_case_insensitive(folder: Path, wanted: str) -> Path | None:
    try:
        return next((p for p in folder.iterdir() if p.is_file() and p.name.casefold() == wanted.casefold()), None)
    except OSError:
        return None


def read_initial_esm_load_order(ini_path: Path) -> list[str]:
    """Read the first three indexed GameFile entries from [Game Files]."""
    section = ""
    entries: dict[int, str] = {}
    try:
        text = ini_path.read_text(encoding="cp1252", errors="replace")
    except OSError as exc:
        raise ValueError(f"cannot read Morrowind.ini: {exc}") from exc
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().casefold()
            continue
        if section != "game files":
            continue
        match = re.match(r"^GameFile\s*(\d+)\s*=\s*(.*?)\s*$", line, flags=re.IGNORECASE)
        if match:
            entries[int(match.group(1))] = match.group(2).strip().strip('"')
    ordered = [entries[index] for index in sorted(entries)[:3]]
    if len(ordered) != 3:
        raise ValueError(f"expected at least three GameFile entries in [Game Files] of {ini_path}; found {len(ordered)}")
    if any(Path(name).suffix.casefold() != ".esm" for name in ordered):
        raise ValueError(f"the first three [Game Files] entries must be ESMs; found: {ordered}")
    if len({name.casefold() for name in ordered}) != 3:
        raise ValueError(f"duplicate ESM names among first three [Game Files] entries: {ordered}")
    return ordered


def enumerate_expected(data_dir: Path, ini_path: Path, canonical_core: bool = False,
                       plugin_files: list[str] | None = None,
                       left_out: list[Path] | None = None) -> tuple[list[Path], list[Path], list[str]]:
    """Print ordered plugin inputs, all expected core archives, and additional ESM/BSA files. An MGE XE grass
    plugin named in plugin_files is left out (and added to left_out): it never reaches the record conflicts."""
    missing: list[str] = []
    found_esms: list[Path] = []
    found_bsas: list[Path] = []
    print(f"Data Files: {data_dir}")
    print(f"Load-order source: {ini_path}")
    try:
        ordered_esms = (list(dict.fromkeys(plugin_files)) if plugin_files is not None else
                        list(CORE_ESMS) if canonical_core else read_initial_esm_load_order(ini_path))
    except ValueError as exc:
        print(f"  ERROR    {exc}")
        return found_esms, found_bsas, [str(exc)]
    print("\nPlugins to catalog:")
    for load_order, name in enumerate(ordered_esms):
        path = find_case_insensitive(data_dir, name)
        if path is None:
            print(f"  MISSING  GameFile{load_order}={name}")
            missing.append(name)
        elif plugin_files is not None and is_grass_plugin(path):
            print(f"  GRASS    {path.name}  (MGE XE grass plugin: belongs in Data Files\\Grass, left out)")
            if left_out is not None:
                left_out.append(path)
        else:
            print(f"  [{load_order}] FOUND    {path.name}  ({path.stat().st_size:,} bytes)")
            found_esms.append(path)
    print("\nExpected core archives:")
    for name in CORE_BSAS:
        path = find_case_insensitive(data_dir, name)
        if path is None:
            print(f"  MISSING  {name}")
            missing.append(name)
        else:
            print(f"  FOUND    {path.name}  ({path.stat().st_size:,} bytes)")
            found_bsas.append(path)
    try:
        expected = {n.casefold() for n in (*ordered_esms, *CORE_BSAS)}
        extras = sorted((p for p in data_dir.iterdir() if p.is_file()
                         and p.suffix.casefold() in {".esm", ".bsa"}
                         and p.name.casefold() not in expected), key=lambda p: p.name.casefold())
        print("\nAdditional ESM/BSA files (listed, not imported):")
        if extras:
            for p in extras:
                print(f"  EXTRA    {p.name}  ({p.stat().st_size:,} bytes)")
        else:
            print("  (none)")
    except OSError as exc:
        missing.append(f"Cannot enumerate Data Files: {exc}")
    return found_esms, found_bsas, missing


def iter_esm_records(path: Path):
    """Yield ordinal, offset, tag, flags, and raw body from TES3 records."""
    with path.open("rb") as stream:
        ordinal = 0
        while True:
            offset = stream.tell()
            header = stream.read(16)
            if not header:
                return
            if len(header) != 16:
                raise ValueError(f"truncated record header at offset {offset}")
            tag_b = header[:4]
            size, _unused, flags = struct.unpack_from("<III", header, 4)
            body = stream.read(size)
            if len(body) != size:
                raise ValueError(f"{tag_b!r} record at offset {offset} is truncated")
            yield ordinal, offset, tag_b.decode("ascii", "replace"), flags, body
            ordinal += 1


def first_name_subrecord(body: bytes) -> bytes | None:
    """Return the raw NAME payload if present, and validate subrecord boundaries."""
    pos = 0
    while pos + 8 <= len(body):
        tag = body[pos:pos + 4]
        size = struct.unpack_from("<I", body, pos + 4)[0]
        start, end = pos + 8, pos + 8 + size
        if end > len(body):
            raise ValueError(f"subrecord {tag!r} runs past record body")
        if tag == b"NAME":
            return body[start:end].split(b"\0", 1)[0]
        pos = end
    if pos != len(body):
        raise ValueError("truncated subrecord header")
    return None


def record_identity(tag: str, body: bytes) -> tuple[str, str] | None:
    """(key, label) from upstream's record_id rules: INFO by INAM, CELL by grid or name, LAND by INTV,
    SCPT by SCHD, SKIL/MGEF by INDX, the rest by NAME. None for records without an identity."""
    if str(Path(__file__).resolve().parents[1]) not in sys.path:
        sys.path.append(str(Path(__file__).resolve().parents[1]))     # mwdialog_tes3 sits in MWManager's folder
    import mwdialog_tes3 as tes3
    identity = tes3.record_id(tag, body)
    if identity is None:
        return None
    parts = []
    for part in identity[1:]:
        for value in (part if isinstance(part, tuple) else (part,)):
            parts.append(value.hex() if isinstance(value, bytes) else str(value))
    key = "|".join(parts)
    if tag == "CELL":
        label = tes3.cell_key(body)[1]
    elif tag == "LAND" and len(identity[1]) >= 8:
        label = "({}, {})".format(*struct.unpack_from("<ii", identity[1]))
    elif tag in ("SKIL", "MGEF") and len(identity[1]) >= 4:
        label = str(struct.unpack_from("<i", identity[1])[0])
    else:                       # original case: saved winner choices are keyed by this text
        if tag == "INFO":
            raw = tes3.first_sub(body, b"INAM")
        elif tag == "SCPT":
            raw = tes3.first_sub(body, b"SCHD")[:32]
        else:
            raw = tes3.first_sub(body, b"NAME")
        label = tes3.cstr(raw or b"")
        if tag == "PGRD":
            label = f"{label} {identity[2]}"
    return key, label


def bsa_entries(path: Path):
    """Yield member path, absolute data offset, and indexed size for TES3 BSA v1."""
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12:
            raise ValueError("archive header is truncated")
        version, directory_size, count = struct.unpack("<III", header)
        if version != 0x100:
            raise ValueError(f"unsupported TES3 BSA version 0x{version:X}")
        if count > 10_000_000:
            raise ValueError(f"implausible BSA file count: {count}")
        directory = stream.read(directory_size)
        if len(directory) != directory_size:
            raise ValueError("archive directory is truncated")
        if len(directory) < 12 * count:
            raise ValueError("archive directory tables are truncated")
        names_start = 12 * count
        data_start = 12 + directory_size + 8 * count
        for index in range(count):
            size, relative_offset = struct.unpack_from("<II", directory, 8 * index)
            name_offset = struct.unpack_from("<I", directory, 8 * count + 4 * index)[0]
            start = names_start + name_offset
            if start >= len(directory):
                raise ValueError(f"BSA filename offset out of range at entry {index}")
            end = directory.find(b"\0", start)
            if end < 0:
                raise ValueError(f"unterminated BSA filename at entry {index}")
            name = directory[start:end].decode("latin-1").replace("\\", "/").strip("/")
            if not name:
                raise ValueError(f"empty BSA filename at entry {index}")
            yield name, data_start + relative_offset, size


IDENTITY_VERSION = 1      # PRAGMA user_version once editor_id_norm follows record_id (it was NAME for every type)


def create_schema(connection: sqlite3.Connection, schema_path: Path) -> None:
    connection.executescript(schema_path.read_text(encoding="utf-8"))
    if connection.execute("PRAGMA user_version").fetchone()[0] < IDENTITY_VERSION:
        # Older catalogs keep every record body, so the keys are recomputed in place, not re-indexed.
        rows = connection.execute("SELECT record_id, record_tag, body FROM esm_record").fetchall()
        updates = []
        for row_id, tag, body in rows:
            identity = record_identity(tag, bytes(body))
            updates.append((*(identity or (None, None)), row_id))
        connection.executemany("UPDATE esm_record SET editor_id_norm = ?, editor_id = ? WHERE record_id = ?", updates)
        connection.execute(f"PRAGMA user_version = {IDENTITY_VERSION}")
        connection.commit()


def index_esm(connection: sqlite3.Connection, path: Path, load_order: int) -> int:
    name, digest = path.name, sha256_file(path)
    connection.execute("DELETE FROM esm_record WHERE source_file = ?", (name,))
    count = 0
    for ordinal, offset, tag, flags, body in iter_esm_records(path):
        editor_raw = first_name_subrecord(body)                 # also validates the subrecord layout
        identity = record_identity(tag, body)
        editor_key, editor_text = identity if identity else (None, None)
        connection.execute(
            """INSERT INTO esm_record
               (source_file, source_sha256, load_order, record_ordinal, file_offset, record_tag,
                record_flags, editor_id_raw, editor_id, editor_id_norm,
                body_size, body_sha256, body)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, digest, load_order, ordinal, offset, tag, flags, editor_raw, editor_text,
             editor_key,
             len(body), hashlib.sha256(body).digest(), body))
        count += 1
        if count % 5000 == 0:
            connection.commit()
            print(f"    {name}: {count:,} records indexed")
    connection.commit()
    return count


def index_bsa(connection: sqlite3.Connection, path: Path) -> int:
    name, digest = path.name, sha256_file(path)
    connection.execute("DELETE FROM bsa_asset WHERE archive_file = ?", (name,))
    count = 0
    for member, offset, size in bsa_entries(path):
        normalized = member.replace("\\", "/").strip("/").casefold()
        connection.execute(
            """INSERT INTO bsa_asset
               (archive_file, archive_sha256, asset_path, asset_path_norm, byte_offset, stored_size)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, digest, member, normalized, offset, size))
        count += 1
        if count % 5000 == 0:
            connection.commit()
            print(f"    {name}: {count:,} assets indexed")
    connection.commit()
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory and index Morrowind plugins and BSA assets into SQLite.")
    parser.add_argument("--game-dir", type=Path, required=True, help="Morrowind installation directory")
    parser.add_argument("--ini", type=Path, help="Morrowind.ini path (defaults to --game-dir/Morrowind.ini)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite catalog database path")
    parser.add_argument("--inventory-only", action="store_true", help="list expected/missing/extra files without creating a database")
    parser.add_argument("--canonical-core", action="store_true", help="use the canonical Morrowind, Tribunal, and Bloodmoon ESMs even if the INI needs repair")
    parser.add_argument("--plugin-files", nargs="*", metavar="PLUGIN", help="ordered ESM/ESP files to index (default: canonical core or first three INI ESMs)")
    parser.add_argument("--append", action="store_true", help="refresh the selected plugins while retaining other catalogued plugins")
    args = parser.parse_args(argv)
    data_dir = args.game_dir / "Data Files"
    ini_path = args.ini or args.game_dir / "Morrowind.ini"
    if not data_dir.is_dir():
        print(f"ERROR: Data Files folder not found: {data_dir}", file=sys.stderr)
        return 2
    grass: list[Path] = []
    esms, bsas, missing = enumerate_expected(data_dir, ini_path, canonical_core=args.canonical_core,
                                               plugin_files=args.plugin_files, left_out=grass)
    if missing:
        print("\nERROR: core catalog is incomplete; import was not started.", file=sys.stderr)
        return 2
    if args.inventory_only:
        return 0
    args.db.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(args.db) as connection:
            create_schema(connection, Path(__file__).with_name("schema.sql"))
            keep_esms = [p.name.casefold() for p in esms]
            if grass:   # a grass plugin catalogued by an earlier run must not keep its records in the conflicts
                connection.execute(
                    "DELETE FROM esm_record WHERE lower(source_file) IN (" + ",".join("?" for _ in grass) + ")",
                    [p.name.casefold() for p in grass])
            if args.append:
                connection.execute(
                    "DELETE FROM esm_record WHERE lower(source_file) IN (" + ",".join("?" for _ in keep_esms) + ")",
                    keep_esms)
            else:
                connection.execute(
                    "DELETE FROM esm_record WHERE lower(source_file) NOT IN (" + ",".join("?" for _ in keep_esms) + ")",
                    keep_esms)
            keep_bsas = [p.name.casefold() for p in bsas]
            if args.append:
                connection.execute(
                    "DELETE FROM bsa_asset WHERE lower(archive_file) IN (" + ",".join("?" for _ in keep_bsas) + ")",
                    keep_bsas)
            else:
                connection.execute(
                    "DELETE FROM bsa_asset WHERE lower(archive_file) NOT IN (" + ",".join("?" for _ in keep_bsas) + ")",
                    keep_bsas)
            connection.commit()
            print(f"\nSQLite catalog: {args.db}")
            for load_order, path in enumerate(esms):
                print(f"  Reading records from {path.name}...")
                count = index_esm(connection, path, load_order)
                print(f"    indexed {count:,} records")
            for path in bsas:
                print(f"  Reading archive directory from {path.name}...")
                count = index_bsa(connection, path)
                print(f"    indexed {count:,} assets")
    except (OSError, sqlite3.Error, ValueError, struct.error) as exc:
        print(f"ERROR: catalog import failed: {exc}", file=sys.stderr)
        return 1
    print("\nCatalog complete. BSA payloads remain in their archives; only paths, offsets, sizes, and archive hashes were indexed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
