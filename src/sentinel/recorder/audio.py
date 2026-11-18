"""Recording the camera's sound (``[camera] include_audio``, off by default).

Requirement change 2026-11-18: sound must be recordable. Whether it MAY be recorded is a
collection-scope decision of the deployment (docs/00 R5, docs/02 A7); the default stays off.

What is stored. The video is copied unchanged, as always. The sound is, by default, converted to AAC
(``audio_format = "aac"``): most cameras send G.711, which the recording format (MPEG-TS) can only hold
as an unlabelled data stream that nothing reads back as sound (measured with the bundled FFmpeg
2026-11-18: ``pcm_alaw`` copied into ``.ts`` comes back as ``bin_data``). ``audio_format = "copy"`` keeps
the camera's own sound stream, for cameras that send AAC; the check below says when that did not work.

Sound never costs the video (:class:`AudioGuard`). A camera that announces a sound stream but sends none
keeps FFmpeg waiting for it, and nothing is written. Two recording attempts in a row that got nothing
while the camera itself answered are followed by one without sound; if that one records, sound stays
off until the recorder is restarted and the status says so (N-002).

What was really stored (:func:`stored_audio`). A few seconds into every run the first file is read back:
the status shows the sound format in the file, or N-001 when sound is switched on and there is none.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

ACTIONS = ("off", "aac", "copy")
FORMATS = ("aac", "copy")
AAC_BITRATE = "96k"                # FFmpeg lowers it by itself where the sample rate allows less (8 kHz: 48 kbit/s)
CHECK_AFTER_S = 10.0               # read the first file back this long after the run's media started
SUSPECT_RUNS = 2                   # attempts with sound that got nothing while the camera answered
CODEC_WORDS = {"aac": "AAC", "pcm_alaw": "G.711 A-law", "pcm_mulaw": "G.711 µ-law", "adpcm_g726": "G.726",
               "adpcm_g726le": "G.726", "adpcm_g722": "G.722", "mp2": "MP2", "mp3": "MP3", "opus": "Opus",
               "ac3": "AC-3"}


def wanted_action(cfg: Any) -> str:
    """"off", "aac" or "copy" from the settings."""
    cam = cfg.camera
    return cam.audio_format if cam.include_audio else "off"


def record_args(action: str) -> list[str]:
    """FFmpeg output options that choose the streams and how each is stored (the video is always copied)."""
    if action == "copy":
        return ["-map", "0:a:0?", "-dn", "-sn", "-c", "copy"]
    if action == "aac":
        return ["-map", "0:a:0?", "-dn", "-sn", "-c:v", "copy", "-c:a", "aac", "-b:a", AAC_BITRATE]
    return ["-an", "-dn", "-sn", "-c", "copy"]


def words(info: dict[str, Any] | None) -> str:
    """"AAC, 8000 Hz, 1 channel" for a sound stream description; "" for none."""
    if not info or not info.get("codec"):
        return ""
    parts = [CODEC_WORDS.get(str(info["codec"]), str(info["codec"]))]
    if info.get("sample_rate"):
        parts.append(f"{int(info['sample_rate'])} Hz")
    if info.get("channels"):
        n = int(info["channels"])
        parts.append("1 channel" if n == 1 else f"{n} channels")
    return ", ".join(parts)


def status_text(action: str, stored: dict[str, Any] | None, suspended: bool) -> str:
    """One line for the status page, when sound is switched on in the settings."""
    if suspended:
        return "Switched off: recording with sound failed. Video continues"
    if action == "off":
        return "Not in the current recording (checking whether sound stops the video)"
    if stored is None:
        return "Switched on; checking the recording…"
    if stored.get("codec"):
        how = "converted by the recorder" if action == "aac" else "as sent by the camera"
        return f"Recorded ({words(stored)}; {how})"
    if stored.get("other"):
        return "NOT recorded: the camera's sound format cannot be kept as it is"
    return "NOT recorded: the camera sends no sound"


class AudioGuard:
    """Decides whether the next recording attempt includes sound. Pure: fed with how each attempt ended."""

    def __init__(self, wanted: str) -> None:
        self.wanted = wanted if wanted in ACTIONS else "off"
        self.suspects = 0             # attempts with sound, no media, camera answering: in a row
        self.trial = False            # the next attempt is made without sound, to find the cause
        self.suspended = False        # sound is off until the recorder is restarted

    def next_action(self) -> str:
        return "off" if (self.suspended or self.trial) else self.wanted

    def needs_camera_check(self, used: str, media_seconds: float, planned: bool) -> bool:
        """After an attempt with sound that recorded nothing: ask the camera whether it is there at all."""
        return used != "off" and media_seconds <= 0 and not planned and not self.suspended

    def after_run(self, used: str, media_seconds: float, planned: bool, camera_answers: bool | None = None) -> str | None:
        """Tell the guard how an attempt ended. ``planned``: ended on purpose (stop, schedule, no motion) or
        for a reason that has nothing to do with the camera. Returns "suspended" the moment sound is given up."""
        if self.wanted == "off" or self.suspended:
            return None
        if self.trial:
            if media_seconds > 0:
                self.trial, self.suspended = False, True
                return "suspended"
            if not planned:                   # no video without sound either: the camera, not the sound
                self.trial, self.suspects = False, 0
            return None
        if used == "off":
            return None
        if media_seconds > 0:
            self.suspects = 0
        elif not planned:
            self.suspects = self.suspects + 1 if camera_answers else 0
            if self.suspects >= SUSPECT_RUNS:
                self.trial = True
        return None


# ---------------------------------------------------------------------- reading streams (FFprobe)
def _streams(argv: list[str], timeout: float, popen_kwargs: dict[str, Any] | None = None) -> list[dict[str, Any]] | None:
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": timeout, "check": False,
                          **(popen_kwargs or {})}
    if sys.platform == "win32":
        kw["creationflags"] = kw.get("creationflags", 0) | 0x08000000        # CREATE_NO_WINDOW
    try:
        cp = subprocess.run(argv, **kw)
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    streams = data.get("streams")
    return list(streams) if isinstance(streams, list) and (cp.returncode == 0 or streams) else None


def _summary(streams: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"video": any(s.get("codec_type") == "video" for s in streams), "codec": None}
    for s in streams:
        if s.get("codec_type") == "audio":
            out["codec"] = s.get("codec_name")
            for key in ("sample_rate", "channels"):
                try:
                    out[key] = int(s[key])
                except (KeyError, TypeError, ValueError):
                    pass
            break
    other = sorted({str(s.get("codec_name") or s.get("codec_type")) for s in streams
                    if s.get("codec_type") not in ("video", "audio")})
    if other:
        out["other"] = other           # e.g. "bin_data": sound stored in a form nothing reads back
    return out


SHOW = ["-show_entries", "stream=codec_type,codec_name,sample_rate,channels", "-of", "json"]


def stored_audio(path: Any, ffprobe: str = "ffprobe", timeout: float = 30.0) -> dict[str, Any] | None:
    """Sound in a recorded file, which may still be being written (read-only): ``{"video", "codec",
    "sample_rate", "channels", "other"}``; ``codec`` is None when the file has no sound stream. None when the
    file cannot be read."""
    streams = _streams([ffprobe, "-v", "error", *SHOW, str(path)], timeout)
    return None if streams is None else _summary(streams)


def camera_argv(ffprobe: str, url: str, transport: str = "tcp", input_format: str | None = None,
                io_timeout_s: float = 8.0) -> list[str]:
    from urllib.parse import urlsplit

    scheme = urlsplit(url).scheme
    io_us = str(int(io_timeout_s * 1_000_000))
    argv = [ffprobe, "-v", "error"]
    if scheme in ("rtsp", "rtsps"):
        argv += ["-rtsp_transport", transport, "-timeout", io_us]
    elif scheme == "tcp":
        argv += ["-rw_timeout", io_us]
    if input_format:
        argv += ["-f", input_format]
    return [*argv, *SHOW, url]


def camera_streams(ffprobe: str, url: str, transport: str = "tcp", input_format: str | None = None,
                   timeout: float = 12.0, popen_kwargs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """What the camera sends right now (one short connection): as :func:`stored_audio`; None when it does not
    answer. ``url`` may carry the account: it is never logged here."""
    streams = _streams(camera_argv(ffprobe, url, transport, input_format, min(8.0, timeout)), timeout, popen_kwargs)
    if streams is None:
        return None
    info = _summary(streams)
    return info if info["video"] else None


def camera_sound_sentence(info: dict[str, Any] | None) -> str:
    """For the connection test on the Camera page."""
    if info is None:
        return ""
    if info.get("codec"):
        return f"It sends sound: {words(info)}."
    return "It sends no sound."


__all__ = ["ACTIONS", "FORMATS", "AudioGuard", "camera_sound_sentence", "camera_streams", "record_args",
           "stored_audio", "wanted_action", "words"]
