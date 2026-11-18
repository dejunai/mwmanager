"""Run before a release: lists every developer-only marker (MARKER below) in the files git tracks (what ships).

    python ready_to_ship.py

Exit 0: none left, ready to ship. Exit 1: each one is listed as file:line so it can be removed.
Untracked local files (mwmanager.ini, outputs, staging) are not checked: they never ship.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MARKER = "REMOVE BEFORE " + "SHIPPING"          # split so this file does not find itself


def markers():
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    found = []
    for name in filter(None, files.decode("utf-8").split("\0")):
        try:
            text = (ROOT / name).read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue                                # binary or unreadable: no comments to find
        found += [f"{name}:{number}: {line.strip()}"
                  for number, line in enumerate(text.splitlines(), 1) if MARKER in line]
    return found


if __name__ == "__main__":
    left = markers()
    print("\n".join(left) if left else "Ready to ship: no developer-only markers left.")
    if left:
        print(f"\n{len(left)} marker(s) to remove before shipping.")
    sys.exit(1 if left else 0)
