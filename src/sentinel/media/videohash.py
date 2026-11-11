"""Video content hashing.

Sentinel keeps three kinds of hash and they answer different questions:

1. **File SHA-256** (``sentinel.hashing.sha256_file``) — "are these exactly the bytes that
   were recorded?"  Computed when a segment is sealed. Any container change alters it, so an
   MP4 derivative never matches its MPEG-TS original, and nothing in Sentinel claims it should.
2. **Per-frame SHA-256 list** — SHA-256 of every decoded video frame's raw pixels, in decoder
   output order (FFmpeg ``framehash`` muxer, ``-fps_mode passthrough`` so each decoded frame is
   hashed exactly once). Localises *which* frames differ.
3. **Content SHA-256** — SHA-256 over the newline-joined per-frame digests (hash column only;
   timestamps and container time bases are excluded). One value that answers "does this file
   show exactly the same pictures?"  Container-independent: an MPEG-TS original and its
   ``-c copy`` MP4 remux give the same value. This is the provenance link for derivatives.

Measured in this repository (FFmpeg 9.0.1; docs/PHASE2_REPORT.md):
* the compressed-packet hash (``-c copy -f hash``) is NOT container-independent — TS and MP4
  differ even after ``h264_mp4toannexb`` — so it is deliberately not used for provenance;
* per-frame decoded hashes of a TS segment and its MP4 remux are identical;
* cost is dominated by hashing raw pixels (~7.5 MB per 5MP frame). An independent review
  measured a two-hash pipeline at ~1.05x real time for 2592x1944@30 on a 20-thread host, which
  is why content hashing runs in a deferred, low-priority worker and never delays sealing.

Caveat stored with every content hash: H.264 and H.265 decoding are bit-exact by standard for conforming
streams, but error concealment of *damaged* streams may differ between decoder versions. The
method string records the FFmpeg version and pixel format so a verifier can reproduce it.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .probe import ProbeError

METHOD_PREFIX = "sha256(per-frame-sha256-list);decoded=rawvideo;fps_mode=passthrough"


@dataclass(frozen=True)
class ContentHash:
    sha256: str          # content hash (over per-frame digests)
    method: str          # how it was computed (needed to reproduce it)
    frame_count: int     # frames actually decoded and hashed


@dataclass(frozen=True)
class FrameHash:
    pts: int
    size_bytes: int
    sha256: str


def aggregate(frames: list[FrameHash]) -> str:
    return hashlib.sha256("\n".join(f.sha256 for f in frames).encode("ascii")).hexdigest()


def compute_content_hash(path: Path, ffmpeg: str = "ffmpeg", ffmpeg_version: str | None = None,
                         pix_fmt: str | None = None, frames_out: Path | None = None,
                         timeout: float = 3600.0, popen_kwargs: dict[str, Any] | None = None) -> ContentHash:
    """Decode ``path`` once; return the content hash. Optionally write the per-frame list."""
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
            "-map", "0:v:0", "-an", "-sn", "-dn", "-fps_mode", "passthrough",
            "-f", "framehash", "-hash", "sha256", "-"]
    try:
        cp = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                            check=False, **(popen_kwargs or {}))
    except FileNotFoundError:
        raise ProbeError(f"tool not found: {ffmpeg}") from None
    except subprocess.TimeoutExpired:
        raise ProbeError(f"content hashing timed out after {timeout}s") from None
    if cp.returncode != 0:
        lines = cp.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ProbeError(f"FFmpeg content hashing failed: {lines[-1] if lines else cp.returncode}")
    text = cp.stdout.decode("ascii", "replace")
    frames = parse_framehash(text)
    if not frames:
        raise ProbeError("no decodable frames to hash")
    if frames_out is not None:
        frames_out.parent.mkdir(parents=True, exist_ok=True)
        frames_out.write_text(text, encoding="ascii", newline="\n")
    method = f"{METHOD_PREFIX};pix_fmt={pix_fmt or 'decoder-native'};ffmpeg={short_version(ffmpeg_version)}"
    return ContentHash(sha256=aggregate(frames), method=method, frame_count=len(frames))


def parse_framehash(text: str) -> list[FrameHash]:
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        out.append(FrameHash(pts=int(parts[2]), size_bytes=int(parts[4]),
                             sha256=parts[5].removeprefix("SHA256=")))
    return out


def compare_frame_lists(a: list[FrameHash], b: list[FrameHash]) -> list[int]:
    """Indices where two per-frame lists differ (including any length-mismatch tail)."""
    diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x.sha256 != y.sha256]
    diffs.extend(range(min(len(a), len(b)), max(len(a), len(b))))
    return diffs


def short_version(v: str | None) -> str:
    if not v:
        return "unknown"
    m = re.search(r"ffmpeg version (\S+)", v)
    return m.group(1) if m else v.split()[0]


_VER = re.compile(r"ffmpeg version n?(\d+)\.(\d+)")


def parse_version(v: str | None) -> tuple[int, int] | None:
    m = _VER.search(v or "")
    return (int(m.group(1)), int(m.group(2))) if m else None
