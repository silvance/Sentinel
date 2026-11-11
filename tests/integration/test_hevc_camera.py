"""An H.265 (HEVC) camera end to end (owner request 2026-11-10: the test camera N803-HS-5X sends H.265).

Recording copies the stream unchanged, so H.265 is recorded, validated, sealed and fingerprinted like
H.264. Review copies are MP4 files tagged ``hvc1`` (what Windows and Apple players expect), they
decode to exactly the same frames as the originals, and the summary tells reviewers how to play them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import Harness, wait_for
from sentinel.db import Ledger
from sentinel.export.engine import ReviewExporter
from sentinel.export.target import ExportTarget
from sentinel.hashing import sha256_file
from sentinel.media.probe import ffmpeg_version, probe_segment
from sentinel.platform import get_platform
from sentinel.recorder.integrity import IntegrityWorker
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]


def _has_libx265() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
    except OSError:
        return False
    return "libx265" in out.stdout


needs_x265 = pytest.mark.skipif(not _has_libx265(), reason="the FFmpeg on PATH has no libx265 (test camera)")


@needs_x265
def test_h265_camera_is_recorded_sealed_fingerprinted_and_exported(tmp_path: Path) -> None:
    cam = SimulatedCamera(fps=15, codec="hevc").start()
    h = Harness(tmp_path, cam.url, recording={"display_timezone": "America/New_York"})
    h.start()
    try:
        h.wait_sealed(3, timeout=90)
        st = wait_for(lambda: (s := h.status()).get("overall") == "RECORDING" and s, 20, what="RECORDING")
        assert st["overall"] == "RECORDING"
    finally:
        h.stop()
        cam.stop()
    assert h.exit_code == 0

    led = Ledger(h.cfg.data_path / "sentinel.db")
    try:
        w = IntegrityWorker(h.cfg, led, get_platform(), ffmpeg_version(h.cfg.tools.ffmpeg))
        for s in led.segments_needing_content_hash():
            w.hash_segment(s["segment_id"])
        sealed = [dict(r) for r in led.query("SELECT * FROM segments WHERE state IN ('FINALIZED','INTERRUPTED') "
                                             "ORDER BY start_utc")]
        assert len(sealed) >= 3
        for s in sealed:
            assert s["video_codec"] == "hevc", s
            assert s["sha256"] and s["content_sha256"], "sealed and fingerprinted like H.264"
            assert probe_segment(h.cfg.spool_path / s["rel_path"]).playable

        drive = tmp_path / "drive"
        drive.mkdir()
        target = ExportTarget(drive, {"drive_uuid": "test-hevc", "label": "REVIEW_HEVC"})
        from conftest import make_config

        cfg = make_config(tmp_path, "tcp://127.0.0.1:9", recording={"display_timezone": "America/New_York"},
                          export={"content_check": "full"})
        res = ReviewExporter(cfg, target, ledger=led, allow_same_volume=True, operator_id="op-1").run()
        assert res.state == "COMPLETE", res
    finally:
        led.close()

    man_path = next(drive.rglob("Export_Manifest.json"))
    man = json.loads(man_path.read_text("utf-8"))
    assert man["derivatives"]
    for d in man["derivatives"]:
        p = drive / d["path"]
        r = probe_segment(p)
        assert r.playable and r.codec == "hevc", r.detail()
        tag = subprocess.run([cfg.tools.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=codec_tag_string", "-of", "default=nw=1:nk=1", str(p)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        assert tag == "hvc1", tag
        assert d["content_check"]["level"] == "full" and d["content_sha256"], "same frames as the originals"
        assert d["sha256"] == sha256_file(p)[0]
    html = next((drive / "Reports").iterdir()).read_text("utf-8")
    assert "H.265 (HEVC)" in html and "HEVC Video Extensions" in html


@needs_x265
def test_motion_is_detected_on_an_h265_stream(tmp_path: Path) -> None:
    """Motion detection decodes the main stream's key frames: also with H.265."""
    import time

    from sentinel.clock import parse_iso_utc

    with SimulatedCamera(fps=15, codec="hevc", motion=[(12, 18)]) as cam:
        t0 = time.time()
        h = Harness(tmp_path, cam.url, motion={"markers": True},
                    recording={"display_timezone": "America/New_York"}).start()
        try:
            wait_for(lambda: time.time() - t0 > 26, 40, what="recording through the motion")
        finally:
            h.stop()
    periods = [json.loads(e["detail"]) for e in h.events("motion_period")]
    assert len(periods) == 1, periods
    start = parse_iso_utc(periods[0]["start_utc"])
    assert t0 + 11.0 <= start <= t0 + 16.0, start - t0
