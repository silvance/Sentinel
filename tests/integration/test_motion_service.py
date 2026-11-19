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


def test_no_motion_for_too_long_records_all_the_time_until_motion_is_seen(tmp_path: Path) -> None:
    """Owner request 2026-11-18: a detector that gets pictures but sees no motion for the set time may no
    longer notice movement, so the recorder records all the time (W-003) until motion is seen again.
    Still scene; the limit is 15 s of watching (0.00018 days), every day counts; a box moves at 50-55 s."""
    quiet_days = 15.0 / 86400.0
    with SimulatedCamera(fps=15, motion=[(50, 55)]) as cam:
        t0 = time.time()
        h = Harness(tmp_path, cam.url, schedule=MOTION_ALWAYS,
                    motion={"keep_recording_s": KEEP, "min_frames": 2, "no_motion_days": quiet_days,
                            "no_motion_count_days": ["daily"]}).start()
        try:
            st = wait_for(lambda: (s := h.status()).get("overall") == "WATCHING" and s, 30, what="WATCHING")
            assert st["motion"]["quiet"]["active"] is False and "allowed" in st["motion"]["quiet"]["text"]
            assert h.runs() == []                                     # still scene: nothing recorded yet
            wait_for(lambda: len(h.runs()) == 1, 40, what="recording started by the fail safe")
            started = time.time() - t0
            assert 14.0 <= started <= 30.0, started                   # after ~15 s of watching, well before 50 s
            st = wait_for(lambda: (s := h.status()).get("overall") == "RECORDING" and s, 30, what="RECORDING")
            assert "W-003" in [a["code"] for a in st["alerts"]] and st["motion"]["quiet"]["active"] is True
            assert st["motion"]["quiet"]["text"].startswith("ON: no motion seen in")
            assert time.time() - t0 < 48.0, "the fail safe must be recording before the box moves"
            # The box moves: the detector works after all. Back to recording on motion only.
            wait_for(lambda: h.events("motion_quiet_ended"), 40, what="fail safe switched off by motion")
            ended = time.time() - t0
            assert 49.0 <= ended <= 62.0, ended
            wait_for(lambda: h.runs()[0]["end_reason"] == "motion_idle", 40, what="recording stopped after the motion")
            stopped = time.time() - t0
            assert 55.0 + KEEP - 1.0 <= stopped <= 55.0 + KEEP + 8.0, stopped
            st = wait_for(lambda: (s := h.status()).get("overall") == "WATCHING" and s, 20, what="WATCHING again")
            assert "W-003" not in [a["code"] for a in st["alerts"]] and st["motion"]["quiet"]["active"] is False
        finally:
            h.stop()
    runs = h.runs()
    assert len(runs) == 1 and runs[0]["end_reason"] == "motion_idle"         # one recording, closed normally
    segs = [s for s in h.segments() if s["run_id"] == runs[0]["run_id"]]
    assert segs and {s["state"] for s in segs} == {"FINALIZED"}
    rec = h.events("motion_record")
    assert len(rec) == 1 and json.loads(rec[0]["detail"])["why"] == "no_motion_fail_safe"
    on = h.events("motion_quiet_fail_safe")
    assert len(on) == 1 and on[0]["severity"] == "warning"
    detail = json.loads(on[0]["detail"])
    assert detail["count_days"] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert 15.0 <= detail["watched_without_motion_s"] <= 20.0 and detail["last_motion_utc"] is None
    off = json.loads(h.events("motion_quiet_ended")[0]["detail"])
    assert t0 + 49.0 <= parse_iso_utc(off["last_motion_utc"]) <= t0 + 60.0
    assert not [e for e in h.events() if e["severity"] == "error"]
    # The count is kept for the next start of the recorder.
    saved = json.loads((h.cfg.data_path / "motion_quiet.json").read_text("utf-8"))
    assert saved["format"] == "sentinel-motion-quiet" and saved["active"] is False


def test_the_room_light_being_switched_starts_a_recording_at_once(tmp_path: Path) -> None:
    """Owner request 2026-11-18 (0.3.22): the room has one light switch, so the light going on or off means
    someone is there. With [motion] light_aware the change of the whole picture is motion at once. Still scene,
    nothing moves; the light goes on at 20 s and off at 45 s of the camera's stream. Each is one changed
    picture, which "2 frames in a row" alone does not count as motion."""
    with SimulatedCamera(fps=15, motion=[], light=[(20, 45)]) as cam:
        t0 = time.time()
        h = Harness(tmp_path, cam.url, schedule=MOTION_ALWAYS,
                    motion={"keep_recording_s": KEEP, "min_frames": 2, "light_aware": True}).start()
        try:
            wait_for(lambda: h.status().get("overall") == "WATCHING", 30, what="WATCHING")
            assert h.runs() == []                                     # still scene, light off: nothing recorded
            wait_for(lambda: len(h.runs()) == 1, 40, what="recording started by the light going on")
            on = time.time() - t0
            assert 20.0 <= on <= 28.0, on
            wait_for(lambda: h.runs()[0]["end_reason"] == "motion_idle", 40, what="first recording stopped")
            stopped = time.time() - t0
            assert 20.0 + KEEP - 1.0 <= stopped <= 20.0 + KEEP + 10.0, stopped
            wait_for(lambda: len(h.runs()) == 2, 40, what="recording started by the light going off")
            off = time.time() - t0
            assert 45.0 <= off <= 53.0, off
            # the status file follows within a moment
            st = wait_for(lambda: (s := h.status())["motion"]["light_count"] == 2 and s, 10, what="both light changes")
            assert st["motion"]["scene_motion_count"] == 0            # the light, not movement in the room
            wait_for(lambda: h.runs()[1]["end_reason"] == "motion_idle", 40, what="second recording stopped")
        finally:
            h.stop()
    runs = h.runs()
    assert [r["end_reason"] for r in runs] == ["motion_idle", "motion_idle"]
    for r in runs:
        segs = [s for s in h.segments() if s["run_id"] == r["run_id"]]
        assert segs and {s["state"] for s in segs} == {"FINALIZED"}
    rec = [json.loads(e["detail"]) for e in h.events("motion_record")]
    assert [(d["why"], d["trigger"]) for d in rec] == [("motion_detected", "light")] * 2
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
