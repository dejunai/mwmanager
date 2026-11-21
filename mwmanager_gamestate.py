"""Per-game decisions. MWManager keeps its answers and rules in its own folder whatever game it works on; when it
is pointed at a different game folder, the last game's decisions are set aside in game_states\\<game> and that
game's own (if it had any) come back, so a fresh install never starts with another install's choices."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

OWNER = "mwmanager_state_owner.json"
STATES = "game_states"
# what the user decided for one install: answers, rules, orders, records of what was merged or disabled
GAME_STATE = (
    "mwmanager_sort_order.txt", "mwmanager_rules.txt", "mwmanager_disabled.txt",
    "mwmanager_conflict_answers.json", "mwmanager_cell_winners.txt", "mwmanager_conflict_blacklist.txt",
    "mwmanager_merged.txt", "mwmanager_merge_rebuild.txt", "mwmanager_added_masters.json",
    "mwmanager_loadorder_undo.txt", "mwmanager_dates_undo.txt",
    "outputs/mwmanager_pending_options.json", "outputs/fomod_answers",
)


def _key(game_dir) -> str:
    return os.path.normcase(os.path.abspath(str(game_dir))).rstrip("\\/")


def _folder(game_dir) -> str:
    """game_states\\ folder name: readable, and unique per path."""
    path = _key(game_dir)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(path).name or "game").strip("._") or "game"
    return f"{name}-{hashlib.sha256(path.encode('utf-8')).hexdigest()[:8]}"


def _move(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    shutil.move(str(source), str(target))


def use_game(manager_dir, game_dir) -> str | None:
    """Make the decisions in `manager_dir` the ones for `game_dir`. Returns a line saying what moved, or None."""
    manager_dir = Path(manager_dir)
    owner_path = manager_dir / OWNER
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8")).get("game_dir")
    except (OSError, ValueError, AttributeError):
        owner = None
    if owner and _key(owner) == _key(game_dir):
        return None
    message = None
    if owner:                                  # another game's decisions: set them aside
        aside = manager_dir / STATES / _folder(owner)
        moved = [rel for rel in GAME_STATE if (manager_dir / rel).exists()]
        for rel in moved:
            _move(manager_dir / rel, aside / rel)
        (aside / OWNER).parent.mkdir(parents=True, exist_ok=True)
        (aside / OWNER).write_text(json.dumps({"game_dir": owner}, indent=2) + "\n", encoding="utf-8")
        back = manager_dir / STATES / _folder(game_dir)
        restored = [rel for rel in GAME_STATE if (back / rel).exists()]
        for rel in restored:
            _move(back / rel, manager_dir / rel)
        message = (f"Switched MWManager's answers and rules from {owner} to {game_dir}: "
                   f"{len(moved)} set aside, {len(restored)} brought back"
                   + ("" if restored else " (a fresh start for this game)") + ".")
    owner_path.write_text(json.dumps({"game_dir": str(game_dir)}, indent=2) + "\n", encoding="utf-8")
    return message
