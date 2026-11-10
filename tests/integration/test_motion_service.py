"""Motion-triggered recording end to end (owner request 2026-11-10: record all the time in working
hours, outside them only on motion, for a set time after the last motion; each new motion restarts
the timer). The real recorder with the simulated camera showing a still scene in which a box
moves only during set windows of stream time."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import Harness, wait_for
from sentinel.clock import parse_iso_utc
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]

MOTION_ALWAYS = {"enabled": True, "timezone": "UTC", "default": "motion"}
KEEP = 10.0


def test_records_only_around_motion_and_restarts_the_timer(tmp_path: Path) -> None:
    # Motion 20-26 s, a pause shorter than the timer, motion again 30-33 s: ONE recording that
    # lasts until ~33 + KEEP. Then nothing until the end.
    with SimulatedCamera(fps=15, motion=[(20, 26), (30, 33)]) as cam:
        t0 = time.time()
        h = Harness(tmp_path, cam.url, schedule=MOTION_ALWAYS,
                    motion={"keep_recording_s": KEEP, "min_frames": 2}).start()
        try:
            st = wait_for(lambda: (s := h.status()).get("overall") == "WATCHING" and s, 30, what="WATCHING")
            assert [a["code"] for a in st["alerts"] if a["code"] != "Z-001"] == ["P-003"]
            assert h.runs() == []                                     # still scene: nothing recorded
            wait_for(lambda: len(h.runs()) == 1, 40, what="recording started by motion")
            started = time.time() - t0
            assert 20.0 <= started <= 28.0, started
            wait_for(lambda: h.runs() and h.runs()[0]["end_reason"] == "motion_idle", 60, what="recording stopped")
            stopped = time.time() - t0
            # Timer restarted by the second motion: stops ~KEEP after 33 s, not ~KEEP after 26 s.
            assert 33.0 + KEEP - 1.0 <= stopped <= 33.0 + KEEP + 8.0, stopped
            wait_for(lambda: h.status().get("overall") == "WATCHING", 20, what="WATCHING again")
            time.sleep(4.0)
            assert len(h.runs()) == 1
        finally:
            h.stop()
    runs = h.runs()
    assert len(runs) == 1 and runs[0]["end_reason"] == "motion_idle"
    segs = [s for s in h.segments() if s["run_id"] == runs[0]["run_id"]]
    assert segs and {s["state"] for s in segs} == {"FINALIZED"}             # closed normally, sealed
    rec = h.events("motion_record")
    assert len(rec) == 1 and json.loads(rec[0]["detail"])["why"] == "motion_detected"
    assert len(h.events("motion_standby")) >= 2                            # at start, and after the run
    gaps = [g for g in h.gaps() if g["reason"] == "motion_idle"]
    assert len(gaps) == 1 and gaps[0]["end_utc"] is None                    # open: still waiting at stop
    assert parse_iso_utc(gaps[0]["start_utc"]) >= parse_iso_utc(runs[0]["launched_utc"])
    assert not [e for e in h.events() if e["severity"] == "error"]


def test_working_hours_record_without_motion(tmp_path: Path) -> None:
    """A 'record' period (working hours) records the still scene; motion rules do not apply."""
    now = time.time()
    hours = {**MOTION_ALWAYS, "dates": [{"action": "record",
                                          "start": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now - 3600)),
                                          "end": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + 3600))}]}
    with SimulatedCamera(fps=15, motion=[]) as cam:
        h = Harness(tmp_path, cam.url, schedule=hours, motion={"keep_recording_s": KEEP}).start()
        try:
            h.wait_sealed(2, timeout=40)
            assert h.status()["overall"] == "RECORDING"
        finally:
            h.stop()
    assert {r["end_reason"] for r in h.runs()} == {"graceful_stop"}
