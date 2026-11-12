"""Field report 2026-11-12: with the archive copier stopped (A-007), "Swap archive drive" waited its
full 10 minutes in silence and every other Drives-page button said "Wait until the current command
has finished". Swap now answers at once when nothing is copying, and leaves no request behind."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sentinel.archive import __main__ as arc_cli
from sentinel.archive.status import COPIER_NOT_RUNNING, HEARTBEAT_STALE_S, STATUS_FILE, copier_running
from sentinel.archive.worker import CONTROL_DIR, SWAP_REQUEST


def _status(data: Path, state: str, age_s: float = 0.0) -> None:
    data.mkdir(parents=True, exist_ok=True)
    (data / STATUS_FILE).write_text(json.dumps({"state": state, "heartbeat_epoch": time.time() - age_s}), "utf-8")


@pytest.fixture()
def cfg(tmp_path: Path, monkeypatch):  # noqa: ANN201
    c = SimpleNamespace(data_path=tmp_path / "data")
    c.data_path.mkdir()
    monkeypatch.setattr(arc_cli, "_load", lambda _a: c)
    return c


def test_copier_running_needs_a_fresh_heartbeat_and_not_stopped() -> None:
    now = time.time()
    assert copier_running({"state": "UP_TO_DATE", "heartbeat_epoch": now - 5}, now)
    assert not copier_running({}, now)
    assert not copier_running({"state": "UP_TO_DATE", "heartbeat_epoch": now - HEARTBEAT_STALE_S - 1}, now)
    assert not copier_running({"state": "STOPPED", "heartbeat_epoch": now}, now)
    assert not copier_running({"state": "UP_TO_DATE", "heartbeat_epoch": "garbage"}, now)


@pytest.mark.parametrize("setup", ["none", "stale", "stopped"])
def test_swap_answers_at_once_when_the_copier_is_not_running(cfg, capsys, setup: str) -> None:  # noqa: ANN001
    if setup == "stale":
        _status(cfg.data_path, "UP_TO_DATE", age_s=HEARTBEAT_STALE_S + 30)
    elif setup == "stopped":
        _status(cfg.data_path, "STOPPED")
    t0 = time.monotonic()
    assert arc_cli.main(["swap", "--config", "x.toml", "--timeout", "600"]) == 3
    assert time.monotonic() - t0 < 5
    assert COPIER_NOT_RUNNING in capsys.readouterr().out
    assert not (cfg.data_path / CONTROL_DIR / SWAP_REQUEST).exists()     # nothing waits for a later copier


def test_swap_stops_waiting_when_the_copier_stops_meanwhile(cfg, capsys) -> None:  # noqa: ANN001
    _status(cfg.data_path, "COPYING")
    t = threading.Timer(0.6, lambda: _status(cfg.data_path, "STOPPED"))
    t.start()
    try:
        t0 = time.monotonic()
        assert arc_cli.main(["swap", "--config", "x.toml", "--timeout", "60"]) == 3
        assert time.monotonic() - t0 < 10
    finally:
        t.cancel()
    out = capsys.readouterr().out
    assert "Preparing the archive drive for removal" in out
    assert "Archive copying stopped while the drive was being prepared" in out


def test_swap_still_waits_for_safe_to_remove_while_the_copier_runs(cfg, capsys) -> None:  # noqa: ANN001
    _status(cfg.data_path, "COPYING")

    def answer() -> None:
        d = cfg.data_path
        (d / STATUS_FILE).write_text(json.dumps({"state": "SAFE_TO_REMOVE", "heartbeat_epoch": time.time() + 1,
                                                 "message": "SAFE TO REMOVE DRIVE"}), "utf-8")
    t = threading.Timer(0.6, answer)
    t.start()
    try:
        assert arc_cli.main(["swap", "--config", "x.toml", "--timeout", "30"]) == 0
    finally:
        t.cancel()
    assert (cfg.data_path / CONTROL_DIR / SWAP_REQUEST).exists()
    assert "SAFE TO REMOVE DRIVE" in capsys.readouterr().out
