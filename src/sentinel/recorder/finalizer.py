"""Segment sealing and gap bookkeeping, serialised through one FIFO queue.

Work items are processed strictly in submission order. A run's segments are sealed *before*
the gap that follows the run is opened, and that gap is opened before any segment of the next
run can close it. Ordering is what prevents an outage from being merged with footage.

Per segment (fast, never waits for content hashing):
    ffprobe validation -> fsync -> file SHA-256 -> read-only seal -> ONE ledger transaction
    (state, metadata, internal gap rows, closing the preceding outage gap, events)
Content hashing (decoded frames) is handed to the low-priority IntegrityWorker afterwards.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..clock import iso_utc
from ..config import AppConfig
from ..db import EventSpec, InternalGap, Ledger
from ..hashing import sha256_file
from ..media.probe import ProbeResult, first_video_pts, probe_segment
from ..models import TERMINAL_STATES, RunEndReason, SegmentState
from ..platform.base import Platform
from ..storage.monitor import StorageMonitor
from .timebase import interpolate_correction

log = logging.getLogger(__name__)

MAX_FINALIZE_ATTEMPTS = 5


@dataclass(frozen=True)
class SegmentJob:
    segment_id: str
    kind: str            # "closed" (FFmpeg listed it) | "interrupted" (writer ended abnormally)


@dataclass(frozen=True)
class GapJob:
    run_id: str
    reason: str
    detail: dict[str, Any]


_STOP = object()


class Finalizer:
    def __init__(self, cfg: AppConfig, ledger: Ledger, platform: Platform, storage: StorageMonitor,
                 ffmpeg_version: str | None, on_sealed: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.platform = platform
        self.storage = storage
        self.ffmpeg_version = ffmpeg_version
        self.on_sealed = on_sealed
        self._q: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._abort = threading.Event()
        self._queued: set[str] = set()
        self._qlock = threading.Lock()
        self.last_good_seal_mono: float | None = None   # FINALIZED or playable INTERRUPTED only
        self.last_corrupt_mono: float | None = None
        self.recent_errors: deque[tuple[float, str]] = deque(maxlen=20)
        self.sealed_count = 0

    # ------------------------------------------------------------------ queue
    def submit(self, job: SegmentJob | GapJob) -> None:
        if isinstance(job, SegmentJob):
            with self._qlock:
                if job.segment_id in self._queued:
                    return
                self._queued.add(job.segment_id)
        self._q.put(job)

    def is_queued(self, segment_id: str) -> bool:
        with self._qlock:
            return segment_id in self._queued

    def pending(self) -> int:
        return self._q.qsize()

    def start(self) -> None:
        self._abort.clear()
        self._thread = threading.Thread(target=self._loop, name="finalizer", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, drain_timeout: float = 60.0, job_timeout: float = 330.0) -> bool:
        """Drain queued work, then stop. If draining takes too long, finish only the job in
        progress (never abandon a half-sealed segment) and leave the rest to startup recovery.
        Returns True if the queue was fully drained."""
        if self._thread is None:
            return True
        self._q.put(_STOP)
        self._thread.join(drain_timeout)
        if not self._thread.is_alive():
            return True
        self._abort.set()
        self._thread.join(job_timeout)
        return False

    def _loop(self) -> None:
        while not self._abort.is_set():
            job = self._q.get()
            if job is _STOP:
                return
            self._run_job(job)

    def _run_job(self, job: Any) -> None:
        try:
            if isinstance(job, SegmentJob):
                self.process_segment(job.segment_id, job.kind)
            elif isinstance(job, GapJob):
                self.process_gap(job)
        except Exception as exc:  # keep the worker alive; surface the failure loudly
            msg = f"{type(exc).__name__}: {exc}"
            log.exception("finalizer job failed: %r", job)
            self.recent_errors.append((time.monotonic(), msg))
            try:
                if isinstance(job, SegmentJob):
                    n = self.ledger.record_finalize_failure(job.segment_id, msg)
                    if n >= MAX_FINALIZE_ATTEMPTS:
                        log.critical("segment %s could not be sealed after %d attempts", job.segment_id, n)
                self.ledger.add_event("error", "recorder", "finalize_failed",
                                      f"Could not finalise recording item: {msg}", {"job": repr(job)})
            except Exception:
                log.exception("could not record finalize failure")
        finally:
            if isinstance(job, SegmentJob):
                with self._qlock:
                    self._queued.discard(job.segment_id)

    def has_recent_errors(self, window_s: float = 300.0) -> bool:
        cutoff = time.monotonic() - window_s
        return any(t >= cutoff for t, _ in self.recent_errors)

    # ------------------------------------------------------------------ segments
    def process_segment(self, segment_id: str, kind: str) -> SegmentState | None:
        seg = self.ledger.get_segment(segment_id)
        if seg is None:
            raise KeyError(segment_id)
        if SegmentState(seg["state"]) in TERMINAL_STATES:
            return SegmentState(seg["state"])
        run = self.ledger.get_run(seg["run_id"])
        path = self.cfg.spool_path / seg["rel_path"]
        rec = self.cfg.recording
        interrupted = kind == "interrupted"

        if not path.exists():
            self.ledger.seal(segment_id, SegmentState.CORRUPT,
                             {"warnings": json.dumps(["file_missing"]),
                              "validation_detail": json.dumps({"reason": "file_missing"}),
                              "interrupted": int(interrupted), "sealed_utc": iso_utc(time.time())},
                             [InternalGap(seg["start_utc"], seg["end_utc"], None, "segment_file_missing")], None,
                             [EventSpec("error", "recorder", "segment_file_missing",
                                        f"Recording file is missing: {seg['rel_path']}", {"segment_id": segment_id})])
            self.last_corrupt_mono = time.monotonic()
            return SegmentState.CORRUPT

        res = probe_segment(path, self.cfg.tools.ffprobe, self.cfg.tools.ffmpeg, rec.validation,
                            rec.max_internal_gap_s)
        warnings: list[str] = list(res.warnings)

        # The writer has finished with this file (listed by FFmpeg, or FFmpeg has exited).
        self.platform.fsync_file(path)
        sha, size = sha256_file(path)
        self.platform.seal_read_only(path)
        self.platform.fsync_dir(path.parent)

        start_utc, end_utc, time_source, gaps, time_cols = self._timestamps(seg, run, res, interrupted, warnings)
        if res.playable:
            new_state = SegmentState.INTERRUPTED if interrupted else SegmentState.FINALIZED
        else:
            new_state = SegmentState.CORRUPT

        events: list[EventSpec] = []
        if new_state is SegmentState.CORRUPT:
            dur = None
            if start_utc and end_utc:
                from ..clock import parse_iso_utc
                dur = parse_iso_utc(end_utc) - parse_iso_utc(start_utc)
            gaps = [InternalGap(start_utc, end_utc, dur, "corrupt_segment", {"probe_reason": res.reason})]
            events.append(EventSpec("error", "recorder", "segment_corrupt",
                                    f"Recording segment is unreadable: {seg['rel_path']} ({res.reason})",
                                    {"segment_id": segment_id}))
        elif new_state is SegmentState.INTERRUPTED:
            events.append(EventSpec("warning", "recorder", "segment_interrupted",
                                    f"Recording segment ended abnormally: {seg['rel_path']}",
                                    {"segment_id": segment_id}))

        closed = self.ledger.seal(
            segment_id, new_state,
            dict(sha256=sha, size_bytes=size, duration_s=res.duration_s,
                 pix_fmt=res.pix_fmt, video_codec=res.codec, width=res.width, height=res.height,
                 packet_count=res.packet_count, keyframe_count=res.keyframe_count, max_pts_gap_s=res.max_pts_gap_s,
                 start_utc=start_utc, end_utc=end_utc, time_source=time_source, interrupted=int(interrupted),
                 warnings=json.dumps(sorted(set(warnings))), validation_detail=json.dumps(res.detail()),
                 sealed_utc=iso_utc(time.time()), last_finalize_error=None, **time_cols),
            gaps, start_utc, events)
        if closed is not None:
            log.warning("recording gap closed: %s -> %s (%.1f s, reason %s)", closed["start_utc"],
                        closed["end_utc"], closed["duration_s"] or -1, closed["reason"])

        if new_state is SegmentState.CORRUPT:
            self.last_corrupt_mono = time.monotonic()
        else:
            self.last_good_seal_mono = time.monotonic()
            self.storage.record_segment(size, res.duration_s)
        self.sealed_count += 1
        log.info("sealed %s state=%s dur=%.2fs size=%d sha256=%s", seg["rel_path"], new_state,
                 res.duration_s or 0, size, sha[:16])
        if self.on_sealed is not None and res.playable:
            self.on_sealed(segment_id)
        return new_state

    def _measured_lead_in(self, seg: Any, res: ProbeResult) -> float | None:
        """Stream time between segment 0's listed start and its first footage, from the PTS
        offset calibrated on segment 1 of the same run (``-reset_timestamps 0``: one constant
        offset per run). None if segment 1 or either PTS is unavailable (e.g. the run ended)."""
        if res.first_pts_s is None or seg["stream_start_s"] is None:
            return None
        rows = self.ledger.query("SELECT rel_path, stream_start_s FROM segments WHERE run_id=? AND seq=1",
                                 (seg["run_id"],))
        if not rows:
            return None
        path1 = self.cfg.spool_path / rows[0]["rel_path"]
        start1 = rows[0]["stream_start_s"] if rows[0]["stream_start_s"] is not None else seg["stream_end_s"]
        if not path1.exists() or path1.stat().st_size == 0 or start1 is None:
            return None
        pts1 = first_video_pts(path1, self.cfg.tools.ffprobe)
        if pts1 is None:
            return None
        offset = pts1 - start1
        return res.first_pts_s - offset - seg["stream_start_s"]

    def _corrections(self, seg: Any, run: Any) -> tuple[float | None, float, float | None, float, bool]:
        """Drift-correction knots for this segment: (k0 stream s, c0, k1 stream s, c1, held).

        Runs recorded before reception tracking (``time_method`` NULL) get no correction (the
        legacy single-anchor times). In a tracked run, a segment whose boundary was never
        measured (closed during startup recovery after a crash, or after an internal error)
        holds the last correction measured for that run; ``held`` flags this."""
        k0, k1 = seg["stream_start_s"], seg["time_correction_stream_s"]
        c0, c1 = seg["time_correction_start_s"], seg["time_correction_end_s"]
        if run is None or run["time_method"] is None:
            return k0, 0.0, k1, 0.0, False
        held = False
        if c0 is None:
            held = True
            prev = self.ledger.query(
                "SELECT time_correction_end_s FROM segments WHERE run_id=? AND seq<? "
                "AND time_correction_end_s IS NOT NULL ORDER BY seq DESC LIMIT 1", (seg["run_id"], seg["seq"]))
            c0 = prev[0][0] if prev else (run["time_correction_s"] or 0.0)
        if c1 is None:
            held, c1, k1 = True, c0, None
        return k0, float(c0), k1, float(c1), held

    def _timestamps(self, seg: Any, run: Any, res: ProbeResult, interrupted: bool, warnings: list[str]
                    ) -> tuple[str | None, str | None, str | None, list[InternalGap], dict[str, Any]]:
        """Derive (start_utc, end_utc, time_source, internal gaps, time-correction columns).
        See docs/03 §4."""
        rec = self.cfg.recording
        dur = res.duration_s or 0.0
        anchor = run["anchor_utc"] if run is not None else None
        gaps: list[InternalGap] = []
        s0 = s1 = None
        source = "run_anchor"
        boundary: tuple[float, float] | None = None

        if not interrupted and seg["stream_end_s"] is not None:
            s0, s1 = seg["stream_start_s"], seg["stream_end_s"]
            listed = s1 - s0
            shortfall = listed - dur                 # stream time the list claims but no video covers
            gop = (dur / res.keyframe_count) if res.keyframe_count else None
            lead_in_limit = (1.5 * gop + 0.5) if gop else 10.0
            assumed_lead_in = False
            if seg["seq"] == 0 and 0 < shortfall <= lead_in_limit:
                # FFmpeg discards packets before a run's first keyframe, but the segment list still
                # starts segment 0 at stream time 0, so footage may begin later (a lead-in that is
                # part of the preceding gap). A timestamp jump at segment 0's end looks the same,
                # so the lead-in is measured, not assumed: segment 1's first PTS calibrates the
                # constant PTS offset of this run's files.
                lead = self._measured_lead_in(seg, res)
                if lead is None:
                    s0, assumed_lead_in = s1 - dur, True
                    warnings.append("lead_in_unverified")
                else:
                    s0 = seg["stream_start_s"] + max(0.0, min(lead, shortfall))
                    shortfall = (s1 - s0) - dur          # what the measured lead-in does not explain
            if assumed_lead_in:
                pass
            elif shortfall > rec.max_internal_gap_s:
                # A timestamp jump fell on the segment boundary: the list's end is the next
                # segment's start. Footage ends earlier; the hole is recorded explicitly.
                boundary = (s0 + dur, s1)
                s1 = s0 + dur
                warnings.append("timestamp_gap_at_segment_end")
            elif abs(shortfall) > max(1.0, 0.05 * listed):
                warnings.append("duration_mismatch")
        elif interrupted:
            ended_by_crash = run is not None and run["end_reason"] == str(RunEndReason.SERVICE_INTERRUPTED)
            if seg["seq"] > 0 and seg["stream_start_s"] is not None:
                s0 = seg["stream_start_s"]           # previous segment's listed end in this run: exact
                s1 = s0 + dur
            elif run is not None and run["last_stream_time_s"] and not ended_by_crash:
                s1 = run["last_stream_time_s"]       # final progress report of the run (<= ~1 s stale)
                s0 = s1 - dur
                source = "run_anchor_end"

        if anchor is None or s0 is None or s1 is None:
            # No usable stream timing (no anchor, or the service died and progress was not
            # persisted recently). The file's last write time is the best remaining evidence.
            mtime = (self.cfg.spool_path / seg["rel_path"]).stat().st_mtime
            warnings.append("time_from_file_mtime")
            return iso_utc(mtime - dur), iso_utc(mtime), "file_mtime", gaps, {}

        from ..clock import add_seconds_iso

        # Drift correction toward measured reception time, fixed at the segment's boundaries
        # (docs/03 §4.2); the start boundary equals the previous segment's end boundary.
        k0, c0, k1, c1, held = self._corrections(seg, run)
        time_cols: dict[str, Any] = {}
        if run is not None and run["time_method"] is not None:
            time_cols = {"time_correction_start_s": c0, "time_correction_end_s": c1}
            if held:
                warnings.append("time_correction_held")
        if seg["time_correction_limited"]:
            warnings.append("time_correction_limited")

        def to_utc(stream_s: float) -> str:
            return add_seconds_iso(anchor, stream_s + interpolate_correction(stream_s, k0, c0, k1, c1))

        # Real time the source lost but FFmpeg removed from the stream timeline (supervisor
        # measured it as stream time falling behind monotonic time): footage after the break
        # point really happened `shift` seconds later than the stream timeline says.
        brk, shift = (run["timeline_break_stream_s"], run["timeline_shift_s"]) if run is not None else (None, None)
        start_shift = end_shift = 0.0
        if brk is not None and shift:
            if s0 >= brk:
                start_shift = end_shift = shift
            elif s1 > brk:
                end_shift = shift
                gaps.append(InternalGap(to_utc(brk), add_seconds_iso(to_utc(brk), shift), shift,
                                        "source_discontinuity_absorbed",
                                        {"stream_time_s": brk, "note": "measured as stream time falling "
                                                                       "behind real time"}))
                warnings.append("timeline_discontinuity")
        start_utc = add_seconds_iso(to_utc(s0), start_shift)
        end_utc = add_seconds_iso(to_utc(s1), end_shift)

        for before, after in res.pts_gaps:
            gaps.append(InternalGap(add_seconds_iso(start_utc, before), add_seconds_iso(start_utc, after),
                                    after - before, "timestamp_discontinuity",
                                    {"pts_offset_before_s": before, "pts_offset_after_s": after}))
        if boundary is not None:
            gaps.append(InternalGap(add_seconds_iso(to_utc(boundary[0]), end_shift),
                                    add_seconds_iso(to_utc(boundary[1]), end_shift),
                                    boundary[1] - boundary[0], "timestamp_discontinuity_at_boundary"))
        return start_utc, end_utc, source, gaps, time_cols

    # ------------------------------------------------------------------ gaps
    def process_gap(self, job: GapJob) -> None:
        run = self.ledger.get_run(job.run_id)
        if run is None or run["gap_recorded"]:
            return
        cam = run["camera_id"]
        segs = [s for s in self.ledger.run_segments(job.run_id) if s["end_utc"]]
        if segs:
            last = segs[-1]                          # highest seq: the run's final footage
            start, basis, prev = last["end_utc"], "segment_end", last["segment_id"]
        else:
            latest = self.ledger.latest_recorded_segment(cam)
            if latest is not None:
                start, basis, prev = latest["end_utc"], "segment_end", latest["segment_id"]
            else:
                start, basis, prev = run["launched_utc"], "run_launch", None
        gap_id = self.ledger.begin_gap(cam, start, job.reason, prev, job.run_id, basis, job.detail)
        log.warning("recording gap open (gap %d) from %s, reason %s", gap_id, start, job.reason)
