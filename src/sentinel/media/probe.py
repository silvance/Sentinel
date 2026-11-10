"""FFprobe-based validation of recorded segments.

Validation levels:
* ``packets`` (default): demux every packet, check video stream presence, packet count,
  keyframes, DTS monotonicity and PTS gaps. Cheap enough for 5MP continuous recording.
* ``decode``: additionally decode all video with FFmpeg and treat decoder errors as warnings.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    pass


@dataclass
class ProbeResult:
    playable: bool
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    duration_s: float | None = None
    first_pts_s: float | None = None
    last_pts_s: float | None = None
    packet_count: int = 0
    keyframe_count: int = 0
    max_pts_gap_s: float = 0.0
    pts_gaps: list[tuple[float, float]] = field(default_factory=list)  # (before, after) relative to first pts
    non_monotonic_dts: int = 0
    starts_with_keyframe: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str | None = None

    def detail(self) -> dict[str, Any]:
        return {
            "playable": self.playable, "codec": self.codec, "width": self.width, "height": self.height,
            "duration_s": self.duration_s, "first_pts_s": self.first_pts_s, "last_pts_s": self.last_pts_s,
            "packet_count": self.packet_count, "keyframe_count": self.keyframe_count,
            "max_pts_gap_s": self.max_pts_gap_s, "pts_gaps": self.pts_gaps[:50],
            "non_monotonic_dts": self.non_monotonic_dts, "starts_with_keyframe": self.starts_with_keyframe,
            "errors": self.errors[:20], "warnings": self.warnings, "reason": self.reason,
        }


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise ProbeError(f"tool not found: {argv[0]}") from None
    except subprocess.TimeoutExpired:
        raise ProbeError(f"{Path(argv[0]).name} timed out after {timeout}s") from None


def first_video_pts(path: Path, ffprobe: str = "ffprobe", timeout: float = 30.0) -> float | None:
    """PTS (seconds) of the first video packet in ``path``, reading only that packet; None if it
    cannot be read. Safe on a file that is still being written (read-only, first packet only).

    Measured with FFmpeg 9.0.1: in segments written with ``-reset_timestamps 0`` the first packet
    PTS of each segment equals its listed start plus one constant MPEG-TS offset (1.4 s in that
    test), so a later segment's first PTS calibrates that offset for segment 0."""
    try:
        cp = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                   "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)], timeout)
    except ProbeError:
        return None
    for line in cp.stdout.decode("utf-8", "replace").splitlines():
        first = line.strip().split(",")[0]
        if first and first != "N/A":
            try:
                return float(first)
            except ValueError:
                return None
    return None


def probe_segment(path: Path, ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg", level: str = "packets",
                  max_internal_gap_s: float = 2.0, timeout: float = 300.0) -> ProbeResult:
    if not path.exists() or path.stat().st_size == 0:
        return ProbeResult(playable=False, reason="empty_or_missing_file")
    argv = [ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,codec_type,width,height,pix_fmt:format=duration,format_name"
                             ":packet=pts_time,dts_time,flags",
            "-of", "json", str(path)]
    cp = _run(argv, timeout)
    errors = [ln for ln in cp.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    try:
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        data = {}
    res = ProbeResult(playable=False, errors=errors)
    streams = [s for s in data.get("streams", []) if s.get("codec_type", "video") == "video"]
    if not streams:
        res.reason = "no_video_stream"
        return res
    st = streams[0]
    res.codec, res.width, res.height = st.get("codec_name"), st.get("width"), st.get("height")
    res.pix_fmt = st.get("pix_fmt")

    packets = data.get("packets", [])
    pts: list[float] = []
    last_dts: float | None = None
    for p in packets:
        res.packet_count += 1
        if "K" in p.get("flags", ""):
            res.keyframe_count += 1
        dts_t = p.get("dts_time")
        if dts_t not in (None, "N/A"):
            d = float(dts_t)
            if last_dts is not None and d <= last_dts:
                res.non_monotonic_dts += 1
            last_dts = d
        pts_t = p.get("pts_time")
        if pts_t not in (None, "N/A"):
            pts.append(float(pts_t))
    if packets:
        res.starts_with_keyframe = "K" in packets[0].get("flags", "")
    if res.packet_count == 0 or not pts:
        res.reason = "no_video_packets"
        return res

    ordered = sorted(pts)
    res.first_pts_s, res.last_pts_s = ordered[0], ordered[-1]
    deltas = [b - a for a, b in zip(ordered, ordered[1:])]
    frame_dur = sorted(deltas)[len(deltas) // 2] if deltas else 0.0
    for a, b in zip(ordered, ordered[1:]):
        gap = b - a
        res.max_pts_gap_s = max(res.max_pts_gap_s, gap)
        if gap > max_internal_gap_s:
            res.pts_gaps.append((round(a - ordered[0], 6), round(b - ordered[0], 6)))
    fmt_dur = data.get("format", {}).get("duration")
    span = (ordered[-1] - ordered[0]) + frame_dur
    res.duration_s = float(fmt_dur) if fmt_dur not in (None, "N/A") else span

    if res.pts_gaps:
        res.warnings.append("internal_timestamp_gap")
    if res.non_monotonic_dts:
        res.warnings.append("non_monotonic_dts")
    if not res.starts_with_keyframe:
        res.warnings.append("does_not_start_with_keyframe")
    if res.keyframe_count == 0:
        res.warnings.append("no_keyframes")
    if errors:
        res.warnings.append("demuxer_errors")

    if level == "decode":
        cp2 = _run([ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"], timeout)
        derr = [ln for ln in cp2.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        if derr:
            res.errors.extend(derr[:20])
            res.warnings.append("decoder_errors")
        if cp2.returncode != 0:
            res.reason = "decode_failed"
            return res

    # Playable: a video stream with packets and at least one keyframe to start decoding from.
    res.playable = res.keyframe_count > 0 and (res.duration_s or 0) > 0
    if not res.playable:
        res.reason = "no_decodable_video"
    return res


def ffmpeg_version(ffmpeg: str = "ffmpeg") -> str | None:
    try:
        cp = _run([ffmpeg, "-hide_banner", "-version"], 15)
    except ProbeError:
        return None
    first = cp.stdout.decode("utf-8", "replace").splitlines()
    return first[0].strip() if first else None
