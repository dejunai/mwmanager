r"""Shared test setup: tests use a temporary folder of their own (TEMP/TMP, inherited by the processes they
start), so their game locks never land in the user's shared %TEMP%\mwmanager-locks, where each temporary game
would leave a lock file for good."""
import os
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


_REAL_LOCAL_APPDATA = os.path.normcase(os.path.abspath(
    os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")))


@pytest.fixture(autouse=True)
def _private_nexus_browser(tmp_path, monkeypatch):
    """No test reaches the user's real MWManager Nexus browser (%LOCALAPPDATA%\\MWManager\\NexusBrowser): a test
    that finishes an options dialog used to send the running browser a close command, and page reads would use its
    cache. While LOCALAPPDATA is still the real one, the browser folder is a private one under the test's tmp_path;
    a test that points LOCALAPPDATA at its own folder keeps that folder."""
    try:
        import mwnexus_browser
    except Exception:
        yield
        return
    original = mwnexus_browser._root
    private = tmp_path / "localappdata"

    def guarded_root():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        if os.path.normcase(os.path.abspath(base)) == _REAL_LOCAL_APPDATA:
            path = private / "MWManager" / "NexusBrowser"
            path.mkdir(parents=True, exist_ok=True)
            return path
        return original()
    monkeypatch.setattr(mwnexus_browser, "_root", guarded_root)
    yield
