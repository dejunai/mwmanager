"""Keep Tk objects on the thread that made them.

Tk (Tcl) is single-threaded. A Tcl interpreter that Python frees on any other thread aborts the process
("Tcl_AsyncDelete: async handler deleted by the wrong thread"), and a Tk variable or PhotoImage freed off the main
thread is just as unsafe. Tk widgets sit in reference cycles (a widget and its bound callbacks), so they are not
freed when the window closes: the cyclic garbage collector frees them later, on whichever thread happens to
allocate enough to trigger a collection. That is often a download or scan worker, so the crash looked random.

The rules this module supports:

* A worker thread never touches Tk and never holds a Tk object. It only puts plain data on a queue.Queue, and the
  window's main thread drains that queue with after() (see PollLoop).
* A window's after() jobs are cancelled when it is destroyed (PollLoop does this), so nothing fires on a dead window.
* Once a window is destroyed, the garbage collector is held off until the owning thread has collected the window's
  objects itself (tk_window / watch_destroy), so no other thread can be the one that frees them.
"""
from __future__ import annotations

import functools
import gc
import queue
import threading

_lock = threading.Lock()
_holds = 0
_reenable = False
_local = threading.local()


def _hold_gc() -> None:
    global _holds, _reenable
    with _lock:
        if _holds == 0:
            _reenable = gc.isenabled()
            gc.disable()
        _holds += 1


def _release_gc() -> None:
    global _holds
    with _lock:
        _holds -= 1
        if _holds == 0 and _reenable:
            gc.enable()


class _Lifetime:
    """One window's hold on the collector: taken when it is destroyed, released after a collection."""

    def __init__(self) -> None:
        self.held = False

    def hold(self) -> None:
        if not self.held:
            self.held = True
            _hold_gc()

    def release(self) -> None:
        # Nothing here may be called from a worker thread: it runs on the thread that owns the window.
        try:
            gc.collect()
        finally:
            if self.held:
                self.held = False
                _release_gc()


def watch_destroy(widget) -> None:
    """From the moment this window is destroyed until its tk_window() call returns, keep the collector off."""
    life = getattr(_local, "life", None)
    if life is None:
        return

    def on_destroy(event):
        if event.widget is widget:
            life.hold()

    widget.bind("<Destroy>", on_destroy, add="+")


def tk_window(func):
    """Decorate the function that creates, runs and closes a window (or the whole app).

    When it returns, the function's locals are gone, so the window's dead widgets, variables and images are
    collected right here on the owning thread. Between destroy() and that collection the collector is held off, so
    a worker thread that allocates cannot be the one to free a Tcl interpreter or a Tk variable."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        previous = getattr(_local, "life", None)
        life = _local.life = _Lifetime()
        try:
            return func(*args, **kwargs)
        finally:
            _local.life = previous
            life.release()

    return wrapper


def collect_tk_garbage() -> None:
    """Collect now, on the calling (owning) thread. For code that destroys a window outside tk_window()."""
    gc.collect()


class PollLoop:
    """after()-driven polling of a queue.Queue on a window's own thread.

    Create it with the window, give it the function to run each tick (it returns True to keep going), call
    start(). The loop ends by itself when the window is destroyed (the pending after() job is cancelled)."""

    def __init__(self, widget, interval_ms: int, tick) -> None:
        self.widget = widget
        self.interval_ms = interval_ms
        self.tick = tick
        self.job = None
        self.stopped = False
        widget.bind("<Destroy>", self._on_destroy, add="+")

    def start(self, first_delay_ms: int | None = None) -> None:
        """Run the first tick now, or after first_delay_ms."""
        if first_delay_ms is None:
            self._run()
        else:
            self.job = self.widget.after(first_delay_ms, self._run)

    def stop(self) -> None:
        self.stopped = True
        job, self.job = self.job, None
        if job is not None:
            try:
                self.widget.after_cancel(job)
            except Exception:       # TclError: the interpreter is already gone
                pass

    def _on_destroy(self, event) -> None:
        if event.widget is self.widget:
            self.stop()

    def _run(self) -> None:
        self.job = None
        if self.stopped:
            return
        try:
            if not self.widget.winfo_exists():
                self.stopped = True
                return
            keep_going = self.tick()
        except Exception as exc:
            # The only Tk error expected here is the window going away mid-tick.
            if exc.__class__.__name__ != "TclError":
                raise
            self.stopped = True
            return
        if keep_going and not self.stopped:
            try:
                self.job = self.widget.after(self.interval_ms, self._run)
            except Exception:
                self.stopped = True

def after_while_open(widget, delay_ms: int, func) -> PollLoop:
    """widget.after(delay_ms, func) that is cancelled if the window is destroyed first, and runs once."""

    def once():
        func()
        return False

    loop = PollLoop(widget, delay_ms, once)
    loop.start(first_delay_ms=delay_ms)
    return loop


def _background_main(work, out) -> None:
    try:
        out.put((work(), None))
    except Exception as exc:
        out.put((None, exc.with_traceback(None)))       # no worker frames kept alive through the traceback


def run_in_background(widget, work, done, *, name: str = "MWManager background task",
                      interval_ms: int = 50) -> threading.Thread:
    """Run work() on a daemon thread; call done(result, error) on widget's own thread, never on the worker.

    work must be a plain function: build it from values you already copied out (paths, strings, dicts), not from
    widgets, Tk variables, images or the window object. Only the thread-safe queue connects the two threads, and
    the worker never calls Tk (not even after()). done gets (result, None) or (None, the Exception). If the window
    is destroyed first, the poll stops and done is never called."""
    out = queue.Queue()

    def tick():
        try:
            result, error = out.get_nowait()
        except queue.Empty:
            return True
        done(result, error)
        return False

    thread = threading.Thread(target=_background_main, args=(work, out), name=name, daemon=True)
    thread.start()
    PollLoop(widget, interval_ms, tick).start(first_delay_ms=interval_ms)
    return thread



def reap_tk() -> None:
    """Collect destroyed Tk objects on this thread.

    Tk widgets and Tcl interpreters sit in reference cycles, so destroy() does not free them right away.
    If the collector runs during a later update() -- often in the next test -- freeing that interpreter
    mid-update can abort the process on Windows. Call this on the thread that created the windows, when
    no update() or mainloop is running.

    Live roots are left alone: destroying the last Tcl interpreter on Python 3.9 can leave the process
    unable to create another (tcl_findLibrary / auto.tcl). Tests still have to destroy their own windows.
    """
    collect_tk_garbage()
