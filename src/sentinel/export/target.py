"""Export targets (a mounted, writable directory on a drive) and safe removal.

Drive enumeration, classification, formatting and ejection belong to ``sentinel.drives``
(another branch). Export only needs:

* :class:`ExportTarget` — a directory root that is already mounted and writable, plus a
  drive-identity dict. The identity decides "already verified on *this* drive", so it must
  survive re-insertion (volume GUID / filesystem UUID / Sentinel ``drive_uuid``), never a
  drive letter or ``/dev/sdX``.
* :class:`SafeRemover` — flush + eject, called once the export is verified.

:class:`ManualRemovalFallback` is the only remover shipped here: it does NOT eject. Ejecting a
USB disk on Windows without administrator rights is possible in principle
(``CM_Request_Device_EjectW``) but could not be verified on the development host without
touching a real drive, so it is not implemented; the operator is told, in plain words, to use
Windows "Safely Remove Hardware" instead.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

DRIVE_MARKER = "SENTINEL_DRIVE.json"      # written by drive preparation (sentinel.drives), read here
# Identity dict keys (same names as the ``drives`` ledger columns): drive_uuid, serial, model,
# size_bytes, bus_type, unique_id (WWN), volume_guid, fs_uuid, label. Diagnostic extras
# (mount_point, filesystem, volume_serial, drive_type, subdir) are kept in the identity JSON.
# Keys in order of preference for the stable drive key:
_KEY_PRIORITY = ("drive_uuid", "volume_guid", "fs_uuid", "unique_id", "serial")


class TargetError(RuntimeError):
    """The target cannot be used (not a directory, not writable, cannot be identified)."""


@dataclass(frozen=True)
class ExportTarget:
    root: Path
    identity: dict[str, str] = field(default_factory=dict)

    @property
    def drive_key(self) -> str:
        """Stable ledger key for this drive (and sub-directory, if the root is not the volume root)."""
        for k in _KEY_PRIORITY:
            v = self.identity.get(k)
            if v:
                key = f"{k}:{v}"
                if k == "serial" and self.identity.get("model"):
                    key += f"|model:{self.identity['model']}"
                sub = self.identity.get("subdir")
                return f"{key}|subdir:{sub}" if sub else key
        raise TargetError("the drive could not be identified (no drive_uuid, volume GUID, filesystem UUID, "
                          "unique id or disk serial)")

    @property
    def label(self) -> str | None:
        return self.identity.get("label") or None


@dataclass(frozen=True)
class RemovalResult:
    ejected: bool               # True only if software actually ejected / powered off the drive
    operator_message: str       # plain language, shown as the final status
    detail: str                 # for administrators


class SafeRemover(Protocol):
    def flush(self, target: ExportTarget) -> None: ...

    def eject(self, target: ExportTarget) -> RemovalResult: ...


class ManualRemovalFallback:
    """Flushes what an unprivileged process can flush and asks the operator to remove the drive
    with the operating system's own safe-removal function. Does NOT eject (see module doc)."""

    def flush(self, target: ExportTarget) -> None:
        # Every file was already flushed individually (fsync / FlushFileBuffers) before it was
        # read back. On POSIX additionally ask the kernel to write back everything. On Windows a
        # whole-volume FlushFileBuffers needs administrator rights, so nothing more is done.
        if hasattr(os, "sync"):
            os.sync()

    def eject(self, target: ExportTarget) -> RemovalResult:
        if sys.platform == "win32":
            msg = ("Export complete and verified. Before unplugging the drive, click the "
                   "\"Safely Remove Hardware\" icon near the clock, choose this drive, and wait for "
                   "the message that it is safe to remove.")
        else:
            msg = ("Export complete and verified. Before unplugging the drive, use the "
                   "\"Eject\" or \"Safely remove\" option for this drive and wait until it disappears.")
        return RemovalResult(False, msg, "software eject not implemented (ManualRemovalFallback); "
                                         "per-file flush done")


# ------------------------------------------------------------------------------ identification
def _read_marker(root: Path) -> dict[str, str]:
    p = root / DRIVE_MARKER
    try:
        data = json.loads(p.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("drive marker unreadable: %s", exc)
        return {"marker_problem": f"{type(exc).__name__}"}
    out: dict[str, str] = {}
    for k in ("drive_uuid", "label"):
        if isinstance(data.get(k), str) and data[k]:
            out[k] = data[k]
    return out


# Archive-drive markers (see sentinel.archive.locator / layout; not imported, to keep export independent).
ARCHIVE_ROLE = "archive"
ARCHIVE_ROOT = "Sentinel"
ARCHIVE_README = "README_ARCHIVE.txt"


def archive_drive_reason(root: Path, identity: dict[str, str] | None = None) -> str | None:
    """Why ``root`` (or the volume it is on) is an archive drive, or None. Looks at the drive marker's
    role and at the archive folder, at ``root`` and at the volume's mount point (review 2026-11-10)."""
    places = [Path(root)]
    mount = (identity or {}).get("mount_point")
    if mount and Path(mount) != Path(root):
        places.append(Path(mount))
    for base in places:
        try:
            doc = json.loads((base / DRIVE_MARKER).read_text("utf-8"))
        except (OSError, ValueError):
            doc = None
        if isinstance(doc, dict) and doc.get("role") == ARCHIVE_ROLE:
            return "its drive marker gives it the archive role"
        arch = base / ARCHIVE_ROOT
        try:
            if (arch / ARCHIVE_README).exists() or any((d / "INDEX").is_dir() for d in arch.iterdir() if d.is_dir()):
                return "it holds the archive folder 'Sentinel'"
        except OSError:
            continue
    return None


def _windows_volume_identity(path: Path) -> dict[str, str]:
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_unicode_buffer(1024)
    if not k32.GetVolumePathNameW(str(path), buf, len(buf)):
        raise TargetError(f"GetVolumePathNameW failed ({ctypes.get_last_error()})")
    mount = buf.value
    out = {"mount_point": mount}
    gbuf = ctypes.create_unicode_buffer(64)
    if k32.GetVolumeNameForVolumeMountPointW(mount, gbuf, len(gbuf)):
        out["volume_guid"] = gbuf.value.rstrip("\\").removeprefix("\\\\?\\Volume")
    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial, maxlen, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    if k32.GetVolumeInformationW(mount, label, len(label), ctypes.byref(serial), ctypes.byref(maxlen),
                                 ctypes.byref(flags), fs, len(fs)):
        out["volume_serial"] = f"{serial.value:08X}"
        out["filesystem"] = fs.value
        if label.value:
            out["label"] = label.value
    out["drive_type"] = {2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}.get(
        int(k32.GetDriveTypeW(mount)), "unknown")
    return out


def _posix_volume_identity(path: Path) -> dict[str, str]:
    # NOT EXECUTED on the development host (Windows). Uses /proc/self/mountinfo and the udev
    # /dev/disk/by-uuid links; no privileges required.
    real = os.path.realpath(path)
    best: tuple[str, str, str] | None = None
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                mnt = parts[4].replace("\\040", " ")
                sep = parts.index("-")
                fstype, source = parts[sep + 1], parts[sep + 2]
                if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and (best is None or len(mnt) > len(best[0])):
                    best = (mnt, fstype, source)
    except OSError:
        return {}
    if best is None:
        return {}
    out = {"mount_point": best[0], "filesystem": best[1], "device": best[2]}
    by_uuid = Path("/dev/disk/by-uuid")
    if by_uuid.is_dir():
        for link in by_uuid.iterdir():
            if os.path.realpath(link) == os.path.realpath(best[2]):
                out["fs_uuid"] = link.name
                break
    return out


def identify_target(root: Path) -> ExportTarget:
    """Build an ExportTarget for an already-mounted directory (no privileges needed)."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise TargetError(f"target is not a directory: {root}")
    ident = _windows_volume_identity(root) if sys.platform == "win32" else _posix_volume_identity(root)
    ident.update(_read_marker(root))
    mount = ident.get("mount_point")
    if mount:
        try:
            rel = os.path.relpath(root, mount)
        except ValueError:
            rel = "."
        if rel not in (".", ""):
            ident["subdir"] = rel.replace("\\", "/")
    return ExportTarget(root=root, identity=ident)


def _volume_id(path: Path) -> int:
    """Device/volume id of ``path`` or, if it does not exist yet, of its nearest existing parent.
    Raises TargetError if nothing can be examined (callers fail closed)."""
    p = Path(path).absolute()
    for cand in (p, *p.parents):
        try:
            return os.stat(cand).st_dev
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise TargetError(f"cannot examine {cand}: {exc}") from None
    raise TargetError(f"cannot examine {path}")


def system_paths() -> dict[str, Path]:
    if sys.platform == "win32":
        return {"system disk": Path(os.environ.get("SystemRoot") or os.environ.get("windir") or "C:\\Windows")}
    return {"system disk": Path("/")}


def protected_volume_conflicts(target: Path, protected: dict[str, Path]) -> list[str]:
    """Names of protected locations (recording storage, ledger, logs, system disk) that share a
    volume with ``target``. Any stat failure raises TargetError: the check fails closed."""
    t = _volume_id(target)
    return [name for name, p in protected.items() if _volume_id(p) == t]


def same_volume(a: Path, b: Path) -> bool:
    """True if two paths are on the same volume. Fails closed: True when either cannot be examined."""
    try:
        return _volume_id(a) == _volume_id(b)
    except TargetError:
        return True
