"""Platform service interfaces.

Everything OS-specific the recorder needs is behind this interface so a Windows service
implementation (and, in Phase 3, a ``DriveManager``) can be swapped in without touching
recording logic.
"""

from __future__ import annotations

import abc
import subprocess
from pathlib import Path
from typing import Any


class InstanceLockError(RuntimeError):
    """Another recorder instance holds the lock."""


class Platform(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    def acquire_instance_lock(self, path: Path) -> Any:
        """Take an exclusive, non-blocking lock. Returns an opaque handle to keep alive."""

    @abc.abstractmethod
    def child_popen_kwargs(self) -> dict[str, Any]:
        """Extra ``subprocess.Popen`` kwargs for child processes (e.g. die-with-parent)."""

    @abc.abstractmethod
    def bind_child(self, proc: subprocess.Popen[bytes]) -> None:
        """Ensure ``proc`` is terminated if this service process dies."""

    @abc.abstractmethod
    def fsync_file(self, path: Path) -> None: ...

    @abc.abstractmethod
    def fsync_dir(self, path: Path) -> None: ...

    @abc.abstractmethod
    def seal_read_only(self, path: Path) -> None:
        """Remove write permission from an original segment."""

    @abc.abstractmethod
    def is_read_only(self, path: Path) -> bool: ...

    @abc.abstractmethod
    def secret_file_problems(self, path: Path) -> list[str]:
        """Return reasons a secrets file is too permissive (empty = acceptable)."""

    def low_priority_popen_kwargs(self) -> dict[str, Any]:
        """Popen kwargs for background work (content hashing) that must yield CPU to recording."""
        return self.child_popen_kwargs()

    def restrict_new_files(self) -> None:
        """Make files/directories created by this process private to the service account + group."""

    def notify(self, state: str) -> None:
        """Service-manager notification (systemd READY=1 / WATCHDOG=1 / STATUS=...). No-op by default."""

    def write_access_problems(self, path: Path) -> list[str]:
        """Reasons accounts other than this one / administrators could change ``path`` (empty = OK).

        Used to refuse configurations, credentials, tools and data folders that a standard
        user could replace. Implementations fail closed (an unreadable ACL is a problem)."""
        return []

    def clock_synchronized(self) -> bool | None:
        """True/False if the OS reports time synchronisation state; None if unknown."""
        return None


def get_platform() -> Platform:
    import sys

    if sys.platform == "win32":
        from .windows import WindowsPlatform

        return WindowsPlatform()
    from .posix import PosixPlatform

    return PosixPlatform()
