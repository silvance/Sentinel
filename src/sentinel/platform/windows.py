"""Windows platform implementation (development host, and the Windows service — see sentinel.winsvc)."""

from __future__ import annotations

import ctypes
import logging
import os
import re
import stat
import subprocess
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
        return None  # NTFS metadata is journaled; directory handles cannot be fsynced via CRT

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
_NOT_SYNCED_SOURCES = ("local cmos clock", "free-running system clock")


@dataclass(frozen=True)
class W32TimeStatus:
    synchronized: bool | None          # None = could not be determined
    reason: str
    leap_indicator: int | None = None
    stratum: int | None = None
    source: str | None = None
    last_sync: str | None = None


def _lead_int(value: str) -> int | None:
    m = re.match(r"\s*(-?\d+)", value)
    return int(m.group(1)) if m else None


def parse_w32tm_status(returncode: int, output: str) -> W32TimeStatus:
    """Interpret ``w32tm /query /status``.

    * Error HRESULTs are matched by number (language-neutral). Service not running means
      Windows is not disciplining the clock now: reported as *not synchronised* (T-002), because
      on a multi-day evidence recorder an undisciplined PC clock drifts seconds per day.
    * Field labels are English. On a non-English Windows the labels are translated, nothing is
      recognised, and the result is ``None`` (unknown) rather than a guess.
    * "Synchronised" means: leap indicator not 3, stratum 1-15, and a source that is not the
      local clock. It does **not** measure the current offset (see docs/07 E1 note).
    """
    rc = returncode & 0xFFFFFFFF
    text = output or ""
    if rc == HRESULT_SERVICE_NOT_STARTED or "0x80070426" in text.lower():
        return W32TimeStatus(False, "the Windows Time service is not running")
    if rc == HRESULT_ACCESS_DENIED or "0x80070005" in text.lower():
        return W32TimeStatus(None, "access denied when querying the Windows Time service")
    if rc != 0:
        return W32TimeStatus(None, f"w32tm failed (exit 0x{rc:08x})")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()
    leap = _lead_int(fields["leap indicator"]) if "leap indicator" in fields else None
    stratum = _lead_int(fields["stratum"]) if "stratum" in fields else None
    source = fields.get("source")
    last = fields.get("last successful sync time")
    if leap is None and stratum is None:
        return W32TimeStatus(None, "w32tm output not recognised (non-English Windows?)", source=source)
    common = {"leap_indicator": leap, "stratum": stratum, "source": source, "last_sync": last}
    if leap == 3:
        return W32TimeStatus(False, "Windows reports the clock as not synchronised (leap indicator 3)", **common)
    if stratum is not None and not 1 <= stratum <= 15:
        return W32TimeStatus(False, f"Windows reports no valid time source (stratum {stratum})", **common)
    if source and source.strip().lower() in _NOT_SYNCED_SOURCES:
        return W32TimeStatus(False, f"Windows is using its own clock ({source}), not a time server", **common)
    if last and last.strip().lower() == "unspecified":
        return W32TimeStatus(False, "Windows has never synchronised with a time server", **common)
    return W32TimeStatus(True, f"synchronised with {source or 'a time source'} (stratum {stratum})", **common)


def query_w32time(timeout_s: float = 10.0) -> W32TimeStatus:
    try:
        cp = subprocess.run(["w32tm", "/query", "/status"], capture_output=True, timeout=timeout_s,
                            check=False, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return W32TimeStatus(None, f"w32tm could not be run ({exc.__class__.__name__})")
    out = (cp.stdout or b"").decode("utf-8", "replace") + (cp.stderr or b"").decode("utf-8", "replace")
    return parse_w32tm_status(cp.returncode, out)
