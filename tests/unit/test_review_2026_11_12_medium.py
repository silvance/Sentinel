"""Medium findings of the 2026-11-12 overnight review (docs/review-2026-11-12-findings.json): recorder,
service status, media, motion, live-view backend and self-check."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_config
from gui_support import base_measurements, status_doc
from sentinel import preview, selfcheck
from sentinel.clock import iso_utc, parse_iso_utc
from sentinel.db import Ledger
from sentinel.models import OverallStatus, RunEndReason
from sentinel.motion import FRAME_BYTES, MotionAnalyzer, MotionConfig
from sentinel.recorder.supervisor import Backoff, LiveState, RecordingSupervisor, RunOutcome
from sentinel.status import evaluate


def T(hms: str) -> str:
    return f"2026-11-10T{hms}.000000Z"


def frame(level: int, box: tuple[int, int] | None = None) -> bytes:
    b = bytearray([level]) * FRAME_BYTES
    if box:
        x, y = box
        for r in range(y, y + 20):
            b[r * 160 + x:r * 160 + x + 20] = bytes([255]) * 20
    return bytes(b)


# ---------------------------------------------------------------------- 1. content hash timeouts
def test_content_hash_timeout_scales_with_the_segment_length() -> None:
    from sentinel.media.videohash import content_hash_timeout

    assert content_hash_timeout(3600.0) == 4 * 3600.0                 # 1-hour segment: 4 h allowed
    assert content_hash_timeout(None, 3600.0) == 4 * 3600.0           # duration unknown: segment length
    assert content_hash_timeout(300.0) == 3600.0                      # short segments keep the old minimum
    assert content_hash_timeout(None, None) == 3600.0


def test_content_hash_timeout_is_a_backlog_not_an_operator_alert(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.media.probe import ProbeError
    from sentinel.media.videohash import ContentHashTimeout
    from sentinel.platform import get_platform
    from sentinel.recorder import integrity as integrity_mod
    from sentinel.recorder.integrity import IntegrityWorker

    cfg = make_config(tmp_path)
    led = Ledger(tmp_path / "s.db")
    try:
        iw = IntegrityWorker(cfg, led, get_platform(), "ffmpeg-test")
        seg = {"content_sha256": None, "sha256": "ab" * 32, "rel_path": "CAM01/r/seg_000000.ts", "pix_fmt": None,
               "content_hash_attempts": 0, "duration_s": 3600.0}
        monkeypatch.setattr(led, "get_segment", lambda _sid: seg)
        monkeypatch.setattr(led, "update_segment", lambda _sid, **kw: None)
        seen: dict[str, float] = {}

        def too_slow(*_a, timeout: float, **_k):  # noqa: ANN202
            seen["timeout"] = timeout
            raise ContentHashTimeout(f"content hashing timed out after {timeout:.0f}s")

        monkeypatch.setattr(integrity_mod, "compute_content_hash", too_slow)
        assert iw.hash_segment("s1") is False
        assert seen["timeout"] == 4 * 3600.0
        assert iw.timeouts == 1 and not iw.recent_errors            # no V-001 for a slow machine

        def broken(*_a, **_k):  # noqa: ANN202
            raise ProbeError("FFmpeg content hashing failed: damaged")

        monkeypatch.setattr(integrity_mod, "compute_content_hash", broken)
        iw.hash_segment("s1")
        assert len(iw.recent_errors) == 1                             # a real failure still alerts
    finally:
        led.close()


def test_decode_validation_allows_the_footage_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.media import probe

    seg = tmp_path / "seg.ts"
    seg.write_bytes(b"x" * 100)
    timeouts: list[float] = []
    out = {"streams": [{"codec_type": "video", "codec_name": "h264"}], "format": {"duration": "3600.0"},
           "packets": [{"pts_time": "0.0", "dts_time": "0.0", "flags": "K_"},
                       {"pts_time": "0.04", "dts_time": "0.04", "flags": "__"}]}

    def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
        timeouts.append(timeout)
        return subprocess.CompletedProcess(argv, 0, json.dumps(out).encode(), b"")

    monkeypatch.setattr(probe, "_run", fake_run)
    res = probe.probe_segment(seg, level="decode")
    assert res.playable and timeouts == [300.0, 3600.0]


# ---------------------------------------------------------------------- 2. motion on a slow second stream
def _moving(n: int) -> list[bytes]:
    return [frame(100, ((i * 30) % 140, 10)) for i in range(n)]


def test_repeated_frames_from_the_fps_filter_do_not_hide_motion() -> None:
    m = MotionConfig(stream_path="sub", analysis_fps=4, min_frames=2)
    a = MotionAnalyzer(m)
    confirmed = [a.feed(f) for src in _moving(6) for f in (src, src)]   # a 2 fps stream at analysis_fps 4
    assert any(confirmed)


def test_a_frozen_second_stream_still_counts_as_no_change() -> None:
    a = MotionAnalyzer(MotionConfig(stream_path="sub", analysis_fps=4, min_frames=3))
    frames = _moving(3)
    a.feed(frames[0])
    a.feed(frames[1])
    assert a.run_length == 1
    for _ in range(20):                                    # frozen far longer than the fps filter repeats
        a.feed(frames[1])
    assert a.run_length == 0
    assert a.feed(frames[2]) is False                      # one new change is not confirmed motion


def test_main_stream_key_frames_are_unchanged() -> None:
    a = MotionAnalyzer(MotionConfig(min_frames=2))         # key-frame path: no repeats are made
    f = _moving(2)
    a.feed(f[0])
    a.feed(f[1])
    assert a.run_length == 1
    a.feed(f[1])                                           # identical key frame: the picture did not change
    assert a.run_length == 0


# ---------------------------------------------------------------------- 3/4. live view backend
def test_latest_names_the_pictures_own_time_after_the_camera_stops(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    t = {"now": 1_800_000_000.0}
    w = preview.PreviewWorker(cfg, "tcp://127.0.0.1:9", clock=lambda: t["now"])
    w.state = "live"
    w._publish(b"\xff\xd8a\xff\xd9")
    doc = preview.read_latest(cfg)
    assert doc is not None and doc["live"] is True and doc["taken_utc"] == iso_utc(t["now"])
    t["now"] += 30                                          # camera gone: the worker reconnects
    for state in ("starting", "no_picture"):
        w.state = state
        w._write_state()
        doc = preview.read_latest(cfg)
        assert doc["taken_utc"] == iso_utc(1_800_000_000.0)  # still the picture's time, not "now"
        assert doc["state_utc"] == iso_utc(t["now"]) and doc["live"] is False


def test_stop_request_ends_the_live_view(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    req = preview.request_preview(cfg)
    assert preview.read_request(req, time.time()) == (True, None)
    preview.stop_preview(cfg)
    assert preview.read_request(req, time.time()) == (False, None)
    preview.request_preview(cfg, session="abc-1")
    assert preview.read_request(req, time.time()) == (True, "abc-1")
    with pytest.raises(ValueError):
        preview.request_preview(cfg, session="bad token")
    req.write_text(f"{iso_utc(time.time())} ../../x", encoding="ascii")
    assert preview.read_request(req, time.time()) == (True, None)          # untrusted: no odd token taken


def test_a_viewing_session_is_capped_and_a_new_session_renews_it(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    mono = {"t": 0.0}
    w = preview.PreviewWorker(cfg, "tcp://127.0.0.1:9", mono=lambda: mono["t"])
    preview.request_preview(cfg, session="one")
    assert w._viewer() is True
    mono["t"] += preview.MAX_SESSION_S + 1
    preview.request_preview(cfg, session="one")                # the window keeps refreshing, nobody looks
    assert w._viewer() is False and w._session == "one"
    preview.request_path(cfg).write_bytes(b"")                 # caught while the window rewrites it
    assert w._viewer() is False and w._session == "one"        # not a new session
    preview.request_preview(cfg, session="two")                # the user asks for the picture again
    assert w._viewer() is True
    preview.stop_preview(cfg)
    assert w._viewer() is False and w._session is None
    preview.request_path(cfg).write_bytes(b"")                 # first look caught mid-rewrite: no session yet
    assert w._viewer() is False and w._session is None


def test_worker_pauses_a_session_that_runs_too_long(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    mono = {"t": 0.0}
    w = preview.PreviewWorker(cfg, "tcp://127.0.0.1:9", mono=lambda: mono["t"])
    runs: list[int] = []

    def fake_ffmpeg() -> bool:
        runs.append(1)
        mono["t"] += preview.MAX_SESSION_S + 1                 # this session has now run too long
        w._stop.wait(0.05)
        return True

    w._run_ffmpeg = fake_ffmpeg  # type: ignore[method-assign]
    preview.request_preview(cfg, session="one")
    w.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (preview.read_latest(cfg) or {}).get("state") != "paused":
            preview.request_preview(cfg, session="one")
            time.sleep(0.05)
        assert preview.read_latest(cfg)["state"] == "paused" and len(runs) == 1
    finally:
        w.stop()


# ---------------------------------------------------------------------- 5/6. supervisor
class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def wall(self) -> float:
        return self.t

    def mono(self) -> float:
        return self.t - 1_700_000_000.0


def _loop_supervisor(tmp_path: Path, mode: str, motion: object, runs: int,
                     outcome: RunOutcome) -> RecordingSupervisor:
    sup = RecordingSupervisor.__new__(RecordingSupervisor)
    sup.cfg = make_config(tmp_path)
    sup.schedule = SimpleNamespace(mode_at=lambda _t: mode)
    sup.motion = motion
    sup.clock = _Clock()
    sup.live, sup.stop_event, sup._first_attempt = LiveState(), threading.Event(), False
    sup.cam_dir = tmp_path / "spool"
    sup._tick = lambda: None
    sup.ledger = SimpleNamespace(switch_open_gap=lambda *a, **k: None, add_event=lambda *a, **k: None)
    sup.storage = SimpleNamespace(sample=lambda: SimpleNamespace(level=SimpleNamespace(value="ok"), free_bytes=1))
    sup.backoff = Backoff(0.0, 0.0, 0.0)
    count = {"n": 0}

    def run_once() -> RunOutcome:
        count["n"] += 1
        if count["n"] >= runs:
            sup.stop_event.set()
        return outcome

    sup.run_once = run_once  # type: ignore[method-assign]
    return sup


def _detector(healthy: bool, since: float | None) -> SimpleNamespace:
    return SimpleNamespace(healthy=lambda: healthy, seconds_since_motion=lambda: since,
                           recording_wanted=lambda: True, warming_up=lambda: False,
                           snapshot=lambda: {"seconds_since_motion": since})


def test_fail_safe_runs_are_not_motion_recording_failures(tmp_path: Path) -> None:
    """Camera off in a motion period: the detector has no pictures, so the fail safe records; those
    failed runs are a camera outage (R-003), not "Motion was detected but could not be recorded"."""
    fail = RunOutcome(RunEndReason.CONNECT_TIMEOUT, 1, 0.0)
    sup = _loop_supervisor(tmp_path, "motion", _detector(False, None), 3, fail)
    sup.run_forever()
    assert sup.live.motion_record_failures == 0
    sup = _loop_supervisor(tmp_path, "motion", _detector(True, 5.0), 3, fail)    # motion seen, not recorded
    sup.run_forever()
    assert sup.live.motion_record_failures == 3


def test_motion_failures_are_not_carried_into_other_schedule_modes(tmp_path: Path) -> None:
    sup = _loop_supervisor(tmp_path, "record", None, 1, RunOutcome(RunEndReason.CONNECT_TIMEOUT, 1, 0.0))
    sup.live.motion_record_failures = 2
    sup.run_forever()
    assert sup.live.motion_record_failures == 0


def test_motion_run_ended_by_a_schedule_pause_is_not_a_failure(tmp_path: Path) -> None:
    sup = _loop_supervisor(tmp_path, "motion", _detector(True, 5.0), 1,
                           RunOutcome(RunEndReason.SCHEDULED_PAUSE, 0, 0.0))
    sup.run_forever()
    assert sup.live.motion_record_failures == 0


def test_w001_does_not_say_recording_continues_when_nothing_records() -> None:
    m = base_measurements(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                          in_backoff=True, outage_seconds=200.0, last_end_reason="connect_timeout", attempts=3,
                          motion_broken=True)
    d = evaluate(m, stall_timeout_s=10, segment_seconds=3600, connect_timeout_s=20, escalate_after_s=180)
    assert [a.code for a in d.alerts] == ["R-003"]
    d = evaluate(base_measurements(motion_broken=True), stall_timeout_s=10, segment_seconds=3600,
                 connect_timeout_s=20, escalate_after_s=180)
    assert [a.code for a in d.alerts] == ["W-001"]                     # recording: the warning stays


def test_storage_critical_after_a_pause_ends_the_planned_gap(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.db")
    try:
        led.create_run(run_id="R1", camera_id="CAM01", directory="CAM01/R1", launched_utc=T("19:00:00"),
                       launch_mono=0.0, display_timezone="UTC", app_version="test")
        evening = "2026-11-09T20:00:00.000000Z"
        led.begin_gap("CAM01", evening, "scheduled_pause", None, "R1", "segment_end")
        sup = RecordingSupervisor.__new__(RecordingSupervisor)
        sup.cfg = make_config(tmp_path)
        sup.ledger, sup.schedule, sup.motion = led, None, None
        clock = _Clock()
        clock.t = parse_iso_utc(T("06:00:00"))                     # the pause has just ended
        sup.clock = clock
        sup.live, sup.stop_event, sup._first_attempt = LiveState(), threading.Event(), False
        sup.cam_dir = tmp_path / "spool"
        sup.storage = SimpleNamespace(sample=lambda: SimpleNamespace(level=SimpleNamespace(value="critical"),
                                                                     free_bytes=1))
        sup._wait = lambda _s: sup.stop_event.set()  # type: ignore[method-assign]
        sup.run_forever()
        gaps = [(g["reason"], g["start_utc"], g["end_utc"]) for g in led.gaps("CAM01")]
        assert gaps[0] == ("scheduled_pause", evening, T("06:00:00"))
        assert gaps[1] == ("storage_critical", T("06:00:00"), None)
    finally:
        led.close()


# ---------------------------------------------------------------------- 8. late gap record
def test_late_gap_record_leaves_a_later_runs_open_gap_alone(tmp_path: Path) -> None:
    from sentinel.ledgerchain import verify_ledger
    from sentinel.recorder.finalizer import Finalizer, GapJob

    led = Ledger(tmp_path / "s.db")
    try:
        for rid, launched, ended in (("A", "00:50:00", "01:00:00"), ("B", "01:00:30", "01:01:10")):
            led.create_run(run_id=rid, camera_id="CAM01", directory=f"CAM01/{rid}", launched_utc=T(launched),
                           launch_mono=0.0, display_timezone="UTC", app_version="test")
            led.update_run(rid, ended_utc=T(ended), end_reason="stall")
        sa = led.insert_segment(camera_id="CAM01", run_id="A", seq=0, rel_path="CAM01/A/seg_000000.ts")
        led.update_segment(sa, start_utc=T("00:50:00"), end_utc=T("01:00:00"))
        sb = led.insert_segment(camera_id="CAM01", run_id="B", seq=0, rel_path="CAM01/B/seg_000000.ts")
        led.update_segment(sb, start_utc=T("01:00:35"), end_utc=T("01:01:10"))
        fin = Finalizer.__new__(Finalizer)
        fin.ledger = led
        fin.process_gap(GapJob("B", "stall", {}))                       # B's gap: the camera is down now
        fin.process_gap(GapJob("A", "ffmpeg_error", {"retried_by": "janitor"}))   # A's gap, retried late
        gaps = {g["ended_run_id"]: g for g in led.gaps("CAM01")}
        assert (gaps["A"]["start_utc"], gaps["A"]["end_utc"]) == (T("01:00:00"), T("01:00:35"))
        assert gaps["A"]["duration_s"] == pytest.approx(35.0) and gaps["A"]["clock_anomaly"] == 0
        assert gaps["A"]["next_segment_id"] == sb and gaps["A"]["reason"] == "ffmpeg_error"
        still_open = led.open_gap("CAM01")
        assert still_open is not None and still_open["ended_run_id"] == "B"
        assert still_open["start_utc"] == T("01:01:10") and still_open["attempts"] == 1
        assert not led.runs_needing_gap()
        assert verify_ledger(led.path).ok
    finally:
        led.close()


def test_late_gap_already_covered_by_the_open_gap_adds_nothing(tmp_path: Path) -> None:
    from sentinel.recorder.finalizer import Finalizer, GapJob

    led = Ledger(tmp_path / "s.db")
    try:
        for rid in ("A", "B"):
            led.create_run(run_id=rid, camera_id="CAM01", directory=f"CAM01/{rid}", launched_utc=T("01:00:00"),
                           launch_mono=0.0, display_timezone="UTC", app_version="test")
            led.update_run(rid, ended_utc=T("01:00:20"), end_reason="connect_timeout")
        led.begin_gap("CAM01", T("00:59:00"), "connect_timeout", None, "B", "segment_end")
        fin = Finalizer.__new__(Finalizer)
        fin.ledger = led
        fin.process_gap(GapJob("A", "connect_timeout", {}))
        assert len(led.gaps("CAM01")) == 1 and led.open_gap("CAM01")["ended_run_id"] == "B"
        assert not [r for r in led.runs_needing_gap() if r["run_id"] == "A"]
    finally:
        led.close()


# ---------------------------------------------------------------------- 9/10. service status
class _SvcClock:
    def __init__(self) -> None:
        self.m = 0.0

    def mono(self) -> float:
        return self.m

    def wall(self) -> float:
        return 1_800_000_000.0 + self.m


def _service(tmp_path: Path):  # noqa: ANN202
    from sentinel.service import RecorderService

    clock = _SvcClock()
    svc = RecorderService(make_config(tmp_path), clock=clock)
    svc.finalizer = SimpleNamespace(has_recent_errors=lambda: False, last_good_seal_mono=None,
                                    last_corrupt_mono=None, pending=lambda: 0)
    live = LiveState()
    svc.supervisor = SimpleNamespace(live=live)
    return svc, clock, live


def test_no_false_r005_when_video_first_arrives_long_after_the_start(tmp_path: Path) -> None:
    svc, clock, live = _service(tmp_path)
    clock.m = 7205.0                                          # 2 h after the start: nothing sealed yet
    live.ffmpeg_running = live.media_seen = True
    live.media_since_mono, live.last_advance_mono, live.last_media_mono_any = 7200.0, 7204.5, 7204.5
    doc = svc._build_status(clock.m, stopping=False)
    assert "R-005" not in [a["code"] for a in doc.alerts] and doc.overall == str(OverallStatus.RECORDING)
    clock.m = 7200.0 + 3 * 3600                               # video for 3 h and still nothing saved
    live.last_advance_mono = live.last_media_mono_any = clock.m - 0.5
    doc = svc._build_status(clock.m, stopping=False)
    assert "R-005" in [a["code"] for a in doc.alerts]


def test_motion_clips_are_not_restarts(tmp_path: Path) -> None:
    from sentinel.service import unstable_restarts

    motion_clips = [(100.0 * i, 70.0, "motion_idle") for i in range(5)]
    assert unstable_restarts(motion_clips, 500.0) == 0
    drops = [(100.0 * i, 70.0, "stall") for i in range(5)]
    assert unstable_restarts(drops, 500.0) == 5
    svc, clock, live = _service(tmp_path)
    clock.m = 500.0
    live.recent_run_ends.extend(motion_clips)
    assert "R-004" not in [a["code"] for a in svc._build_status(clock.m, stopping=False).alerts]
    live.recent_run_ends.extend(drops)
    assert "R-004" in [a["code"] for a in svc._build_status(clock.m, stopping=False).alerts]


# ---------------------------------------------------------------------- 7/11. self-check
def _cfg(tmp: Path) -> Path:
    from sentinel.launcher import setup
    from sentinel.paths import Layout

    r = tmp / "m"
    lay = Layout("machine", r / "config" / "sentinel.toml", r / "config" / "camera.secret", r / "spool", r / "data",
                 r / "logs")
    p, _ = setup.write_setup(setup.SetupAnswers("192.0.2.5", "s", username="viewer", password="pw1"), lay)
    return p


@pytest.mark.parametrize("overall_m", [base_measurements(), base_measurements(stopping=True)])
def test_an_outdated_status_is_a_recorder_that_does_not_answer(tmp_path: Path, overall_m) -> None:  # noqa: ANN001
    from sentinel.status import read_status

    cfg = _cfg(tmp_path)
    now = time.time()
    status = tmp_path / "status.json"
    status.write_text(json.dumps(status_doc(overall_m, now, heartbeat_age_s=3600, last_segment_end=now - 3600)),
                      encoding="utf-8")
    for require in (True, False):
        rep = selfcheck.run_checks(cfg, lambda: read_status(status, max_age_s=15.0), now, require)
        assert not rep.ok
        assert [c.name for c in rep.failures] == ["Recorder running"]      # the update watch goes back
        assert "X-001" in rep.failures[0].detail


def test_restore_keeps_the_known_good_bytes(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    lf = p.read_bytes().replace(b"\r\n", b"\n")
    good = selfcheck.good_path(p)
    good.write_bytes(lf)                                       # e.g. a hand-copied example with LF endings
    p.write_bytes(lf.replace(b"192.0.2.5:", b"192.0.2.99:").replace(b"\n", b"\r\n"))
    selfcheck.restore(p, good)
    assert p.read_bytes() == lf and selfcheck.same_settings(good, p)
