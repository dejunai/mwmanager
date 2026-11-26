r"""Shared test setup: tests use a temporary folder of their own (TEMP/TMP, inherited by the processes they
start), so their game locks never land in the user's shared %TEMP%\mwmanager-locks, where each temporary game
would leave a lock file for good."""
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mwdialog_paths  # noqa: E402


@pytest.fixture(autouse=True)
def _reap_tk_after_test():
    """Free every test's Tcl interpreters on this thread before the next test can call update()."""
    yield
    from tk_lifetime import reap_tk
    reap_tk()


@pytest.fixture(autouse=True)
def _private_temp(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir()
    for var in ("TEMP", "TMP"):
        monkeypatch.setenv(var, str(temp))
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    import mwdialog_paths as paths               # the copy this test sees (the root conftest may switch copies)
    monkeypatch.setattr(paths, "GAME_LOCK_DIR", str(temp / "mwmanager-locks"))
