"""Shared ownership and cancellation for GUI workers and external tools."""
import os
import signal
import subprocess
import threading
import time

CHILDREN = set()
_POPEN_TYPE = subprocess.Popen
_guard = threading.RLock()
_windows_job = None


def _assign_windows_job(process):
    """Windows kills the whole owned tool tree if its GUI process disappears."""
    global _windows_job
    import ctypes
    from ctypes import wintypes as w

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", w.DWORD), ("min_working", ctypes.c_size_t),
                    ("max_working", ctypes.c_size_t), ("active_processes", w.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                    ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = (ctypes.c_void_p, w.LPCWSTR), w.HANDLE
    kernel.SetInformationJobObject.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD)
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.AssignProcessToJobObject.argtypes, kernel.AssignProcessToJobObject.restype = (w.HANDLE, w.HANDLE), w.BOOL
    kernel.CloseHandle.argtypes = (w.HANDLE,)
    with _guard:
        if _windows_job is None:
            handle = kernel.CreateJobObjectW(None, None)
            info = Extended()
            info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                error = ctypes.get_last_error()
                kernel.CloseHandle(handle)
                raise ctypes.WinError(error)
            _windows_job = handle
        if not kernel.AssignProcessToJobObject(_windows_job, w.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())


def popen(*args, **kwargs):
    from workflow_safety import child_environment, inherited_lock_fds
    kwargs["env"] = child_environment(kwargs.get("env"))
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
        descriptors = tuple(sorted(set(kwargs.get("pass_fds", ())) | set(inherited_lock_fds())))
        if descriptors:
            kwargs["pass_fds"] = descriptors
            kwargs["env"]["MWMANAGER_LOCK_FDS"] = ",".join(map(str, descriptors))
    else:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(*args, **kwargs)
    if os.name == "nt" and isinstance(process, _POPEN_TYPE):
        try:
            _assign_windows_job(process)
        except OSError:
            process.kill()
            process.wait()
            raise
    with _guard:
        for old in list(CHILDREN):
            poll = getattr(old, "poll", None)
            if poll is None or poll() is not None:
                CHILDREN.discard(old)
        CHILDREN.add(process)
    return process


def terminate(process):
    try:
        if os.name == "nt":
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, TypeError, subprocess.SubprocessError):
        if process.poll() is None:
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    with _guard:
        CHILDREN.discard(process)


def run(command, *, progress=None, timeout=7200, **kwargs):
    process = popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, **kwargs)
    try:
        deadline = time.monotonic() + timeout
        while True:
            check = getattr(progress, "check_cancelled", None)
            if check:
                check()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        terminate(process)
        raise
    finally:
        with _guard:
            CHILDREN.discard(process)


def end_all():
    with _guard:
        children = list(CHILDREN)
    for process in children:
        poll = getattr(process, "poll", None)
        if poll is not None and poll() is None:
            terminate(process)
        else:
            with _guard:
                CHILDREN.discard(process)
