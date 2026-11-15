"""Checkpoints for the one-time MWManager setup workflow, so an interrupted install resumes where it stopped.

mwmanager_setup_state.json, one entry per step, in order:

    "tidy_complete": {"started_at": "...", "completed_at": null, "attempts": 2, "pid": 4312,
                      "last_error": "Tidying Data Files failed (exit 1) ..."}

started_at without completed_at is a step that was interrupted (or is still running: pid is the process doing
it). attempts counts every start. last_error is what stopped the last attempt. complete() clears pid and the error.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

STEPS = ("paths_confirmed", "preflight_complete", "sort_complete", "tidy_complete",
         "initial_full_pass_complete", "initial_full_complete", "catalog_complete",
         "major_masters_started", "initialization_complete")
LABELS = {"paths_confirmed": "confirming the paths", "preflight_complete": "the preflight",
          "sort_complete": "sorting the shipped plugins", "tidy_complete": "the initial Tidy",
          "initial_full_pass_complete": "the initial --full pass",
          "initial_full_complete": "the initial --full cycle",
          "catalog_complete": "building the catalog database",
          "major_masters_started": "opening major-master staging",
          "initialization_complete": "the major-master step"}


def _game_key(game_dir) -> str:
    return str(Path(game_dir).resolve()).casefold()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _empty() -> dict:
    return {"started_at": None, "completed_at": None, "attempts": 0, "pid": None, "last_error": None}


def blank(game_dir) -> dict:
    return {"version": 2, "game_dir": str(Path(game_dir).resolve()), "steps": {step: _empty() for step in STEPS}}


def load(path, game_dir) -> dict:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return blank(game_dir)
    try:
        state = json.loads(text)
        if not isinstance(state, dict) or not isinstance(state.get("steps"), dict):
            raise ValueError("not a setup state file")
    except (ValueError, TypeError):
        # unreadable: keep it for a look instead of silently starting setup over on top of it
        try:
            os.replace(path, path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"))
        except OSError:
            pass
        return blank(game_dir)
    if _game_key(state.get("game_dir", "")) != _game_key(game_dir):
        return blank(game_dir)                    # another install: its record does not apply here
    for step in STEPS:
        entry = state["steps"].setdefault(step, _empty())
        for key, value in _empty().items():       # files written before version 2 only had completed_at
            entry.setdefault(key, value)
    return state


def _write(path, state) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _check(step: str) -> None:
    if step not in STEPS:
        raise ValueError(f"Unknown setup step: {step}")


def start(path, game_dir) -> dict:
    state = blank(game_dir)
    _write(path, state)
    return state


def begin(path, game_dir, step: str, pid: int | None = None) -> dict:
    """A step is starting (again): stamp it, count the attempt, remember the process doing it."""
    _check(step)
    state = load(path, game_dir)
    entry = state["steps"][step]
    entry.update(started_at=_now(), completed_at=None, attempts=int(entry.get("attempts") or 0) + 1,
                 pid=pid, last_error=None)
    _write(path, state)
    return state


def set_pid(path, game_dir, step: str, pid: int | None) -> None:
    _check(step)
    state = load(path, game_dir)
    state["steps"][step]["pid"] = pid
    _write(path, state)


def fail(path, game_dir, step: str, error) -> dict:
    _check(step)
    state = load(path, game_dir)
    state["steps"][step].update(pid=None, last_error=str(error)[:2000])
    _write(path, state)
    return state


def complete(path, game_dir, step: str) -> dict:
    _check(step)
    state = load(path, game_dir)
    entry = state["steps"][step]
    if not entry.get("completed_at"):
        entry.update(completed_at=_now(), pid=None, last_error=None)
        entry["started_at"] = entry.get("started_at") or entry["completed_at"]
        _write(path, state)
    return state


def reset(path, game_dir, step: str) -> None:
    """Take a completion back (the step has to run again)."""
    _check(step)
    state = load(path, game_dir)
    if state["steps"][step].get("completed_at"):
        state["steps"][step].update(completed_at=None, pid=None)
        _write(path, state)


def done(path, game_dir, step: str) -> bool:
    return bool(load(path, game_dir)["steps"].get(step, {}).get("completed_at"))


def interrupted(path, game_dir):
    """(step, its entry) for the first step that was started and never completed, else (None, None)."""
    state = load(path, game_dir)
    for step in STEPS:
        entry = state["steps"][step]
        if entry.get("started_at") and not entry.get("completed_at"):
            return step, entry
    return None, None


def running(path, game_dir):
    """(step, its entry) for an unfinished step whose recorded process is still alive, else (None, None)."""
    state = load(path, game_dir)
    for step in STEPS:
        entry = state["steps"][step]
        if not entry.get("completed_at") and entry.get("pid") and pid_alive(entry["pid"]):
            return step, entry
    return None, None


def pid_alive(pid) -> bool:
    """Is that process still running? (Never os.kill on Windows: there it ends the process.)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)             # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102       # WAIT_TIMEOUT: not ended
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def end_process(pid) -> None:
    """End a process and what it started (a setup step left running by a closed MWManager)."""
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True)
        if result.returncode:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0001, False, int(pid))       # PROCESS_TERMINATE
            if handle:
                try:
                    kernel32.TerminateProcess(handle, 1)
                finally:
                    kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(int(pid), 15)
        except OSError:
            pass
