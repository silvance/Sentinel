from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.db import IllegalTransition, Ledger
from sentinel.models import SegmentState


@pytest.fixture()
def ledger(tmp_path: Path) -> Ledger:
    led = Ledger(tmp_path / "s.db")
    for rid in ("R1", "R2", "R3"):
        led.create_run(run_id=rid, camera_id="CAM01", directory=f"CAM01/{rid}", launched_utc="2026-11-09T00:00:00.000000Z",
                       launch_mono=0.0, display_timezone="UTC", app_version="test")
    yield led
    led.close()


def _seg(led: Ledger, run: str = "R1", seq: int = 0) -> str:
    return led.insert_segment(camera_id="CAM01", run_id=run, seq=seq, rel_path=f"CAM01/{run}/seg_{seq:06d}.ts")


def test_pragmas_are_durable(ledger: Ledger) -> None:
    assert ledger.query("PRAGMA journal_mode")[0][0] == "wal"
    assert ledger.integrity_check() == "ok"


def test_legal_transitions(ledger: Ledger) -> None:
    a = _seg(ledger)
    ledger.transition(a, SegmentState.CLOSED)
    ledger.transition(a, SegmentState.FINALIZED, sha256="00" * 32)
    b = _seg(ledger, seq=1)
    ledger.transition(b, SegmentState.INTERRUPTED)
    assert ledger.get_segment(b)["state"] == "INTERRUPTED"


@pytest.mark.parametrize("path", [
    [SegmentState.FINALIZED],                                 # skip CLOSED
    [SegmentState.CLOSED, SegmentState.INTERRUPTED],          # closed segments are not "interrupted"
    [SegmentState.INTERRUPTED, SegmentState.FINALIZED],       # terminal states are final
    [SegmentState.CLOSED, SegmentState.FINALIZED, SegmentState.CORRUPT],
])
def test_illegal_transitions_rejected(ledger: Ledger, path: list[SegmentState]) -> None:
    s = _seg(ledger)
    with pytest.raises(IllegalTransition):
        for st in path:
            ledger.transition(s, st)


def test_sealed_integrity_fields_are_immutable(ledger: Ledger) -> None:
    s = _seg(ledger)
    ledger.transition(s, SegmentState.INTERRUPTED, sha256="ab" * 32, size_bytes=10)
    with pytest.raises(IllegalTransition):
        ledger.update_segment(s, sha256="cd" * 32)
    with pytest.raises(IllegalTransition):
        ledger.update_segment(s, size_bytes=11)
    with pytest.raises(ValueError):
        ledger.update_segment(s, state="ACTIVE")
    ledger.update_segment(s, clock_anomaly=1)                 # non-integrity metadata may change


def test_rel_path_unique(ledger: Ledger) -> None:
    _seg(ledger)
    with pytest.raises(Exception):
        _seg(ledger)


def test_gap_lifecycle(ledger: Ledger) -> None:
    g = ledger.begin_gap("CAM01", "2026-11-09T00:00:10.000000Z", "stall", None, "R1", "segment_end")
    # A second failed run while the gap is open bumps attempts, keeps start and reason.
    assert ledger.begin_gap("CAM01", "2026-11-09T00:00:20.000000Z", "connect_timeout", None, "R2", "x") == g
    row = ledger.open_gap("CAM01")
    assert row["attempts"] == 2 and row["reason"] == "stall" and row["ended_run_id"] == "R2"
    # A segment from the run that ended (R2) must not close its own gap.
    assert ledger.close_gap("CAM01", "2026-11-09T00:00:30.000000Z", "seg", "R2") is None
    closed = ledger.close_gap("CAM01", "2026-11-09T00:00:30.000000Z", "seg", "R3")
    assert closed["duration_s"] == pytest.approx(20.0) and closed["clock_anomaly"] == 0
    assert ledger.open_gap("CAM01") is None


def test_gap_with_backwards_time_flags_clock_anomaly(ledger: Ledger) -> None:
    ledger.begin_gap("CAM01", "2026-11-09T00:10:00.000000Z", "ffmpeg_error", None, "R1", "segment_end")
    closed = ledger.close_gap("CAM01", "2026-11-09T00:05:00.000000Z", "seg", "R2")
    assert closed["clock_anomaly"] == 1 and closed["duration_s"] < 0


def test_readonly_connection_cannot_write(ledger: Ledger, tmp_path: Path) -> None:
    ro = Ledger(tmp_path / "s.db", readonly=True)
    try:
        assert len(ro.query("SELECT * FROM recording_runs")) == 3
        with pytest.raises(Exception):
            ro.add_event("info", "x", "y", "z")
    finally:
        ro.close()
