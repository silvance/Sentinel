"""Motion-triggered recording: analysis, settings, the record/wait decision (incl. fail safe),
schedule action "motion", status wording, and the tuning command."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import base_config_dict, make_config
from gui_support import base_measurements
from sentinel.alerts import CATALOG, Severity
from sentinel.config import ConfigError, load_config_dict
from sentinel.export.manifest import GAP_REASONS
from sentinel.gui.ledger_view import GAP_REASON_WORDS
from sentinel.models import OverallStatus, RunEndReason
from sentinel.motion import (FRAME_BYTES, MotionAnalyzer, MotionConfig, MotionDetector, detection_url,
                             detector_argv, run_motion_test)
from sentinel.recorder.supervisor import RecordingSupervisor
from sentinel.schedule import RecordingSchedule, parse_schedule, render_plan
from sentinel.status import evaluate

ROOT = Path(__file__).resolve().parents[2]


def frame(level: int, box: tuple[int, int] | None = None) -> bytes:
    b = bytearray([level]) * FRAME_BYTES
    if box:                                  # a 20x20 bright square at (x, y) on the 160x120 frame
        x, y = box
        for r in range(y, y + 20):
            b[r * 160 + x:r * 160 + x + 20] = bytes([255]) * 20
    return bytes(b)


# ---------------------------------------------------------------------- analysis
def test_analyzer_needs_enough_change_in_enough_frames() -> None:
    a = MotionAnalyzer(MotionConfig(changed_percent=1.0, pixel_threshold=25, min_frames=2))
    assert a.feed(frame(100)) is False                          # first frame: nothing to compare
    assert a.feed(frame(110)) is False and a.last_score == 0.0  # small brightness change: noise
    assert a.feed(frame(100, (10, 10))) is False                # 400/19200 = 2.1 %, first frame of a run
    assert a.last_score == pytest.approx(2.08, abs=0.01)
    assert a.feed(frame(100, (40, 10))) is True                 # second in a row: motion
    assert a.feed(frame(100, (40, 10))) is False                # stopped moving


def test_tiny_change_is_not_motion() -> None:
    a = MotionAnalyzer(MotionConfig(changed_percent=5.0, min_frames=1))
    a.feed(frame(100))
    assert a.feed(frame(100, (0, 0))) is False                  # 2 % < 5 %


# ---------------------------------------------------------------------- settings
def test_motion_settings_are_validated(tmp_path: Path) -> None:
    assert make_config(tmp_path).motion == MotionConfig()
    for bad, match in (({"keep_recording_s": 5}, "keep_recording_s"), ({"changed_percent": 0}, "changed_percent"),
                       ({"stream_path": "rtsp://x/y"}, "stream_path"), ({"bogus": 1}, "unknown")):
        with pytest.raises(ConfigError, match=match):
            load_config_dict(base_config_dict(tmp_path, motion=bad), base_dir=tmp_path)


def test_detection_url_and_command() -> None:
    url = "rtsp://user:pw@192.0.2.5:554/stream1"
    assert detection_url(url, "") == url
    assert detection_url(url, "stream2") == "rtsp://user:pw@192.0.2.5:554/stream2"
    assert detection_url(url, "/cam/realmonitor?channel=1&subtype=1") == \
        "rtsp://user:pw@192.0.2.5:554/cam/realmonitor?channel=1&subtype=1"
    sub = detector_argv("ffmpeg", url, MotionConfig(stream_path="stream2", analysis_fps=4))
    assert "-skip_frame" not in sub and "fps=4,scale=160:120,format=gray" in sub
    main = detector_argv("ffmpeg", url, MotionConfig())
    assert main[main.index("-skip_frame") + 1] == "nokey" and main[main.index("-fps_mode") + 1] == "passthrough"
    assert main[main.index("-rtsp_transport") + 1] == "tcp"


# ---------------------------------------------------------------------- record / wait decision
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _detector(m: MotionConfig) -> tuple[MotionDetector, Clock]:
    c = Clock()
    d = MotionDetector("ffmpeg", "rtsp://h/s", m, clock=c, mono=c)
    d.started_mono = c.t
    return d, c


def test_timer_restarts_with_every_motion() -> None:
    d, c = _detector(MotionConfig(keep_recording_s=60, unhealthy_after_s=15))
    assert d.recording_wanted() is False                  # just started, no picture yet: not known, wait
    c.t += 1
    d.last_frame_mono = c.t
    assert d.recording_wanted() is False                  # pictures, no motion
    d.last_motion_mono = c.t
    c.t += 59
    d.last_frame_mono = c.t
    assert d.recording_wanted() is True
    d.last_motion_mono = c.t                              # new motion restarts the timer
    c.t += 59
    d.last_frame_mono = c.t
    assert d.recording_wanted() is True
    c.t += 2
    d.last_frame_mono = c.t
    assert d.recording_wanted() is False


def test_broken_detector_records(tmp_path: Path) -> None:
    d, c = _detector(MotionConfig(unhealthy_after_s=15))
    c.t += 16                                             # never got a picture
    assert d.healthy() is False and d.warming_up() is False and d.recording_wanted() is True
    d.last_frame_mono = c.t
    assert d.recording_wanted() is False
    c.t += 16                                             # pictures stopped
    assert d.recording_wanted() is True


def _sup(schedule: object, motion: object, tmp_path: Path) -> RecordingSupervisor:
    sup = RecordingSupervisor.__new__(RecordingSupervisor)
    sup.cfg, sup.schedule, sup.motion = make_config(tmp_path), schedule, motion
    sup.clock = SimpleNamespace(wall=lambda: 0.0)
    return sup


def test_pause_reason(tmp_path: Path) -> None:
    def sched(mode: str) -> object:
        return SimpleNamespace(mode_at=lambda _t: mode)

    idle, moving = SimpleNamespace(recording_wanted=lambda: False), SimpleNamespace(recording_wanted=lambda: True)
    assert _sup(None, None, tmp_path)._pause_reason() is None
    assert _sup(sched("record"), idle, tmp_path)._pause_reason() is None
    assert _sup(sched("pause"), moving, tmp_path)._pause_reason() is RunEndReason.SCHEDULED_PAUSE
    assert _sup(sched("motion"), idle, tmp_path)._pause_reason() is RunEndReason.MOTION_IDLE
    assert _sup(sched("motion"), moving, tmp_path)._pause_reason() is None
    assert _sup(sched("motion"), None, tmp_path)._pause_reason() is None          # no detector: record

    def broken(_t: float) -> str:
        raise RuntimeError("boom")

    assert _sup(SimpleNamespace(mode_at=broken), idle, tmp_path)._pause_reason() is None   # fail safe


# ---------------------------------------------------------------------- schedule
WORK_HOURS = {"enabled": True, "default": "motion",
              "weekly": [{"action": "record", "days": ["weekdays"], "start": "07:00", "end": "18:00"}]}


def test_work_hours_record_and_the_rest_is_motion() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    s = RecordingSchedule(parse_schedule(WORK_HOURS, "America/New_York"))
    at = lambda t: datetime.fromisoformat(t).replace(tzinfo=ZoneInfo("America/New_York")).timestamp()  # noqa: E731
    assert s.uses_motion
    assert s.mode_at(at("2026-11-09 12:00")) == "record" and s.mode_at(at("2026-11-09 19:00")) == "motion"
    assert s.mode_at(at("2026-11-14 12:00")) == "motion"                       # Saturday
    assert s.next_change(at("2026-11-09 12:00")) == at("2026-11-09 18:00")
    assert "MOTION  from Mon 09 Nov 2026 18:00" in render_plan(s, at("2026-11-09 12:00"), days=1)
    assert not RecordingSchedule(parse_schedule({"enabled": True}, "UTC")).uses_motion


def test_motion_only_counts_as_recording() -> None:
    parse_schedule({"enabled": True, "default": "motion"}, "UTC")              # not "never records"


# ---------------------------------------------------------------------- status and wording
def test_waiting_for_motion_is_planned_not_an_outage() -> None:
    m = base_measurements(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                          motion_idle=True, outage_seconds=40_000.0, attempts=3, last_end_reason="motion_idle")
    d = evaluate(m, stall_timeout_s=6, segment_seconds=300, connect_timeout_s=10, escalate_after_s=180)
    assert d.overall is OverallStatus.WATCHING and [a.code for a in d.alerts] == ["P-003"]
    assert d.alerts[0].severity == Severity.NOTICE and d.alerts[0].message.startswith("Waiting for motion (P-003).")


def test_broken_detector_is_a_warning_while_recording() -> None:
    d = evaluate(base_measurements(motion_broken=True), stall_timeout_s=6, segment_seconds=300,
                 connect_timeout_s=10, escalate_after_s=180)
    assert d.overall is OverallStatus.RECORDING and [a.code for a in d.alerts] == ["W-001"]
    assert d.alerts[0].message.startswith("Error W-001: Motion detection is not working, so the recorder records")


def test_alert_codes_are_unique() -> None:
    codes = re.findall(r'AlertDef\("([A-Z]-\d+)"', (ROOT / "src" / "sentinel" / "alerts.py").read_text("utf-8"))
    assert len(codes) == len(set(codes)) == len(CATALOG)


def test_gap_wording() -> None:
    assert "motion" in GAP_REASONS["motion_idle"] and "planned" in GAP_REASONS["motion_idle"]
    assert "motion" in GAP_REASON_WORDS["motion_idle"]


# ---------------------------------------------------------------------- tuning command
def test_motion_test_refuses_without_a_camera(tmp_path: Path) -> None:
    import dataclasses

    cfg = make_config(tmp_path)
    cfg = dataclasses.replace(cfg, camera=dataclasses.replace(cfg.camera, url="", pending=True,
                                                              development_source=False, input_format=None))
    out: list[str] = []
    assert run_motion_test(cfg, 1, out.append) == 2 and "set-camera" in out[0]


# ---------------------------------------------------------------------- reviewer markers
def _tracker(merge_s: float = 10.0) -> tuple[MotionDetector, list[tuple[float, float, float]]]:
    periods: list[tuple[float, float, float]] = []
    d = MotionDetector("ffmpeg", "rtsp://h/s", MotionConfig(min_frames=2, marker_merge_s=merge_s),
                       on_period=lambda a, b, p: periods.append((a, b, p)))
    return d, periods


def _feed(d: MotionDetector, t: float, moving: bool) -> None:
    f = frame(100, (int(t * 7) % 120, 10)) if moving else frame(100)
    d._track(t, d.analyzer.feed(f))


def test_motion_periods_start_at_the_first_moving_frame_and_merge() -> None:
    d, periods = _tracker(merge_s=10)
    _feed(d, 0.0, False)
    for t in (5.0, 6.0, 7.0):                        # moving from 5 s (confirmed at 6 s)
        _feed(d, t, True)
    for t in (8.0, 12.0):                           # still for 5 s: shorter than merge_s
        _feed(d, t, False)
    for t in (13.0, 14.0):                          # moving again: same period
        _feed(d, t, True)
    for t in (15.0, 16.0, 27.0):                    # the box vanishing at 15 s is a change too; then still
        _feed(d, t, False)
    assert len(periods) == 1 and periods[0][:2] == (5.0, 15.0) and periods[0][2] > 1.0
    _feed(d, 30.0, True)
    _feed(d, 31.0, True)
    d.stop()                                        # an open period is logged on stop
    assert len(periods) == 2 and periods[1][:2] == (30.0, 31.0)


def test_long_motion_is_logged_in_parts() -> None:
    from sentinel.motion import MAX_PERIOD_S

    d, periods = _tracker()
    t = 0.0
    while t <= MAX_PERIOD_S + 30:
        _feed(d, t, True)
        t += 1.0
    assert len(periods) == 1 and periods[0][1] - periods[0][0] >= MAX_PERIOD_S


def test_timeline_motion_is_clipped_and_per_camera() -> None:
    import json as _json

    from sentinel.gui.ledger_view import motion_periods

    rows = [{"detail": _json.dumps({"camera_id": "CAM01", "start_utc": "2026-11-10T10:00:00.000000Z",
                                    "end_utc": "2026-11-10T10:00:30.000000Z"})},
            {"detail": _json.dumps({"camera_id": "CAM02", "start_utc": "2026-11-10T11:00:00.000000Z",
                                    "end_utc": "2026-11-10T11:00:30.000000Z"})},
            {"detail": "not json"}]
    from sentinel.clock import parse_iso_utc

    t = parse_iso_utc("2026-11-10T10:00:10.000000Z")
    assert motion_periods(rows, "CAM01", t, t + 3600) == ((t, t + 20.0),)


def test_setup_turns_markers_on(tmp_path: Path) -> None:
    from sentinel.launcher import setup
    from sentinel.paths import Layout

    root = tmp_path / "m"
    lay = Layout("machine", root / "sentinel.toml", root / "camera.secret", root / "spool", root / "data", root / "logs")
    d = setup.build_config(setup.SetupAnswers("192.0.2.10", "stream1"), lay)
    assert load_config_dict(d, base_dir=tmp_path).motion.markers is True
    assert make_config(tmp_path).motion.markers is False             # code default: off
