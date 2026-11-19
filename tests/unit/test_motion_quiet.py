"""No-motion fail safe (owner request 2026-11-18, 0.3.21): when the detector has watched for the set number of
counted days without seeing any motion, the recorder records all the time until motion is seen again. Days of
the week that are not picked, schedule pauses and the schedule's particular dates (days off) are not counted."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import base_config_dict, make_config
from gui_support import base_measurements
from sentinel.alerts import Severity
from sentinel.config import ConfigError, load_config_dict
from sentinel.motion import (QUIET_MAX_STEP_S, QUIET_STATE, MotionConfig, MotionConfigError, MotionDetector,
                             QuietWatch, count_day_numbers, counts_now, quiet_duration, quiet_text)
from sentinel.schedule import RecordingSchedule, parse_schedule
from sentinel.service import RecorderService
from sentinel.status import evaluate

DAY = 86400.0
LIMIT = 0.001 * DAY                       # 86.4 s: [motion] no_motion_days = 0.001


def cfg(**kw: Any) -> MotionConfig:
    return MotionConfig(**{"no_motion_days": 0.001, **kw})


def epoch(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------- settings
def test_it_is_off_unless_asked_for_and_the_settings_are_checked(tmp_path: Path) -> None:
    m = make_config(tmp_path).motion
    assert m.no_motion_days == 0.0 and m.no_motion_count_days == ("weekdays",)
    on = make_config(tmp_path, motion={"no_motion_days": 2, "no_motion_count_days": ["mon", "sat"]}).motion
    assert on.no_motion_days == 2.0 and on.no_motion_count_days == ("mon", "sat")
    assert make_config(tmp_path, motion={"no_motion_count_days": "daily"}).motion.no_motion_count_days == ("daily",)
    for bad, match in (({"no_motion_days": -1}, "no_motion_days"), ({"no_motion_days": 61}, "no_motion_days"),
                       ({"no_motion_days": "2"}, "must be a number"),
                       ({"no_motion_count_days": ["funday"]}, "unknown day"),
                       ({"no_motion_count_days": []}, "at least one day")):
        with pytest.raises(ConfigError, match=match):
            load_config_dict(base_config_dict(tmp_path, motion=bad), base_dir=tmp_path)


def test_days_are_written_like_the_schedules_days() -> None:
    assert count_day_numbers(("weekdays",)) == frozenset(range(5))
    assert count_day_numbers(["Sat", "sunday"]) == frozenset({5, 6})
    assert count_day_numbers("daily") == frozenset(range(7))
    assert count_day_numbers(["weekdays", "sat"]) == frozenset(range(6))
    for bad in ([], None, [3], ["mo"]):
        with pytest.raises(MotionConfigError):
            count_day_numbers(bad)


# ---------------------------------------------------------------------- what counts
def test_weekends_pauses_and_days_off_do_not_count() -> None:
    sc = RecordingSchedule(parse_schedule({
        "enabled": True, "default": "motion", "timezone": "UTC",
        "weekly": [{"action": "record", "days": ["weekdays"], "start": "07:00", "end": "18:00"},
                   {"action": "pause", "days": ["wed"], "start": "20:00", "end": "22:00"}],
        # the target is on leave Thursday and Friday; recording stays on motion
        "dates": [{"action": "motion", "start": "2026-11-19", "end": "2026-11-20"}]}, "UTC"))
    weekdays = count_day_numbers(("weekdays",))
    assert counts_now(sc, weekdays, epoch("2026-11-16 12:00"))              # Monday, recording hours
    assert counts_now(sc, weekdays, epoch("2026-11-16 23:00"))              # Monday night, motion period
    assert not counts_now(sc, weekdays, epoch("2026-11-18 21:00"))          # Wednesday, schedule pause
    assert counts_now(sc, weekdays, epoch("2026-11-18 22:30"))
    assert not counts_now(sc, weekdays, epoch("2026-11-19 12:00"))          # day off
    assert not counts_now(sc, weekdays, epoch("2026-11-20 23:59"))          # day off (a date alone: to its end)
    assert not counts_now(sc, weekdays, epoch("2026-11-21 12:00"))          # Saturday
    assert counts_now(sc, count_day_numbers("daily"), epoch("2026-11-21 12:00"))
    assert counts_now(sc, weekdays, epoch("2026-11-23 00:01"))              # Monday again


def test_the_day_of_the_week_is_the_schedules_local_day() -> None:
    sc = RecordingSchedule(parse_schedule({"enabled": True, "default": "motion", "timezone": "America/New_York"},
                                          "UTC"))
    weekdays = count_day_numbers(("weekdays",))
    assert counts_now(sc, weekdays, epoch("2026-11-21 03:00"))              # Friday 22:00 in New York
    assert not counts_now(sc, weekdays, epoch("2026-11-21 06:00"))          # Saturday 01:00 in New York


# ---------------------------------------------------------------------- the count
def looks(q: QuietWatch, start: float, n: int, step: float = 10.0, **kw: Any) -> list[str | None]:
    args = {"watching": True, "counts": True, "motion_count": 0, **kw}
    return [q.update(start + i * step, 1_000_000.0 + start + i * step, **args) for i in range(n)]


def test_only_watching_time_that_counts_is_added_and_the_limit_switches_it_on() -> None:
    q = QuietWatch(cfg())
    assert looks(q, 0, 4) == [None] * 4 and q.quiet_s == pytest.approx(30.0) and q.counting
    assert looks(q, 40, 3, counts=False) == [None] * 3                       # a day off: the clock stands still
    assert q.quiet_s == pytest.approx(30.0) and not q.counting
    looks(q, 70, 3, watching=False)                                          # no pictures: that is W-001, not this
    assert q.quiet_s == pytest.approx(30.0)
    out = looks(q, 100, 6)                                                   # 30 + 6 x 10 = 90 s >= 86.4 s
    assert out[:5] == [None] * 5 and out[5] == "started" and q.active
    assert q.quiet_s == pytest.approx(90.0) and q.active_since_epoch == pytest.approx(1_000_150.0)
    snap = q.snapshot()
    assert snap["active"] and snap["count_days"] == ["mon", "tue", "wed", "thu", "fri"]
    assert snap["text"].startswith("ON: no motion seen in 1 min of watching") and "W-003" in snap["text"]
    assert looks(q, 160, 3) == [None] * 3 and q.active                       # stays on, says so once


def test_motion_sets_the_count_back_and_switches_it_off() -> None:
    q = QuietWatch(cfg())
    looks(q, 0, 6)
    assert q.quiet_s == pytest.approx(50.0)
    assert q.update(60.0, 5000.0, watching=True, counts=True, motion_count=3, last_motion_epoch=4990.0) is None
    assert q.quiet_s == 0.0 and not q.active and q.last_motion_epoch == 4990.0
    out = looks(q, 70, 9, motion_count=3)                                    # the same count: no new motion
    assert out[-1] == "started" and out.count("started") == 1 and q.active   # counted again from the motion
    assert q.update(200.0, 6000.0, watching=True, counts=True, motion_count=4, last_motion_epoch=5999.0) == "ended"
    assert not q.active and q.quiet_s == 0.0 and q.active_since_epoch is None
    # Motion on a day that does not count still proves the detector works.
    looks(q, 210, 5, motion_count=4)
    q.update(300.0, 7000.0, watching=True, counts=False, motion_count=5)
    assert q.quiet_s == 0.0 and q.last_motion_epoch == 7000.0


def test_a_long_gap_between_two_looks_is_not_watching_time() -> None:
    q = QuietWatch(cfg(no_motion_days=1))
    q.update(0.0, 0.0, watching=True, counts=True, motion_count=0)
    q.update(8 * 3600.0, 8 * 3600.0, watching=True, counts=True, motion_count=0)      # the laptop slept
    assert q.quiet_s == pytest.approx(QUIET_MAX_STEP_S) and not q.active
    q.update(8 * 3600.0 - 50.0, 0.0, watching=True, counts=True, motion_count=0)      # never goes backwards
    assert q.quiet_s == pytest.approx(QUIET_MAX_STEP_S)


def test_the_count_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / QUIET_STATE
    q = QuietWatch(cfg(), path)
    looks(q, 0, 6)
    assert json.loads(path.read_text("utf-8"))["format"] == "sentinel-motion-quiet"   # saved on the first change
    q.save(123.0)
    again = QuietWatch(cfg(), path)
    assert again.quiet_s == pytest.approx(50.0) and not again.active
    assert looks(again, 0, 5)[-1] == "started"                               # 50 + 4 x 10 = 90 s
    on = QuietWatch(cfg(), path)                                             # written at the switch
    assert on.active and on.active_since_epoch is not None
    # A raised limit: it would not be on with today's setting, so it is not.
    assert not QuietWatch(cfg(no_motion_days=1), path).active
    QuietWatch.forget(path)
    assert not path.exists() and QuietWatch(cfg(), path).quiet_s == 0.0
    QuietWatch.forget(path)                                                  # nothing to forget: no error


@pytest.mark.parametrize("text", ["", "{", "[]", '{"format": "other"}',
                                  '{"format": "sentinel-motion-quiet", "quiet_s": "x"}',
                                  '{"format": "sentinel-motion-quiet", "quiet_s": -5}'])
def test_a_count_that_cannot_be_read_starts_from_zero(tmp_path: Path, text: str) -> None:
    path = tmp_path / QUIET_STATE
    path.write_text(text, "utf-8")
    q = QuietWatch(cfg(), path)
    assert q.quiet_s == 0.0 and not q.active
    looks(q, 0, 2)
    assert QuietWatch(cfg(), path).quiet_s == pytest.approx(10.0)            # and is written properly again


def test_a_count_that_cannot_be_saved_never_stops_the_look(tmp_path: Path) -> None:
    q = QuietWatch(cfg(), tmp_path / "no such folder" / QUIET_STATE)
    assert looks(q, 0, 10)[-1] == "started" and q.active


def test_status_wording() -> None:
    assert quiet_duration(0) == "0 min" and quiet_duration(40 * 60) == "40 min"
    assert quiet_duration(5 * 3600 + 20 * 60) == "5 h 20 min" and quiet_duration(11 * 3600 + 20 * 60) == "11 h"
    assert quiet_duration(DAY) == "1 day" and quiet_duration(2 * DAY + 3 * 3600 + 59) == "2 days 3 h"
    assert quiet_text({"limit_s": 2 * DAY, "quiet_s": 5 * 3600, "active": False, "counting": True}) == \
        "no motion for 5 h of the 2 days allowed"
    assert quiet_text({"limit_s": 2 * DAY, "quiet_s": 5 * 3600, "active": False, "counting": False}) == \
        "no motion for 5 h of the 2 days allowed; not counting now (day off, pause, or a day that does not count)"


# ---------------------------------------------------------------------- the recorder
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_the_detector_records_while_the_fail_safe_is_on() -> None:
    c = Clock()
    d = MotionDetector("ffmpeg", "rtsp://h/s", MotionConfig(keep_recording_s=60, unhealthy_after_s=15), clock=c, mono=c)
    d.started_mono = d.last_frame_mono = c.t
    assert d.healthy() and d.recording_wanted() is False and d.snapshot()["forced"] is False
    d.force_record = True
    assert d.recording_wanted() is True and d.snapshot()["forced"] is True
    d.force_record = False
    assert d.recording_wanted() is False


class Events:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str, dict]] = []

    def add_event(self, severity: str, category: str, code: str, message: str, detail: dict | None = None) -> None:
        self.rows.append((severity, category, code, message, detail or {}))


def service(tmp_path: Path, schedule: dict, **motion: Any) -> tuple[Any, Any, Events]:
    """The recorder's own step (RecorderService._watch_quiet) with a detector that can be told what it saw."""
    conf = make_config(tmp_path, schedule=schedule, motion={"no_motion_days": 0.001, **motion})
    now = SimpleNamespace(wall=epoch("2026-11-16 12:00"), mono=0.0)                # a Monday
    det = SimpleNamespace(healthy=lambda: True, motion_count=0, last_motion_epoch=None, force_record=False,
                          scene_motion_count=0, last_scene_motion_epoch=None)
    led = Events()
    svc = SimpleNamespace(cfg=conf, quiet=QuietWatch(conf.motion), motion=det, ledger=led,
                          schedule=RecordingSchedule(conf.schedule),
                          clock=SimpleNamespace(wall=lambda: now.wall, mono=lambda: now.mono))
    svc.step = lambda seconds=10.0: (setattr(now, "wall", now.wall + seconds), setattr(now, "mono", now.mono + seconds),
                                     RecorderService._watch_quiet(svc))
    svc.now = now
    return svc, det, led


MOTION_ALWAYS = {"enabled": True, "default": "motion", "timezone": "UTC"}


def test_the_recorder_switches_to_recording_all_the_time_and_back(tmp_path: Path) -> None:
    svc, det, led = service(tmp_path, MOTION_ALWAYS)
    for _ in range(9):
        svc.step()
    assert not det.force_record and led.rows == []                           # 80 s of the 86.4 s
    svc.step()
    assert det.force_record is True
    sev, cat, code, msg, detail = led.rows[-1]
    assert (sev, cat, code) == ("warning", "motion", "motion_quiet_fail_safe") and "recording all the time" in msg
    assert detail["no_motion_days"] == 0.001 and detail["count_days"] == ["mon", "tue", "wed", "thu", "fri"]
    assert detail["watched_without_motion_s"] == pytest.approx(90.0) and detail["last_motion_utc"] is None
    for _ in range(5):
        svc.step()
    assert det.force_record and len(led.rows) == 1                           # said once
    det.motion_count, det.last_motion_epoch = 1, svc.now.wall + 5.0          # the light is switched: not counted
    svc.step()
    assert det.force_record is True and len(led.rows) == 1
    det.motion_count, det.scene_motion_count = 2, 1                          # someone walks in
    det.last_motion_epoch = det.last_scene_motion_epoch = svc.now.wall + 9.0
    svc.step()
    assert det.force_record is False
    assert led.rows[-1][:3] == ("audit", "motion", "motion_quiet_ended") and len(led.rows) == 2
    assert led.rows[-1][4]["last_motion_utc"].startswith("2026-11-16T12:02:")


def test_days_off_stop_the_count_in_the_recorder(tmp_path: Path) -> None:
    leave = {**MOTION_ALWAYS, "dates": [{"action": "motion", "start": "2026-11-16", "end": "2026-11-16"}]}
    svc, det, led = service(tmp_path, leave)                                 # Monday is a day off
    for _ in range(30):
        svc.step()
    assert not det.force_record and svc.quiet.quiet_s == 0.0 and led.rows == []
    svc.now.wall = epoch("2026-11-17 00:00")                                 # Tuesday: back at work
    for _ in range(10):
        svc.step()
    assert det.force_record and led.rows[-1][2] == "motion_quiet_fail_safe"


def test_a_weekend_does_not_count_unless_it_is_picked(tmp_path: Path) -> None:
    svc, det, _ = service(tmp_path / "a", MOTION_ALWAYS)
    svc.now.wall = epoch("2026-11-21 12:00")                                 # Saturday
    for _ in range(30):
        svc.step()
    assert not det.force_record and svc.quiet.quiet_s == 0.0
    svc, det, _ = service(tmp_path / "b", MOTION_ALWAYS, no_motion_count_days=["daily"])
    svc.now.wall = epoch("2026-11-21 12:00")
    for _ in range(10):
        svc.step()
    assert det.force_record


def test_a_detector_without_pictures_is_not_counted_and_a_fault_never_stops_recording(tmp_path: Path) -> None:
    svc, det, led = service(tmp_path, MOTION_ALWAYS)
    det.healthy = lambda: False                                              # W-001 already records then
    for _ in range(30):
        svc.step()
    assert not det.force_record and svc.quiet.quiet_s == 0.0

    def boom() -> bool:
        raise RuntimeError("detector state unreadable")

    det.healthy = boom
    svc.step()                                                               # logged, not raised
    svc.quiet = None
    svc.step()                                                               # switched off: nothing to do


# ---------------------------------------------------------------------- what the operator sees
def test_w003_is_shown_only_while_video_is_recorded() -> None:
    def codes(**kw: Any) -> dict[str, Any]:
        return {a.code: a for a in evaluate(base_measurements(**kw), 15.0, 3600.0, 20.0).alerts}

    assert "W-003" not in codes()
    a = codes(motion_quiet=True)["W-003"]
    assert a.severity == Severity.WARNING and not a.recording_stopped
    assert a.message.startswith("Error W-003: No motion has been seen for a long time, so the recorder records "
                                "all the time.") and "by itself when motion is seen" in a.message
    assert "W-003" not in codes(motion_quiet=True, ffmpeg_running=False)     # camera off: the outage alert speaks
    both = codes(motion_quiet=True, motion_broken=True)
    assert "W-001" in both and "W-003" not in both                           # no pictures is the nearer cause
