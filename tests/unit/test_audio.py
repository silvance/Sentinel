"""Recording the camera's sound ([camera] include_audio; requirement change 2026-11-18): the rules that must
hold without a camera. The recorder end to end is in tests/integration/test_audio_recording.py."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import base_config_dict, make_config
from gui_support import base_measurements
from sentinel.alerts import CATALOG, Severity
from sentinel.config import ConfigError, load_config_dict
from sentinel.export.media import audio_codec_of, remux_argv, review_audio, segment_audio
from sentinel.export.planner import _params, snapshot
from sentinel.launcher.setcamera import test_camera as ask_camera     # not a test: do not collect it by name
from sentinel.media.probe import ProbeResult
from sentinel.models import RunEndReason
from sentinel.recorder import audio
from sentinel.recorder.ffmpeg_cmd import build_record_argv
from sentinel.recorder.supervisor import RecordingSupervisor, RunOutcome
from sentinel.status import evaluate


# ---------------------------------------------------------------------- settings and the FFmpeg command
def test_sound_is_off_unless_switched_on_and_the_format_is_checked(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert cfg.camera.include_audio is False and cfg.camera.audio_format == "aac"
    assert audio.wanted_action(cfg) == "off"
    assert audio.wanted_action(make_config(tmp_path, camera={"include_audio": True})) == "aac"
    assert audio.wanted_action(make_config(tmp_path, camera={"include_audio": True, "audio_format": "copy"})) == "copy"
    assert audio.wanted_action(make_config(tmp_path, camera={"audio_format": "copy"})) == "off"   # not switched on
    with pytest.raises(ConfigError, match="audio_format"):
        load_config_dict(base_config_dict(tmp_path, camera={"include_audio": True, "audio_format": "mp3"}),
                         base_dir=tmp_path)


def test_the_video_is_copied_in_every_case(tmp_path: Path) -> None:
    def tail(cfg, action=None):  # noqa: ANN001, ANN202
        argv = build_record_argv(cfg, "rtsp://192.0.2.5/stream0", tmp_path / "run", action)
        return " ".join(argv[argv.index("-map"):argv.index("segment") - 1])        # up to "-f segment"

    off = make_config(tmp_path)
    assert tail(off) == "-map 0:v:0 -an -dn -sn -c copy"                                 # exactly as before
    on = make_config(tmp_path, camera={"include_audio": True})
    assert tail(on) == "-map 0:v:0 -map 0:a:0? -dn -sn -c:v copy -c:a aac -b:a 96k"
    keep = make_config(tmp_path, camera={"include_audio": True, "audio_format": "copy"})
    assert tail(keep) == "-map 0:v:0 -map 0:a:0? -dn -sn -c copy"
    assert tail(on, "off") == tail(off)                       # the guard's video-only attempt
    for cfg in (off, on, keep):
        assert "0:a:0?" in tail(cfg) or "-an" in tail(cfg)    # a camera without sound never fails the command


def test_words_for_people() -> None:
    assert audio.words({"codec": "pcm_alaw", "sample_rate": 8000, "channels": 1}) == "G.711 A-law, 8000 Hz, 1 channel"
    assert audio.words({"codec": "aac", "sample_rate": 48000, "channels": 2}) == "AAC, 48000 Hz, 2 channels"
    assert audio.words({"codec": "weird"}) == "weird" and audio.words(None) == "" and audio.words({"codec": None}) == ""
    assert audio.camera_sound_sentence({"codec": None}) == "It sends no sound."
    assert audio.camera_sound_sentence({"codec": "pcm_mulaw", "sample_rate": 8000, "channels": 1}) == \
        "It sends sound: G.711 µ-law, 8000 Hz, 1 channel."
    aac = {"codec": "aac", "sample_rate": 8000, "channels": 1}
    assert audio.status_text("aac", aac, False) == "Recorded (AAC, 8000 Hz, 1 channel; converted by the recorder)"
    assert audio.status_text("copy", aac, False).endswith("as sent by the camera)")
    assert audio.status_text("aac", None, False) == "Switched on; checking the recording…"
    assert audio.status_text("aac", {"codec": None}, False) == "NOT recorded: the camera sends no sound"
    assert "cannot be kept as it is" in audio.status_text("copy", {"codec": None, "other": ["bin_data"]}, False)
    assert audio.status_text("off", None, True).startswith("Switched off: recording with sound failed")


# ---------------------------------------------------------------------- sound never costs the video
def test_guard_gives_sound_up_only_when_video_works_without_it() -> None:
    g = audio.AudioGuard("aac")
    assert g.next_action() == "aac"
    # an ordinary outage: the camera does not answer, so sound is not suspected however long it lasts
    for _ in range(6):
        assert g.needs_camera_check("aac", 0.0, planned=False)
        assert g.after_run("aac", 0.0, planned=False, camera_answers=False) is None
        assert g.next_action() == "aac"
    assert g.after_run("aac", 120.0, planned=False) is None and g.suspects == 0
    # the camera answers, but an attempt with sound records nothing: twice, then one attempt without sound
    assert g.after_run("aac", 0.0, planned=False, camera_answers=True) is None and g.next_action() == "aac"
    assert g.after_run("aac", 0.0, planned=False, camera_answers=True) is None and g.next_action() == "off"
    assert g.trial and not g.suspended
    # ... which records: sound was the cause. Off until the recorder is restarted.
    assert g.after_run("off", 30.0, planned=False) == "suspended"
    assert g.suspended and g.next_action() == "off"
    assert g.after_run("off", 0.0, planned=False) is None and g.next_action() == "off"
    assert not g.needs_camera_check("off", 0.0, planned=False)


def test_guard_does_not_blame_sound_when_video_fails_without_it_too() -> None:
    g = audio.AudioGuard("copy")
    g.after_run("copy", 0.0, False, True)
    g.after_run("copy", 0.0, False, True)
    assert g.next_action() == "off"
    assert g.after_run("off", 0.0, planned=False) is None          # no video either: the camera
    assert not g.trial and not g.suspended and g.next_action() == "copy" and g.suspects == 0


def test_guard_ignores_planned_ends_and_one_good_run_clears_suspicion() -> None:
    g = audio.AudioGuard("aac")
    g.after_run("aac", 0.0, False, True)
    assert g.after_run("aac", 0.0, planned=True) is None and g.suspects == 1          # a stop, a pause: no verdict
    assert not g.needs_camera_check("aac", 0.0, planned=True)
    g.after_run("aac", 5.0, False)
    assert g.suspects == 0
    g.after_run("aac", 0.0, False, True)
    assert g.after_run("aac", 0.0, False, None) is None and g.suspects == 0           # could not ask: not suspected
    off = audio.AudioGuard("off")
    assert off.next_action() == "off" and off.after_run("off", 0.0, False, True) is None and not off.trial


class _Ledger:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def add_event(self, *a: object) -> None:
        self.events.append(a)


def _supervisor(tmp_path: Path, answers: list[bool | None]) -> RecordingSupervisor:
    sup = object.__new__(RecordingSupervisor)
    sup.cfg = make_config(tmp_path, camera={"include_audio": True})
    sup.audio_guard = audio.AudioGuard("aac")
    sup.ledger = _Ledger()
    sup.live = SimpleNamespace(lock=__import__("threading").Lock(), audio_suspended=False)
    sup._run_audio = "aac"
    sup._camera_answers = lambda: answers.pop(0)        # type: ignore[method-assign]
    return sup


def test_supervisor_asks_the_camera_only_after_a_failed_attempt_with_sound(tmp_path: Path) -> None:
    asked: list[bool | None] = [True, True]
    sup = _supervisor(tmp_path, asked)
    sup._after_run_audio(RunOutcome(RunEndReason.GRACEFUL_STOP, 0, 0.0))        # planned: nobody is asked
    sup._after_run_audio(RunOutcome(RunEndReason.FFMPEG_ERROR, 1, 50.0))        # recorded: nobody is asked
    assert asked == [True, True]
    sup._after_run_audio(RunOutcome(RunEndReason.CONNECT_TIMEOUT, None, 0.0))
    sup._after_run_audio(RunOutcome(RunEndReason.CONNECT_TIMEOUT, None, 0.0))
    assert asked == [] and sup.audio_guard.next_action() == "off" and not sup.live.audio_suspended
    sup._run_audio = "off"                                                      # the attempt without sound
    sup._after_run_audio(RunOutcome(RunEndReason.FFMPEG_ERROR, 1, 12.0))
    assert sup.live.audio_suspended and sup.audio_guard.suspended
    assert [e[2] for e in sup.ledger.events] == ["audio_suspended"]
    assert "continues without sound" in sup.ledger.events[0][3]


# ---------------------------------------------------------------------- status
def test_status_says_when_recordings_have_no_sound() -> None:
    def codes(**kw: object) -> list[str]:
        m = base_measurements()
        for k, v in kw.items():
            setattr(m, k, v)
        return [a.code for a in evaluate(m, 6.0, 2.0, 10.0).alerts]

    assert not [c for c in codes() if c.startswith("N-")]
    assert "N-001" in codes(audio_missing=True)
    assert "N-002" in codes(audio_suspended=True) and "N-001" not in codes(audio_suspended=True, audio_missing=True)
    # they say "video recording continues": not while no video is recorded (the outage alert speaks then)
    assert not [c for c in codes(audio_missing=True, audio_suspended=True, ffmpeg_running=False) if c.startswith("N-")]
    for code in ("N-001", "N-002"):
        assert "Video recording continues" in CATALOG[code].action and not CATALOG[code].stops_recording
    assert Severity.WARNING


# ---------------------------------------------------------------------- what was stored
def test_sound_is_noted_with_a_sealed_recording_only_when_there_is_some() -> None:
    plain = ProbeResult(playable=True)
    assert "audio" not in plain.detail()
    plain.audio = {"video": True, "codec": None}                       # sound on, the camera sends none
    assert "audio" not in plain.detail()
    with_sound = ProbeResult(playable=True, audio={"video": True, "codec": "aac", "sample_rate": 8000, "channels": 1})
    assert with_sound.detail()["audio"] == {"codec": "aac", "sample_rate": 8000, "channels": 1}
    unreadable = ProbeResult(playable=True, audio={"video": True, "codec": None, "other": ["bin_data"]})
    assert unreadable.detail()["audio"] == {"codec": None, "other": ["bin_data"]}


def test_streams_are_summarised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake(streams: list[dict], rc: int = 0):  # noqa: ANN202
        return lambda argv, **kw: SimpleNamespace(returncode=rc, stdout=json.dumps({"streams": streams}).encode())

    v = {"codec_type": "video", "codec_name": "h264"}
    monkeypatch.setattr(audio.subprocess, "run", fake([v, {"codec_type": "audio", "codec_name": "pcm_alaw",
                                                           "sample_rate": "8000", "channels": 1}]))
    assert audio.stored_audio(tmp_path / "x.ts") == {"video": True, "codec": "pcm_alaw", "sample_rate": 8000,
                                                     "channels": 1}
    assert audio.camera_streams("ffprobe", "rtsp://u:p@192.0.2.5/stream0")["codec"] == "pcm_alaw"
    monkeypatch.setattr(audio.subprocess, "run", fake([v, {"codec_type": "data", "codec_name": "bin_data"}]))
    assert audio.stored_audio(tmp_path / "x.ts") == {"video": True, "codec": None, "other": ["bin_data"]}
    monkeypatch.setattr(audio.subprocess, "run", fake([{"codec_type": "audio", "codec_name": "aac"}]))
    assert audio.camera_streams("ffprobe", "rtsp://192.0.2.5/stream0") is None           # no video: not an answer
    monkeypatch.setattr(audio.subprocess, "run", fake([], rc=1))
    assert audio.stored_audio(tmp_path / "x.ts") is None and audio.camera_streams("ffprobe", "tcp://127.0.0.1:9") is None
    argv = audio.camera_argv("ffprobe", "rtsp://192.0.2.5/stream0", "udp")
    assert argv[argv.index("-rtsp_transport") + 1] == "udp" and argv[-1] == "rtsp://192.0.2.5/stream0"
    assert "-f" in audio.camera_argv("ffprobe", "tcp://127.0.0.1:9", input_format="mpegts")


def test_connection_test_says_whether_the_camera_sends_sound() -> None:
    def run_with(out: str):  # noqa: ANN202
        return lambda argv, **kw: SimpleNamespace(returncode=0, stdout=out, stderr="")

    both = ("index=0\ncodec_name=h264\ncodec_type=video\nwidth=2592\nheight=1944\n"
            "index=1\ncodec_name=pcm_alaw\ncodec_type=audio\nsample_rate=8000\nchannels=1\n")
    ok, msg = ask_camera("ffprobe", "rtsp://192.0.2.5/stream0", None, run=run_with(both))
    assert ok and msg == ("The camera answered: h264 video, 2592x1944. It sends sound: G.711 A-law, 8000 Hz, 1 channel.")
    sound_first = ("index=0\ncodec_name=aac\ncodec_type=audio\nsample_rate=16000\nchannels=1\n"
                   "index=1\ncodec_name=hevc\ncodec_type=video\nwidth=1920\nheight=1080\n")
    ok, msg = ask_camera("ffprobe", "rtsp://192.0.2.5/stream0", None, run=run_with(sound_first))
    assert ok and "hevc video, 1920x1080" in msg and "It sends sound: AAC, 16000 Hz, 1 channel." in msg
    ok, msg = ask_camera("ffprobe", "rtsp://192.0.2.5/stream0", None,
                         run=run_with("index=0\ncodec_name=h264\ncodec_type=video\nwidth=640\nheight=480\n"))
    assert ok and msg.endswith("It sends no sound.")
    ok, msg = ask_camera("ffprobe", "rtsp://192.0.2.5/stream0", None,
                         run=run_with("index=0\ncodec_name=aac\ncodec_type=audio\nsample_rate=8000\nchannels=1\n"))
    assert not ok and "did not send video" in msg


# ---------------------------------------------------------------------- review drive
def test_review_videos_get_the_sound_the_recordings_have(tmp_path: Path) -> None:
    aac = {"validation_detail": json.dumps({"playable": True, "audio": {"codec": "aac", "sample_rate": 8000}})}
    none = {"validation_detail": json.dumps({"playable": True})}
    assert segment_audio(aac)["codec"] == "aac" and segment_audio(none) is None and segment_audio({}) is None
    assert audio_codec_of(aac) == "aac" and audio_codec_of(none) is None
    assert audio_codec_of({"audio_codec": "mp2", "validation_detail": aac["validation_detail"]}) == "mp2"   # the plan's
    assert snapshot({"segment_id": "s1", **aac})["audio_codec"] == "aac" and snapshot({"segment_id": "s2"})["audio_codec"] is None
    # a change of sound starts a new review video
    base = {"video_codec": "h264", "width": 640, "height": 480, "pix_fmt": "yuv420p"}
    assert _params({**base, **aac}) != _params({**base, **none}) and _params({**base, **none}) == _params(base)
    assert review_audio([]) is None and review_audio([None, "aac"]) is None
    assert review_audio(["aac", "aac"]) == "copy" and review_audio(["mp2", "mp2"]) == "aac" and review_audio(["aac", "mp2"]) == "aac"
    lst, out = tmp_path / "l.ffconcat", tmp_path / "o.mp4.partial"
    silent = " ".join(remux_argv("ffmpeg", lst, out))
    assert "-map 0:v:0 -c copy -an -sn -dn -movflags +faststart -f mp4 -n" in silent              # exactly as before
    kept = " ".join(remux_argv("ffmpeg", lst, out, "hevc", "copy"))
    assert "-map 0:v:0 -map 0:a:0? -c:v copy -tag:v hvc1 -c:a copy -sn -dn" in kept and " -an " not in kept
    conv = " ".join(remux_argv("ffmpeg", lst, out, None, "aac"))
    assert "-c:v copy -c:a aac -b:a 96k" in conv
