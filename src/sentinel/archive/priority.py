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

import ctypes
import logging
import os
import platform as _platform
import sys

log = logging.getLogger(__name__)

PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
_IOPRIO_SET = {"x86_64": 251, "aarch64": 30, "armv7l": 314, "i686": 289}
IOPRIO_WHO_PROCESS, IOPRIO_CLASS_BE, IOPRIO_CLASS_SHIFT = 1, 2, 13


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
