"""How the launcher finds out whether the recorder runs, starts it, and stops it.

``RecorderControl`` is the seam for other delivery mechanisms. Today there is one
implementation, :class:`DetachedProcessControl`: the launcher starts the recorder as a
background process of the same executable (``Sentinel.exe recorder run``) with no window.

To plug in the Windows service (branch design/windows-service), add a provider to
``CONTROL_PROVIDERS`` that returns a service-backed control when the service is installed
(``is_running`` = service state RUNNING, ``start`` = StartService, ``stop`` = ControlService).
The executable, the spec file and the build script do not change: the service host runs the
same ``Sentinel.exe`` with a different sub-command.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..config import AppConfig
from ..paths import is_frozen
from ..platform import InstanceLockError, Platform, get_platform
from ..status import read_status
from . import stopsignal

log = logging.getLogger(__name__)

EXIT_CONFIG, EXIT_LOCKED = 2, 3       # mirror sentinel.service exit codes
LOCK_NAME = "recorder.lock"

# Windows process-creation flags (values from the Win32 API, also exposed by ``subprocess``).
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000          # hidden console: FFmpeg/FFprobe children open no windows
CREATE_BREAKAWAY_FROM_JOB = 0x01000000  # survive a launcher started inside a kill-on-close job
ERROR_ACCESS_DENIED = 5


@dataclass(frozen=True)
class StartResult:
    ok: bool                    # True = recorder running or (re)started; status view tells the rest
    code: str | None = None     # operator alert code when not ok (C-001 / C-003)
    detail: str = ""            # for the launcher log and support, never shown to operators


class RecorderControl(Protocol):
    name: str

    def is_running(self) -> bool: ...

    def start(self) -> StartResult: ...

    def stop(self, timeout_s: float = 90.0) -> bool: ...


def recorder_command(config_path: Path, frozen: bool | None = None, executable: str | None = None) -> list[str]:
    """argv that runs the recorder in the foreground of a new process."""
    exe = executable or sys.executable
    if is_frozen() if frozen is None else frozen:
        return [exe, "recorder", "run", "--config", str(config_path)]
    return [exe, "-m", "sentinel.launcher", "recorder", "run", "--config", str(config_path)]


def detached_popen_kwargs(cwd: Path, platform: str | None = None, breakaway: bool = True) -> dict[str, Any]:
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": str(cwd)}
    if (platform or sys.platform) == "win32":
        flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        if breakaway:
            flags |= CREATE_BREAKAWAY_FROM_JOB
        kw["creationflags"] = flags
    else:
        kw["start_new_session"] = True  # own session: closing the terminal does not SIGHUP it
    return kw


class DetachedProcessControl:
    """Interim control: recorder as a windowless background process guarded by its instance lock.

    Limitations (documented, replaced by the service branch): no automatic start at boot or
    logon; runs under the account of whoever started it; restarted only when someone opens
    Sentinel again after it stopped.
    """

    name = "background process"

    def __init__(self, config_path: Path, cfg: AppConfig, platform: Platform | None = None,
                 popen: Callable[..., Any] = subprocess.Popen, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, command: list[str] | None = None) -> None:
        self.config_path = Path(config_path)
        self.cfg = cfg
        self.platform = platform or get_platform()
        self._popen = popen
        self._clock = clock
        self._sleep = sleep
        self.command = command or recorder_command(self.config_path)
        self.lock_path = cfg.data_path / LOCK_NAME
        self.status_path = cfg.data_path / "status.json"

    def is_running(self) -> bool:
        """True if a recorder holds the instance lock. Do not call while one may be starting:
        the probe takes the lock for an instant and a starting recorder would then refuse to run."""
        try:
            handle = self.platform.acquire_instance_lock(self.lock_path)
        except InstanceLockError:
            return True
        handle.close()
        return False

    def _spawn(self) -> Any:
        self.cfg.data_path.mkdir(parents=True, exist_ok=True)
        try:
            return self._popen(self.command, **detached_popen_kwargs(self.cfg.data_path))
        except OSError as exc:
            # A job that forbids breakaway rejects CREATE_BREAKAWAY_FROM_JOB with access denied.
            if sys.platform == "win32" and getattr(exc, "winerror", None) == ERROR_ACCESS_DENIED:
                log.warning("job object forbids breakaway; starting recorder inside the launcher's job")
                return self._popen(self.command, **detached_popen_kwargs(self.cfg.data_path, breakaway=False))
            raise

    def start(self, confirm_s: float = 20.0) -> StartResult:
        started = self._clock()
        try:
            proc = self._spawn()
        except OSError as exc:
            log.error("could not start the recorder: %s", exc)
            return StartResult(False, "C-003", f"spawn failed: {exc}")
        log.info("recorder started in the background (pid %s): %s", getattr(proc, "pid", "?"), self.command)
        # Confirmation = a status heartbeat written after we spawned it (the recorder writes one
        # right after taking its lock, also when it then fails on configuration). The lock is
        # deliberately not probed here (see is_running).
        while self._clock() - started < confirm_s:
            rc = proc.poll()
            if rc is not None:
                if rc == EXIT_LOCKED:
                    return StartResult(True, None, "another recorder instance already holds the lock")
                if rc == EXIT_CONFIG:
                    return StartResult(False, "C-001", "recorder exited: configuration or tool problem")
                return StartResult(False, "C-003", f"recorder exited with code {rc}")
            doc, _ = read_status(self.status_path, max_age_s=float("inf"))
            if doc and float(doc.get("heartbeat_epoch", 0)) >= started - 1.0:
                return StartResult(True, None, f"recorder confirmed by status heartbeat (pid {proc.pid})")
            self._sleep(0.25)
        return StartResult(True, None, f"recorder process alive but not yet confirmed after {confirm_s:.0f} s")

    def stop(self, timeout_s: float = 90.0) -> bool:
        """Graceful stop (closes the current segment). True once the instance lock is free."""
        if not self.is_running():
            return True
        if not stopsignal.request_stop(self.cfg.data_path):
            log.error("recorder holds its lock but does not listen on the stop channel")
            return False
        end = self._clock() + timeout_s
        while self._clock() < end:
            self._sleep(0.5)
            if not self.is_running():
                return True
        return False


# Providers are tried in order; the first that returns a control wins. The detached process is
# the fallback. Signature: (config_path, cfg) -> RecorderControl | None.
ControlProvider = Callable[[Path, AppConfig], "RecorderControl | None"]
CONTROL_PROVIDERS: list[ControlProvider] = []


def select_control(config_path: Path, cfg: AppConfig) -> RecorderControl:
    for provider in CONTROL_PROVIDERS:
        control = provider(config_path, cfg)
        if control is not None:
            return control
    return DetachedProcessControl(config_path, cfg)
