"""Build a deterministic archive corpus for safety and installer regression tests."""
from __future__ import annotations

import argparse
import json
import stat
import zipfile
from pathlib import Path


def build(destination: Path) -> list[Path]:
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    made = []

    clean = destination / "clean-plugin.zip"
    with zipfile.ZipFile(clean, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Data Files/Example.esp", b"TES3" + b"\0" * 12)
        archive.writestr("Data Files/Textures/example.dds", b"DDS " + b"x" * 20)
    made.append(clean)

    traversal = destination / "unsafe-traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape.txt", b"escape")
    made.append(traversal)

    duplicate = destination / "case-collision.zip"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("Textures/Rock.dds", b"one")
        archive.writestr("textures/rock.dds", b"two")
    made.append(duplicate)

    executable = destination / "blocked-payload.zip"
    with zipfile.ZipFile(executable, "w") as archive:
        archive.writestr("Tools/setup.exe", b"MZ")
    made.append(executable)

    link = destination / "symlink-member.zip"
    with zipfile.ZipFile(link, "w") as archive:
        entry = zipfile.ZipInfo("redirect")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, b"../outside")
        archive.writestr("redirect/payload.txt", b"must not follow")
    made.append(link)

    bomb = destination / "high-ratio.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Textures/repeated.dds", b"\0" * (20 * 1024 * 1024))
    made.append(bomb)

    manifest = {"version": 1, "files": [{"name": path.name, "bytes": path.stat().st_size}
                                      for path in made]}
    (destination / "corpus.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                            encoding="utf-8")
    return made


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", help="folder to create")
    args = parser.parse_args(argv)
    for path in build(Path(args.destination)):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
