"""End-to-end recorder tests: real RecorderService + real FFmpeg + SimulatedCamera with faults.

Segment length is 2 s so each scenario runs in seconds; production uses 300 s.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import FakeDisk, Harness, ShiftableClock, contiguous, wait_for
from sentinel.clock import parse_iso_utc
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]


@pytest.fixture()
def cam():
    c = SimulatedCamera(fps=15).start()
    yield c
    c.stop()


def _between(h: Harness) -> list[dict]:
    return h.gaps("between_runs")


# ------------------------------------------------------------------ continuous recording
def test_continuous_recording_segments_are_contiguous_hashed_sealed(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url).start()
    try:
        h.wait_sealed(4, timeout=40)
        wait_for(lambda: h.status().get("overall") == "RECORDING", 15, what="RECORDING status")
        st = h.status()
        assert st["camera_link"] == "connected" and st["last_segment"]["state"] == "FINALIZED"
        # The only alert on a healthy simulated system is the permanent test-camera warning.
        assert [a["code"] for a in st["alerts"]] == ["Z-001"]
        # Deferred content hashing catches up with the sealed segments.
        wait_for(lambda: all(s["content_sha256"] for s in h.segments("FINALIZED")[:4]), 30,
                 what="deferred content hashes")
    finally:
        h.stop()
    segs = h.segments()
    assert len(h.runs()) == 1, "no restarts expected on a healthy stream"
    assert all(s["state"] == "FINALIZED" for s in segs), [s["state"] for s in segs]
    for s in segs:
        h.assert_sealed_integrity(s)
    for s in segs[:4]:
        assert s["content_sha256"] and s["decoded_frame_count"] == s["packet_count"]
        assert s["content_hash_method"].startswith("sha256(per-frame-sha256-list)")
    for s in segs:
        assert s["time_source"] == "run_anchor" and json.loads(s["warnings"]) == []
        assert s["observed_closed_utc"] >= s["end_utc"]          # closed after the footage ended
        assert parse_iso_utc(s["observed_closed_utc"]) - parse_iso_utc(s["end_utc"]) < 5.0
    for a, b in zip(segs, segs[1:]):
        assert contiguous(a, b), f"{a['end_utc']} -> {b['start_utc']}"
    assert h.gaps("within_segment") == []
    gaps = _between(h)
    assert len(gaps) == 1 and gaps[0]["reason"] == "graceful_stop" and gaps[0]["end_utc"] is None
    assert gaps[0]["start_utc"] == segs[-1]["end_utc"]
    assert h.exit_code == 0
    assert h.status()["overall"] == "STOPPED"


def test_first_segment_time_matches_wall_clock(tmp_path: Path, cam: SimulatedCamera) -> None:
    t0 = time.time()
    h = Harness(tmp_path, cam.url).start()
    try:
        h.wait_sealed(1)
    finally:
        h.stop()
    first = h.segments()[0]
    start = parse_iso_utc(first["start_utc"])
    # Footage cannot start before the recorder launched, nor (healthy LAN) seconds later.
    assert t0 - 0.1 <= start <= t0 + 3.0, (t0, first["start_utc"])


# ------------------------------------------------------------------ camera faults
def test_disconnect_reconnect_records_gap(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url).start()
    try:
        h.wait_sealed(2)
        cam.drop_clients()
        wait_for(lambda: len(h.runs()) >= 2, 20, what="second run")
        wait_for(lambda: any(g["end_utc"] for g in _between(h)), 30, what="gap closed by new footage")
    finally:
        h.stop()
    runs = h.runs()
    assert runs[0]["end_reason"] in ("source_ended", "ffmpeg_error")
    closed = [g for g in _between(h) if g["end_utc"]]
    g = closed[0]
    assert g["reason"] == runs[0]["end_reason"] and 0 < g["duration_s"] < 10
    before = [s for s in h.segments() if s["run_id"] == runs[0]["run_id"]]
    after = [s for s in h.segments() if s["run_id"] == runs[1]["run_id"]]
    assert g["start_utc"] == max(s["end_utc"] for s in before)
    assert g["end_utc"] == min(s["start_utc"] for s in after)
    assert parse_iso_utc(g["end_utc"]) > parse_iso_utc(g["start_utc"])


def test_camera_offline_shows_interrupted_and_recovers(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url, recording={"connect_timeout_s": 8}).start()
    try:
        h.wait_sealed(2)
        cam.go_offline()
        st = wait_for(lambda: (s := h.status()).get("overall") == "INTERRUPTED" and s, 25,
                      what="INTERRUPTED status")
        assert st["camera_link"] == "disconnected" and st["open_gap"] is not None
        time.sleep(4)                                          # several failed attempts
        cam.go_online()
        wait_for(lambda: h.status().get("overall") == "RECORDING", 40, what="RECORDING after outage")
        wait_for(lambda: any(g["end_utc"] for g in _between(h)), 30, what="gap closed")
    finally:
        h.stop()
    g = [x for x in _between(h) if x["end_utc"]][0]
    assert g["attempts"] >= 2, "retries during the outage must be counted in one gap"
    assert g["duration_s"] >= 4.0
    reasons = {r["end_reason"] for r in h.runs()}
    assert reasons & {"connect_timeout", "ffmpeg_error"}


def test_stream_freeze_detected_as_stall(tmp_path: Path, cam: SimulatedCamera) -> None:
    # I/O timeout longer than the stall timeout so the supervisor (not FFmpeg) detects it.
    h = Harness(tmp_path, cam.url, recording={"stall_timeout_s": 3, "io_timeout_s": 30}).start()
    try:
        h.wait_sealed(2)
        cam.freeze()
        t_freeze = time.monotonic()
        wait_for(lambda: any(r["end_reason"] == "stall" for r in h.runs()), 20, what="stall end reason")
        detect_s = time.monotonic() - t_freeze
        cam.resume()
        wait_for(lambda: any(g["end_utc"] for g in _between(h)), 40, what="recovered after stall")
    finally:
        h.stop()
    assert detect_s < 3 + 3 + 3, f"stall took {detect_s:.1f}s to detect"   # timeout + poll + grace
    stalled = [r for r in h.runs() if r["end_reason"] == "stall"][0]
    segs = [s for s in h.segments() if s["run_id"] == stalled["run_id"]]
    assert all(s["state"] in ("FINALIZED", "INTERRUPTED") for s in segs),         [(s["state"], s["size_bytes"], s["validation_detail"]) for s in segs]
    assert [g for g in _between(h) if g["reason"] == "stall"]


def test_short_freeze_is_recorded_as_internal_gap(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url, recording={"stall_timeout_s": 10, "io_timeout_s": 30,
                                              "max_internal_gap_s": 1.0, "segment_seconds": 4}).start()
    try:
        h.wait_sealed(1)
        cam.freeze()
        time.sleep(2.5)
        cam.resume()
        wait_for(lambda: h.gaps("within_segment"), 30, what="internal gap row")
    finally:
        h.stop()
    assert len(h.runs()) == 1, "a short freeze must not restart recording"
    g = h.gaps("within_segment")[0]
    assert g["reason"] in ("timestamp_discontinuity", "timestamp_discontinuity_at_boundary")
    assert g["duration_s"] == pytest.approx(2.5, abs=1.0)
    seg = [s for s in h.segments() if s["segment_id"] == g["prev_segment_id"]][0]
    w = json.loads(seg["warnings"])
    assert "internal_timestamp_gap" in w or "timestamp_gap_at_segment_end" in w


def test_ffmpeg_crash_marks_segment_interrupted_and_restarts(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url, recording={"segment_seconds": 4}).start()
    try:
        h.wait_sealed(1)
        time.sleep(1.5)                                        # be mid-segment
        proc = wait_for(lambda: h.svc.supervisor.current_proc, 5, what="ffmpeg process")
        proc.kill()
        wait_for(lambda: h.segments("INTERRUPTED"), 20,
                 what=f"interrupted segment (states: {[(x['state'], x['size_bytes']) for x in h.segments()]})")
        wait_for(lambda: any(g["end_utc"] for g in _between(h)), 30, what="recording resumed")
    finally:
        h.stop()
    seg = h.segments("INTERRUPTED")[0]
    assert seg["interrupted"] == 1 and seg["duration_s"] > 0 and seg["sha256"]
    h.assert_sealed_integrity(seg)
    assert h.runs()[0]["end_reason"] in ("ffmpeg_error", "ffmpeg_killed")
    g = [x for x in _between(h) if x["end_utc"]][0]
    assert g["start_utc"] == seg["end_utc"] and g["prev_segment_id"] == seg["segment_id"]


def test_camera_reboot_with_timestamp_reset(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url).start()
    try:
        h.wait_sealed(2)
        n = h.sealed_count()
        cam.restart_encoder()                                   # stream timestamps restart from zero
        h.wait_sealed(n + 3, timeout=40)
    finally:
        h.stop()
    segs = h.segments()
    assert all(s["state"] in ("FINALIZED", "INTERRUPTED") for s in segs)
    # Whatever FFmpeg did with the backwards timestamps, ledger times must never go backwards.
    starts = [parse_iso_utc(s["start_utc"]) for s in segs]
    assert starts == sorted(starts)
    for s in segs:
        h.assert_sealed_integrity(s)


def test_corrupted_stream_data_does_not_stop_recording(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url, recording={"validation": "decode"}).start()
    try:
        h.wait_sealed(2)
        n = h.sealed_count()
        cam.corrupt(400)
        h.wait_sealed(n + 3, timeout=40)
    finally:
        h.stop()
    segs = h.segments()
    flagged = [s for s in segs if json.loads(s["warnings"] or "[]") or s["state"] != "FINALIZED"]
    assert flagged, "corruption injected into the stream left no trace in the ledger"
    assert segs[-1]["state"] == "FINALIZED", "recording should continue cleanly after the corrupt burst"


# ------------------------------------------------------------------ storage
def test_storage_warning_then_full_then_recovery(tmp_path: Path, cam: SimulatedCamera) -> None:
    disk = FakeDisk(free_gb=500)
    h = Harness(tmp_path, cam.url, disk=disk, storage={"warning_free_gb": 100, "critical_free_gb": 10}).start()
    try:
        h.wait_sealed(2)
        disk.free_gb = 50
        wait_for(lambda: h.status().get("overall") == "STORAGE_WARNING", 10, what="STORAGE_WARNING")
        disk.free_gb = 5
        wait_for(lambda: h.status().get("overall") == "STORAGE_FULL", 10, what="STORAGE_FULL")
        wait_for(lambda: any(r["end_reason"] == "storage_critical" for r in h.runs()), 15, what="clean stop")
        n_runs = len(h.runs())
        time.sleep(3)
        assert len(h.runs()) == n_runs, "must not keep launching FFmpeg while storage is critical"
        disk.free_gb = 500
        wait_for(lambda: h.status().get("overall") == "RECORDING", 40, what="RECORDING after space freed")
    finally:
        h.stop()
    stopped = [r for r in h.runs() if r["end_reason"] == "storage_critical"][0]
    segs = [s for s in h.segments() if s["run_id"] == stopped["run_id"]]
    assert segs and segs[-1]["state"] == "FINALIZED", "graceful stop should close the last segment cleanly"
    assert [g for g in _between(h) if g["reason"] == "storage_critical"]


# ------------------------------------------------------------------ clock
def test_wall_clock_step_flags_run_and_segments(tmp_path: Path, cam: SimulatedCamera) -> None:
    clock = ShiftableClock()
    h = Harness(tmp_path, cam.url, clock=clock).start()
    try:
        h.wait_sealed(2)
        clock.offset = 3600.0                                  # someone sets the clock an hour ahead
        wait_for(lambda: h.events("clock_step"), 10, what="clock_step event")
        n = h.sealed_count()
        h.wait_sealed(n + 2)
    finally:
        h.stop()
    run = h.runs()[0]
    assert run["clock_anomaly"] == 1
    segs = [s for s in h.segments() if s["run_id"] == run["run_id"]]
    assert all(s["clock_anomaly"] == 1 for s in segs)
    # Derived times stay on the run's monotonic timeline: still contiguous, no 1-hour jump.
    for a, b in zip(segs, segs[1:]):
        assert contiguous(a, b)
