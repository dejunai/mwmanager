"""Read-only access to Vortex's application state (%APPDATA%\\Vortex\\state.v2, a LevelDB database).

Only reads: table (.ldb/.sst) and log files are opened for reading, nothing is locked, copied or written. A small
LevelDB and Snappy reader lives here so no native module is needed. Keys are Vortex's "###"-joined state paths
("persistent###downloads###files###<id>###localPath") with JSON values; the newest sequence number of a key wins
and a deletion hides it."""

from __future__ import annotations
import json
import os
import struct
from pathlib import Path

TABLE_MAGIC = bytes.fromhex("57fb808b247547db")
LOG_BLOCK = 32768


def default_state_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Vortex" / "state.v2"


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def snappy_decompress(data: bytes) -> bytes:
    """Raw (unframed) Snappy, as LevelDB stores compressed blocks."""
    length, pos = _varint(data, 0)
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            size = tag >> 2
            if size >= 60:
                extra = size - 59
                size = int.from_bytes(data[pos:pos + extra], "little")
                pos += extra
            size += 1
            out += data[pos:pos + size]
            pos += size
            continue
        if kind == 1:
            size = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | data[pos]
            pos += 1
        elif kind == 2:
            size = (tag >> 2) + 1
            offset = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
        else:
            size = (tag >> 2) + 1
            offset = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        if offset <= 0 or offset > len(out):
            raise ValueError("bad snappy copy offset")
        start = len(out) - offset
        if offset >= size:
            out += out[start:start + size]
        else:                                                   # an overlapping copy repeats its own output
            for i in range(size):
                out.append(out[start + i])
    if len(out) != length:
        raise ValueError("snappy length mismatch")
    return bytes(out)


def _block_entries(block: bytes):
    if len(block) < 4:
        return
    restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    end = len(block) - 4 - 4 * restarts
    pos, key = 0, b""
    while pos < end:
        shared, pos = _varint(block, pos)
        unshared, pos = _varint(block, pos)
        value_len, pos = _varint(block, pos)
        key = key[:shared] + block[pos:pos + unshared]
        pos += unshared
        yield key, block[pos:pos + value_len]
        pos += value_len


def _read_block(data: bytes, offset: int, size: int) -> bytes:
    raw, kind = data[offset:offset + size], data[offset + size]
    return snappy_decompress(raw) if kind == 1 else raw


def table_entries(path: Path):
    """(user key, sequence, value or None for a deletion) from one sorted table file."""
    data = Path(path).read_bytes()
    if len(data) < 48 or data[-8:] != TABLE_MAGIC:
        return
    footer = data[-48:]
    _meta_off, pos = _varint(footer, 0)
    _meta_size, pos = _varint(footer, pos)
    index_off, pos = _varint(footer, pos)
    index_size, pos = _varint(footer, pos)
    for _last_key, handle in _block_entries(_read_block(data, index_off, index_size)):
        off, p = _varint(handle, 0)
        size, _p = _varint(handle, p)
        for ikey, value in _block_entries(_read_block(data, off, size)):
            if len(ikey) < 8:
                continue
            tag = int.from_bytes(ikey[-8:], "little")
            yield ikey[:-8], tag >> 8, (value if tag & 0xFF == 1 else None)


def log_entries(path: Path):
    """(user key, sequence, value or None) from a write-ahead log; a torn tail (Vortex still writing) is skipped."""
    data = Path(path).read_bytes()
    records, pending, pos = [], b"", 0
    while pos + 7 <= len(data):
        left = LOG_BLOCK - pos % LOG_BLOCK
        if left < 7:
            pos += left
            continue
        length = int.from_bytes(data[pos + 4:pos + 6], "little")
        kind = data[pos + 6]
        chunk = data[pos + 7:pos + 7 + length]
        pos += 7 + length
        if len(chunk) < length:
            break
        if kind == 1:
            records.append(chunk)
        elif kind == 2:
            pending = chunk
        elif kind == 3:
            pending += chunk
        elif kind == 4:
            records.append(pending + chunk)
            pending = b""
    for record in records:
        if len(record) < 12:
            continue
        seq, count = int.from_bytes(record[:8], "little"), int.from_bytes(record[8:12], "little")
        p, batch = 12, []
        try:
            for i in range(count):
                kind = record[p]
                p += 1
                klen, p = _varint(record, p)
                key = record[p:p + klen]
                p += klen
                value = None
                if kind == 1:
                    vlen, p = _varint(record, p)
                    value = record[p:p + vlen]
                    p += vlen
                batch.append((key, seq + i, value))
        except IndexError:
            continue
        yield from batch


def read_prefix(prefix: str, state_dir: str | os.PathLike | None = None) -> dict[str, object]:
    """{key: decoded JSON value} for every live key starting with `prefix`. Empty when there is no database."""
    folder = Path(state_dir) if state_dir else default_state_dir()
    want = prefix.encode("utf-8")
    newest: dict[bytes, tuple[int, bytes | None]] = {}
    try:
        files = sorted(p for p in folder.iterdir() if p.suffix.casefold() in {".ldb", ".sst", ".log"})
    except OSError:
        return {}
    for path in files:
        try:
            entries = log_entries(path) if path.suffix.casefold() == ".log" else table_entries(path)
            for key, seq, value in entries:
                if key.startswith(want) and seq >= newest.get(key, (-1, None))[0]:
                    newest[key] = (seq, value)
        except (OSError, ValueError, IndexError, struct.error):
            continue
    result = {}
    for key, (_seq, value) in newest.items():
        if value is None:
            continue
        try:
            result[key.decode("utf-8")] = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
    return result


def _tree(prefix: str, state_dir) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    depth = prefix.count("###")
    for key, value in read_prefix(prefix, state_dir).items():
        parts = key.split("###")
        if len(parts) <= depth:
            continue
        _set_path(rows.setdefault(parts[depth], {}), parts[depth + 1:], value)
    return rows


def downloads(game: str = "morrowind", state_dir: str | os.PathLike | None = None) -> list[dict]:
    """Vortex's download records for `game`: {"file", "size", "md5", "mod_id", "file_id", "name", "version"}."""
    found = []
    for entry in _tree("persistent###downloads###files###", state_dir).values():
        games = entry.get("game") or [game]
        if game not in (games if isinstance(games, list) else [games]):
            continue
        nexus = ((entry.get("modInfo") or {}).get("nexus") or {})
        ids = nexus.get("ids") or {}
        info, mod = nexus.get("fileInfo") or {}, nexus.get("modInfo") or {}
        found.append({"file": str(entry.get("localPath") or ""), "size": entry.get("size"),
                      "md5": str(entry.get("fileMD5") or "").casefold(),
                      "mod_id": _digits(ids.get("modId") or mod.get("mod_id")),
                      "file_id": _digits(ids.get("fileId") or info.get("file_id")),
                      "name": str(mod.get("name") or info.get("name") or ""),
                      "version": str(info.get("version") or mod.get("version") or "")})
    return found


def staged_mods(game: str = "morrowind", state_dir: str | os.PathLike | None = None) -> list[dict]:
    """Vortex's installed-mod records: {"folder", "archive", "md5", "size", "mod_id", "file_id", "name"}."""
    found = []
    for mod_key, entry in _tree(f"persistent###mods###{game}###", state_dir).items():
        attrs = entry.get("attributes") or {}
        found.append({"folder": str(entry.get("installationPath") or mod_key),
                      "archive": str(attrs.get("fileName") or ""),
                      "md5": str(attrs.get("fileMD5") or "").casefold(), "size": attrs.get("fileSize"),
                      "mod_id": _digits(attrs.get("modId")), "file_id": _digits(attrs.get("fileId")),
                      "name": str(attrs.get("modName") or attrs.get("logicalFileName") or attrs.get("name") or ""),
                      "version": str(attrs.get("version") or "")})
    return found


def _digits(value) -> str:
    text = str(value if value is not None else "").strip()
    return text if text.isdigit() and int(text) > 0 else ""


def _set_path(target: dict, parts: list[str], value) -> None:
    if not parts:
        if isinstance(value, dict):
            target.update(value)
        return
    for part in parts[:-1]:
        nxt = target.get(part)
        if not isinstance(nxt, dict):
            nxt = target[part] = {}
        target = nxt
    target[parts[-1]] = value
