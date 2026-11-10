"""Linux/POSIX platform implementation.

NOTE: not executed in the Phase 2 development environment (Windows 11). Must be covered
by the Linux CI run before deployment (see docs/06).
"""

from __future__ import annotations

import ctypes
import os
import signal
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import InstanceLockError, Platform

PR_SET_PDEATHSIG = 1

# Resolve prctl in the parent at import time: loading a library inside preexec_fn (between
# fork and exec in a multi-threaded process) can deadlock.
try:
    _PRCTL = ctypes.CDLL(None, use_errno=True).prctl if sys.platform.startswith("linux") else None
except (OSError, AttributeError):
    _PRCTL = None


def _make_pdeathsig(parent_pid: int):  # noqa: ANN202
    def _child() -> None:  # runs in the child between fork and exec: async-signal-safe calls only
        if _PRCTL is not None:
            _PRCTL(PR_SET_PDEATHSIG, signal.SIGKILL)
        if os.getppid() != parent_pid:  # parent died before prctl took effect
            os._exit(1)
    return _child


class PosixPlatform(Platform):
    name = "posix"

    def acquire_instance_lock(self, path: Path) -> Any:
        import fcntl

        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise InstanceLockError(f"another recorder instance holds {path}") from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        return fh

    def child_popen_kwargs(self) -> dict[str, Any]:
        if sys.platform.startswith("linux"):
            return {"preexec_fn": _make_pdeathsig(os.getpid())}
        return {}

    def low_priority_popen_kwargs(self) -> dict[str, Any]:
        base = self.child_popen_kwargs()
        inner = base.get("preexec_fn")
        parent = os.getpid()

        def _child() -> None:
            if inner is not None:
                inner()
            elif os.getppid() != parent:
                os._exit(1)
            os.nice(10)
        return {"preexec_fn": _child}

    def restrict_new_files(self) -> None:
        os.umask(0o027)  # files 0640, directories 0750 (service user + sentinel group)

    def notify(self, state: str) -> None:
        addr = os.environ.get("NOTIFY_SOCKET")
        if not addr:
            return
        if addr.startswith("@"):
            addr = "\0" + addr[1:]  # abstract namespace
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(addr)
                sock.sendall(state.encode("utf-8"))
        except OSError:
            pass

    def clock_synchronized(self) -> bool | None:
        try:
            cp = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        val = cp.stdout.strip().lower()
        return True if val == "yes" else False if val == "no" else None

    def bind_child(self, proc: subprocess.Popen[bytes]) -> None:
        # PDEATHSIG (set in child_popen_kwargs) + systemd KillMode=control-group.
        return None

    def fsync_file(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def fsync_dir(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def seal_read_only(self, path: Path) -> None:
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP)  # 0440: owner + group read only

    def is_read_only(self, path: Path) -> bool:
        return not (os.stat(path).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    def write_access_problems(self, path: Path) -> list[str]:
        try:
            st = os.stat(path)
        except OSError as exc:
            return [f"permissions could not be read ({exc})"]
        problems = []
        if st.st_mode & stat.S_IWOTH:
            problems.append("can be changed by all users")
        if st.st_mode & stat.S_IWGRP and st.st_gid != 0:
            problems.append("can be changed by members of its group")
        if st.st_uid not in (0, os.geteuid()):  # type: ignore[attr-defined]
            problems.append("is owned by another account")
        return problems

    def secret_file_problems(self, path: Path) -> list[str]:
        st = os.stat(path)
        problems = []
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            problems.append(f"{path} is accessible by group/other (mode {oct(st.st_mode & 0o777)}); use 0600")
        if st.st_uid != os.getuid():  # type: ignore[attr-defined]
            problems.append(f"{path} is not owned by the service account")
        return problems
