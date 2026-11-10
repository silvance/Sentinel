"""Lower the scheduling priority of the archive copy (no administrator rights needed).

* Windows: ``SetPriorityClass(GetCurrentProcess(), PROCESS_MODE_BACKGROUND_BEGIN)`` for the
  archive process (lowers CPU, I/O and memory priority of the whole process), or
  ``SetThreadPriority(GetCurrentThread(), THREAD_MODE_BACKGROUND_BEGIN)`` when the worker runs as
  a thread (tests). Both may only be applied to the calling process / thread, which is what is
  done. Exercised on the development host (return value checked).
* Linux: ``nice(10)`` plus best-effort I/O class, lowest level (``ioprio_set``, via the raw
  syscall). NOT EXECUTED on the development host. Note: the ``none`` / ``mq-deadline`` I/O
  schedulers common on SSD/NVMe ignore I/O priorities, so the byte-rate throttle
  (:class:`sentinel.archive.io.Throttle`) is the primary protection, priority the second.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import platform as _platform
import sys
import threading
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)

PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
_IOPRIO_SET = {"x86_64": 251, "aarch64": 30, "armv7l": 314, "i686": 289}
IOPRIO_WHO_PROCESS, IOPRIO_CLASS_BE, IOPRIO_CLASS_SHIFT = 1, 2, 13


PROCESS_MODE_BACKGROUND_END = 0x00200000
THREAD_MODE_BACKGROUND_END = 0x00020000
_bg = threading.local()                # which background mode this thread applied ("process" | "thread")


@contextlib.contextmanager
def normal_priority() -> Iterator[None]:
    """Leave Windows background mode while holding a ledger write lock (SQLite BEGIN IMMEDIATE +
    WAL fsync): a background-priority process holding the lock while starved of I/O would delay the
    recorder's own ledger writes (priority inversion). Re-entered afterwards. No-op elsewhere."""
    mode = getattr(_bg, "mode", None)
    if sys.platform != "win32" or mode is None or getattr(_bg, "depth", 0):
        _bg.depth = getattr(_bg, "depth", 0) + 1
        try:
            yield
        finally:
            _bg.depth -= 1
        return
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _bg.depth = 1
    try:
        _set_mode(k32, mode, begin=False)
        yield
    finally:
        _bg.depth = 0
        _set_mode(k32, mode, begin=True)


def _set_mode(k32: Any, mode: str, begin: bool) -> bool:
    from ctypes import wintypes

    if mode == "process":
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        flag = PROCESS_MODE_BACKGROUND_BEGIN if begin else PROCESS_MODE_BACKGROUND_END
        ok = bool(k32.SetPriorityClass(k32.GetCurrentProcess(), flag))
    else:
        k32.GetCurrentThread.restype = wintypes.HANDLE
        k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        flag = THREAD_MODE_BACKGROUND_BEGIN if begin else THREAD_MODE_BACKGROUND_END
        ok = bool(k32.SetThreadPriority(k32.GetCurrentThread(), flag))
    if not ok:
        log.debug("background mode %s (%s) failed: %s", "begin" if begin else "end", mode, ctypes.get_last_error())
    return ok


def lower_priority(whole_process: bool) -> str:
    """Return a short description of what was applied (recorded in the archive status)."""
    if sys.platform == "win32":
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if whole_process:
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            ok = k32.SetPriorityClass(k32.GetCurrentProcess(), PROCESS_MODE_BACKGROUND_BEGIN)
            what = "windows-process-background-mode"
        else:
            k32.GetCurrentThread.restype = wintypes.HANDLE
            k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
            ok = k32.SetThreadPriority(k32.GetCurrentThread(), THREAD_MODE_BACKGROUND_BEGIN)
            what = "windows-thread-background-mode"
        if not ok:
            err = ctypes.get_last_error()
            log.warning("could not lower archive priority (%s, error %s)", what, err)
            return f"{what}-failed({err})"
        _bg.mode = "process" if whole_process else "thread"
        return what
    # POSIX: NOT EXECUTED on the development host.
    applied = []
    try:
        os.nice(10)       # on Linux this affects the calling thread only; the archive process is single-threaded
        applied.append("nice+10")
    except OSError as exc:
        log.warning("nice failed: %s", exc)
    nr = _IOPRIO_SET.get(_platform.machine())
    if sys.platform.startswith("linux") and nr is not None:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            value = (IOPRIO_CLASS_BE << IOPRIO_CLASS_SHIFT) | 7
            if libc.syscall(nr, IOPRIO_WHO_PROCESS, 0, value) == 0:
                applied.append("ioprio-be7")
            else:
                log.warning("ioprio_set failed: errno %s", ctypes.get_errno())
        except (OSError, AttributeError) as exc:
            log.warning("ioprio_set unavailable: %s", exc)
    return "+".join(applied) or "unchanged"
