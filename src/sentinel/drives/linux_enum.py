"""Linux disk enumeration (read-only) via ``lsblk --json`` (util-linux).

NOT EXECUTED on a Linux appliance. The parser is unit-tested with (a) the REAL lsblk JSON captured
from the WSL2 Fedora VM on the development host (util-linux 2.41.4; virtual disks, no partitions)
and (b) a constructed fixture with a USB disk and partitions in the same format. Column names were
checked against that lsblk binary.

Health (SMART) is not queried: ``smartctl`` needs root and is not installed by default; ``health``
is None (unknown) and the operator message never claims a drive is healthy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import DriveQueryError, ProtectedStorageError
from .model import (
    GPT_BASIC_DATA,
    GPT_EFI,
    GPT_LINUX_DATA,
    GPT_LINUX_SWAP,
    GPT_MSR,
    GPT_WIN_RECOVERY,
    KIND_DATA,
    KIND_EFI,
    KIND_MSR,
    KIND_OTHER,
    KIND_RECOVERY,
    KIND_WHOLE_DISK,
    DiskInfo,
    PartitionInfo,
    VolumeInfo,
    norm_fs,
    norm_guid,
    norm_serial,
    norm_text,
)

LSBLK_COLUMNS = ("NAME,PATH,KNAME,PKNAME,MAJ:MIN,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,RO,RM,HOTPLUG,PTTYPE,PTUUID,"
                 "PARTUUID,PARTTYPE,PARTLABEL,FSTYPE,LABEL,UUID,MOUNTPOINTS,FSUSED,FSAVAIL,STATE")
LSBLK_ARGV = ("lsblk", "--json", "--bytes", "--output", LSBLK_COLUMNS)

_GPT_KIND = {GPT_BASIC_DATA: KIND_DATA, GPT_LINUX_DATA: KIND_DATA, GPT_MSR: KIND_MSR, GPT_EFI: KIND_EFI,
             GPT_WIN_RECOVERY: KIND_RECOVERY, GPT_LINUX_SWAP: KIND_OTHER}
_MBR_DATA = {"0x7", "0xb", "0xc", "0xe", "0x6", "0x83", "0x4", "0x1"}


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _flag(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip() in ("1", "true", "yes")
    return bool(v)


def _mounts(node: dict[str, Any]) -> list[str]:
    mp = node.get("mountpoints")
    if mp is None and "mountpoint" in node:  # older util-linux (< 2.37) has a single MOUNTPOINT column
        mp = [node.get("mountpoint")]
    return [m for m in (mp or []) if m]


def _descendants(node: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in node.get("children") or []:
        out.append(c)
        out.extend(_descendants(c))
    return out


def _part_kind(parttype: Any) -> str:
    t = norm_guid(parttype)
    if t is None:
        return KIND_OTHER
    if t in _GPT_KIND:
        return _GPT_KIND[t]
    if t.startswith("0x"):
        if t in _MBR_DATA:
            return KIND_DATA
        if t == "0xef":
            return KIND_EFI
        if t == "0x27":
            return KIND_RECOVERY
    return KIND_OTHER


def _volume(node: dict[str, Any], stacked: list[dict[str, Any]]) -> VolumeInfo | None:
    """Filesystem on this node (or on a single device stacked on it, e.g. an opened LUKS container)."""
    fs = norm_fs(node.get("fstype"))
    if fs is None and not _mounts(node):
        return None
    mounts = _mounts(node)
    for s in stacked:
        mounts.extend(_mounts(s))
    used, avail = _int(node.get("fsused")), _int(node.get("fsavail"))
    size = (used + avail) if used is not None and avail is not None else _int(node.get("size"))
    uuid = norm_text(node.get("uuid"))
    return VolumeInfo(
        volume_id=f"uuid:{uuid}" if uuid else f"dev:{node.get('path') or node.get('name')}",
        filesystem=fs, label=norm_text(node.get("label")), size_bytes=size,
        free_bytes=avail if avail is not None else None,
        mount_paths=tuple(m for m in mounts if m != "[SWAP]"),
        page_file="[SWAP]" in mounts, health=None, os_device=norm_text(node.get("maj:min")),
        dev_path=norm_text(node.get("path")))


def parse_lsblk(data: dict[str, Any]) -> list[DiskInfo]:
    if not isinstance(data, dict) or not isinstance(data.get("blockdevices"), list):
        raise DriveQueryError("enum_bad_output", "lsblk returned unexpected data")
    out: list[DiskInfo] = []
    for d in data["blockdevices"]:
        if d.get("type") != "disk":
            continue  # loop, rom, zram: never archive media
        children = [c for c in d.get("children") or [] if c.get("type") == "part"]
        partitions: list[PartitionInfo] = []
        for i, c in enumerate(children, start=1):
            partitions.append(PartitionInfo(
                number=i, guid=norm_guid(c.get("partuuid")), type_id=norm_guid(c.get("parttype")),
                kind=_part_kind(c.get("parttype")), size_bytes=_int(c.get("size")) or 0,
                volume=_volume(c, _descendants(c)), os_device=norm_text(c.get("maj:min"))))
        if not children and (norm_fs(d.get("fstype")) or _mounts(d)):
            partitions.append(PartitionInfo(number=0, guid=None, type_id=None, kind=KIND_WHOLE_DISK,
                                            size_bytes=_int(d.get("size")) or 0,
                                            volume=_volume(d, _descendants(d)), os_device=norm_text(d.get("maj:min"))))
        pttype = (norm_text(d.get("pttype")) or "").lower()
        style = {"gpt": "GPT", "dos": "MBR"}.get(pttype, pttype.upper() or "RAW")
        tran = norm_text(d.get("tran"))
        devs = [norm_text(d.get("maj:min"))] + [norm_text(x.get("maj:min")) for x in _descendants(d)]
        out.append(DiskInfo(
            platform="linux", disk_id=str(d.get("kname") or d.get("name")),
            serial=norm_serial(d.get("serial")), model=norm_text(d.get("model")),
            size_bytes=_int(d.get("size")) or 0, bus_type=tran.upper() if tran else None,
            unique_id=norm_text(d.get("wwn")), disk_guid=norm_guid(d.get("ptuuid")), partition_style=style,
            is_offline=(norm_text(d.get("state")) or "running") not in ("running", "live"),
            is_read_only=_flag(d.get("ro")), health=None,
            removable=_flag(d.get("rm")) or _flag(d.get("hotplug")),
            device_path=norm_text(d.get("path")), partitions=tuple(partitions),
            os_devices=tuple(x for x in devs if x)))
    return out


def capture_lsblk(timeout_s: float = 30.0) -> dict[str, Any]:
    exe = shutil.which("lsblk")
    if exe is None:
        raise DriveQueryError("lsblk_missing", "lsblk (util-linux) was not found")
    try:
        proc = subprocess.run([exe, *LSBLK_ARGV[1:]], capture_output=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriveQueryError("lsblk_failed", f"lsblk failed: {exc}") from None
    if proc.returncode != 0:
        raise DriveQueryError("lsblk_failed", f"lsblk exited with {proc.returncode}",
                              {"stderr": proc.stderr.decode(errors="replace")[-1000:]})
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError:
        raise DriveQueryError("enum_bad_output", "lsblk did not return JSON") from None


def enumerate_linux() -> list[DiskInfo]:
    return parse_lsblk(capture_lsblk())


def linux_device_for_path(path: str | Path) -> str:
    """maj:min of the block device holding ``path``.

    Filesystems with anonymous devices (btrfs subvolumes, overlayfs: major 0) do not match lsblk by
    st_dev, so ``findmnt`` is asked for the mount source instead. If that also fails the caller
    must treat protection as unknown and refuse to prepare anything (fail closed).
    """
    p = Path(os.path.realpath(path))
    while not p.exists() and p.parent != p:
        p = p.parent
    st = os.stat(p)
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)  # type: ignore[attr-defined]
    if major != 0:
        return f"{major}:{minor}"
    exe = shutil.which("findmnt")
    if exe:
        proc = subprocess.run([exe, "--json", "--target", str(p), "--output", "SOURCE,MAJ:MIN"],
                              capture_output=True, timeout=10, check=False)
        if proc.returncode == 0:
            try:
                fs = json.loads(proc.stdout.decode("utf-8"))["filesystems"][0]
                src = str(fs.get("source") or "").split("[", 1)[0]
                if src.startswith("/dev/"):
                    st2 = os.stat(src)
                    return f"{os.major(st2.st_rdev)}:{os.minor(st2.st_rdev)}"  # type: ignore[attr-defined]
            except (KeyError, IndexError, OSError, json.JSONDecodeError):
                pass
    raise ProtectedStorageError("anonymous_device", f"cannot determine the disk holding {p}")
