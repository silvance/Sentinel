"""Applying an update file, and going back (owner request 2026-11-10).

How an update is applied (Windows; ``Sentinel update apply FILE``, or System > Updates in the window):

1. **Check** the file (``package.read_package``: signature, product, platform, newer, applicable).
2. **Stage**: build the complete new program folder next to the installed one
   (``<app>.update-<version>``) from the installed files whose SHA-256 is unchanged plus the files
   carried in the update; every file is hash-checked against the signed file list. Files someone
   added to the program folder (such as a technician's ``sentinel.toml``) are carried over.
   Nothing has been changed yet; recording continues.
3. **Stop** the recorder and the archive copier gracefully (current segment closed), remembering
   how they ran (Windows services or background processes).
4. **Swap** (``finisher_script``, a PowerShell script started outside the program folder, in a
   visible window): wait until no program runs from the folder, copy the folder's permissions to the
   new one, rename ``<app>`` to ``<app>.previous`` and the new folder to ``<app>``.
5. **Check** with the *new* version (``Sentinel update finish``): every file matches its build
   manifest, the settings load, FFmpeg runs, and the recorder starts again (if it was running).
   The update and its file's SHA-256 are written to the recording log.
6. If 4 or 5 fails, the finisher swaps the folders back and the *old* version restarts the
   recorder (``Sentinel update restart``) and records why. The window, reopened, watches the
   recorder for 3 minutes as after a settings change and goes back to the previous version by
   itself if the recorder does not run.

``<app>.previous`` keeps the version before; ``Sentinel update rollback`` swaps back to it the same
way. Work files (state, finisher script, log, result) are in ``<app>.updates``, next to the program
folder, so they have the program folder's permissions (only administrators may change them).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..clock import iso_utc
from .package import BUILD_FILES, PAYLOAD_PREFIX, Package, UpdateRefused

log = logging.getLogger(__name__)

STATE_NAME = "pending.json"
RESULT_NAME = "last_update.json"
LOG_NAME = "update.log"
SERVICES = ("SentinelRecorder", "SentinelArchive")
# Review 2026-11-12: a normal recorder stop seals the segment just closed (up to an hour of video) and waits
# for the content-hash worker, so it can take minutes; the services end themselves after 600 s
# (winsvc.scm stop_deadline_s). 90 s gave up early and left both services stopped.
STOP_TIMEOUT_S = 660.0


# ---------------------------------------------------------------------- folders
@dataclass(frozen=True)
class Folders:
    app: Path

    @property
    def work(self) -> Path:
        return self.app.parent / f"{self.app.name}.updates"

    @property
    def previous(self) -> Path:
        return self.app.parent / f"{self.app.name}.previous"

    def staging(self, version: str) -> Path:
        return self.app.parent / f"{self.app.name}.update-{version}"

    @property
    def state(self) -> Path:
        return self.work / STATE_NAME

    @property
    def result(self) -> Path:
        return self.work / RESULT_NAME

    @property
    def log(self) -> Path:
        return self.work / LOG_NAME


def installed_manifest(app: Path) -> dict[str, Any]:
    try:
        return json.loads((app / "BUILD-MANIFEST.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def folder_version(folder: Path) -> str | None:
    v = installed_manifest(folder).get("version")
    return str(v) if v else None


# ---------------------------------------------------------------------- staging
@dataclass
class StagePlan:
    from_package: list[str] = field(default_factory=list)
    from_installed: list[str] = field(default_factory=list)
    local_files: list[str] = field(default_factory=list)      # added by someone; carried over unchanged
    missing: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.from_package) + len(self.from_installed) + len(self.local_files)


def _walk(folder: Path) -> list[str]:
    out = []
    for p in folder.rglob("*"):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(folder).as_posix()
            if "__pycache__" not in rel.split("/"):
                out.append(rel)
    return sorted(out)


def plan_stage(app: Path, pkg: Package) -> StagePlan:
    old = installed_manifest(app).get("files") or {}
    known = set(old) | set(BUILD_FILES)
    plan = StagePlan()
    for rel, meta in sorted(pkg.files.items()):
        if rel in pkg.payload:
            plan.from_package.append(rel)
        elif old.get(rel) == meta["sha256"] and (app / rel).is_file():
            plan.from_installed.append(rel)
        else:
            plan.missing.append(rel)
    targets = {r.lower() for r in pkg.files}
    plan.local_files = [r for r in _walk(app) if r not in known and r.lower() not in targets]
    return plan


def _copy_hashed(src, dest: Path) -> tuple[str, int]:  # noqa: ANN001 - binary file object
    dest.parent.mkdir(parents=True, exist_ok=True)
    h, n = hashlib.sha256(), 0
    with dest.open("wb") as out:
        for block in iter(lambda: src.read(1 << 20), b""):
            h.update(block)
            n += len(block)
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    return h.hexdigest(), n


def stage(app: Path, pkg: Package, staging: Path,
          progress: Callable[[int, int, str], None] | None = None) -> StagePlan:
    """Build the complete new program folder at ``staging``. Raises UpdateRefused (and removes the
    partial folder) if any file is missing or does not match the signed file list."""
    plan = plan_stage(app, pkg)
    if plan.missing:
        shown = ", ".join(plan.missing[:5]) + (" ..." if len(plan.missing) > 5 else "")
        raise UpdateRefused("needs_files", f"This update cannot be applied to the installed program: "
                            f"{len(plan.missing)} file(s) are not in it or were changed ({shown}). "
                            "Install the full program folder instead. Nothing was changed.")
    if staging.exists():
        shutil.rmtree(staging)
    done = 0

    def tick(rel: str) -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, plan.total, rel)

    try:
        staging.mkdir(parents=True)
        for rel in plan.from_installed:
            with (app / rel).open("rb") as src:
                digest, _ = _copy_hashed(src, staging / rel)
            if digest != pkg.files[rel]["sha256"]:
                raise UpdateRefused("installed_changed", f"The installed file {rel} does not match its build "
                                    "manifest (changed or damaged). Install the full program folder instead. "
                                    "Nothing was changed.")
            tick(rel)
        with zipfile.ZipFile(pkg.path) as zf:
            for rel in plan.from_package:
                with zf.open(PAYLOAD_PREFIX + rel) as src:
                    digest, size = _copy_hashed(src, staging / rel)
                if digest != pkg.files[rel]["sha256"] or size != pkg.files[rel]["size"]:
                    raise UpdateRefused("payload_mismatch", f"{rel} in the update file does not match its signed "
                                        "hash. Nothing was changed.")
                tick(rel)
        for rel in plan.local_files:
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(app / rel, dest)
            tick(rel)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return plan


# ---------------------------------------------------------------------- recorder state
def _sc(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def service_state(name: str, sc: Callable[[list[str]], subprocess.CompletedProcess] = _sc) -> str | None:
    """'RUNNING' / 'STOPPED' / ... or None when the service is not installed."""
    r = sc(["query", name])
    if r.returncode != 0:
        return None
    m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", r.stdout or "")
    return m.group(1).upper() if m else None


def _exit_code(name: str, sc: Callable[[list[str]], subprocess.CompletedProcess]) -> str:
    """'exit 2' from ``sc query`` (the service's own code when WIN32_EXIT_CODE is 1066), or ''."""
    try:
        out = sc(["query", name]).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""
    w = re.search(r"WIN32_EXIT_CODE\s*:\s*(\d+)", out)
    s = re.search(r"SERVICE_EXIT_CODE\s*:\s*(\d+)", out)
    code = s.group(1) if w and w.group(1) == "1066" and s else (w.group(1) if w else "")
    return f"exit {code}" if code else ""


@dataclass
class RecordingState:
    service_managed: bool = False
    services: list[str] = field(default_factory=list)   # services that were running (restart these)
    recorder_running: bool = False


def _not_stopped(st: RecordingState, message: str) -> UpdateRefused:
    exc = UpdateRefused("not_stopped", message)
    exc.state = st              # type: ignore[attr-defined]  # what was stopped: the caller starts it again
    return exc


def restart_after_refusal(exc: BaseException, cfg_path: Path | None) -> str:
    """After ``stop_recording`` gave up: start again what it had stopped. A sentence for the operator."""
    st = getattr(exc, "state", None)
    if not isinstance(st, RecordingState) or not (st.services or st.recorder_running):
        return ""
    ok, detail = start_recording(st, cfg_path)
    if ok:
        return "Recording was started again."
    return (f"Recording could not be started again ({detail}). Restart the computer so that recording starts, "
            "and contact technical support.")


def stop_recording(cfg_path: Path | None, *, control_factory: Callable[..., Any] | None = None,
                   sc: Callable[[list[str]], subprocess.CompletedProcess] = _sc,
                   sleep: Callable[[float], None] = time.sleep, timeout_s: float = STOP_TIMEOUT_S,
                   on_wait: Callable[[float], None] | None = None) -> RecordingState:
    """Remember how recording ran, then stop it gracefully. Raises UpdateRefused if the recorder does
    not stop (no program file has been changed); the exception's ``state`` says what was stopped, and
    the caller starts it again (:func:`restart_after_refusal`)."""
    st = RecordingState()
    if sys.platform == "win32":
        running = [s for s in SERVICES if service_state(s, sc) == "RUNNING"]
        installed = [s for s in SERVICES if service_state(s, sc) is not None]
        st.service_managed = bool(installed)
        st.services = running
        st.recorder_running = "SentinelRecorder" in running
        try:
            for name in reversed(running):          # archive copier first, then the recorder
                sc(["stop", name])
            t0 = time.monotonic()
            end = t0 + timeout_s
            next_note = t0 + 30.0
            while any(service_state(s, sc) not in ("STOPPED", None) for s in running):
                if time.monotonic() > end:
                    raise _not_stopped(st, "The recorder service did not stop in time, so the update was not "
                                           "applied (no program file was changed).")
                if on_wait is not None and time.monotonic() >= next_note:
                    on_wait(time.monotonic() - t0)
                    next_note += 30.0
                sleep(1.0)
        except UpdateRefused:
            raise
        except BaseException as exc:  # noqa: BLE001 - review 2026-11-12: sc.exe timed out, Ctrl+C, ...
            # Whatever went wrong after a stop was asked for, the caller must start the services again.
            raise _not_stopped(st, f"Stopping the recorder service went wrong ({exc.__class__.__name__}: {exc}), "
                                   "so the update was not applied (no program file was changed).") from exc
        if st.service_managed:
            return st
    if cfg_path is None or not Path(cfg_path).is_file():
        return st
    from ..config import ConfigError, load_config
    from ..launcher.control import select_control

    try:
        cfg = load_config(cfg_path)
    except (ConfigError, OSError):
        return st
    control = (control_factory or select_control)(Path(cfg_path), cfg)
    st.recorder_running = bool(control.is_running())
    if st.recorder_running and not control.stop(timeout_s):
        raise _not_stopped(st, "The recorder did not stop in time, so the update was not applied (no program "
                               "file was changed).")
    return st


def _confirm_running(name: str, sc: Callable[[list[str]], subprocess.CompletedProcess],
                     sleep: Callable[[float], None], confirm_s: float, settle_s: float) -> str | None:
    """Wait until the service reports RUNNING and is still running ``settle_s`` later. None = fine, else why not.
    Review 2026-11-12: ``sc start`` returns once the process has connected (START_PENDING); a new version whose
    service host fails while starting (configuration, permission check, a missing module) stops only after."""
    for _ in range(max(1, int(confirm_s))):
        state = service_state(name, sc)
        if state == "RUNNING":
            break
        if state in ("STOPPED", None):
            return f"{name} stopped while starting ({_exit_code(name, sc) or 'no exit code'})"
        sleep(1.0)
    else:
        return f"{name} did not report running within {confirm_s:.0f} s"
    if settle_s > 0:
        sleep(settle_s)
        if service_state(name, sc) != "RUNNING":
            return f"{name} stopped again right after starting ({_exit_code(name, sc) or 'no exit code'})"
    return None


def stop_services(names: list[str], sc: Callable[[list[str]], subprocess.CompletedProcess] = _sc,
                  sleep: Callable[[float], None] = time.sleep, timeout_s: float = 120.0) -> bool:
    """Best effort: stop these services and wait until they are stopped (so the program folder can be swapped
    back after a failed check). True when all are stopped."""
    try:
        for name in reversed(names):            # archive copier first, then the recorder
            sc(["stop", name])
        for _ in range(max(1, int(timeout_s))):
            if all(service_state(n, sc) in ("STOPPED", None) for n in names):
                return True
            sleep(1.0)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not stop %s: %s", ", ".join(names), exc)
    return False


def start_recording(st: RecordingState, cfg_path: Path | None, *, control_factory: Callable[..., Any] | None = None,
                    sc: Callable[[list[str]], subprocess.CompletedProcess] = _sc,
                    sleep: Callable[[float], None] = time.sleep, confirm_s: float = 60.0,
                    settle_s: float = 5.0) -> tuple[bool, str]:
    """Start again what ``stop_recording`` stopped and check that it runs. (ok, detail)."""
    if st.service_managed:
        problems = []
        for name in st.services:                # recorder first, then the archive copier
            try:
                r = sc(["start", name])
            except (OSError, subprocess.SubprocessError) as exc:
                problems.append(f"{name}: {exc}")
                continue
            if r.returncode != 0 and "1056" not in (r.stdout or ""):     # 1056 = already running
                problems.append(f"{name}: {(r.stdout or r.stderr or '').strip()[-200:]}")
                continue
            try:
                why = _confirm_running(name, sc, sleep, confirm_s, settle_s)
            except (OSError, subprocess.SubprocessError) as exc:
                why = f"{name}: state could not be read ({exc})"
            if why:
                problems.append(why)
        return (not problems, "; ".join(problems) or "services started")
    if not st.recorder_running or cfg_path is None:
        return True, "the recorder was not running before the update"
    from ..config import ConfigError, load_config
    from ..launcher.control import select_control

    try:
        cfg = load_config(cfg_path)
    except (ConfigError, OSError) as exc:
        return False, f"settings cannot be read: {exc}"
    control = (control_factory or select_control)(Path(cfg_path), cfg)
    res = control.start()
    starter = getattr(control, "start_archive", None)
    if res.ok and callable(starter):
        starter()
    return bool(res.ok), res.detail or (res.code or "")


# ---------------------------------------------------------------------- state and result
@dataclass
class Pending:
    action: str                  # "update" | "rollback"
    from_version: str
    to_version: str
    app: str
    staged: str
    previous: str
    config: str | None
    recording: dict[str, Any]
    package: str | None = None
    package_sha256: str | None = None
    key_id: str | None = None
    who: str = ""
    started_utc: str = ""
    reason: str = ""
    reopen: bool = True          # open the Sentinel window again at the end (tests: False)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1), "utf-8")
        tmp.replace(path)

    @classmethod
    def read(cls, path: Path) -> Pending:
        doc = json.loads(Path(path).read_text("utf-8-sig"))
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in doc.items() if k in known})   # a newer version may add fields


def write_result(folders: Folders, outcome: str, pending: Pending | None, message: str, *,
                 watch: bool = False, version: str | None = None) -> dict[str, Any]:
    doc = {"outcome": outcome, "message": message, "finished_utc": iso_utc(time.time()),
           "version": version or __version__, "watch": watch, "shown": False}
    if pending is not None:
        doc.update(action=pending.action, from_version=pending.from_version, to_version=pending.to_version,
                   package_sha256=pending.package_sha256, who=pending.who)
    folders.work.mkdir(parents=True, exist_ok=True)
    tmp = folders.result.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), "utf-8")
    tmp.replace(folders.result)
    return doc


FINISHER_PID_NAME = "finisher.pid"     # in Folders.work: the PowerShell window doing the swap


def _process_alive(pid: int) -> bool | None:
    """True / False, or None when it cannot be told (e.g. another account's process)."""
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return True
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    h = k32.OpenProcess(0x1000, False, pid)                  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False if ctypes.get_last_error() == 87 else None   # 87: no such process
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return None
        if code.value != 259:                                      # STILL_ACTIVE
            return False
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return "powershell" in buf.value.lower()               # the number was reused by another program
        return True
    finally:
        k32.CloseHandle(h)


def finisher_alive(folders: Folders, alive: Callable[[int], bool | None] | None = None) -> bool | None:
    """Whether the swap window of the current update still runs (None: not known, e.g. no pid file)."""
    try:
        pid = int(folders.work.joinpath(FINISHER_PID_NAME).read_text("utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        return (alive or _process_alive)(pid)
    except OSError:
        return None


def in_progress(folders: Folders, now: float | None = None, stale_s: float = 3600.0,
                alive: Callable[[int], bool | None] | None = None) -> str | None:
    """'Updating to Sentinel 0.3.4' while an update or rollback is under way (its state file is less
    than an hour old), else None. The window shows it instead of "System error" while recording is
    stopped for the swap (field report 2026-11-11).

    Review 2026-11-12: not when the update has ended without removing its state file (a result written
    after it) or when its swap window is gone (closed, or never started); the real status is shown then."""
    try:
        mtime = folders.state.stat().st_mtime
        if (time.time() if now is None else now) - mtime > stale_s:
            return None
        p = Pending.read(folders.state)
    except (OSError, ValueError, TypeError):
        return None
    try:
        if folders.result.stat().st_mtime >= mtime:
            return None
    except OSError:
        pass
    if finisher_alive(folders, alive) is False:
        return None
    return (f"Updating to Sentinel {p.to_version}" if p.action == "update"
            else f"Going back to Sentinel {p.to_version}")


def abandoned(folders: Folders, alive: Callable[[int], bool | None] | None = None) -> Pending | None:
    """The state of an update or rollback that ended without cleaning up (its swap window is gone, or it is
    older than an hour), so a new one can start and restart what that one had stopped. None otherwise."""
    if not folders.state.is_file():
        return None
    try:
        p = Pending.read(folders.state)
    except (OSError, ValueError, TypeError):
        return None
    return p if in_progress(folders, alive=alive) is None else None


def end_state(folders: Folders) -> None:
    """Remove the state and pid files: the update or rollback is over (whatever its outcome)."""
    for f in (folders.state, folders.work / FINISHER_PID_NAME):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove %s: %s", f, exc)


def read_result(folders: Folders) -> dict[str, Any] | None:
    try:
        # utf-8-sig: Windows PowerShell 5.1 writes a byte-order mark (review 2026-11-12)
        doc = json.loads(folders.result.read_text("utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def mark_result(folders: Folders, **changes: Any) -> None:
    doc = read_result(folders)
    if doc is None:
        return
    doc.update(changes)
    tmp = folders.result.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), "utf-8")
    tmp.replace(folders.result)


def record_event(cfg_path: str | None, code: str, message: str, detail: dict[str, Any],
                 severity: str = "audit") -> None:
    """Software changes go into the recording log (hash chain), so the log shows which version
    recorded what. Best effort: a failure is logged, never fatal to the update."""
    if not cfg_path:
        return
    try:
        from ..config import load_config
        from ..db import Ledger

        cfg = load_config(Path(cfg_path))
        led = Ledger(cfg.data_path / "sentinel.db")
        try:
            led.add_event(severity, "software", code, message, detail)
        finally:
            led.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not write %s to the recording log: %s", code, exc)


# ---------------------------------------------------------------------- the swap (Windows)
def _ps(s: str | Path) -> str:
    """A PowerShell single-quoted string literal. Paths are normalised (backslashes, no '..') so the
    script's "is this process running from the program folder?" test cannot miss by spelling."""
    text = os.path.normpath(str(s)) if isinstance(s, Path) else str(s)
    return "'" + text.replace("'", "''") + "'"


def finisher_script(p: Pending, folders: Folders, window_pid: int | None) -> str:
    """PowerShell that swaps the folders once nothing runs from them, then lets the new version check
    itself; swaps back and restarts the old version if anything fails. Runs in a visible window."""
    app, staged, prev = Path(p.app), Path(p.staged), Path(p.previous)
    hold = app.parent / f"{app.name}.swap-hold"
    prev_old = app.parent / f"{app.name}.previous-old"
    failed = app.parent / f"{app.name}.failed-{p.to_version}"
    exe = app / "Sentinel.exe"
    what = (f"Updating Sentinel {p.from_version} to {p.to_version}" if p.action == "update"
            else f"Going back from Sentinel {p.from_version} to {p.to_version}")
    rec = p.recording if isinstance(p.recording, dict) else {}
    services = [s for s in rec.get("services") or [] if s in SERVICES] if rec.get("service_managed") else []
    svc_list = ", ".join(_ps(s) for s in services)
    return f"""# Written by Sentinel {__version__} ({p.action}); runs once, then is deleted.
$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'Sentinel update - do not close'
$app = {_ps(app)}; $staged = {_ps(staged)}; $prev = {_ps(prev)}; $hold = {_ps(hold)}; $failed = {_ps(failed)}
$prevOld = {_ps(prev_old)}
$exe = {_ps(exe)}; $state = {_ps(folders.state)}; $logf = {_ps(folders.log)}; $result = {_ps(folders.result)}
$pidf = {_ps(folders.work / FINISHER_PID_NAME)}
$services = @({svc_list})
function Say($m) {{ $line = (Get-Date).ToString('s') + '  ' + $m; Write-Host $m; Add-Content -LiteralPath $logf -Value $line -Encoding UTF8 }}
# The result is read by Python: UTF-8 WITHOUT a byte-order mark (Set-Content -Encoding UTF8 in Windows
# PowerShell 5.1 writes one, and the result could then not be read; review 2026-11-12).
function Fail-Plain($m) {{
  Say "FAILED: $m"
  $doc = @{{ outcome = 'failed'; message = $m; finished_utc = (Get-Date).ToUniversalTime().ToString('o')
            shown = $false; watch = $false
            action = {_ps(p.action)}; from_version = {_ps(p.from_version)}; to_version = {_ps(p.to_version)} }}
  [IO.File]::WriteAllText($result, ($doc | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
}}
# Every way out removes the state file, so the window shows the real status again (review 2026-11-12).
function End-Update {{
  Remove-Item -LiteralPath $state -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $pidf -Force -ErrorAction SilentlyContinue
}}
function Stop-Services {{
  $rev = @($services); [array]::Reverse($rev)          # the archive copier first, then the recorder
  foreach ($s in $rev) {{ if ($s) {{ try {{ $null = & sc.exe stop $s }} catch {{}} }} }}
}}
# Every use is wrapped in @(): Windows PowerShell 5.1 unrolls a one-item result into a single CIM
# object, which has no .Count, so one process would count as none (end-to-end test 2026-11-11).
function From-App {{
  @(Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -and $_.ExecutablePath.StartsWith($app + '\\', [StringComparison]::OrdinalIgnoreCase) }})
}}
# Only the recorder must have stopped by itself (it closes its current file first). Windows and the
# archive copier are ended here: the copier is built to survive a drive pulled mid-copy and is
# started again with the recorder (field report 2026-11-11: it was taken for the recorder).
function Recorders {{
  @(From-App | Where-Object {{ $_.CommandLine -match '\\brecorder\\s+run\\b' -or $_.Name -like 'SentinelService*' }})
}}
# A click in this window must not pause the update (field report 2026-11-11: Windows' text selection
# mode froze it at the next line printed). Switch off QuickEdit for this console.
try {{
  $cm = Add-Type -PassThru -Name ConsoleMode -Namespace SentinelUpdate -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern System.IntPtr GetStdHandle(int n);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(System.IntPtr h, out uint m);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(System.IntPtr h, uint m);
'@
  $hin = $cm::GetStdHandle(-10); $mode = 0
  if ($cm::GetConsoleMode($hin, [ref]$mode)) {{ [void]$cm::SetConsoleMode($hin, (($mode -band (-bnot 0x40)) -bor 0x80)) }}
}} catch {{}}
Say {_ps(what + ".")}
Say 'Recording has been stopped for the update; it restarts by itself. Please do not close this window.'
{f"try {{ Wait-Process -Id {int(window_pid)} -Timeout 30 -ErrorAction SilentlyContinue }} catch {{}}" if window_pid else ""}
$deadline = (Get-Date).AddSeconds(60)
while (@(Recorders).Count -gt 0 -and (Get-Date) -lt $deadline) {{ Start-Sleep -Seconds 1 }}
if (@(Recorders).Count -gt 0) {{
  Say 'The recorder is still running; this is cancelled and recording continues on the installed version.'
  & $exe update restart --state $state --outcome aborted --reason 'the recorder did not stop'
  {"Remove-Item -LiteralPath $staged -Recurse -Force -ErrorAction SilentlyContinue" if p.action == "update"
   else "# (going back: $staged is the kept previous version; it stays)"}
  End-Update
  Start-Sleep -Seconds 5; exit 1
}}
$others = @(From-App)
if ($others.Count -gt 0) {{
  Say ('Closing ' + $others.Count + ' other Sentinel program(s): open windows and the archive copier (it starts again after the update).')
  $others | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
  $gone = (Get-Date).AddSeconds(20)
  while (@(From-App).Count -gt 0 -and (Get-Date) -lt $gone) {{ Start-Sleep -Seconds 1 }}
}}
try {{
  try {{ Get-Acl -LiteralPath $app | Set-Acl -LiteralPath $staged }} catch {{ Say ('Note: permissions not copied: ' + $_.Exception.Message) }}
  if (Test-Path -LiteralPath $hold) {{ Remove-Item -LiteralPath $hold -Recurse -Force }}
  Move-Item -LiteralPath $staged -Destination $hold
  # The version kept from before is set aside, not deleted, until the new one has passed its check.
  if (Test-Path -LiteralPath $prevOld) {{ Remove-Item -LiteralPath $prevOld -Recurse -Force }}
  if (Test-Path -LiteralPath $prev) {{ Move-Item -LiteralPath $prev -Destination $prevOld }}
  Move-Item -LiteralPath $app -Destination $prev
  try {{ Move-Item -LiteralPath $hold -Destination $app }}
  catch {{ Move-Item -LiteralPath $prev -Destination $app; throw }}
}} catch {{
  if ((Test-Path -LiteralPath $prevOld) -and -not (Test-Path -LiteralPath $prev)) {{ Move-Item -LiteralPath $prevOld -Destination $prev }}
  if (Test-Path -LiteralPath $hold) {{ Move-Item -LiteralPath $hold -Destination $staged -ErrorAction SilentlyContinue }}
  Fail-Plain ('The program folders could not be swapped: ' + $_.Exception.Message + '. The installed version is unchanged.')
  & $exe update restart --state $state --outcome aborted --reason 'the program folders could not be swapped'
  End-Update
  Start-Sleep -Seconds 10; exit 1
}}
Say {_ps(f"Checking Sentinel {p.to_version}...")}
& $exe update finish --state $state
$rc = $LASTEXITCODE
if ($rc -ne 0) {{
  Say 'The check failed; going back to the version that was installed before.'
  # Review 2026-11-12: a service the new version started (or that Windows restarts after it failed) runs from
  # the program folder, and a folder in use cannot be moved. Stop them and wait, then move; try again.
  $outDone = $false; $backDone = $false; $why = ''
  for ($try = 1; $try -le 4 -and -not $backDone; $try++) {{
    Stop-Services
    $gone = (Get-Date).AddSeconds(30)
    while (@(From-App).Count -gt 0 -and (Get-Date) -lt $gone) {{ Start-Sleep -Seconds 1 }}
    $keep = @(@(Recorders) | ForEach-Object {{ $_.ProcessId }})   # a recorder is never killed; it is asked to stop
    @(From-App) | Where-Object {{ $keep -notcontains $_.ProcessId }} |
      ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
    try {{
      if (-not $outDone) {{
        if (Test-Path -LiteralPath $failed) {{ Remove-Item -LiteralPath $failed -Recurse -Force }}
        Move-Item -LiteralPath $app -Destination $failed
        $outDone = $true
      }}
      Move-Item -LiteralPath $prev -Destination $app
      $backDone = $true
    }} catch {{
      $why = $_.Exception.Message
      Say ('Going back: attempt ' + $try + ' did not work (' + $why + ')')
      Start-Sleep -Seconds 3
    }}
  }}
  if (-not $backDone) {{
    Fail-Plain ('Going back failed: ' + $why + '. Call technical support; the previous version is in ' + $prev)
    if (Test-Path -LiteralPath $exe) {{
      foreach ($s in $services) {{ try {{ $null = & sc.exe start $s }} catch {{}} }}
    }}
    End-Update
    Start-Sleep -Seconds 30; exit 2
  }}
  if (Test-Path -LiteralPath $prevOld) {{
    try {{ Move-Item -LiteralPath $prevOld -Destination $prev }} catch {{ Say ('Note: ' + $_.Exception.Message) }}
  }}
  & $exe update restart --state $state --outcome rolled_back --reason 'the new version did not pass its check'
}} else {{
  if (Test-Path -LiteralPath $prevOld) {{ Remove-Item -LiteralPath $prevOld -Recurse -Force -ErrorAction SilentlyContinue }}
  Say 'Done.'
}}
End-Update
{"Start-Process -FilePath $exe" if p.reopen else "# (the window is not reopened)"}
Start-Sleep -Seconds 3
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
exit $rc
"""


def launch_finisher(script: str, folders: Folders, popen: Callable[..., Any] = subprocess.Popen) -> Path:
    """Write the finisher next to the program folder and start it in its own visible console,
    outside the program folder, so it outlives this process."""
    folders.work.mkdir(parents=True, exist_ok=True)
    path = folders.work / f"finish-{int(time.time())}.ps1"
    path.write_text(script, encoding="utf-8-sig")
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    ps = system / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    flags |= 0x01000000                                   # CREATE_BREAKAWAY_FROM_JOB
    argv = [str(ps), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
    try:
        proc = popen(argv, cwd=str(system), creationflags=flags, close_fds=True)
    except OSError:
        proc = popen(argv, cwd=str(system), creationflags=flags & ~0x01000000, close_fds=True)
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int):
        # So the window can tell a running update from one whose window was closed (review 2026-11-12).
        try:
            (folders.work / FINISHER_PID_NAME).write_text(str(pid), "utf-8")
        except OSError as exc:
            log.warning("could not note the update window's process id: %s", exc)
    return path


# ---------------------------------------------------------------------- checks run by the new version
def check_installed(app: Path, cfg_path: str | None, *, run: Callable[..., Any] = subprocess.run) -> list[str]:
    """Problems with the program now in ``app`` (empty = fine): files vs. their build manifest,
    version, settings, FFmpeg. Run by the new version right after the swap."""
    from ..launcher.cli import verify_manifest

    problems = []
    manifest = installed_manifest(app)
    if not manifest.get("files"):
        return ["the program folder has no build manifest"]
    if str(manifest.get("version")) != __version__:
        problems.append(f"the build manifest says {manifest.get('version')}, the program is {__version__}")
    problems += verify_manifest(app, manifest)[:10]
    if cfg_path and Path(cfg_path).is_file():
        from ..config import ConfigError, load_config

        try:
            load_config(Path(cfg_path))
        except (ConfigError, OSError) as exc:
            problems.append(f"the settings cannot be read by this version: {exc}")
    ff = app / "ffmpeg" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if ff.is_file():
        try:
            r = run([str(ff), "-hide_banner", "-version"], capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0:
                problems.append(f"FFmpeg does not run (exit code {r.returncode})")
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"FFmpeg does not run: {exc}")
    return problems
