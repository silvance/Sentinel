"""Builders and fakes for sentinel.drives tests. Nothing here touches a real disk."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from sentinel.drives.commands import PreparePlan
from sentinel.drives.marker import RootContents
from sentinel.drives.model import GPT_BASIC_DATA, GPT_MSR, KIND_DATA, KIND_MSR, DiskInfo, PartitionInfo, VolumeInfo
from sentinel.drives.prepare import HelperResult
from sentinel.drives.protected import ROLE_OS, ROLE_SPOOL, ProtectedItem, ProtectedStorage

FIXTURES = Path(__file__).parent / "fixtures" / "drives"
SYSTEM_VOL = "\\\\?\\Volume{17af65b4-223e-4c82-9361-18f8b22cfaa6}\\"   # C: on the development host (fixture)
TB = 10**12


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def vol(guid: str, fs: str | None = "ntfs", *, label: str | None = None, size: int = 4 * TB,
        free: int | None = None, letter: str | None = "X", page_file: bool = False,
        fs_serial: str | None = "auto") -> VolumeInfo:
    vid = f"\\\\?\\Volume{{{guid}}}\\"
    paths = ((f"{letter}:\\",) if letter else ()) + (vid,)
    if fs_serial == "auto":
        fs_serial = str(int(guid.replace("-", "")[-8:], 16)) if fs else None
    return VolumeInfo(volume_id=vid, filesystem=fs, label=label, size_bytes=size,
                      free_bytes=size if free is None else free, mount_paths=paths, page_file=page_file,
                      fs_serial=fs_serial)


def usb_disk(*, disk_id: str = "9", serial: str | None = "WD-WCC4E1234ABCD", unique_id: str | None = "5000CCA0DEADBEEF",
             model: str = "WDC WD40EFRX-68N", size: int = 4 * TB, bus: str = "USB", style: str = "GPT",
             disk_guid: str | None = "11111111-2222-3333-4444-555555555555",
             partitions: tuple[PartitionInfo, ...] = (), **kw: Any) -> DiskInfo:
    return DiskInfo(platform="windows", disk_id=disk_id, serial=serial, model=model, size_bytes=size, bus_type=bus,
                    unique_id=unique_id, disk_guid=disk_guid if style != "RAW" else None, partition_style=style,
                    health="Healthy", removable=True, pnp_device_id="USBSTOR\\DISK&VEN_WDC\\FAKE",
                    partitions=partitions, **kw)


def data_part(n: int, v: VolumeInfo | None, guid: str = "aaaaaaaa-0000-0000-0000-000000000001",
              offset: int = 135266304) -> PartitionInfo:
    return PartitionInfo(number=n, guid=guid, type_id=GPT_BASIC_DATA, kind=KIND_DATA,
                         size_bytes=v.size_bytes if v and v.size_bytes else 4 * TB, volume=v, offset=offset)


def msr_part() -> PartitionInfo:
    return PartitionInfo(number=1, guid="bbbbbbbb-0000-0000-0000-000000000001", type_id=GPT_MSR, kind=KIND_MSR,
                         size_bytes=16 * 2**20, offset=17408)


def blank_disk(**kw: Any) -> DiskInfo:
    return usb_disk(style="RAW", partitions=(), **kw)


def exfat_disk(**kw: Any) -> DiskInfo:
    return usb_disk(partitions=(data_part(1, vol("c0ffee00-0000-0000-0000-000000000001", "exfat", label="BACKUP")),), **kw)


def foreign_disk(**kw: Any) -> DiskInfo:
    v = vol("c0ffee00-0000-0000-0000-000000000002", "ntfs", label="Photos", free=3 * TB)
    return usb_disk(partitions=(data_part(1, v),), **kw)


class FakeInspector:
    """Root contents by volume id; default empty and readable."""

    def __init__(self, contents: dict[str, RootContents] | None = None) -> None:
        self.contents = contents or {}

    def __call__(self, v: VolumeInfo) -> RootContents:
        return self.contents.get(v.volume_id, RootContents(readable=True))


def protected_for(system_vol: str = SYSTEM_VOL, spool_vol: str | None = None) -> ProtectedStorage:
    items = [ProtectedItem(ROLE_OS, "C:\\Windows", volume_id=system_vol)]
    items.append(ProtectedItem(ROLE_SPOOL, "spool", volume_id=spool_vol or system_vol))
    return ProtectedStorage(tuple(items))


def system_disk() -> DiskInfo:
    v = vol("17af65b4-223e-4c82-9361-18f8b22cfaa6", "ntfs", letter="C", page_file=True)
    return DiskInfo(platform="windows", disk_id="0", serial="SYS123", model="NVMe System", size_bytes=2 * TB,
                    bus_type="NVME", unique_id="eui.SYS", disk_guid="99999999-0000-0000-0000-000000000000",
                    partition_style="GPT", is_system=True, is_boot=True, health="Healthy",
                    partitions=(replace(data_part(3, v), is_boot=True),))


class Sequence:
    """enumerate() returning successive snapshots (the last one repeats)."""

    def __init__(self, *snapshots: list[DiskInfo]) -> None:
        self.snapshots = list(snapshots)
        self.calls = 0

    def __call__(self) -> list[DiskInfo]:
        i = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return list(self.snapshots[i])


class FakeRunner:
    """Records plans; optionally switches the fake world to the 'after' state when run."""

    def __init__(self, result: HelperResult, on_run: Any = None) -> None:
        self.result = result
        self.plans: list[PreparePlan] = []
        self.on_run = on_run

    def run(self, plan: PreparePlan) -> HelperResult:
        self.plans.append(plan)
        if self.on_run:
            self.on_run(plan)
        return self.result


def prepared_disk_like(d: DiskInfo, label: str, fs: str = "ntfs") -> DiskInfo:
    v = vol("ddddeeee-0000-0000-0000-000000000009", fs, label=label, letter="S")
    return replace(d, partition_style="GPT", disk_guid="12121212-0000-0000-0000-000000000000",
                   partitions=(msr_part(), data_part(2, v, guid="ffffffff-0000-0000-0000-000000000002")))
