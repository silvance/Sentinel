"""Drive data model: what the operating system reports about a disk, and its identity.

Everything here is plain data built from read-only OS queries (Windows Storage cmdlets, Linux
lsblk). Nothing in this module touches a disk.

Identity (docs/04 §5, risk C2): a disk is identified by what the *hardware* reports — serial
number, UniqueId/WWN, model, size, bus — plus the GUIDs of its partition table and partitions.
Disk numbers (Windows ``Disk 3``), device nodes (``/dev/sdb``) and drive letters are *locators*
only: they can change between plugging in, confirming, and acting, so they are never part of
the identity and never used to decide whether a disk is safe to erase.

Caveat C2: some USB-SATA bridges (docks) report their own serial number instead of the disk's.
Two different disks in the same dock may then look alike by serial; model, size and partition
GUIDs still differ in practice, which is why the identity compares all of them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

# Partition kinds that carry user data vs. ones the OS creates for itself.
KIND_DATA = "data"
KIND_MSR = "msr"            # Microsoft Reserved (created automatically by Initialize-Disk -PartitionStyle GPT)
KIND_EFI = "efi"            # EFI system partition: the disk was used to start a computer
KIND_RECOVERY = "recovery"  # OS recovery partition
KIND_OTHER = "other"
KIND_WHOLE_DISK = "whole_disk"  # filesystem written directly on the disk, no partition table (Linux)

# GPT partition type GUIDs (lower case, no braces).
GPT_BASIC_DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
GPT_MSR = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
GPT_EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
GPT_WIN_RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
GPT_LINUX_DATA = "0fc63daf-8483-4772-8e79-3d69d8477de4"
GPT_LINUX_SWAP = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"


def norm_guid(value: Any) -> str | None:
    """'{EBD0A0A2-...}' -> 'ebd0a0a2-...'; None/empty -> None."""
    if value is None:
        return None
    s = str(value).strip().strip("{}").lower()
    return s or None


def norm_text(value: Any) -> str | None:
    """Strip padding that firmware puts in serial/model strings; empty -> None."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def norm_serial(value: Any) -> str | None:
    """Serial as shown to people: whitespace removed; NVMe serials' trailing '.' dropped.

    Windows reports the same disk as '            Z505GDE8' (Get-Disk) and 'Z505GDE8'
    (Get-PhysicalDisk); NVMe EUI-style serials end in '.'. Both are normalised here so the
    confirmation tail and identity comparison are stable across query methods.
    """
    s = norm_text(value)
    if s is None:
        return None
    s = "".join(s.split()).rstrip(".")
    return s or None


def serial_tail(serial: str | None, n: int = 4) -> str | None:
    """Last ``n`` letters/digits of the serial (what the operator must type for FOREIGN_DATA)."""
    if not serial:
        return None
    alnum = [c for c in serial if c.isalnum()]
    if len(alnum) < n:
        return None
    return "".join(alnum[-n:]).upper()


FS_DISPLAY = {
    "ntfs": "NTFS", "exfat": "exFAT", "fat32": "FAT32", "fat": "FAT", "vfat": "FAT32", "refs": "ReFS",
    "ext4": "ext4", "ext3": "ext3", "ext2": "ext2", "xfs": "XFS", "btrfs": "Btrfs", "hfsplus": "Mac (HFS+)",
    "apfs": "Mac (APFS)", "swap": "swap", "crypto_luks": "encrypted (LUKS)", "bitlocker": "encrypted (BitLocker)",
}


def norm_fs(value: Any) -> str | None:
    """Normalise filesystem names from Get-Volume / lsblk to one vocabulary (lower case)."""
    s = norm_text(value)
    if s is None:
        return None
    s = s.lower()
    if s in ("raw", "unknown"):
        return None
    if s == "vfat":
        return "fat32"
    if s in ("hfs+", "hfs"):
        return "hfsplus"
    return s


def fs_display(fs: str | None) -> str:
    if fs is None:
        return "unrecognised"
    return FS_DISPLAY.get(fs, fs)


@dataclass(frozen=True)
class VolumeInfo:
    volume_id: str                      # Windows: \\?\Volume{GUID}\ ; Linux: filesystem UUID or "dev:<path>"
    filesystem: str | None              # normalised (see norm_fs); None = no recognised filesystem
    label: str | None
    size_bytes: int | None
    free_bytes: int | None
    mount_paths: tuple[str, ...] = ()   # where it can be read: "E:\\", "\\\\?\\Volume{..}\\", "/media/x"
    page_file: bool = False             # Windows page file / Linux swap lives here
    health: str | None = None
    os_device: str | None = None        # Linux maj:min, for matching protected paths
    dev_path: str | None = None         # Linux block device node (/dev/sdb1), diagnostics + mounting

    @property
    def used_bytes(self) -> int | None:
        if self.size_bytes is None or self.free_bytes is None:
            return None
        return max(0, self.size_bytes - self.free_bytes)


@dataclass(frozen=True)
class PartitionInfo:
    number: int
    guid: str | None                    # GPT partition GUID / PARTUUID (normalised)
    type_id: str | None                 # GPT type GUID or MBR type code
    kind: str                           # KIND_*
    size_bytes: int
    is_system: bool = False
    is_boot: bool = False
    volume: VolumeInfo | None = None
    os_device: str | None = None        # Linux maj:min of the partition node


@dataclass(frozen=True)
class DriveIdentity:
    """The facts compared before anything destructive happens. Locators are deliberately absent."""

    serial: str | None
    unique_id: str | None               # Windows UniqueId (WWN / EUI-64 / VPD 0x83); Linux WWN
    model: str | None
    size_bytes: int
    bus_type: str | None
    disk_guid: str | None               # GPT disk GUID / PTUUID; None when no partition table
    partition_guids: tuple[str, ...]    # sorted
    volume_ids: tuple[str, ...]         # sorted

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["partition_guids"] = list(self.partition_guids)
        d["volume_ids"] = list(self.volume_ids)
        return d

    def fingerprint(self) -> str:
        """Stable hash of the full identity; shown-then-checked across confirm and execute."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def same_device(self, other: DriveIdentity) -> bool:
        """Same physical disk, ignoring its current contents (used *after* preparing, when the
        partition table and partitions have been replaced)."""
        return (self.serial, self.unique_id, self.model, self.size_bytes, self.bus_type) == (
            other.serial, other.unique_id, other.model, other.size_bytes, other.bus_type)

    def differences(self, other: DriveIdentity) -> list[str]:
        a, b = self.to_dict(), other.to_dict()
        return sorted(k for k in a if a[k] != b[k])

    @property
    def has_hardware_identity(self) -> bool:
        return bool(self.serial or self.unique_id)


@dataclass(frozen=True)
class DiskInfo:
    platform: str                       # "windows" | "linux"
    disk_id: str                        # locator: Windows disk number, Linux kernel name (sdb)
    serial: str | None
    model: str | None
    size_bytes: int
    bus_type: str | None                # normalised upper case: USB, SATA, NVME, SAS, SCSI, ...
    unique_id: str | None
    disk_guid: str | None
    partition_style: str | None         # GPT | MBR | RAW (RAW = no partition table)
    is_system: bool = False
    is_boot: bool = False
    is_offline: bool = False
    is_read_only: bool = False
    health: str | None = None           # Healthy | Warning | Unhealthy | None (unknown)
    removable: bool | None = None
    device_path: str | None = None      # Windows \\?\scsi#disk..., Linux /dev/sdb (diagnostics)
    pnp_device_id: str | None = None    # Windows PnP instance ID (for safe removal)
    location: str | None = None
    partitions: tuple[PartitionInfo, ...] = field(default_factory=tuple)
    os_devices: tuple[str, ...] = ()    # Linux: maj:min of the disk and everything stacked on it (LVM, LUKS)

    @property
    def identity(self) -> DriveIdentity:
        pg = sorted(p.guid for p in self.partitions if p.guid)
        vids = sorted(p.volume.volume_id for p in self.partitions if p.volume is not None)
        return DriveIdentity(serial=self.serial, unique_id=self.unique_id, model=self.model,
                             size_bytes=self.size_bytes, bus_type=self.bus_type, disk_guid=self.disk_guid,
                             partition_guids=tuple(pg), volume_ids=tuple(vids))

    @property
    def volumes(self) -> list[VolumeInfo]:
        return [p.volume for p in self.partitions if p.volume is not None]

    def describe(self) -> str:
        """Plain words for a confirmation screen: model, size, serial."""
        model = self.model or "Unknown model"
        serial = self.serial or "no serial number"
        return f"{model}, {human_size(self.size_bytes)}, serial {serial}"


class DriveClass(StrEnum):
    READY = "READY"
    BLANK = "BLANK"
    WRONG_FORMAT = "WRONG_FORMAT"
    FOREIGN_DATA = "FOREIGN_DATA"
    UNSUITABLE = "UNSUITABLE"


def human_size(n: int | None) -> str:
    """Decimal units, as printed on drive boxes (a '4 TB' drive is 4,000,787,030,016 bytes)."""
    if n is None:
        return "unknown size"
    for unit, scale in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6)):
        if n >= scale:
            v = n / scale
            return f"{v:.0f} {unit}" if v >= 100 or abs(v - round(v)) < 0.05 else f"{v:.1f} {unit}"
    return f"{n} bytes"
