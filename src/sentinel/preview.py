"""Live view (owner request 2026-11-10): the recorder service shows the window what the camera sees.

The window never connects to the camera itself: operators are standard users and must not be able to
read the camera account. Instead, while a window shows the live view it keeps a request file fresh in
the operators' request folder (``<status_dir>/requests/preview.request``, the folder the window
already uses for archive swaps). While that file is fresh, this worker runs a low-priority FFmpeg on
the camera's MAIN stream (owner decision 2026-11-10: viewers see the full-quality picture, not the
low-resolution second stream used for motion detection) and writes the newest frame, about five
times a second and up to 1920 pixels wide, as a JPEG into ``<status_dir>/preview`` (readable by
operators):

* ``frame-0.jpg`` / ``frame-1.jpg`` alternately, so the window never reads a file being written;
* ``latest.json``: which file is newest, when it was taken, its size, and the worker's state.

The request is untrusted input from a standard user: only a small regular file is accepted, and all it
can do is turn the preview on for ``REQUEST_TTL_S``. Nothing here is recorded or hashed: the live
view is for checking the picture, it is not evidence. With no viewer the FFmpeg is stopped and the
frames are deleted.

The frame source is deliberately separate from the window, so a later live-stream feature can use it.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .clock import iso_utc

log = logging.getLogger("sentinel.preview")

REQUEST_NAME = "preview.request"
REQUEST_TTL_S = 20.0            # the window refreshes the request every few seconds
REQUEST_MAX_BYTES = 1024
PREVIEW_DIR = "preview"
LATEST = "latest.json"
FPS = 5
WIDTH = 1920                    # full detail of the main stream, at most full-HD wide
MAX_FRAME_BYTES = 4 * 1024 * 1024


def preview_dir(cfg: Any) -> Path:
    return Path(cfg.status_dir_path) / PREVIEW_DIR


def request_path(cfg: Any) -> Path:
    from .archive.worker import operator_request_dir

    return operator_request_dir(cfg) / REQUEST_NAME


def request_preview(cfg: Any, clock: Callable[[], float] = time.time) -> Path:
    """Used by the window: ask for (or keep) the live view. Best effort."""
    p = request_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(iso_utc(clock()), encoding="ascii")
    return p


def read_latest(cfg: Any) -> dict[str, Any] | None:
    """Used by the window: the newest frame's description (None if there is none)."""
    try:
        doc = json.loads((preview_dir(cfg) / LATEST).read_text("utf-8"))
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def preview_argv(ffmpeg: str, url: str, transport: str = "tcp", input_format: str | None = None,
                 io_timeout_s: float = 10.0) -> list[str]:
    scheme = urlsplit(url).scheme
    io_us = str(int(io_timeout_s * 1_000_000))
    argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-nostdin"]
    if scheme in ("rtsp", "rtsps"):
        argv += ["-rtsp_transport", transport, "-timeout", io_us]
    elif scheme == "tcp":
        argv += ["-rw_timeout", io_us]
    if input_format:
        argv += ["-f", input_format]
    argv += ["-i", url, "-map", "0:v:0", "-an", "-dn", "-sn",
             "-vf", f"fps={FPS},scale='min({WIDTH},iw)':-2", "-q:v", "4",
             "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]
    return argv


def split_jpegs(buf: bytearray) -> list[bytes]:
    """Remove and return complete JPEG images (SOI ... EOI) from the start of ``buf``."""
    out = []
    while True:
        start = buf.find(b"\xff\xd8")
        if start < 0:
            buf.clear()
            return out
        end = buf.find(b"\xff\xd9", start + 2)
        if end < 0:
            if start:
                del buf[:start]
            if len(buf) > MAX_FRAME_BYTES:
                buf.clear()
            return out
        out.append(bytes(buf[start:end + 2]))
        del buf[:end + 2]


def request_is_fresh(path: Path, now: float, ttl: float | None = None) -> bool:
    """A small regular file (not a link or folder) changed within ``ttl`` seconds."""
    ttl = REQUEST_TTL_S if ttl is None else ttl
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > REQUEST_MAX_BYTES:
        return False
    if getattr(st, "st_file_attributes", 0) & 0x400:      # reparse point (junction / symlink)
        return False
    return -5.0 <= now - st.st_mtime <= ttl


class PreviewWorker:
    """Runs (and stops) the preview FFmpeg on demand. All failures are logged and retried; nothing
    here can affect recording."""

    def __init__(self, cfg: Any, url: str, popen_kwargs: dict[str, Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.url = url                         # may contain the camera account: never logged
        self.popen_kwargs = dict(popen_kwargs or {})
        self.clock = clock
        self.out = preview_dir(cfg)
        self.req = request_path(cfg)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self.seq = 0
        self.frame: str | None = None
        self.state = "off"                     # off | starting | live | no_picture

    def start(self) -> PreviewWorker:
        self._thread = threading.Thread(target=self._loop, name="live-preview", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._kill()
        if self._thread is not None:
            self._thread.join(10)
        self._clear()

    # ---------------------------------------------------------------- loop
    def _loop(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            if not request_is_fresh(self.req, self.clock()):
                if self.state != "off":
                    log.info("live view: no viewer; stopped")
                    self._clear()
                self.state = "off"
                self._stop.wait(1.0)
                continue
            self.state = "starting"
            self._write_state()
            got = self._run_ffmpeg()
            if self._stop.is_set():
                break
            if not got:
                self.state = "no_picture"
                self._write_state()
                delay = min(delay * 2, 15.0)
            else:
                delay = 1.0
            self._stop.wait(delay)

    def _argv(self) -> list[str]:
        c = self.cfg
        return preview_argv(c.tools.ffmpeg, self.url, c.camera.transport, c.camera.input_format)

    def _run_ffmpeg(self) -> bool:
        kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
                              **self.popen_kwargs}
        if sys.platform == "win32":
            kw["creationflags"] = kw.get("creationflags", 0) | 0x08000000      # CREATE_NO_WINDOW
        try:
            self._proc = proc = subprocess.Popen(self._argv(), **kw)
        except OSError as exc:
            log.warning("live view: FFmpeg could not be started: %s", exc)
            return False
        got = False
        buf = bytearray()
        last_check = 0.0
        try:
            assert proc.stdout is not None
            while not self._stop.is_set():
                chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                for jpeg in split_jpegs(buf):
                    got = True
                    self.state = "live"
                    self._publish(jpeg)
                now = time.monotonic()
                if now - last_check > 1.0:     # viewer gone: stop the camera connection
                    last_check = now
                    if not request_is_fresh(self.req, self.clock()):
                        break
        finally:
            self._kill()
        return got

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    # ---------------------------------------------------------------- output
    def _publish(self, jpeg: bytes) -> None:
        self.seq += 1
        name = f"frame-{self.seq % 2}.jpg"
        try:
            self.out.mkdir(parents=True, exist_ok=True)
            tmp = self.out / (name + ".partial")
            tmp.write_bytes(jpeg)
            os.replace(tmp, self.out / name)
            self.frame = name
            self._write_state()
        except OSError as exc:                # e.g. the window was reading that file: next frame
            log.debug("live view frame not written: %s", exc)

    def _write_state(self) -> None:
        doc = {"state": self.state, "seq": self.seq, "frame": self.frame, "taken_utc": iso_utc(self.clock()),
               "fps": FPS, "source": "main stream"}
        try:
            self.out.mkdir(parents=True, exist_ok=True)
            tmp = self.out / (LATEST + ".partial")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            os.replace(tmp, self.out / LATEST)
        except OSError:
            pass

    def _clear(self) -> None:
        self.frame = None
        for p in (self.out.glob("*") if self.out.is_dir() else []):
            try:
                p.unlink()
            except OSError:
                pass
