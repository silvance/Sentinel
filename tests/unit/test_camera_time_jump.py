"""A camera whose time labels leap ahead (0.3.28).

Field report 2026-11-20, from two recordings the owner brought from the laptop (KTNC KNC-HMSS347): about 7 s
after a connection starts, the timestamps in the stream leap forward once, by hours, and run on smoothly. No
picture is missing. Until 0.3.28 the run's anchor followed the later labels, so the first seconds were logged
hours before the recorder had even been started, with a gap that never happened.

The numbers of the second recording (run 20261120T030714Z, launched 03:07:14.27 UTC, 16 s long) are used as
they are: 483 packets, timestamps 1.400 .. 9.137, then 11992.738 .. 12000.904."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import make_config

from sentinel.clock import parse_iso_utc
from sentinel.db import Ledger
from sentinel.media.probe import ProbeResult
from sentinel.recorder.finalizer import Finalizer
from sentinel.recorder.segment_list import SegmentEntry, on_one_timeline
from sentinel.recorder.timebase import RELABEL_EVENT, RELABEL_MIN_S, RunTimebase, StreamRelabel
from sentinel.storage.monitor import StorageMonitor

LAUNCHED = "2026-11-20T03:07:14.270969Z"
LEAP = 11983.601                      # 9.137 -> 11992.738 in the file
STEP = (12000.904422 - 1.4 - LEAP) / 481
AHEAD = LEAP - STEP                   # what the labels are ahead by; the two pictures are one step apart
REAL = 11999.537756 - AHEAD           # 15.97 s of footage


def laptop_file() -> ProbeResult:
    res = ProbeResult(True)
    res.duration_s, res.first_pts_s, res.last_pts_s = 11999.537756, 1.4, 12000.904422
    res.packet_count, res.keyframe_count, res.max_pts_gap_s = 483, 17, LEAP
    res.pts_gaps = [(7.737, 11991.338)]
    res.warnings = ["internal_timestamp_gap"]
    return res


# ------------------------------------------------------------------ the recorder's one timeline
def _reports(leap_at: int = 8, leap_s: float = LEAP, n: int = 16) -> list[tuple[float, float]]:
    """(raw stream time, monotonic time) once a second; from report ``leap_at`` on the labels are ahead."""
    return [(float(i) + (leap_s if i >= leap_at else 0.0), 1000.0 + i + 0.1) for i in range(1, n + 1)]


def test_a_leap_of_the_labels_is_taken_out_of_the_stream_time() -> None:
    r, tb = StreamRelabel(), RunTimebase(wall_launch=parse_iso_utc(LAUNCHED), mono_launch=1000.0)
    seen = []
    for raw, mono in _reports():
        leapt = r.observe(raw, mono)
        if leapt is not None:
            seen.append(leapt)
        tb.observe(r.norm(raw), mono)
    assert seen == [pytest.approx(LEAP)] and r.active and r.at_stream_s == 7.0
    assert [r.norm(raw) for raw, _m in _reports()] == pytest.approx([float(i) for i in range(1, 17)])
    # The anchor stays where reception puts it: just after the launch, not three hours and twenty minutes before.
    assert tb.anchor_epoch == pytest.approx(parse_iso_utc(LAUNCHED) + 0.1)
    # Without it (0.3.27): the smallest "reception minus stream time" wins, and that is the later labels'.
    old = RunTimebase(wall_launch=parse_iso_utc(LAUNCHED), mono_launch=1000.0)
    for raw, mono in _reports():
        old.observe(raw, mono)
    assert parse_iso_utc(LAUNCHED) - old.anchor_epoch == pytest.approx(LEAP - 0.1)


def test_catching_up_and_freezes_are_not_leaps() -> None:
    r = StreamRelabel()
    # A new connection gets the pictures since the camera's last key picture at once: 20 s of stream in 1 s.
    assert r.observe(0.5, 10.0) is None and r.observe(20.5, 11.0) is None
    # The network stalls for 90 s and the stream catches up in a burst: stream time never gets ahead of real time.
    assert r.observe(21.5, 12.0) is None and r.observe(111.4, 102.0) is None
    # The camera freezes for two minutes (reports go on, the stream time stands still), then resumes two minutes on.
    for i in range(1, 120):
        assert r.observe(111.4, 102.0 + i) is None
    assert r.observe(232.0, 222.5) is None and not r.active
    # Less than a minute ahead is left alone; a minute or more is a leap.
    assert r.observe(232.0 + 1.0 + RELABEL_MIN_S - 1.0, 223.5) is None
    assert r.observe(233.0 + RELABEL_MIN_S - 1.0 + 1.0 + RELABEL_MIN_S, 224.5) == pytest.approx(RELABEL_MIN_S)


def test_one_leap_a_run_and_its_log_entry_rebuilds_it() -> None:
    r = StreamRelabel()
    for raw, mono in _reports():
        r.observe(raw, mono)
    assert r.observe(16.0 + LEAP + 5000.0, 1017.1) is None          # a second one is left as it is
    assert r.shift_s == pytest.approx(LEAP)
    again = StreamRelabel.from_detail(json.loads(json.dumps(r.detail())))
    assert again.active and again.norm(12000.0) == pytest.approx(r.norm(12000.0)) and again.norm(5.0) == 5.0
    assert not StreamRelabel.from_detail({"run_id": "x"}).active and not StreamRelabel.from_detail(None).active
    # FFmpeg's list of closed files is read on the same timeline, whenever the leap became known.
    entry = SegmentEntry("seg_000000.ts", 0.0, 11999.537756)
    assert on_one_timeline(entry, StreamRelabel()) is entry and on_one_timeline(entry, None) is entry
    one = on_one_timeline(entry, r)
    assert one.start_s == 0.0 and one.end_s == pytest.approx(11999.537756 - LEAP)


# ------------------------------------------------------------------ sealing a file that holds the leap
class Sealing:
    def __init__(self, tmp: Path, anchor: str | None, listed: tuple[float, float] | None, launched: str = LAUNCHED,
                 seq: int = 0, ended: str | None = None) -> None:
        self.cfg = make_config(tmp)
        self.led = Ledger(tmp / "l.db")
        self.fin = Finalizer(self.cfg, self.led, None, StorageMonitor(tmp, 0, 0), None)  # type: ignore[arg-type]
        cols: dict[str, Any] = {} if anchor is None else {"anchor_utc": anchor}
        if ended:
            cols.update(ended_utc=ended, end_reason="service_interrupted")
        self.led.create_run(run_id="r", camera_id="CAM01", directory="CAM01/r", launched_utc=launched, launch_mono=1.0,
                            display_timezone="UTC", app_version="t", time_method="reception_tracking_v1", **cols)
        seg: dict[str, Any] = {}
        if listed is not None:
            seg = dict(stream_start_s=listed[0], stream_end_s=listed[1], time_correction_start_s=0.0,
                       time_correction_end_s=0.0, time_correction_stream_s=listed[1])
        self.rel = f"CAM01/r/seg_{seq:06d}.ts"
        self.seg = self.led.insert_segment(camera_id="CAM01", run_id="r", seq=seq, rel_path=self.rel, **seg)

    def times(self, res: ProbeResult, interrupted: bool = False):
        warnings = list(res.warnings)
        try:
            out = self.fin._timestamps(self.led.get_segment(self.seg), self.led.get_run("r"), res, interrupted, warnings)
        finally:
            self.led.close()
        return (*out, warnings)


def test_a_file_with_the_leap_is_timed_by_the_recorders_one_timeline(tmp_path: Path) -> None:
    """The recorder saw the leap: the listed times are continuous, the anchor is from just after the launch."""
    anchor = "2026-11-20T03:07:14.400000Z"
    s = Sealing(tmp_path, anchor, (0.0, REAL - 0.02))               # the recorder's measure is a little off the file's
    start, end, source, gaps, _cols, footage, warnings = s.times(laptop_file())
    assert start == anchor and parse_iso_utc(end) - parse_iso_utc(start) == pytest.approx(REAL - 0.02)
    assert source == "run_anchor" and gaps == []                    # no time passed at the leap: not a gap
    assert footage["rule"] == "listed" and footage["duration_s"] == pytest.approx(REAL)
    assert footage["leaps"] == [(7.737, 11991.338, pytest.approx(AHEAD))]
    assert "camera_time_jump" in warnings and "internal_timestamp_gap" in warnings
    assert "duration_mismatch" not in warnings and "timestamp_gap_at_segment_end" not in warnings


def test_a_file_the_recorder_did_not_see_leap_cannot_start_before_its_run(tmp_path: Path) -> None:
    """What the laptop logged with 0.3.23: anchor 23:47:30, three hours twenty before the launch. The leap in the
    file explains it; the end was right all along."""
    s = Sealing(tmp_path, "2026-11-19T23:47:30.801275Z", (0.0, 11999.537756))
    start, end, _source, gaps, _cols, footage, warnings = s.times(laptop_file())
    assert end == "2026-11-20T03:07:30.339031Z"                    # as the laptop had it
    assert parse_iso_utc(start) - parse_iso_utc(LAUNCHED) == pytest.approx(0.098, abs=0.01)
    assert parse_iso_utc(end) - parse_iso_utc(start) == pytest.approx(REAL)
    assert gaps == [] and footage["rule"] == "launch" and footage["duration_s"] == pytest.approx(REAL)
    assert "camera_time_jump" in warnings


def test_a_file_cut_off_by_a_crash_is_timed_from_its_last_write_and_its_real_length(tmp_path: Path) -> None:
    """The recorder was killed with this file open and nothing listed (the laptop's run of 19 Nov 12:19): the
    time comes from the file's last write. Its own span would put its start hours before the launch."""
    s = Sealing(tmp_path, None, None, ended="2026-11-20T03:07:30.400000Z")
    path = s.cfg.spool_path / s.rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x47" * 188)
    written = parse_iso_utc("2026-11-20T03:07:30.400000Z")
    os.utime(path, (written, written))
    start, end, source, gaps, _cols, footage, warnings = s.times(laptop_file(), interrupted=True)
    assert source == "file_mtime" and parse_iso_utc(end) == pytest.approx(written)
    assert parse_iso_utc(end) - parse_iso_utc(start) == pytest.approx(REAL)
    assert parse_iso_utc(start) >= parse_iso_utc(LAUNCHED) and footage["rule"] == "launch"
    assert {"time_from_file_mtime", "camera_time_jump"} <= set(warnings)


def test_time_that_really_passed_is_still_a_gap(tmp_path: Path) -> None:
    """A camera that stopped sending for two minutes: the listed span has room for the break, and the footage
    starts after the launch. Nothing is taken for a leap."""
    res = ProbeResult(True)
    res.duration_s, res.first_pts_s, res.last_pts_s = 300.0, 1.4, 301.3
    res.packet_count, res.keyframe_count = 1801, 150
    res.pts_gaps, res.warnings = [(100.0, 220.0)], ["internal_timestamp_gap"]
    s = Sealing(tmp_path, "2026-11-20T03:07:14.400000Z", (0.0, 300.0))
    start, _end, _source, gaps, _cols, footage, warnings = s.times(res)
    assert len(gaps) == 1 and gaps[0].duration_s == pytest.approx(120.0) and gaps[0].reason == "timestamp_discontinuity"
    assert parse_iso_utc(gaps[0].start_utc) - parse_iso_utc(start) == pytest.approx(100.0)
    assert footage["leaps"] == [] and footage["duration_s"] == 300.0 and "camera_time_jump" not in warnings


def test_a_real_gap_after_the_leap_keeps_its_place(tmp_path: Path) -> None:
    """10 s of footage, the leap of an hour, 10 s more, the camera stops for 30 s, 10 s more."""
    res = ProbeResult(True)
    res.duration_s, res.first_pts_s, res.last_pts_s = 3660.1, 0.0, 3660.0
    res.packet_count, res.keyframe_count = 303, 30                  # 300 steps of 0.1 s outside the two breaks
    res.pts_gaps, res.warnings = [(10.0, 3610.0), (3620.0, 3650.0)], ["internal_timestamp_gap"]
    s = Sealing(tmp_path, "2026-11-20T03:07:14.400000Z", (0.0, 60.2), seq=1)
    start, end, _source, gaps, _cols, footage, _warnings = s.times(res)
    assert footage["leaps"] == [(10.0, 3610.0, pytest.approx(3599.9))] and footage["duration_s"] == pytest.approx(60.2)
    assert len(gaps) == 1 and gaps[0].duration_s == pytest.approx(30.0)
    assert parse_iso_utc(gaps[0].start_utc) - parse_iso_utc(start) == pytest.approx(20.1)      # 3620 less the leap
    assert parse_iso_utc(end) - parse_iso_utc(start) == pytest.approx(60.2)


def test_startup_recovery_finds_the_leap_the_recorder_logged(tmp_path: Path) -> None:
    from sentinel.recorder.recovery import Recovery

    led = Ledger(tmp_path / "l.db")
    try:
        r = StreamRelabel()
        for raw, mono in _reports():
            r.observe(raw, mono)
        led.add_event("warning", "recorder", RELABEL_EVENT, "x", {"run_id": "other", **StreamRelabel().detail()})
        led.add_event("warning", "recorder", RELABEL_EVENT, "x", {"run_id": "r", **r.detail()})
        rec = Recovery.__new__(Recovery)
        rec.ledger = led
        found = rec._run_relabel("r")
        assert found is not None and found.norm(11999.537756) == pytest.approx(11999.537756 - LEAP)
        assert rec._run_relabel("other") is None and rec._run_relabel("none") is None
    finally:
        led.close()
