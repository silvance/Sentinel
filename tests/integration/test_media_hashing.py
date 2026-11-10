"""Real-FFmpeg tests of segment validation and video hashing.

These establish *what each hash proves*:
* file SHA-256 changes on any byte change, including a lossless remux;
* decoded-content SHA-256 survives a TS->MP4 remux but changes if pictures change;
* per-frame hashes localise altered frames.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from sentinel.hashing import sha256_file
from sentinel.media.probe import probe_segment
from sentinel.media.videohash import compare_frame_lists, compute_content_hash, parse_framehash

pytestmark = [pytest.mark.integration, requires_ffmpeg]

FPS = 15


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args], check=True)


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("media")
    out = d / "clip.ts"
    _ff("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={FPS}", "-t", "4", "-c:v", "libx264",
        "-preset", "ultrafast", "-g", str(FPS), "-bf", "0", "-pix_fmt", "yuv420p", "-f", "mpegts", str(out))
    return out


def test_probe_valid_segment(clip: Path) -> None:
    r = probe_segment(clip, max_internal_gap_s=1.0)
    assert r.playable and r.codec == "h264" and (r.width, r.height) == (320, 240)
    assert r.packet_count == 4 * FPS and r.keyframe_count == 4
    assert r.duration_s == pytest.approx(4.0, abs=0.1)
    assert r.pts_gaps == [] and r.starts_with_keyframe and r.pix_fmt == "yuv420p"


def test_probe_truncated_segment_is_playable_but_shorter(clip: Path, tmp_path: Path) -> None:
    cut = tmp_path / "cut.ts"
    data = clip.read_bytes()
    cut.write_bytes(data[: len(data) // 2 + 77])          # not on a TS packet boundary, like a power cut
    r = probe_segment(cut)
    assert r.playable
    assert 0 < r.packet_count < 4 * FPS


@pytest.mark.parametrize("content,reason", [(b"", "empty_or_missing_file"),
                                            (b"\x00" * 50_000, "no_video_stream")], ids=["empty", "zeros"])
def test_probe_unusable_files(tmp_path: Path, content: bytes, reason: str) -> None:
    f = tmp_path / "bad.ts"
    f.write_bytes(content)
    r = probe_segment(f)
    assert not r.playable and r.reason == reason


def test_probe_random_bytes_not_playable(tmp_path: Path) -> None:
    f = tmp_path / "rand.ts"
    f.write_bytes(bytes((i * 7919) % 251 for i in range(200_000)))
    assert not probe_segment(f).playable


def test_probe_detects_internal_timestamp_gap(tmp_path: Path) -> None:
    a, b, joined = tmp_path / "a.ts", tmp_path / "b.ts", tmp_path / "gap.ts"
    common = ["-c:v", "libx264", "-preset", "ultrafast", "-g", str(FPS), "-bf", "0", "-pix_fmt", "yuv420p"]
    _ff("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={FPS}", "-t", "2", *common, "-f", "mpegts", str(a))
    # Second part starts 5 s later in stream time: a 3 s hole, as when a camera stream freezes.
    _ff("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={FPS}", "-t", "2", *common,
        "-output_ts_offset", "5", "-f", "mpegts", str(b))
    _ff("-i", f"concat:{a}|{b}", "-c", "copy", "-f", "mpegts", str(joined))
    r = probe_segment(joined, max_internal_gap_s=1.0)
    assert r.playable and "internal_timestamp_gap" in r.warnings
    assert len(r.pts_gaps) == 1
    before, after = r.pts_gaps[0]
    assert after - before == pytest.approx(3.0 + 1 / FPS, abs=0.2)


def test_pts_wraparound_is_not_a_gap(tmp_path: Path) -> None:
    # MPEG-TS timestamps are 33-bit at 90 kHz and wrap every ~26.5 h. A segment recorded across
    # the wrap must not be reported as a huge timestamp gap during multi-day recording.
    f = tmp_path / "wrap.ts"
    _ff("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={FPS}", "-t", "4", "-c:v", "libx264",
        "-preset", "ultrafast", "-g", str(FPS), "-bf", "0", "-pix_fmt", "yuv420p",
        "-output_ts_offset", f"{2**33 / 90000 - 2.0:.3f}", "-f", "mpegts", str(f))
    r = probe_segment(f, max_internal_gap_s=1.0)
    assert r.playable and r.pts_gaps == [] and r.duration_s == pytest.approx(4.0, abs=0.1)
    assert r.max_pts_gap_s < 0.2


def test_content_hash_survives_remux_but_file_hash_does_not(clip: Path, tmp_path: Path) -> None:
    mp4 = tmp_path / "derivative.mp4"
    _ff("-i", str(clip), "-map", "0:v:0", "-c", "copy", "-movflags", "+faststart", str(mp4))
    assert sha256_file(clip)[0] != sha256_file(mp4)[0]
    f_ts, f_mp4 = tmp_path / "ts.frames", tmp_path / "mp4.frames"
    h_ts = compute_content_hash(clip, frames_out=f_ts)
    h_mp4 = compute_content_hash(mp4, frames_out=f_mp4)
    assert h_ts.sha256 == h_mp4.sha256
    assert h_ts.frame_count == h_mp4.frame_count == 4 * FPS
    assert compare_frame_lists(parse_framehash(f_ts.read_text()), parse_framehash(f_mp4.read_text())) == []
    assert "fps_mode=passthrough" in h_ts.method


def test_content_hash_detects_changed_pictures(clip: Path, tmp_path: Path) -> None:
    altered = tmp_path / "altered.ts"
    # Re-encode with one frame region painted over: same length, same container, different pictures.
    _ff("-i", str(clip), "-vf", "drawbox=x=10:y=10:w=40:h=40:color=red:t=fill:enable='eq(n,20)'",
        "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-g", str(FPS), "-bf", "0",
        "-pix_fmt", "yuv420p", "-f", "mpegts", str(altered))
    ref = tmp_path / "ref.ts"
    _ff("-i", str(clip), "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-g", str(FPS), "-bf", "0",
        "-pix_fmt", "yuv420p", "-f", "mpegts", str(ref))
    fa, fr = tmp_path / "a.frames", tmp_path / "r.frames"
    ha = compute_content_hash(altered, frames_out=fa)
    hr = compute_content_hash(ref, frames_out=fr)
    assert ha.sha256 != hr.sha256
    assert compare_frame_lists(parse_framehash(fa.read_text()), parse_framehash(fr.read_text())) == [20]


def test_single_flipped_byte_changes_file_hash(clip: Path, tmp_path: Path) -> None:
    t = tmp_path / "tampered.ts"
    data = bytearray(clip.read_bytes())
    data[len(data) // 3] ^= 0x01
    t.write_bytes(bytes(data))
    assert sha256_file(t)[0] != sha256_file(clip)[0]
