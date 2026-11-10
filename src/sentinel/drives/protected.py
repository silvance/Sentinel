"""Which disks must never be offered for preparation, determined by IDENTITY.

A disk is protected when it holds any of: the operating system, the Python runtime / Sentinel
software, the recording spool, the ledger/data directory, or the logs. Each path is resolved to
the identity of its volume — the Windows volume GUID (``\\\\?\\Volume{...}\\``) or the Linux
block device number (maj:min) — never to a drive letter, mount point name or disk number.

Fail closed: if any protected path cannot be resolved, or resolves to a volume that is not found
on any enumerated disk (network share, Storage Spaces, SUBST, an unusual stacking), the result is
``complete = False`` and classification marks every drive UNSUITABLE ("drive checks unavailable").
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import DriveError
from .model import DiskInfo

ROLE_OS = "operating_system"
ROLE_SOFTWARE = "software"
ROLE_SPOOL = "recordings"
ROLE_DATA = "ledger"
ROLE_LOGS = "logs"


@dataclass(frozen=True)
class ProtectedItem:
    role: str
    path: str
    volume_id: str | None = None      # Windows
    os_device: str | None = None      # Linux maj:min
    error: str | None = None


@dataclass(frozen=True)
class ProtectedStorage:
    items: tuple[ProtectedItem, ...]

    @property
    def problems(self) -> list[str]:
        return [f"{i.role}: {i.error}" for i in self.items if i.error]

    def roles_on(self, disk: DiskInfo) -> list[str]:
        vids = {v.volume_id.lower() for v in disk.volumes}
        devs = set(disk.os_devices) | {p.os_device for p in disk.partitions if p.os_device}
        roles = []
        for i in self.items:
            if (i.volume_id and i.volume_id.lower() in vids) or (i.os_device and i.os_device in devs):
                roles.append(i.role)
        return sorted(set(roles))

    def unmatched(self, disks: Iterable[DiskInfo]) -> list[str]:
        """Protected items that resolved but were found on no disk (=> cannot exclude => fail closed)."""
        disks = list(disks)
        out = []
        for i in self.items:
            if i.error:
                continue
            probe = ProtectedStorage((i,))
            if not any(probe.roles_on(d) for d in disks):
                out.append(f"{i.role}: its volume was not found on any disk")
        return out

    def complete_for(self, disks: Iterable[DiskInfo]) -> tuple[bool, list[str]]:
        problems = self.problems + self.unmatched(disks)
        roles = {i.role for i in self.items}
        for required in (ROLE_OS, ROLE_SPOOL):
            if required not in roles:
                problems.append(f"{required}: location not provided")
        return (not problems, problems)


def default_paths(spool_dir: str | None, data_dir: str | None, log_dir: str | None) -> dict[str, list[str]]:
    paths: dict[str, list[str]] = {}
    if sys.platform == "win32":
        paths[ROLE_OS] = [os.environ.get("SystemRoot", r"C:\Windows")]
    else:
        paths[ROLE_OS] = [p for p in ("/", "/boot", "/boot/efi", "/usr", "/var") if Path(p).exists()]
    paths[ROLE_SOFTWARE] = [sys.executable, sys.prefix, str(Path(__file__).resolve().parent)]
    if spool_dir:
        paths[ROLE_SPOOL] = [spool_dir]
    if data_dir:
        paths[ROLE_DATA] = [data_dir]
    if log_dir:
        paths[ROLE_LOGS] = [log_dir]
    return paths


def resolve(paths: dict[str, list[str]], *, windows: bool | None = None,
            resolver: Callable[[str], str] | None = None) -> ProtectedStorage:
    """Resolve role -> paths to volume identities. ``resolver`` is injectable for tests."""
    win = (sys.platform == "win32") if windows is None else windows
    if resolver is None:
        if win:
            from .windows_enum import windows_volume_id_for_path as resolver
        else:
            from .linux_enum import linux_device_for_path as resolver
    items = []
    for role, plist in paths.items():
        for p in plist:
            try:
                ident = resolver(p)
            except (DriveError, OSError) as exc:
                items.append(ProtectedItem(role, p, error=str(exc)))
                continue
            items.append(ProtectedItem(role, p, volume_id=ident if win else None, os_device=None if win else ident))
    return ProtectedStorage(tuple(items))
