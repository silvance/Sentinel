"""FFmpeg work for review files: MP4 remux and decoded per-frame hashing.

Remux (checked against FFmpeg 9.0.1 ``ffmpeg -h demuxer=concat`` / ``-h muxer=mp4``)::

    ffmpeg -f concat -safe 0 -i <list.ffconcat> -map 0:v:0 -c copy -an -sn -dn
           -movflags +faststart -f mp4 -n <dest>.mp4.partial

* concat demuxer: file paths in the list are absolute (relative ones resolve against the list's
  directory) and single-quoted; ``-safe 0`` allows absolute paths.
* ``-c copy``: no re-encoding — the pictures are the camera's. ``+faststart`` moves the index to
  the front so players start immediately and so the file can be decoded from a pipe (used to
  verify the bytes read back from the drive in a single pass).
* ``-n``: never overwrite. Video only: audio is excluded by default (R5); if a deployment ever
  records audio it is present only in the originals (documented limitation).

Measured on this host (FFmpeg 9.0.1): concatenating recorder segments (including a truncated,
interrupted one) gives an MP4 whose per-frame decoded SHA-256 list equals the concatenation of
the segments' lists; remuxing twice gives byte-identical files.
"""

from __future__ import annotations

import json
import subprocess
import threading
from fractions import Fraction
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from ..media.videohash import parse_framehash

FRAMEHASH_ARGS = ["-map", "0:v:0", "-an", "-sn", "-dn", "-fps_mode", "passthrough",
                  "-f", "framehash", "-hash", "sha256", "-"]


class MediaError(RuntimeError):
    def __init__(self, message: str, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail

    @property
    def disk_full(self) -> bool:
        t = self.stderr_tail.lower()
        return "no space left" in t or "disk full" in t or "not enough space" in t


def _tail(b: bytes, n: int = 600) -> str:
    return b.decode("utf-8", "replace").strip()[-n:]


def write_concat_list(path: Path, sources: list[Path]) -> None:
    lines = ["ffconcat version 1.0"]
    for s in sources:
        p = Path(s).resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{p}'")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remux_argv(ffmpeg: str, list_path: Path, out_partial: Path, codec: str | None = None) -> list[str]:
    """``codec`` is the recorded video codec (ffprobe name). H.265 is labelled ``hvc1``, the MP4 tag
    Windows' and Apple's players expect (FFmpeg's default ``hev1`` is not opened by QuickTime)."""
    tag = ["-tag:v", "hvc1"] if codec == "hevc" else []
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-map", "0:v:0", "-c", "copy", *tag, "-an", "-sn", "-dn", "-movflags", "+faststart",
            "-f", "mp4", "-n", str(out_partial)]


def remux_concat(ffmpeg: str, sources: list[Path], out_partial: Path, list_path: Path, timeout: float,
                 popen_kwargs: dict[str, Any] | None = None, codec: str | None = None) -> None:
    write_concat_list(list_path, sources)
    try:
        cp = subprocess.run(remux_argv(ffmpeg, list_path, out_partial, codec), stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=timeout, check=False, **(popen_kwargs or {}))
    except FileNotFoundError:
        raise MediaError(f"tool not found: {ffmpeg}") from None
    except subprocess.TimeoutExpired:
        raise MediaError(f"MP4 remux timed out after {timeout:.0f}s") from None
    finally:
        try:
            list_path.unlink()
        except OSError:
            pass
    if cp.returncode != 0:
        raise MediaError(f"MP4 remux failed (exit {cp.returncode})", _tail(cp.stderr))


def frame_hashes_from_path(ffmpeg: str, path: Path, timeout: float,
                           popen_kwargs: dict[str, Any] | None = None, input_args: list[str] | None = None) -> list[str]:
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *(input_args or []), "-i", str(path), *FRAMEHASH_ARGS]
    try:
        cp = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False,
                            **(popen_kwargs or {}))
    except FileNotFoundError:
        raise MediaError(f"tool not found: {ffmpeg}") from None
    except subprocess.TimeoutExpired:
        raise MediaError(f"frame hashing timed out after {timeout:.0f}s") from None
    if cp.returncode != 0:
        raise MediaError(f"frame hashing failed (exit {cp.returncode})", _tail(cp.stderr))
    return [f.sha256 for f in parse_framehash(cp.stdout.decode("ascii", "replace"))]


def has_decode_errors(ffmpeg: str, path: Path, timeout: float, popen_kwargs: dict[str, Any] | None = None) -> bool:
    """True if FFmpeg's decoder reports errors (damaged pictures) anywhere in ``path``."""
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"]
    try:
        cp = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False,
                            **(popen_kwargs or {}))
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"decoder check failed: {exc}") from None
    return cp.returncode != 0 or bool(cp.stderr.strip())


def packet_pts(ffprobe: str, path: Path, timeout: float) -> tuple[list[int], Fraction]:
    """(sorted presentation timestamps of every video packet, stream time base). Sorted pts order
    is decoded frame order with ``-fps_mode passthrough``. Demux only (no decoding): fast."""
    argv = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=time_base:packet=pts",
            "-of", "json", str(path)]
    try:
        cp = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"packet listing failed: {exc}") from None
    if cp.returncode != 0:
        raise MediaError(f"packet listing failed (exit {cp.returncode})", _tail(cp.stderr))
    try:
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
        tb = Fraction(data["streams"][0]["time_base"])
    except (ValueError, KeyError, IndexError, ZeroDivisionError):
        raise MediaError("packet listing unreadable") from None
    pts = sorted(int(p["pts"]) for p in data.get("packets", []) if p.get("pts") not in (None, "N/A"))
    return pts, tb


def frame_hashes_span(ffmpeg: str, path: Path, seek_s: float | None, frames: int, timeout: float,
                      popen_kwargs: dict[str, Any] | None = None) -> list[tuple[int, str]]:
    """Decode ``frames`` frames starting at the first frame with pts >= ``seek_s`` (accurate
    seek: FFmpeg decodes from the preceding keyframe and drops the frames before the target).
    Returns (pts in the stream time base, frame SHA-256) pairs. ``-copyts`` and
    ``-enc_time_base demux`` keep the container timestamps (verified on FFmpeg 9.0.1)."""
    seek = ["-copyts", "-ss", f"{seek_s:.6f}"] if seek_s is not None else []
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *seek, "-i", str(path), "-frames:v", str(frames),
            "-map", "0:v:0", "-an", "-sn", "-dn", "-fps_mode", "passthrough", "-enc_time_base:v", "demux",
            "-f", "framehash", "-hash", "sha256", "-"]
    try:
        cp = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False,
                            **(popen_kwargs or {}))
    except FileNotFoundError:
        raise MediaError(f"tool not found: {ffmpeg}") from None
    except subprocess.TimeoutExpired:
        raise MediaError(f"span decode timed out after {timeout:.0f}s") from None
    if cp.returncode != 0:
        raise MediaError(f"span decode failed (exit {cp.returncode})", _tail(cp.stderr))
    return [(f.pts, f.sha256) for f in parse_framehash(cp.stdout.decode("ascii", "replace"))]


def frame_hashes_from_stream(ffmpeg: str, chunks: Iterable[bytes], timeout: float,
                             popen_kwargs: dict[str, Any] | None = None,
                             on_chunk: Callable[[bytes], None] | None = None) -> list[str]:
    """Decode bytes fed through stdin (``-i pipe:0``) and return the per-frame SHA-256 list.

    ``on_chunk`` sees every chunk before FFmpeg does (used to hash the same bytes), so one read
    of the destination file yields both its file SHA-256 and its decoded content. Exceptions
    raised while reading ``chunks`` (e.g. the drive was unplugged) are re-raised here.
    """
    argv = [ffmpeg, "-hide_banner", "-v", "error", "-i", "pipe:0", *FRAMEHASH_ARGS]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                **(popen_kwargs or {}))
    except FileNotFoundError:
        raise MediaError(f"tool not found: {ffmpeg}") from None
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    feed_error: list[BaseException] = []
    err_buf: list[bytes] = []
    out_buf: list[bytes] = []

    def feed() -> None:
        assert proc.stdin is not None
        it: Iterator[bytes] = iter(chunks)
        try:
            for c in it:
                if on_chunk is not None:
                    on_chunk(c)
                try:
                    proc.stdin.write(c)
                except (BrokenPipeError, OSError):
                    # FFmpeg stopped reading (error); keep consuming so the hash covers the file.
                    for rest in it:
                        if on_chunk is not None:
                            on_chunk(rest)
                    break
        except BaseException as exc:  # read error on the drive
            feed_error.append(exc)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=feed, name="export-feed", daemon=True),
               threading.Thread(target=lambda: err_buf.append(proc.stderr.read()), daemon=True),  # type: ignore[union-attr]
               threading.Thread(target=lambda: out_buf.append(proc.stdout.read()), daemon=True)]  # type: ignore[union-attr]
    for t in threads:
        t.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(10)
        raise MediaError(f"verification decode timed out after {timeout:.0f}s") from None
    for t in threads:
        t.join(30)
    if feed_error:
        raise feed_error[0]
    if rc != 0:
        raise MediaError(f"verification decode failed (exit {rc})", _tail(b"".join(err_buf)))
    return [f.sha256 for f in parse_framehash(b"".join(out_buf).decode("ascii", "replace"))]
