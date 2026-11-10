"""Generation of the destructive preparation scripts (pure functions; unit-tested).

Windows (verified against the development host with Get-Command / Get-Help, NOT EXECUTED on any
disk): ``Clear-Disk -RemoveData -RemoveOEM -Confirm:$false``, ``Initialize-Disk -PartitionStyle GPT
-Confirm:$false``, ``New-Partition -UseMaximumSize`` (New-Partition has no -Confirm parameter),
``Format-Volume -FileSystem NTFS|exFAT -NewFileSystemLabel <label> -Confirm:$false -Force``
(Format-Volume's FileSystem ValidateSet is FAT, FAT32, exFAT, NTFS, ReFS), then
``Add-PartitionAccessPath -AssignDriveLetter``. Initialize-Disk GPT also creates the Microsoft
Reserved partition, which classification ignores.

Design choice: the drive letter is assigned *after* formatting rather than with
``New-Partition -AssignDriveLetter``. A lettered but unformatted partition makes Explorer pop up
"You need to format the disk in drive X:" — a dialog a non-technical operator could act on. Sentinel
itself never relies on the letter (it uses the volume GUID path); failure to get a letter (all 26 in
use) is reported but is not an error.

The script re-verifies the disk's identity **inside the elevated process, immediately before
Clear-Disk** (serial, UniqueId, model, size, disk GUID, partition GUIDs) and refuses if the disk is
a system/boot disk, holds a page file, or holds any protected volume. After Clear-Disk it
re-checks the hardware identity again before each further step. Expected values are embedded as
base64 JSON, so no hardware-supplied string is ever parsed as PowerShell code.

Linux (NOT EXECUTED; tool presence/options checked only where noted): ``wipefs --all`` and
``sfdisk --wipe always`` (util-linux; options verified with --help on util-linux 2.41.4),
``mkfs.ntfs --fast --label`` (ntfs-3g), ``mkfs.exfat -L`` (exfatprogs), ``mkfs.ext4 -L``
(e2fsprogs) — the mkfs options were NOT verified on a real host (the tools are not installed in the
available Linux VM).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .errors import PrepareRefused
from .model import GPT_BASIC_DATA, DiskInfo, DriveIdentity
from .powershell import ps_b64_json

WINDOWS_FS = {"ntfs": "NTFS", "exfat": "exFAT"}
LINUX_MKFS = {
    "ntfs": ["mkfs.ntfs", "--fast", "--label"],
    "exfat": ["mkfs.exfat", "-L"],
    "ext4": ["mkfs.ext4", "-F", "-L"],
}
_LABEL_RE = re.compile(r"^[A-Z0-9_]{1,32}$")

# Exit codes of the elevated helper (both platforms).
EXIT_OK = 0
EXIT_FAILED = 2              # failure; check "step" to know whether anything was erased
EXIT_IDENTITY_MISMATCH = 3   # the disk is not the confirmed one
EXIT_PROTECTED = 4           # the disk is (now) a protected disk
EXIT_ELEVATION_CANCELLED = 1223  # ERROR_CANCELLED: the person declined the UAC prompt
NON_DESTRUCTIVE_STEPS = ("start", "verify")


@dataclass(frozen=True)
class PreparePlan:
    platform: str
    disk_id: str
    identity: DriveIdentity
    filesystem: str
    label: str
    drive_uuid: str
    script: str
    result_path: str | None = None

    def summary(self) -> dict[str, str]:
        return {"platform": self.platform, "disk_id": self.disk_id, "filesystem": self.filesystem,
                "label": self.label, "drive_uuid": self.drive_uuid, "fingerprint": self.identity.fingerprint()}


def _assert_plannable(disk: DiskInfo, label: str, protected_ids: tuple[str, ...]) -> None:
    """Independent last-line guard (classification already refused these)."""
    if disk.is_system or disk.is_boot or any(p.is_system or p.is_boot for p in disk.partitions):
        raise PrepareRefused("system_disk", "refusing to plan preparation of a system or boot disk")
    if any(v.page_file for v in disk.volumes):
        raise PrepareRefused("page_file", "refusing to plan preparation of a disk holding a page file")
    vids = {v.volume_id.lower() for v in disk.volumes}
    devs = set(disk.os_devices)
    if any(p.lower() in vids or p in devs for p in protected_ids):
        raise PrepareRefused("recording_storage", "refusing to plan preparation of a protected disk")
    if not _LABEL_RE.fullmatch(label):
        raise PrepareRefused("bad_label", f"invalid volume label {label!r}")
    if not disk.identity.has_hardware_identity:
        raise PrepareRefused("no_identity", "disk has no serial number or unique id")


WINDOWS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$exp = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__EXPECTED__')) | ConvertFrom-Json
$step = 'start'
function Out-Result([hashtable]$r) {
    $r['step'] = $step
    ($r | ConvertTo-Json -Depth 4 -Compress) | Set-Content -LiteralPath $exp.result_path -Encoding UTF8
}
function N([object]$v) { if ($null -eq $v) { return '' }; return ([string]$v).Trim() }
function G([object]$v) { return (N $v).Trim('{', '}').ToLowerInvariant() }
function Assert-Disk($d, [bool]$contents) {
    $model = N $d.Model
    if (-not $model) { $model = N $d.FriendlyName }
    $serial = ((N $d.SerialNumber) -replace '\s', '').TrimEnd('.')
    if ($serial -ne $exp.serial) { throw 'IDENTITY_MISMATCH serial' }
    if ((N $d.UniqueId) -ne $exp.unique_id) { throw 'IDENTITY_MISMATCH unique_id' }
    if ($model -ne $exp.model) { throw 'IDENTITY_MISMATCH model' }
    if ([string]([uint64]$d.Size) -ne $exp.size) { throw 'IDENTITY_MISMATCH size' }
    if ($d.IsSystem -or $d.IsBoot) { throw 'PROTECTED system or boot disk' }
    $parts = @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue)
    foreach ($p in $parts) {
        if ($p.IsSystem -or $p.IsBoot) { throw 'PROTECTED system or boot partition' }
        foreach ($ap in @($p.AccessPaths)) {
            if ($ap -and ($exp.protected_volumes -contains ([string]$ap).ToLowerInvariant())) { throw 'PROTECTED volume' }
        }
    }
    foreach ($w in @(Get-CimInstance -ClassName Win32_Volume | Where-Object { $_.PageFilePresent })) {
        foreach ($p in $parts) {
            if (@($p.AccessPaths) -contains $w.DeviceID) { throw 'PROTECTED page file' }
        }
    }
    if ($contents) {
        if ((G $d.Guid) -ne $exp.disk_guid) { throw 'IDENTITY_MISMATCH disk_guid' }
        $pg = (@($parts | ForEach-Object { G $_.Guid } | Where-Object { $_ }) | Sort-Object) -join ','
        if ($pg -ne $exp.partition_guids) { throw 'IDENTITY_MISMATCH partition_guids' }
    }
}
try {
    $step = 'verify'
    $d = Get-Disk -Number $exp.disk_number
    Assert-Disk $d $true
    $step = 'clear'
    if ((N $d.PartitionStyle) -ne 'RAW') {
        Clear-Disk -InputObject $d -RemoveData -RemoveOEM -Confirm:$false
    }
    $step = 'initialize'
    $d = Get-Disk -Number $exp.disk_number
    Assert-Disk $d $false
    Initialize-Disk -InputObject $d -PartitionStyle GPT -Confirm:$false
    $step = 'partition'
    $d = Get-Disk -Number $exp.disk_number
    Assert-Disk $d $false
    $p = New-Partition -InputObject $d -UseMaximumSize
    $step = 'format'
    $v = Format-Volume -Partition $p -FileSystem $exp.filesystem -NewFileSystemLabel $exp.label -Confirm:$false -Force
    $step = 'letter'
    $letter = 'assigned'
    try { Add-PartitionAccessPath -InputObject $p -AssignDriveLetter } catch { $letter = N $_.Exception.Message }
    $step = 'done'
    $p = Get-Partition -DiskNumber $exp.disk_number -PartitionNumber $p.PartitionNumber
    Out-Result @{ ok = $true; partition_guid = (G $p.Guid); access_paths = @($p.AccessPaths);
                  filesystem = (N $v.FileSystem); label = (N $v.FileSystemLabel); drive_letter = $letter }
    exit 0
} catch {
    $msg = N $_.Exception.Message
    $code = 2
    if ($msg -like 'IDENTITY_MISMATCH*') { $code = 3 } elseif ($msg -like 'PROTECTED*') { $code = 4 }
    try { Out-Result @{ ok = $false; error = $msg } } catch { }
    exit $code
}
"""


def build_windows_script(disk: DiskInfo, *, filesystem: str, label: str, protected_volume_ids: tuple[str, ...],
                         result_path: str) -> str:
    if disk.platform != "windows":
        raise PrepareRefused("wrong_platform", "not a Windows disk")
    if filesystem not in WINDOWS_FS:
        raise PrepareRefused("filesystem_unsupported", f"{filesystem} cannot be created on Windows")
    _assert_plannable(disk, label, protected_volume_ids)
    ident = disk.identity
    expected = {
        "disk_number": int(disk.disk_id),
        "serial": ident.serial or "", "unique_id": ident.unique_id or "", "model": ident.model or "",
        "size": str(ident.size_bytes), "disk_guid": ident.disk_guid or "",
        "partition_guids": ",".join(ident.partition_guids),
        "protected_volumes": sorted(v.lower() for v in protected_volume_ids),
        "filesystem": WINDOWS_FS[filesystem], "label": label, "result_path": result_path,
    }
    return WINDOWS_TEMPLATE.replace("__EXPECTED__", ps_b64_json(expected))


def build_windows_plan(disk: DiskInfo, *, filesystem: str, label: str, drive_uuid: str,
                       protected_volume_ids: tuple[str, ...], result_path: str) -> PreparePlan:
    script = build_windows_script(disk, filesystem=filesystem, label=label,
                                  protected_volume_ids=protected_volume_ids, result_path=result_path)
    return PreparePlan("windows", disk.disk_id, disk.identity, filesystem, label, drive_uuid, script, result_path)


def build_linux_script(disk: DiskInfo, *, filesystem: str, label: str, protected_devices: tuple[str, ...]) -> str:
    """POSIX sh script for ``pkexec /bin/sh -c``. NOT EXECUTED. Prints one RESULT line."""
    if disk.platform != "linux":
        raise PrepareRefused("wrong_platform", "not a Linux disk")
    if filesystem not in LINUX_MKFS:
        raise PrepareRefused("filesystem_unsupported", f"{filesystem} is not supported")
    if not disk.device_path or not re.fullmatch(r"/dev/[A-Za-z0-9]+", disk.device_path):
        raise PrepareRefused("bad_device", "unexpected device path")
    _assert_plannable(disk, label, protected_devices)
    ident = disk.identity
    q = shlex.quote
    parttype = "L" if filesystem == "ext4" else GPT_BASIC_DATA.upper()
    mkfs = " ".join(q(x) for x in LINUX_MKFS[filesystem])
    prot = " ".join(q(p) for p in sorted(protected_devices))
    return f"""set -eu
DEV={q(disk.device_path)}
step=verify
fail() {{ echo "RESULT ok=0 step=$step code=$1 error=$2"; exit "$1"; }}
check() {{
  [ "$(lsblk -dno SERIAL "$DEV" | tr -d '[:space:]')" = {q(ident.serial or '')} ] || fail 3 IDENTITY_MISMATCH_serial
  [ "$(lsblk -dno WWN "$DEV" | tr -d '[:space:]')" = {q(ident.unique_id or '')} ] || fail 3 IDENTITY_MISMATCH_wwn
  [ "$(blockdev --getsize64 "$DEV")" = {q(str(ident.size_bytes))} ] || fail 3 IDENTITY_MISMATCH_size
  if [ -n "$(lsblk -no MOUNTPOINTS "$DEV" | tr -d '[:space:]')" ]; then fail 4 PROTECTED_mounted; fi
  for p in {prot or "''"}; do
    if [ -n "$p" ] && lsblk -no MAJ:MIN "$DEV" | tr -d ' ' | grep -qx "$p"; then fail 4 PROTECTED_device; fi
  done
}}
check
[ "$(lsblk -dno PTUUID "$DEV" | tr -d '[:space:]')" = {q(ident.disk_guid or '')} ] || fail 3 IDENTITY_MISMATCH_ptuuid
step=clear
wipefs --all "$DEV" >/dev/null || fail 2 wipefs_failed
step=partition
check
printf 'label: gpt\\n,,%s\\n' {q(parttype)} | sfdisk --quiet --lock --wipe always --wipe-partitions always "$DEV" || fail 2 sfdisk_failed
udevadm settle
PART=$(lsblk -nlpo PATH,TYPE "$DEV" | awk '$2=="part" {{ print $1; exit }}')
[ -n "$PART" ] || fail 2 no_partition
step=format
{mkfs} {q(label)} "$PART" >/dev/null || fail 2 mkfs_failed
udevadm settle
step=done
echo "RESULT ok=1 step=done code=0 part=$PART"
"""
