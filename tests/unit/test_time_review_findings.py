"""Regression tests for the independent QA review of design/time-accuracy @4af7b3b
(findings 1-9; see docs/branches/design-time-accuracy.md "Review findings")."""

from __future__ import annotations

import random
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from conftest import make_config
from sentinel.db import Ledger
from sentinel.media.probe import ProbeResult
from sentinel.recorder.segment_list import SegmentEntry
from sentinel.recorder.supervisor import segment_ready
from sentinel.recorder.timebase import (NON_DRIFT_ALLOWANCE_S, ClockStepDetector, DriftTracker, RunTimebase,
                                        read_clock_pair)
from sentinel.timesync import NTP_UNIX_OFFSET, parse_w32tm_status, sntp_offset


# ------------------------------------------------------------------ findings 1, 2: reviewer's probe
def _reviewer_probe(stall_at: float | None = None, stall_len: float = 0.0, ppm: float = 0.0,
                    host_lag: float | None = None, seed: int = 1) -> list[tuple[float, float, float, bool]]:
    """Port of the reviewer's drift_probe.py: 300 s segments, 1 report/s with exponential
    (mean 20 ms) reporting jitter; optionally every report around one boundary delayed by
    ``stall_len`` (disk/AV/GIL stall), or the wall clock read ``host_lag`` late around each
    boundary. Returns (boundary, single-anchor error, corrected error, limited) per boundary."""
    rnd = random.Random(seed)
    seg = 300.0
    tb = RunTimebase(1000.0, 0.0)
    dt = DriftTracker(1000.0)
    steps = ClockStepDetector(1000.0, 0.0, 2.0)
    true_lat = 0.3
    out = []
    t = m = 0.0
    nextb = seg
    while t < seg * 12:
        m += 1.0
        t = m * (1 + ppm * 1e-6) - true_lat
        rep_m = m + rnd.expovariate(1 / 0.02)
        if stall_at is not None and stall_at - 9 <= t <= stall_at + 3:
            rep_m += stall_len
        tb.observe(t, rep_m)
        dt.observe(t, rep_m)
        lag = host_lag if (host_lag and abs(t - nextb) < 1.5) else 0.0
        steps.update(1000.0 + m + lag, m)
        if t >= nextb + 0.5:
            tb.freeze()
            assert tb.anchor_mono is not None
            k = dt.knot(nextb, tb.anchor_mono, steps.drift_s)
            true_utc = 1000.0 + (nextb + true_lat) / (1 + ppm * 1e-6)
            base, corr = tb.epoch_for(nextb), tb.epoch_for(nextb, k.correction_s)
            assert base is not None and corr is not None
            out.append((nextb, base - true_utc, corr - true_utc, k.limited))
            nextb += seg
    return out


@pytest.mark.parametrize("stall_len", [3.0, 0.35])
def test_stall_at_a_boundary_never_moves_it(stall_len: float) -> None:
    # Finding 1: was +0.400 s and a false time_correction_limited (3 s stall), 0.35 s unflagged.
    for boundary, base, corr, limited in _reviewer_probe(stall_at=1500.0, stall_len=stall_len):
        assert abs(corr) <= abs(base) + 0.02, (boundary, base, corr)
        assert not limited, boundary


def test_stall_of_a_whole_minute_is_not_followed() -> None:
    for boundary, base, corr, limited in _reviewer_probe(stall_at=1500.0, stall_len=60.0):
        assert abs(corr) <= abs(base) + 0.02 and not limited, (boundary, corr)


def test_late_wall_clock_reads_do_not_become_drift() -> None:
    # Finding 2: wall read 0.25 s late around each boundary used to add +0.25 s to every knot.
    for boundary, base, corr, limited in _reviewer_probe(host_lag=0.25):
        assert abs(corr) <= 0.02 and not limited, (boundary, corr)


def test_genuine_50ppm_drift_is_still_corrected() -> None:
    res = _reviewer_probe(ppm=50.0)
    assert res[-1][1] > 0.15                                         # single anchor: +0.167 s after 1 h
    assert all(abs(corr) <= 0.02 and not limited for _, _, corr, limited in res)


def test_correction_change_per_boundary_is_bounded_by_genuine_drift() -> None:
    tr = DriftTracker(1000.0)
    for i in range(1, 700):
        tr.observe(float(i), i + 0.01 + (5.0 if i > 350 else 0.0))  # 5 s step in reception at 350 s
    k1 = tr.knot(300.0, 0.0, 0.0)
    k2 = tr.knot(600.0, 0.0, 0.0)
    assert abs(k2.correction_s - k1.correction_s) <= 1000e-6 * 300 + NON_DRIFT_ALLOWANCE_S + 1e-12


def test_read_clock_pair_takes_wall_between_two_monotonic_reads() -> None:
    class Clock:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.m = 100.0

        def mono(self) -> float:
            self.calls.append("m")
            self.m += 0.0005
            return self.m

        def wall(self) -> float:
            self.calls.append("w")
            return 5000.0

    c = Clock()
    w, m, spread = read_clock_pair(c)
    assert c.calls == ["m", "w", "m"] and w == 5000.0
    assert m == pytest.approx(100.00075) and spread == pytest.approx(0.0005)


def test_loose_clock_pairs_are_not_used_for_drift() -> None:
    d = ClockStepDetector(wall=1000.0, mono=0.0, tolerance_s=2.0)
    for i in range(1, 50):
        d.update(1000.0 + i + 0.3, float(i), spread_s=0.3)      # thread delayed 0.3 s between reads
    assert d.drift_s == pytest.approx(0.0, abs=1e-9)
    d.update(1000.0 + 50.0, 50.0, spread_s=0.0001)
    assert d.drift_s == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------------------ finding 7: fast wall-clock slew
def test_fast_wall_clock_slew_is_flagged_as_clock_anomaly_not_followed() -> None:
    tr = DriftTracker(1000.0)
    steps = ClockStepDetector(wall=1000.0, mono=0.0, tolerance_s=2.0)
    knots = []
    for i in range(1, 1201):
        slew = min(1.5, max(0.0, (i - 300) * 0.01))             # 1.5 s slewed in over 150 s (10 000 ppm)
        steps.update(1000.0 + i + slew, float(i))
        tr.observe(float(i), i + 0.01)
        if i % 300 == 0:
            knots.append(tr.knot(float(i), 0.0, steps.drift_s))
    assert steps.total_s == 0.0                                   # below the step tolerance
    assert knots[1].host_limited and not knots[1].limited        # PC clock, not camera
    assert tr.host_limited_count >= 1
    for a, b in zip(knots, knots[1:]):
        assert abs(b.host_applied_s - a.host_applied_s) <= 1000e-6 * 300 + NON_DRIFT_ALLOWANCE_S + 1e-12


# ------------------------------------------------------------------ finding 8: hold until a newer report
def test_segment_is_held_until_a_newer_progress_report_was_checked() -> None:
    e = SegmentEntry("seg_000003.ts", 15.0, 20.0)
    assert not segment_ready(e, None)
    assert not segment_ready(e, 19.9)
    assert not segment_ready(e, 20.0)            # the report at the boundary itself may predate the check
    assert segment_ready(e, 20.07)


# ------------------------------------------------------------------ finding 5: w32tm freshness
SYNCED = """Leap Indicator: 0(no warning)
Stratum: 3 (secondary reference - syncd by (S)NTP)
ReferenceId: 0x0A000001 (source IP:  10.0.0.1)
Last Successful Sync Time: 11/9/2026 10:15:02 AM
Source: 10.0.0.1,0x8
Poll Interval: 10 (1024s)
Last Sync Error: {err}
Time since Last Good Sync Time: {age}s
"""


def test_w32tm_week_old_or_failing_sync_is_not_synchronized() -> None:
    ok = parse_w32tm_status(SYNCED.format(err="0 (The command completed successfully.)", age="900.0"), 1e6)
    assert ok["synchronized"] is True and ok["sync_reason"] == "ok" and ok["max_sync_age_s"] == 2048
    old = parse_w32tm_status(SYNCED.format(err="0 (The command completed successfully.)", age="604800.0"), 1e6)
    assert old["synchronized"] is False and old["sync_reason"] == "last_sync_too_old"
    fail = parse_w32tm_status(SYNCED.format(err="1 (The computer did not resync because no time data was "
                                                "available.)", age="900.0"), 1e6)
    assert fail["synchronized"] is False and fail["sync_reason"] == "last_sync_error"
    custom = parse_w32tm_status(SYNCED.format(err="0 (ok)", age="900.0"), 1e6, max_sync_age_s=600.0)
    assert custom["synchronized"] is False and custom["max_sync_age_s"] == 600.0
    non_verbose = "\n".join(SYNCED.splitlines()[:6])            # no Last Sync Error / sync age
    nv = parse_w32tm_status(non_verbose)
    assert nv["synchronized"] is None and nv["sync_reason"] == "sync_age_not_reported"


# ------------------------------------------------------------------ finding 4: SNTP measured offset
class _FakeNtpServer:
    """Local UDP SNTP responder whose clock is ``skew_s`` ahead of this PC (test only)."""

    def __init__(self, skew_s: float, echo: bool = True, stratum: int = 2, leap: int = 0) -> None:
        self.skew_s, self.echo, self.stratum, self.leap = skew_s, echo, stratum, leap
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def _ntp(t: float) -> bytes:
        sec = int(t) + NTP_UNIX_OFFSET
        return struct.pack("!II", sec, int((t - int(t)) * 2**32))

    def _serve(self) -> None:
        try:
            data, addr = self.sock.recvfrom(512)
        except OSError:
            return
        t2 = time.time() + self.skew_s
        time.sleep(0.01)                                          # server processing time
        reply = bytearray(48)
        reply[0] = (self.leap << 6) | (4 << 3) | 4
        reply[1] = self.stratum
        reply[24:32] = data[40:48] if self.echo else b"\x00" * 8
        reply[32:40] = self._ntp(t2)
        reply[40:48] = self._ntp(time.time() + self.skew_s)
        self.sock.sendto(bytes(reply), addr)

    def close(self) -> None:
        self.sock.close()


def test_sntp_measures_offset_from_reference() -> None:
    srv = _FakeNtpServer(skew_s=0.5)
    try:
        r = sntp_offset(f"127.0.0.1:{srv.port}")
    finally:
        srv.close()
    assert r.offset_s == pytest.approx(-0.5, abs=0.01)           # this PC is 0.5 s behind the reference
    assert r.delay_s is not None and 0 <= r.delay_s < 0.1
    assert r.detail["stratum"] == 2 and "error" not in r.detail


@pytest.mark.parametrize("kwargs,error", [({"echo": False}, "not_a_reply_to_this_request"),
                                          ({"leap": 3}, "server_not_synchronised"),
                                          ({"stratum": 0}, "server_not_synchronised")])
def test_sntp_rejects_bad_replies(kwargs: dict, error: str) -> None:
    srv = _FakeNtpServer(skew_s=0.0, **kwargs)
    try:
        r = sntp_offset(f"127.0.0.1:{srv.port}")
    finally:
        srv.close()
    assert r.offset_s is None and r.detail["error"] == error


def test_sntp_unreachable_is_reported_not_raised() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                     # nothing listens there now
    r = sntp_offset(f"127.0.0.1:{port}", timeout_s=0.5)
    assert r.offset_s is None and "error" in r.detail


def test_reference_offset_recorded_in_clock_observation(tmp_path: Path) -> None:
    from sentinel.service import RecorderService

    srv = _FakeNtpServer(skew_s=-0.25)
    cfg = make_config(tmp_path, service={"time_reference_server": f"127.0.0.1:{srv.port}"})
    svc = RecorderService(cfg)
    svc.ledger = Ledger(cfg.data_path / "sentinel.db")
    try:
        svc._observe_clock("periodic")
        row = svc.ledger.query("SELECT * FROM clock_observations")[0]
    finally:
        srv.close()
        svc.ledger.close()
    assert row["ref_server"] == f"127.0.0.1:{srv.port}"
    assert row["ref_offset_s"] == pytest.approx(0.25, abs=0.01)  # PC ahead of the reference by 0.25 s


# ------------------------------------------------------------------ finding 9: no write after close
def test_clock_observation_is_not_written_once_stopping(tmp_path: Path) -> None:
    from sentinel.service import RecorderService

    cfg = make_config(tmp_path)
    svc = RecorderService(cfg)
    svc.ledger = Ledger(cfg.data_path / "sentinel.db")
    try:
        svc._closing.set()
        svc._observe_clock("periodic")
        assert svc.ledger.query("SELECT COUNT(*) FROM clock_observations")[0][0] == 0
    finally:
        svc.ledger.close()


# ------------------------------------------------------------------ finding 6: segment-0 lead-in
def _seg0_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seg0_first_pts: float,
               seg1_first_pts: float | None, with_seg1: bool = True):
    import sentinel.recorder.finalizer as fin_mod
    from sentinel.recorder.finalizer import Finalizer
    from sentinel.storage.monitor import StorageMonitor

    cfg = make_config(tmp_path)
    led = Ledger(tmp_path / "l.db")
    fin = Finalizer(cfg, led, None, StorageMonitor(tmp_path, 0, 0), None)  # type: ignore[arg-type]
    led.create_run(run_id="r", camera_id="CAM01", directory="CAM01/r", launched_utc="2026-11-10T00:00:00.000000Z",
                   launch_mono=1.0, display_timezone="UTC", app_version="t", time_method="reception_tracking_v1",
                   anchor_utc="2026-11-10T00:00:00.000000Z")
    s0 = led.insert_segment(camera_id="CAM01", run_id="r", seq=0, rel_path="CAM01/r/seg_000000.ts",
                            stream_start_s=0.0, stream_end_s=300.0, time_correction_start_s=0.0,
                            time_correction_end_s=0.0, time_correction_stream_s=300.0)
    if with_seg1:
        p1 = cfg.spool_path / "CAM01/r/seg_000001.ts"
        p1.parent.mkdir(parents=True, exist_ok=True)
        p1.write_bytes(b"\x47" * 188)
        led.insert_segment(camera_id="CAM01", run_id="r", seq=1, rel_path="CAM01/r/seg_000001.ts")
    monkeypatch.setattr(fin_mod, "first_video_pts", lambda path, ffprobe="ffprobe": seg1_first_pts)
    res = ProbeResult(True)
    res.duration_s, res.keyframe_count, res.first_pts_s = 297.0, 149, seg0_first_pts
    warnings: list[str] = []
    try:
        out = fin._timestamps(led.get_segment(s0), led.get_run("r"), res, False, warnings)
    finally:
        led.close()
    return out, warnings


def test_seg0_timestamp_jump_at_end_is_not_hidden_as_lead_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Reviewer's seg0_probe shape: footage 0..297 s then a 3 s jump; PTS offset 1.4 s (seg 1: 301.4).
    (start, end, _src, gaps, _cols), warnings = _seg0_case(tmp_path, monkeypatch, 1.4, 301.4)
    assert start == "2026-11-10T00:00:00.000000Z" and end == "2026-11-10T00:04:57.000000Z"
    assert [g.reason for g in gaps] == ["timestamp_discontinuity_at_boundary"] and gaps[0].duration_s == 3.0
    assert "timestamp_gap_at_segment_end" in warnings


def test_seg0_genuine_lead_in_is_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (start, end, _src, gaps, _cols), warnings = _seg0_case(tmp_path, monkeypatch, 4.4, 301.4)   # 3 s late start
    assert start == "2026-11-10T00:00:03.000000Z" and end == "2026-11-10T00:05:00.000000Z"
    assert gaps == [] and warnings == []


def test_seg0_lead_in_unverifiable_is_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (start, _end, _src, gaps, _cols), warnings = _seg0_case(tmp_path, monkeypatch, 1.4, None, with_seg1=False)
    assert start == "2026-11-10T00:00:03.000000Z" and gaps == []  # previous assumption kept...
    assert "lead_in_unverified" in warnings                        # ...but no longer silently
