"""Automatic drive classification (docs/04 §4): READY, BLANK, WRONG_FORMAT, FOREIGN_DATA, UNSUITABLE.

Rules (design choices are marked *):
* UNSUITABLE wins over everything: system/boot disk, disk holding the OS, Sentinel software,
  recording spool, ledger or logs (by volume identity), page file / active swap, offline,
  read-only, health Warning/Unhealthy, smaller than ``min_size_gb``, bus not in
  ``allowed_bus_types``*, no serial and no UniqueId* (cannot be re-identified safely), or the
  protected disks could not be determined* (fail closed).
* READY: exactly one data partition, approved filesystem, valid ``SENTINEL_DRIVE.json``.
* BLANK: no partition table / no data partitions, or one empty data partition already in the
  approved filesystem but without a marker.
* WRONG_FORMAT: only empty data partitions, but not exactly one in the approved filesystem.
* FOREIGN_DATA: any files at the root of any data volume; or anything that cannot be read and
  therefore might hold data* (unrecognised filesystem, e.g. ext4/HFS+ seen from Windows; unmounted
  volume on Linux; access denied); or EFI/recovery/unknown-type partitions* (the disk was used by
  another computer); or a Sentinel marker on a non-approved filesystem. Uncertainty always moves a
  drive towards the stricter confirmation, never towards "empty".
* The Microsoft Reserved partition (created automatically by Initialize-Disk GPT) is ignored.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .marker import RootContents, inspect_volume
from .model import (
    KIND_DATA,
    KIND_EFI,
    KIND_MSR,
    KIND_RECOVERY,
    KIND_WHOLE_DISK,
    DiskInfo,
    DriveClass,
    VolumeInfo,
    fs_display,
    human_size,
)
from .policy import DrivePolicy
from .protected import ROLE_OS, ROLE_SOFTWARE, ProtectedStorage

Inspector = Callable[[VolumeInfo], RootContents]

# UNSUITABLE reasons in the order their message is preferred.
UNSUITABLE_MESSAGES: dict[str, str] = {
    "system_disk": "This drive is used by the computer itself and can never be used as an archive drive.",
    "recording_storage": "This drive holds this computer's recordings and can never be used as an archive drive.",
    "page_file": "This drive is used by the computer itself and can never be used as an archive drive.",
    "protected_unknown": "Drive checks are unavailable because the computer's own drives could not be identified. "
                         "Contact technical support.",
    "offline": "This drive is switched off in Windows and cannot be used. Contact technical support.",
    "read_only": "This drive is write-protected and cannot be used.",
    "health": "This drive is reporting a hardware problem and must not be used.",
    "too_small": "This drive is too small ({size}). Use a drive of at least {min}.",
    "not_external": "This drive is not connected through the drive dock and cannot be used as an archive drive.",
    "no_identity": "This drive does not report a serial number, so it cannot be identified safely. "
                   "Use a different drive.",
}

ALERT_FOR_CLASS = {DriveClass.BLANK: "M-001", DriveClass.WRONG_FORMAT: "M-002",
                   DriveClass.FOREIGN_DATA: "M-003", DriveClass.UNSUITABLE: "M-004"}


@dataclass(frozen=True)
class Classification:
    disk: DiskInfo
    drive_class: DriveClass
    reasons: tuple[str, ...]
    message: str                         # operator sentence, fixed wording, no paths/commands
    alert_code: str | None
    filesystem: str | None = None        # filesystem of the (first) data volume
    used_bytes: int | None = None
    marker: dict[str, Any] | None = None
    can_prepare: bool = False
    needs_admin: bool = False
    needs_serial: bool = False
    refusal: str | None = None           # why Prepare is not offered (machine token)
    protected_roles: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)  # administrator detail

    def to_dict(self) -> dict[str, Any]:
        d = self.disk
        return {
            "disk_id": d.disk_id, "class": str(self.drive_class), "message": self.message,
            "alert_code": self.alert_code, "reasons": list(self.reasons), "can_prepare": self.can_prepare,
            "needs_admin": self.needs_admin, "needs_serial": self.needs_serial, "refusal": self.refusal,
            "filesystem": self.filesystem, "used_bytes": self.used_bytes, "protected_roles": list(self.protected_roles),
            "description": d.describe(), "bus_type": d.bus_type, "identity": d.identity.to_dict(),
            "fingerprint": d.identity.fingerprint(), "drive_uuid": (self.marker or {}).get("drive_uuid"),
            "label": (self.marker or {}).get("label"), "notes": list(self.notes),
        }


def _unsuitable_reasons(disk: DiskInfo, policy: DrivePolicy, protected: ProtectedStorage,
                        protection_ok: bool) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    roles = protected.roles_on(disk)
    if (disk.is_system or disk.is_boot or any(p.is_system or p.is_boot for p in disk.partitions)
            or ROLE_OS in roles or ROLE_SOFTWARE in roles):
        reasons.append("system_disk")
    if any(r not in (ROLE_OS, ROLE_SOFTWARE) for r in roles):
        reasons.append("recording_storage")
    if any(v.page_file for v in disk.volumes):
        reasons.append("page_file")
    if not protection_ok:
        reasons.append("protected_unknown")
    if disk.is_offline:
        reasons.append("offline")
    if disk.is_read_only:
        reasons.append("read_only")
    if disk.health in ("Warning", "Unhealthy"):
        reasons.append("health")
    if disk.size_bytes < policy.min_size_bytes:
        reasons.append("too_small")
    if (disk.bus_type or "UNKNOWN") not in policy.allowed_bus_types:
        reasons.append("not_external")
    if not disk.identity.has_hardware_identity:
        reasons.append("no_identity")
    return reasons, roles


def _unsuitable_message(reasons: list[str], disk: DiskInfo, policy: DrivePolicy) -> str:
    first = next(r for r in UNSUITABLE_MESSAGES if r in reasons)
    return UNSUITABLE_MESSAGES[first].format(size=human_size(disk.size_bytes), min=human_size(policy.min_size_bytes))


def classify(disk: DiskInfo, policy: DrivePolicy, protected: ProtectedStorage, *,
             inspector: Inspector = inspect_volume, protection_ok: bool = True) -> Classification:
    reasons, roles = _unsuitable_reasons(disk, policy, protected, protection_ok)
    if reasons:
        code = "M-008" if reasons == ["protected_unknown"] else "M-004"
        return Classification(disk, DriveClass.UNSUITABLE, tuple(reasons), _unsuitable_message(reasons, disk, policy),
                              code, refusal=reasons[0], protected_roles=tuple(roles))

    approved = policy.approved_filesystem
    parts = [p for p in disk.partitions if p.kind != KIND_MSR]
    if not parts:
        why = "no_partition_table" if disk.partition_style in (None, "RAW") else "no_data_partitions"
        return _preparable(disk, policy, DriveClass.BLANK, (why,), "This drive is empty and needs to be prepared.")

    foreign: list[str] = []
    notes: list[str] = []
    data_fs: list[str | None] = []
    used = 0
    marker = None
    sentinel_marker_elsewhere = False
    for p in parts:
        if p.kind in (KIND_EFI, KIND_RECOVERY):
            foreign.append("os_partition")
            continue
        if p.kind not in (KIND_DATA, KIND_WHOLE_DISK):
            foreign.append("unknown_partition")
            notes.append(f"partition {p.number}: type {p.type_id}")
            continue
        v = p.volume
        if v is None or v.filesystem is None:
            foreign.append("unreadable")
            data_fs.append(None)
            continue
        data_fs.append(v.filesystem)
        used += v.used_bytes or 0
        rc = inspector(v)
        if not rc.readable:
            foreign.append("unreadable")
            notes.append(f"partition {p.number}: {rc.error}")
            continue
        if rc.marker is not None:
            if v.filesystem == approved:
                marker = rc.marker
            else:
                sentinel_marker_elsewhere = True
        if rc.marker_error:
            notes.append(f"partition {p.number}: invalid marker ({rc.marker_error})")
        if rc.entries:
            foreign.append("has_files")
    first_fs = next((f for f in data_fs if f), None)

    if marker is not None and len(parts) == 1:
        # With a single readable partition the only possible "foreign" reason is has_files, and
        # files on a READY drive are Sentinel's own archive. READY never offers erasing.
        return Classification(disk, DriveClass.READY, ("sentinel_drive",), "Archive drive ready.", None,
                              filesystem=first_fs, used_bytes=used, marker=marker, refusal="already_prepared",
                              notes=tuple(notes))
    if sentinel_marker_elsewhere:
        foreign.append("sentinel_drive_wrong_format")
    if marker is not None:
        foreign.append("sentinel_drive_extra_partitions")

    if foreign:
        uniq = tuple(dict.fromkeys(foreign))
        if "has_files" in uniq or "sentinel_drive_wrong_format" in uniq or "sentinel_drive_extra_partitions" in uniq:
            msg = f"This drive contains other files ({human_size(used)}). Preparing it will erase them."
        elif "os_partition" in uniq:
            msg = ("This drive appears to have been used to start a computer. "
                   "Preparing it will erase everything on it.")
        else:
            msg = "This drive contains data this computer cannot read. Preparing it will erase everything on it."
        return _preparable(disk, policy, DriveClass.FOREIGN_DATA, uniq, msg, filesystem=first_fs, used=used,
                           notes=notes)

    if len(parts) == 1 and first_fs == approved:
        return _preparable(disk, policy, DriveClass.BLANK, ("empty_volume",),
                           "This drive is empty and needs to be prepared.", filesystem=first_fs, used=used, notes=notes)
    wrong = first_fs if first_fs != approved else next((f for f in data_fs if f != approved), first_fs)
    why = "wrong_filesystem" if wrong != approved else "multiple_partitions"
    msg = (f"This drive uses the wrong format ({fs_display(wrong)}). It must be prepared before use."
           if why == "wrong_filesystem" else "This drive is not set up correctly. It must be prepared before use.")
    return _preparable(disk, policy, DriveClass.WRONG_FORMAT, (why,), msg, filesystem=first_fs, used=used, notes=notes)


def _preparable(disk: DiskInfo, policy: DrivePolicy, cls: DriveClass, reasons: tuple[str, ...], msg: str, *,
                filesystem: str | None = None, used: int | None = None, notes: Iterable[str] = ()) -> Classification:
    can, admin, serial, refusal = True, False, False, None
    if cls is DriveClass.FOREIGN_DATA:
        mode = policy.foreign_data_authorization
        if mode == "never":
            can, refusal = False, "policy_never"
        else:
            serial = True
            admin = mode == "admin_only"
    return Classification(disk, cls, reasons, msg, ALERT_FOR_CLASS[cls], filesystem=filesystem, used_bytes=used,
                          can_prepare=can, needs_admin=admin, needs_serial=serial, refusal=refusal, notes=tuple(notes))


def classify_all(disks: list[DiskInfo], policy: DrivePolicy, protected: ProtectedStorage, *,
                 inspector: Inspector = inspect_volume) -> tuple[list[Classification], list[str]]:
    """Classify every disk. Protection completeness is judged against the whole disk set."""
    ok, problems = protected.complete_for(disks)
    return [classify(d, policy, protected, inspector=inspector, protection_ok=ok) for d in disks], problems
