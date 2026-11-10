"""Watchdog and Windows-service host with the real recorder, real FFmpeg and the simulated camera.

The Service Control Manager itself is replaced by a fake (registering a real service needs
administrator rights): these tests cover everything in-process — ServiceMain, the pre-shutdown
control reaching RecorderService.request_stop, FFmpeg's graceful stop, and the exit-code mapping.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import requires_ffmpeg
from harness import Harness, wait_for
from sentinel.config import AppConfig
from sentinel.platform import get_platform
from sentinel.service import EXIT_WATCHDOG, RecorderService
from sentinel.sim.camera import SimulatedCamera
from sentinel.winsvc.scm import (NO_ERROR, SERVICE_CONTROL_PRESHUTDOWN, SERVICE_RUNNING, SERVICE_STOP_PENDING,
                                 SERVICE_STOPPED, ServiceHost, ServiceStatus)

pytestmark = [pytest.mark.integration, requires_ffmpeg]


def test_watchdog_fires_on_hung_recording_loop_and_stops_ffmpeg_gracefully(tmp_path: Path) -> None:
    with SimulatedCamera(fps=15) as cam:
        h = Harness(tmp_path, cam.url, service={"watchdog_timeout_s": 5})
        exits: list[int] = []
        fired = threading.Event()
        h.svc._watchdog_exit = lambda code: (exits.append(code), fired.set())  # noqa: SLF001
        h.start()
        release = threading.Event()
        try:
            h.wait_sealed(2, timeout=40)
            sup = h.svc.supervisor
            assert sup is not None and sup.current_proc is not None
            proc = sup.current_proc
            sup._tick = lambda: release.wait(60)  # noqa: SLF001 - the recording loop hangs inside its tick
            assert fired.wait(30), "watchdog did not fire"
            assert exits == [EXIT_WATCHDOG]
            st = h.status()
            assert st["overall"] == "ERROR" and st["alert"]["code"] == "R-006"
            assert h.events("watchdog_restart")
            # FFmpeg was asked to finish ('q') so the open segment ends cleanly before the restart.
            wait_for(lambda: proc.poll() is not None, 15, what="FFmpeg exit after watchdog q")
            assert list(h.cfg.log_path.glob("watchdog-*.txt"))
        finally:
            release.set()
            h.stop()


class _ThreadedFakeScm:
    def __init__(self) -> None:
        self.statuses: list[ServiceStatus] = []
        self.handler: Callable[[int, int], int] | None = None
        self.thread: threading.Thread | None = None

    def run_dispatcher(self, name: str, service_main: Callable[[], None]) -> None:
        self.thread = threading.Thread(target=service_main, name="fake-scm-servicemain", daemon=True)
        self.thread.start()

    def register_handler(self, name: str, handler: Callable[[int, int], int]) -> Any:
        self.handler = handler
        return 1

    def set_status(self, handle: Any, status: ServiceStatus) -> None:
        self.statuses.append(status)


def _write_config(tmp: Path, url: str) -> Path:
    cfg = tmp / "sentinel.toml"
    cfg.write_text(f"""
[camera]
id = "CAM01"
url = "{url}"
input_format = "mpegts"
development_source = true

[recording]
segment_seconds = 2
align_to_clock = false
connect_timeout_s = 10
stall_timeout_s = 6
io_timeout_s = 4
backoff_initial_s = 0.5
backoff_max_s = 2

[storage]
spool_dir = "spool"
data_dir = "data"
log_dir = "logs"
warning_free_gb = 0
critical_free_gb = 0

[service]
status_interval_s = 0.5
poll_interval_s = 0.2
""", encoding="utf-8")
    return cfg


def test_service_host_preshutdown_stops_recorder_gracefully(tmp_path: Path) -> None:
    from sentinel.winsvc.__main__ import run_recorder_in_host

    root = logging.getLogger()
    saved = list(root.handlers), root.level

    def factory(cfg: AppConfig) -> RecorderService:
        p = get_platform()
        p.clock_synchronized = lambda: None  # type: ignore[method-assign]  # host NTP not under test
        return RecorderService(cfg, platform=p)

    try:
        with SimulatedCamera(fps=15) as cam:
            cfg_path = _write_config(tmp_path, cam.url)
            api = _ThreadedFakeScm()
            host = ServiceHost("SentinelRecorder", lambda h: run_recorder_in_host(h, str(cfg_path), factory), api,
                               checkpoint_interval_s=0.5)
            host.dispatch()
            data = tmp_path / "data"
            wait_for(lambda: any(s.state == SERVICE_RUNNING for s in api.statuses), 20, what="RUNNING reported")
            wait_for(lambda: (data / "status.json").exists()
                     and json.loads((data / "status.json").read_text("utf-8")).get("last_segment"), 45,
                     what="first sealed segment")
            assert api.handler is not None
            assert api.handler(SERVICE_CONTROL_PRESHUTDOWN, 0) == NO_ERROR      # Windows is shutting down
            assert api.thread is not None
            api.thread.join(120)
            assert not api.thread.is_alive(), "service did not stop"
        states = [s.state for s in api.statuses]
        assert SERVICE_STOP_PENDING in states and states[-1] == SERVICE_STOPPED
        assert api.statuses[-1].win32_exit_code == NO_ERROR and host.exit_code == 0
        st = json.loads((data / "status.json").read_text("utf-8"))
        assert st["overall"] == "STOPPED"
        from sentinel.db import Ledger

        led = Ledger(data / "sentinel.db", readonly=True)
        try:
            last = led.query("SELECT state FROM segments ORDER BY rowid DESC LIMIT 1")[0]
            assert last["state"] == "FINALIZED", "pre-shutdown must let FFmpeg close the final segment cleanly"
            assert not led.query("SELECT 1 FROM segments WHERE state IN ('ACTIVE','CLOSED')")
        finally:
            led.close()
        assert (tmp_path / "logs" / "recorder.log").exists()
    finally:
        for hd in list(root.handlers):
            root.removeHandler(hd)
            hd.close()
        for hd in saved[0]:
            root.addHandler(hd)
        root.setLevel(saved[1])
