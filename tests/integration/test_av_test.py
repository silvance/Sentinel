"""Quick test of the camera's picture and sound, end to end with the simulated camera (0.3.22): a few seconds
are recorded straight from the camera, the report says what arrived, and the clip plays (it holds the picture
and, when the camera sends sound, the sound)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import requires_ffmpeg, tools_for_tests
from sentinel import avtest
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]


def tools() -> tuple[str, str]:
    t = tools_for_tests() or {}
    return t.get("ffmpeg", "ffmpeg"), t.get("ffprobe", "ffprobe")


def streams(clip: Path) -> list[dict]:
    out = subprocess.run([tools()[1], "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json",
                          str(clip)], capture_output=True, check=True).stdout
    return json.loads(out)["streams"]


def test_picture_and_sound_arrive_and_the_clip_holds_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(avtest.tempfile, "gettempdir", lambda: str(tmp_path))
    ffmpeg, ffprobe = tools()
    said: list[str] = []
    with SimulatedCamera(fps=15, audio="aac") as cam:                 # a 440 Hz tone, as a camera with a microphone
        code, clip = avtest.av_test(ffmpeg, ffprobe, cam.url, "tcp", "mpegts", seconds=4, out=said.append,
                                    records_sound=False)
    text = "\n".join(said)
    assert code == 0 and clip == tmp_path / "Sentinel-camera-test" / "camera-test.mp4", text
    assert said[0] == "Asking the camera what it sends..." and said[1].startswith("Recording 4 seconds for the test")
    assert "Picture: H.264, 640x480, 15 pictures a second;" in text
    sound = next(s for s in said if s.startswith("Sound:"))
    assert sound.startswith("Sound: the camera sends AAC, 16000 Hz, 1 channel. In this test: loudest -"), sound
    assert "SILENT" not in sound and "Play the clip to hear it." in sound
    assert "Sentinel is NOT set to record sound" in text and "It is not evidence." in text
    kinds = {s["codec_type"]: s["codec_name"] for s in streams(clip)}
    assert kinds == {"video": "h264", "audio": "aac"}                 # plays anywhere: picture copied, sound as AAC
    assert clip.stat().st_size > 20_000
    assert avtest.delete_clip() and not clip.exists()


def test_a_camera_without_sound_is_said_plainly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(avtest.tempfile, "gettempdir", lambda: str(tmp_path))
    ffmpeg, ffprobe = tools()
    said: list[str] = []
    with SimulatedCamera(fps=15) as cam:
        code, clip = avtest.av_test(ffmpeg, ffprobe, cam.url, "tcp", "mpegts", seconds=3, out=said.append)
    assert code == 0 and clip is not None, said
    assert "Sound: the camera sends no sound." in said and not any(s.startswith("Sentinel is") for s in said)
    assert [s["codec_type"] for s in streams(clip)] == ["video"]
    # the next test replaces the clip; a camera that is not there leaves none
    code, gone = avtest.av_test(ffmpeg, ffprobe, "tcp://127.0.0.1:9", "tcp", "mpegts", seconds=3, out=said.append)
    assert (code, gone) == (4, None) and not clip.exists()
