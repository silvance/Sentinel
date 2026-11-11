"""Fixes from the code review of 2026-11-10 (docs/REVIEW-2026-11-10.md): schedule and motion."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import pytest

from gui_support import base_measurements
from sentinel.models import OverallStatus
from sentinel.motion import MotionConfig, MotionDetector
from sentinel.schedule import RecordingSchedule, ScheduleError, parse_schedule
from sentinel.status import evaluate


def _utc(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------- DST
def test_repeated_autumn_hour_is_in_the_plan_and_the_resume_time() -> None:
    """Europe/London, pause daily 00:00-01:30: on 25 Oct 2026 the clock goes back at 02:00 BST, so
    00:00-01:30 local happens twice and the second 01:00-01:30 (01:00-01:30 UTC) is paused too."""
    s = RecordingSchedule(parse_schedule({"enabled": True, "default": "record", "timezone": "Europe/London",
                                          "weekly": [{"action": "pause", "days": ["daily"], "start": "00:00",
                                                      "end": "01:30"}]}, "UTC"))
    assert s.mode_at(_utc("2026-10-25T01:05:00")) == "pause"            # the recorder pauses (as before)
    nxt = s.next_change(_utc("2026-10-25T01:05:00"))
    assert nxt == _utc("2026-10-25T01:30:00")                           # was 26 Oct 01:30
    plan = [(datetime.fromtimestamp(a, timezone.utc).strftime("%d %H:%M"), m)
            for a, _b, m in s.plan(_utc("2026-10-24T22:00:00"), days=1)]
    assert ("25 01:00", "pause") in plan and ("25 01:30", "record") in plan


def test_spring_forward_resume_time() -> None:
    """America/New_York, pause 20:00-02:30: on 8 Mar 2026 02:30 does not exist; recording resumes when
    the clock jumps from 02:00 to 03:00 (07:00 UTC), and next_change says so."""
    s = RecordingSchedule(parse_schedule({"enabled": True, "default": "record", "timezone": "America/New_York",
                                          "weekly": [{"action": "pause", "days": ["daily"], "start": "20:00",
                                                      "end": "02:30"}]}, "UTC"))
    t = _utc("2026-03-08T06:30:00")                                     # 01:30 EST, paused
    assert s.mode_at(t) == "pause"
    resume = s.next_change(t)
    assert resume is not None and s.mode_at(resume) == "record" and resume <= _utc("2026-03-08T07:00:00")


# ---------------------------------------------------------------------- never records
def test_expired_record_date_does_not_hide_a_never_recording_schedule() -> None:
    with pytest.raises(ScheduleError, match="never records"):
        parse_schedule({"enabled": True, "default": "pause",
                        "dates": [{"action": "record", "start": "2020-01-01", "end": "2020-01-02"}]}, "UTC")
    future = datetime.now().year + 1
    parse_schedule({"enabled": True, "default": "pause",
                    "dates": [{"action": "record", "start": f"{future}-01-01", "end": f"{future}-01-02"}]}, "UTC")


def test_far_future_end_date_is_a_config_message_not_a_crash() -> None:
    with pytest.raises(ScheduleError, match="too far in the future"):
        parse_schedule({"enabled": True, "default": "record",
                        "dates": [{"action": "pause", "start": "2026-12-24", "end": "9999-12-31"}]}, "UTC")


# ---------------------------------------------------------------------- motion seen but not recorded
def test_failed_recordings_after_motion_are_an_alarm_not_waiting() -> None:
    ok = evaluate(base_measurements(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                                    motion_idle=True, outage_seconds=100.0, attempts=3),
                  stall_timeout_s=10, segment_seconds=300, connect_timeout_s=20, escalate_after_s=180)
    assert ok.overall is OverallStatus.WATCHING and [a.code for a in ok.alerts] == ["P-003"]
    bad = evaluate(base_measurements(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                                     motion_idle=True, outage_seconds=100.0, attempts=3, motion_record_failures=2),
                   stall_timeout_s=10, segment_seconds=300, connect_timeout_s=20, escalate_after_s=180)
    assert bad.overall is not OverallStatus.WATCHING
    assert bad.alerts[0].code == "W-002" and "could not be recorded" in bad.alerts[0].message


# ---------------------------------------------------------------------- motion detector FFmpeg
def test_detector_that_gets_no_pictures_is_restarted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stand-in FFmpeg that stays alive and sends nothing is killed and restarted."""
    m = MotionConfig(unhealthy_after_s=5)
    det = MotionDetector("ffmpeg", "rtsp://192.0.2.1/x", m, "tcp", None)
    det.argv = [sys.executable, "-c", "import time, sys; sys.stderr.write('x\\n' * 5000); time.sleep(120)"]
    clock = {"t": 0.0}
    det.mono = lambda: clock["t"]  # type: ignore[method-assign]
    t0 = time.monotonic()

    def advance() -> None:
        while time.monotonic() - t0 < 10:
            clock["t"] += 5.0
            time.sleep(0.2)

    import threading

    threading.Thread(target=advance, daemon=True).start()
    got = det._run_ffmpeg()
    assert not got and det.last_error.startswith("no pictures") and time.monotonic() - t0 < 15
