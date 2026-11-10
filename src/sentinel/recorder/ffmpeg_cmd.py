"""FFmpeg argv construction (pure functions, unit-tested).

The recorder never re-encodes: ``-c copy`` into the segment muxer with MPEG-TS segments.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from ..config import AppConfig
from ..credentials import redact

SEGMENT_PATTERN = "seg_%06d.ts"
SEGMENT_LIST_NAME = "segments.csv"


def build_record_argv(cfg: AppConfig, input_url: str, run_dir: Path) -> list[str]:
    cam, rec = cfg.camera, cfg.recording
    scheme = urlsplit(input_url).scheme
    io_us = str(int(rec.io_timeout_s * 1_000_000))
    argv: list[str] = [cfg.tools.ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning"]

    if scheme in ("rtsp", "rtsps"):
        argv += ["-rtsp_transport", cam.transport, "-timeout", io_us]
    elif scheme == "tcp":                       # development sources only (config enforces)
        argv += ["-rw_timeout", io_us]
    if cam.input_format:
        argv += ["-f", cam.input_format]
    argv += ["-i", input_url]

    # Stream selection: first video stream only; audio only by explicit collection-scope decision.
    argv += ["-map", "0:v:0"]
    if cam.include_audio:
        argv += ["-map", "0:a:0?"]
    else:
        argv += ["-an"]
    argv += ["-dn", "-sn", "-c", "copy"]

    argv += [
        "-f", "segment",
        "-segment_format", "mpegts",
        # Write each packet through to the OS immediately so a hard kill or power loss loses
        # at most the last packet, not FFmpeg's ~256 KB I/O buffer. (Measured: the global
        # -flush_packets option does not reach the segment muxer's inner muxer; this does.)
        "-segment_format_options", "flush_packets=1",
        "-segment_time", _fmt(rec.segment_seconds),
        "-reset_timestamps", "0",
        "-segment_list", str(run_dir / SEGMENT_LIST_NAME),
        "-segment_list_type", "csv",
    ]
    if rec.align_to_clock:
        argv += ["-segment_atclocktime", "1"]
    argv += ["-progress", "pipe:1", "-stats_period", "1"]
    argv.append(str(run_dir / SEGMENT_PATTERN))
    return argv


def redacted_command(argv: list[str]) -> str:
    return redact(" ".join(_quote(a) for a in argv))


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.3f}"


def _quote(a: str) -> str:
    return f'"{a}"' if (" " in a or not a) else a
