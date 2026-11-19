"""Quick test of the camera's picture and sound (owner request 2026-11-18, 0.3.22): what the test says for
what arrived, the FFmpeg commands it uses, and that the clip never stays. The recording itself is tried with
the simulated camera in tests/integration/test_av_test.py."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from sentinel import avtest
from sentinel.avtest import (parse_volume, picture_sentence, record_argv, run_av_test, sound_sentence, volume_argv)
from sentinel.launcher.cli import build_parser

G711 = {"video": True, "codec": "pcm_alaw", "sample_rate": 8000, "channels": 1}
LOUD = """[Parsed_volumedetect_0 @ 000001c2] n_samples: 80000
[Parsed_volumedetect_0 @ 000001c2] mean_volume: -27.4 dB
[Parsed_volumedetect_0 @ 000001c2] max_volume: -5.1 dB
[Parsed_volumedetect_0 @ 000001c2] histogram_5db: 12
"""


def test_how_loud_is_read_from_ffmpegs_lines() -> None:
    assert parse_volume(LOUD) == {"mean": -27.4, "max": -5.1}
    assert parse_volume("mean_volume: -91.0 dB\nmax_volume: -91.0 dB") == {"mean": -91.0, "max": -91.0}
    assert parse_volume("mean_volume: -inf dB\nmax_volume: -inf dB") == {"mean": -120.0, "max": -120.0}
    assert parse_volume("Output file #0 does not contain any stream") == {}


def test_what_it_says_about_the_sound() -> None:
    assert sound_sentence(None, False, {}) == "Sound: the camera sends no sound."
    assert sound_sentence({"video": True, "codec": None}, False, {}) == "Sound: the camera sends no sound."
    heard = sound_sentence(G711, True, {"mean": -27.4, "max": -5.1})
    assert heard == ("Sound: the camera sends G.711 A-law, 8000 Hz, 1 channel. In this test: loudest -5 dB, average "
                     "-27 dB (0 dB is the loudest possible). Play the clip to hear it.")
    # a sound stream with nothing in it: a microphone input with no microphone
    silent = sound_sentence(G711, True, {"mean": -91.0, "max": -91.0})
    assert silent.startswith("Sound: the camera sends G.711 A-law, 8000 Hz, 1 channel, but it is SILENT in this test "
                             "(loudest -91 dB).") and "Check the microphone" in silent
    assert "SILENT" in sound_sentence(G711, True, {"mean": -80.0, "max": avtest.QUIET_DB})
    assert "SILENT" not in sound_sentence(G711, True, {"mean": -70.0, "max": avtest.QUIET_DB + 1})
    assert sound_sentence(G711, False, {}) == ("Sound: the camera sends G.711 A-law, 8000 Hz, 1 channel, but none "
                                               "arrived in this test.")
    assert sound_sentence(G711, True, {}).endswith("but none arrived in this test.")


def test_what_it_says_about_the_picture() -> None:
    facts = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                          "nb_read_packets": "300"}, {"codec_type": "audio", "codec_name": "aac"}],
             "format": {"duration": "10.000000"}}
    assert picture_sentence(facts) == "Picture: H.264, 1920x1080, 30 pictures a second; 10.0 s received (300 pictures)."
    # the picture's own rate and length are used when the clip gives them (the clip starts with the sound)
    own = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 480,
                        "avg_frame_rate": "30000/1001", "nb_read_packets": "93", "duration": "3.103"}],
           "format": {"duration": "4.053333"}}
    assert picture_sentence(own) == "Picture: H.264, 640x480, 30 pictures a second; 3.1 s received (93 pictures)."
    hevc = {"streams": [{"codec_type": "video", "codec_name": "hevc", "width": 2560, "height": 1440}], "format": {}}
    assert picture_sentence(hevc) == "Picture: H.265, 2560x1440, 0.0 s received (0 pictures)."
    assert picture_sentence({"streams": [{"codec_type": "audio"}]}) == "Picture: none arrived."
    assert picture_sentence({}) == "Picture: none arrived."


def test_the_commands_copy_the_video_and_convert_the_sound() -> None:
    clip = Path("c.mp4")
    argv = record_argv("ffmpeg", "rtsp://viewer:pw@192.0.2.5:554/stream0", clip, 10, "tcp")
    i = argv.index("-i")
    assert argv[i - 4:i] == ["-rtsp_transport", "tcp", "-timeout", "10000000"]
    assert argv[i + 2:i + 4] == ["-t", "10"]
    tail = argv[i + 4:]
    assert tail == ["-map", "0:v:0", "-map", "0:a:0?", "-dn", "-sn", "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+faststart", "c.mp4"]                 # sound is optional: a silent camera still tests
    assert "-y" in argv and "-nostdin" in argv
    hevc = record_argv("ffmpeg", "tcp://127.0.0.1:9", clip, 4.5, "tcp", "mpegts", hevc=True)
    assert hevc[hevc.index("-c:v"):hevc.index("-c:a")] == ["-c:v", "copy", "-tag:v", "hvc1"]
    j = hevc.index("-i")
    assert hevc[j - 4:j] == ["-rw_timeout", "10000000", "-f", "mpegts"] and hevc[j + 2:j + 4] == ["-t", "4.5"]
    vol = volume_argv("ffmpeg", clip)
    assert vol[vol.index("-af") + 1] == "volumedetect" and vol[-3:] == ["-f", "null", "-"] and "-vn" in vol


def test_the_clip_is_in_the_temporary_folder_and_can_be_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(avtest.tempfile, "gettempdir", lambda: str(tmp_path))
    clip = avtest.clip_path()
    assert clip == tmp_path / "Sentinel-camera-test" / "camera-test.mp4"
    assert avtest.delete_clip() is True                              # nothing there: nothing to do
    clip.parent.mkdir()
    clip.write_bytes(b"x")
    assert avtest.delete_clip() is True and not clip.exists() and not clip.parent.exists()


def test_without_an_answer_it_says_so_and_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(avtest.tempfile, "gettempdir", lambda: str(tmp_path))
    said: list[str] = []
    monkeypatch.setattr(avtest.sound, "_streams", lambda *a, **k: None)
    assert avtest.av_test("ffmpeg", "ffprobe", "rtsp://h/s", out=said.append) == (4, None)
    assert said[-1].startswith("The camera did not answer with a picture.")
    # the camera answers, but the test recording gets nothing (and FFmpeg's reason never shows the account)
    monkeypatch.setattr(avtest.sound, "_streams", lambda *a, **k: [{"codec_type": "video", "codec_name": "h264"}])

    class Done:
        stderr = b"rtsp://viewer:pw@192.0.2.5:554/stream0: Connection refused\n"
        stdout = b""

    ran: list[list[str]] = []
    monkeypatch.setattr(avtest, "_run", lambda argv, timeout: ran.append(argv) or Done())
    said.clear()
    assert avtest.av_test("ffmpeg", "ffprobe", "rtsp://viewer:pw@192.0.2.5:554/stream0", out=said.append) == (4, None)
    assert said[-1].startswith("No test recording arrived from the camera.") and "pw@" not in "\n".join(said)
    assert not avtest.clip_path().parent.exists() and len(ran) == 1


def test_the_command_needs_a_camera_and_is_in_the_program(tmp_path: Path) -> None:
    said: list[str] = []
    cfg = make_config(tmp_path)
    later = dataclasses.replace(cfg, camera=dataclasses.replace(cfg.camera, url="", pending=True))
    assert run_av_test(later, out=said.append) == 2 and "No camera has been added yet" in said[0]
    args = build_parser().parse_args(["av-test", "--seconds", "15"])
    assert args.cmd == "av-test" and args.seconds == 15.0


def test_the_length_is_kept_within_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(avtest.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(avtest.sound, "_streams", lambda *a, **k: [{"codec_type": "video", "codec_name": "h264"}])
    seen: list[Any] = []

    class Done:
        stderr = stdout = b""

    def run(argv: list[str], timeout: float) -> Any:
        seen.append((argv[argv.index("-t") + 1] if "-t" in argv else None, timeout))
        return Done()

    monkeypatch.setattr(avtest, "_run", run)
    avtest.av_test("ffmpeg", "ffprobe", "rtsp://h/s", seconds=5000, out=lambda _s: None)
    avtest.av_test("ffmpeg", "ffprobe", "rtsp://h/s", seconds=0.1, out=lambda _s: None)
    assert [s for s, _t in seen] == ["60", "2"]
