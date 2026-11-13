"""Motion-triggered recording: Sentinel watches the camera for movement (``[motion]``).

Used where the recording schedule (``sentinel.schedule``) says ``action = "motion"``, typically
outside working hours: the recorder records only after motion is seen and keeps recording for
``keep_recording_s`` after the LAST motion; every new motion restarts that timer.

    [motion]
    stream_path = "stream2"     # camera's low-resolution second stream (recommended; "" = main stream)
    keep_recording_s = 120      # keep recording this long after the last motion
    changed_percent = 1.0       # share of the picture that must change to count as motion
    pixel_threshold = 25        # how much a point must brighten/darken to count as changed (0-255)
    min_frames = 2              # motion must be seen in this many analysed frames in a row
    analysis_fps = 4            # frames analysed per second (second stream only)

Motion markers for reviewers (``markers = true``, on in deployed configurations): the detector
also runs while recording, and every stretch of motion (from its first moving frame to its last,
joined when less than ``marker_merge_s`` apart, split every ``MAX_PERIOD_S``) is written to the
recording log as a ``motion_period`` event. The review-drive export lists them per video as
"time into this video", and the operator screen's timeline marks them.

How it works. A separate low-priority FFmpeg reads the camera stream (the second, low-resolution
stream when ``stream_path`` is set, otherwise only the key frames of the main stream), shrinks
each analysed frame to 160x120 grey levels and hands it to :class:`MotionAnalyzer`, which counts
how many points changed by more than ``pixel_threshold`` since the previous frame. Motion =
``changed_percent`` of the picture or more, in ``min_frames`` frames in a row (filters single-frame
noise). Detection is Sentinel's own, so it works the same with any camera and is tested here with
the simulated camera; it is not the camera's built-in motion detection.

Fail safe. If the detector gets no frames for ``unhealthy_after_s`` (camera second stream
unreachable, FFmpeg failing), :meth:`MotionDetector.recording_wanted` says "record": a broken
detector never stops recording; the status shows W-001.

Honest limits (also in docs/branches/motion-recording.md): recording starts a few seconds after
motion begins (detection, then connecting to the main stream and waiting for its next key frame);
nothing before the trigger is kept. Slow movement, small objects far away, darkness and the
camera's day/night switch can be missed or cause false triggers; tune on site with
``Sentinel.exe motion-test`` and the bench test (docs/09).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 160, 120
FRAME_BYTES = WIDTH * HEIGHT
MAX_PERIOD_S = 300.0          # a longer stretch of motion is logged in parts (none is lost in a crash)


class MotionConfigError(ValueError):
    pass


@dataclass(frozen=True)
class MotionConfig:
    stream_path: str = ""
    keep_recording_s: float = 120.0
    changed_percent: float = 1.0
    pixel_threshold: int = 25
    min_frames: int = 2
    analysis_fps: float = 4.0
    unhealthy_after_s: float = 15.0
    markers: bool = False              # log motion periods for reviewers (deployed configurations: true)
    marker_merge_s: float = 10.0       # motion less than this far apart is one period


def validate(m: MotionConfig) -> None:
    def rng(name: str, lo: float, hi: float) -> None:
        v = getattr(m, name)
        if not lo <= v <= hi:
            raise MotionConfigError(f"[motion] {name} must be between {lo:g} and {hi:g}")

    rng("keep_recording_s", 10, 3600)
    rng("changed_percent", 0.05, 50)
    rng("pixel_threshold", 5, 200)
    rng("min_frames", 1, 20)
    rng("analysis_fps", 0.5, 15)
    rng("unhealthy_after_s", 5, 300)
    rng("marker_merge_s", 1, 300)
    p = m.stream_path
    if p and (any(c.isspace() for c in p) or "@" in p or "://" in p):
        raise MotionConfigError("[motion] stream_path must be only the path of the camera's second stream "
                                "(no spaces, '@' or '://')")


def detection_url(camera_url: str, stream_path: str) -> str:
    """The camera URL with its path replaced by ``stream_path`` ("" = the main stream itself)."""
    if not stream_path:
        return camera_url
    parts = urlsplit(camera_url)
    path, _, query = stream_path.lstrip("/").partition("?")
    return urlunsplit((parts.scheme, parts.netloc, "/" + path, query, ""))


def detector_argv(ffmpeg: str, url: str, m: MotionConfig, transport: str = "tcp", input_format: str | None = None,
                  io_timeout_s: float = 10.0) -> list[str]:
    scheme = urlsplit(url).scheme
    io_us = str(int(io_timeout_s * 1_000_000))
    argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-nostdin"]
    if scheme in ("rtsp", "rtsps"):
        argv += ["-rtsp_transport", transport, "-timeout", io_us]
    elif scheme == "tcp":
        argv += ["-rw_timeout", io_us]
    if not m.stream_path:
        argv += ["-skip_frame", "nokey"]        # main stream: decode key frames only (cheap)
    if input_format:
        argv += ["-f", input_format]
    argv += ["-i", url, "-map", "0:v:0", "-an", "-dn", "-sn"]
    vf = f"scale={WIDTH}:{HEIGHT},format=gray"
    if m.stream_path:
        vf = f"fps={m.analysis_fps:g}," + vf
    argv += ["-vf", vf]
    if not m.stream_path:
        # Key frames only: never repeat a key frame to fill the frame rate (repeats look like
        # "no change" and would hide motion).
        argv += ["-fps_mode", "passthrough"]
    argv += ["-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    return argv


# ---------------------------------------------------------------------- analysis (pure)
class MotionAnalyzer:
    """Frame differencing on small grey frames. ``feed`` returns True when motion is confirmed."""

    def __init__(self, m: MotionConfig) -> None:
        self.m = m
        self._prev: bytes | None = None
        self._run = 0
        self.last_score = 0.0           # percent of the picture changed in the last frame
        self.duplicate = False          # the last frame was an exact repeat of the one before
        self._repeats = 0
        # Repeats the fps filter can make (second stream only, down to ~0.5 pictures a second); a longer
        # run of identical frames is a picture that really did not change (a frozen stream), scored as
        # such. Main-stream key frames are passed through, never repeated: no allowance there.
        self._max_repeats = max(2, int(2 * float(m.analysis_fps))) if m.stream_path else 0

    @property
    def run_length(self) -> int:
        """Consecutive frames above the threshold so far (0 = this frame showed no motion)."""
        return self._run

    def score(self, a: bytes, b: bytes) -> float:
        t = self.m.pixel_threshold
        changed = sum(1 for x, y in zip(a, b) if (x - y if x > y else y - x) > t)
        return 100.0 * changed / max(1, len(a))

    def feed(self, frame: bytes) -> bool:
        # An exact repeat carries no new picture: FFmpeg's fps filter repeats frames when the second
        # stream is slower than analysis_fps. Scoring it as "no change" reset the count every other
        # frame, so motion was never confirmed (review 2026-11-12). It is skipped instead.
        self.duplicate = False
        if self._prev is not None and frame == self._prev:
            self._repeats += 1
            if self._repeats <= self._max_repeats:
                self.duplicate = True
                return False
            self.last_score, self._run = 0.0, 0
            return False
        self._repeats = 0
        prev, self._prev = self._prev, frame
        if prev is None or len(prev) != len(frame):
            return False
        self.last_score = self.score(prev, frame)
        if self.last_score >= self.m.changed_percent:
            self._run += 1
        else:
            self._run = 0
        return self._run >= self.m.min_frames

    def reset(self) -> None:
        self._prev, self._run, self.duplicate, self._repeats = None, 0, False, 0


# ---------------------------------------------------------------------- detector (thread + FFmpeg)
class MotionDetector:
    """Keeps a low-priority FFmpeg reading the camera and tracks the last motion time."""

    def __init__(self, ffmpeg: str, url: str, m: MotionConfig, transport: str = "tcp",
                 input_format: str | None = None, popen_kwargs: dict[str, Any] | None = None,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                 on_motion: Callable[[float, float], None] | None = None,
                 on_period: Callable[[float, float, float], None] | None = None) -> None:
        self.m = m
        self.argv = detector_argv(ffmpeg, url, m, transport, input_format)
        self.popen_kwargs = dict(popen_kwargs or {})
        self.clock, self.mono = clock, mono
        self.on_motion = on_motion
        self.on_period = on_period            # (start_epoch, end_epoch, peak_percent) of a closed motion period
        self._candidate_start: float | None = None
        self._period: list[float] | None = None   # [start, last motion, peak]
        self.analyzer = MotionAnalyzer(m)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self.started_mono: float | None = None
        self.last_frame_mono: float | None = None
        self.last_motion_epoch: float | None = None
        self.last_motion_mono: float | None = None
        self.motion_count = 0
        self.restarts = 0
        self.last_error = ""

    # -------------------------------------------------------------- state (thread-safe)
    def healthy(self) -> bool:
        with self._lock:
            if self.started_mono is None:
                return False
            ref = self.last_frame_mono if self.last_frame_mono is not None else self.started_mono
            return self.mono() - ref <= self.m.unhealthy_after_s and self.last_frame_mono is not None

    def warming_up(self) -> bool:
        """Just started, no frame yet, still within the allowance (not yet a fault)."""
        with self._lock:
            return (self.started_mono is not None and self.last_frame_mono is None
                    and self.mono() - self.started_mono <= self.m.unhealthy_after_s)

    def seconds_since_motion(self) -> float | None:
        with self._lock:
            return None if self.last_motion_mono is None else self.mono() - self.last_motion_mono

    def recording_wanted(self) -> bool:
        """Record now? Yes within keep_recording_s of the last motion, and whenever the detector is
        not working (fail safe). In its first seconds (no frame yet, within unhealthy_after_s) it
        is not yet known; the answer is then no, and turns into yes if no frame arrives in time."""
        since = self.seconds_since_motion()
        if since is not None and since <= self.m.keep_recording_s:
            return True
        return not self.healthy() and not self.warming_up()

    def snapshot(self) -> dict[str, Any]:
        since = self.seconds_since_motion()
        return {"healthy": self.healthy(), "warming_up": self.warming_up(),
                "last_motion_epoch": self.last_motion_epoch,
                "seconds_since_motion": None if since is None else round(since, 1),
                "keep_recording_s": self.m.keep_recording_s, "motion_count": self.motion_count,
                "last_score_percent": round(self.analyzer.last_score, 2), "restarts": self.restarts}

    # -------------------------------------------------------------- lifecycle
    def start(self) -> MotionDetector:
        with self._lock:
            self.started_mono = self.mono()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="motion", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._kill()
        if self._thread is not None:
            self._thread.join(timeout)
        self._close_period()

    # -------------------------------------------------------------- motion periods (reviewer markers)
    def _track(self, now: float, confirmed: bool) -> None:
        score = self.analyzer.last_score
        if self.analyzer.duplicate:
            pass                                     # a repeated frame: the count did not change
        elif self.analyzer.run_length == 1:
            self._candidate_start = now              # first moving frame of a possible period
        elif self.analyzer.run_length == 0:
            self._candidate_start = None
        p = self._period
        if confirmed:
            if p is None:
                self._period = [self._candidate_start or now, now, score]
            else:
                p[1], p[2] = now, max(p[2], score)
                if p[1] - p[0] >= MAX_PERIOD_S:
                    self._close_period()
                    self._period = [now, now, score]
        elif p is not None and now - p[1] > self.m.marker_merge_s:
            self._close_period()

    def _close_period(self) -> None:
        with self._lock:                         # reader thread and stop() may both close it
            p, self._period = self._period, None
        if p is not None and self.on_period is not None:
            try:
                self.on_period(p[0], p[1], p[2])
            except Exception:
                log.exception("motion period could not be logged")

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _loop(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            got = self._run_ffmpeg()
            if self._stop.is_set():
                break
            self.restarts += 1
            delay = 1.0 if got else min(delay * 2, 30.0)
            log.warning("motion detector stopped (%s); restarting in %.0f s", self.last_error or "end of stream",
                        delay)
            self._stop.wait(delay)

    def _run_ffmpeg(self) -> bool:
        """One FFmpeg lifetime. True if any frame arrived."""
        self.analyzer.reset()
        got = False
        kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                              **self.popen_kwargs}
        if sys.platform == "win32":
            kw["creationflags"] = kw.get("creationflags", 0) | 0x08000000      # CREATE_NO_WINDOW
        try:
            self._proc = proc = subprocess.Popen(self.argv, **kw)
        except OSError as exc:
            self.last_error = f"FFmpeg could not be started: {exc}"
            return False
        # Only the last lines are kept (review 2026-11-10: the whole stderr of a days-long FFmpeg
        # was held in memory).
        err: deque[bytes] = deque(maxlen=40)

        def read_err() -> None:
            for line in iter(proc.stderr.readline, b"") if proc.stderr else ():
                err.append(line)

        t = threading.Thread(target=read_err, name="motion-stderr", daemon=True)
        t.start()
        # An FFmpeg that stays connected but delivers no frames is restarted (review 2026-11-10:
        # it blocked the reader for good, so motion stayed broken until the service restarted).
        started = self.mono()
        stall_s = max(30.0, 2 * self.m.unhealthy_after_s)
        stalled: list[bool] = []

        def watchdog() -> None:
            while proc.poll() is None and not self._stop.is_set():
                with self._lock:
                    last = self.last_frame_mono
                if self.mono() - max(started, last or started) > stall_s:
                    stalled.append(True)
                    log.warning("motion detector: no pictures for %.0f s; restarting it", stall_s)
                    self._kill()
                    return
                self._stop.wait(1.0)

        threading.Thread(target=watchdog, name="motion-watchdog", daemon=True).start()
        try:
            assert proc.stdout is not None
            while not self._stop.is_set():
                frame = _read_exact(proc.stdout, FRAME_BYTES)
                if frame is None:
                    break
                got = True
                now_mono, now = self.mono(), self.clock()
                with self._lock:
                    self.last_frame_mono = now_mono
                confirmed = self.analyzer.feed(frame)
                self._track(now, confirmed)
                if confirmed:
                    with self._lock:
                        first = self.last_motion_mono is None or now_mono - self.last_motion_mono > self.m.keep_recording_s
                        self.last_motion_mono, self.last_motion_epoch = now_mono, now
                        self.motion_count += 1
                    if first and self.on_motion is not None:
                        try:
                            self.on_motion(now, self.analyzer.last_score)
                        except Exception:
                            log.exception("motion callback failed")
        finally:
            self._close_period()                     # stream ended: the period ends at its last motion
            self._kill()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
            t.join(2)
            from .credentials import redact

            tail = b"".join(err).decode("utf-8", "replace").strip().splitlines()
            self.last_error = (f"no pictures for {stall_s:.0f} s" if stalled else
                               redact(tail[-1].strip()) if tail else f"exit code {proc.returncode}")
        return got


def _read_exact(stream: Any, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


# ---------------------------------------------------------------------- technician tuning
def run_motion_test(cfg: Any, seconds: float = 60.0, out: Callable[[str], None] = print,
                    sleep: Callable[[float], None] = time.sleep) -> int:
    """``Sentinel.exe motion-test``: watch the camera with the configured settings and print, once a
    second, how much of the picture changed and whether that counts as motion. Walk through the
    scene while it runs; adjust [motion] changed_percent / pixel_threshold until walking through
    is always MOTION and an empty scene never is. Records nothing."""
    from .credentials import CredentialError, load_credentials, url_with_credentials

    if cfg.camera.pending:
        out("No camera has been added yet (run 'Sentinel set-camera' first).")
        return 2
    creds = None
    if cfg.camera.credentials_file:
        try:
            creds = load_credentials(cfg.camera.credentials_file)
        except CredentialError as exc:
            out(f"The camera credentials cannot be read: {exc}")
            return 2
    m = cfg.motion
    url = detection_url(url_with_credentials(cfg.camera.url, creds), m.stream_path)
    det = MotionDetector(cfg.tools.ffmpeg, url, m, cfg.camera.transport, cfg.camera.input_format).start()
    out(f"Watching {'the second stream ' + m.stream_path if m.stream_path else 'key frames of the main stream'} "
        f"for {seconds:.0f} s. Motion = at least {m.changed_percent:g} % of the picture changed by more than "
        f"{m.pixel_threshold} levels, in {m.min_frames} analysed frame(s) in a row. Walk through the scene now.")
    shown_motion = 0
    try:
        for i in range(int(seconds)):
            sleep(1.0)
            snap = det.snapshot()
            if not snap["healthy"]:
                state = "connecting..." if snap["warming_up"] else f"NO PICTURES ({det.last_error or 'no answer'})"
                out(f"{i + 1:4d}s  {state}")
                continue
            moving = det.motion_count > shown_motion
            shown_motion = det.motion_count
            bar = "#" * min(40, int(snap["last_score_percent"] / max(m.changed_percent, 0.01) * 10))
            out(f"{i + 1:4d}s  changed {snap['last_score_percent']:6.2f} %  {'MOTION' if moving else '      '}  {bar}")
    finally:
        det.stop()
    out(f"Done: motion seen {det.motion_count} time(s). (Ten '#' = exactly the motion threshold.)")
    return 0 if det.last_frame_mono is not None else 4
