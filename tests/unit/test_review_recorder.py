"""Fixes from the code review of 2026-11-10 (docs/REVIEW-2026-11-10.md): recorder, gaps, status."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from gui_support import base_measurements
from sentinel.clock import parse_iso_utc
from sentinel.db import Ledger
from sentinel.recorder.supervisor import LiveState, RecordingSupervisor
from sentinel.service import seal_age
from sentinel.status import evaluate

def T(hms: str) -> str:
    """Evening times are on 9 Nov, morning times (before 12:00) on 10 Nov."""
    return f"2026-11-{'09' if hms >= '12' else '10'}T{hms}.000000Z"


@pytest.fixture()
def ledger(tmp_path: Path) -> Ledger:
    led = Ledger(tmp_path / "s.db")
    for rid, launched in (("R1", "00:00:00"), ("R2", "06:00:05"), ("R3", "09:00:00")):
        led.create_run(run_id=rid, camera_id="CAM01", directory=f"CAM01/{rid}", launched_utc=T(launched),
                       launch_mono=0.0, display_timezone="UTC", app_version="test")
    yield led
    led.close()


def _gaps(led: Ledger) -> list[tuple[str, str | None, str | None]]:
    return [(g["reason"], g["start_utc"], g["end_utc"]) for g in led.gaps("CAM01")]


# ---------------------------------------------------------------------- H: planned gap absorbing an outage
def test_outage_after_a_pause_is_not_labelled_planned(ledger: Ledger) -> None:
    ledger.begin_gap("CAM01", T("20:00:00"), "scheduled_pause", None, "R1", "segment_end")
    ledger.switch_open_gap("CAM01", T("06:00:00"), "resuming", "recording_resumed")   # schedule: record
    ledger.begin_gap("CAM01", None, "connect_timeout", None, "R2", "x")                      # camera is down
    ledger.close_gap("CAM01", T("09:00:10"), "seg", "R3")                             # footage at 09:00
    assert _gaps(ledger) == [("scheduled_pause", T("20:00:00"), T("06:00:00")),
                             ("connect_timeout", T("06:00:00"), T("09:00:10"))]


def test_normal_resume_leaves_only_seconds_of_resuming(ledger: Ledger) -> None:
    ledger.begin_gap("CAM01", T("20:00:00"), "motion_idle", None, "R1", "segment_end")
    ledger.switch_open_gap("CAM01", T("06:00:00"), "resuming", "recording_resumed")
    ledger.close_gap("CAM01", T("06:00:04"), "seg", "R2")
    assert [r for r, *_ in _gaps(ledger)] == ["motion_idle", "resuming"]
    assert ledger.gaps("CAM01")[-1]["duration_s"] == pytest.approx(4.0)


def test_failure_while_a_planned_gap_is_open_splits_it_at_the_attempt(ledger: Ledger) -> None:
    """Safety net when the switch was not recorded: the failed run's start ends the planned gap."""
    ledger.begin_gap("CAM01", T("20:00:00"), "motion_idle", None, "R1", "segment_end")
    ledger.begin_gap("CAM01", None, "connect_timeout", None, "R2", "x")
    ledger.close_gap("CAM01", T("09:00:10"), "seg", "R3")
    assert _gaps(ledger) == [("motion_idle", T("20:00:00"), T("06:00:05")),
                             ("connect_timeout", T("06:00:05"), T("09:00:10"))]


def test_unplanned_gap_is_never_switched(ledger: Ledger) -> None:
    ledger.begin_gap("CAM01", T("05:00:00"), "connect_timeout", None, "R1", "segment_end")
    assert ledger.switch_open_gap("CAM01", T("06:00:00"), "resuming", "recording_resumed") is None
    assert _gaps(ledger) == [("connect_timeout", T("05:00:00"), None)]


def _supervisor(ledger: Ledger, wall: float, pause: object = None) -> RecordingSupervisor:
    sup = RecordingSupervisor.__new__(RecordingSupervisor)
    sup.cfg = SimpleNamespace(camera=SimpleNamespace(id="CAM01"))
    sup.ledger, sup.schedule, sup.motion = ledger, None, None
    sup.clock = SimpleNamespace(wall=lambda: wall, mono=lambda: 0.0)
    sup.live, sup.stop_event, sup._first_attempt = LiveState(), threading.Event(), True
    sup._pause_reason = lambda: pause  # type: ignore[method-assign]
    return sup


def test_recorder_restarted_during_a_pause_records_the_time_it_was_not_running(ledger: Ledger) -> None:
    ledger.begin_gap("CAM01", T("20:00:00"), "scheduled_pause", None, "R1", "segment_end")
    ledger.add_event("info", "recorder", "x", "last sign of life of the old recorder")
    ledger.query("SELECT 1")
    with ledger.tx() as c:   # put that event at 22:00, then this recorder starts at 23:00
        c.execute("UPDATE events SET ts_utc=? WHERE code='x'", (T("22:00:00"),))
    ledger.add_event("audit", "recorder", "service_start", "started")
    from sentinel.models import RunEndReason

    _supervisor(ledger, parse_iso_utc(T("23:00:00")), RunEndReason.SCHEDULED_PAUSE)._reconcile_gap_at_start()
    assert _gaps(ledger) == [("scheduled_pause", T("20:00:00"), T("22:00:00")),
                             ("recorder_not_running", T("22:00:00"), T("23:00:00")),
                             ("scheduled_pause", T("23:00:00"), None)]


def test_last_sign_of_life_includes_clock_samples(ledger: Ledger) -> None:
    ledger.add_event("info", "recorder", "x", "early event")
    with ledger.tx() as c:
        c.execute("UPDATE events SET ts_utc=? WHERE code='x'", (T("20:00:00"),))
        c.execute("INSERT INTO clock_observations(observed_utc, mono_s, reason, platform) VALUES (?,?,?,?)",
                  (T("04:55:00"), 1.0, "periodic", "test"))
    ledger.add_event("audit", "recorder", "service_start", "started")
    assert ledger.previous_instance_last_event() == T("04:55:00")


# ---------------------------------------------------------------------- H: false R-005 after a resume
def test_seal_deadline_counts_from_the_new_runs_first_media() -> None:
    assert seal_age(10_000.0, 1_000.0, None) == 9_000.0             # no new media: as before
    assert seal_age(10_000.0, 1_000.0, 9_990.0) == 10.0             # media resumed 10 s ago
    assert seal_age(10_000.0, 9_000.0, 8_000.0) == 1_000.0          # older media start: last seal counts


def test_no_r005_right_after_media_resumes() -> None:
    m = base_measurements(seconds_since_last_seal=seal_age(10_000.0, 1_000.0, 9_995.0))
    d = evaluate(m, stall_timeout_s=10, segment_seconds=60, connect_timeout_s=30, escalate_after_s=180)
    assert "R-005" not in [a.code for a in d.alerts]
    m = base_measurements(seconds_since_last_seal=seal_age(10_000.0, 1_000.0, 9_000.0))   # 1000 s, no file
    d = evaluate(m, stall_timeout_s=10, segment_seconds=60, connect_timeout_s=30, escalate_after_s=180)
    assert "R-005" in [a.code for a in d.alerts]                    # a real sealing failure still alarms



# ---------------------------------------------------------------------- M: gap recorded late
def test_gap_recorded_late_ends_at_footage_that_already_exists(ledger: Ledger) -> None:
    """The gap after R1 could not be written in time (ledger busy); R2 then recorded. When the gap
    is written later (janitor retry or startup recovery) it must end where R2's footage starts."""
    from sentinel.recorder.finalizer import Finalizer, GapJob

    s1 = ledger.insert_segment(camera_id="CAM01", run_id="R1", seq=0, rel_path="CAM01/R1/seg_000000.ts")
    ledger.update_segment(s1, start_utc=T("21:00:00"), end_utc=T("21:05:00"))
    s2 = ledger.insert_segment(camera_id="CAM01", run_id="R2", seq=0, rel_path="CAM01/R2/seg_000000.ts")
    ledger.update_segment(s2, start_utc=T("06:00:09"), end_utc=T("06:05:09"))
    with ledger.tx() as c:
        c.execute("UPDATE recording_runs SET ended_utc=?, end_reason='ffmpeg_error' WHERE run_id='R1'",
                  (T("21:05:00"),))
    fin = Finalizer.__new__(Finalizer)
    fin.ledger = ledger
    fin.process_gap(GapJob("R1", "ffmpeg_error", {}))
    assert _gaps(ledger) == [("ffmpeg_error", T("21:05:00"), T("06:00:09"))]
    assert ledger.open_gap("CAM01") is None


def test_gap_jobs_are_not_queued_twice() -> None:
    import queue

    from sentinel.recorder.finalizer import Finalizer, GapJob

    fin = Finalizer.__new__(Finalizer)
    fin._qlock, fin._queued, fin._q = threading.Lock(), set(), queue.Queue()
    fin.submit(GapJob("R1", "stall", {}))
    fin.submit(GapJob("R1", "stall", {}))
    assert fin._q.qsize() == 1 and fin.gap_queued("R1")


def test_late_gap_ignores_footage_dated_before_it(ledger: Ledger) -> None:
    """A later-created run may hold older footage (imported backlog, clock change): it must not end
    a gap that starts after it (found by the archive backlog integration test)."""
    from sentinel.recorder.finalizer import Finalizer, GapJob

    s1 = ledger.insert_segment(camera_id="CAM01", run_id="R1", seq=0, rel_path="CAM01/R1/seg_000000.ts")
    ledger.update_segment(s1, start_utc=T("21:00:00"), end_utc=T("21:05:00"))
    s2 = ledger.insert_segment(camera_id="CAM01", run_id="R2", seq=0, rel_path="CAM01/R2/seg_000000.ts")
    ledger.update_segment(s2, start_utc=T("20:00:00"), end_utc=T("20:05:00"))     # older than R1's end
    fin = Finalizer.__new__(Finalizer)
    fin.ledger = ledger
    fin.process_gap(GapJob("R1", "graceful_stop", {}))
    assert _gaps(ledger) == [("graceful_stop", T("21:05:00"), None)]


def test_log_check_allows_only_the_gap_notes_and_resuming_reason() -> None:
    from sentinel.ledgerchain import illegal_changes

    gap = {"gap_id": 1, "reason": "scheduled_pause", "detail": '{"exit_code": 0}', "closed_utc": None,
           "end_utc": None, "attempts": 1}
    ok = dict(gap, detail='{"exit_code": 0, "ended_by": "recording_resumed"}', end_utc="x", closed_utc="y")
    assert illegal_changes("recording_gaps", gap, ok) == []
    assert illegal_changes("recording_gaps", gap, dict(gap, detail='{"exit_code": 1}'))            # rewritten
    assert illegal_changes("recording_gaps", gap, dict(gap, detail='{"exit_code": 0, "x": "y"}'))  # other key
    assert illegal_changes("recording_gaps", gap, dict(gap, reason="connect_timeout"))             # planned → other
    resuming = dict(gap, reason="resuming")
    assert illegal_changes("recording_gaps", resuming, dict(resuming, reason="connect_timeout")) == []
    closed = dict(ok)
    assert illegal_changes("recording_gaps", closed, dict(closed, detail='{"exit_code": 0, "ended_by": "z"}'))
