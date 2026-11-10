"""Drive policy: which drives may be prepared, how, and who may authorise it.

Every default here is a DESIGN CHOICE of this branch, not an owner decision, and is listed as an
open item in docs/branches/design-drive-prepare.md:

* ``approved_filesystem = "ntfs"`` — provisional, pending policy decision C1 (docs/02).
* ``foreign_data_authorization = "admin_only"`` — open owner decision in docs/04 §4
  (admin_only | operator_with_serial | never).
* ``allowed_bus_types = ("USB",)`` — the archive dock (Sabrent) is a USB device; disks built into
  the computer (SATA/NVMe) are never offered unless an administrator lists their bus here.
* ``min_size_gb = 128`` — less than about one day of recording at the upper bitrate estimate
  (12 Mbit/s ≈ 130 GB/day, docs/04 §1) is not useful as an archive drive.

The policy is read from an optional ``[drives]`` table in the Sentinel TOML file. ``sentinel.config``
is not changed by this branch; the drives CLI strips ``[drives]`` before passing the rest of the
file to ``load_config_dict``. Integration task: accept ``[drives]`` in ``sentinel.config``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any

FOREIGN_POLICIES = ("admin_only", "operator_with_serial", "never")
# Filesystems Sentinel knows how to create. Windows Format-Volume accepts FAT, FAT32, exFAT, NTFS,
# ReFS (ValidateSet checked on the development host); only NTFS and exFAT are offered. ext4 is
# Linux-only and unreadable by Windows reviewers (U3), so it is accepted only if explicitly chosen.
APPROVABLE_FILESYSTEMS = ("ntfs", "exfat", "ext4")
# Maximum volume label lengths: NTFS 32, exFAT 11 (Windows limits), ext4 16.
LABEL_MAX = {"ntfs": 32, "exfat": 11, "ext4": 16}
_LABEL_RE = re.compile(r"^[A-Z0-9_]{1,32}$")


class DrivePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class DrivePolicy:
    approved_filesystem: str = "ntfs"
    foreign_data_authorization: str = "admin_only"
    allowed_bus_types: tuple[str, ...] = ("USB",)
    min_size_gb: float = 128.0
    label_prefix: str = "SENTINEL_"
    serial_confirm_chars: int = 4

    @property
    def min_size_bytes(self) -> int:
        return int(self.min_size_gb * 10**9)

    def label_for(self, n: int) -> str:
        label = f"{self.label_prefix}{n}"
        limit = LABEL_MAX[self.approved_filesystem]
        if not _LABEL_RE.fullmatch(label) or len(label) > limit:
            raise DrivePolicyError(f"volume label {label!r} is not valid for {self.approved_filesystem} "
                                   f"(A-Z, 0-9, _ ; at most {limit} characters)")
        return label


def load_policy(raw: dict[str, Any] | None) -> DrivePolicy:
    raw = dict(raw or {})
    known = {f.name for f in fields(DrivePolicy)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise DrivePolicyError(f"[drives] unknown key(s): {', '.join(unknown)}")
    kw: dict[str, Any] = {}
    if "approved_filesystem" in raw:
        fs = str(raw["approved_filesystem"]).lower()
        if fs not in APPROVABLE_FILESYSTEMS:
            raise DrivePolicyError(f"[drives] approved_filesystem must be one of {', '.join(APPROVABLE_FILESYSTEMS)}")
        kw["approved_filesystem"] = fs
    if "foreign_data_authorization" in raw:
        v = raw["foreign_data_authorization"]
        if v not in FOREIGN_POLICIES:
            raise DrivePolicyError(f"[drives] foreign_data_authorization must be one of {', '.join(FOREIGN_POLICIES)}")
        kw["foreign_data_authorization"] = v
    if "allowed_bus_types" in raw:
        v = raw["allowed_bus_types"]
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
            raise DrivePolicyError("[drives] allowed_bus_types must be a non-empty list of names, e.g. [\"USB\"]")
        kw["allowed_bus_types"] = tuple(x.upper() for x in v)
    if "min_size_gb" in raw:
        v = raw["min_size_gb"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 1:
            raise DrivePolicyError("[drives] min_size_gb must be a number >= 1")
        kw["min_size_gb"] = float(v)
    if "label_prefix" in raw:
        v = raw["label_prefix"]
        if not isinstance(v, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", v):
            raise DrivePolicyError("[drives] label_prefix must be 1-16 characters A-Z, 0-9, _ (starting with a letter)")
        kw["label_prefix"] = v
    if "serial_confirm_chars" in raw:
        v = raw["serial_confirm_chars"]
        if isinstance(v, bool) or not isinstance(v, int) or not 4 <= v <= 8:
            raise DrivePolicyError("[drives] serial_confirm_chars must be a whole number from 4 to 8")
        kw["serial_confirm_chars"] = v
    pol = DrivePolicy(**kw)
    pol.label_for(1)  # label prefix must fit the chosen filesystem
    return pol
