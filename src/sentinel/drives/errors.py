"""Structured errors for drive handling. ``code`` is an operator alert code (sentinel.alerts)."""

from __future__ import annotations

from typing import Any


class DriveError(RuntimeError):
    """Base class. ``reason`` is a stable machine-readable token; ``detail`` is for administrators."""

    code = "M-005"

    def __init__(self, reason: str, message: str, detail: dict[str, Any] | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.detail = detail or {}
        if code is not None:
            self.code = code


class DriveQueryError(DriveError):
    """Read-only enumeration failed (PowerShell/lsblk missing, unexpected output)."""

    code = "M-008"


class ProtectedStorageError(DriveError):
    """The disk holding the OS / recordings could not be determined: preparation is blocked."""

    code = "M-008"


class PrepareRefused(DriveError):
    """A guard refused the request. Nothing was changed on any disk."""

    code = "M-004"


class IdentityChanged(PrepareRefused):
    """The disk is not the one that was confirmed (swapped, renumbered, or its contents changed)."""

    code = "M-006"


class PrepareFailed(DriveError):
    """The destructive step started and did not complete; the drive may be partly prepared."""

    code = "M-005"


class SafeRemoveError(DriveError):
    code = "M-007"
