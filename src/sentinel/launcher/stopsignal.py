"""Graceful-stop channel for a recorder the launcher started in the background.

A recorder started detached (no console window) cannot receive CTRL_BREAK from another
console on Windows, so the recorder listens on a *named event* instead:

* name ``Local\\SentinelRecorderStop-<hash of the data folder>`` — per logon session, one per
  data folder, so two installations on one machine do not stop each other;
* created with the default security descriptor of the recorder's own token: only the same
  account, SYSTEM and Administrators can open it (other standard users cannot stop recording);
* the event exists only while a recorder is listening, so a stop request can never be left
  "armed" for the next start (``request_stop`` reports "not running" instead).

On Linux the recorder already handles SIGTERM; ``request_stop`` sends SIGTERM to the PID that
the POSIX instance lock writes into ``recorder.lock``, after checking that this PID is alive and
is a recorder (``posix_recorder_pid``).

``recorder_alive`` is the launcher's non-intrusive running check: it never takes the instance
lock, so it cannot make a recorder that is starting at that moment exit as "already running".

This is the interim mechanism for the launcher-managed background process. The Windows service
(branch design/windows-service) replaces it with Service Control Manager stop requests.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x0
ERROR_ALREADY_EXISTS = 183


def event_name(data_dir: Path) -> str:
    key = os.path.normcase(str(Path(data_dir).resolve()))
    return "Local\\SentinelRecorderStop-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _kernel32():  # noqa: ANN202 - ctypes library object
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.SetEvent.restype = wintypes.BOOL
    k32.ResetEvent.argtypes = [wintypes.HANDLE]
    k32.ResetEvent.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


class StopListener:
    """Calls ``on_stop`` once when a stop is requested for ``data_dir``. Windows only; no-op elsewhere."""

    def __init__(self, data_dir: Path, on_stop: Callable[[], None]) -> None:
        self.name = event_name(data_dir)
        self._on_stop = on_stop
        self._closed = threading.Event()
        self._handle: int | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> StopListener:
        if sys.platform != "win32":
            return self
        k32 = _kernel32()
        import ctypes

        handle = k32.CreateEventW(None, True, False, self.name)
        if not handle:
            log.error("could not create the stop channel %s (error %s); stop the recorder by other means",
                      self.name, ctypes.get_last_error())
            return self
        if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            k32.ResetEvent(handle)   # only the creator resets; never swallow a pending request of another
        else:
            log.warning("stop channel %s already exists (another recorder is starting or running)", self.name)
        self._handle = handle
        self._thread = threading.Thread(target=self._wait, args=(k32, handle), name="stop-listener", daemon=True)
        self._thread.start()
        return self

    def _wait(self, k32, handle: int) -> None:  # noqa: ANN001
        while not self._closed.is_set():
            if k32.WaitForSingleObject(handle, 500) == WAIT_OBJECT_0:
                if not self._closed.is_set():
                    log.info("stop requested through the launcher stop channel")
                    self._on_stop()
                break

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(2.0)
        if self._handle is not None:
            _kernel32().CloseHandle(self._handle)
            self._handle = None


def request_stop(data_dir: Path) -> bool:
    """Ask the recorder that uses ``data_dir`` to stop gracefully. False = nobody listening."""
    if sys.platform == "win32":
        k32 = _kernel32()
        handle = k32.OpenEventW(EVENT_MODIFY_STATE, False, event_name(data_dir))
        if not handle:
            return False
        try:
            return bool(k32.SetEvent(handle))
        finally:
            k32.CloseHandle(handle)
    pid = posix_recorder_pid(Path(data_dir))
    if pid is None:
        return False
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def recorder_alive(data_dir: Path) -> bool:
    """Non-intrusive "is a recorder running for ``data_dir``?" (never touches the instance lock).

    Windows: the recorder's stop event exists (it exists only while a recorder process holds
    it). POSIX: the PID in ``recorder.lock`` is alive and is a recorder (see posix_recorder_pid)."""
    if sys.platform == "win32":
        k32 = _kernel32()
        handle = k32.OpenEventW(SYNCHRONIZE, False, event_name(data_dir))
        if not handle:
            return False
        k32.CloseHandle(handle)
        return True
    return posix_recorder_pid(Path(data_dir)) is not None


def posix_recorder_pid(data_dir: Path) -> int | None:
    """PID that PosixPlatform's lock wrote into ``recorder.lock`` if it still belongs to a recorder.

    The file keeps the last recorder's PID after it exits, so the PID is checked: alive
    (``kill(pid, 0)``) and, where ``/proc`` exists, its command line is a recorder run
    (``... recorder run ...`` or ``... sentinel.service run ...``), which rules out PID reuse.
    Without ``/proc`` (non-Linux POSIX) only the liveness check is possible."""
    try:
        pid = int((data_dir / "recorder.lock").read_text().strip() or "0")
    except (OSError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, owned by another account
    cmdline = Path(f"/proc/{pid}/cmdline")
    if Path("/proc/self").exists():
        try:
            args = cmdline.read_bytes().split(b"\0")
        except OSError:
            return None
        if b"run" not in args or not (b"recorder" in args or b"sentinel.service" in args):
            return None
    return pid