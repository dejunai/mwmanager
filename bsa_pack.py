"""Pack a large mod's vanilla-readable staged assets into a TES3 BSA.

This helper is intended for a private MWManager staging tree. It asks before packing when the
configured threshold is reached, keeps unsupported top-level folders loose, registers the archive
in the staging game's Morrowind.ini, and moves packed loose files into the mod's Off tree.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

DEFAULT_THRESHOLD = 10_000
DEFAULT_UNSUPPORTED = {"sound", "fonts", "splash", "music"}
PACKABLE_ROOTS = {"meshes", "textures", "icons"}


def read_unsupported(manager_dir: Path) -> set[str]:
    path = manager_dir / "mwmanager_bsa_unsupported_folders.txt"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return set(DEFAULT_UNSUPPORTED)
    return {line.strip().strip("/\\").casefold() for line in lines
            if line.strip() and not line.lstrip().startswith(";")}


def packable_files(mod_dir: Path, unsupported: set[str]) -> list[tuple[Path, Path]]:
    """Return (source, BSA-relative path), accepting either mod-root or nested Data Files layout."""
    out = []
    for path in mod_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(mod_dir)
        except ValueError:
            continue
        parts = relative.parts
        if parts and parts[0].casefold() == "data files":
            parts = parts[1:]
        if not parts or parts[0].casefold() not in PACKABLE_ROOTS or parts[0].casefold() in unsupported:
            continue
        out.append((path, Path(*parts)))
    return sorted(out, key=lambda item: str(item[0]).casefold())


def plugin_folder_name(mod_dir: Path) -> str:
    """Use the plugin shipped by this mod as the Off folder name, falling back to the mod folder."""
    candidates = sorted((p for p in mod_dir.rglob("*") if p.is_file()
                         and p.suffix.casefold() in {".esm", ".esp", ".omwaddon"}
                         and "off" not in {part.casefold() for part in p.relative_to(mod_dir).parts}),
                        key=lambda p: (p.name.casefold(), str(p).casefold()))
    return candidates[0].stem if candidates else mod_dir.name


def ask_to_pack(count: int) -> bool:
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        answer = messagebox.askyesno(
            "Pack large mod assets into a BSA",
            f"This mod has {count:,} BSA-compatible assets. Pack them into a TES3 BSA?\n\n"
            "The staged loose files will be moved into MWManager backups after the archive is built. "
            "Sound, Fonts, Splash, Music, and other configured folders stay loose.",
            parent=root)
        root.destroy()
        return answer
    except Exception:
        if not sys.stdin.isatty():
            print("  Packing needs an end-user choice; no archive was built.")
            return False
        try:
            return input(f"Pack {count:,} assets into a BSA? [y/N] ").strip().casefold() in {"y", "yes"}
        except (EOFError, OSError):
            return False


def _append_archive_ini(ini: Path, archive_name: str, backup_dir: Path) -> Path | None:
    raw = ini.read_bytes()
    text = raw.decode("cp1252", errors="replace")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    section = "archives"
    start = next((i for i, line in enumerate(lines)
                  if line.strip().startswith("[") and line.strip().endswith("]")
                  and line.strip()[1:-1].strip().casefold() == section), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[Archives]"])
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith("[") and lines[i].strip().endswith("]")), len(lines))
    existing = []
    pat = re.compile(r"^\s*Archive\s*(\d+)\s*=\s*(.*?)\s*$", re.I)
    for line in lines[start + 1:end]:
        match = pat.match(line)
        if match:
            existing.append((int(match.group(1)), match.group(2).strip().strip('"')))
    if archive_name.casefold() in {name.casefold() for _idx, name in existing}:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{ini.name}.mwmanager-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.bak"
    shutil.copy2(ini, backup)
    retained = [line for line in lines[start + 1:end] if not pat.match(line)]
    names = [name for _idx, name in sorted(existing)] + [archive_name]
    lines = lines[:start + 1] + retained + [f"Archive {i}={name}" for i, name in enumerate(names)] + lines[end:]
    tmp = ini.with_suffix(ini.suffix + ".mwmanager-tmp")
    tmp.write_bytes((newline.join(lines) + newline).encode("cp1252", errors="replace"))
    os.replace(tmp, ini)
    return backup


def build_for_mod(mod_dir: Path, data_files: Path, ini: Path, manager_dir: Path,
                  *, threshold: int = DEFAULT_THRESHOLD, dry_run: bool = False,
                  confirm=None) -> int:
    mod_dir = mod_dir.resolve()
    data_files = data_files.resolve()
    ini = ini.resolve()
    manager_dir = manager_dir.resolve()
    if not mod_dir.is_dir() or not data_files.is_dir() or not ini.is_file():
        print("mod-dir, data-files, and the staging Morrowind.ini must exist", file=sys.stderr)
        return 2
    if mod_dir == data_files or data_files not in mod_dir.parents:
        print("mod-dir must be a per-mod folder inside the staging Data Files directory", file=sys.stderr)
        return 2
    bsapack = manager_dir / "tools" / "bsapack.dej.exe"
    if not bsapack.is_file():
        print(f"BSAPack was not found: {bsapack}", file=sys.stderr)
        return 2
    unsupported = read_unsupported(manager_dir)
    files = packable_files(mod_dir, unsupported)
    count = len(files)
    print(f"{mod_dir.name}: {count:,} vanilla-readable assets eligible for BSA packing (threshold {threshold:,})")
    if count < threshold:
        print("  Below threshold; leaving the staged assets loose.")
        return 0
    bsa_name = f"mwmanager_{re.sub(r'[^A-Za-z0-9_-]+', '_', mod_dir.name).strip('_')[:48]}.bsa"
    print(f"  Unsupported folders stay loose: {', '.join(sorted(unsupported))}")
    proposed = data_files / bsa_name
    if proposed.exists() or any(data_files.glob(f"{proposed.stem}[0-9]*.bsa")):
        print(f"  Refusing to overwrite an existing archive for this mod: {proposed}", file=sys.stderr)
        return 2
    print(f"  Proposed archive: {proposed}")
    if dry_run:
        print("  Dry run: no prompt, archive, backup, or INI change.")
        return 0
    if not (confirm(count) if confirm else ask_to_pack(count)):
        print("  User declined packing; staged assets remain loose.")
        return 0
    import tempfile
    with tempfile.TemporaryDirectory(prefix="mwmanager-bsa-") as tmp_name:
        temp_root = Path(tmp_name)
        pack_root = temp_root / "pack"
        pack_root.mkdir()
        for source, relative in files:
            dest = pack_root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        out_base = data_files / bsa_name
        try:
            result = subprocess.run([str(bsapack), "pack", str(out_base)], cwd=pack_root,
                                    capture_output=True, text=True, errors="replace", check=False)
        except OSError as exc:
            print(f"  BSAPack could not start: {exc}", file=sys.stderr)
            return 2
        if result.returncode:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            print("  BSA build failed; loose assets were not moved.", file=sys.stderr)
            return result.returncode or 1
    stem = re.escape(Path(bsa_name).stem)
    archives = sorted((p for p in data_files.glob(f"{Path(bsa_name).stem}*.bsa")
                       if p.name.casefold() == bsa_name.casefold()
                       or re.fullmatch(stem + r"\d+\.bsa", p.name, re.I)),
                      key=lambda p: p.name.casefold())
    if not archives:
        print("  BSAPack reported success but created no BSA; loose assets were not moved.", file=sys.stderr)
        return 2
    # BSAPack can split output into a base archive and numbered parts. Validate every
    # part before registering any archive or moving source assets.
    for archive in archives:
        try:
            check = subprocess.run([str(bsapack), "list", str(archive)], cwd=data_files,
                                   capture_output=True, text=True, errors="replace", check=False)
            check_error = check.stderr or check.stdout or f"exit code {check.returncode}"
        except OSError as exc:
            check = None
            check_error = str(exc)
        if check is None or check.returncode:
            for generated in archives:
                try:
                    generated.unlink()
                except OSError:
                    pass
            print(f"  BSAPack validation failed for {archive.name}; generated archives removed and loose assets retained: "
                  f"{check_error}", file=sys.stderr)
            return 2
    try:
        plugin_name = plugin_folder_name(mod_dir)
        backups = mod_dir / "Off" / plugin_name / "bsa-pack"
        ini_backup_dir = backups / "ini"
        backup_ini = _append_archive_ini(ini, archives[0].name, ini_backup_dir)
        for archive in archives[1:]:
            backup_ini = _append_archive_ini(ini, archive.name, ini_backup_dir)
        moved = 0
        for source, _archive_relative in files:
            relative = source.relative_to(mod_dir)
            dest = backups / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
            moved += 1
    except OSError as exc:
        print(f"  BSA created, but staging cleanup or INI registration failed: {exc}", file=sys.stderr)
        print("  Original staged files are retained where they were not already moved.", file=sys.stderr)
        return 2
    print(f"  Built {len(archives)} BSA archive(s), registered them in {ini.name}, and backed up {moved:,} loose files.")
    if backup_ini is not None:
        print(f"  INI backup: {backup_ini}")
    print(f"  Asset backup: {backups}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pack a large staged Morrowind mod's compatible assets into a BSA after user approval.")
    parser.add_argument("mod_dir", help="that mod's extracted folder inside the staging Data Files directory")
    parser.add_argument("data_files", help="MWManager staging Data Files folder")
    parser.add_argument("--ini", required=True, help="staging game's Morrowind.ini")
    parser.add_argument("--manager-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return build_for_mod(Path(args.mod_dir), Path(args.data_files), Path(args.ini), Path(args.manager_dir),
                         threshold=args.threshold, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
