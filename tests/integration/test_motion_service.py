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


def test_motion_markers_reach_the_review_drive(tmp_path: Path) -> None:
    """Owner request: show reviewers when there is motion in a recording. Continuous recording with
    markers on; motion at 20-26 s and 40-44 s of the camera's stream; export; the manifest and the
    summary list both, as time into the video."""
    from sentinel.db import Ledger
    from sentinel.export.engine import ReviewExporter
    from sentinel.export.target import ExportTarget
    from sentinel.gui.ledger_view import load_timeline
    from sentinel.media.probe import ffmpeg_version
    from sentinel.platform import get_platform
    from sentinel.recorder.integrity import IntegrityWorker

    with SimulatedCamera(fps=15, motion=[(20, 26), (40, 44)]) as cam:
        t0 = time.time()
        h = Harness(tmp_path, cam.url, motion={"markers": True},
                    recording={"display_timezone": "America/New_York"}).start()
        try:
            wait_for(lambda: time.time() - t0 > 58, 70, what="recording through both motions")
        finally:
            h.stop()
    periods = [json.loads(e["detail"]) for e in h.events("motion_period")]
    assert len(periods) == 2, periods
    for p, (a, b) in zip(periods, [(20, 26), (40, 44)]):
        start, end = parse_iso_utc(p["start_utc"]), parse_iso_utc(p["end_utc"])
        assert t0 + a - 1.0 <= start <= t0 + a + 4.0, (a, start - t0)          # key frames: ~1 s apart
        assert t0 + b - 3.0 <= end <= t0 + b + 3.0, (b, end - t0)
        assert p["peak_changed_percent"] > 1.0
    assert {r["end_reason"] for r in h.runs()} == {"graceful_stop"}             # markers never stop recording

    led = Ledger(h.cfg.data_path / "sentinel.db")
    try:
        w = IntegrityWorker(h.cfg, led, get_platform(), ffmpeg_version(h.cfg.tools.ffmpeg))
        for s in led.segments_needing_content_hash():
            w.hash_segment(s["segment_id"])
        root = tmp_path / "drive"
        root.mkdir()
        res = ReviewExporter(h.cfg, ExportTarget(root, {"drive_uuid": "t-1", "label": "REVIEW"}), ledger=led,
                             allow_same_volume=True, operator_id="op").run()
        assert res.state == "COMPLETE", res
    finally:
        led.close()
    man = json.loads(next(root.glob("Manifests/*/Export_Manifest.json")).read_text("utf-8"))
    assert len(man["motion"]) == 2 and "approximate" in man["motion_note"]
    videos = {d["path"]: d for d in man["derivatives"]}
    for m, p in zip(man["motion"], periods):
        assert m["start_utc"] == p["start_utc"] and m["in_videos"]
        w0 = m["in_videos"][0]
        v = videos[w0["video"]]
        assert w0["from_s"] == pytest.approx(parse_iso_utc(p["start_utc"]) - parse_iso_utc(v["start_utc"]), abs=0.1)
        assert v["motion"] and w0["to_s"] >= w0["from_s"]
    html = next(root.glob("Reports/*.html")).read_text("utf-8")
    assert "Motion (time into the video)" in html and "0:00:" in html and "approximate" in html

    tl = load_timeline(h.cfg.data_path / "sentinel.db", h.cfg.camera.id, time.time())
    assert len(tl.motion) == 2
