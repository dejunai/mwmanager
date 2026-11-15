"""Persistent, interactive Nexus browser for MWManager's FOMOD helper.

The browser is a visible WebView2 window in a small child process. Its profile
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
        return pid if os.name != "nt" else 0
    image, created = identity
    if os.name == "nt" and not Path(image).name.casefold().startswith("python"):
        return 0
    if created > written + 2.0:
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


def capture_page(mod_id: str) -> dict:
    """Request readable DOM text from the currently visible embedded browser."""
    mod_id = str(mod_id).strip()
    if not mod_id.isdigit() or int(mod_id) <= 0:
        raise ValueError("A valid Nexus mod ID is required.")
    root = _root()
    pid = _tracked_browser_pid()
    if not pid:
        detail = _startup_error()
        if detail:
            raise RuntimeError(f"The MWManager browser could not start: {detail}")
        raise RuntimeError("The MWManager browser is closed. Click Open Nexus in MWManager browser first.")

    request_id = secrets.token_hex(16)
    _write_json(root / "command.json", {"id": request_id, "action": "capture", "mod_id": mod_id})
    response_file = root / "response.json"
    # The browser answers with {"state": "working"} heartbeats while it waits for
    # Nexus to render the description, then with the final page or an error.
    started = time.monotonic()
    deadline = started + 15  # until the first heartbeat
    while time.monotonic() < deadline:
        response = _read_json(response_file)
        if response.get("id") == request_id:
            if response.get("state") == "working":
                deadline = max(deadline, min(started + 75, time.monotonic() + 15))
                time.sleep(0.2)
                continue
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            page = response.get("page") or {}
            if not isinstance(page, dict):
                break
            from mwnexus_parse import page_text_from_capture
            text = page_text_from_capture(page)
            if _challenge_page(str(page.get("title", "")), str(page.get("text", ""))):
                raise RuntimeError(
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
    last_command = str(initial.get("id", ""))
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

        edgechromium.EdgeChrome.on_download_starting = download_to_component
    window = webview.create_window(
        "MWManager — Prerequisite Browser", url=url, js_api=api,
        width=1180, height=820, min_size=(760, 520), confirm_close=False,
    )
    api._window = window

    import threading
    from mwnexus_parse import CAPTURE_JS, decode_script_result, is_challenge

    def run_script(script: str, timeout: float):
        """evaluate_js on a helper thread so a stalled page never wedges the mailbox loop."""
        box = {}

        def worker():
            try:
                box["value"] = window.evaluate_js(script)
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

    def capture(command_id: str, mod_id: str):
        expected = re.compile(r"/mods/" + re.escape(mod_id) + r"(?:/|$)")
        started = time.monotonic()
        deadline = started + 45
        page, detail = None, ""
        while time.monotonic() < deadline and not closed["value"]:
            _write_json(root / "response.json", {"id": command_id, "state": "working"})
            page, detail = run_script(CAPTURE_JS, 12)
            if page:
                parsed = urlparse(str(page.get("url", "")))
                if parsed.hostname not in {"www.nexusmods.com", "nexusmods.com"} \
                        or not expected.search(parsed.path.rstrip("/") + "/"):
                    raise RuntimeError(
                        "Open the matching Nexus mod page in the MWManager browser before reading it."
                    )
                if is_challenge(str(page.get("title", "")), str(page.get("text", ""))):
                    raise RuntimeError(
                        "Nexus is still showing its security check. Complete it in the MWManager "
                        "browser, then click Read page from browser again."
                    )
                # Some layouts render the description client-side: give it up to 15 s.
                if page.get("readyState") == "complete" and (
                        len(str(page.get("description", ""))) > 200 or time.monotonic() - started > 15):
                    return page
            time.sleep(1.0)
        if page:
            return page
        raise RuntimeError(
            "The MWManager browser could not read the page (" + (detail or "no result") + "). "
            "Wait for Nexus to finish loading, then retry."
        )

    def service_requests():
        nonlocal last_command
        while not closed["value"]:
            try:
                command = _read_json(command_file)
                command_id = str(command.get("id", ""))
                if command_id and command_id != last_command:
                    last_command = command_id
                    if command.get("action") == "open":
                        new_url = str(command.get("url", ""))
                        new_folder = str(command.get("download_dir", "")).strip()
                        if new_folder:
                            download_folder["path"] = Path(new_folder)
                            download_folder["path"].mkdir(parents=True, exist_ok=True)
                        if _valid_browser_url(new_url):
                            window.load_url(new_url)
                    elif command.get("action") == "close":
                        # Graceful close: pywebview tears WebView2 down normally, so the
                        # persistent profile (cookies, security-check clearance) is flushed.
                        _write_json(root / "response.json", {"id": command_id, "closed": True})
                        closed["value"] = True
                        window.destroy()
                        return
                    elif command.get("action") == "capture":
                        try:
                            page = capture(command_id, str(command.get("mod_id", "")))
                            _write_json(root / "response.json", {"id": command_id, "page": page})
                        except Exception as exc:
                            _write_json(root / "response.json", {"id": command_id, "error": str(exc)})
            except Exception:
                pass
            time.sleep(0.25)

    def on_closed():
        closed["value"] = True
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
