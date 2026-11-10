"""FFmpeg recording supervisor.

One FFmpeg child per *run*. The supervisor measures, never assumes:
* media is flowing only if FFmpeg's reported stream time advances;
* a run that never produces media within ``connect_timeout_s`` is a connection failure;
* a run whose stream time stops advancing for ``stall_timeout_s`` is a stall, even if the
  process is alive;
* a run whose stream time falls persistently behind real (monotonic) time has lost footage
  that FFmpeg removed from its timeline (e.g. MPEG-TS discontinuity correction absorbs forward
  jumps > 10 s). The lost interval is recorded as a gap and the run is restarted on a fresh
  time anchor;
* every run end is followed by a gap record (via the finaliser queue);
* segment times follow measured reception time: at each segment boundary the run anchor gets a
  rate-limited correction for camera-clock and PC-monotonic drift (``DriftTracker``, docs/03
  §4.2); wall-clock steps and sudden timing jumps are flagged, never followed.

It retries forever: a camera that is unplugged for a week is retried every ``backoff_max_s``
for a week. Unexpected internal errors are retried too; only repeated internal failures end
the process, so the service manager (systemd ``Restart=always``) starts it fresh.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import socket
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..clock import Clock, SystemClock, compact_utc, iso_utc
from ..config import AppConfig
from ..credentials import redact
from ..db import Ledger
from ..models import RunEndReason, SegmentState
from ..platform.base import Platform
from ..storage.monitor import StorageMonitor
from .ffmpeg_cmd import SEGMENT_LIST_NAME, build_record_argv, redacted_command
from .finalizer import Finalizer, GapJob, SegmentJob
from .progress import ProgressParser
from .segment_list import SegmentEntry, SegmentListReader
from .timebase import TIME_METHOD, ClockStepDetector, DriftTracker, RunTimebase, TimeKnot, read_clock_pair

log = logging.getLogger(__name__)
_SEG_RE = re.compile(r"^seg_(\d{6,})\.ts$")
MAX_CONSECUTIVE_INTERNAL_ERRORS = 5


def segment_ready(entry: SegmentEntry, newest_out_time_s: float | None) -> bool:
    """A listed segment may be timed once a progress report beyond its end has been seen (and
    passed through the lost-time check); see the supervisor loop."""
    return newest_out_time_s is not None and newest_out_time_s > entry.end_s


class Backoff:
    def __init__(self, initial: float, maximum: float, reset_after: float) -> None:
        self.initial, self.maximum, self.reset_after = initial, maximum, reset_after
        self._next = initial

    def next_delay(self, media_seconds: float) -> float:
        """Delay before the next attempt, given how long the previous run recorded media."""
        if media_seconds >= self.reset_after:
            self._next = self.initial
        delay = self._next
        self._next = min(self.maximum, self._next * 2)
        return delay


@dataclass
class LiveState:
    """Measurements shared with the status writer (guarded by ``lock``)."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    ffmpeg_running: bool = False
    run_id: str | None = None
    run_launched_utc: str | None = None
    media_seen: bool = False
    last_out_time_s: float | None = None
    last_advance_mono: float | None = None
    last_media_mono_any: float | None = None   # across runs: when media last advanced at all
    fps: float | None = None
    in_backoff: bool = False
    next_attempt_mono: float | None = None
    clock_step_s: float = 0.0
    attempts: int = 0
    last_end_reason: str | None = None
    recent_run_ends: deque = field(default_factory=lambda: deque(maxlen=50))  # (mono, media_s, reason)
    # timeline drift (stream time behind monotonic time)
    lag_s: float = 0.0
    lag_baseline_s: float | None = None
    drift_since_mono: float | None = None
    drift_break_stream_s: float | None = None
    drift_lag_at_start: float = 0.0
    last_ok_out_time_s: float | None = None
    internal_errors: int = 0
    # time accuracy (docs/03 §4.2): measured drift of the current run and the correction applied
    camera_drift_ppm: float | None = None
    host_clock_drift_ppm: float | None = None
    time_correction_s: float | None = None
    clock_slew_run: bool = False               # current run saw a faster-than-drift clock change


@dataclass
class RunOutcome:
    reason: RunEndReason
    exit_code: int | None
    media_seconds: float


class RecordingSupervisor:
    def __init__(self, cfg: AppConfig, ledger: Ledger, platform: Platform, storage: StorageMonitor,
                 finalizer: Finalizer, input_url: str, stop_event: threading.Event,
                 clock: Clock | None = None, ffmpeg_version: str | None = None,
                 tick: Callable[[], None] | None = None) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.platform = platform
        self.storage = storage
        self.finalizer = finalizer
        self._input_url = input_url          # may contain credentials: never log it
        self.stop_event = stop_event
        self.clock = clock or SystemClock()
        self.ffmpeg_version = ffmpeg_version
        self.live = LiveState()
        self._tick = tick or (lambda: None)
        self.cam_dir = cfg.spool_path / cfg.camera.id
        self.current_proc: subprocess.Popen[bytes] | None = None
        self._open_run: str | None = None
        self._open_run_dir: Path = self.cam_dir
        self.backoff = Backoff(cfg.recording.backoff_initial_s, cfg.recording.backoff_max_s,
                               cfg.recording.backoff_reset_after_s)

    # ------------------------------------------------------------------ main loop
    def run_forever(self) -> None:
        self.cam_dir.mkdir(parents=True, exist_ok=True)
        consecutive_internal = 0
        while not self.stop_event.is_set():
            sample = self.storage.sample()
            if sample.level.value == "critical":
                log.error("storage critical (%d bytes free); not starting FFmpeg", sample.free_bytes)
                self._wait(10.0)
                continue
            try:
                outcome = self.run_once()
                consecutive_internal = 0
            except Exception:
                consecutive_internal += 1
                with self.live.lock:
                    self.live.internal_errors += 1
                log.exception("internal error during recording run (%d in a row)", consecutive_internal)
                self._abort_open_run()
                try:
                    self.ledger.add_event("error", "recorder", "internal_error",
                                          "Unexpected internal error in the recorder; retrying",
                                          {"consecutive": consecutive_internal})
                except Exception:
                    log.exception("could not record internal_error event")
                if consecutive_internal >= MAX_CONSECUTIVE_INTERNAL_ERRORS:
                    raise  # let the service manager restart the whole process from a clean state
                outcome = RunOutcome(RunEndReason.INTERNAL_ERROR, None, 0.0)
            if self.stop_event.is_set():
                break
            delay = self.backoff.next_delay(outcome.media_seconds)
            log.warning("run ended (%s, exit %s, %.1fs media); retrying in %.1fs",
                        outcome.reason, outcome.exit_code, outcome.media_seconds, delay)
            with self.live.lock:
                self.live.in_backoff = True
                self.live.next_attempt_mono = self.clock.mono() + delay
            self._wait(delay)
            with self.live.lock:
                self.live.in_backoff = False
                self.live.next_attempt_mono = None

    def _wait(self, seconds: float) -> None:
        end = self.clock.mono() + seconds
        while not self.stop_event.is_set():
            remaining = end - self.clock.mono()
            if remaining <= 0:
                return
            self._tick()
            self.stop_event.wait(min(remaining, self.cfg.service.poll_interval_s))

    # ------------------------------------------------------------------ one FFmpeg lifetime
    def run_once(self) -> RunOutcome:
        rec = self.cfg.recording
        wall0, mono0, _ = read_clock_pair(self.clock)       # tight pair: reference for host drift
        run_id =f"{compact_utc(wall0)}-{secrets.token_hex(4)}"
        run_dir = self.cam_dir / run_id
        argv = build_record_argv(self.cfg, self._input_url, run_dir)
        run_dir.mkdir(parents=True, exist_ok=False)
        rel_dir = str(run_dir.relative_to(self.cfg.spool_path)).replace("\\", "/")
        try:
            self.ledger.create_run(
                run_id=run_id, camera_id=self.cfg.camera.id, directory=rel_dir, launched_utc=iso_utc(wall0),
                launch_mono=mono0, display_timezone=rec.display_timezone, app_version=__version__,
                ffmpeg_version=self.ffmpeg_version, host=socket.gethostname(),
                command_redacted=redacted_command(argv), time_method=TIME_METHOD)
        except Exception:
            self._remove_empty_run_dir(run_dir)
            raise
        self._open_run = run_id
        self._open_run_dir = run_dir
        timebase = RunTimebase(wall0, mono0)
        drift = DriftTracker(rec.max_clock_drift_ppm)
        steps = ClockStepDetector(wall0, mono0, rec.clock_step_tolerance_s)
        with self.live.lock:
            lv = self.live
            lv.run_id, lv.run_launched_utc = run_id, iso_utc(wall0)
            lv.media_seen, lv.last_out_time_s, lv.last_advance_mono = False, None, None
            lv.lag_s, lv.drift_since_mono, lv.drift_break_stream_s, lv.last_ok_out_time_s = 0.0, None, None, None
            lv.lag_baseline_s = None
            lv.camera_drift_ppm, lv.host_clock_drift_ppm, lv.time_correction_s = None, None, None
            lv.clock_slew_run = False
            lv.attempts += 1
        log.info("run %s: launching FFmpeg: %s", run_id, redacted_command(argv))

        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    **self.platform.child_popen_kwargs())
        except OSError as exc:
            log.error("run %s: cannot launch FFmpeg: %s", run_id, exc)
            return self._end_run(run_id, run_dir, timebase, RunEndReason.LAUNCH_FAILED, None, [str(exc)], 0.0,
                                 drift, steps)
        self.platform.bind_child(proc)
        self.current_proc = proc  # exposed for diagnostics and fault-injection tests
        with self.live.lock:
            self.live.ffmpeg_running = True

        stderr_tail: deque[str] = deque(maxlen=40)
        t_out = threading.Thread(target=self._read_progress, args=(proc, timebase, drift), daemon=True,
                                 name=f"progress-{run_id}")
        t_err = threading.Thread(target=self._read_stderr, args=(proc, stderr_tail, run_id), daemon=True,
                                 name=f"stderr-{run_id}")
        t_out.start()
        t_err.start()

        seglist = SegmentListReader(run_dir / SEGMENT_LIST_NAME)
        known: dict[str, str] = {}               # filename -> segment_id
        pending: list[tuple[SegmentEntry, str]] = []   # (entry, wall time the close was seen)
        reason: RunEndReason | None = None
        clock_flagged = False
        last_persist = mono0
        try:
            while True:
                if proc.poll() is not None:
                    break
                now = self.clock.mono()
                self._register_new_files(run_id, run_dir, known)
                # Closed segments wait until the run's time anchor has been measured: after a
                # (re)connect FFmpeg may flush buffered media and close a segment before its
                # first progress report.
                seen = iso_utc(self.clock.wall())
                pending.extend((e, seen) for e in seglist.read_new())
                with self.live.lock:
                    anchored = timebase.has_anchor
                    drift_suspected = self.live.drift_since_mono is not None
                    newest_t = self.live.last_out_time_s
                # While stream time is suspected of having lost real time, hold closed segments:
                # if confirmed, the break must be recorded before they are sealed. A segment is
                # also held until a progress report *newer than its end* has been checked for
                # lost time: FFmpeg can list a segment (closed by the first packet after a freeze)
                # before reporting the stream time that reveals the freeze.
                if anchored and not drift_suspected:
                    while pending and segment_ready(pending[0][0], newest_t):
                        entry, seen_at = pending.pop(0)
                        self._on_closed(run_id, run_dir, entry, known, timebase, drift, steps, seen_at)

                with self.live.lock:
                    lv = self.live
                    media_seen, last_adv, last_t = lv.media_seen, lv.last_advance_mono, lv.last_out_time_s
                    drift_since, lag, lag0, brk = (lv.drift_since_mono, lv.lag_s, lv.drift_lag_at_start,
                                                   lv.drift_break_stream_s)
                    baseline = lv.lag_baseline_s or 0.0
                if not media_seen and now - mono0 > rec.connect_timeout_s:
                    reason = RunEndReason.CONNECT_TIMEOUT
                    log.error("run %s: no media within %.0fs", run_id, rec.connect_timeout_s)
                    self._stop_ffmpeg(proc, graceful=False)
                    break
                if media_seen and last_adv is not None and now - last_adv > rec.stall_timeout_s:
                    reason = RunEndReason.STALL
                    log.error("run %s: stream time has not advanced for %.1fs (stall)", run_id, now - last_adv)
                    self._stop_ffmpeg(proc, graceful=True, grace=3.0)
                    break
                if (drift_since is not None and now - drift_since >= rec.timeline_drift_persist_s
                        and lag >= lag0 - 0.5 and brk is not None):
                    # Stream time is persistently behind real time and is not catching up: real
                    # time passed that is not in the recording. Record where and how much.
                    reason = RunEndReason.TIMELINE_DISCONTINUITY
                    with self.live.lock:
                        # Measured from reception offsets either side of the break (unbiased by
                        # clock drift); the lag-vs-baseline figure is the fallback.
                        jump = drift.measure_jump(brk)
                    shift = round(jump if jump is not None and jump > 0 else lag - baseline, 3)
                    log.error("run %s: %.1f s of real time missing from the stream after stream time %.2f s",
                              run_id, shift, brk)
                    self.ledger.update_run(run_id, timeline_break_stream_s=brk, timeline_shift_s=shift)
                    with self.live.lock:
                        # Held segments after the break are measured on the shifted timeline; the
                        # shift itself becomes an explicit gap (finaliser), never a drift correction.
                        drift.set_break(brk, shift)
                    self.ledger.add_event("warning", "recorder", "timeline_discontinuity",
                                          f"Camera stream lost about {shift:.1f} s of footage",
                                          {"run_id": run_id, "stream_time_s": brk, "missing_s": shift})
                    self._stop_ffmpeg(proc, graceful=True, grace=3.0)
                    break
                st = self.storage.sample()
                if st.level.value == "critical":
                    reason = RunEndReason.STORAGE_CRITICAL
                    log.error("run %s: storage critical; stopping recording cleanly", run_id)
                    self._stop_ffmpeg(proc, graceful=True)
                    break
                if self.stop_event.is_set():
                    reason = RunEndReason.GRACEFUL_STOP
                    self._stop_ffmpeg(proc, graceful=True)
                    break

                # Wall and monotonic read back to back (``now`` is ~a whole loop iteration old):
                # loop latency must not masquerade as clock drift.
                w_pair, m_pair, spread = read_clock_pair(self.clock)
                steps.update(w_pair, m_pair, spread)
                step = steps.total_s
                with self.live.lock:
                    self.live.clock_step_s = step
                if not clock_flagged and step != 0.0:
                    clock_flagged = True
                    log.error("run %s: system clock changed by %.1fs during recording", run_id, step)
                    self.ledger.mark_run_clock_anomaly(run_id)
                    self.ledger.add_event("warning", "clock", "clock_step",
                                          f"System clock changed by {step:+.1f} s during recording",
                                          {"run_id": run_id, "step_s": step})
                if now - last_persist >= 10.0 and last_t is not None:
                    self.ledger.update_run(run_id, last_stream_time_s=last_t)
                    last_persist = now
                self._tick()
                self.stop_event.wait(self.cfg.service.poll_interval_s)
        finally:
            if proc.poll() is None:
                self._stop_ffmpeg(proc, graceful=False)
            try:
                exit_code: int | None = proc.wait(30)
            except subprocess.TimeoutExpired:
                exit_code = None
                log.critical("run %s: FFmpeg could not be terminated", run_id)
            t_out.join(5)
            t_err.join(5)
            self.current_proc = None
            with self.live.lock:
                self.live.ffmpeg_running = False

        # Final segment list entries (a graceful 'q' lists the last segment) and late files.
        self._register_new_files(run_id, run_dir, known)
        seen = iso_utc(self.clock.wall())
        pending.extend((e, seen) for e in seglist.read_new())
        for entry, seen_at in pending:  # without an anchor, the finaliser falls back to file times (flagged)
            self._on_closed(run_id, run_dir, entry, known, timebase, drift, steps, seen_at)
        if seglist.bad_lines:
            log.error("run %s: unparseable segment list lines: %r", run_id, seglist.bad_lines[:5])

        tail = [redact(x) for x in stderr_tail]
        if reason is None:
            joined = "\n".join(tail).lower()
            if "no space left" in joined:
                reason = RunEndReason.STORAGE_CRITICAL
            elif exit_code == 0:
                reason = RunEndReason.SOURCE_ENDED
            elif exit_code is not None and exit_code < 0:
                reason = RunEndReason.FFMPEG_KILLED
            else:
                reason = RunEndReason.FFMPEG_ERROR
        with self.live.lock:
            media_s = self.live.last_out_time_s or 0.0
        return self._end_run(run_id, run_dir, timebase, reason, exit_code, tail, media_s, drift, steps)

    def _end_run(self, run_id: str, run_dir: Path, timebase: RunTimebase, reason: RunEndReason,
                 exit_code: int | None, tail: list[str], media_s: float,
                 drift: DriftTracker | None = None, steps: ClockStepDetector | None = None) -> RunOutcome:
        ended = iso_utc(self.clock.wall())
        last_line = tail[-1] if tail else ""
        final: TimeKnot | None = None
        with self.live.lock:
            timebase.freeze()
            anchor = timebase.iso_for(0.0)
            if drift is not None and steps is not None and timebase.anchor_mono is not None and media_s > 0:
                # Correction at the run's last reported stream time: where an interrupted
                # (never listed) final segment ends.
                final = drift.knot(media_s, timebase.anchor_mono, steps.drift_s)
        # Integration with the tamper-evident ledger: the run's final drift figures are persisted
        # BEFORE the run is marked ended, because an ended run never changes afterwards (chain rule;
        # a later update would be reported as an alteration).
        if final is not None and drift is not None and steps is not None:
            self._persist_drift(run_id, final, drift, steps)
        self.ledger.update_run(run_id, ended_utc=ended, end_reason=str(reason), exit_code=exit_code,
                               end_detail="\n".join(tail[-10:]) or None, last_stream_time_s=media_s or None,
                               anchor_utc=anchor, anchor_method="progress_min" if anchor else None)
        # Any segment of this run that FFmpeg never listed as closed was interrupted. Its stream
        # start is the previous segment's stream end in this run.
        segs = self.ledger.run_segments(run_id)
        prev_end: float | None = 0.0
        for s in segs:
            if s["state"] == str(SegmentState.ACTIVE):
                cols: dict[str, object] = {"stream_start_s": prev_end}
                if final is not None and drift is not None:
                    with self.live.lock:
                        c0 = drift.correction_at(prev_end) if prev_end is not None else final.correction_s
                    cols.update(time_correction_start_s=c0, time_correction_end_s=final.correction_s,
                                time_correction_stream_s=final.stream_s,
                                time_correction_limited=int(final.limited),
                                time_correction_detail=json.dumps(final.detail(), sort_keys=True))
                self.ledger.update_segment(s["segment_id"], **cols)
                self.finalizer.submit(SegmentJob(s["segment_id"], "interrupted"))
                prev_end = None  # nothing after an interrupted segment is contiguous
            else:
                prev_end = s["stream_end_s"]
        # During a long outage every retry is a run with no media; the open gap's attempt
        # counter records those, so they do not each need an event (days of retries).
        if media_s > 0 or segs or self.ledger.open_gap(self.cfg.camera.id) is None:
            level = "info" if reason is RunEndReason.GRACEFUL_STOP else "warning"
            self.ledger.add_event(level, "recorder", f"run_end_{reason}", f"Recording run ended: {reason}",
                                  {"run_id": run_id, "exit_code": exit_code, "last_message": last_line,
                                   "media_seconds": media_s})
        self.finalizer.submit(GapJob(run_id, str(reason), {"exit_code": exit_code, "last_message": last_line}))
        if not segs:
            self._remove_empty_run_dir(run_dir)
        self._open_run = None
        with self.live.lock:
            self.live.last_end_reason = str(reason)
            self.live.recent_run_ends.append((self.clock.mono(), media_s, str(reason)))
        return RunOutcome(reason, exit_code, media_s)

    def _abort_open_run(self) -> None:
        """After an internal error: kill FFmpeg and close the run's books (best effort)."""
        run_id, proc = self._open_run, self.current_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(10)
            except Exception:
                log.exception("could not kill FFmpeg after internal error")
        self.current_proc = None
        with self.live.lock:
            self.live.ffmpeg_running = False
        if run_id is None:
            return
        try:
            run = self.ledger.get_run(run_id)
            if run is not None and run["ended_utc"] is None:
                self.ledger.update_run(run_id, ended_utc=iso_utc(self.clock.wall()),
                                       end_reason=str(RunEndReason.INTERNAL_ERROR))
                prev_end: float | None = 0.0
                for s in self.ledger.run_segments(run_id):
                    if s["state"] == str(SegmentState.ACTIVE):
                        self.ledger.update_segment(s["segment_id"], stream_start_s=prev_end)
                        self.finalizer.submit(SegmentJob(s["segment_id"], "interrupted"))
                        prev_end = None
                    else:
                        prev_end = s["stream_end_s"]
                self.finalizer.submit(GapJob(run_id, str(RunEndReason.INTERNAL_ERROR), {}))
                if not self.ledger.run_segments(run_id):
                    self._remove_empty_run_dir(self._open_run_dir)
        except Exception:
            log.exception("could not close run %s after internal error; startup recovery will", run_id)
        self._open_run = None

    # ------------------------------------------------------------------ helpers
    def _remove_empty_run_dir(self, run_dir: Path) -> None:
        """A failed connection attempt leaves an empty directory; days of retries would leave
        thousands. Only directories containing no media at all are removed; the run stays in
        the ledger."""
        try:
            entries = list(run_dir.iterdir())
            if any(p.name != SEGMENT_LIST_NAME for p in entries):
                return
            for p in entries:
                if p.stat().st_size == 0:
                    p.unlink()
                else:
                    return
            run_dir.rmdir()
        except OSError:
            log.debug("could not tidy %s", run_dir, exc_info=True)

    def _register_new_files(self, run_id: str, run_dir: Path, known: dict[str, str]) -> None:
        try:
            names = sorted(p.name for p in run_dir.iterdir())
        except FileNotFoundError:
            return
        for name in names:
            m = _SEG_RE.match(name)
            if not m or name in known:
                continue
            rel = str((run_dir / name).relative_to(self.cfg.spool_path)).replace("\\", "/")
            existing = self.ledger.segment_by_path(rel)
            if existing is not None:
                known[name] = existing["segment_id"]
                continue
            known[name] = self.ledger.insert_segment(
                camera_id=self.cfg.camera.id, run_id=run_id, seq=int(m.group(1)), rel_path=rel,
                observed_first_utc=iso_utc(self.clock.wall()))

    def _on_closed(self, run_id: str, run_dir: Path, entry: SegmentEntry, known: dict[str, str],
                   timebase: RunTimebase, drift: DriftTracker, steps: ClockStepDetector,
                   observed: str | None = None) -> None:
        if entry.filename not in known:
            self._register_new_files(run_id, run_dir, known)
        seg_id = known.get(entry.filename)
        if seg_id is None:
            log.error("run %s: segment list names unknown file %r", run_id, entry.filename)
            self.ledger.add_event("error", "recorder", "segment_list_unknown_file",
                                  "FFmpeg listed a file that does not exist", {"file": entry.filename})
            return
        observed = observed or iso_utc(self.clock.wall())   # when the close was seen, not processed
        with self.live.lock:
            first = not timebase.frozen and timebase.has_anchor
            if first:
                timebase.freeze()
            anchor = timebase.iso_for(0.0)
        if first and anchor:
            self.ledger.update_run(run_id, anchor_utc=anchor, anchor_method="progress_min")
        seg = self.ledger.get_segment(seg_id)
        if seg is None or seg["state"] != str(SegmentState.ACTIVE):
            return
        knot: TimeKnot | None = None
        c0 = 0.0
        with self.live.lock:
            if timebase.anchor_mono is not None:
                # Fix this boundary's correction toward measured reception time (docs/03 §4.2).
                # The start boundary is the previous segment's end knot, so segments stay contiguous.
                c0 = drift.correction_at(entry.start_s)
                knot = drift.knot(entry.end_s, timebase.anchor_mono, steps.drift_s)
            c1 = knot.correction_s if knot is not None else c0
            start_iso, end_iso = timebase.iso_for(entry.start_s, c0), timebase.iso_for(entry.end_s, c1)
        cols: dict[str, object] = {}
        if knot is not None:
            cols = dict(time_correction_start_s=c0, time_correction_end_s=c1, time_correction_stream_s=entry.end_s,
                        time_correction_limited=int(knot.limited),
                        time_correction_detail=json.dumps(knot.detail(), sort_keys=True))
        self.ledger.transition(
            seg_id, SegmentState.CLOSED, stream_start_s=entry.start_s, stream_end_s=entry.end_s,
            start_utc=start_iso, end_utc=end_iso, time_source="run_anchor" if start_iso else None,
            observed_closed_utc=observed, **cols)
        if knot is not None:
            self._persist_drift(run_id, knot, drift, steps)
        self.finalizer.submit(SegmentJob(seg_id, "closed"))

    def _persist_drift(self, run_id: str, knot: TimeKnot, drift: DriftTracker, steps: ClockStepDetector) -> None:
        """Record the run's measured drift and current correction (ledger + live status)."""
        with self.live.lock:
            cam_ppm, span = drift.camera_drift_ppm()
            host_ppm = steps.drift_ppm
            limited = drift.limited_count
            self.live.camera_drift_ppm, self.live.host_clock_drift_ppm = cam_ppm, host_ppm
            self.live.time_correction_s = knot.correction_s
        self.ledger.update_run(run_id, time_correction_s=round(knot.correction_s, 6),
                               camera_drift_ppm=None if cam_ppm is None else round(cam_ppm, 3),
                               host_clock_drift_ppm=None if host_ppm is None else round(host_ppm, 3),
                               drift_measured_over_s=None if span is None else round(span, 3),
                               time_correction_limited_count=limited)
        if knot.limited and limited == 1:
            log.warning("run %s: measured reception time moved faster than %.0f ppm at stream time %.1f s "
                        "(measured %s s, applied %.3f s); not followed", run_id, self.cfg.recording.max_clock_drift_ppm,
                        knot.stream_s, knot.measured_s, knot.correction_s)
            self.ledger.add_event("warning", "clock", "time_correction_limited",
                                  "Recording time correction was rate-limited: measured timing changed faster "
                                  "than clock drift can explain", {"run_id": run_id, **knot.detail()})
        with self.live.lock:
            host_limited = drift.host_limited_count
        if knot.host_limited and host_limited == 1:
            # The PC wall clock moved against the monotonic clock faster than drift (fast time-service
            # slew or a step below clock_step_tolerance_s): not followed beyond the drift limit and
            # treated like a clock step — the run's segments are flagged clock_anomaly (T-001).
            log.error("run %s: system clock slewed faster than %.0f ppm (host drift %.3f s, applied %.3f s)",
                      run_id, self.cfg.recording.max_clock_drift_ppm, knot.host_s, knot.host_applied_s)
            self.ledger.mark_run_clock_anomaly(run_id)
            self.ledger.add_event("warning", "clock", "clock_slew",
                                  "System clock changed faster than normal drift during recording",
                                  {"run_id": run_id, **knot.detail()})
            with self.live.lock:
                self.live.clock_slew_run = True                               # surfaces T-001 in status

    def _read_progress(self, proc: subprocess.Popen[bytes], timebase: RunTimebase, drift: DriftTracker) -> None:
        parser = ProgressParser()
        tol = self.cfg.recording.timeline_drift_tolerance_s
        assert proc.stdout is not None
        for raw in proc.stdout:
            snap = parser.feed(raw.decode("utf-8", "replace"))
            if snap is None or snap.out_time_s is None:
                continue
            mono = self.clock.mono()
            with self.live.lock:
                lv = self.live
                prev = lv.last_out_time_s
                if prev is None or snap.out_time_s > prev:
                    lv.last_out_time_s = snap.out_time_s
                    lv.last_advance_mono = mono
                    if snap.out_time_s > 0:
                        lv.media_seen = True
                        lv.last_media_mono_any = mono
                        timebase.observe(snap.out_time_s, mono)
                        drift.observe(snap.out_time_s, mono)
                    if timebase.anchor_mono is not None:
                        # How far stream time is behind elapsed real time since the anchor,
                        # relative to a baseline that follows slow clock drift (camera vs host,
                        # or an undisciplined monotonic clock). Only a sudden excess means real
                        # time passed that is missing from the stream.
                        lag = (mono - snap.out_time_s) - timebase.anchor_mono
                        lv.lag_s = lag
                        if lv.lag_baseline_s is None:
                            lv.lag_baseline_s = lag
                        excess = lag - lv.lag_baseline_s
                        if excess <= tol:
                            lv.lag_baseline_s += 0.05 * (lag - lv.lag_baseline_s)
                            lv.last_ok_out_time_s = snap.out_time_s
                            lv.drift_since_mono = None
                        elif lv.drift_since_mono is None:
                            lv.drift_since_mono = mono
                            lv.drift_lag_at_start = lag
                            lv.drift_break_stream_s = lv.last_ok_out_time_s
                lv.fps = snap.fps

    def _read_stderr(self, proc: subprocess.Popen[bytes], tail: deque[str], run_id: str) -> None:
        assert proc.stderr is not None
        count = 0
        for raw in proc.stderr:
            line = redact(raw.decode("utf-8", "replace").rstrip())
            if not line:
                continue
            tail.append(line)
            count += 1
            # Rate-limited: a mid-GOP join or a damaged stream can emit a line per frame. The
            # last 40 lines are always kept and stored with the run's end record.
            if count <= 15 or count % 500 == 0:
                log.warning("ffmpeg[%s]: %s%s", run_id, line, f" (message #{count})" if count > 15 else "")

    def _stop_ffmpeg(self, proc: subprocess.Popen[bytes], graceful: bool, grace: float | None = None) -> None:
        if proc.poll() is not None:
            return
        if graceful:
            try:
                assert proc.stdin is not None
                proc.stdin.write(b"q")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(grace if grace is not None else self.cfg.recording.stop_grace_s)
                return
            except subprocess.TimeoutExpired:
                log.warning("FFmpeg did not stop gracefully; killing")
        proc.kill()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            log.error("FFmpeg did not exit after kill")
