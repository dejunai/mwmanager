"""Persistent, interactive Nexus browser for MWManager's FOMOD helper.

The browser is a WebView2 window in a small child process. MWManager's own page reads
(capture_page_background) run hidden: no window appears and focus never moves; the window
is shown only when the user opens the browser (open_page) or a download needs them. Its profile
is private to MWManager and persistent, so users can sign in, complete a
CAPTCHA and download files without exposing or copying cookies into the Python
application. Page text is handed back to MWManager over an atomic local-file
mailbox; credentials and cookies are never read by MWManager.
"""
from __future__ import annotations

import json
import importlib.util
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse


def _root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    path = base / "MWManager" / "NexusBrowser"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _valid_nexus_url(url: str) -> bool:
    parsed = urlparse(str(url))
    return parsed.scheme == "https" and parsed.hostname in {"www.nexusmods.com", "nexusmods.com"}


def _valid_browser_url(url: str) -> bool:
    parsed = urlparse(str(url))
    return parsed.scheme == "https" and parsed.hostname in {
        "www.nexusmods.com", "nexusmods.com", "github.com", "www.github.com",
    }


class NexusCheckNeeded(RuntimeError):
    """Nexus showed its security check (a CAPTCHA) instead of the page: only the user can pass it, once, in the
    visible MWManager browser."""


class _HiddenChallenge(RuntimeError):
    """Browser side: the hidden reader met a security check that did not clear by itself."""


CHECK_NEEDED_STATUS = ("Nexus needs a quick check: open the Nexus page in the MWManager browser once and complete "
                       "it, then retry.")
BACKGROUND_INTERVAL = 2.0      # seconds between hidden page reads (rate limit)
BACKGROUND_TIMEOUT = 75.0      # seconds one hidden read may take, browser start-up included
CHECK_RETRY_AFTER = 15 * 60.0  # once Nexus asked for its check, hidden reads pause this long or until open_page
HIDDEN_CHALLENGE_WAIT = 20.0   # a saved clearance passes Nexus's check unattended within this
HIDDEN_IDLE_CLOSE = 300.0      # a browser nobody can see closes after this long without work
_BACKGROUND_LOCK = threading.Lock()
_BACKGROUND = {"last": 0.0, "check_needed_at": 0.0}


def check_needed() -> bool:
    """True while hidden reads are paused because Nexus asked for its security check."""
    at = _BACKGROUND["check_needed_at"]
    return bool(at) and time.monotonic() - at < CHECK_RETRY_AFTER


def _pid_running(pid: int) -> bool:
    """Liveness check without signalling the process.

    On Windows ``os.kill(pid, 0)`` is not a probe: 0 is CTRL_C_EVENT, which is
    delivered to every process sharing the caller's console. Query the exit
    code instead.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # an exited-but-unreaped child (zombie) is not running
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _process_identity(pid: int):
    """(image_path, creation_epoch_seconds) for pid, or None if unavailable."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                            ctypes.byref(kernel), ctypes.byref(user)):
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return buffer.value, ticks / 10_000_000 - 11_644_473_600
        finally:
            kernel32.CloseHandle(handle)
    try:  # Linux (tests): exe path and start time from procfs
        proc = Path(f"/proc/{pid}")
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])
        boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime "))
        return os.readlink(proc / "exe"), boot + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _tracked_browser_pid() -> int:
    """PID of the browser MWManager launched, or 0.

    Only trusts browser.pid when that process is alive, is a Python
    interpreter, and was created before the pid file was written (so a
    recycled PID belonging to some other program is never treated as ours).
    """
    pid_file = _root() / "browser.pid"
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
        written = pid_file.stat().st_mtime
    except (OSError, ValueError):
        return 0
    if not _pid_running(pid):
        return 0
    identity = _process_identity(pid)
    if identity is None:
        return 0  # Unknown ownership is never permission to terminate a process.
    image, created = identity
    if not Path(image).name.casefold().startswith("python"):
        return 0
    if created > written + 0.05:
        return 0
    return pid


def _terminate_pid(pid: int) -> bool:
    """Hard-stop one specific process (the tracked browser only)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x0001 | 0x1000, False, pid)  # TERMINATE | QUERY_LIMITED
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _write_downloads(root: Path, state: dict) -> None:
    _write_json(root / "downloads.json", {"active": sorted(state["active"]), "progress": state["progress"],
                                          "finished": state["finished"], "failed": state["failed"]})


def _track_download(root: Path, state: dict, target: str, operation) -> None:
    """Record a WebView2 download in downloads.json: progress while running, then finished or failed."""
    state["active"].add(target)
    state["progress"][target] = [0, 0]
    _write_downloads(root, state)
    last = {"write": 0.0}

    def received(sender, _event):
        try:
            state["progress"][target] = [int(sender.BytesReceived), int(sender.TotalBytesToReceive or 0)]
        except (TypeError, ValueError):
            return
        if time.monotonic() - last["write"] > 0.5:          # every chunk would rewrite the file thousands of times
            last["write"] = time.monotonic()
            _write_downloads(root, state)

    def changed(sender, _event):
        status = str(sender.State)
        if "InProgress" in status:
            return
        state["active"].discard(target)
        state["finished" if "Completed" in status else "failed"].append(target)
        _write_downloads(root, state)

    operation.BytesReceivedChanged += received
    operation.StateChanged += changed


def active_downloads() -> list[str]:
    """Files the running MWManager browser is still downloading (closing it would cancel them)."""
    if not _tracked_browser_pid():
        return []
    return list(_read_json(_root() / "downloads.json").get("active", []))


# Runs on a Nexus file page (?tab=files&file_id=N): reports the page and where its download button is ("Slow
# download" on the older page, "Start download" on the current one). The browser then clicks it from script; if
# no download starts within 15 s it also sends a trusted DevTools mouse click there (CR-25 stress run: the button
# sits in a shadow root; the script click starts the download, a DevTools click into the hidden window does not).
DOWNLOAD_JS = r"""
(() => {
  const text = ((document.body && document.body.innerText) || '').slice(0, 20000);
  const clicked = window.__mwmClicked || '';
  let target = null;
  if (!clicked && document.readyState === 'complete') {
    // the current page draws its button inside a web component (<mod-file-download>'s shadow root), which
    // document.querySelectorAll does not reach: walk open shadow roots too (found by the CR-25 stress run)
    const deep = (root, sel, out) => {
      for (const e of root.querySelectorAll('*')) {
        if (e.matches(sel)) out.push(e);
        if (e.shadowRoot) deep(e.shadowRoot, sel, out);
      }
      return out;
    };
    const visible = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const label = e => (e.innerText || e.textContent || '').trim();
    const els = deep(document, 'button, a, [role=button], #slowDownloadButton', []).filter(visible);
    const el = els.find(e => e.id === 'slowDownloadButton') || els.find(e => /^(slow|start) download$/i.test(label(e)));
    if (el) {
      el.scrollIntoView({block: 'center', inline: 'center'});
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0)
        target = {x: r.left + r.width / 2, y: r.top + r.height / 2, label: label(el) || 'slow download'};
      window.__mwmButton = el;
    }
  }
  return JSON.stringify({url: location.href, title: document.title, readyState: document.readyState,
                         text: text, clicked: clicked, target: target});
})()
"""

# Marks the button as clicked; with click=true it also clicks it from script (the normal path).
MARK_CLICKED_JS = r"""
((fallback) => {
  const el = window.__mwmButton;
  if (!el) return '';
  const label = (el.innerText || el.textContent || '').trim() || 'slow download';
  if (fallback) el.click();
  window.__mwmClicked = label;
  return label;
})(%s)
"""


def trusted_click_events(x: float, y: float) -> list[dict]:
    """DevTools Input.dispatchMouseEvent parameters for one left click at viewport point (x, y)."""
    x, y = round(float(x), 1), round(float(y), 1)
    return [{"type": "mouseMoved", "x": x, "y": y, "button": "none"},
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1}]


def download_file(url: str, download_dir, progress=None, timeout: float = 4 * 3600) -> Path:
    """Download a Nexus file in the background browser; it shows itself only for a security check or login.

    Returns the finished file. The browser stays open (hidden): call close_browser() afterwards.
    """
    progress = progress or (lambda _message: None)
    if not _valid_nexus_url(url):
        raise ValueError("Only an approved Nexus HTTPS page can be downloaded from.")
    if importlib.util.find_spec("webview") is None:
        raise RuntimeError("MWManager's embedded browser is not installed (requirements-nexus-browser.txt).")
    root = _root()
    request_id = secrets.token_hex(16)
    command = {"id": request_id, "action": "download", "url": url,
               "download_dir": str(Path(download_dir).resolve())}
    if _tracked_browser_pid():
        _write_json(root / "command.json", command)
    else:
        _launch_if_needed(url, command)
    deadline, last_message = time.monotonic() + timeout, ""
    while time.monotonic() < deadline:
        response = _read_json(root / "response.json")
        if response.get("id") == request_id:
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            if response.get("file"):
                return Path(response["file"])
            message = str(response.get("message", ""))
            if message and message != last_message:
                last_message = message
                progress(message)
        if not _tracked_browser_pid():
            raise RuntimeError("The MWManager browser was closed before the download finished.")
        time.sleep(0.3)
    raise RuntimeError("The download did not finish in time.")


def close_browser(timeout: float = 5.0, force: bool = True) -> str:
    """Close the MWManager Nexus browser gracefully; terminate it only if it is wedged.

    Returns "not-running", "closed", "terminated" or "still-running".
    The persistent webview2-profile (Nexus sign-in / security-check cookies) is
    never touched: a graceful close lets WebView2 flush it, and the fallback only
    ends the tracked browser process.
    """
    root = _root()
    pid_file = root / "browser.pid"
    pid = _tracked_browser_pid()
    if not pid:
        if pid_file.exists() and not _pid_running(_read_pid(pid_file)):
            pid_file.unlink(missing_ok=True)  # stale file from a crashed/terminated browser
        return "not-running"
    request_id = secrets.token_hex(12)
    _write_json(root / "command.json", {"id": request_id, "action": "close"})
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            return "closed"
        time.sleep(0.1)
    if not _pid_running(pid):
        return "closed"
    if not force:
        return "still-running"
    # It ignored the close request for `timeout` seconds: its message loop is stuck.
    # Re-check identity right before the hard stop, then end only that process.
    if _tracked_browser_pid() != pid:
        return "closed" if not _pid_running(pid) else "still-running"
    _terminate_pid(pid)
    for _ in range(30):
        if not _pid_running(pid):
            break
        time.sleep(0.1)
    if not _pid_running(pid):
        pid_file.unlink(missing_ok=True)
        return "terminated"
    return "still-running"


def close_browser_in_background(timeout: float = 5.0):
    """Start close_browser on a worker thread so dialogs never wait on the browser.

    The thread is non-daemon so a short-lived caller still finishes the
    (bounded, ~timeout+3 s) fallback before the interpreter exits.
    """
    import threading
    result = {}

    def worker():
        try:
            result["status"] = close_browser(timeout=timeout)
        except Exception as exc:  # never let cleanup surface as an install error
            result["status"] = f"error: {exc}"

    thread = threading.Thread(target=worker, name="mwnexus-browser-close", daemon=False)
    thread.start()
    thread.result = result
    return thread


def _read_pid(pid_file: Path) -> int:
    try:
        return int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def _startup_error() -> str:
    value = _read_json(_root() / "browser-error.json")
    return str(value.get("error", "")).strip()


def _launch_if_needed(url: str, command: dict | None = None) -> None:
    root = _root()
    pid_file = root / "browser.pid"
    if _tracked_browser_pid():
        return
    pid_file.unlink(missing_ok=True)
    (_root() / "browser-error.json").unlink(missing_ok=True)
    command = command or {"id": secrets.token_hex(12), "action": "open", "url": url}
    _write_json(root / "command.json", command)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--embedded-browser"],
        cwd=str(Path(__file__).resolve().parent), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
        close_fds=True,
    )
    # Report fast startup failures in the FOMOD dialog instead of silently
    # returning as if the browser window had opened.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        detail = _startup_error()
        if detail:
            raise RuntimeError(f"The MWManager browser could not start: {detail}")
        if process.poll() is not None:
            detail = _startup_error()
            raise RuntimeError(detail or "The MWManager browser process exited before opening its window.")
        try:
            if int(pid_file.read_text(encoding="ascii").strip()) == process.pid:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.05)


def open_page(url: str, download_dir=None) -> None:
    """Open an approved prerequisite page and optionally download directly into its component folder."""
    if not _valid_browser_url(url):
        raise ValueError("Only approved HTTPS prerequisite pages can be opened in the MWManager browser.")
    if importlib.util.find_spec("webview") is None:
        raise RuntimeError(
            "MWManager's embedded browser is not installed yet. Install the packages listed in "
            "requirements-nexus-browser.txt, then reopen MWManager."
        )
    _BACKGROUND["check_needed_at"] = 0.0   # the user opened the browser: Nexus's check can be passed there
    root = _root()
    command = {"id": secrets.token_hex(12), "action": "open", "url": url,
               "download_dir": str(Path(download_dir).resolve()) if download_dir else ""}
    if _tracked_browser_pid():
        _write_json(root / "command.json", command)
    else:
        _launch_if_needed(url, command)


def _challenge_page(title: str, text: str) -> bool:
    from mwnexus_parse import is_challenge
    return is_challenge(title, text)


def _capture_ready(page: dict, elapsed: float) -> bool:
    if page.get("readyState") != "complete":
        return False
    text, url = str(page.get("text", "")), str(page.get("url", ""))
    return (len(str(page.get("description", ""))) > 200
            or ("tab=files" in url and "\nMain files\n" in text)
            or elapsed > 15)


def _checked_mod_id(mod_id) -> str:
    mod_id = str(mod_id).strip()
    if not mod_id.isdigit() or int(mod_id) <= 0:
        raise ValueError("A valid Nexus mod ID is required.")
    return mod_id


def capture_page(mod_id: str, url: str | None = None) -> dict:
    """Request readable DOM text from the browser the user has open, optionally navigating to the matching page."""
    mod_id = _checked_mod_id(mod_id)
    if url is not None and not _valid_nexus_url(url):
        raise ValueError("Only an approved Nexus HTTPS page can be captured.")
    root = _root()
    pid = _tracked_browser_pid()
    if not pid:
        detail = _startup_error()
        if detail:
            raise RuntimeError(f"The MWManager browser could not start: {detail}")
        raise RuntimeError("The MWManager browser is closed. Click Open Nexus in MWManager browser first.")

    request_id = secrets.token_hex(16)
    command = {"id": request_id, "action": "capture", "mod_id": mod_id}
    if url is not None:
        command["url"] = url
    _write_json(root / "command.json", command)
    return _await_capture(root, request_id, pid)


def _wait_for_browser_pid(limit: float) -> int:
    deadline = time.monotonic() + limit
    while True:
        pid = _tracked_browser_pid()
        if pid or time.monotonic() >= deadline:
            return pid
        time.sleep(0.1)


def capture_page_background(mod_id: str, url: str, timeout: float = BACKGROUND_TIMEOUT, force: bool = False) -> dict:
    """Read a Nexus page in MWManager's browser without showing anything.

    The browser keeps the user's saved Nexus clearance, so it loads pages a plain HTTP request is refused (403).
    When it is not running it starts hidden; when the user has it open, the read happens in a second hidden window
    so their page is left alone. One read at a time, at most one every BACKGROUND_INTERVAL seconds, each bounded by
    `timeout`. If Nexus shows a real CAPTCHA, nothing pops up: NexusCheckNeeded (CHECK_NEEDED_STATUS) is raised and
    further hidden reads pause until the user opens the browser (`force` tries anyway).
    """
    mod_id = _checked_mod_id(mod_id)
    if not _valid_nexus_url(url):
        raise ValueError("Only an approved Nexus HTTPS page can be captured.")
    if check_needed() and not force:
        raise NexusCheckNeeded(CHECK_NEEDED_STATUS)
    if importlib.util.find_spec("webview") is None:
        raise RuntimeError("MWManager's embedded browser is not installed (requirements-nexus-browser.txt).")
    started = time.monotonic()
    if not _BACKGROUND_LOCK.acquire(timeout=max(0.0, timeout)):
        raise RuntimeError("Another hidden Nexus page read did not finish in time.")
    try:
        pause = _BACKGROUND["last"] + BACKGROUND_INTERVAL - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        root = _root()
        pid = _tracked_browser_pid()
        if not pid:
            _launch_if_needed(url, {"id": secrets.token_hex(12), "action": "open", "url": url, "hidden": True})
            pid = _wait_for_browser_pid(15.0)
            if not pid:
                raise RuntimeError(_startup_error() or "The hidden MWManager browser did not start.")
        request_id = secrets.token_hex(16)
        _write_json(root / "command.json", {"id": request_id, "action": "capture", "mod_id": mod_id,
                                            "url": url, "background": True})
        remaining = max(10.0, timeout - (time.monotonic() - started))
        try:
            page = _await_capture(root, request_id, pid, first_reply=min(30.0, remaining), limit=remaining)
        except NexusCheckNeeded as exc:
            _BACKGROUND["check_needed_at"] = time.monotonic()
            raise NexusCheckNeeded(CHECK_NEEDED_STATUS) from exc
        _BACKGROUND["check_needed_at"] = 0.0
        return page
    finally:
        _BACKGROUND["last"] = time.monotonic()
        _BACKGROUND_LOCK.release()


def _await_capture(root: Path, request_id: str, pid: int, first_reply: float = 15.0, limit: float = 75.0) -> dict:
    response_file = root / "response.json"
    # The browser answers with {"state": "working"} heartbeats while it waits for
    # Nexus to render the description, then with the final page or an error.
    started = time.monotonic()
    deadline = started + first_reply  # until the first heartbeat
    while time.monotonic() < deadline:
        response = _read_json(response_file)
        if response.get("id") == request_id:
            if response.get("state") == "working":
                deadline = max(deadline, min(started + limit, time.monotonic() + 15))
                time.sleep(0.2)
                continue
            if response.get("error"):
                if response.get("challenge"):
                    raise NexusCheckNeeded(str(response["error"]))
                raise RuntimeError(str(response["error"]))
            page = response.get("page") or {}
            if not isinstance(page, dict):
                break
            from mwnexus_parse import page_text_from_capture
            text = page_text_from_capture(page)
            if _challenge_page(str(page.get("title", "")), str(page.get("text", ""))):
                raise NexusCheckNeeded(
                    "Nexus is still showing its security check. Complete it in the MWManager browser, "
                    "then click Read page from browser again."
                )
            if len(text) < 80:
                raise RuntimeError("The open page has too little readable text. Wait for Nexus to finish loading and retry.")
            return {"url": page.get("url", ""), "title": page.get("title", ""),
                    "selector": page.get("selector", ""), "description": str(page.get("description", ""))[:60000],
                    "about": page.get("about", ""), "text": text[:150000]}
        if not _pid_running(pid):
            raise RuntimeError("The MWManager browser window was closed before the page was read.")
        time.sleep(0.2)
    raise RuntimeError(
        "The MWManager browser did not respond. If its window says (Not Responding), close it and click "
        "Open Nexus in MWManager browser again, then retry."
    )


def cache_page(mod_id: str, page: dict) -> None:
    """Persist rendered page text for later option descriptions."""
    root = _root() / "pages"
    destination = root / f"{int(mod_id)}.json"
    _write_json(destination, {**page, "cached_at": time.time()})


def cached_page(mod_id: str) -> dict | None:
    try:
        value = _read_json(_root() / "pages" / f"{int(mod_id)}.json")
        return value if value.get("text") else None
    except (OSError, ValueError, TypeError):
        return None


GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = -20, 0x80, 0x40000, 0x8000000


def quiet_style(style: int, quiet: bool) -> int:
    """Extended window style for hidden work (never activated, no taskbar button) or for the user's window."""
    if quiet:
        return (style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    return (style | WS_EX_APPWINDOW) & ~(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


def _set_quiet(window, quiet: bool) -> None:
    """Apply quiet_style to a pywebview window's form. Style bits only: the window handle (and the WebView2 inside
    it) stays the same, unlike WinForms' ShowInTaskbar, which recreates the handle."""
    import ctypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowLongW.argtypes = (ctypes.c_void_p, ctypes.c_int)
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_long)
    window.focus = not quiet          # pywebview re-adds WS_EX_NOACTIVATE on activation while focus is False
    hwnd = ctypes.c_void_p(int(window.native.Handle.ToInt64()))
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    new = quiet_style(style, quiet)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ctypes.c_long(new - (1 << 32) if new >= 1 << 31 else new).value)


READER_TITLE = "MWManager - Nexus reader"


def blocks_activation(hwnd: int, title: str, main_hwnd: int, main_quiet: bool) -> bool:
    """Whether the activation guard refuses this window: the hidden reader always, the main window while it only
    does MWManager's hidden work. The user's own windows and dialogs are never refused."""
    return title == READER_TITLE or (main_quiet and bool(main_hwnd) and hwnd == main_hwnd)


def _install_activation_guard(should_block):
    """A CBT hook on the browser's GUI thread (this process only) that refuses activation for `should_block(hwnd)`
    windows. pywebview shows a hidden window once at opacity 0 to set it up; WS_EX_NOACTIVATE alone did not stop
    that Show() from taking the foreground for a moment (measured live), the hook does. Keep the return value."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hook_proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = (ctypes.c_int, hook_proc, wintypes.HINSTANCE, wintypes.DWORD)
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    wh_cbt, hcbt_activate = 5, 5

    def proc(code, wparam, lparam):
        try:
            if code == hcbt_activate and should_block(int(wparam)):
                return 1
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    callback = hook_proc(proc)
    hook = user32.SetWindowsHookExW(wh_cbt, callback, None, kernel32.GetCurrentThreadId())
    return hook, callback


def _window_title(hwnd: int) -> str:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buffer, 256)
    return buffer.value


def _navigate(target, url: str) -> None:
    """load_url, except to the page the window already shows: WebView2 ignores a Source equal to its current one,
    so no load ever completes and pywebview's next evaluate_js waits forever (a second hidden read of one page
    timed out live). The page is already there, so it is read as it is."""
    browser = getattr(getattr(target, "native", None), "browser", None)
    current = str(getattr(browser, "url", "") or "")
    if current and current.rstrip("/") == str(url).rstrip("/"):
        return
    target.load_url(url)


def _add_navigation_bar(window) -> None:
    """Back and Forward buttons in a strip above the page (outside it, so page reads never see them), each enabled
    only while WebView2 has history that way. Alt+Left / Alt+Right keep working as in Edge."""
    from System import Func, Object
    from System.Windows.Forms import DockStyle, ToolStrip, ToolStripButton, ToolStripGripStyle
    form = window.native
    control = form.browser.webview

    def build():
        bar = ToolStrip()
        bar.Dock = DockStyle.Top
        bar.GripStyle = ToolStripGripStyle.Hidden
        back = ToolStripButton("\u2190 Back")
        forward = ToolStripButton("Forward \u2192")
        back.ToolTipText = "Previous page (Alt+Left)"
        forward.ToolTipText = "Next page (Alt+Right)"

        def refresh(*_args):
            back.Enabled = bool(control.CanGoBack)
            forward.Enabled = bool(control.CanGoForward)

        def go_back(*_args):
            if control.CanGoBack:
                control.GoBack()

        def go_forward(*_args):
            if control.CanGoForward:
                control.GoForward()

        back.Click += go_back
        forward.Click += go_forward
        bar.Items.Add(back)
        bar.Items.Add(forward)
        form.Controls.Add(bar)
        control.BringToFront()        # WinForms docks back to front: the page fills what the strip leaves
        control.CoreWebView2.HistoryChanged += refresh
        refresh()
        return None

    form.Invoke(Func[Object](build))


def _run_embedded_browser() -> None:
    """Child process entry point for the Edge WebView2 browser and its mailbox."""
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError(
            "The embedded browser dependency is missing. Install MWManager's browser requirements "
            "(pywebview) and retry."
        ) from exc

    # Nexus downloads must stay in this visible, cookie-bearing browser so the
    # user can complete any security check before WebView2 shows Save As.
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False

    root = _root()
    profile = root / "webview2-profile"
    profile.mkdir(parents=True, exist_ok=True)
    pid_file = root / "browser.pid"
    error_file = root / "browser-error.json"
    error_file.unlink(missing_ok=True)
    _write_json(root / "response.json", {"id": "", "error": ""})
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    command_file = root / "command.json"
    last_command = ""
    closed = {"value": False}
    downloads = {"active": set(), "progress": {}, "finished": [], "failed": []}
    _write_downloads(root, downloads)

    class BrowserApi:
        # pywebview exposes every *public* attribute of js_api to the page and
        # walks it recursively on each page load. Keep the window reference
        # private: a public ``window`` attribute made pywebview recurse through
        # window.native (the WinForms Form, e.g. AccessibilityObject.Bounds.Empty...)
        # with cross-thread UI calls, freezing the browser ("Not Responding")
        # and leaving every capture request unanswered.
        def __init__(self):
            self._window = None

        def navigate(self, url):
            if _valid_browser_url(url):
                self._window.load_url(url)
                return True
            return False

    api = BrowserApi()
    initial = _read_json(command_file)
    # a first "download" or "capture" command still has to run once the window exists; "open" is done by loading
    # its URL
    last_command = "" if initial.get("action") in ("download", "capture") else str(initial.get("id", ""))
    # started for MWManager's own work (a hidden read or a download): no window until the user asks for one
    start_hidden = initial.get("action") == "download" or bool(initial.get("hidden") or initial.get("background"))
    view = {"visible": not start_hidden, "reader": None, "busy": 0, "activity": time.monotonic(), "nav": False}
    url = initial.get("url", "https://www.nexusmods.com/morrowind/")
    if not _valid_browser_url(url):
        url = "https://www.nexusmods.com/morrowind/"
    download_folder = {"path": Path(initial["download_dir"]) if initial.get("download_dir") else None}
    if download_folder["path"]:
        download_folder["path"].mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        from webview.platforms import edgechromium
        original_download_handler = edgechromium.EdgeChrome.on_download_starting

        # shortcut: pywebview has no public download-directory API; remove this patch when it gains one.
        def download_to_component(self, sender, args):
            folder = download_folder["path"]
            if folder is None:
                return original_download_handler(self, sender, args)
            if not webview.settings["ALLOW_DOWNLOADS"]:
                args.Cancel = True
                return
            target = folder / Path(str(args.ResultFilePath)).name
            if target.exists():
                stem, suffix, number = target.stem, target.suffix, 2
                while target.exists():
                    target = folder / f"{stem} ({number}){suffix}"
                    number += 1
            args.ResultFilePath = str(target)
            _track_download(root, downloads, str(target), args.DownloadOperation)

        edgechromium.EdgeChrome.on_download_starting = download_to_component
    window = webview.create_window(
        "MWManager — Prerequisite Browser", url=url, js_api=api,
        width=1180, height=820, min_size=(760, 520), confirm_close=False,
        hidden=start_hidden, focus=not start_hidden,
    )
    api._window = window
    view["quiet"] = start_hidden and os.name == "nt"

    def guard_activation():
        # GUI thread, before the first Show(): from here on hidden windows cannot take the foreground
        view["main_hwnd"] = int(window.native.Handle.ToInt64())
        view["guard"] = _install_activation_guard(lambda hwnd: blocks_activation(
            hwnd, _window_title(hwnd), view["main_hwnd"], view["quiet"]))

    if os.name == "nt":
        window.events.before_show += guard_activation
    if view["quiet"]:
        def quiet_from_the_start():
            # GUI thread, between creating the form and pywebview's hidden Show()/Hide() (drawn at opacity 0).
            # Setting Opacity rewrites the extended style, so the quiet bits go on again in Load, which WinForms
            # raises inside Show() just before the window appears: never activated, no taskbar button.
            _set_quiet(window, True)
            window.native.Load += lambda _sender, _args: _set_quiet(window, True)

        window.events.before_show += quiet_from_the_start

    def add_navigation():
        if view["nav"] or os.name != "nt":
            return
        view["nav"] = True
        try:
            _add_navigation_bar(window)
        except Exception as exc:  # the browser still works without the buttons
            _write_json(root / "navigation-error.json", {"error": str(exc), "time": time.time()})

    window.events.loaded += add_navigation

    import threading
    from mwnexus_parse import CAPTURE_JS, decode_script_result, is_challenge

    def run_script(script: str, timeout: float, target=None):
        """evaluate_js on a helper thread so a stalled page never wedges the mailbox loop."""
        box = {}
        target = target or window

        def worker():
            try:
                box["value"] = target.evaluate_js(script)
            except Exception as exc:  # JavascriptException, WebViewException
                box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            return None, "timeout"
        if "error" in box:
            return None, str(box["error"])
        return decode_script_result(box.get("value")), ""

    def capture(command_id: str, mod_id: str, target=None, background: bool = False):
        target = target or window
        expected = re.compile(r"/mods/" + re.escape(mod_id) + r"(?:/|$)")
        started = time.monotonic()
        deadline = started + 45
        page, detail, challenge_since = None, "", 0.0
        while time.monotonic() < deadline and not closed["value"]:
            _write_json(root / "response.json", {"id": command_id, "state": "working"})
            page, detail = run_script(CAPTURE_JS, 12, target)
            if page:
                parsed = urlparse(str(page.get("url", "")))
                if parsed.hostname not in {"www.nexusmods.com", "nexusmods.com"} \
                        or not expected.search(parsed.path.rstrip("/") + "/"):
                    if background and time.monotonic() - started < 15:
                        page = None          # the hidden reader is still leaving its previous page
                        time.sleep(1.0)
                        continue
                    raise RuntimeError(
                        "Open the matching Nexus mod page in the MWManager browser before reading it."
                    )
                if is_challenge(str(page.get("title", "")), str(page.get("text", ""))):
                    if not background:
                        raise RuntimeError(
                            "Nexus is still showing its security check. Complete it in the MWManager "
                            "browser, then click Read page from browser again."
                        )
                    # a saved clearance passes the check by itself within seconds; a real CAPTCHA does not
                    challenge_since = challenge_since or time.monotonic()
                    if time.monotonic() - challenge_since > HIDDEN_CHALLENGE_WAIT:
                        raise _HiddenChallenge("Nexus showed its security check to the hidden reader ("
                                               + str(page.get("title", "")).strip() + ").")
                    page = None
                    time.sleep(1.0)
                    continue
                # Files pages have no description body; their populated file list is the readiness signal.
                if _capture_ready(page, time.monotonic() - started):
                    return page
            time.sleep(1.0)
        if page:
            return page
        if challenge_since:
            raise _HiddenChallenge("Nexus kept showing its security check to the hidden reader.")
        raise RuntimeError(
            "The MWManager browser could not read the page (" + (detail or "no result") + "). "
            "Wait for Nexus to finish loading, then retry."
        )

    def show_main():
        view["visible"] = True
        if view["quiet"]:
            view["quiet"] = False
            from System import Func, Object
            window.native.Invoke(Func[Object](lambda: _set_quiet(window, False)))
        window.show()

    def hide_main():
        view["visible"] = False
        window.hide()

    def reader_window(url: str):
        """Where a hidden read happens: the main window while nobody can see it, else a second hidden window, so a
        page the user is looking at is never navigated away. Returns (window, already loading url)."""
        if not view["visible"]:
            return window, False
        if view["reader"] is None:
            view["reader"] = webview.create_window(READER_TITLE, url=url, hidden=True,
                                                   focus=False, width=1180, height=820)
            view["reader"].events.loaded.wait(30)
            return view["reader"], True
        return view["reader"], False

    def trusted_click(x, y) -> bool:
        """Click viewport point (x, y) through WebView2's DevTools protocol: a trusted click, like the user's.

        Returns False when it cannot be sent (no WebView2 handle), so the caller falls back to a script click."""
        try:
            from System import Func, Object
            control = window.native.browser.webview
            events = trusted_click_events(x, y)

            def send():
                core = control.CoreWebView2
                for event in events:
                    core.CallDevToolsProtocolMethodAsync("Input.dispatchMouseEvent", json.dumps(event))
                return None
            control.Invoke(Func[Object](send))
            return True
        except Exception:
            return False

    def download(command_id: str, url: str):
        """Load a file page hidden, click its download button, and report until the file is complete."""
        def say(**fields):
            _write_json(root / "response.json", {"id": command_id, **fields})

        def new_results():
            return (set(downloads["finished"]) | set(downloads["failed"])) - known

        known = set(downloads["finished"]) | set(downloads["failed"])
        hide_main()
        _navigate(window, url)
        started, shown = time.monotonic(), False
        say(message="Opening the Nexus file page…")
        clicked_at, fallback_done, last_target = 0.0, False, {}
        while not closed["value"] and not downloads["active"] and not new_results():
            page, _detail = run_script(DOWNLOAD_JS, 10)
            target = page.get("target") if page else None
            if target and not page.get("clicked"):
                run_script(MARK_CLICKED_JS % "true", 10)       # script click: starts the download on Nexus
                clicked_at, fallback_done, last_target = time.monotonic(), False, target
            elif clicked_at and not fallback_done and time.monotonic() - clicked_at > 15:
                # no download yet: a trusted click at the same spot (works when the window is shown)
                trusted_click(last_target.get("x"), last_target.get("y"))
                fallback_done = True
            blocked = bool(page) and is_challenge(str(page.get("title", "")), str(page.get("text", "")))
            stuck = time.monotonic() - started > 30          # no download button found: login or a changed page
            if (blocked or stuck) and not shown:
                show_main()
                shown = True
            elif shown and page and not blocked and page.get("clicked"):
                hide_main()                                   # the proper page is back: carry on unseen
                shown = False
            say(message="Nexus needs you in the MWManager browser: complete the security check or sign in, "
                        "then click Start download (Slow download on older pages) if it does not start." if shown else
                        "Waiting for Nexus to start the download…")
            time.sleep(1.0)
        hide_main()
        while not closed["value"] and downloads["active"]:
            for target in sorted(downloads["active"]):
                got, total = downloads["progress"].get(target, [0, 0])
                size = f"{got / 2**20:,.0f} MB" + (f" of {total / 2**20:,.0f} MB" if total else "")
                say(message=f"Downloading {Path(target).name}: {size}")
            time.sleep(0.5)
        new = [f for f in downloads["finished"] if f not in known]
        if new:
            say(file=new[-1], message=f"Downloaded {Path(new[-1]).name}")
        else:
            say(error="The download failed or was cancelled in the MWManager browser.")

    def busy_download(command_id: str, url: str):
        view["busy"] += 1
        try:
            download(command_id, url)
        finally:
            view["busy"] -= 1
            view["activity"] = time.monotonic()

    def service_requests():
        nonlocal last_command
        while not closed["value"]:
            try:
                command = _read_json(command_file)
                command_id = str(command.get("id", ""))
                if command_id and command_id != last_command:
                    last_command = command_id
                    view["activity"] = time.monotonic()
                    if command.get("action") == "open":
                        new_url = str(command.get("url", ""))
                        new_folder = str(command.get("download_dir", "")).strip()
                        if new_folder:
                            download_folder["path"] = Path(new_folder)
                            download_folder["path"].mkdir(parents=True, exist_ok=True)
                        if _valid_browser_url(new_url):
                            _navigate(window, new_url)
                            if not command.get("hidden"):
                                show_main()          # the user asked for the browser: show it
                    elif command.get("action") == "close":
                        # Graceful close: pywebview tears WebView2 down normally, so the
                        # persistent profile (cookies, security-check clearance) is flushed.
                        _write_json(root / "response.json", {"id": command_id, "closed": True})
                        closed["value"] = True
                        window.destroy()
                        return
                    elif command.get("action") == "download":
                        folder = str(command.get("download_dir", "")).strip()
                        target_url = str(command.get("url", ""))
                        if not folder or not _valid_nexus_url(target_url):
                            _write_json(root / "response.json", {"id": command_id, "error": "Refused the download."})
                        else:
                            download_folder["path"] = Path(folder)
                            download_folder["path"].mkdir(parents=True, exist_ok=True)
                            threading.Thread(target=busy_download, args=(command_id, target_url), daemon=True).start()
                    elif command.get("action") == "capture":
                        background = bool(command.get("background"))
                        view["busy"] += 1
                        try:
                            capture_url = str(command.get("url", ""))
                            if capture_url and not _valid_nexus_url(capture_url):
                                raise ValueError("Refused an invalid Nexus capture URL.")
                            target, loading = (reader_window(capture_url) if background and capture_url
                                               else (window, False))
                            if capture_url and not loading:
                                _navigate(target, capture_url)
                            page = capture(command_id, str(command.get("mod_id", "")), target, background)
                            _write_json(root / "response.json", {"id": command_id, "page": page})
                        except _HiddenChallenge as exc:
                            _write_json(root / "response.json",
                                        {"id": command_id, "error": str(exc), "challenge": True})
                        except Exception as exc:
                            _write_json(root / "response.json", {"id": command_id, "error": str(exc)})
                        finally:
                            view["busy"] -= 1
                            view["activity"] = time.monotonic()
                if (not view["visible"] and not view["busy"] and not downloads["active"]
                        and time.monotonic() - view["activity"] > HIDDEN_IDLE_CLOSE):
                    # only MWManager's hidden work used this browser and it is done: do not linger unseen
                    closed["value"] = True
                    window.destroy()
                    return
            except Exception:
                pass
            time.sleep(0.25)

    def on_closed():
        closed["value"] = True
        reader, view["reader"] = view["reader"], None
        if reader is not None:
            try:
                reader.destroy()
            except Exception:
                pass
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    window.events.closed += on_closed
    try:
        webview.start(service_requests, gui="edgechromium", private_mode=False, storage_path=str(profile))
    finally:
        on_closed()


if __name__ == "__main__" and "--embedded-browser" in sys.argv:
    try:
        _run_embedded_browser()
    except Exception as exc:
        try:
            _write_json(_root() / "browser-error.json", {"error": str(exc), "time": time.time()})
        except OSError:
            pass
