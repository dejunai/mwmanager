"""Small stand-ins so MWManager runs on Python 3.9 as well as newer versions.

Each helper uses the standard library's own version when it has one and only adds the missing behaviour on older
Pythons:

* TemporaryDirectory(ignore_cleanup_errors=...)  (3.10+)
* write_text(path, data, encoding=, errors=, newline=)  (Path.write_text(newline=) is 3.10+)
* toml_loads(text)  (tomllib is 3.11+): tomllib, else tomli if installed, else a small reader for the flat
  tables and scalar values MWManager reads from files such as mgeXE.toml.
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

if sys.version_info >= (3, 10):
    TemporaryDirectory = tempfile.TemporaryDirectory
else:
    class TemporaryDirectory(tempfile.TemporaryDirectory):
        """3.9's TemporaryDirectory plus the ignore_cleanup_errors keyword."""

        def __init__(self, suffix=None, prefix=None, dir=None, ignore_cleanup_errors=False):
            super().__init__(suffix, prefix, dir)
            self._ignore_cleanup_errors = ignore_cleanup_errors

        def cleanup(self):
            if not self._ignore_cleanup_errors:
                super().cleanup()
                return
            if self._finalizer.detach() or Path(self.name).exists():
                shutil.rmtree(self.name, ignore_errors=True)

        def __exit__(self, exc_type, exc, tb):
            self.cleanup()


def write_text(path, data: str, encoding: str | None = None, errors: str | None = None,
               newline: str | None = None) -> int:
    """Path.write_text with the newline argument, which Python 3.9 lacks."""
    with open(path, "w", encoding=encoding, errors=errors, newline=newline) as stream:
        return stream.write(data)


class _FallbackTOMLError(ValueError):
    """Raised by the fallback reader."""


try:
    import tomllib as _toml
except ModuleNotFoundError:             # Python 3.9 and 3.10
    try:
        import tomli as _toml
    except ModuleNotFoundError:
        _toml = None

# What callers catch: the parser's own error when there is one, else the fallback reader's.
TOMLDecodeError = _toml.TOMLDecodeError if _toml is not None else _FallbackTOMLError


_TABLE = re.compile(r"^\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_PAIR = re.compile(r"^([A-Za-z0-9_\-\.\"']+)\s*=\s*(.*)$")


def _scalar(raw: str):
    raw = raw.strip()
    if raw[:1] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        if end < 0:
            raise _FallbackTOMLError(f"unterminated string: {raw}")
        return raw[1:end]
    raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        return int(raw.replace("_", ""), 0)
    except ValueError:
        pass
    try:
        return float(raw.replace("_", ""))
    except ValueError:
        raise _FallbackTOMLError(f"unsupported value: {raw}") from None


def _simple_toml(text: str) -> dict:
    """Flat tables of key = scalar (string, bool, number). Arrays, inline tables and multi-line strings are not
    supported and raise TOMLDecodeError, so a caller never gets a silently wrong answer."""
    root: dict = {}
    table = root
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        header = _TABLE.match(line)
        if header and not line.startswith("[["):
            table = root
            for part in (p.strip().strip("\"'") for p in header.group(1).split(".")):
                table = table.setdefault(part, {})
            continue
        pair = _PAIR.match(line)
        if not pair or pair.group(2).lstrip()[:1] in ("[", "{") or pair.group(2).lstrip().startswith('"""'):
            raise _FallbackTOMLError(f"line {number}: unsupported TOML: {line[:60]}")
        table[pair.group(1).strip("\"'")] = _scalar(pair.group(2))
    return root


def toml_loads(text: str) -> dict:
    """tomllib.loads on 3.11+, tomli.loads if installed, else the small flat reader above."""
    if _toml is not None:
        return _toml.loads(text)
    return _simple_toml(text)