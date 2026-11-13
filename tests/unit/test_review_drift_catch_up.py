"""First review run, 2026-11-12: after a stream freeze, a partial burst catch-up (the camera sends the few
seconds it had buffered, the rest is lost) left the "not catching up" test comparing against the lag of the
FIRST late report for ever: the timeline break was never recorded and every segment of the run was held
unsealed. The loss must be recognised once the catch-up has stopped."""

from __future__ import annotations

import types
from collections.abc import Iterator

from sentinel.recorder.supervisor import LiveState
from sentinel.recorder.supervisor import RecordingSupervisor as Supervisor
from sentinel.recorder.timebase import DriftTracker, RunTimebase

PERSIST_S = 5.0


def _feed(reports: list[tuple[float, float]]) -> tuple[LiveState, types.SimpleNamespace]:
    clock = types.SimpleNamespace(t=1000.0)
    clock.mono = lambda: clock.t
    live = LiveState()

    def lines() -> Iterator[bytes]:
        for mono, out in reports:
            clock.t = mono
            yield f"out_time_us={int(out * 1e6)}\n".encode()
            yield b"progress=continue\n"

    cfg = types.SimpleNamespace(recording=types.SimpleNamespace(timeline_drift_tolerance_s=3.0))
    fake = types.SimpleNamespace(cfg=cfg, clock=clock, live=live)
    Supervisor._read_progress(fake, types.SimpleNamespace(stdout=lines()), RunTimebase(0.0, 1000.0),  # type: ignore[arg-type]
                              DriftTracker(100.0))
    return live, clock


def _fires(live: LiveState, now: float) -> bool:
    """The supervisor's timeline-discontinuity condition."""
    return (live.drift_since_mono is not None and now - live.drift_since_mono >= PERSIST_S
            and live.lag_s >= live.drift_lag_at_start - 0.5 and live.drift_break_stream_s is not None
            and (live.drift_low_mono is None or now - live.drift_low_mono >= PERSIST_S))


def test_a_partial_burst_catch_up_after_a_freeze_is_recorded_as_missing_time() -> None:
    reports = [(1000.5 + i, float(i)) for i in range(1, 60)]      # live, lag 0.5 s
    reports.append((1071.5, 59.04))                               # 12 s freeze, first late report
    t, s = 1072.5, 62.5                                           # 3 s burst, then real time again
    for _ in range(30):
        reports.append((t, s))
        t, s = t + 1, s + 1
    live, clock = _feed(reports)
    assert live.drift_since_mono is not None and live.drift_break_stream_s is not None
    assert _fires(live, clock.t)                                  # was False for ever before the fix


def test_still_catching_up_is_not_yet_a_break() -> None:
    reports = [(1000.5 + i, float(i)) for i in range(1, 60)]
    reports.append((1071.5, 59.04))
    t, s = 1072.5, 60.5
    for _ in range(5):                                            # catching up 1.5 s per second
        reports.append((t, s))
        t, s = t + 1, s + 2.5
    live, clock = _feed(reports)
    assert live.drift_since_mono is not None
    assert not _fires(live, clock.t)
