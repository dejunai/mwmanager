#!/usr/bin/env python3
"""Replace a deployed exclusive master variant (Tamriel_Data HD/SD) in place.

Usage:
  python swap_master_variant.py --manager-dir DIR --game-dir DIR --downloads-dir DIR
                                --master Tamriel_Data.esm --label SD
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Swap a deployed exclusive master variant without undeploying its dependents.")
    parser.add_argument("--manager-dir", required=True, help="MWManager folder (settings, staging, journal)")
    parser.add_argument("--game-dir", required=True, help="Morrowind game folder (contains Morrowind.ini)")
    parser.add_argument("--downloads-dir", required=True, help="Folder that already holds both variant archives")
    parser.add_argument("--master", default="Tamriel_Data.esm")
    parser.add_argument("--label", required=True, help="Variant to switch to, for example HD or SD")
    parser.add_argument("--keep-old-stage", action="store_true",
                        help="Leave the previous variant's staged package in place")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core_addons

    def progress(message: str) -> None:
        print(message, flush=True)

    result = core_addons.swap_master_variant(
        args.manager_dir, args.game_dir, args.master, args.label,
        downloads_dir=args.downloads_dir, progress_callback=progress,
        unstage_old=not args.keep_old_stage)
    print("swap_master_variant:", {key: result.get(key) for key in
                                   ("choice", "ready", "replaced", "linked", "removed",
                                    "archives", "unregistered", "unstaged", "new_package")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
