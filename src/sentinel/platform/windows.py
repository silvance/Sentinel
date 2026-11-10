"""Windows platform implementation (development host, and the Windows service — see sentinel.winsvc)."""

from __future__ import annotations

import ctypes
import logging
import os
import stat
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
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
        self._last_clock_reason: str | None = None

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

    def restrict_new_files(self) -> None:
        """Windows has no umask: new files inherit the ACL of their parent directory.

        The POSIX ``umask 027`` intent is met on Windows by the service installer, which gives the
        data/spool/log directories an inheritable, non-inherited-from-parent ACL (SYSTEM,
        Administrators, the service account) with ``icacls`` — see ``sentinel.winsvc.commands``.
        Everything the recorder creates below them inherits that ACL. Nothing to do per process.
        ``python -m sentinel.winsvc status`` reports directories whose ACL is wider than that.
        """
        return None

    def write_access_problems(self, path: Path) -> list[str]:
        from . import winacl

        try:
            sddl = winacl.get_sddl(path)
        except OSError as exc:
            return [f"permissions could not be read ({exc})"]
        return [f"can be changed by {who}" for who in winacl.broad_write_aces(sddl)]

    def secret_file_problems(self, path: Path) -> list[str]:
        """Who besides SYSTEM, Administrators, this process's account and the Sentinel service
        account may read, change or re-permission the credentials file. Uses the file's DACL
        (language-neutral SDDL), not localised ``icacls`` text."""
        from . import winacl

        try:
            dacl = winacl.read_dacl(path)
            allowed = {winacl.SID_SYSTEM, winacl.SID_ADMINISTRATORS, winacl.current_user_sid(),
                       winacl.service_sid(SENTINEL_SERVICE_NAME)}
        except (winacl.AclError, OSError) as exc:
            return [f"could not read the permissions of {path} ({exc})"]
        return [f"{path} {p}" for p in winacl.disallowed_grants(dacl, allowed)]

    def clock_synchronized(self) -> bool | None:
        st = query_w32time()
        if st.synchronized is not True and st.reason != self._last_clock_reason:
            log.warning("Windows time synchronisation: %s", st.reason)
        self._last_clock_reason = st.reason
        return st.synchronized


# Must match sentinel.winsvc.SERVICE_NAME (not imported: the platform layer must not depend on
# the service package). A unit test asserts they are equal.
SENTINEL_SERVICE_NAME = "SentinelRecorder"


# ---------------------------------------------------------------------- Windows Time (w32tm)
HRESULT_SERVICE_NOT_STARTED = 0x80070426
HRESULT_ACCESS_DENIED = 0x80070005


@dataclass(frozen=True)
class W32TimeStatus:
    synchronized: bool | None          # None = could not be determined
    reason: str
    leap_indicator: int | None = None
    stratum: int | None = None
    source: str | None = None
    last_sync: str | None = None


_REASON_WORDS = {
    "ok": "synchronised with {source} (stratum {stratum})",
    "not_synchronized": "Windows reports the clock as not synchronised, or is using its own clock ({source})",
    "no_valid_time_source": "Windows reports no valid time source (stratum {stratum})",
    "never_synchronized": "Windows has never synchronised with a time server",
    "last_sync_error": "the last Windows Time synchronisation failed",
    "last_sync_too_old": "the last successful Windows Time synchronisation is too old",
    "sync_age_not_reported": "Windows did not report when it last synchronised (cannot judge freshness)",
    "sync_age_limit_unknown": "the synchronisation age limit is unknown (no poll interval reported)",
    "status_not_recognised": "w32tm output not recognised (non-English Windows?)",
}


def parse_w32tm_status(returncode: int, output: str, now_epoch: float | None = None,
                       max_sync_age_s: float | None = None) -> W32TimeStatus:
    """Interpret ``w32tm /query /status [/verbose]``.

    Integration (docs/INTEGRATION.md): ONE parser. The field parsing and the "synchronised"
    decision are ``sentinel.timesync.parse_w32tm_status`` (time-accuracy branch), whose rule is
    stricter: True only with leap indicator 0-2, stratum 1-15, Last Sync Error 0 and a last good
    sync no older than ``max_sync_age_s`` (default 2 x the reported poll interval) -- which needs
    the ``/verbose`` output. This adapter keeps the windows-service branch's language-neutral
    handling of error HRESULTs: service not running -> *not synchronised* (T-002); access denied
    or another failure -> unknown. Labels are English; on a non-English Windows nothing is
    recognised and the result is ``None`` (unknown) rather than a guess.
    """
    from ..timesync import parse_w32tm_status as _parse

    rc = returncode & 0xFFFFFFFF
    text = output or ""
    if rc == HRESULT_SERVICE_NOT_STARTED or "0x80070426" in text.lower():
        return W32TimeStatus(False, "the Windows Time service is not running")
    if rc == HRESULT_ACCESS_DENIED or "0x80070005" in text.lower():
        return W32TimeStatus(None, "access denied when querying the Windows Time service")
    if rc != 0:
        return W32TimeStatus(None, f"w32tm failed (exit 0x{rc:08x})")
    p = _parse(text, time.time() if now_epoch is None else now_epoch, max_sync_age_s)
    leap, stratum, source = p.get("leap_indicator"), p.get("stratum"), p.get("source")
    common = {"leap_indicator": leap, "stratum": stratum, "source": source,
              "last_sync": p.get("last_successful_sync_time_local_text")}
    if leap is None and stratum is None:
        return W32TimeStatus(None, _REASON_WORDS["status_not_recognised"], source=source)
    code = str(p.get("sync_reason"))
    reason = _REASON_WORDS.get(code, code).format(source=source or "a time source", stratum=stratum)
    return W32TimeStatus(p.get("synchronized"), reason, **common)


def query_w32time(timeout_s: float = 10.0, max_sync_age_s: float | None = None) -> W32TimeStatus:
    """``w32tm /query /status /verbose`` (the verbose form reports the sync age the freshness rule
    needs); falls back to the plain form if the verbose one fails for another reason."""
    def run(argv: list[str]) -> tuple[int, str] | None:
        try:
            cp = subprocess.run(argv, capture_output=True, timeout=timeout_s, check=False,
                                creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return cp.returncode, (cp.stdout or b"").decode("utf-8", "replace") + (cp.stderr or b"").decode("utf-8", "replace")

    got = run(["w32tm", "/query", "/status", "/verbose"])
    if got is None or (got[0] != 0 and (got[0] & 0xFFFFFFFF) not in (HRESULT_SERVICE_NOT_STARTED, HRESULT_ACCESS_DENIED)):
        plain = run(["w32tm", "/query", "/status"])
        got = plain if plain is not None else got
    if got is None:
        return W32TimeStatus(None, "w32tm could not be run")
    return parse_w32tm_status(got[0], got[1], max_sync_age_s=max_sync_age_s)
