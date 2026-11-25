"""One pytest run for MWManager's tests and the vendored upstream ones:

    python -m pytest tests tools/steam-upstream/tests

Both sides import the same module names (mwdialog_check, mwdialog_paths, vortex_tidy, conftest...) from different
folders, and MWManager composes its copies (mwmanager_upstream.compose). So each side keeps its own set of those
modules: the upstream set is what tools/steam-upstream/tests/conftest.py loads, and before each file is collected
and each test runs, sys.modules (and which of the two folders comes first on sys.path) is switched to that side's
set. Running either folder alone is unchanged: nothing is switched until the upstream conftest is loaded."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
UP = ROOT / "tools" / "steam-upstream"
UP_TESTS = UP / "tests"
NAMES = {p.stem for p in UP.glob("*.py")} | {"conftest"}
_sets = {"mwm": {}, "up": {}}
_state = {"side": "mwm", "split": False}


def _side(path):
    try:
        Path(path).resolve().relative_to(UP_TESTS)
    except ValueError:
        return "mwm"
    return "up"


def _switch(side):
    if not _state["split"] or _state["side"] == side:
        return
    held = _sets[_state["side"]]
    held.clear()
    for name in NAMES:
        if name in sys.modules:
            held[name] = sys.modules.pop(name)
    sys.modules.update(_sets[side])
    first, second = (str(UP), str(ROOT)) if side == "up" else (str(ROOT), str(UP))
    for p in (second, first):
        while p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    _state["side"] = side


def pytest_plugin_registered(plugin, manager):
    f = getattr(plugin, "__file__", None)
    if _state["split"] or not f:
        return
    if Path(f).resolve() == UP_TESTS / "conftest.py":
        _state["split"] = True
        _state["side"] = "up"                   # what is loaded now is what the upstream conftest loaded
        _switch("mwm")


def pytest_collectstart(collector):
    path = getattr(collector, "path", None)
    if path is not None and path != ROOT:
        _switch(_side(path))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    _switch(_side(item.path))


def pytest_sessionfinish(session):
    _switch("mwm")
