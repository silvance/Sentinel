"""Windows platform implementation (development host today; future Windows service)."""

from __future__ import annotations

import ctypes
import logging
import os
import stat
import subprocess
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .base import InstanceLockError, Platform

log = logging.getLogger(__name__)

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def flush_directory(path: Path) -> bool:
    """Commit a directory's metadata (e.g. a rename into it) to the device: open the directory with
    ``FILE_FLAG_BACKUP_SEMANTICS`` and ``GENERIC_WRITE`` and call ``FlushFileBuffers``. Works without
    administrator rights on NTFS (verified on the development host). Returns False if the file
    system refuses (callers then confirm the result another way); raises OSError if the directory
    cannot be opened at all (e.g. the drive is gone)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    GENERIC_WRITE, SHARE_ALL, OPEN_EXISTING, BACKUP_SEMANTICS = 0x40000000, 0x7, 3, 0x02000000
    p = os.path.abspath(path)
    p = p if p.startswith("\\\\") else "\\\\?\\" + p
    h = k32.CreateFileW(p, GENERIC_WRITE, SHARE_ALL, None, OPEN_EXISTING, BACKUP_SEMANTICS, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        err = ctypes.get_last_error()
        if err in (5, 50, 87):         # access denied / not supported / invalid parameter: refused
            return False
        raise ctypes.WinError(err)
    try:
        if k32.FlushFileBuffers(h):
            return True
        err = ctypes.get_last_error()
        if err in (5, 1, 50, 87):
            return False
        raise ctypes.WinError(err)
    finally:
        k32.CloseHandle(h)


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsPlatform(Platform):
    name = "windows"

    def __init__(self) -> None:
        self._job: int | None = None

    def _job_handle(self) -> int | None:
        if self._job is not None:
            return self._job
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            log.warning("CreateJobObject failed; child processes may outlive the service")
            return None
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            log.warning("SetInformationJobObject failed; child processes may outlive the service")
            return None
        self._job = job
        return job

    def acquire_instance_lock(self, path: Path) -> Any:
        import msvcrt

        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            raise InstanceLockError(f"another recorder instance holds {path}") from None
        return fh

    def child_popen_kwargs(self) -> dict[str, Any]:
        return {"creationflags": CREATE_NO_WINDOW}

    def low_priority_popen_kwargs(self) -> dict[str, Any]:
        return {"creationflags": CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS}

    def bind_child(self, proc: subprocess.Popen[bytes]) -> None:
        job = self._job_handle()
        if job is None:
            return
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):  # type: ignore[attr-defined]
            log.warning("AssignProcessToJobObject failed (error %s)", ctypes.get_last_error())

    def fsync_file(self, path: Path) -> None:
        # Windows requires a writable handle for FlushFileBuffers; temporarily lift read-only.
        mode = os.stat(path).st_mode
        restored = False
        if not mode & stat.S_IWRITE:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            restored = True
        try:
            fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if restored:
                os.chmod(path, stat.S_IREAD)

    def fsync_dir(self, path: Path) -> None:
        # Best effort for the recorder: NTFS journals metadata, and a refusal must never stop
        # recording. Archive/export use flush_directory() directly and act on a refusal.
        try:
            if not flush_directory(path):
                log.debug("directory flush refused for %s", path)
        except OSError as exc:
            log.debug("directory flush failed for %s: %s", path, exc)

    def seal_read_only(self, path: Path) -> None:
        os.chmod(path, stat.S_IREAD)  # sets FILE_ATTRIBUTE_READONLY

    def is_read_only(self, path: Path) -> bool:
        return not (os.stat(path).st_mode & stat.S_IWRITE)

    def secret_file_problems(self, path: Path) -> list[str]:
        # NOT IMPLEMENTED: Windows ACL inspection needs pywin32 or icacls parsing; planned for the
        # Windows-service phase. Reported explicitly so nobody mistakes this for a passed check.
        return ["credential file permissions are not checked on Windows (not implemented)"]
