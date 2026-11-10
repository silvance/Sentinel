"""Which archive drive(s) are attached? Found by **marker**, never by drive letter alone.

An archive drive is a volume whose root holds ``SENTINEL_DRIVE.json`` (written by drive preparation,
``sentinel.drives``) with a ``drive_uuid`` **and** ``"role": "archive"`` (added by
``python -m sentinel.archive designate``, or by the preparation step once it supports roles).
Drives without the role — blank drives, review drives, anything else — are never written.

:class:`VolumeScanLocator` (default) enumerates every mounted volume each poll:

* Windows: ``FindFirstVolumeW`` / ``FindNextVolumeW``; each volume is addressed by its volume GUID
  path (``\\\\?\\Volume{…}\\``), which stays the same whatever drive letter Windows assigns — so a
  new drive inserted before the old one is removed (and given another letter) is still found, and
  a letter re-used by a different disk is never mistaken for the old drive.
* Linux: mount points from ``/proc/self/mountinfo`` (NOT EXECUTED on the development host).
* ``extra_roots`` (``[archive] drive_path``, optional) are scanned too, e.g. a sub-folder.

The scan reports usable candidates and drives that are present but unusable (marker unreadable,
wrong role) separately, so the worker can tell "absent" from "present but not readable".
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..export.target import DRIVE_MARKER, ExportTarget, TargetError, identify_target

log = logging.getLogger(__name__)

ROLE_ARCHIVE = "archive"


@dataclass
class DriveScan:
    candidates: list[ExportTarget] = field(default_factory=list)
    unusable: list[tuple[str, str]] = field(default_factory=list)     # (root, reason): present, not usable


class DriveLocator(Protocol):
    def scan(self) -> DriveScan:
        """Attached archive drives. Must not raise for 'absent'."""


class DriveUnusable(Exception):
    """A drive is attached at the archive location but must not be written (operator alert A-005)."""


def read_marker(root: Path) -> dict[str, object] | None:
    """The marker JSON, None if there is none. Raises ValueError/OSError if present but unreadable."""
    p = Path(root) / DRIVE_MARKER
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        return None
    doc = json.loads(data.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("marker is not a JSON object")
    return doc


def windows_volume_roots() -> list[str]:
    """Volume GUID paths of all volumes (read-only enumeration; nothing is opened)."""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
    k32.FindFirstVolumeW.restype = wintypes.HANDLE
    k32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
    k32.FindVolumeClose.argtypes = [wintypes.HANDLE]
    buf = ctypes.create_unicode_buffer(1024)
    h = k32.FindFirstVolumeW(buf, len(buf))
    if h is None or h == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        while True:
            out.append(buf.value)
            if not k32.FindNextVolumeW(h, buf, len(buf)):
                break
    finally:
        k32.FindVolumeClose(h)
    return out


def posix_mount_roots() -> list[str]:  # NOT EXECUTED on the development host
    out = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                out.append(parts[4].replace("\\040", " "))
    except OSError:
        pass
    return out


def default_roots() -> list[str]:
    return windows_volume_roots() if sys.platform == "win32" else posix_mount_roots()


class VolumeScanLocator:
    def __init__(self, extra_roots: Iterable[str | Path] = (), roots: Callable[[], list[str]] | None = None,
                 require_role: bool = True) -> None:
        self.extra_roots = [Path(r) for r in extra_roots if str(r)]
        self.roots = roots or default_roots
        self.require_role = require_role

    def scan(self) -> DriveScan:
        res = DriveScan()
        seen: set[str] = set()
        for r in [*self.roots(), *map(str, self.extra_roots)]:
            root = Path(r)
            try:
                marker = read_marker(root)
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                # No media / not ready / access denied volumes are not drives we know; a marker that
                # exists but cannot be read is reported as present-but-unusable.
                if _marker_exists(root):
                    res.unusable.append((str(root), f"drive marker unreadable: {type(exc).__name__}"))
                continue
            if marker is None:
                continue
            if self.require_role and marker.get("role") != ROLE_ARCHIVE:
                res.unusable.append((str(root), f"drive role is {marker.get('role')!r}, not 'archive'"))
                continue
            try:
                ident = identify_target(root).identity
                # Keep the scanned path (on Windows the volume GUID path) for I/O: resolving it would
                # turn it back into a drive-letter path.
                target = ExportTarget(root, ident)
                key = target.drive_key
            except (OSError, TargetError) as exc:
                res.unusable.append((str(root), f"cannot identify the drive: {exc}"))
                continue
            if not target.identity.get("drive_uuid"):
                res.unusable.append((str(root), "drive marker has no drive_uuid"))
                continue
            if key in seen:          # same volume reached twice (GUID path and an extra root)
                continue
            seen.add(key)
            res.candidates.append(target)
        return res


def _marker_exists(root: Path) -> bool:
    try:
        return os.path.lexists(root / DRIVE_MARKER)
    except OSError:
        return False


def designate_archive(root: Path, admin_id: str, has_review_batches: Callable[[str], bool] | None = None) -> dict[str, object]:
    """Administrator: mark a PREPARED drive (valid ``SENTINEL_DRIVE.json``) as an archive drive by adding
    ``"role": "archive"`` to its marker (atomic rewrite). Refuses a drive that already has another role,
    holds a review-drive layout, or has review exports recorded in the ledger."""
    root = Path(root)
    marker = read_marker(root)
    if marker is None or not marker.get("drive_uuid"):
        raise DriveUnusable("the drive is not prepared (no SENTINEL_DRIVE.json with a drive_uuid)")
    if marker.get("role") not in (None, ROLE_ARCHIVE):
        raise DriveUnusable(f"the drive already has the role {marker.get('role')!r}")
    review = [n for n in ("README_FIRST.txt", "Recordings", "Manifests", "Originals") if (root / n).exists()]
    if review:
        raise DriveUnusable(f"the drive holds a review-drive layout ({', '.join(review)})")
    if has_review_batches is not None and has_review_batches(f"drive_uuid:{marker['drive_uuid']}"):
        raise DriveUnusable("review exports to this drive are recorded in the ledger")
    if marker.get("role") == ROLE_ARCHIVE:
        return marker
    from ..clock import iso_utc
    import time

    new = dict(marker, role=ROLE_ARCHIVE, role_set_utc=iso_utc(time.time()), role_set_by=admin_id)
    tmp = root / (DRIVE_MARKER + ".partial")
    with open(tmp, "wb") as fh:
        fh.write(json.dumps(new, indent=2, sort_keys=True).encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, root / DRIVE_MARKER)
    return new
