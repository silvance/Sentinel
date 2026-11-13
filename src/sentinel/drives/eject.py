"""Safe removal ("Safe to Remove", U4) of a prepared drive.

Windows (design): (1) refuse system/protected disks; (2) flush every volume of the disk with
``Write-VolumeCache -Path \\\\?\\Volume{GUID}\\``; (3) find the disk's Plug-and-Play device node
(``Win32_DiskDrive.PNPDeviceID``) and walk up at most 3 levels to the first node flagged
DN_REMOVABLE, refusing if that node is also an ancestor of any *other* disk (e.g. a two-bay dock);
(4) ``CM_Request_Device_EjectW`` on it — Windows dismounts the filesystems, and vetoes if files
are open; (5) re-enumerate and confirm the disk is gone before reporting success.

Verified on the development host (non-elevated): step 2 succeeds without administrator rights
(Write-VolumeCache on the system volume, by drive letter and by volume GUID path); the cfgmgr32
bindings used in step 3 (CM_Locate_DevNodeW, CM_Get_DevNode_Status, CM_Get_Parent,
CM_Get_Device_IDW) work read-only against the internal disks. Step 4 is NOT EXECUTED (no removable
disk attached); it is expected not to need administrator rights for USB storage — the same request
Explorer's "Safely Remove Hardware" makes for standard users — but this is NOT VERIFIED.

Linux: ``udisksctl unmount`` for each mounted filesystem, then ``udisksctl power-off`` — NOT EXECUTED.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import SafeRemoveError
from .model import DiskInfo
from .protected import ProtectedStorage

CR_SUCCESS = 0
DN_REMOVABLE = 0x00004000
MAX_ANCESTOR_LEVELS = 3
PNP_VETO_NAMES = {0: "none", 1: "unknown", 2: "legacy device", 3: "pending close", 4: "windows app",
                  5: "windows service", 6: "outstanding open (files in use)", 7: "device", 8: "driver",
                  9: "illegal device request", 10: "insufficient power", 11: "non-disableable",
                  12: "legacy driver", 13: "insufficient rights", 14: "already removed"}


@dataclass
class SafeRemoveResult:
    ok: bool
    message: str                     # operator sentence
    needs_admin: bool | None         # None = unknown
    steps: list[dict[str, Any]] = field(default_factory=list)
    veto: str | None = None


class _CfgMgr:
    """Minimal cfgmgr32 binding (Windows only)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ct = ctypes
        lib = ctypes.WinDLL("cfgmgr32")
        self.lib = lib
        lib.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG]
        lib.CM_Get_Parent.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.ULONG]
        lib.CM_Get_Device_IDW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG]
        lib.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(wintypes.ULONG),
                                              wintypes.DWORD, wintypes.ULONG]
        lib.CM_Request_Device_EjectW.argtypes = [wintypes.DWORD, ctypes.POINTER(ctypes.c_int), wintypes.LPWSTR,
                                                 wintypes.ULONG, wintypes.ULONG]
        for f in (lib.CM_Locate_DevNodeW, lib.CM_Get_Parent, lib.CM_Get_Device_IDW, lib.CM_Get_DevNode_Status,
                  lib.CM_Request_Device_EjectW):
            f.restype = wintypes.DWORD
        self.w = wintypes

    def locate(self, instance_id: str) -> int:
        inst = self.w.DWORD(0)
        cr = self.lib.CM_Locate_DevNodeW(self.ct.byref(inst), instance_id, 0)
        if cr != CR_SUCCESS:
            raise SafeRemoveError("devnode_not_found", f"device node not found (CONFIGRET {cr})")
        return int(inst.value)

    def parent(self, devinst: int) -> int | None:
        p = self.w.DWORD(0)
        return int(p.value) if self.lib.CM_Get_Parent(self.ct.byref(p), devinst, 0) == CR_SUCCESS else None

    def device_id(self, devinst: int) -> str:
        buf = self.ct.create_unicode_buffer(512)
        cr = self.lib.CM_Get_Device_IDW(devinst, buf, len(buf), 0)
        return buf.value if cr == CR_SUCCESS else f"<CONFIGRET {cr}>"

    def status(self, devinst: int) -> int:
        st, prob = self.w.ULONG(0), self.w.ULONG(0)
        cr = self.lib.CM_Get_DevNode_Status(self.ct.byref(st), self.ct.byref(prob), devinst, 0)
        return int(st.value) if cr == CR_SUCCESS else 0

    def ancestors(self, devinst: int, levels: int = 16) -> list[int]:
        out, cur = [], devinst
        for _ in range(levels):
            p = self.parent(cur)
            if p is None:
                break
            out.append(p)
            cur = p
        return out

    def eject(self, devinst: int) -> tuple[int, int, str]:  # NOT EXECUTED in this environment
        veto = self.ct.c_int(0)
        name = self.ct.create_unicode_buffer(260)
        cr = self.lib.CM_Request_Device_EjectW(devinst, self.ct.byref(veto), name, len(name), 0)
        return int(cr), int(veto.value), name.value


def describe_device_chain(pnp_device_id: str) -> list[dict[str, Any]]:
    """Read-only: the disk's device node and its ancestors with their removable flag."""
    cm = _CfgMgr()
    node = cm.locate(pnp_device_id)
    chain = [node, *cm.ancestors(node, MAX_ANCESTOR_LEVELS)]
    return [{"device_id": cm.device_id(n), "removable": bool(cm.status(n) & DN_REMOVABLE)} for n in chain]


def _flush_windows(disk: DiskInfo, steps: list[dict[str, Any]]) -> None:
    from .powershell import ps_b64_json, run_powershell

    paths = sorted({p for v in disk.volumes for p in v.mount_paths if p.startswith("\\\\?\\Volume{")})
    if not paths:
        steps.append({"step": "flush", "ok": True, "detail": "no mounted volumes"})
        return
    script = ("$ErrorActionPreference = 'Stop'\n"
              f"$paths = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{ps_b64_json(paths)}'))"
              " | ConvertFrom-Json\n"
              "foreach ($p in $paths) { Write-VolumeCache -Path $p }\n")
    run_powershell(script, timeout_s=300)
    steps.append({"step": "flush", "ok": True, "volumes": len(paths)})


def _is_this_disk(disk: DiskInfo, other: DiskInfo) -> bool:
    """Is ``other`` (from a new listing) still the disk that was ejected? Review 2026-11-12: compared by its
    Plug-and-Play instance ID, which no two connected devices share, not by the hardware key: behind bridges
    that report placeholder serials a second disk of the same model and size has the same key, and the removal
    was reported as failed ("Do not unplug it") although it had worked. A disk listed without an instance ID
    counts as this one if its whole identity (contents included) is the same."""
    pnp = (disk.pnp_device_id or "").lower()
    if other.pnp_device_id:
        return other.pnp_device_id.lower() == pnp
    return other.identity.fingerprint() == disk.identity.fingerprint()


def safe_remove_windows(disk: DiskInfo, all_disks: list[DiskInfo], protected: ProtectedStorage, *,
                        enumerate_fn: Callable[[], list[DiskInfo]], wait_s: float = 15.0) -> SafeRemoveResult:
    steps: list[dict[str, Any]] = []
    if disk.is_system or disk.is_boot or protected.roles_on(disk) or any(v.page_file for v in disk.volumes):
        raise SafeRemoveError("protected", "This drive is used by the computer itself and cannot be removed.")
    if not disk.pnp_device_id:
        raise SafeRemoveError("no_pnp_id", "The drive's device could not be identified.")
    _flush_windows(disk, steps)
    cm = _CfgMgr()
    node = cm.locate(disk.pnp_device_id)
    chain = [node, *cm.ancestors(node, MAX_ANCESTOR_LEVELS)]
    target = next((n for n in chain if cm.status(n) & DN_REMOVABLE), None)
    if target is None:
        raise SafeRemoveError("not_removable", "This drive cannot be removed safely while the computer is on.",
                              {"chain": [cm.device_id(n) for n in chain]})
    for other in all_disks:
        if other.disk_id == disk.disk_id or not other.pnp_device_id:
            continue
        try:
            onode = cm.locate(other.pnp_device_id)
        except SafeRemoveError:
            continue
        if target == onode or target in cm.ancestors(onode):
            raise SafeRemoveError("shared_device", "Another drive is connected through the same device. "
                                  "Remove it from the dock first, or contact technical support.",
                                  {"target": cm.device_id(target), "other_disk": other.disk_id})
    steps.append({"step": "locate", "ok": True, "target": cm.device_id(target)})
    cr, veto, veto_name = cm.eject(target)
    steps.append({"step": "eject", "ok": cr == CR_SUCCESS and veto == 0, "configret": cr, "veto_type": veto,
                  "veto_name": veto_name})
    if cr != CR_SUCCESS or veto != 0:
        vt = PNP_VETO_NAMES.get(veto, str(veto))
        return SafeRemoveResult(False, "The drive is still in use and cannot be removed yet. Close any open files "
                                "on it and try again.", needs_admin=(veto == 13) or None, steps=steps, veto=vt)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not any(_is_this_disk(disk, d) for d in enumerate_fn()):
            steps.append({"step": "verify_gone", "ok": True})
            return SafeRemoveResult(True, "SAFE TO REMOVE DRIVE", needs_admin=False, steps=steps)
        time.sleep(1.0)
    steps.append({"step": "verify_gone", "ok": False})
    return SafeRemoveResult(False, "The drive did not disconnect. Do not unplug it. Contact technical support.",
                            needs_admin=None, steps=steps)


def safe_remove_linux(disk: DiskInfo, protected: ProtectedStorage) -> SafeRemoveResult:  # NOT EXECUTED
    if protected.roles_on(disk) or any(v.page_file for v in disk.volumes):
        raise SafeRemoveError("protected", "This drive is used by the computer itself and cannot be removed.")
    steps: list[dict[str, Any]] = []
    subprocess.run(["sync"], check=False, timeout=300)
    for v in disk.volumes:
        if v.mount_paths and v.dev_path:
            p = subprocess.run(["udisksctl", "unmount", "--block-device", v.dev_path, "--no-user-interaction"],
                               capture_output=True, timeout=120, check=False)
            steps.append({"step": "unmount", "device": v.dev_path, "ok": p.returncode == 0})
            if p.returncode != 0:
                return SafeRemoveResult(False, "The drive is still in use and cannot be removed yet. Close any open "
                                        "files on it and try again.", needs_admin=None, steps=steps)
    p = subprocess.run(["udisksctl", "power-off", "--block-device", str(disk.device_path), "--no-user-interaction"],
                       capture_output=True, timeout=120, check=False)
    steps.append({"step": "power_off", "ok": p.returncode == 0})
    if p.returncode != 0:
        return SafeRemoveResult(False, "The drive could not be switched off. Do not unplug it. "
                                "Contact technical support.", needs_admin=None, steps=steps)
    return SafeRemoveResult(True, "SAFE TO REMOVE DRIVE", needs_admin=False, steps=steps)


ARCHIVE_SWAP_READY = "archive_swap_ready"   # the archive copier's own "taken out for the office" event


def archive_marker(disk: DiskInfo, inspector: Callable[[Any], Any] | None = None) -> dict[str, Any] | None:
    """The disk's SENTINEL_DRIVE.json if it marks an archive drive (``"role": "archive"`` with a drive_uuid)."""
    from .marker import inspect_volume

    look = inspector or inspect_volume
    for v in disk.volumes:
        try:
            rc = look(v)
        except OSError:
            continue
        m = rc.marker if rc.readable else None
        if isinstance(m, dict) and m.get("role") == "archive" and m.get("drive_uuid"):
            return m
    return None


def record_archive_removal(ledger: Any, marker: dict[str, Any]) -> None:
    """Record an archive drive taken out with Safely remove exactly as the archive copier records one taken out
    with Swap (review 2026-11-12): event ``archive_swap_ready`` (category ``archive``) with the copier's drive
    key ``drive_uuid:<uuid>`` and the highest copy id on the drive. Return to service compares the copies on the
    drive with that id (sentinel.archive.ledger.ArchiveLedger.last_swap_since_return). Only after the drive
    was ejected while the copier was not running. Retries while the ledger is locked."""
    from ..export.ledger import retry_locked

    drive_id = f"drive_uuid:{marker['drive_uuid']}"

    @retry_locked
    def write() -> None:
        rows = ledger.query("SELECT MAX(copy_id) FROM archive_copies WHERE drive_id=?", (drive_id,))
        last = int(rows[0][0] or 0) if rows else 0
        ledger.add_event("audit", "archive", ARCHIVE_SWAP_READY, "Archive drive taken out with Safely remove",
                         {"drive_id": drive_id, "ejected": True, "detail": "drives eject (Safely remove)",
                          "last_copy_id": last, "label": marker.get("label"), "via": "drives_eject"})

    write()


def safe_remove(disk: DiskInfo, all_disks: list[DiskInfo], protected: ProtectedStorage, *,
                enumerate_fn: Callable[[], list[DiskInfo]]) -> SafeRemoveResult:
    if sys.platform == "win32":
        return safe_remove_windows(disk, all_disks, protected, enumerate_fn=enumerate_fn)
    return safe_remove_linux(disk, protected)
