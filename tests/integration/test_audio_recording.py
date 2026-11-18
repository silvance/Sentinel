"""Recording the camera's sound (requirement change 2026-11-18; [camera] include_audio, off by default).

The real recorder with the simulated camera sending a 440 Hz tone. The simulator sends MPEG-TS, which carries
AAC and MP2 but not G.711: MP2 stands in for "a camera whose sound is not AAC". That G.711 copied into the
recording format cannot be read back, and that converting it works, was measured with the bundled FFmpeg
(docs/branches/audio-recording.md)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import Harness, contiguous, wait_for
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]


def streams(h: Harness, path: Path) -> list[tuple[str, str]]:
    cp = subprocess.run([h.cfg.tools.ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                         "-of", "json", str(path)], capture_output=True, timeout=60)
    return [(s["codec_type"], s["codec_name"]) for s in json.loads(cp.stdout or b"{}").get("streams", [])]


def sound_codes(st: dict) -> list[str]:
    return [a["code"] for a in st.get("alerts", []) if a["code"].startswith("N-")]


def test_sound_is_converted_to_aac_and_the_video_is_still_copied(tmp_path: Path) -> None:
    with SimulatedCamera(fps=15, audio="mp2") as cam:
        h = Harness(tmp_path, cam.url, camera={"include_audio": True}).start()
        try:
            st = wait_for(lambda: (s := h.status()).get("recording", {}).get("audio", {}).get("stored") and s, 40,
                          what="the recording read back for its sound")
            audio = st["recording"]["audio"]
            assert audio["action"] == "aac" and audio["stored"]["codec"] == "aac" and not audio["suspended"]
            assert audio["text"] == "Recorded (AAC, 16000 Hz, 1 channel; converted by the recorder)"
            assert st["overall"] == "RECORDING" and sound_codes(st) == []
            h.wait_sealed(4, timeout=40)
        finally:
            h.stop()
    segs = h.segments("FINALIZED")
    assert len(segs) >= 4 and {r["end_reason"] for r in h.runs()} == {"graceful_stop"}
    for a, b in zip(segs, segs[1:]):
        assert contiguous(a, b), "sound must not open gaps between files"
    for s in segs[:3]:
        assert streams(h, h.path(s)) == [("video", "h264"), ("audio", "aac")]
        assert json.loads(s["validation_detail"])["audio"] == {"codec": "aac", "sample_rate": 16000, "channels": 1}
        h.assert_sealed_integrity(s)
    assert "-c:v copy -c:a aac" in h.runs()[0]["command_redacted"]
    said = h.events("audio_recorded")
    assert len(said) == 1 and "converted to AAC by the recorder" in said[0]["message"]
    assert len(h.events("audio_enabled")) == 1 and not h.events("audio_missing")
    assert not [e for e in h.events() if e["severity"] == "error"]


def test_sound_kept_as_the_camera_sends_it(tmp_path: Path) -> None:
    with SimulatedCamera(fps=15, audio="aac") as cam:
        h = Harness(tmp_path, cam.url, camera={"include_audio": True, "audio_format": "copy"}).start()
        try:
            st = wait_for(lambda: (s := h.status()).get("recording", {}).get("audio", {}).get("stored") and s, 40,
                          what="the recording read back for its sound")
            assert st["recording"]["audio"]["text"] == "Recorded (AAC, 16000 Hz, 1 channel; as sent by the camera)"
            h.wait_sealed(3, timeout=40)
        finally:
            h.stop()
    seg = h.segments("FINALIZED")[0]
    assert streams(h, h.path(seg)) == [("video", "h264"), ("audio", "aac")]
    cmd = h.runs()[0]["command_redacted"]
    assert "-map 0:a:0? -dn -sn -c copy" in cmd and "-c:a aac" not in cmd


def test_sound_switched_on_but_the_camera_sends_none(tmp_path: Path) -> None:
    """The mic is not there yet: video is recorded as always, and the screen says the recordings have no sound."""
    with SimulatedCamera(fps=15) as cam:
        h = Harness(tmp_path, cam.url, camera={"include_audio": True}).start()
        try:
            st = wait_for(lambda: (s := h.status()) and "N-001" in sound_codes(s) and s, 40, what="N-001")
            assert st["overall"] == "RECORDING" or st["alerts"]            # video goes on
            assert st["recording"]["audio"]["text"] == "NOT recorded: the camera sends no sound"
            n = next(a for a in st["alerts"] if a["code"] == "N-001")
            assert "Video recording continues" in n["message"] and not n["recording_stopped"]
            h.wait_sealed(4, timeout=40)
        finally:
            h.stop()
    segs = h.segments("FINALIZED")
    assert len(segs) >= 4 and streams(h, h.path(segs[0])) == [("video", "h264")]
    assert "audio" not in json.loads(segs[0]["validation_detail"])
    assert len(h.events("audio_missing")) == 1 and not h.events("audio_recorded")
    assert {r["end_reason"] for r in h.runs()} == {"graceful_stop"}


def test_sound_is_not_recorded_unless_switched_on(tmp_path: Path) -> None:
    with SimulatedCamera(fps=15, audio="aac") as cam:
        h = Harness(tmp_path, cam.url).start()
        try:
            h.wait_sealed(3, timeout=40)
            st = h.status()
            assert "audio" not in st["recording"] and sound_codes(st) == []
        finally:
            h.stop()
    seg = h.segments("FINALIZED")[0]
    assert streams(h, h.path(seg)) == [("video", "h264")]
    assert "audio" not in json.loads(seg["validation_detail"])
    assert " -an " in h.runs()[0]["command_redacted"]
    assert not h.events("audio_enabled") and not h.events("audio_recorded")


def test_review_drive_videos_have_the_sound(tmp_path: Path) -> None:
    from sentinel.db import Ledger
    from sentinel.export.engine import ReviewExporter
    from sentinel.export.target import ExportTarget
    from sentinel.media.probe import ffmpeg_version
    from sentinel.platform import get_platform
    from sentinel.recorder.integrity import IntegrityWorker

    with SimulatedCamera(fps=15, audio="mp2") as cam:
        h = Harness(tmp_path, cam.url, camera={"include_audio": True},
                    recording={"display_timezone": "America/New_York"}).start()
        try:
            h.wait_sealed(5, timeout=50)
        finally:
            h.stop()
    led = Ledger(h.cfg.data_path / "sentinel.db")
    try:
        w = IntegrityWorker(h.cfg, led, get_platform(), ffmpeg_version(h.cfg.tools.ffmpeg))
        for s in led.segments_needing_content_hash():
            w.hash_segment(s["segment_id"])
        root = tmp_path / "drive"
        root.mkdir()
        res = ReviewExporter(h.cfg, ExportTarget(root, {"drive_uuid": "t-1", "label": "REVIEW"}), ledger=led,
                             allow_same_volume=True, operator_id="op").run()
        assert res.state == "COMPLETE", res
    finally:
        led.close()
    man = json.loads(next(root.glob("Manifests/*/Export_Manifest.json")).read_text("utf-8"))
    assert man["derivatives"]
    for d in man["derivatives"]:
        assert d["audio"] == {"included": True, "in_originals": ["aac"], "in_every_source": True,
                              "made_by": "copied unchanged (-c:a copy)"}
        assert streams(h, root / d["path"]) == [("video", "h264"), ("audio", "aac")]
        assert d["content_check"]["level"]                       # the picture check ran as before
    html = next(root.glob("Reports/*.html")).read_text("utf-8")
    assert "<b>Sound.</b>" in html and "with sound" in html
