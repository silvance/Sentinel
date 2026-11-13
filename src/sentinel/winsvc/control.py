"""Launcher control for an installed Windows service (integration of design/single-executable
and design/windows-service; see docs/INTEGRATION.md).

``Sentinel.exe`` asks the providers in ``sentinel.launcher.control.CONTROL_PROVIDERS`` how the
recorder is run. :func:`service_control_provider` (first in that list) returns a
:class:`ServiceControl` whenever the ``SentinelRecorder`` service is **installed**, whatever its
state, so the launcher never starts a second, detached recorder beside it (two recorders would
race for the instance lock, and the one in the user session would stop at log-off).

* ``is_running``: the SCM reports RUNNING (or START_PENDING: starting is in progress).
* ``start``: StartService. Standard users normally lack the right: access denied gives C-003 and
  **no fallback** to a detached recorder. A service that stops again with exit code 2 (its
  configuration-problem code) gives C-001; any other stop gives C-003. The operator screen still
  shows what the service itself wrote into ``status.json``.
* ``stop``: ControlService(STOP); the service host stops the recorder gracefully.
* ``start_archive`` / ``stop_archive`` / ``restart_archive``: the same for ``SentinelArchive``, which reads
  its settings once at start and is not restarted by Windows after a clean stop.
* If the SCM cannot be asked at all, the provider fails closed: a control that never starts a
  recorder and reports C-003, rather than guessing that no service exists.

The SCM calls are injected (``api``) so this is unit-tested with a fake. Real StartService /
ControlService against an installed service: NOT EXECUTED (needs administrator rights to install).
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import SERVICE_NAME

log = logging.getLogger(__name__)

EXIT_CONFIG = 2   # sentinel.service.EXIT_CONFIG (configuration / startup problem)
ARCHIVE_SERVICE_NAME = "SentinelArchive"   # sentinel.winsvc.commands.ARCHIVE_SERVICE_NAME (unit test)


@dataclass
class ScmCalls:
    """The SCM operations ServiceControl needs (real ones from :mod:`sentinel.winsvc.scm`)."""
    query: Callable[[str], Any]          # -> ServiceInfo | None, may raise OSError
    start: Callable[[str], int]          # -> 0 or Windows error code
    stop: Callable[[str], int]           # -> 0 or Windows error code


def real_calls() -> ScmCalls:
    from . import scm

    return ScmCalls(scm.query_service, scm.start_service, scm.stop_service)


class ServiceControl:
    """RecorderControl backed by the installed Windows service."""

    name = "Windows service"
    service_managed = True     # the launcher only views status; the service checks its own setup

    def __init__(self, api: ScmCalls | None = None, service_name: str = SERVICE_NAME,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 query_error: str | None = None, archive_enabled: bool | None = None,
                 archive_service_name: str = ARCHIVE_SERVICE_NAME) -> None:
        self.api = api or real_calls()
        self.service_name = service_name
        self._clock = clock
        self._sleep = sleep
        self.query_error = query_error      # set when the provider could not ask the SCM
        # [archive] enabled in the configuration (None = not known: act on the service if it is installed).
        self.archive_enabled = archive_enabled
        self.archive_service_name = archive_service_name

    def _state(self, name: str | None = None) -> int | None:
        name = name or self.service_name
        try:
            info = self.api.query(name)
        except OSError as exc:
            log.error("cannot query the %s service: %s", name, exc)
            return None
        return None if info is None else int(info.state)

    def is_running(self) -> bool:
        from .scm import SERVICE_RUNNING, SERVICE_START_PENDING

        return self._state() in (SERVICE_RUNNING, SERVICE_START_PENDING)

    def start(self, confirm_s: float = 20.0):  # noqa: ANN201 - StartResult (import cycle avoided)
        from ..launcher.control import StartResult
        from .scm import (ERROR_ACCESS_DENIED, ERROR_SERVICE_ALREADY_RUNNING, SERVICE_RUNNING, SERVICE_STOPPED)

        if self.query_error is not None:
            return StartResult(False, "C-003", f"service state unknown: {self.query_error}")
        err = self.api.start(self.service_name)
        if err == ERROR_ACCESS_DENIED:
            return StartResult(False, "C-003", "the recorder service is installed but this account may not "
                                               "start it (access denied); no background recorder was started")
        if err not in (0, ERROR_SERVICE_ALREADY_RUNNING):
            return StartResult(False, "C-003", f"StartService failed (Windows error {err})")
        end = self._clock() + confirm_s
        while True:
            try:
                info = self.api.query(self.service_name)
            except OSError as exc:
                return StartResult(True, None, f"start requested; state could not be read ({exc})")
            if info is not None and info.state == SERVICE_RUNNING:
                return StartResult(True, None, "recorder service running")
            if info is not None and info.state == SERVICE_STOPPED and err == 0:
                code = "C-001" if info.service_exit_code == EXIT_CONFIG else "C-003"
                return StartResult(False, code, f"service stopped again (exit {info.win32_exit_code}/"
                                                f"{info.service_exit_code})")
            if self._clock() >= end:
                return StartResult(True, None, f"start requested; not yet RUNNING after {confirm_s:.0f} s")
            self._sleep(0.5)

    def stop(self, timeout_s: float = 90.0) -> bool:
        from .scm import ERROR_SERVICE_NOT_ACTIVE, SERVICE_STOPPED

        err = self.api.stop(self.service_name)
        if err not in (0, ERROR_SERVICE_NOT_ACTIVE):
            log.error("could not stop the %s service (Windows error %d)", self.service_name, err)
            return False
        end = self._clock() + timeout_s
        while True:
            if self._state() == SERVICE_STOPPED:
                return True
            if self._clock() >= end:
                return False
            self._sleep(0.5)

    # ---- the archive-drive copy service (review 2026-11-12: nothing started it again after a clean stop)
    def start_archive(self) -> bool:
        """Start ``SentinelArchive`` when ``[archive] enabled``, it is installed and it is not running.
        Best effort (standard users may not start services: access denied is logged, not raised); the
        status shows A-007 while archive copying is not running. True when a start was accepted."""
        from .scm import ERROR_SERVICE_ALREADY_RUNNING, SERVICE_RUNNING, SERVICE_START_PENDING

        if self.archive_enabled is False or self.query_error is not None:
            return False
        state = self._state(self.archive_service_name)
        if state is None or state in (SERVICE_RUNNING, SERVICE_START_PENDING):
            return False
        err = self.api.start(self.archive_service_name)
        if err not in (0, ERROR_SERVICE_ALREADY_RUNNING):
            log.warning("could not start the %s service (Windows error %d)", self.archive_service_name, err)
            return False
        log.info("%s service start requested", self.archive_service_name)
        return err == 0

    def stop_archive(self, timeout_s: float = 180.0) -> bool:
        """Stop ``SentinelArchive`` (it is built to survive a stop mid-copy). True once it is stopped or
        when it is not installed."""
        from .scm import ERROR_SERVICE_NOT_ACTIVE, SERVICE_STOPPED

        state = self._state(self.archive_service_name)
        if state is None or state == SERVICE_STOPPED:
            return True
        err = self.api.stop(self.archive_service_name)
        if err not in (0, ERROR_SERVICE_NOT_ACTIVE):
            log.warning("could not stop the %s service (Windows error %d)", self.archive_service_name, err)
            return False
        end = self._clock() + timeout_s
        while self._state(self.archive_service_name) not in (SERVICE_STOPPED, None):
            if self._clock() >= end:
                log.warning("the %s service did not stop within %.0f s", self.archive_service_name, timeout_s)
                return False
            self._sleep(0.5)
        return True

    def restart_archive(self, timeout_s: float = 180.0) -> bool:
        """Stop and start ``SentinelArchive`` so it reads the saved settings again (it reads them once, at
        start). Call after a settings save, together with the recorder restart. True when it was started."""
        if not self.stop_archive(timeout_s):
            return False
        return self.start_archive()


def service_control_provider(config_path: Path, cfg: Any, api: ScmCalls | None = None,
                             platform: str | None = None) -> ServiceControl | None:
    """CONTROL_PROVIDERS entry: a ServiceControl when the service is installed (Windows only)."""
    if (platform or sys.platform) != "win32":
        return None
    try:
        calls = api or real_calls()
        info = calls.query(SERVICE_NAME)
    except OSError as exc:
        log.error("cannot ask Windows whether the recorder service is installed: %s", exc)
        return ServiceControl(api, query_error=str(exc))
    if info is None:
        return None
    archive = getattr(cfg, "archive", None)
    return ServiceControl(calls, archive_enabled=None if archive is None else bool(getattr(archive, "enabled", False)))
