"""Read-only views of the ledger for the GUI.

Every function opens ``Ledger(path, readonly=True)`` (SQLite ``mode=ro``), runs SELECTs in
autocommit mode (so no read transaction is left open to hold back the recorder's checkpoints),
and closes the connection again. A connection is opened per refresh, so a ledger that appears
after the GUI started, or is briefly busy, is handled without special cases.

Pure builders (``build_timeline``) are separated from queries so they can be unit-tested.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..clock import iso_utc, parse_iso_utc
from ..config import LEDGER_SNAPSHOT_NAME
from ..db import Ledger

# Plain-language reason for a missing interval, keyed by the ledger's gap reason.
GAP_REASON_WORDS: dict[str, str] = {
    "graceful_stop": "recording service was shut down",
    "source_ended": "camera stopped sending video",
    "ffmpeg_error": "camera disconnected",
    "ffmpeg_killed": "recording was interrupted",
    "stall": "camera video stopped",
    "connect_timeout": "could not connect to camera",
    "storage_critical": "storage full",
    "service_interrupted": "recorder stopped unexpectedly, for example a power cut",
    "launch_failed": "recording could not start",
    "timeline_discontinuity": "camera video froze",
    "internal_error": "recording software problem",
    "scheduled_pause": "paused by the recording schedule (planned)",
    "motion_idle": "no motion outside recording hours (motion-triggered recording; planned)",
    "source_discontinuity_absorbed": "camera video froze",
    "timestamp_discontinuity": "break in the camera video",
    "timestamp_discontinuity_at_boundary": "break in the camera video",
    "corrupt_segment": "damaged recording",
    "segment_file_missing": "recording missing",
}
UNKNOWN_REASON = "reason not recorded"
NO_INFO_REASON = "no record of the system running"


def reason_words(reason: str | None) -> str:
    return GAP_REASON_WORDS.get(reason or "", UNKNOWN_REASON)


@contextmanager
def open_ledger(db_path: Path) -> Iterator[Ledger | None]:
    """Yield a read-only ledger, or None if it does not exist or cannot be opened.

    Integration (Windows service layout): when the GUI reads the operator snapshot in
    ``[storage] status_dir`` (``ledger-snapshot.db``, rollback journal, replaced atomically by
    the recorder), it is opened ``mode=ro&immutable=1`` so an operator account with read-only
    access never needs to create -wal/-shm files there."""
    if not db_path.exists():
        yield None
        return
    try:
        led = Ledger(db_path, readonly=True, immutable=db_path.name == LEDGER_SNAPSHOT_NAME)
    except sqlite3.Error:
        yield None
        return
    try:
        yield led
    finally:
        led.close()


def _ep(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return parse_iso_utc(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------- dashboard snapshot
@dataclass(frozen=True)
class LedgerSnapshot:
    available: bool
    last_saved_epoch: float | None = None
    last_gap_end_epoch: float | None = None
    first_segment_epoch: float | None = None
    error: str | None = None             # administrator only


def snapshot(db_path: Path, camera_id: str) -> LedgerSnapshot:
    try:
        with open_ledger(db_path) as led:
            if led is None:
                return LedgerSnapshot(False)
            row = led.latest_sealed_segment(camera_id)
            last = _ep(row["end_utc"]) if row is not None else None
            gaps = led.query("SELECT end_utc FROM recording_gaps WHERE camera_id=? AND end_utc IS NOT NULL "
                             "ORDER BY gap_id DESC LIMIT 20", (camera_id,))
            ends = [e for e in (_ep(g["end_utc"]) for g in gaps) if e is not None]
            first = led.query("SELECT start_utc FROM segments WHERE camera_id=? AND start_utc IS NOT NULL "
                              "ORDER BY rowid LIMIT 1", (camera_id,))
            return LedgerSnapshot(True, last, max(ends) if ends else None,
                                  _ep(first[0]["start_utc"]) if first else None)
    except sqlite3.Error as exc:
        return LedgerSnapshot(False, error=str(exc))


def recording_since(snap: LedgerSnapshot) -> float | None:
    """Start of the current uninterrupted stretch: the end of the latest gap, else the first footage."""
    return snap.last_gap_end_epoch or snap.first_segment_epoch


# --------------------------------------------------------------------------- timeline
class SpanKind(StrEnum):
    RECORDED = "recorded"
    GAP = "gap"              # no recording, with a reason from the ledger
    DAMAGED = "damaged"      # recorded but the segment is unreadable
    LIVE = "live"            # being recorded right now, not yet saved
    NO_INFO = "no_info"      # nothing in the ledger covers this time


@dataclass(frozen=True)
class Span:
    start: float
    end: float
    kind: SpanKind
    reason: str = ""         # plain words
    ongoing: bool = False

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class Timeline:
    window_start: float
    window_end: float
    spans: tuple[Span, ...] = ()
    available: bool = True
    motion: tuple[tuple[float, float], ...] = ()     # motion periods (reviewer markers), clipped to the window

    @property
    def recorded_s(self) -> float:
        return sum(s.seconds for s in self.spans if s.kind in (SpanKind.RECORDED, SpanKind.LIVE))

    @property
    def interruptions(self) -> list[Span]:
        return [s for s in self.spans if s.kind in (SpanKind.GAP, SpanKind.DAMAGED)]

    @property
    def missing_s(self) -> float:
        return sum(s.seconds for s in self.interruptions)


def _merge(iv: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(iv):
        if b < a:
            continue
        if out and a <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _subtract(iv: list[tuple[float, float]], cut: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result = iv
    for c0, c1 in cut:
        nxt: list[tuple[float, float]] = []
        for a, b in result:
            if c1 <= a or c0 >= b:
                nxt.append((a, b))
                continue
            if a < c0:
                nxt.append((a, c0))
            if c1 < b:
                nxt.append((c1, b))
        result = nxt
    return result


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def build_timeline(segments: list[dict[str, Any]], gaps: list[dict[str, Any]], window_start: float,
                   now: float, live: bool, live_slack_s: float, merge_tol_s: float = 2.0,
                   min_gap_s: float = 1.0) -> Timeline:
    """Recorded spans vs. gaps in [window_start, now].

    * recorded = union of segment intervals (corrupt segments are "damaged"), minus closed
      gap rows that lie inside them (within-segment gaps);
    * every uncovered interval becomes a gap; its reason is the ledger gap row overlapping it most;
    * an uncovered tail ending at ``now`` while the recorder reports recording, with no open gap,
      and shorter than ``live_slack_s`` is "being recorded" (the current segment is not saved yet);
    * uncovered time no gap row explains is "no information" (never presented as recorded).
    """
    def clip(a: float, b: float) -> tuple[float, float] | None:
        a, b = max(a, window_start), min(b, now)
        return (a, b) if b > a else None

    good: list[tuple[float, float]] = []
    bad: list[tuple[float, float]] = []
    for s in segments:
        a, b = _ep(s.get("start_utc")), _ep(s.get("end_utc"))
        if a is None or b is None:
            continue
        c = clip(a, b)
        if c is None:
            continue
        (bad if s.get("state") == "CORRUPT" else good).append(c)

    gap_iv: list[tuple[float, float, str, bool]] = []    # start, end, reason, open
    for g in gaps:
        a = _ep(g.get("start_utc"))
        b = _ep(g.get("end_utc"))
        is_open = g.get("end_utc") is None
        if a is None:
            continue
        if b is None:
            b = now if is_open else a
        c = clip(a, b)
        if c is not None:
            gap_iv.append((c[0], c[1], str(g.get("reason") or ""), is_open))

    damaged = _merge(bad, merge_tol_s)
    recorded = _merge(good, merge_tol_s)
    inner_cuts = [(a, b) for a, b, _r, op in gap_iv if not op and b - a >= min_gap_s]
    recorded = _subtract(recorded, inner_cuts)
    damaged = _subtract(damaged, recorded)

    covered = _merge(recorded + damaged, 0.0)
    holes: list[tuple[float, float]] = []
    cursor = window_start
    for a, b in covered:
        if a > cursor:
            holes.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < now:
        holes.append((cursor, now))

    spans: list[Span] = [Span(a, b, SpanKind.RECORDED) for a, b in recorded]
    spans += [Span(a, b, SpanKind.DAMAGED, reason_words("corrupt_segment")) for a, b in damaged]
    has_open_gap = any(op for *_x, op in gap_iv)
    for a, b in holes:
        if b - a < min_gap_s and b < now:
            continue
        best, best_ov, best_open = None, 0.0, False
        for g0, g1, reason, op in gap_iv:
            ov = _overlap(a, b, g0, g1)
            if ov > best_ov or (op and ov > 0 and ov >= best_ov):
                best, best_ov, best_open = reason, ov, op
        tail = b >= now - 1e-6
        if tail and live and not has_open_gap and (b - a) <= live_slack_s:
            spans.append(Span(a, b, SpanKind.LIVE, "being recorded now", ongoing=True))
        elif best is not None:
            spans.append(Span(a, b, SpanKind.GAP, reason_words(best), ongoing=tail and best_open))
        else:
            spans.append(Span(a, b, SpanKind.NO_INFO, NO_INFO_REASON if a <= window_start + 1e-6 else UNKNOWN_REASON,
                              ongoing=tail))
    spans.sort(key=lambda s: s.start)
    return Timeline(window_start, now, tuple(spans))


def motion_periods(rows: list[dict[str, Any]], camera_id: str, window_start: float, now: float) -> tuple[tuple[float, float], ...]:
    """``motion_period`` event details -> (start, end) epochs clipped to [window_start, now]."""
    out = []
    for r in rows:
        try:
            d = json.loads(r.get("detail") or "{}")
        except ValueError:
            continue
        if d.get("camera_id", camera_id) != camera_id:
            continue
        a, b = _ep(d.get("start_utc")), _ep(d.get("end_utc"))
        if a is None or b is None:
            continue
        a, b = max(a, window_start), min(b, now)
        if b >= a:
            out.append((a, b))
    return tuple(sorted(out))


def load_timeline(db_path: Path, camera_id: str, now: float, *, hours: float = 24.0, live: bool = False,
                  live_slack_s: float = 600.0) -> Timeline:
    start = now - hours * 3600.0
    lo, hi = iso_utc(start), iso_utc(now)
    try:
        with open_ledger(db_path) as led:
            if led is None:
                return Timeline(start, now, (), available=False)
            segs = [dict(r) for r in led.query(
                "SELECT start_utc, end_utc, state FROM segments WHERE camera_id=? AND start_utc IS NOT NULL "
                "AND end_utc IS NOT NULL AND end_utc >= ? AND start_utc <= ?", (camera_id, lo, hi))]
            gaps = [dict(r) for r in led.query(
                "SELECT start_utc, end_utc, reason, kind FROM recording_gaps WHERE camera_id=? "
                "AND (end_utc IS NULL OR end_utc >= ?) AND (start_utc IS NULL OR start_utc <= ?)",
                (camera_id, lo, hi))]
            motion = [dict(r) for r in led.query(
                "SELECT detail FROM events WHERE code='motion_period' AND ts_utc >= ?", (iso_utc(start - 600.0),))]
    except sqlite3.Error:
        return Timeline(start, now, (), available=False)
    tl = build_timeline(segs, gaps, start, now, live, live_slack_s)
    return replace(tl, motion=motion_periods(motion, camera_id, start, now))


# --------------------------------------------------------------------------- previous exports
# Matches the export branch's ``export_batches`` (state PLANNED|COPYING|VERIFYING|MANIFEST|EJECTING|
# COMPLETE|FAILED|INTERRUPTED, finished_utc/updated_utc/planned_utc, drive_id -> drives.label,
# error_code E-0xx) and tolerates a few generic column names. Ledger strings are NEVER shown
# directly: every state maps to a fixed plain-language outcome (unknown states included).
EXPORT_TABLE_CANDIDATES = ("export_batches", "exports", "export_jobs")
_TIME_COLS = ("finished_utc", "completed_utc", "ended_utc", "updated_utc", "planned_utc", "created_utc",
              "started_utc")
_STATE_COLS = ("state", "status")
_DRIVE_COLS = ("drive_label", "label", "drive_name")
_FROM_COLS = ("interval_start_utc", "from_utc", "range_start_utc")
_TO_COLS = ("interval_end_utc", "to_utc", "range_end_utc")
OUTCOME_COMPLETED = "Completed"
OUTCOME_FAILED = "Did not complete — needs attention"
OUTCOME_INTERRUPTED = "Interrupted — the drive was removed before it finished"
OUTCOME_REJECTED = "The drive could not be used"
OUTCOME_NOT_FINISHED = "Not finished"
OUTCOME_UNKNOWN = "Result not recognised by this screen"
EXPORT_STATE_WORDS: dict[str, str] = {
    "COMPLETE": OUTCOME_COMPLETED, "COMPLETED": OUTCOME_COMPLETED,
    "FAILED": OUTCOME_FAILED, "FAILED_ATTENTION": OUTCOME_FAILED,
    "INTERRUPTED": OUTCOME_INTERRUPTED,
    "REJECTED": OUTCOME_REJECTED,
    **{s: OUTCOME_NOT_FINISHED for s in ("PLANNED", "PLANNING", "CHECKING", "READY", "COPYING", "VERIFYING",
                                          "MANIFEST", "EJECTING")},
}
EXPORT_OUTCOMES = frozenset(EXPORT_STATE_WORDS.values()) | {OUTCOME_UNKNOWN}
_CODE_RE = re.compile(r"^[A-Z]-\d{3}$")


def export_outcome(state: object) -> str:
    return EXPORT_STATE_WORDS.get(str(state or "").strip().upper(), OUTCOME_UNKNOWN)


@dataclass(frozen=True)
class ExportRow:
    when_epoch: float | None
    outcome: str                         # always one of EXPORT_OUTCOMES
    drive: str | None = None
    from_epoch: float | None = None
    to_epoch: float | None = None
    error_code: str | None = None        # operator code such as "E-005", only if well-formed


@dataclass(frozen=True)
class ExportList:
    table: str | None                    # None => the ledger has no export records at all
    rows: tuple[ExportRow, ...] = ()
    readable: bool = True                # False => records exist but this version cannot interpret them
    available: bool = True               # ledger could be opened


def _first(cols: set[str], options: tuple[str, ...]) -> str | None:
    return next((c for c in options if c in cols), None)


def load_exports(db_path: Path, limit: int = 200) -> ExportList:
    """List previous review-drive exports, if the export feature has created its tables.

    If export records exist that this reader cannot interpret it says so instead of guessing.
    """
    try:
        with open_ledger(db_path) as led:
            if led is None:
                return ExportList(None, available=False)
            names = {r["name"] for r in led.query("SELECT name FROM sqlite_master WHERE type='table'")}
            table = next((t for t in EXPORT_TABLE_CANDIDATES if t in names), None)
            if table is None:
                if any("export" in n.lower() for n in names):
                    return ExportList(next(n for n in names if "export" in n.lower()), readable=False)
                return ExportList(None)
            cols = {r["name"] for r in led.query(f"PRAGMA table_info({table})")}
            tcols = [c for c in _TIME_COLS if c in cols]
            scol = _first(cols, _STATE_COLS)
            if not tcols or scol is None:
                return ExportList(table, readable=False)
            dcol, fcol, ocol = _first(cols, _DRIVE_COLS), _first(cols, _FROM_COLS), _first(cols, _TO_COLS)
            labels: dict[str, str] = {}
            if dcol is None and "drive_id" in cols and "drives" in names:
                dcols = {r["name"] for r in led.query("PRAGMA table_info(drives)")}
                if {"drive_id", "label"} <= dcols:
                    labels = {str(r["drive_id"]): str(r["label"]) for r in
                              led.query("SELECT drive_id, label FROM drives WHERE label IS NOT NULL")}
            rows = led.query(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,))
            out = []
            for r in rows:
                when = next((e for e in (_ep(r[c]) for c in tcols) if e is not None), None)
                drive = (str(r[dcol]) if dcol and r[dcol] else
                         labels.get(str(r["drive_id"])) if labels and r["drive_id"] is not None else None)
                code = r["error_code"] if "error_code" in cols else None
                out.append(ExportRow(
                    when_epoch=when, outcome=export_outcome(r[scol]), drive=drive,
                    from_epoch=_ep(r[fcol]) if fcol else None, to_epoch=_ep(r[ocol]) if ocol else None,
                    error_code=code if isinstance(code, str) and _CODE_RE.fullmatch(code) else None))
            return ExportList(table, tuple(out))
    except sqlite3.Error:
        return ExportList(None, available=False)


# --------------------------------------------------------------------------- administrator data
@dataclass
class AdminData:
    available: bool
    events: list[dict[str, Any]] = field(default_factory=list)
    open_gaps: list[dict[str, Any]] = field(default_factory=list)
    recent_gaps: list[dict[str, Any]] = field(default_factory=list)
    state_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def load_admin(db_path: Path, camera_id: str, events: int = 200) -> AdminData:
    try:
        with open_ledger(db_path) as led:
            if led is None:
                return AdminData(False, error="ledger not found or cannot be opened")
            ev = [dict(r) for r in led.query(
                "SELECT event_id, ts_utc, severity, category, code, message, detail FROM events "
                "ORDER BY event_id DESC LIMIT ?", (events,))]
            og = [dict(r) for r in led.query(
                "SELECT * FROM recording_gaps WHERE camera_id=? AND end_utc IS NULL ORDER BY gap_id DESC",
                (camera_id,))]
            rg = [dict(r) for r in led.query(
                "SELECT gap_id, kind, start_utc, end_utc, duration_s, reason, attempts, clock_anomaly "
                "FROM recording_gaps WHERE camera_id=? ORDER BY gap_id DESC LIMIT 50", (camera_id,))]
            return AdminData(True, ev, og, rg, led.state_counts())
    except sqlite3.Error as exc:
        return AdminData(False, error=str(exc))


def pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)
