"""Time accuracy over long runs (requirement E1, docs/03 §4.2): segment times must track the
time the footage was received, even when the camera's clock runs fast or slow and the PC's
monotonic clock drifts against its (synchronised) wall clock.

Drift is exaggerated (1 %, i.e. 10 000 ppm, ~200x a typical camera) so that what would take
days at real rates shows in two minutes. The reference for "reception time" is each segment's
``observed_closed_utc``: the host wall clock when the recorder saw FFmpeg close the segment
(an independent measurement; the poll interval is shortened to 50 ms here so it lags the
closing packet's arrival by well under 0.5 s).

Segment 0 is excluded from that comparison: FFmpeg buffers the first second or so of input
while probing the stream and writes segment 0 in a burst afterwards, so its close is observed
about 1.1-1.2 s after its last packet arrived. Measured with NO drift (clock_rate 1.0):
end_utc - observed_closed_utc = -1.19 s for segment 0 and -0.18..-0.41 s (0.2 s poll) for the
rest. That is a limitation of the reference, not of the timestamps.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import DriftingClock, Harness, contiguous, wait_for
from sentinel.clock import parse_iso_utc
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]

TOL_S = 0.5


def _record(tmp_path: Path, clock_rate: float, seconds: float, clock: DriftingClock | None = None) -> Harness:
    cam = SimulatedCamera(fps=15, clock_rate=clock_rate).start()
    try:
        h = Harness(tmp_path, cam.url, clock=clock, service={"poll_interval_s": 0.05},
                    recording={"segment_seconds": 5, "max_clock_drift_ppm": 20000.0}).start()
        try:
            time.sleep(seconds)
        finally:
            h.stop()
    finally:
        cam.stop()
    return h


def _errors(h: Harness) -> tuple[list[float], list[float]]:
    """Per sealed segment after segment 0 (see module docstring):
    (new end_utc - reception, old single-anchor end - reception)."""
    run = h.runs()[0]
    anchor = parse_iso_utc(run["anchor_utc"])
    new, old = [], []
    for s in h.segments("FINALIZED")[1:]:
        seen = parse_iso_utc(s["observed_closed_utc"])
        new.append(parse_iso_utc(s["end_utc"]) - seen)
        old.append(anchor + s["stream_end_s"] - seen)
    return new, old


def _common_checks(h: Harness) -> list[dict]:
    runs = h.runs()
    assert len(runs) == 1, f"slow drift must not restart recording: {[r['end_reason'] for r in runs]}"
    assert runs[0]["end_reason"] == "graceful_stop"
    assert runs[0]["time_method"] == "reception_tracking_v1"
    assert not [g for g in h.gaps() if g["reason"] == "source_discontinuity_absorbed"], \
        "slow drift was mistaken for lost footage"
    assert not h.events("timeline_discontinuity") and not h.events("clock_step")
    assert h.gaps("within_segment") == []
    segs = h.segments()
    assert segs and all(s["state"] == "FINALIZED" for s in segs), [s["state"] for s in segs]
    for a, b in zip(segs, segs[1:]):
        assert contiguous(a, b, tol=0.002), f"{a['end_utc']} -> {b['start_utc']}"
        assert a["time_correction_end_s"] == b["time_correction_start_s"]
    for s in segs:
        assert "time_correction_limited" not in json.loads(s["warnings"])
        assert s["time_correction_limited"] == 0
    assert runs[0]["time_correction_limited_count"] == 0
    obs = h.q("SELECT * FROM clock_observations ORDER BY obs_id")
    assert obs and obs[0]["reason"] == "startup" and obs[0]["wall_minus_mono_s"] is not None
    return segs


def test_slow_camera_and_drifting_pc_clock_stay_on_reception_time(tmp_path: Path) -> None:
    # Camera clock 1 % slow; PC wall clock 0.5 % fast against its monotonic clock (Windows-like).
    clock = DriftingClock(ppm=5000.0)
    h = _record(tmp_path, clock_rate=0.99, seconds=150, clock=clock)
    segs = _common_checks(h)
    assert len(segs) >= 25
    new, old = _errors(h)
    worst = max(new, key=abs)
    assert abs(worst) <= TOL_S, f"segment end strayed {worst:+.3f} s from reception time: {new}"
    # The single-anchor scheme would have drifted: about -1.5 % of elapsed time by the end.
    assert old[-1] < -1.5, f"old scheme error only {old[-1]:+.3f} s"
    assert old[-1] < old[len(old) // 2] < old[1]
    run = h.runs()[0]
    assert run["camera_drift_ppm"] == pytest.approx(-10000, abs=1500)       # camera slow
    assert run["host_clock_drift_ppm"] == pytest.approx(5000, abs=300)      # wall fast vs monotonic
    assert run["time_correction_s"] == pytest.approx(0.015 * run["last_stream_time_s"], abs=0.5)


def test_absorbed_freeze_is_still_a_gap_under_drift(tmp_path: Path) -> None:
    # Like test_freeze_absorbed_by_ffmpeg_is_recorded_as_gap, but with a 1 % slow camera: the lost
    # 12 s must be an explicit gap, not a drift correction, and drift must not distort it.
    cam = SimulatedCamera(fps=15, clock_rate=0.99).start()
    try:
        h = Harness(tmp_path, cam.url, service={"poll_interval_s": 0.05},
                    recording={"stall_timeout_s": 15, "io_timeout_s": 30, "segment_seconds": 4,
                               "max_clock_drift_ppm": 20000.0}).start()
        try:
            time.sleep(40)                                   # let 0.4 s of drift accumulate
            cam.freeze()
            t_freeze = time.time()
            time.sleep(12)
            cam.resume()
            t_resume = time.time()
            wait_for(lambda: any(g["reason"] == "source_discontinuity_absorbed" for g in h.gaps()), 40,
                     what="absorbed-discontinuity gap row")
            n = h.sealed_count()
            h.wait_sealed(n + 2, timeout=40)
        finally:
            h.stop()
    finally:
        cam.stop()
    g = [x for x in h.gaps() if x["reason"] == "source_discontinuity_absorbed"]
    assert len(g) == 1
    assert g[0]["duration_s"] == pytest.approx(12.0, abs=2.0)
    assert parse_iso_utc(g[0]["start_utc"]) == pytest.approx(t_freeze, abs=1.5)
    assert parse_iso_utc(g[0]["end_utc"]) == pytest.approx(t_resume, abs=1.5)
    broken = h.runs()[0]
    assert broken["end_reason"] == "timeline_discontinuity"
    assert broken["time_correction_limited_count"] == 0, "the absorbed jump leaked into the drift correction"
    assert broken["time_correction_s"] == pytest.approx(0.01 * broken["timeline_break_stream_s"], abs=0.3)
    segs = [s for s in h.segments() if s["run_id"] == broken["run_id"]]
    for s in segs[1:]:
        if s["stream_end_s"] is not None and s["stream_end_s"] < broken["timeline_break_stream_s"] - 1:
            err = parse_iso_utc(s["end_utc"]) - parse_iso_utc(s["observed_closed_utc"])
            assert abs(err) <= TOL_S, (s["seq"], err)
    starts = [parse_iso_utc(s["start_utc"]) for s in h.segments()]
    assert starts == sorted(starts)


def test_fast_camera_stays_on_reception_time(tmp_path: Path) -> None:
    h = _record(tmp_path, clock_rate=1.01, seconds=100)
    segs = _common_checks(h)
    assert len(segs) >= 15
    new, old = _errors(h)
    worst = max(new, key=abs)
    assert abs(worst) <= TOL_S, f"segment end strayed {worst:+.3f} s from reception time: {new}"
    assert old[-1] > 0.8, f"old scheme error only {old[-1]:+.3f} s"
    run = h.runs()[0]
    assert run["camera_drift_ppm"] == pytest.approx(10000, abs=1500)        # camera fast
    assert abs(run["host_clock_drift_ppm"] or 0.0) < 300                    # real clocks: no exaggerated drift
