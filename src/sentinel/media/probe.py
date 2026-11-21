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


# Pictures missing inside a file, judged by the file's own rhythm (0.3.29). A step between two pictures of at
# least HOLE_STEPS picture intervals means three or more pictures in a row are not there. Steps longer than
# ``max_internal_gap_s`` are not counted here: those are gaps (or a leap of the camera's time labels) and are
# recorded as such. The site camera's own recordings step by exactly one interval throughout, so this says
# nothing about a healthy stream.
HOLE_STEPS = 4
RHYTHM_MIN_PACKETS = 8          # fewer pictures than this have no rhythm to judge by


def picture_holes(ordered_pts: list[float], frame_interval_s: float,
                  max_internal_gap_s: float) -> tuple[int, list[tuple[float, float]]]:
    """(pictures missing, [(seconds into the file, length of the hole in seconds), ...])."""
    if frame_interval_s <= 0 or len(ordered_pts) < RHYTHM_MIN_PACKETS:
        return 0, []
    missing, holes = 0, []
    for a, b in zip(ordered_pts, ordered_pts[1:]):
        step = b - a
        if step > max_internal_gap_s:
            continue
        n = round(step / frame_interval_s) - 1
        if n >= HOLE_STEPS - 1:
            missing += n
            holes.append((round(a - ordered_pts[0], 6), round(step, 6)))
    return missing, holes


def hole_before(first_pts_s: float | None, previous_last_pts_s: float | None, frame_interval_s: float | None,
                max_internal_gap_s: float) -> tuple[int, float]:
    """Pictures missing between the last picture of the file before (same run, same timeline) and the first of
    this one: (pictures, seconds). (0, 0.0) when there is no hole, or when it is long enough to be a gap."""
    if first_pts_s is None or previous_last_pts_s is None or not frame_interval_s or frame_interval_s <= 0:
        return 0, 0.0
    step = first_pts_s - previous_last_pts_s
    if step <= 0 or step > max_internal_gap_s:
        return 0, 0.0
    n = round(step / frame_interval_s) - 1
    return (n, round(step, 6)) if n >= HOLE_STEPS - 1 else (0, 0.0)


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
    audio: dict[str, Any] | None = None     # the sound stream, looked for only while sound recording is on
    frame_interval_s: float | None = None   # the usual step between two pictures (the median)
    pictures_missing: int = 0               # pictures that are not there, by that rhythm (see picture_holes)
    picture_holes: list[tuple[float, float]] = field(default_factory=list)   # (seconds into the file, length)
    missing_before_start: int = 0           # ... between the file before and this one (set by the finaliser)
    hole_before_start_s: float = 0.0

    def detail(self) -> dict[str, Any]:
        d = self._detail()
        if self.audio and (self.audio.get("codec") or self.audio.get("other")):
            # Only when there is something to say: records made without sound keep their exact form.
            d["audio"] = {k: v for k, v in self.audio.items() if k != "video"}
        if self.pictures_missing or self.missing_before_start:      # likewise: only when pictures are missing
            d["pictures_missing"] = self.pictures_missing + self.missing_before_start
            d["frame_interval_s"] = self.frame_interval_s
            if self.picture_holes:
                d["picture_holes"] = self.picture_holes[:50]
            if self.missing_before_start:
                d["hole_before_start_s"] = self.hole_before_start_s
        return d

    def _detail(self) -> dict[str, Any]:
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
                  max_internal_gap_s: float = 2.0, timeout: float = 300.0, audio: bool = False) -> ProbeResult:
    """``audio``: also note the file's sound stream (sound recording is switched on). It never decides whether
    the file is playable: that is about the video."""
    res = _probe_video(path, ffprobe, ffmpeg, level, max_internal_gap_s, timeout)
    if audio and res.reason != "empty_or_missing_file":
        from ..recorder.audio import stored_audio

        try:
            res.audio = stored_audio(path, ffprobe, min(timeout, 60.0))
        except Exception:  # noqa: BLE001 - a note about the sound must not fail the video check
            res.audio = None
    return res


def _probe_video(path: Path, ffprobe: str, ffmpeg: str, level: str, max_internal_gap_s: float,
                 timeout: float) -> ProbeResult:
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
    res.frame_interval_s = frame_dur or None
    res.pictures_missing, res.picture_holes = picture_holes(ordered, frame_dur, max_internal_gap_s)
    fmt_dur = data.get("format", {}).get("duration")
    span = (ordered[-1] - ordered[0]) + frame_dur
    res.duration_s = float(fmt_dur) if fmt_dur not in (None, "N/A") else span

    if res.pts_gaps:
        res.warnings.append("internal_timestamp_gap")
    if res.pictures_missing:
        res.warnings.append("pictures_missing")
    if res.non_monotonic_dts:
        res.warnings.append("non_monotonic_dts")
    if not res.starts_with_keyframe:
        res.warnings.append("does_not_start_with_keyframe")
    if res.keyframe_count == 0:
        res.warnings.append("no_keyframes")
    if errors:
        res.warnings.append("demuxer_errors")

    if level == "decode":
        # A full decode of a 1-hour segment can take longer than the packet scan's limit (review
        # 2026-11-12): allow up to the footage's own length.
        decode_timeout = max(timeout, res.duration_s or 0.0)
        cp2 = _run([ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
                   decode_timeout)
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
