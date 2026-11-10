"""Unit tests for drift-corrected segment times, clock observations and OS time-sync parsing
(requirement E1, docs/03 §4)."""

from __future__ import annotations

import json
import random
import sqlite3
import zipfile
from pathlib import Path

import pytest

from conftest import make_config
from sentinel.db import Ledger
from sentinel.ledger_timing import (RUN_COLUMNS, SEGMENT_COLUMNS, TIMING_SCHEMA_KEY, existing_columns,
                                    insert_clock_observation)
from sentinel.recorder.timebase import (NON_DRIFT_ALLOWANCE_S, ClockStepDetector, DriftTracker,
                                        interpolate_correction)
from sentinel.timesync import (CommandResult, parse_chronyc_tracking, parse_sc_state, parse_timedatectl_show,
                               parse_w32tm_status, query_linux, query_windows)


# ---------------------------------------------------------------- simulation helper
def _simulate(days: float, camera_ppm: float, host_ppm: float, seg_s: float = 300.0, seed: int = 1,
              jump: tuple[float, float] | None = None, max_ppm: float = 1000.0):
    """Synthetic run: progress report every 1 s of host time with 0-80 ms reporting jitter and
    occasional 1-3 s latency spikes. Returns (tracker, knots, truth) where truth[i] is the true
    correction (reception wall time minus single-anchor time) at knot i."""
    rng = random.Random(seed)
    tr = DriftTracker(max_ppm)
    steps = ClockStepDetector(wall=1_000_000.0, mono=0.0, tolerance_s=2.0)
    cam = camera_ppm * 1e-6
    host = host_ppm * 1e-6
    anchor = 0.0                       # reception offset (m - t) at t = 0, by construction
    lost = 0.0                         # real time absorbed by the source (jump)
    knots, truth = [], []
    next_knot = seg_s
    m = 0.0
    end = days * 86400
    while m < end:
        m += 1.0
        if jump is not None and m >= jump[0] and lost == 0.0:
            lost = jump[1]
        t = (m - lost) * (1 + cam)                     # stream time of the latest received packet
        latency = rng.uniform(0.0, 0.08) + (rng.uniform(1, 3) if rng.random() < 0.01 else 0.0)
        tr.observe(t, m + latency)
        steps.update(1_000_000.0 + m * (1 + host), m)
        if t >= next_knot:
            k = tr.knot(next_knot, anchor, steps.drift_s)
            m_rx = next_knot / (1 + cam) + (lost if next_knot > 0 else 0)   # true reception (mono)
            truth.append((m_rx - next_knot - anchor) + m_rx * host)         # camera part + host part
            knots.append(k)
            next_knot += seg_s
    return tr, knots, truth


# ---------------------------------------------------------------- drift tracking
@pytest.mark.parametrize("camera_ppm,host_ppm", [(50.0, 0.0), (-50.0, 20.0), (80.0, -30.0)])
def test_three_day_drift_is_tracked_without_accumulating(camera_ppm: float, host_ppm: float) -> None:
    tr, knots, truth = _simulate(3, camera_ppm, host_ppm)
    errors = [k.correction_s - t for k, t in zip(knots, truth)]
    # Single anchor (old scheme): the error grows to |ppm| x 3 days, e.g. 13 s at 50 ppm.
    assert abs(truth[-1]) > 0.9 * abs(camera_ppm - host_ppm) * 1e-6 * 3 * 86400
    # Tracked: bounded by the reporting jitter, at the start and after 3 days alike.
    assert max(abs(e) for e in errors) < 0.1, max(errors, key=abs)
    assert tr.limited_count == 0
    ppm, span = tr.camera_drift_ppm()
    assert ppm == pytest.approx(camera_ppm, abs=1.0) and span > 86400


def test_exaggerated_one_percent_drift_needs_matching_rate_limit() -> None:
    _, knots, truth = _simulate(0.05, -10000.0, 0.0, seg_s=5.0, max_ppm=20000.0)
    assert max(abs(k.correction_s - t) for k, t in zip(knots, truth)) < 0.15
    assert not any(k.limited for k in knots)
    # With the production limit (1000 ppm) and 5-minute segments such drift is not believed:
    # every boundary is flagged and the correction only follows at the limit rate.
    _, knots, truth = _simulate(0.1, -10000.0, 0.0, seg_s=300.0, max_ppm=1000.0)
    assert all(k.limited for k in knots)
    for a, b in zip(knots, knots[1:]):
        assert b.correction_s - a.correction_s == pytest.approx(1000e-6 * 300 + NON_DRIFT_ALLOWANCE_S)
    assert truth[-1] - knots[-1].correction_s > 10


def test_sudden_reception_jump_is_not_followed() -> None:
    # 12 s of real time vanish from the stream (absorbed discontinuity) after 2 hours.
    tr, knots, truth = _simulate(0.25, 30.0, 0.0, jump=(7200.0, 12.0))
    before = [k for k in knots if k.stream_s < 7200]
    assert not any(k.limited for k in before)
    # The robust fit over the last hour does not follow the jump at first (the supervisor's
    # lost-time detector handles it); if it persists for most of the hour it is followed only at
    # the limit rate and flagged. Never more than limit + allowance per knot.
    first_after = [k for k in knots if 7200 < k.stream_s <= 7200 + 600]
    assert all(abs(k.correction_s - before[-1].correction_s) < 0.05 for k in first_after)
    assert any(k.limited for k in knots if k.stream_s > 7200)
    for a, b in zip(knots, knots[1:]):
        assert abs(b.correction_s - a.correction_s) <= 1000e-6 * 300 + NON_DRIFT_ALLOWANCE_S + 1e-9


def test_break_registered_by_supervisor_keeps_drift_measurement_clean() -> None:
    tr = DriftTracker(1000.0)
    for i in range(1, 60):
        tr.observe(float(i), 100.0 + i + 0.01)          # anchor 100
    tr.knot(30.0, 100.0, 0.0)
    for i in range(60, 120):                            # 12 s of real time lost after stream 59 s
        tr.observe(float(i), 100.0 + i + 12.0 + 0.01)
    assert tr.measure_jump(59.0) == pytest.approx(12.0)
    tr.set_break(59.0, 12.0)
    k = tr.knot(80.0, 100.0, 0.0)
    assert not k.limited and k.correction_s == pytest.approx(0.01, abs=1e-6)


def test_jump_measurement_is_not_biased_by_drift() -> None:
    # Camera 1 % slow: m - t grows 0.01 s per second; 12 s lost after stream time 40 s.
    tr = DriftTracker(20000.0)
    for i in range(1, 41):
        tr.observe(float(i), i / 0.99 + 0.02)
    for i in range(41, 47):
        tr.observe(float(i), i / 0.99 + 12.0 + 0.02)
    # Bias is the drift over the short pre-break window (4 s x 1 % = 0.04 s), not an EMA lag.
    assert tr.measure_jump(40.0) == pytest.approx(12.0, abs=0.06)
    assert DriftTracker(1000.0).measure_jump(5.0) is None


def test_wall_clock_step_is_flagged_not_followed() -> None:
    steps = ClockStepDetector(wall=1000.0, mono=0.0, tolerance_s=2.0)
    tr = DriftTracker(1000.0)
    for i in range(1, 400):
        steps.update(1000.0 + i + (3600.0 if i >= 200 else 0.0), float(i))   # +1 h step at 200 s
        tr.observe(float(i), float(i) + 0.02)
        if i % 100 == 0:
            tr.knot(float(i), 0.0, steps.drift_s)
    assert steps.total_s == pytest.approx(3600.0)
    assert steps.drift_s == pytest.approx(0.0, abs=1e-9)
    assert all(abs(k.correction_s - 0.02) < 1e-9 for k in tr.knots)
    assert steps.drift_ppm == pytest.approx(0.0, abs=1e-3)


def test_correction_is_contiguous_and_interpolated() -> None:
    tr = DriftTracker(20000.0)
    for i in range(0, 40):
        tr.observe(i * 0.5, i * 0.5 * 1.01 + 0.03)
    k1 = tr.knot(5.0, 0.0, 0.0)
    k2 = tr.knot(10.0, 0.0, 0.0)
    assert tr.correction_at(5.0) == k1.correction_s           # next segment's start == this end, exactly
    assert tr.correction_at(10.0) == k2.correction_s
    assert tr.correction_at(0.0) == 0.0
    assert tr.correction_at(7.5) == pytest.approx((k1.correction_s + k2.correction_s) / 2)
    assert tr.correction_at(99.0) == k2.correction_s          # held beyond the last knot
    assert tr.knot(10.0, 0.0, 0.0) is k2                      # boundaries never move once fixed
    assert tr.knot(4.0, 0.0, 0.0) is k2                       # nor go backwards


def test_no_reports_in_window_holds_last_correction() -> None:
    tr = DriftTracker(1000.0)
    tr.observe(1.0, 1.25)
    k1 = tr.knot(2.0, 0.0, 0.0)
    k2 = tr.knot(100.0, 0.0, 0.0)                              # nothing reported near stream 100 s
    assert k2.measured_s is None and k2.reports == 0 and k2.correction_s == k1.correction_s
    assert not k2.limited


def test_interpolate_correction_edges() -> None:
    assert interpolate_correction(5.0, None, 1.0, 10.0, 2.0) == 2.0
    assert interpolate_correction(5.0, 0.0, 1.0, None, 2.0) == 1.0
    assert interpolate_correction(5.0, 10.0, 1.0, 10.0, 2.0) == 2.0
    assert interpolate_correction(-1.0, 0.0, 1.0, 10.0, 2.0) == 1.0
    assert interpolate_correction(11.0, 0.0, 1.0, 10.0, 2.0) == 2.0
    assert interpolate_correction(2.5, 0.0, 1.0, 10.0, 2.0) == pytest.approx(1.25)


def test_host_drift_ppm_measured_separately_from_steps() -> None:
    d = ClockStepDetector(wall=0.0, mono=0.0, tolerance_s=2.0)
    assert d.drift_ppm is None
    for i in range(1, 7201):                                   # 1 h at 0.5 s, wall 40 ppm fast
        mono = i * 0.5
        wall = mono * (1 + 40e-6) + (30.0 if i > 3600 else 0.0)  # plus one 30 s step
        d.update(wall, mono)
    assert d.total_s == pytest.approx(30.0, abs=0.01)
    assert d.drift_ppm == pytest.approx(40.0, abs=0.5)


# ---------------------------------------------------------------- ledger schema
OLD_DDL = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta VALUES ('schema_version', '1');
CREATE TABLE recording_runs (run_id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, directory TEXT NOT NULL,
    launched_utc TEXT NOT NULL, launch_mono REAL NOT NULL, anchor_utc TEXT, display_timezone TEXT NOT NULL,
    app_version TEXT NOT NULL);
INSERT INTO recording_runs VALUES ('r1', 'CAM01', 'CAM01/r1', '2026-11-09T00:00:00.000000Z', 1.0,
    '2026-11-09T00:00:01.000000Z', 'UTC', '0.1');
"""


def test_old_ledger_gains_timing_columns_append_only(tmp_path: Path) -> None:
    db = tmp_path / "sentinel.db"
    con = sqlite3.connect(db)
    con.executescript(OLD_DDL)
    con.close()
    led = Ledger(db)
    try:
        assert set(RUN_COLUMNS) <= existing_columns(led._conn, "recording_runs")
        assert set(SEGMENT_COLUMNS) <= existing_columns(led._conn, "segments")
        run = led.get_run("r1")
        assert run["anchor_utc"] == "2026-11-09T00:00:01.000000Z"       # existing data untouched
        assert run["time_method"] is None and run["time_correction_limited_count"] == 0
        meta = dict(led.query("SELECT key, value FROM schema_meta"))
        assert meta["schema_version"] == "1" and meta[TIMING_SCHEMA_KEY] == "1"
        oid = insert_clock_observation(led, {"observed_utc": "2026-11-09T00:00:00.000000Z", "mono_s": 5.0,
                                             "reason": "startup", "synchronized": 0})
        assert led.query("SELECT synchronized FROM clock_observations WHERE obs_id=?", (oid,))[0][0] == 0
        with pytest.raises(ValueError):
            insert_clock_observation(led, {"observed_utc": "x", "mono_s": 1.0, "reason": "r", "bogus": 1})
    finally:
        led.close()
    Ledger(db).close()                                                   # idempotent on reopen


def test_finalizer_corrections_legacy_and_held(tmp_path: Path) -> None:
    from sentinel.recorder.finalizer import Finalizer
    from sentinel.storage.monitor import StorageMonitor

    cfg = make_config(tmp_path)
    led = Ledger(tmp_path / "l.db")
    try:
        fin = Finalizer(cfg, led, None, StorageMonitor(tmp_path, 0, 0), None)  # type: ignore[arg-type]
        base = dict(camera_id="CAM01", directory="d", launched_utc="2026-11-09T00:00:00.000000Z",
                    launch_mono=1.0, display_timezone="UTC", app_version="t")
        led.create_run(run_id="legacy", **base)
        led.create_run(run_id="tracked", time_method="reception_tracking_v1", time_correction_s=0.7, **base)
        s_old = led.insert_segment(camera_id="CAM01", run_id="legacy", seq=0, rel_path="a", stream_start_s=0.0,
                                   stream_end_s=5.0)
        assert fin._corrections(led.get_segment(s_old), led.get_run("legacy")) == (0.0, 0.0, None, 0.0, False)
        s0 = led.insert_segment(camera_id="CAM01", run_id="tracked", seq=0, rel_path="b", stream_start_s=0.0,
                                stream_end_s=5.0, time_correction_start_s=0.0, time_correction_end_s=0.3,
                                time_correction_stream_s=5.0)
        s1 = led.insert_segment(camera_id="CAM01", run_id="tracked", seq=1, rel_path="c", stream_start_s=5.0,
                                stream_end_s=10.0)                       # e.g. closed by startup recovery
        assert fin._corrections(led.get_segment(s0), led.get_run("tracked")) == (0.0, 0.0, 5.0, 0.3, False)
        assert fin._corrections(led.get_segment(s1), led.get_run("tracked")) == (5.0, 0.3, None, 0.3, True)
    finally:
        led.close()


def test_diagnostics_include_clock_observations(tmp_path: Path) -> None:
    from sentinel.diagnostics import build_report

    cfg = make_config(tmp_path)
    led = Ledger(cfg.data_path / "sentinel.db")
    insert_clock_observation(led, {"observed_utc": "2026-11-09T10:00:00.000000Z", "mono_s": 5.0,
                                   "reason": "periodic", "sync_tool": "w32tm", "synchronized": 0,
                                   "sync_source": "time.windows.com,0x9", "camera_drift_ppm": 12.5,
                                   "host_clock_drift_ppm": -3.5, "time_correction_s": 0.042})
    led.close()
    report = build_report(cfg, "test", tmp_path / "out")
    with zipfile.ZipFile(report) as zf:
        assert "clock_observations.csv" in zf.namelist()
        summary = zf.read("summary.txt").decode()
        csv_text = zf.read("clock_observations.csv").decode()
    assert "OS clock synchronised:  NO" in summary and "time.windows.com" in summary
    assert "+12.5 ppm" in summary and "-3.5 ppm" in summary and "+0.042 s" in summary
    assert "periodic" in csv_text


# ---------------------------------------------------------------- OS time-sync parsing
# Real output captured on the development PC (Windows 11 Home, Windows Time service stopped):
W32TM_SERVICE_STOPPED = "The following error occurred: The service has not been started. (0x80070426)\n"
SC_QUERY_STOPPED = """
SERVICE_NAME: w32time
        TYPE               : 30  WIN32
        STATE              : 1  STOPPED
        WIN32_EXIT_CODE    : 0  (0x0)
        SERVICE_EXIT_CODE  : 0  (0x0)
        CHECKPOINT         : 0x0
        WAIT_HINT          : 0x0
"""
# Documented format (Microsoft w32tm documentation); NOT captured on this machine.
W32TM_SYNCED_VERBOSE = """Leap Indicator: 0(no warning)
Stratum: 3 (secondary reference - syncd by (S)NTP)
Precision: -23 (119.209ns per tick)
Root Delay: 0.0312500s
Root Dispersion: 7.7754814s
ReferenceId: 0x0A000001 (source IP:  10.0.0.1)
Last Successful Sync Time: 11/9/2026 10:15:02 AM
Source: 10.0.0.1,0x8
Poll Interval: 10 (1024s)

Phase Offset: -0.0012345s
ClockRate: 0.0156250s
State Machine: 2 (Sync)
Time Source Flags: 0 (None)
Server Role: 0 (None)
Last Sync Error: 0 (The command completed successfully.)
Time since Last Good Sync Time: 120.5000000s
"""
W32TM_UNSYNCED = """Leap Indicator: 3(not synchronized)
Stratum: 0 (unspecified)
Precision: -23 (119.209ns per tick)
Root Delay: 0.0000000s
Root Dispersion: 0.0000000s
ReferenceId: 0x00000000 (unspecified)
Last Successful Sync Time: unspecified
Source: Local CMOS Clock
Poll Interval: 10 (1024s)
"""
CHRONYC = """Reference ID    : C0A80101 (ntp.example.lan)
Stratum         : 3
Ref time (UTC)  : Mon Nov 09 10:00:00 2026
System time     : 0.000012345 seconds slow of NTP time
Last offset     : -0.000001234 seconds
RMS offset      : 0.000023456 seconds
Frequency       : 12.345 ppm fast
Residual freq   : +0.001 ppm
Skew            : 0.012 ppm
Root delay      : 0.001234567 seconds
Root dispersion : 0.000123456 seconds
Update interval : 64.2 seconds
Leap status     : Normal
"""


def test_parse_w32tm_synced_and_unsynced() -> None:
    p = parse_w32tm_status(W32TM_SYNCED_VERBOSE, now_epoch=1_000_000.0)
    assert p["synchronized"] is True and p["stratum"] == 3 and p["source"] == "10.0.0.1,0x8"
    assert p["phase_offset_s_raw"] == pytest.approx(-0.0012345)
    assert p["since_last_good_sync_s"] == pytest.approx(120.5)
    assert p["last_good_sync_utc"] == "1970-01-12T13:44:39.500000Z"
    u = parse_w32tm_status(W32TM_UNSYNCED)
    assert u["synchronized"] is False and u["leap_indicator"] == 3
    assert parse_w32tm_status("Sprungindikator: 0\n")["synchronized"] is None   # localised: unknown


def test_query_windows_with_service_stopped_uses_real_output() -> None:
    def run(argv: list[str]) -> CommandResult:
        if argv[0] == "sc":
            return CommandResult(0, SC_QUERY_STOPPED)
        return CommandResult(2147943462, W32TM_SERVICE_STOPPED)

    r = query_windows(run)
    assert r.tool == "w32tm" and r.synchronized is False and r.offset_s is None
    assert r.detail["service_state"] == "STOPPED" and r.detail["reason"] == "windows_time_service_stopped"
    assert "0x80070426" in r.detail["error"]
    assert parse_sc_state(SC_QUERY_STOPPED) == "STOPPED"


def test_query_windows_synced() -> None:
    def run(argv: list[str]) -> CommandResult:
        if argv[0] == "sc":
            return CommandResult(0, SC_QUERY_STOPPED.replace("1  STOPPED", "4  RUNNING"))
        return CommandResult(0, W32TM_SYNCED_VERBOSE)

    r = query_windows(run, now=lambda: 1_000_000.0)
    assert r.synchronized is True and r.source == "10.0.0.1,0x8"
    assert r.last_sync_utc == "1970-01-12T13:44:39.500000Z" and r.detail["service_state"] == "RUNNING"


def test_parse_chronyc_and_timedatectl() -> None:
    c = parse_chronyc_tracking(CHRONYC)
    assert c["synchronized"] is True and c["offset_s"] == pytest.approx(-0.000012345)
    assert c["frequency_ppm"] == pytest.approx(12.345) and c["reference_name"] == "ntp.example.lan"
    assert parse_chronyc_tracking(CHRONYC.replace("Normal", "Not synchronised"))["synchronized"] is False
    t = parse_timedatectl_show("Timezone=Etc/UTC\nNTP=yes\nNTPSynchronized=no\n")
    assert t["synchronized"] is False and t["NTP"] == "yes"


def test_query_linux_prefers_chrony_then_timedatectl() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> CommandResult:
        calls.append(argv)
        if argv[0] == "chronyc":
            return CommandResult(0, CHRONYC)
        if argv[:2] == ["timedatectl", "show"]:
            return CommandResult(0, "NTP=yes\nNTPSynchronized=yes\n")
        return CommandResult(0, "ServerName=ntp.example.lan\nServerAddress=10.0.0.1\n")

    r = query_linux(run, which=lambda name: f"/usr/bin/{name}")
    assert r.tool == "chronyc" and r.offset_s == pytest.approx(-0.000012345)
    r = query_linux(run, which=lambda name: None if name == "chronyc" else f"/usr/bin/{name}")
    assert r.tool == "timedatectl" and r.synchronized is True and r.source == "ntp.example.lan"
    r = query_linux(run, which=lambda name: None)
    assert r.tool is None and r.synchronized is None and "error" in r.detail
    assert json.dumps(r.detail)
