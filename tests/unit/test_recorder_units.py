from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_config
from sentinel.clock import add_seconds_iso, iso_utc, parse_iso_utc
from sentinel.credentials import Credentials, url_with_credentials
from sentinel.hashing import sha256_file
from sentinel.models import CameraLink, OverallStatus, StorageLevel
from sentinel.recorder.ffmpeg_cmd import build_record_argv, redacted_command
from sentinel.recorder.progress import ProgressParser
from sentinel.recorder.segment_list import SegmentListReader
from sentinel.recorder.supervisor import Backoff
from sentinel.recorder.timebase import ClockStepDetector, RunTimebase
from sentinel.status import Measurements, decide, read_status
from sentinel.storage.monitor import GB, DiskUsage, StorageMonitor


# ---------------------------------------------------------------- FFmpeg argv
def test_argv_stream_copy_no_audio_by_default(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, url="rtsp://192.0.2.10:554/s")
    argv = build_record_argv(cfg, "rtsp://192.0.2.10:554/s", tmp_path)
    s = " ".join(argv)
    assert ["-c", "copy"] == argv[argv.index("-c"):argv.index("-c") + 2]
    assert "-an" in argv and "0:a" not in s
    assert "libx264" not in s and "-c:v" not in s            # never transcode
    assert ["-rtsp_transport", "tcp"] == argv[argv.index("-rtsp_transport"):argv.index("-rtsp_transport") + 2]
    assert argv[argv.index("-timeout") + 1] == "4000000"      # µs
    assert "-segment_list_type" in argv and "csv" in argv
    assert argv[argv.index("-segment_format") + 1] == "mpegts"
    assert argv[-1].endswith("seg_%06d.ts")


def test_argv_audio_only_when_enabled(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, camera={"include_audio": True})
    argv = build_record_argv(cfg, cfg.camera.url, tmp_path)
    assert "-an" not in argv and "0:a:0?" in argv


def test_argv_tcp_uses_rw_timeout_and_alignment(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, recording={"align_to_clock": True})
    argv = build_record_argv(cfg, cfg.camera.url, tmp_path)
    assert "-rw_timeout" in argv and "-rtsp_transport" not in argv
    assert argv[argv.index("-segment_atclocktime") + 1] == "1"
    assert argv[argv.index("-f") + 1] == "mpegts"            # input format precedes -i


def test_redacted_command_hides_credentials(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    url = url_with_credentials("rtsp://192.0.2.10/s", Credentials("viewer", "pa55word"))
    text = redacted_command(build_record_argv(cfg, url, tmp_path))
    assert "pa55word" not in text and "viewer" not in text and "***:***@192.0.2.10" in text


# ---------------------------------------------------------------- parsers
def test_progress_parser_blocks() -> None:
    p = ProgressParser()
    lines = ["frame=10", "fps=15.0", "out_time_us=N/A", "progress=continue",
             "frame=25", "fps=15.1", "out_time_us=1666667", "speed=1.0x", "progress=continue",
             "out_time_us=3000000", "progress=end"]
    snaps = [s for s in (p.feed(x) for x in lines) if s]
    assert [s.out_time_s for s in snaps] == [None, pytest.approx(1.666667), 3.0]
    assert snaps[1].frame == 25 and snaps[-1].ended


def test_segment_list_ignores_partial_line(tmp_path: Path) -> None:
    f = tmp_path / "segments.csv"
    r = SegmentListReader(f)
    assert r.read_new() == []                                  # file absent
    f.write_bytes(b"seg_000000.ts,0.000000,3.000000\nseg_000001.ts,3.0000")
    got = r.read_new()
    assert [e.filename for e in got] == ["seg_000000.ts"]
    with open(f, "ab") as fh:
        fh.write(b"00,6.000000\ngarbage-line\n")
    got = r.read_new()
    assert [(e.filename, e.start_s, e.end_s) for e in got] == [("seg_000001.ts", 3.0, 6.0)]
    assert r.bad_lines == ["garbage-line"]
    assert len(r.read_all()) == 2


# ---------------------------------------------------------------- time base
def test_timebase_takes_minimum_and_freezes() -> None:
    tb = RunTimebase(wall_launch=1_000_000.0, mono_launch=50.0)
    assert not tb.has_anchor and tb.iso_for(0) is None
    tb.observe(1.0, 52.5)        # latency 1.5 s after launch
    tb.observe(3.0, 54.2)        # better estimate: m - t = 51.2
    tb.observe(5.0, 56.9)        # worse, ignored
    assert tb.anchor_mono == pytest.approx(51.2)
    assert tb.anchor_epoch == pytest.approx(1_000_001.2)
    tb.freeze()
    tb.observe(7.0, 57.0)        # would lower the anchor; frozen, ignored
    assert tb.anchor_mono == pytest.approx(51.2)
    assert tb.epoch_for(10.0) == pytest.approx(1_000_011.2)


def test_clock_step_detector_ignores_drift_flags_jumps() -> None:
    d = ClockStepDetector(wall=1000.0, mono=10.0, tolerance_s=2.0)
    # 3 days of 50 ppm drift sampled every 0.5 s: never a step, however large it accumulates.
    wall, mono = 1000.0, 10.0
    for _ in range(1000):
        mono += 259.2
        wall += 259.2 * (1 + 50e-6)
        assert d.update(wall, mono) == 0.0
    assert d.total_s == 0.0
    mono += 0.5
    wall += 0.5 + 50.0                                   # NTP/operator steps the clock forward 50 s
    assert d.update(wall, mono) == pytest.approx(50.0)
    mono += 0.5
    wall += 0.5 - 80.0                                   # then back 80 s
    assert d.update(wall, mono) == pytest.approx(-80.0)
    assert d.total_s == pytest.approx(-30.0)


def test_iso_roundtrip() -> None:
    t = 1794279484.123456
    s = iso_utc(t)
    assert s.endswith("Z") and abs(parse_iso_utc(s) - t) < 1e-6
    assert add_seconds_iso("2026-11-09T23:59:59.500000Z", 1.0) == "2026-11-10T00:00:00.500000Z"
    with pytest.raises(ValueError):
        parse_iso_utc("2026-11-09T23:59:59.500000")              # no implicit timezone


# ---------------------------------------------------------------- backoff
def test_backoff_grows_and_resets() -> None:
    b = Backoff(1, 8, reset_after=60)
    assert [b.next_delay(0) for _ in range(5)] == [1, 2, 4, 8, 8]
    assert b.next_delay(120) == 1


# ---------------------------------------------------------------- storage
def test_storage_levels_and_estimate(tmp_path: Path) -> None:
    free = {"v": 100 * GB}
    mon = StorageMonitor(tmp_path, warning_free_gb=50, critical_free_gb=5,
                         disk_usage=lambda p: DiskUsage(500 * GB, 400 * GB, free["v"]))
    s = mon.sample()
    assert s.level is StorageLevel.OK and s.rate_bytes_per_s is None and s.seconds_until_critical is None
    mon.record_segment(300 * 1_000_000, 300.0)                # 1 MB/s measured
    s = mon.sample()
    assert s.rate_bytes_per_s == pytest.approx(1_000_000)
    assert s.seconds_until_critical == pytest.approx((95 * GB) / 1_000_000)
    free["v"] = 40 * GB
    assert mon.sample().level is StorageLevel.WARNING
    free["v"] = 5 * GB
    assert mon.sample().level is StorageLevel.CRITICAL


def test_storage_read_failure_is_critical_error(tmp_path: Path) -> None:
    def boom(_p: Path) -> DiskUsage:
        raise OSError("device gone")
    s = StorageMonitor(tmp_path, 50, 5, disk_usage=boom).sample()
    assert s.level is StorageLevel.CRITICAL and "device gone" in (s.error or "")


# ---------------------------------------------------------------- status decision
def _m(**kw: object) -> Measurements:
    base = dict(now_mono=1000.0, service_started_mono=0.0, ffmpeg_running=True, media_seen_this_run=True,
                seconds_since_media_advance=1.0, seconds_since_last_seal=10.0, storage_level=StorageLevel.OK,
                storage_error=None, component_errors=[], in_backoff=False)
    base.update(kw)
    return Measurements(**base)  # type: ignore[arg-type]


def decide_(m: Measurements) -> tuple[OverallStatus, CameraLink, list[str]]:
    return decide(m, stall_timeout_s=15, segment_seconds=300, connect_timeout_s=20)


def test_status_recording_requires_measured_progress() -> None:
    assert decide_(_m())[0] is OverallStatus.RECORDING
    # FFmpeg process alive but stream time not advancing => not "recording normally".
    st, link, _ = decide_(_m(seconds_since_media_advance=40.0))
    assert st is OverallStatus.INTERRUPTED and link is CameraLink.DISCONNECTED
    st, link, _ = decide_(_m(media_seen_this_run=False, seconds_since_media_advance=None))
    assert st is OverallStatus.INTERRUPTED and link is CameraLink.CONNECTING


def test_status_media_flowing_but_nothing_sealed_is_error() -> None:
    st, _, msgs = decide_(_m(seconds_since_last_seal=2000.0))
    assert st is OverallStatus.ERROR and "not being saved" in msgs[0]
    st, _, _ = decide_(_m(seconds_since_last_seal=None, now_mono=5000.0))
    assert st is OverallStatus.ERROR


def test_status_storage_and_component_errors() -> None:
    assert decide_(_m(storage_level=StorageLevel.WARNING))[0] is OverallStatus.STORAGE_WARNING
    assert decide_(_m(storage_level=StorageLevel.CRITICAL))[0] is OverallStatus.STORAGE_FULL
    assert decide_(_m(component_errors=["C-001"]))[0] is OverallStatus.ERROR
    assert decide_(_m(stopping=True))[0] is OverallStatus.STOPPED


def test_status_starting_grace_then_interrupted() -> None:
    m = _m(now_mono=5.0, ffmpeg_running=True, media_seen_this_run=False, seconds_since_media_advance=None,
           seconds_since_last_seal=None)
    assert decide_(m)[0] is OverallStatus.STARTING
    m.now_mono = 30.0
    assert decide_(m)[0] is OverallStatus.INTERRUPTED


def test_read_status_detects_stale_heartbeat(tmp_path: Path) -> None:
    f = tmp_path / "status.json"
    doc, problem = read_status(f)
    assert doc is None and "not running" in (problem or "")
    f.write_text('{"heartbeat_epoch": 0, "overall": "RECORDING"}')
    doc, problem = read_status(f)
    assert doc is not None and "not responding" in (problem or "")


# ---------------------------------------------------------------- hashing
def test_sha256_known_vector(tmp_path: Path) -> None:
    f = tmp_path / "abc.bin"
    f.write_bytes(b"abc")
    assert sha256_file(f) == ("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", 3)
