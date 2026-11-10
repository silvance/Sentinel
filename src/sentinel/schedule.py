"""Recording schedule: pause and resume recording at times set in advance (``[schedule]``).

For sites where an agent cannot always get to the recorder: the technician sets, ahead of time,
when the recorder records and when it pauses, by day of the week and time of day ("weekly") and
for one-off date ranges such as holidays ("dates").

    [schedule]
    enabled = true
    default = "record"             # outside every listed period: "record" or "pause"
    timezone = ""                  # "" = [recording] display_timezone

    [[schedule.weekly]]            # every Saturday and Sunday, all day
    action = "pause"
    days = ["sat", "sun"]
    start = "00:00"
    end = "24:00"

    [[schedule.weekly]]            # weekday nights; an end before the start runs past midnight
    action = "pause"
    days = ["weekdays"]
    start = "20:00"
    end = "06:00"

    [[schedule.dates]]             # record straight through the holiday, whatever the weekly plan says
    action = "record"
    start = "2026-12-24 18:00"
    end = "2026-12-27"             # a date alone: the END of that day

Rules (all checked when the configuration is loaded; a schedule that breaks one is refused):

* Precedence: a ``dates`` period overrides ``weekly`` periods, which override ``default``.
* ``days`` are mon..sun, or "weekdays", "weekend", "daily". Times are 24-hour "HH:MM" (or
  "HH:MM:SS"); "24:00" is allowed as an end. A weekly period whose end is not after its start runs
  past midnight into the next day; ``days`` names the day it starts. Start = end is refused.
* Periods at the same level may overlap only if they agree (two weekly periods that say "record"
  and "pause" for the same time are refused, and so are two such date periods).
* A schedule that would never record is refused.
* Times are local wall-clock times in ``timezone``. Daylight-saving changes follow the wall
  clock: "06:00" is 06:00 local time on both sides of a change. Following the wall clock also
  means a wrong or changed system clock moves the schedule (alerts T-001/T-002 report clock
  problems).

What a pause does is decided in the recorder (``recorder.supervisor``), not here: the current
recording file is closed normally, the pause is recorded in the recording log as a gap with the
reason ``scheduled_pause`` plus ``schedule_pause``/``schedule_resume`` events, and the status shows
P-001 "Recording is paused by the recording schedule" with the time recording resumes. Nothing is
deleted and the archive copy keeps running.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RECORD, PAUSE = "record", "pause"
ACTIONS = (RECORD, PAUSE)
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_GROUPS = {"weekdays": (0, 1, 2, 3, 4), "weekend": (5, 6), "daily": (0, 1, 2, 3, 4, 5, 6)}
DAY = 86400
WEEK = 7 * DAY
MAX_PERIODS = 500
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$")


class ScheduleError(ValueError):
    """The schedule cannot be used. The message names the period and is shown to the technician."""


@dataclass(frozen=True)
class WeeklyPeriod:
    action: str
    days: tuple[int, ...]           # 0 = Monday
    start_s: int                    # seconds after local midnight
    end_s: int                      # 1..86400; <= start_s means "next day"
    text: str = ""                  # as written, for messages

    def intervals(self) -> list[tuple[int, int]]:
        """[start, end) in seconds after Monday 00:00, split at the end of the week."""
        out = []
        length = (self.end_s - self.start_s) if self.end_s > self.start_s else (DAY - self.start_s + self.end_s)
        for d in self.days:
            s = d * DAY + self.start_s
            e = s + length
            if e <= WEEK:
                out.append((s, e))
            else:
                out += [(s, WEEK), (0, e - WEEK)]
        return out


@dataclass(frozen=True)
class DatePeriod:
    action: str
    start: datetime                 # naive local wall-clock time
    end: datetime
    text: str = ""


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool = False
    default: str = RECORD
    timezone: str = "UTC"
    weekly: tuple[WeeklyPeriod, ...] = field(default_factory=tuple)
    dates: tuple[DatePeriod, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------- parsing
def _seconds(value: Any, where: str, allow_24: bool) -> int:
    m = _TIME.match(value) if isinstance(value, str) else None
    if not m:
        raise ScheduleError(f"{where}: time must look like 08:00 (24-hour clock), not {value!r}")
    h, mi, s = int(m[1]), int(m[2]), int(m[3] or 0)
    if (h, mi, s) == (24, 0, 0) and allow_24:
        return DAY
    if h > 23 or mi > 59 or s > 59:
        raise ScheduleError(f"{where}: {value!r} is not a time of day" + (" (24:00 is only allowed as an end)"
                                                                         if h == 24 else ""))
    return h * 3600 + mi * 60 + s


def _datetime(value: Any, where: str, is_end: bool) -> datetime:
    m = _DATE.match(value) if isinstance(value, str) else None
    if not m:
        raise ScheduleError(f"{where}: date must look like 2026-12-24 or 2026-12-24 18:00, not {value!r}")
    try:
        d = date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        raise ScheduleError(f"{where}: {value!r} is not a real date") from None
    if m[4] is None:                          # date alone: start of the day, or (end) the end of that day
        return datetime.combine(d + timedelta(days=1) if is_end else d, datetime.min.time())
    secs = _seconds(f"{m[4]}:{m[5]}:{m[6] or '00'}", where, allow_24=is_end)
    return datetime.combine(d, datetime.min.time()) + timedelta(seconds=secs)


def _action(raw: dict, where: str) -> str:
    a = raw.get("action")
    if a not in ACTIONS:
        raise ScheduleError(f'{where}: action must be "record" or "pause", not {a!r}')
    return a


def _keys(raw: Any, allowed: set[str], where: str) -> dict:
    if not isinstance(raw, dict):
        raise ScheduleError(f"{where} must be a table")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ScheduleError(f"{where}: unknown key(s): {', '.join(unknown)}")
    missing = sorted(k for k in allowed if k not in raw)
    if missing:
        raise ScheduleError(f"{where}: missing {', '.join(missing)}")
    return raw


def _days(value: Any, where: str) -> tuple[int, ...]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items:
        raise ScheduleError(f'{where}: days must be a list such as ["mon", "tue"] or ["weekdays"]')
    out: set[int] = set()
    full = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    for it in items:
        key = it.strip().lower() if isinstance(it, str) else None
        if key in DAY_GROUPS:
            out.update(DAY_GROUPS[key])
        elif key in DAY_NAMES or key in full:
            out.add(DAY_NAMES.index(key[:3]))
        else:
            raise ScheduleError(f"{where}: unknown day {it!r} (use mon, tue, wed, thu, fri, sat, sun, "
                                "weekdays, weekend or daily)")
    return tuple(sorted(out))


def parse_schedule(raw: Any, default_timezone: str) -> ScheduleConfig:
    """``[schedule]`` table -> validated :class:`ScheduleConfig` (raises :class:`ScheduleError`)."""
    if raw is None:
        return ScheduleConfig(timezone=default_timezone)
    if not isinstance(raw, dict):
        raise ScheduleError("[schedule] must be a table")
    unknown = sorted(set(raw) - {"enabled", "default", "timezone", "weekly", "dates"})
    if unknown:
        raise ScheduleError(f"[schedule] unknown key(s): {', '.join(unknown)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ScheduleError("[schedule] enabled must be true or false")
    default = raw.get("default", RECORD)
    if default not in ACTIONS:
        raise ScheduleError('[schedule] default must be "record" or "pause"')
    tz = raw.get("timezone", "") or default_timezone
    if not isinstance(tz, str):
        raise ScheduleError("[schedule] timezone must be text")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"[schedule] unknown timezone {tz!r}") from None
    weekly_raw, dates_raw = raw.get("weekly", []), raw.get("dates", [])
    if not isinstance(weekly_raw, list) or not isinstance(dates_raw, list):
        raise ScheduleError("[schedule] weekly and dates must be lists of periods ([[schedule.weekly]])")
    if len(weekly_raw) + len(dates_raw) > MAX_PERIODS:
        raise ScheduleError(f"[schedule] has more than {MAX_PERIODS} periods")
    weekly = []
    for i, w in enumerate(weekly_raw, 1):
        where = f"[[schedule.weekly]] #{i}"
        w = _keys(w, {"action", "days", "start", "end"}, where)
        start, end = _seconds(w["start"], where, allow_24=False), _seconds(w["end"], where, allow_24=True)
        if start == end % DAY and end != DAY:
            raise ScheduleError(f"{where}: start and end are the same time (for all day use 00:00 to 24:00)")
        weekly.append(WeeklyPeriod(_action(w, where), _days(w["days"], where), start, end,
                                   f"{where} ({w['action']} {w['start']}-{w['end']})"))
    dates = []
    for i, d in enumerate(dates_raw, 1):
        where = f"[[schedule.dates]] #{i}"
        d = _keys(d, {"action", "start", "end"}, where)
        s, e = _datetime(d["start"], where, is_end=False), _datetime(d["end"], where, is_end=True)
        if e <= s:
            raise ScheduleError(f"{where}: end {d['end']!r} is not after start {d['start']!r}")
        dates.append(DatePeriod(_action(d, where), s, e, f"{where} ({d['action']} {d['start']} to {d['end']})"))
    cfg = ScheduleConfig(enabled, default, tz, tuple(weekly), tuple(dates))
    _check_conflicts(cfg)
    return cfg


def _check_conflicts(cfg: ScheduleConfig) -> None:
    flat = [(s, e, w.action, w.text) for w in cfg.weekly for s, e in w.intervals()]
    for i, (s1, e1, a1, t1) in enumerate(flat):
        for s2, e2, a2, t2 in flat[i + 1:]:
            if a1 != a2 and s1 < e2 and s2 < e1:
                raise ScheduleError(f"{t1} and {t2} overlap but one records and the other pauses; "
                                    "change one so they no longer overlap")
    for i, p in enumerate(cfg.dates):
        for q in cfg.dates[i + 1:]:
            if p.action != q.action and p.start < q.end and q.start < p.end:
                raise ScheduleError(f"{p.text} and {q.text} overlap but one records and the other pauses")
    if not cfg.enabled:
        return
    # Would it ever record? (weekly coverage, ignoring dates that can only add "record" time)
    if any(d.action == RECORD for d in cfg.dates):
        return
    pause = _merge([(s, e) for s, e, a, _ in flat if a == PAUSE])
    record = [(s, e) for s, e, a, _ in flat if a == RECORD]
    if record:
        return
    if cfg.default == PAUSE or pause == [(0, WEEK)]:
        raise ScheduleError("[schedule] never records: add a period with action = \"record\", "
                            "or set enabled = false")


def _merge(iv: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


# ---------------------------------------------------------------------- evaluation
@dataclass(frozen=True)
class ScheduleState:
    record: bool
    source: str                      # "default" | "weekly" | "dates"
    period: str = ""                 # which period decided (technician wording), "" for the default


class RecordingSchedule:
    """Decides, for a UTC epoch, whether the recorder should record (pure; unit-tested)."""

    def __init__(self, cfg: ScheduleConfig) -> None:
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self._weekly = [(s, e, w) for w in cfg.weekly for s, e in w.intervals()]
        self._week_bounds = sorted({b for s, e, _ in self._weekly for b in (s % WEEK, e % WEEK)})

    def state_at(self, epoch: float) -> ScheduleState:
        if not self.cfg.enabled:
            return ScheduleState(True, "default")
        local = datetime.fromtimestamp(epoch, self.tz)
        naive = local.replace(tzinfo=None)
        for d in self.cfg.dates:
            if d.start <= naive < d.end:
                return ScheduleState(d.action == RECORD, "dates", d.text)
        sow = local.weekday() * DAY + local.hour * 3600 + local.minute * 60 + local.second
        for s, e, w in self._weekly:
            if s <= sow < e:
                return ScheduleState(w.action == RECORD, "weekly", w.text)
        return ScheduleState(self.cfg.default == RECORD, "default")

    def should_record(self, epoch: float) -> bool:
        return self.state_at(epoch).record

    def _local_epoch(self, naive: datetime) -> float:
        return naive.replace(tzinfo=self.tz).timestamp()

    def _candidates(self, epoch: float, horizon_days: int) -> list[float]:
        """Moments the decision may change: date period edges, and weekly edges for the next
        8 days from now and from the end of every date period (weekly plans repeat each week)."""
        if not self.cfg.enabled:
            return []
        out: set[float] = set()
        limit = epoch + horizon_days * DAY
        starts = [epoch]
        for d in self.cfg.dates:
            for edge in (d.start, d.end):
                t = self._local_epoch(edge)
                if epoch < t:
                    out.add(t)
                    if edge is d.end:
                        starts.append(t)
        for base in starts:
            if base > limit:
                continue
            day0 = datetime.fromtimestamp(base, self.tz).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
            for k in range(-1, 9):
                day = day0 + timedelta(days=k)
                monday = day - timedelta(days=day.weekday())
                for b in self._week_bounds:
                    t = self._local_epoch(monday + timedelta(seconds=b))
                    if t > epoch:
                        out.add(t)
        return sorted(out)

    def next_change(self, epoch: float, horizon_days: int = 400) -> float | None:
        """First moment after ``epoch`` when recording starts or stops; None if none is scheduled."""
        now = self.should_record(epoch)
        for t in self._candidates(epoch, horizon_days):
            if self.should_record(t) != now:
                return t
        return None

    def plan(self, epoch: float, days: int = 14) -> list[tuple[float, float | None, bool]]:
        """(from, until, record) intervals from ``epoch`` for ``days`` days, for technicians to check."""
        out = []
        t, end = epoch, epoch + days * DAY
        while t < end:
            nxt = self.next_change(t)
            out.append((t, nxt, self.should_record(t)))
            if nxt is None:
                break
            t = nxt
        return out

    def format_local(self, epoch: float) -> str:
        """'Mon 10 Nov 2026 06:00' in the schedule's time zone (operator wording)."""
        return datetime.fromtimestamp(epoch, self.tz).strftime("%a %d %b %Y %H:%M")


def pause_message(schedule: RecordingSchedule, resume_epoch: float | None) -> str:
    """Operator sentence for P-001."""
    if resume_epoch is None:
        return ("Recording is paused by the recording schedule (P-001). No time to resume recording is "
                "scheduled. Contact technical support if recording should be running.")
    return (f"Recording is paused by the recording schedule (P-001). Recording resumes automatically on "
            f"{schedule.format_local(resume_epoch)}. No action is needed.")


def render_plan(schedule: RecordingSchedule, epoch: float, days: int = 14) -> str:
    """Text table of the next ``days`` days (``sentinel.service schedule``)."""
    cfg = schedule.cfg
    if not cfg.enabled:
        return "The recording schedule is off: the recorder records all the time."
    lines = [f"Recording schedule (times in {cfg.timezone}), next {days} days:"]
    for start, until, record in schedule.plan(epoch, days):
        what = "RECORD" if record else "PAUSE "
        lines.append(f"  {what}  from {schedule.format_local(start)}  "
                     f"until {schedule.format_local(until) if until else '(no change scheduled)'}")
    return "\n".join(lines)
