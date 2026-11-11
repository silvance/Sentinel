""""Erase and Set Up Drive" (docs/04 "Prepare This Drive"; renamed for operators, review finding 1.4):
guarded, confirmed, audited preparation of an archive/review drive (S2, S3).

Never automatic. The only entry point that changes a disk is :func:`prepare_drive`, and it needs:

1. a :class:`Confirmation` produced from what was shown to the person — the disk's identity
   *fingerprint* and its classification at that moment (no confirmation => refused);
2. for FOREIGN_DATA, depending on ``DrivePolicy.foreign_data_authorization`` (OPEN OWNER
   DECISION, default ``admin_only``): administrator authorisation and the last 4 characters of the
   serial typed by the person (``operator_with_serial``: serial only; ``never``: always refused);
3. passing every guard twice — when called, and again immediately before the elevated helper is
   started — and then a third time inside the elevated helper right before Clear-Disk
   (TOCTOU: the drive may be swapped or renumbered between confirmation and execution).

Elevation on Windows (design choice): the formatting script runs in a separate elevated Windows
PowerShell started through UAC (``Start-Process -Verb RunAs``), passed as ``-EncodedCommand`` (no
script file to tamper with). The recorder never runs elevated. If the operator account is a
standard user, the UAC prompt itself requires an administrator's credentials. Linux: ``pkexec``
(polkit) — NOT EXECUTED.

After the helper reports success, Sentinel (unelevated) re-enumerates, finds the same physical disk
by hardware identity (not disk number), checks layout/filesystem/label, writes and re-reads a test
file bypassing the cache, writes ``SENTINEL_DRIVE.json``, re-classifies (must be READY) and
records an ``audit`` event in the ledger. Audit events are also written when preparation is
authorised and when it fails.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .. import __version__
from ..clock import iso_utc
from .classify import ERASE_DISABLED_MESSAGE, Classification, Inspector, classify_all
from .commands import (
    EXIT_ELEVATION_CANCELLED,
    EXIT_IDENTITY_MISMATCH,
    EXIT_IN_USE,
    EXIT_OK,
    EXIT_PROTECTED,
    NON_DESTRUCTIVE_STEPS,
    PreparePlan,
    build_linux_script,
    build_windows_plan,
)
from .errors import IdentityChanged, PrepareFailed, PrepareRefused
from .known import load_known
from .marker import MARKER_FORMAT, MARKER_VERSION, inspect_volume, write_marker, write_read_back_test
from .model import KIND_MSR, DiskInfo, DriveClass, VolumeInfo, serial_tail
from .policy import DrivePolicy
from .protected import ProtectedStorage

AUDIT_CATEGORY = "drives"


class AuditSink(Protocol):
    def add_event(self, severity: str, category: str, code: str, message: str,
                  detail: dict[str, Any] | None = None) -> None: ...

    def query(self, sql: str, params: Any = ()) -> list[Any]: ...


@dataclass(frozen=True)
class Authorization:
    who: str                    # person who pressed the button (recorded in audit + marker)
    administrator: bool         # established by the caller; see os_administrator_check()
    method: str                 # how administrator status was established (audit trail)


@dataclass(frozen=True)
class Confirmation:
    """What the person saw and agreed to. Bound to the identity fingerprint, not a disk number."""

    disk_id: str
    fingerprint: str
    drive_class: DriveClass
    typed_serial_tail: str | None = None

    @classmethod
    def for_classification(cls, c: Classification, typed_serial_tail: str | None = None) -> Confirmation:
        return cls(c.disk.disk_id, c.disk.identity.fingerprint(), c.drive_class, typed_serial_tail)


@dataclass(frozen=True)
class HelperResult:
    exit_code: int
    result: dict[str, Any] | None
    stderr: str = ""


class ElevatedRunner(Protocol):
    def run(self, plan: PreparePlan) -> HelperResult: ...


@dataclass
class PrepareContext:
    policy: DrivePolicy
    protected: ProtectedStorage
    enumerate: Callable[[], list[DiskInfo]]
    runner: ElevatedRunner
    audit: AuditSink
    platform: str = "windows" if sys.platform == "win32" else "linux"
    inspector: Inspector = inspect_volume
    root_for_volume: Callable[[VolumeInfo], str] | None = None   # where to write the marker
    mount_volume: Callable[[VolumeInfo], None] | None = None     # Linux: udisksctl mount (NOT EXECUTED)
    settle_s: float = 3.0                                        # wait for the OS to mount the new volume
    work_dir: Path | None = None                                 # helper result files


@dataclass(frozen=True)
class PrepareResult:
    disk_id: str
    label: str
    drive_uuid: str
    filesystem: str
    marker: dict[str, Any]
    write_test: dict[str, Any]
    previous_class: str
    identity: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------- guards

def requirements(c: Classification) -> dict[str, Any]:
    """What the confirmation screen must collect for this drive."""
    return {"can_prepare": c.can_prepare, "needs_admin": c.needs_admin, "needs_serial": c.needs_serial,
            "refusal": c.refusal, "erases": c.disk.describe()}


def check_guards(c: Classification, confirmation: Confirmation | None, auth: Authorization | None,
                 policy: DrivePolicy) -> None:
    """Raise PrepareRefused / IdentityChanged unless this exact drive may be prepared now."""
    if not policy.erase_enabled:  # owner decision 2026-11-10; checked first, independent of classification
        raise PrepareRefused("erase_disabled", ERASE_DISABLED_MESSAGE, code="M-010")
    if confirmation is None:
        raise PrepareRefused("not_confirmed", "Erasing was not confirmed. Nothing was erased.")
    if auth is None or not auth.who.strip():
        raise PrepareRefused("no_operator", "The person erasing the drive was not identified. Nothing was erased.")
    if c.drive_class is DriveClass.SENTINEL_EVIDENCE:
        raise PrepareRefused("sentinel_evidence", c.message, {"reasons": list(c.reasons)}, code="M-009")
    if c.drive_class is DriveClass.UNSUITABLE:
        raise PrepareRefused(c.refusal or "unsuitable", c.message, {"reasons": list(c.reasons)},
                             code="M-008" if c.alert_code == "M-008" else "M-004")
    if c.drive_class is DriveClass.READY:
        raise PrepareRefused("already_prepared", "This drive is already set up as an archive drive. Nothing was erased.")
    if confirmation.disk_id != c.disk.disk_id or confirmation.fingerprint != c.disk.identity.fingerprint():
        raise IdentityChanged("identity_changed", "The drive changed after it was confirmed. Nothing was erased.")
    if confirmation.drive_class is not c.drive_class:
        raise IdentityChanged("class_changed", "The drive's contents changed after it was confirmed. "
                              "Nothing was erased.", {"confirmed": str(confirmation.drive_class),
                                                       "now": str(c.drive_class)})
    if not c.can_prepare:
        raise PrepareRefused(c.refusal or "not_allowed", "Erasing drives that contain other files is not "
                             "allowed on this system. Nothing was erased.")
    if c.needs_admin and not auth.administrator:
        raise PrepareRefused("admin_required", "An administrator must approve erasing this drive. Nothing was erased.")
    if c.needs_serial:
        want = serial_tail(c.disk.serial, policy.serial_confirm_chars)
        typed = (confirmation.typed_serial_tail or "").strip().upper()
        if want is None or typed != want:
            raise PrepareRefused("serial_mismatch", "The serial number characters typed do not match this drive. "
                                 "Nothing was erased.")
    gaps = c.disk.identity_gaps()
    if gaps:
        raise PrepareRefused("identity_incomplete", "This drive cannot be identified reliably enough to be erased. "
                             "Nothing was erased.", {"missing": gaps})


def _find(disks: list[DiskInfo], disk_id: str) -> DiskInfo | None:
    return next((d for d in disks if d.disk_id == disk_id), None)


def current_classification(ctx: PrepareContext, disk_id: str) -> tuple[Classification, list[DiskInfo]]:
    disks = ctx.enumerate()
    known = load_known(ctx.audit)  # re-read every time: evidence drives are never erased (finding 1.1)
    classes, _problems = classify_all(disks, ctx.policy, ctx.protected, inspector=ctx.inspector, known=known)
    c = next((x for x in classes if x.disk.disk_id == disk_id), None)
    if c is None:
        raise IdentityChanged("disk_gone", "The drive is no longer connected. Nothing was erased.")
    return c, disks


# ---------------------------------------------------------------------------- labels

_LABEL_NUM = re.compile(r"^(?P<p>[A-Z][A-Z0-9_]*?)(?P<n>\d+)$")


def next_label(policy: DrivePolicy, audit: AuditSink, disks: list[DiskInfo]) -> str:
    """SENTINEL_<n>: one more than any number used before (ledger) or currently attached."""
    used: list[int] = []
    for row in audit.query("SELECT detail FROM events WHERE category=? AND code='drive_prepared'", (AUDIT_CATEGORY,)):
        try:
            label = json.loads(row[0] or "{}").get("label", "")
        except ValueError:
            continue
        used.append(_label_number(policy, label))
    for d in disks:
        for v in d.volumes:
            used.append(_label_number(policy, v.label or ""))
    return policy.label_for(max([0, *used]) + 1)


def _label_number(policy: DrivePolicy, label: str) -> int:
    if label.upper().startswith(policy.label_prefix) and label[len(policy.label_prefix):].isdigit():
        return int(label[len(policy.label_prefix):])
    return 0


# ---------------------------------------------------------------------------- orchestration

def _audit(ctx: PrepareContext, code: str, message: str, detail: dict[str, Any], severity: str = "audit") -> None:
    ctx.audit.add_event(severity, AUDIT_CATEGORY, code, message, detail)


def _protected_ids(ctx: PrepareContext) -> tuple[str, ...]:
    if ctx.platform == "windows":
        return tuple(sorted(i.volume_id for i in ctx.protected.items if i.volume_id))
    return tuple(sorted(i.os_device for i in ctx.protected.items if i.os_device))


def build_plan(ctx: PrepareContext, disk: DiskInfo, label: str, drive_uuid: str) -> PreparePlan:
    if ctx.platform == "windows":
        work = ctx.work_dir or Path(tempfile.mkdtemp(prefix="sentinel-prepare-"))
        work.mkdir(parents=True, exist_ok=True)
        result_path = str(work / f"prepare-{drive_uuid}.json")
        return build_windows_plan(disk, filesystem=ctx.policy.approved_filesystem, label=label, drive_uuid=drive_uuid,
                                  protected_volume_ids=_protected_ids(ctx), result_path=result_path)
    script = build_linux_script(disk, filesystem=ctx.policy.approved_filesystem, label=label,
                                protected_devices=_protected_ids(ctx))
    return PreparePlan("linux", disk.disk_id, disk.identity, ctx.policy.approved_filesystem, label, drive_uuid, script)


def prepare_drive(ctx: PrepareContext, disk_id: str, confirmation: Confirmation | None,
                  auth: Authorization | None) -> PrepareResult:
    c, disks = current_classification(ctx, disk_id)
    check_guards(c, confirmation, auth, ctx.policy)
    assert auth is not None and confirmation is not None  # narrowed by check_guards
    label = next_label(ctx.policy, ctx.audit, disks)
    drive_uuid = str(uuid.uuid4())
    plan = build_plan(ctx, c.disk, label, drive_uuid)
    base = {"disk": c.disk.identity.to_dict(), "description": c.disk.describe(), "class": str(c.drive_class),
            "reasons": list(c.reasons), "who": auth.who, "administrator": auth.administrator,
            "auth_method": auth.method, "os_user": _os_user(), "host": socket.gethostname(),
            "label": label, "drive_uuid": drive_uuid, "filesystem": ctx.policy.approved_filesystem,
            "foreign_policy": ctx.policy.foreign_data_authorization, "used_bytes": c.used_bytes}
    _audit(ctx, "drive_prepare_authorized", f"Drive preparation authorised by {auth.who}: {c.disk.describe()}", base)

    # TOCTOU: re-check everything immediately before the destructive step.
    try:
        c2, _ = current_classification(ctx, disk_id)
        check_guards(c2, confirmation, auth, ctx.policy)
        if c2.disk.identity.fingerprint() != plan.identity.fingerprint():
            raise IdentityChanged("identity_changed", "The drive changed after it was confirmed. Nothing was erased.")
    except PrepareRefused as exc:
        _audit(ctx, "drive_prepare_refused", f"Drive preparation refused before erasing: {exc.reason}",
               {**base, "reason": exc.reason})
        raise

    started = time.monotonic()
    hr = ctx.runner.run(plan)
    res = hr.result or {}
    step = str(res.get("step") or "start")
    if hr.exit_code != EXIT_OK or not res.get("ok"):
        still_running = bool(res.get("still_running"))
        # A helper that may still be running can go on to erase even if it was last seen verifying.
        erased = still_running or step not in NON_DESTRUCTIVE_STEPS
        detail = {**base, "exit_code": hr.exit_code, "step": step, "error": res.get("error"),
                  "stderr": hr.stderr[-1000:], "erase_started": erased,
                  "helper_pid": res.get("helper_pid", res.get("pid")), "still_running": still_running,
                  "terminate_error": res.get("terminate_error")}
        _audit(ctx, "drive_prepare_failed", f"Drive setup failed at step {step}", detail, severity="error")
        if still_running:
            raise PrepareFailed("helper_still_running", "Drive setup is taking too long and may still be running. "
                                "Do not unplug the drive. Contact technical support.", detail)
        if hr.exit_code == EXIT_ELEVATION_CANCELLED:
            raise PrepareRefused("elevation_cancelled", "Administrator approval was not given. Nothing was erased.")
        if not erased and hr.exit_code == EXIT_IDENTITY_MISMATCH:
            raise IdentityChanged("identity_changed", "The drive changed after it was confirmed. Nothing was erased.",
                                  detail)
        if not erased and hr.exit_code == EXIT_PROTECTED:
            raise PrepareRefused("protected", "This drive is used by the computer itself. Nothing was erased.", detail)
        if not erased and hr.exit_code == EXIT_IN_USE:   # review 2026-11-10 (Linux: still mounted)
            raise PrepareRefused("in_use", "This drive is still in use (a program has files open on it, or it "
                                 "could not be unmounted). Close those programs and try again. Nothing was erased.",
                                 detail)
        if not erased:
            raise PrepareFailed("helper_failed", "Drive setup could not start. Nothing was erased.", detail)
        raise PrepareFailed("prepare_incomplete", "Drive setup did not finish. The drive may be partly erased "
                            "and must not be used until it is set up again.", detail)
    return _finish(ctx, plan, c, auth, base, res, time.monotonic() - started)


def _finish(ctx: PrepareContext, plan: PreparePlan, before: Classification, auth: Authorization,
            base: dict[str, Any], res: dict[str, Any], helper_s: float) -> PrepareResult:
    def fail(reason: str, msg: str, extra: dict[str, Any] | None = None) -> PrepareFailed:
        d = {**base, "reason": reason, **(extra or {})}
        _audit(ctx, "drive_prepare_failed", f"Drive preparation could not be verified: {reason}", d, severity="error")
        return PrepareFailed(reason, msg, d)

    disk = None
    deadline = time.monotonic() + max(ctx.settle_s, 0.0)
    while True:
        same = [d for d in ctx.enumerate() if d.identity.same_device(plan.identity)]
        if len(same) > 1:
            raise fail("ambiguous_identity", "Two connected drives look identical. Remove one and try again.")
        disk = same[0] if same else None
        vols = [p.volume for p in disk.partitions if p.kind != KIND_MSR and p.volume] if disk else []
        if disk and len(vols) == 1 and (vols[0].mount_paths or ctx.mount_volume):
            break
        if time.monotonic() >= deadline:
            raise fail("not_found_after_prepare", "The drive could not be found again after setup. "
                       "Do not use this drive. Contact technical support.")
        time.sleep(0.5)
    data_parts = [p for p in disk.partitions if p.kind != KIND_MSR]
    vol = data_parts[0].volume if len(data_parts) == 1 else None
    if vol is None or vol.filesystem != plan.filesystem or (vol.label or "").upper() != plan.label:
        raise fail("layout_mismatch", "The drive was not set up as expected. Do not use this drive. "
                   "Contact technical support.",
                   {"partitions": len(data_parts), "filesystem": vol.filesystem if vol else None,
                    "label": vol.label if vol else None})
    if ctx.protected.roles_on(disk):
        raise fail("protected_after_prepare", "Unexpected drive state. Contact technical support.")
    if ctx.mount_volume is not None:  # Linux: mount, then look the volume up again for its mount point
        ctx.mount_volume(vol)
        again = [d for d in ctx.enumerate() if d.identity.same_device(plan.identity)]
        vol = next((v for d in again for v in d.volumes if v.volume_id == vol.volume_id), vol)
    root = ctx.root_for_volume(vol) if ctx.root_for_volume else _default_root(vol)
    try:
        test = write_read_back_test(root)
        marker = {
            "format": MARKER_FORMAT, "version": MARKER_VERSION, "drive_uuid": plan.drive_uuid,
            "label": plan.label, "prepared_utc": iso_utc(time.time()), "prepared_by": auth.who,
            "administrator_authorised": auth.administrator, "os_user": _os_user(), "host": socket.gethostname(),
            "software_version": __version__, "filesystem": plan.filesystem, "previous_class": str(before.drive_class),
            "disk": {k: v for k, v in disk.identity.to_dict().items() if k in ("serial", "unique_id", "model",
                                                                                 "size_bytes", "bus_type")},
            "write_test": test,
        }
        written = write_marker(root, marker)
    except (OSError, ValueError) as exc:
        raise fail("marker_failed", "The drive was set up but could not be written to. Do not use this drive. "
                   "Contact technical support.", {"error": str(exc)}) from None
    after, disks = current_classification(ctx, disk.disk_id)
    if after.drive_class is not DriveClass.READY or (after.marker or {}).get("drive_uuid") != plan.drive_uuid:
        raise fail("not_ready_after_prepare", "The drive was set up but is not recognised. Contact technical support.",
                   {"class": str(after.drive_class), "reasons": list(after.reasons)})
    _audit(ctx, "drive_prepared", f"Drive prepared as {plan.label} by {auth.who}: {disk.describe()}",
           {**base, "identity_after": disk.identity.to_dict(), "write_test": test, "helper_seconds": round(helper_s, 1),
            "volume_id": vol.volume_id})
    return PrepareResult(disk.disk_id, plan.label, plan.drive_uuid, plan.filesystem, written, test,
                         str(before.drive_class), disk.identity.to_dict())


def _default_root(vol: VolumeInfo) -> str:
    # Prefer the volume GUID path (identity) over a drive letter; Linux uses the mount point.
    guid = [p for p in vol.mount_paths if p.startswith("\\\\?\\Volume{")]
    if guid:
        return guid[0]
    if vol.mount_paths:
        return vol.mount_paths[0]
    raise OSError("the prepared volume is not mounted")


def _os_user() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "unknown"


# ---------------------------------------------------------------------------- runners

EXIT_TIMEOUT = 5


def terminate_process(pid: int, expected_image: str = "powershell.exe") -> tuple[bool, str | None]:
    """Try to stop the timed-out elevated helper (finding 1.6).

    Expected to FAIL from a non-elevated parent: Windows' mandatory integrity policy denies
    PROCESS_TERMINATE on a High-integrity (elevated) process to a Medium-integrity caller, even of
    the same user. The failure is reported, never hidden. To avoid killing an unrelated process that
    reused the PID, the image name is checked first. NOT VERIFIED against a real elevated helper.
    """
    if sys.platform != "win32":
        return False, "not supported on this platform"
    import ctypes
    from ctypes import wintypes

    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False, f"cannot open process {pid} (error {ctypes.get_last_error()}); it may have exited"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) or \
                not buf.value.lower().endswith(expected_image):
            return False, f"process {pid} is not the helper ({buf.value or 'unknown image'}); not terminated"
    finally:
        k32.CloseHandle(h)
    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False, f"access denied terminating elevated helper {pid} (error {ctypes.get_last_error()})"
    try:
        if not k32.TerminateProcess(h, 1):
            return False, f"TerminateProcess failed (error {ctypes.get_last_error()})"
    finally:
        k32.CloseHandle(h)
    return True, None


class WindowsUacRunner:
    """Runs the plan's script in an elevated Windows PowerShell via UAC. NOT EXECUTED on any disk.

    The unelevated launcher is ``powershell.exe -Command`` containing only a fixed command, the
    powershell.exe path and base64 text (no hardware strings). ``Start-Process -Verb RunAs -Wait
    -PassThru`` returns the elevated process's exit code; declining the UAC prompt throws and is
    mapped to 1223 (ERROR_CANCELLED). Results come back through ``plan.result_path``.
    """

    def __init__(self, timeout_s: float = 1800.0) -> None:
        self.timeout_s = timeout_s

    def launcher_argv(self, plan: PreparePlan) -> list[str]:
        from .powershell import encode_command, powershell_exe

        exe = powershell_exe()
        if "'" in exe:
            raise PrepareRefused("bad_path", "unexpected PowerShell path")
        inner = encode_command(plan.script)
        cmd = ("$ErrorActionPreference = 'Stop'; try { "
               f"$p = Start-Process -FilePath '{exe}' -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
               f"-ArgumentList '-NoLogo -NoProfile -NonInteractive -EncodedCommand {inner}'; exit $p.ExitCode "
               "} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1223 }")
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cmd]

    def run(self, plan: PreparePlan) -> HelperResult:
        if plan.platform != "windows" or not plan.result_path:
            raise PrepareRefused("wrong_platform", "Windows runner needs a Windows plan")
        from .powershell import CREATE_NO_WINDOW

        try:
            proc = subprocess.run(self.launcher_argv(plan), capture_output=True, timeout=self.timeout_s,
                                  creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            # subprocess.run has killed the (non-elevated) launcher; the elevated helper is separate.
            progress = _read_result(plan.result_path) or {}
            pid = progress.get("pid")
            killed, err = terminate_process(int(pid)) if isinstance(pid, int) else (False, "helper PID unknown")
            return HelperResult(EXIT_TIMEOUT, {"ok": False, "step": progress.get("step", "unknown"), "error": "timeout",
                                               "helper_pid": pid, "still_running": not killed,
                                               "terminate_error": err}, "timeout")
        return HelperResult(proc.returncode, _read_result(plan.result_path),
                            proc.stderr.decode("utf-8", errors="replace"))


EXIT_FAILED_UNKNOWN = 2


def _read_result(path: str) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))  # PowerShell 5.1 writes a BOM
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class LinuxPkexecRunner:
    """Runs the plan's sh script as root through polkit. NOT EXECUTED (no Linux host available)."""

    def __init__(self, timeout_s: float = 1800.0) -> None:
        self.timeout_s = timeout_s

    def run(self, plan: PreparePlan) -> HelperResult:
        if plan.platform != "linux":
            raise PrepareRefused("wrong_platform", "Linux runner needs a Linux plan")
        p = subprocess.Popen(["pkexec", "/bin/sh", "-c", plan.script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = p.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            try:  # pkexec runs the script as root: an unprivileged parent normally cannot kill it
                p.kill()
                p.wait(timeout=10)
                killed, kerr = p.poll() is not None, None
            except (PermissionError, subprocess.TimeoutExpired) as exc:
                killed, kerr = False, str(exc)
            return HelperResult(EXIT_TIMEOUT, {"ok": False, "step": "unknown", "error": "timeout", "helper_pid": p.pid,
                                               "still_running": not killed, "terminate_error": kerr}, "timeout")
        proc = subprocess.CompletedProcess(p.args, p.returncode, out, err)
        if proc.returncode in (126, 127):  # pkexec: not authorised / dismissed
            return HelperResult(EXIT_ELEVATION_CANCELLED, {"ok": False, "step": "start"}, proc.stderr.decode(errors="replace"))
        return HelperResult(proc.returncode, parse_linux_result(proc.stdout.decode(errors="replace")),
                            proc.stderr.decode(errors="replace"))


def parse_linux_result(stdout: str) -> dict[str, Any] | None:
    line = next((x for x in reversed(stdout.splitlines()) if x.startswith("RESULT ")), None)
    if line is None:
        return None
    kv = dict(tok.split("=", 1) for tok in line.split()[1:] if "=" in tok)
    return {"ok": kv.get("ok") == "1", "step": kv.get("step", "unknown"), "error": kv.get("error"),
            "partition": kv.get("part")}


def udisks_mount(vol: VolumeInfo) -> None:  # NOT EXECUTED
    if not vol.dev_path:
        raise OSError("no device node for the prepared volume")
    if vol.mount_paths:
        return
    subprocess.run(["udisksctl", "mount", "--block-device", vol.dev_path, "--no-user-interaction"],
                   capture_output=True, check=True, timeout=60)


# ---------------------------------------------------------------------------- administrator check

ADMINISTRATORS_SID = "S-1-5-32-544"


def os_administrator_check() -> tuple[bool, str]:
    """Is the current OS account an administrator? (Interim answer to D4; Phase 4 adds app roles.)

    Windows: membership of BUILTIN\\Administrators (SID S-1-5-32-544) in the token, including as a
    deny-only group under UAC — i.e. the account *is* an administrator even when not elevated; the
    destructive step still needs the UAC prompt. Linux: root or membership of group
    ``sentinel-admin`` (docs/01 §2.5) — NOT EXECUTED.
    """
    if sys.platform == "win32":
        try:
            out = subprocess.run(["whoami", "/groups", "/fo", "csv", "/nh"], capture_output=True, timeout=30,
                                 creationflags=0x08000000).stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return False, "whoami unavailable"
        return (f'"{ADMINISTRATORS_SID}"' in out), "windows_administrators_group"
    if os.geteuid() == 0:  # type: ignore[attr-defined]
        return True, "root"
    import grp

    try:
        gid = grp.getgrnam("sentinel-admin").gr_gid
    except KeyError:
        return False, "group sentinel-admin missing"
    return gid in os.getgroups(), "group sentinel-admin"
