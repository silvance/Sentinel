"""End to end: the real recorder and the real FFmpeg against a camera whose time labels leap ahead (0.3.28).

``sentinel.sim.leap_camera`` reproduces what reached the recorder from the KTNC camera on site (field report
2026-11-20): a few seconds after the connection starts the timestamps leap forward by an hour or more, once,
with no picture missing. Until 0.3.28 the first seconds of such a recording were logged that much before the
recorder had been started, and a gap was logged that never happened."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import requires_ffmpeg
from harness import Harness, wait_for

from sentinel.clock import parse_iso_utc
from sentinel.recorder.timebase import RELABEL_EVENT
from sentinel.sim.leap_camera import LeapingCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]
LEAP_S = 11984.0                # the leap in the laptop's recording of 20 Nov 03:07 UTC


def _recorded(h: Harness, seconds: float) -> None:
    wait_for(lambda: h.status().get("overall") == "RECORDING", 30, what="RECORDING status")
    time.sleep(seconds)


def _checks(h: Harness, t0: float, t1: float) -> list[dict]:
    """What must hold whatever file the leap fell into."""
    segs = h.segments()
    assert segs and all(s["state"] == "FINALIZED" for s in segs), [(s["state"], s["warnings"]) for s in segs]
    runs = h.runs()
    assert len(runs) == 1, "the leap must not end the run"
    launched = parse_iso_utc(runs[0]["launched_utc"])
    # Nothing is timed before the recorder was started, or after it was stopped.
    assert launched - 0.5 <= parse_iso_utc(runs[0]["anchor_utc"]) <= launched + 8.0, runs[0]["anchor_utc"]
    for s in segs:
        assert t0 - 0.5 <= parse_iso_utc(s["start_utc"]) <= parse_iso_utc(s["end_utc"]) <= t1 + 1.0, s
        assert s["duration_s"] < 60.0, s["duration_s"]           # the real length, not an hour and more
        assert "time_from_file_mtime" not in json.loads(s["warnings"])
    covered = parse_iso_utc(segs[-1]["end_utc"]) - parse_iso_utc(segs[0]["start_utc"])
    assert covered == pytest.approx(sum(s["duration_s"] for s in segs), abs=1.0)
    # No gap inside or between the files: no time passed at the leap.
    assert h.gaps("within_segment") == []
    for a, b in zip(segs, segs[1:]):
        assert abs(parse_iso_utc(b["start_utc"]) - parse_iso_utc(a["end_utc"])) <= 0.5, (a["end_utc"], b["start_utc"])
    between = h.gaps("between_runs")
    assert len(between) == 1 and between[0]["reason"] == "graceful_stop" and between[0]["start_utc"] == segs[-1]["end_utc"]
    # The leap is in the log, once, with its size; and the run's stream time was kept on one timeline.
    ev = [json.loads(e["detail"]) for e in h.events(RELABEL_EVENT)]
    assert len(ev) == 1 and ev[0]["run_id"] == runs[0]["run_id"]
    assert ev[0]["labels_ahead_s"] == pytest.approx(LEAP_S, abs=1.0)
    assert runs[0]["last_stream_time_s"] < 60.0
    assert [e for e in h.events() if e["code"] in ("timeline_discontinuity", "clock_step", "clock_slew")] == []
    return segs


def test_a_leap_inside_one_file(tmp_path: Path) -> None:
    """The set-up in use: files are cut by the clock, so the leap stays inside the file it happens in."""
    with LeapingCamera(leap_after_s=5.4, leap_s=LEAP_S) as cam:
        t0 = time.time()
        h = Harness(tmp_path, cam.url, camera={"input_format": "nut"},
                    recording={"segment_seconds": 3600, "align_to_clock": True}).start()
        try:
            _recorded(h, 11.0)
        finally:
            h.stop()
        t1 = time.time()
    segs = _checks(h, t0, t1)
    holding = [s for s in segs if "camera_time_jump" in json.loads(s["warnings"])]
    assert len(holding) == 1, [s["warnings"] for s in segs]
    s = holding[0]
    assert "internal_timestamp_gap" in json.loads(s["warnings"])            # the file keeps what the camera sent
    detail = json.loads(s["validation_detail"])
    assert detail["duration_s"] > LEAP_S and len(detail["pts_gaps"]) == 1   # by the file's own labels
    assert s["duration_s"] == pytest.approx(detail["duration_s"] - LEAP_S, abs=0.5)
    h.assert_sealed_integrity(s)


def test_a_leap_where_a_file_is_cut(tmp_path: Path) -> None:
    """Files cut by stream time: FFmpeg starts a new file at the leap. Both files and those after keep their
    true times and follow one another."""
    with LeapingCamera(leap_after_s=5.0, leap_s=LEAP_S) as cam:           # at a key picture, where files are cut
        t0 = time.time()
        h = Harness(tmp_path, cam.url, camera={"input_format": "nut"},
                    recording={"segment_seconds": 3, "align_to_clock": False}).start()
        try:
            _recorded(h, 12.0)
        finally:
            h.stop()
        t1 = time.time()
    segs = _checks(h, t0, t1)
    assert len(segs) >= 3
    for s in segs:
        h.assert_sealed_integrity(s)
