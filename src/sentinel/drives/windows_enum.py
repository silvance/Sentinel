"""Windows disk enumeration (read-only) via the built-in Storage module and CIM.

Verified on the development host (Windows 11 Home 10.0.26200, non-elevated, Medium integrity):
``Get-Disk``, ``Get-Partition``, ``Get-Volume``, ``Get-CimInstance Win32_Volume`` and
``Get-CimInstance Win32_DiskDrive`` all return complete data without administrator rights.
The raw JSON of that host is kept as a test fixture (tests/fixtures/drives/windows_dev_host.json).

The PowerShell 5.1 JSON serialiser emits most Storage enums as strings (observed: BusType "SATA",
PartitionStyle "GPT", HealthStatus "Healthy"); integer codes are mapped too in case another
Windows build emits the raw CIM values.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Any

from .errors import DriveQueryError, ProtectedStorageError
from .model import (
    GPT_BASIC_DATA,
    GPT_EFI,
    GPT_MSR,
    GPT_WIN_RECOVERY,
    KIND_DATA,
    KIND_EFI,
    KIND_MSR,
    KIND_OTHER,
    KIND_RECOVERY,
    DiskInfo,
    PartitionInfo,
    VolumeInfo,
    norm_fs,
    norm_guid,
    norm_serial,
    norm_text,
)

ENUM_SCHEMA = 1

# Read-only. Select-Object keeps the JSON small and the property set explicit.
ENUM_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$disks = @(Get-Disk | Select-Object Number, FriendlyName, Model, SerialNumber, UniqueId, BusType, Size,
    PartitionStyle, Guid, Signature, IsSystem, IsBoot, IsOffline, IsReadOnly, HealthStatus, OperationalStatus, Path, Location)
$parts = @(Get-Partition | Select-Object DiskNumber, PartitionNumber, DriveLetter, Guid, GptType, MbrType, Type,
    Size, Offset, IsSystem, IsBoot, IsHidden, AccessPaths)
$vols = @(Get-Volume | Select-Object UniqueId, Path, DriveLetter, FileSystem, FileSystemLabel, Size, SizeRemaining,
    HealthStatus, DriveType, OperationalStatus)
$w32 = @(Get-CimInstance -ClassName Win32_Volume | Select-Object DeviceID, SerialNumber, PageFilePresent, BootVolume, SystemVolume)
$dd = @(Get-CimInstance -ClassName Win32_DiskDrive | Select-Object Index, PNPDeviceID, SerialNumber, InterfaceType)
[pscustomobject]@{
    schema = 1
    ps_version = $PSVersionTable.PSVersion.ToString()
    disks = $disks
    partitions = $parts
    volumes = $vols
    win32_volumes = $w32
    disk_drives = $dd
} | ConvertTo-Json -Depth 5 -Compress
"""

BUS_TYPES = {0: "UNKNOWN", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "FIBRE CHANNEL", 7: "USB",
             8: "RAID", 9: "ISCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC", 14: "VIRTUAL",
             15: "FILE BACKED VIRTUAL", 16: "STORAGE SPACES", 17: "NVME"}
HEALTH = {0: "Healthy", 1: "Warning", 2: "Unhealthy", 5: "Unknown"}
PART_STYLE = {0: "RAW", 1: "MBR", 2: "GPT"}
REMOVABLE_BUSES = ("USB", "SD", "MMC", "1394")


def _list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _bool(v: Any) -> bool:
    return bool(v) if v is not None else False


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _enum(v: Any, table: dict[int, str]) -> str | None:
    if v is None:
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        return table.get(v, str(v))
    return norm_text(v)


def _serial_str(v: Any) -> str | None:
    """Volume serial / MBR signature as a decimal string (the elevated helper uses the same form)."""
    n = _int(v)
    return str(n) if n is not None and n != 0 else None


def _partition_kind(p: dict[str, Any]) -> str:
    gpt = norm_guid(p.get("GptType"))
    if gpt == GPT_MSR:
        return KIND_MSR
    if gpt == GPT_EFI:
        return KIND_EFI
    if gpt == GPT_WIN_RECOVERY:
        return KIND_RECOVERY
    if gpt == GPT_BASIC_DATA:
        return KIND_DATA
    t = (norm_text(p.get("Type")) or "").lower()
    if gpt is None:  # MBR disk: Type is the descriptive string ("IFS", "FAT32", "Recovery", ...)
        if t == "recovery" or _int(p.get("MbrType")) == 0x27:
            return KIND_RECOVERY
        if t == "system":
            return KIND_EFI
        if t == "reserved":
            return KIND_MSR
        return KIND_DATA
    return KIND_OTHER  # unknown GPT type: Linux/macOS/other OS partition — never treated as empty


def parse_windows_snapshot(data: dict[str, Any]) -> list[DiskInfo]:
    """Turn the ENUM_SCRIPT JSON into DiskInfo objects (pure; unit-tested with fixtures)."""
    if not isinstance(data, dict) or "disks" not in data:
        raise DriveQueryError("enum_bad_output", "disk enumeration returned unexpected data")
    volumes: dict[str, dict[str, Any]] = {}
    for v in _list(data.get("volumes")):
        for key in (v.get("Path"), v.get("UniqueId")):
            if key:
                volumes[str(key).lower()] = v
    w32 = {str(w.get("DeviceID") or "").lower(): w for w in _list(data.get("win32_volumes"))}
    pnp = {_int(d.get("Index")): d for d in _list(data.get("disk_drives"))}
    parts_by_disk: dict[int, list[dict[str, Any]]] = {}
    for p in _list(data.get("partitions")):
        n = _int(p.get("DiskNumber"))
        if n is not None:
            parts_by_disk.setdefault(n, []).append(p)

    out: list[DiskInfo] = []
    for d in _list(data.get("disks")):
        number = _int(d.get("Number"))
        if number is None:
            continue
        partitions: list[PartitionInfo] = []
        for p in sorted(parts_by_disk.get(number, []), key=lambda x: _int(x.get("PartitionNumber")) or 0):
            access = tuple(str(a) for a in _list(p.get("AccessPaths")) if a)
            vol_raw = None
            vol_key = None
            for a in access:
                if a.lower().startswith("\\\\?\\volume{") and a.lower() in volumes:
                    vol_raw, vol_key = volumes[a.lower()], a
                    break
            volume = None
            if vol_raw is not None and vol_key is not None:
                w = w32.get(vol_key.lower(), {})
                volume = VolumeInfo(
                    volume_id=vol_key, filesystem=norm_fs(vol_raw.get("FileSystem")),
                    label=norm_text(vol_raw.get("FileSystemLabel")), size_bytes=_int(vol_raw.get("Size")),
                    free_bytes=_int(vol_raw.get("SizeRemaining")), mount_paths=access,
                    page_file=_bool(w.get("PageFilePresent")), health=_enum(vol_raw.get("HealthStatus"), HEALTH),
                    fs_serial=_serial_str(w.get("SerialNumber")))
            partitions.append(PartitionInfo(
                number=_int(p.get("PartitionNumber")) or 0, guid=norm_guid(p.get("Guid")),
                type_id=norm_guid(p.get("GptType")) or (str(p.get("MbrType")) if p.get("MbrType") is not None else None),
                kind=_partition_kind(p), size_bytes=_int(p.get("Size")) or 0,
                is_system=_bool(p.get("IsSystem")), is_boot=_bool(p.get("IsBoot")), volume=volume,
                offset=_int(p.get("Offset"))))
        bus = _enum(d.get("BusType"), BUS_TYPES)
        bus = bus.upper() if bus else None
        style = _enum(d.get("PartitionStyle"), PART_STYLE)
        style = style.upper() if style else None
        dd = pnp.get(number, {})
        # Serial: Get-Disk's, else Win32_DiskDrive's (same Index). The elevated helper applies the same rule
        # (commands.WINDOWS_TEMPLATE, Disk-Serial); review 2026-11-12: it used to read Get-Disk only, so a
        # drive whose serial came from Win32_DiskDrive could never be set up ("The drive changed").
        out.append(DiskInfo(
            platform="windows", disk_id=str(number),
            serial=norm_serial(d.get("SerialNumber")) or norm_serial(dd.get("SerialNumber")),
            model=norm_text(d.get("Model")) or norm_text(d.get("FriendlyName")),
            size_bytes=_int(d.get("Size")) or 0, bus_type=bus, unique_id=norm_text(d.get("UniqueId")),
            disk_guid=norm_guid(d.get("Guid")), partition_style=style,
            is_system=_bool(d.get("IsSystem")), is_boot=_bool(d.get("IsBoot")),
            is_offline=_bool(d.get("IsOffline")), is_read_only=_bool(d.get("IsReadOnly")),
            health=_enum(d.get("HealthStatus"), HEALTH), removable=(bus in REMOVABLE_BUSES) if bus else None,
            device_path=norm_text(d.get("Path")), pnp_device_id=norm_text(dd.get("PNPDeviceID")),
            location=norm_text(d.get("Location")), partitions=tuple(partitions),
            mbr_signature=_serial_str(d.get("Signature")) if style == "MBR" else None))
    out.sort(key=lambda x: int(x.disk_id))
    return out


def capture_windows_snapshot(timeout_s: float = 120.0) -> dict[str, Any]:
    """Run ENUM_SCRIPT (read-only, no elevation) and return the raw JSON."""
    from .powershell import run_powershell

    text = run_powershell(ENUM_SCRIPT, timeout_s=timeout_s).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise DriveQueryError("enum_bad_output", "disk enumeration did not return JSON",
                              {"output": text[-1000:]}) from None
    if not isinstance(data, dict) or data.get("schema") != ENUM_SCHEMA:
        raise DriveQueryError("enum_bad_output", "disk enumeration returned an unknown format")
    return data


def enumerate_windows(timeout_s: float = 120.0) -> list[DiskInfo]:
    return parse_windows_snapshot(capture_windows_snapshot(timeout_s))


# ---------------------------------------------------------------------------- protected volumes

def _nearest_existing(path: Path) -> Path:
    p = Path(os.path.realpath(path))
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return p


def windows_volume_id_for_path(path: str | Path) -> str:
    """``\\\\?\\Volume{GUID}\\`` of the volume holding ``path`` (identity, not drive letter).

    Uses GetVolumePathNameW (mount point of the path, following mounted folders) and
    GetVolumeNameForVolumeMountPointW. Network drives and SUBST drives have no volume GUID and
    raise ProtectedStorageError: Sentinel then refuses to prepare anything (fail closed).
    """
    if sys.platform != "win32":
        raise ProtectedStorageError("not_windows", "Windows volume lookup is only available on Windows")
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumePathNameW.restype = wintypes.BOOL
    k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
    target = str(_nearest_existing(Path(path)))
    mount = ctypes.create_unicode_buffer(1024)
    if not k32.GetVolumePathNameW(target, mount, len(mount)):
        raise ProtectedStorageError("volume_path_failed", f"cannot find the volume holding {target}",
                                    {"winerror": ctypes.get_last_error()})
    name = ctypes.create_unicode_buffer(64)
    if not k32.GetVolumeNameForVolumeMountPointW(mount.value, name, len(name)):
        raise ProtectedStorageError("volume_guid_failed", f"no volume identity for {mount.value} (network or SUBST drive?)",
                                    {"winerror": ctypes.get_last_error(), "mount": mount.value})
    return name.value
