"""Internal recording-loop watchdog (cross-platform)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from conftest import make_config
from sentinel.config import ConfigError
from sentinel.db import Ledger
from sentinel.service import EXIT_WATCHDOG, RecorderService
from sentinel.watchdog import LoopWatchdog


class FakeMono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_fires_once_when_beats_stop() -> None:
    mono = FakeMono()
    fired: list[float] = []
    wd = LoopWatchdog(120.0, fired.append, mono=mono)
    wd._last = mono()
    for _ in range(10):            # beating every 60 s keeps it quiet
        mono.t += 60
        wd.beat()
        assert wd.check() is False
    mono.t += 119.0
    assert wd.check() is False
    mono.t += 2.0
    assert wd.check() is True and fired == [pytest.approx(121.0)]
    mono.t += 500
    assert wd.check() is False and len(fired) == 1


def test_disarmed_watchdog_never_fires() -> None:
    mono = FakeMono()
    fired: list[float] = []
    wd = LoopWatchdog(10.0, fired.append, mono=mono)
    wd.disarm()
    wd.start()                     # one-shot: stays disarmed
    mono.t += 1000
    assert wd.check() is False and fired == []


def test_thread_detects_hang_in_real_time() -> None:
    ev = threading.Event()
    wd = LoopWatchdog(0.3, lambda age: ev.set(), check_interval_s=0.05)
    wd.start()
    try:
        assert ev.wait(3.0)
    finally:
        wd.stop()


def test_config_rejects_tiny_watchdog(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        make_config(tmp_path, service={"watchdog_timeout_s": 1})
    assert make_config(tmp_path).service.watchdog_timeout_s == 120.0


def test_watchdog_expiry_records_status_event_threads_and_exits_5(tmp_path: Path) -> None:
    """Simulated hung supervisor loop: _tick stops being called while the service is 'running'."""
    exits: list[int] = []
    cfg = make_config(tmp_path, service={"watchdog_timeout_s": 5})
    svc = RecorderService(cfg, watchdog_exit=exits.append)
    cfg.data_path.mkdir(parents=True)
    svc.ledger = Ledger(cfg.data_path / "sentinel.db")
    try:
        svc.watchdog = LoopWatchdog(0.5, svc._on_watchdog_expired, check_interval_s=0.05)
        svc.watchdog.start()
        svc._tick = lambda: None   # noqa: SLF001 - nothing beats: the loop is "hung"
        deadline = time.monotonic() + 10
        while not exits and time.monotonic() < deadline:
            time.sleep(0.05)
        assert exits == [EXIT_WATCHDOG] == [5]
        st = json.loads((cfg.data_path / "status.json").read_text("utf-8"))
        assert st["overall"] == "ERROR" and st["alert"]["code"] == "R-006" and st["banner"] is True
        assert "Recording stopped. Error R-006" in st["alert"]["message"]
        ev = svc.ledger.query("SELECT * FROM events WHERE code='watchdog_restart'")
        assert len(ev) == 1 and json.loads(ev[0]["detail"])["exit_code"] == 5
        dumps = list(cfg.log_path.glob("watchdog-*.txt"))
        assert dumps and "Thread" in dumps[0].read_text("utf-8")
    finally:
        svc.watchdog.stop()
        svc.ledger.close()


def test_watchdog_exits_even_if_the_ledger_is_deadlocked(tmp_path: Path) -> None:
    exits: list[int] = []
    cfg = make_config(tmp_path)
    svc = RecorderService(cfg, watchdog_exit=exits.append)
    cfg.data_path.mkdir(parents=True)
    svc.ledger = Ledger(cfg.data_path / "sentinel.db")
    held = threading.Event()
    release = threading.Event()

    def hog() -> None:          # the "hung supervisor" holds the ledger lock forever
        with svc.ledger._lock:  # noqa: SLF001
            held.set()
            release.wait(30)
    t = threading.Thread(target=hog, daemon=True)
    t.start()
    held.wait(5)
    import sentinel.service as service_mod

    old = service_mod.WATCHDOG_STEP_TIMEOUT_S
    service_mod.WATCHDOG_STEP_TIMEOUT_S = 0.5
    try:
        t0 = time.monotonic()
        svc._on_watchdog_expired(130.0)   # noqa: SLF001
        assert exits == [5] and time.monotonic() - t0 < 10
        assert json.loads((cfg.data_path / "status.json").read_text("utf-8"))["alert"]["code"] == "R-006"
    finally:
        service_mod.WATCHDOG_STEP_TIMEOUT_S = old
        release.set()
        t.join(5)
        svc.ledger.close()
