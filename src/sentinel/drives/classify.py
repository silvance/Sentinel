"""Automatic drive classification (docs/04 §4): READY, BLANK, WRONG_FORMAT, FOREIGN_DATA,
SENTINEL_EVIDENCE, UNSUITABLE.

Rules, in order of precedence (design choices are marked *):
1. UNSUITABLE (hard): system/boot disk, disk holding the OS, Sentinel software, recording spool,
   ledger or logs (by volume identity), page file / active swap.
2. READY: exactly one data partition, approved filesystem, valid ``SENTINEL_DRIVE.json``, and no
   other UNSUITABLE reason.
3. SENTINEL_EVIDENCE* (review finding 1.1): anything suggesting the drive holds Sentinel recordings —
   a marker in any state (corrupt, newer version, ``.partial``, on a non-approved filesystem, on a
   second partition), an archive layout (``Sentinel/``), a review-drive layout (``Manifests/
   Export_Manifest.json``, ``README_FIRST.txt`` with ``Recordings/``/``Originals/``…), or a
   drive_uuid / volume GUID / filesystem serial / hardware identity known in the ledger. **Never
   erasable by this tool under any policy.**
4. UNSUITABLE (other): offline, read-only, health Warning/Unhealthy, smaller than ``min_size_gb``,
   bus not in ``allowed_bus_types``*, no serial and no UniqueId*, protected disks could not be
   determined*, ledger unreadable* (fail closed).
5. BLANK: no partition table / no data partitions, or one empty data partition already in the
   approved filesystem but without a marker.
6. WRONG_FORMAT: only empty data partitions, but not exactly one in the approved filesystem.
7. FOREIGN_DATA: files at the root of any data volume; or anything that cannot be read and therefore
   might hold data* (unrecognised filesystem, unmounted volume on Linux, access denied); or
   EFI/recovery/unknown-type partitions*. Uncertainty always moves a drive towards the stricter
   path, never towards "empty".
The Microsoft Reserved partition (created automatically by Initialize-Disk GPT) is ignored.

Operator wording: the destructive action is called **Erase and Set Up Drive** (finding 1.4), so it
cannot be confused with the export action **Prepare Review Drive** (U4).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from .known import KnownDrives
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
ACTION_NAME = "Erase and Set Up Drive"

# UNSUITABLE reasons in the order their message is preferred.
UNSUITABLE_MESSAGES: dict[str, str] = {
    "system_disk": "This drive is used by the computer itself and can never be used as an archive drive.",
    "recording_storage": "This drive holds this computer's recordings and can never be used as an archive drive.",
    "page_file": "This drive is used by the computer itself and can never be used as an archive drive.",
    "protected_unknown": "Drive checks are unavailable because the computer's own drives could not be identified. "
                         "Contact technical support.",
    "ledger_unavailable": "Drive checks are unavailable because the recording records could not be read. "
                          "Contact technical support.",
    "offline": "This drive is switched off in Windows and cannot be used. Contact technical support.",
    "read_only": "This drive is write-protected and cannot be used.",
    "health": "This drive is reporting a hardware problem and must not be used.",
    "too_small": "This drive is too small ({size}). Use a drive of at least {min}.",
    "not_external": "This drive is not connected through the drive dock and cannot be used as an archive drive.",
    "no_identity": "This drive does not report a serial number, so it cannot be identified safely. "
                   "Use a different drive.",
}
HARD_UNSUITABLE = ("system_disk", "recording_storage", "page_file")
CHECKS_UNAVAILABLE = ("protected_unknown", "ledger_unavailable")
EVIDENCE_MESSAGE = "This drive holds Sentinel recordings and will not be erased. Contact technical support."
# Why a drive counts as holding recordings, in plain words (owner request 2026-11-12: the refusal named no
# reason). Several reasons can give the same sentence; each sentence is shown once.
EVIDENCE_REASON_WORDS = {
    "marker": "It has Sentinel's drive file (SENTINEL_DRIVE.json).",
    "marker_partial": "It has Sentinel's drive file (SENTINEL_DRIVE.json).",
    "marker_invalid": "It has Sentinel's drive file (SENTINEL_DRIVE.json).",
    "marker_unapproved_or_extra": "It has Sentinel's drive file (SENTINEL_DRIVE.json).",
    "archive_layout": "It has a folder named Sentinel at the top level.",
    "export_manifest": "It has folders laid out like a Sentinel review drive.",
    "export_manifest_unreadable": "It has folders laid out like a Sentinel review drive.",
    "review_layout": "It has folders laid out like a Sentinel review drive.",
    "known_drive_uuid": "Sentinel's records show it set up this drive before.",
    "known_volume": "Sentinel's records show it set up or wrote to this drive before.",
    "known_filesystem": "Sentinel's records show it set up or wrote to this drive before.",
    "known_hardware": "Sentinel's records show it set up a drive with this serial number before.",
}


def evidence_why(reasons: Iterable[str]) -> str:
    """The plain-words reasons a drive counts as holding recordings ("" if none is known)."""
    return " ".join(dict.fromkeys(EVIDENCE_REASON_WORDS[r] for r in reasons if r in EVIDENCE_REASON_WORDS))
BLANK_MESSAGE = f"This drive is empty and must be set up before use. Press {ACTION_NAME}."
ERASE_DISABLED_MESSAGE = "Setting up drives is not enabled on this system yet. Contact technical support."

ALERT_FOR_CLASS = {DriveClass.BLANK: "M-001", DriveClass.WRONG_FORMAT: "M-002",
                   DriveClass.FOREIGN_DATA: "M-003", DriveClass.UNSUITABLE: "M-004",
                   DriveClass.SENTINEL_EVIDENCE: "M-009"}
ERASE_DISABLED_CODE = "M-010"


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
    refusal: str | None = None           # why erasing is not offered (machine token)
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
            # For the window's drive list (where a prepared drive can be opened).
            "size_bytes": d.size_bytes, "model": d.model, "serial": d.serial, "serial_hidden": d.serial_hidden,
            "mount_paths": [p for v in d.volumes for p in v.mount_paths],
            # The marker's role: "archive" for the archive drive (sentinel.archive.locator adds it),
            # None for a prepared drive without one (review drives), so the window can tell them apart.
            "role": _marker_role(self.marker),
        }


def _marker_role(marker: dict[str, Any] | None) -> str | None:
    role = (marker or {}).get("role")
    return role if isinstance(role, str) and role else None


def _unsuitable_reasons(disk: DiskInfo, policy: DrivePolicy, protected: ProtectedStorage,
                        protection_ok: bool, known: KnownDrives) -> tuple[list[str], list[str]]:
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
    if known.error:
        reasons.append("ledger_unavailable")
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


def _unsuitable(disk: DiskInfo, policy: DrivePolicy, reasons: list[str], roles: list[str]) -> Classification:
    first = next(r for r in UNSUITABLE_MESSAGES if r in reasons)
    msg = UNSUITABLE_MESSAGES[first].format(size=human_size(disk.size_bytes), min=human_size(policy.min_size_bytes))
    code = "M-008" if all(r in CHECKS_UNAVAILABLE for r in reasons) else "M-004"
    return Classification(disk, DriveClass.UNSUITABLE, tuple(reasons), msg, code, refusal=reasons[0],
                          protected_roles=tuple(roles))


@dataclass
class _Scan:
    parts: list[Any]
    foreign: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    data_fs: list[str | None] = field(default_factory=list)
    used: int = 0
    marker: dict[str, Any] | None = None          # valid marker on an approved-filesystem partition
    marker_uuids: list[str] = field(default_factory=list)
    signs: list[str] = field(default_factory=list)


def _scan(disk: DiskInfo, policy: DrivePolicy, inspector: Inspector) -> _Scan:
    s = _Scan(parts=[p for p in disk.partitions if p.kind != KIND_MSR])
    for p in s.parts:
        if p.kind in (KIND_EFI, KIND_RECOVERY):
            s.foreign.append("os_partition")
            continue
        if p.kind not in (KIND_DATA, KIND_WHOLE_DISK):
            s.foreign.append("unknown_partition")
            s.notes.append(f"partition {p.number}: type {p.type_id}")
            continue
        v = p.volume
        if v is None or v.filesystem is None:
            s.foreign.append("unreadable")
            s.data_fs.append(None)
            continue
        s.data_fs.append(v.filesystem)
        s.used += v.used_bytes or 0
        rc = inspector(v)
        if not rc.readable:
            s.foreign.append("unreadable")
            s.notes.append(f"partition {p.number}: {rc.error}")
            continue
        s.signs.extend(rc.sentinel_signs)
        if rc.marker is not None:
            s.marker_uuids.append(str(rc.marker.get("drive_uuid")))
            if v.filesystem == policy.approved_filesystem and s.marker is None:
                s.marker = rc.marker
            else:
                s.signs.append("marker_unapproved_or_extra")
        if rc.marker_error:
            s.signs.append("marker_invalid")
            s.notes.append(f"partition {p.number}: invalid marker ({rc.marker_error})")
        if rc.entries:
            s.foreign.append("has_files")
    return s


def classify(disk: DiskInfo, policy: DrivePolicy, protected: ProtectedStorage, *,
             inspector: Inspector = inspect_volume, protection_ok: bool = True,
             known: KnownDrives | None = None) -> Classification:
    known = known or KnownDrives()
    reasons, roles = _unsuitable_reasons(disk, policy, protected, protection_ok, known)
    if any(r in HARD_UNSUITABLE for r in reasons):
        return _unsuitable(disk, policy, reasons, roles)

    s = _scan(disk, policy, inspector)
    first_fs = next((f for f in s.data_fs if f), None)
    ready = s.marker is not None and len(s.parts) == 1 and "marker_invalid" not in s.signs
    if ready and not reasons:
        # Files on a READY drive are Sentinel's own archive. READY never offers erasing.
        return Classification(disk, DriveClass.READY, ("sentinel_drive",), "Archive drive ready.", None,
                              filesystem=first_fs, used_bytes=s.used, marker=s.marker, refusal="already_prepared",
                              notes=tuple(s.notes))
    evidence = list(dict.fromkeys(s.signs + known.matches(disk, tuple(s.marker_uuids))))
    if evidence:
        return Classification(disk, DriveClass.SENTINEL_EVIDENCE, tuple(evidence), EVIDENCE_MESSAGE,
                              ALERT_FOR_CLASS[DriveClass.SENTINEL_EVIDENCE], filesystem=first_fs, used_bytes=s.used,
                              marker=s.marker, refusal="sentinel_evidence", notes=tuple(s.notes))
    if reasons:
        return _unsuitable(disk, policy, reasons, roles)

    approved = policy.approved_filesystem
    if not s.parts:
        why = "no_partition_table" if disk.partition_style in (None, "RAW") else "no_data_partitions"
        return _preparable(disk, policy, DriveClass.BLANK, (why,), BLANK_MESSAGE)
    if s.foreign:
        uniq = tuple(dict.fromkeys(s.foreign))
        if "has_files" in uniq:
            msg = f"This drive contains other files ({human_size(s.used)}). Erasing it will delete them."
        elif "os_partition" in uniq:
            msg = "This drive appears to have been used to start a computer. Erasing it will delete everything on it."
        else:
            msg = "This drive contains data this computer cannot read. Erasing it will delete everything on it."
        return _preparable(disk, policy, DriveClass.FOREIGN_DATA, uniq, msg, filesystem=first_fs, used=s.used,
                           notes=s.notes)
    if len(s.parts) == 1 and first_fs == approved:
        return _preparable(disk, policy, DriveClass.BLANK, ("empty_volume",), BLANK_MESSAGE, filesystem=first_fs,
                           used=s.used, notes=s.notes)
    wrong = first_fs if first_fs != approved else next((f for f in s.data_fs if f != approved), first_fs)
    why = "wrong_filesystem" if wrong != approved else "multiple_partitions"
    msg = (f"This drive uses the wrong format ({fs_display(wrong)}). It must be set up before use. "
           f"Press {ACTION_NAME}." if why == "wrong_filesystem"
           else f"This drive is not set up correctly. Press {ACTION_NAME}.")
    return _preparable(disk, policy, DriveClass.WRONG_FORMAT, (why,), msg, filesystem=first_fs, used=s.used,
                       notes=s.notes)


def _preparable(disk: DiskInfo, policy: DrivePolicy, cls: DriveClass, reasons: tuple[str, ...], msg: str, *,
                filesystem: str | None = None, used: int | None = None, notes: Iterable[str] = ()) -> Classification:
    can, admin, serial, refusal = True, False, False, None
    code = ALERT_FOR_CLASS[cls]
    if not policy.erase_enabled:
        # Owner decision 2026-11-10: the class is still reported, but no erase button is offered.
        first = msg.split(". ")[0].rstrip(".") + "."
        return Classification(disk, cls, reasons, f"{first} {ERASE_DISABLED_MESSAGE}", ERASE_DISABLED_CODE,
                              filesystem=filesystem, used_bytes=used, can_prepare=False, refusal="erase_disabled",
                              notes=tuple(notes))
    if cls is DriveClass.FOREIGN_DATA:
        mode = policy.foreign_data_authorization
        if mode == "never":
            can, refusal = False, "policy_never"
        else:
            serial = True
            admin = mode == "admin_only"
    return Classification(disk, cls, reasons, msg, code, filesystem=filesystem, used_bytes=used,
                          can_prepare=can, needs_admin=admin, needs_serial=serial, refusal=refusal, notes=tuple(notes))


def classify_all(disks: list[DiskInfo], policy: DrivePolicy, protected: ProtectedStorage, *,
                 inspector: Inspector = inspect_volume,
                 known: KnownDrives | None = None) -> tuple[list[Classification], list[str]]:
    """Classify every disk. Protection completeness is judged against the whole disk set."""
    ok, problems = protected.complete_for(disks)
    known = known or KnownDrives()
    if known.error:
        problems = [*problems, f"ledger: {known.error}"]
    classes = [classify(d, policy, protected, inspector=inspector, protection_ok=ok, known=known) for d in disks]
    return [_refuse_lookalike(c, disks) for c in classes], problems


LOOKALIKE_MESSAGE = ("Another connected drive looks exactly like this one to the computer (the drive dock hides "
                     "their serial numbers and both are empty). Remove the other drive, then try again.")


def _refuse_lookalike(c: Classification, disks: list[DiskInfo]) -> Classification:
    """Erasing is refused while another connected disk has the same *full* identity: same serial,
    model and size and the same partition table and volume IDs (C2: a dock that reports one
    placeholder serial for both bays, with two blank disks of one model). Only then could the disk
    confirmed and the disk erased be different ones without the identity check before erasing
    noticing. A prepared drive (e.g. the current archive drive) next to a new one of the same model
    differs in its partition and volume IDs, so it is no lookalike (field report 2026-11-10)."""
    if not c.can_prepare:
        return c
    fp = c.disk.identity.fingerprint()
    if not any(d.disk_id != c.disk.disk_id and d.identity.fingerprint() == fp for d in disks):
        return c
    return replace(c, can_prepare=False, refusal="lookalike", message=LOOKALIKE_MESSAGE,
                   notes=(*c.notes, "another connected disk cannot be told apart from this one"))
