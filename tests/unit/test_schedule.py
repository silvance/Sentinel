"""Recording schedule rules (sentinel.schedule): parsing, refusals, decisions, next change, DST."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sentinel.schedule import (RecordingSchedule, ScheduleError, parse_schedule, pause_message, render_plan)

NY = ZoneInfo("America/New_York")


def at(text: str, tz: ZoneInfo = NY) -> float:
    """Local wall-clock time -> epoch (2026-11-09 is a Monday)."""
    return datetime.fromisoformat(text).replace(tzinfo=tz).timestamp()


def sched(**raw: object) -> RecordingSchedule:
    raw.setdefault("enabled", True)
    return RecordingSchedule(parse_schedule(raw, "America/New_York"))


WEEKEND_AND_NIGHTS = dict(weekly=[
    {"action": "pause", "days": ["sat", "sun"], "start": "00:00", "end": "24:00"},
    {"action": "pause", "days": ["weekdays"], "start": "20:00", "end": "06:00"},
])


def test_disabled_or_missing_schedule_always_records() -> None:
    assert parse_schedule(None, "UTC").enabled is False
    s = RecordingSchedule(parse_schedule({"enabled": False, **WEEKEND_AND_NIGHTS}, "UTC"))
    assert s.should_record(at("2026-11-14 12:00")) and s.next_change(0.0) is None
    assert "records all the time" in render_plan(s, 0.0)


def test_weekly_periods_and_overnight_wrap() -> None:
    s = sched(**WEEKEND_AND_NIGHTS)
    assert s.should_record(at("2026-11-09 12:00"))          # Monday noon
    assert not s.should_record(at("2026-11-09 20:00"))      # Monday night starts exactly at 20:00
    assert not s.should_record(at("2026-11-10 05:59:59"))   # ... runs past midnight into Tuesday
    assert s.should_record(at("2026-11-10 06:00"))
    assert not s.should_record(at("2026-11-14 12:00"))      # Saturday
    # "weekdays" nights start Mon-Fri evenings: Sunday night is not one, so Monday 05:00 records.
    assert s.should_record(at("2026-11-16 05:00"))
    st = s.state_at(at("2026-11-14 12:00"))
    assert st.source == "weekly" and "sat" not in st.period and "#1" in st.period


def test_friday_night_runs_into_saturday_and_sunday_wraps_to_monday() -> None:
    s = sched(weekly=[{"action": "pause", "days": ["sun"], "start": "22:00", "end": "02:00"}])
    assert not s.should_record(at("2026-11-15 23:00"))      # Sunday
    assert not s.should_record(at("2026-11-16 01:00"))      # Monday, wrapped across the week end
    assert s.should_record(at("2026-11-16 02:00"))


def test_dates_override_weekly_and_date_only_end_means_end_of_day() -> None:
    s = sched(**WEEKEND_AND_NIGHTS, dates=[
        {"action": "record", "start": "2026-12-24 18:00", "end": "2026-12-26"},
        {"action": "pause", "start": "2026-11-11", "end": "2026-11-11"},        # one whole day
    ])
    assert s.should_record(at("2026-12-25 03:00"))          # Christmas night: weekly says pause
    assert s.should_record(at("2026-12-26 23:59"))          # through the end of the 26th
    assert not s.should_record(at("2026-12-27 01:00"))      # Sunday: weekly pause again
    assert not s.should_record(at("2026-11-11 12:00"))      # Wednesday noon, paused by date
    assert s.should_record(at("2026-11-12 12:00"))
    assert s.state_at(at("2026-12-25 03:00")).source == "dates"


def test_default_pause_records_only_in_listed_periods() -> None:
    s = sched(default="pause", weekly=[{"action": "record", "days": ["mon"], "start": "09:00", "end": "17:00"}])
    assert s.should_record(at("2026-11-09 09:00")) and not s.should_record(at("2026-11-09 17:00"))
    assert not s.should_record(at("2026-11-10 10:00"))


def test_next_change_and_plan() -> None:
    s = sched(**WEEKEND_AND_NIGHTS)
    assert s.next_change(at("2026-11-09 12:00")) == at("2026-11-09 20:00")
    assert s.next_change(at("2026-11-13 21:00")) == at("2026-11-16 00:00")   # Friday night + weekend
    plan = s.plan(at("2026-11-09 12:00"), days=2)
    assert [p[2] for p in plan] == [True, False, True, False, True]
    assert plan[1][0] == at("2026-11-09 20:00") and plan[1][1] == at("2026-11-10 06:00")
    text = render_plan(s, at("2026-11-09 12:00"), days=1)
    assert "PAUSE   from Mon 09 Nov 2026 20:00  until Tue 10 Nov 2026 06:00" in text


def test_next_change_found_after_a_long_date_period() -> None:
    s = sched(**WEEKEND_AND_NIGHTS, dates=[{"action": "pause", "start": "2026-11-09", "end": "2027-01-31"}])
    # Jan 31 2027 is a Sunday: the weekly weekend pause continues until Monday 00:00
    assert s.next_change(at("2026-11-09 12:00")) == at("2027-02-01 00:00")


def test_daylight_saving_follows_the_wall_clock() -> None:
    s = sched(weekly=[{"action": "pause", "days": ["daily"], "start": "01:00", "end": "06:00"}])
    # 2026-11-01: clocks go back 02:00 -> 01:00 in New York; the night is one hour longer.
    start, end = at("2026-11-01 01:00"), s.next_change(at("2026-11-01 01:00"))
    assert end == at("2026-11-01 06:00") and end - start == 6 * 3600
    assert not s.should_record(datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NY).timestamp())
    # 2026-03-08: clocks go forward; the night is one hour shorter, resume is still 06:00 local.
    assert s.next_change(at("2026-03-08 01:00")) == at("2026-03-08 06:00")


@pytest.mark.parametrize("raw,match", [
    ({"weekly": [{"action": "pause", "days": ["mon"], "start": "08:00", "end": "08:00"}]}, "same time"),
    ({"weekly": [{"action": "pause", "days": ["mon"], "start": "8am", "end": "09:00"}]}, "24-hour"),
    ({"weekly": [{"action": "pause", "days": ["mon"], "start": "24:00", "end": "09:00"}]}, "only allowed as an end"),
    ({"weekly": [{"action": "pause", "days": ["funday"], "start": "08:00", "end": "09:00"}]}, "unknown day"),
    ({"weekly": [{"action": "stop", "days": ["mon"], "start": "08:00", "end": "09:00"}]}, "record\" or \"pause"),
    ({"weekly": [{"action": "pause", "days": ["mon"], "start": "08:00"}]}, "missing end"),
    ({"weekly": [{"action": "pause", "days": ["mon"], "start": "08:00", "end": "09:00", "x": 1}]}, "unknown key"),
    ({"weekly": [{"action": "pause", "days": ["mon"], "start": "08:00", "end": "12:00"},
                 {"action": "record", "days": ["weekdays"], "start": "11:00", "end": "13:00"}]}, "overlap"),
    ({"dates": [{"action": "pause", "start": "2026-12-24", "end": "2026-12-26"},
                {"action": "record", "start": "2026-12-25 10:00", "end": "2026-12-25 11:00"}]}, "overlap"),
    ({"dates": [{"action": "pause", "start": "2026-12-24 10:00", "end": "2026-12-24 09:00"}]}, "not after"),
    ({"dates": [{"action": "pause", "start": "2026-02-30", "end": "2026-03-01"}]}, "real date"),
    ({"default": "pause"}, "never records"),
    ({"weekly": [{"action": "pause", "days": ["daily"], "start": "00:00", "end": "24:00"}]}, "never records"),
    ({"timezone": "Mars/Base"}, "unknown timezone"),
    ({"bogus": 1}, "unknown key"),
])
def test_unusable_schedules_are_refused(raw: dict, match: str) -> None:
    with pytest.raises(ScheduleError, match=match):
        parse_schedule({"enabled": True, **raw}, "UTC")


def test_agreeing_overlaps_are_allowed() -> None:
    sched(weekly=[{"action": "pause", "days": ["mon"], "start": "08:00", "end": "12:00"},
                  {"action": "pause", "days": ["weekdays"], "start": "11:00", "end": "13:00"}])


def test_pause_message_is_plain_and_names_the_resume_time() -> None:
    s = sched(**WEEKEND_AND_NIGHTS)
    msg = pause_message(s, at("2026-11-16 06:00"))
    assert msg == ("Recording is paused by the recording schedule (P-001). Recording resumes automatically on "
                   "Mon 16 Nov 2026 06:00. No action is needed.")
    assert "Contact technical support" in pause_message(s, None)
