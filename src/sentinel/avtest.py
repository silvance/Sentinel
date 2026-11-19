"""Quick test of the camera's picture and sound (owner request 2026-11-18, 0.3.22).

The live view shows a still picture every few seconds, which says nothing about sound. This records a few
seconds straight from the camera into a small clip, says what arrived (picture format; for sound the format
the camera sends and how loud it was, or that it was silent or missing), and leaves the clip to be played so
that the sound can be heard. Settings > Camera > "Test picture and sound", or ``Sentinel.exe av-test``.

* It is a connection of its own to the camera, like "Test connection": the recorder is not touched and the
  clip never enters the recordings, the recording log or an archive drive. **The clip is not evidence.**
* It works whether or not sound recording is switched on, so it is the first thing to try once a microphone
  is fitted. Switching sound recording on stays a separate, deliberate step (docs/00 R5).
* The clip holds a few seconds of the scene, with sound. It is kept in the user's temporary folder, replaced
  by the next test and deleted when the Camera page is left or the next test starts
  (:func:`delete_clip`). A player that still has it open keeps it until it is closed.
* The video is copied as the camera sends it; the sound is converted to AAC, as the recorder does, so that
  the clip plays in any player.

How loud: FFmpeg's ``volumedetect`` gives the loudest and the average level in dB, where 0 dB is the loudest
a recording can be. A stream that is there but carries nothing (a camera with its microphone input switched
on and no microphone) reads about -91 dB; :data:`QUIET_DB` and below is reported as silent.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .recorder import audio as sound

DEFAULT_SECONDS = 10.0
MAX_SECONDS = 60.0
QUIET_DB = -60.0
TEST_FOLDER = "Sentinel-camera-test"
CLIP_NAME = "camera-test.mp4"
VIDEO_WORDS = {"h264": "H.264", "hevc": "H.265", "mjpeg": "Motion JPEG", "mpeg4": "MPEG-4"}


def clip_path() -> Path:
    return Path(tempfile.gettempdir()) / TEST_FOLDER / CLIP_NAME


def delete_clip() -> bool:
    """Remove the test clip and its folder. False if a player still holds it (tried again next time)."""
    clip = clip_path()
    try:
        clip.unlink(missing_ok=True)
        if clip.parent.is_dir():
            clip.parent.rmdir()
        return True
    except OSError:
        return False


def _input_args(url: str, transport: str, input_format: str | None, io_timeout_s: float = 10.0) -> list[str]:
    scheme = urlsplit(url).scheme
    io_us = str(int(io_timeout_s * 1_000_000))
    argv: list[str] = []
    if scheme in ("rtsp", "rtsps"):
        argv += ["-rtsp_transport", transport, "-timeout", io_us]
    elif scheme == "tcp":
        argv += ["-rw_timeout", io_us]
    if input_format:
        argv += ["-f", input_format]
    return [*argv, "-i", url]


def record_argv(ffmpeg: str, url: str, clip: Path, seconds: float, transport: str = "tcp",
                input_format: str | None = None, hevc: bool = False) -> list[str]:
    """Video copied, first sound stream (if any) converted to AAC, into an MP4 that any player opens."""
    return [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-nostdin", "-y",
            *_input_args(url, transport, input_format), "-t", f"{seconds:g}",
            "-map", "0:v:0", "-map", "0:a:0?", "-dn", "-sn", "-c:v", "copy", *(["-tag:v", "hvc1"] if hevc else []),
            "-c:a", "aac", "-b:a", sound.AAC_BITRATE, "-movflags", "+faststart", str(clip)]


def volume_argv(ffmpeg: str, clip: Path) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-i", str(clip), "-map", "0:a:0", "-vn",
            "-af", "volumedetect", "-f", "null", "-"]


_VOLUME = re.compile(r"(mean|max)_volume:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dB")


def parse_volume(text: str) -> dict[str, float]:
    """{"mean": dB, "max": dB} from volumedetect's lines ("-inf" = nothing at all, given as -120)."""
    out: dict[str, float] = {}
    for kind, value in _VOLUME.findall(text):
        out[kind] = -120.0 if value == "-inf" else float(value)
    return out


def clip_argv(ffprobe: str, clip: Path) -> list[str]:
    return [ffprobe, "-v", "error", "-count_packets", "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,nb_read_packets,sample_rate,channels,duration:"
            "format=duration", "-of", "json", str(clip)]


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def picture_sentence(facts: dict[str, Any]) -> str:
    v = next((s for s in facts.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        return "Picture: none arrived."
    # The picture's own length: the clip starts with the sound, the picture at its first key frame.
    seconds = _number(v.get("duration")) or _number(facts.get("format", {}).get("duration"))
    pictures = int(_number(v.get("nb_read_packets")))
    top, _, bottom = str(v.get("avg_frame_rate") or "").partition("/")
    per_second = _number(top) / _number(bottom) if _number(bottom) else 0.0
    if not per_second and pictures and seconds > 0.5:
        per_second = pictures / seconds
    rate = f"{per_second:.0f} pictures a second; " if per_second >= 0.5 else ""
    name = VIDEO_WORDS.get(str(v.get("codec_name")), str(v.get("codec_name") or "?"))
    return (f"Picture: {name}, {v.get('width', '?')}x{v.get('height', '?')}, {rate}"
            f"{seconds:.1f} s received ({pictures} pictures).")


def sound_sentence(camera: dict[str, Any] | None, clip_has_sound: bool, volume: dict[str, float]) -> str:
    if not camera or not camera.get("codec"):
        return "Sound: the camera sends no sound."
    sends = f"the camera sends {sound.words(camera)}"
    if not clip_has_sound or "max" not in volume:
        return f"Sound: {sends}, but none arrived in this test."
    loudest, average = volume["max"], volume.get("mean", volume["max"])
    if loudest <= QUIET_DB:
        return (f"Sound: {sends}, but it is SILENT in this test (loudest {loudest:.0f} dB). Check the microphone "
                "and the camera's own sound settings.")
    return (f"Sound: {sends}. In this test: loudest {loudest:.0f} dB, average {average:.0f} dB "
            "(0 dB is the loudest possible). Play the clip to hear it.")


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": timeout, "check": False}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
    return subprocess.run(argv, **kw)


def av_test(ffmpeg: str, ffprobe: str, url: str, transport: str = "tcp", input_format: str | None = None,
            seconds: float = DEFAULT_SECONDS, out: Callable[[str], None] = print,
            records_sound: bool | None = None) -> tuple[int, Path | None]:
    """Record ``seconds`` from ``url`` (which may carry the account; it is never shown) and say what arrived.
    Returns (exit code, clip): 0 = a picture arrived, 4 = the camera did not answer or sent no picture."""
    from .credentials import redact

    seconds = min(max(float(seconds), 2.0), MAX_SECONDS)
    delete_clip()
    clip = clip_path()
    out("Asking the camera what it sends...")
    rows = sound._streams(sound.camera_argv(ffprobe, url, transport, input_format), 20.0)
    if not rows or not any(r.get("codec_type") == "video" for r in rows):
        out("The camera did not answer with a picture. Try Test connection for the reason.")
        return 4, None
    camera = sound._summary(rows)
    hevc = any(r.get("codec_type") == "video" and r.get("codec_name") == "hevc" for r in rows)
    out(f"Recording {seconds:g} seconds for the test (not saved with the recordings; make some noise now)...")
    try:
        clip.parent.mkdir(parents=True, exist_ok=True)
        cp = _run(record_argv(ffmpeg, url, clip, seconds, transport, input_format, hevc), seconds + 40.0)
    except (OSError, subprocess.SubprocessError) as exc:
        out(f"The test recording could not be made ({type(exc).__name__}).")
        return 4, None
    if not clip.is_file() or clip.stat().st_size == 0:
        tail = redact(cp.stderr.decode("utf-8", "replace").strip().splitlines()[-1]) if cp.stderr.strip() else ""
        out("No test recording arrived from the camera." + (f" ({tail})" if tail else ""))
        delete_clip()
        return 4, None
    try:
        facts = json.loads(_run(clip_argv(ffprobe, clip), 30.0).stdout.decode("utf-8", "replace") or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        facts = {}
    has_sound = any(s.get("codec_type") == "audio" for s in facts.get("streams", []))
    volume: dict[str, float] = {}
    if has_sound:
        try:
            volume = parse_volume(_run(volume_argv(ffmpeg, clip), 60.0).stderr.decode("utf-8", "replace"))
        except (OSError, subprocess.SubprocessError):
            volume = {}
    out(picture_sentence(facts))
    out(sound_sentence(camera, has_sound, volume))
    if records_sound is not None and camera.get("codec"):
        out("Sentinel is set to record sound with the video." if records_sound else
            "Sentinel is NOT set to record sound (Settings > Camera > Record the camera's sound with the video).")
    out(f"Test clip: {clip}")
    out("It is not evidence. It is replaced by the next test and deleted when the Camera page is left.")
    has_picture = any(s.get("codec_type") == "video" for s in facts.get("streams", []))
    return (0 if has_picture else 4), clip


def run_av_test(cfg: Any, seconds: float = DEFAULT_SECONDS, out: Callable[[str], None] = print) -> int:
    """``Sentinel.exe av-test``: the test with the saved camera settings."""
    from .credentials import CredentialError, load_credentials, url_with_credentials

    if cfg.camera.pending or not cfg.camera.url:
        out("No camera has been added yet (add it on the Camera page first).")
        return 2
    creds = None
    if cfg.camera.credentials_file:
        try:
            creds = load_credentials(cfg.camera.credentials_file)
        except CredentialError as exc:
            out(f"The camera credentials cannot be read: {exc}")
            return 2
    code, clip = av_test(cfg.tools.ffmpeg, cfg.tools.ffprobe, url_with_credentials(cfg.camera.url, creds),
                         cfg.camera.transport, cfg.camera.input_format, seconds, out, cfg.camera.include_audio)
    if clip is not None:
        out("Open that file to watch and listen (double-click it in the file manager).")
    return code
