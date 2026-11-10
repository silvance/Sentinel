"""Per-run wall-clock anchoring, reception-time drift tracking and clock-step detection
(docs/03 §4).

Three clocks are involved:

* the camera's clock, which stamps the stream (stream time ``t``, from FFmpeg ``-progress``);
* the host monotonic clock ``m`` (never steps; on Windows it is *not* disciplined by time
  synchronisation, so it drifts slowly against UTC);
* the host wall clock ``w`` (UTC, disciplined by the OS time service; may step or slew).

``RunTimebase`` fixes one anchor per run: the monotonic time at which stream time 0 was
received. ``DriftTracker`` estimates, at every segment boundary, how far the anchor-plus-
stream-time estimate has drifted from actual reception (camera clock rate vs host monotonic),
and ``ClockStepDetector`` how far the monotonic clock has drifted from the wall clock (steps
excluded). Both components are *rate-limited* to what genuine clock drift can explain, fixed at
segment boundaries ("knots") and interpolated linearly in between, so consecutive segments stay
contiguous and nothing that is not drift — a wall-clock step or fast slew, real time FFmpeg
removed from the stream, a disk/antivirus/CPU stall delaying progress reports — moves a
boundary by more than ``NON_DRIFT_ALLOWANCE_S``. Such events are flagged instead.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..clock import iso_utc

TIME_METHOD = "reception_tracking_v1"
# Change of a correction component allowed per knot on top of max_clock_drift_ppm x interval:
# rounding/measurement noise only. A stall or step can never move a boundary by more than this.
NON_DRIFT_ALLOWANCE_S = 0.02
# Reception-offset estimator: minimum of (m - t) per BUCKET_S of stream time (the lower envelope:
# a report can be late, never early), then a Theil-Sen line (median of pairwise slopes, robust
# to ~29 % outlier buckets) through the bucket minima of the last FIT_HISTORY_S. A stall shorter
# than a bucket does not change that bucket's minimum at all; a longer one raises a few points
# the median ignores.
BUCKET_S = 30.0
FIT_HISTORY_S = 3600.0
MIN_FIT_POINTS = 3
# Drift rates (ppm) are only reported once measured over at least this much time; over shorter
# spans measurement noise of a few tens of milliseconds dominates.
MIN_PPM_SPAN_S = 60.0
# Wall/monotonic sample pairs read further apart than this are not used for drift (the reading
# thread was delayed between the two reads).
MAX_PAIR_SPREAD_S = 0.002
HOST_MEDIAN_SAMPLES = 15


def read_clock_pair(clock: Any, tries: int = 3) -> tuple[float, float, float]:
    """(wall, mono, spread): wall read between two monotonic reads; mono is their midpoint and
    spread their difference (the uncertainty of the pairing). Retries for a tight pair."""
    best: tuple[float, float, float] | None = None
    for _ in range(max(1, tries)):
        m1 = clock.mono()
        w = clock.wall()
        m2 = clock.mono()
        cand = (w, (m1 + m2) / 2, m2 - m1)
        if best is None or cand[2] < best[2]:
            best = cand
        if cand[2] <= MAX_PAIR_SPREAD_S:
            break
    assert best is not None
    return best


@dataclass
class RunTimebase:
    wall_launch: float
    mono_launch: float
    anchor_mono: float | None = None
    frozen: bool = False

    def observe(self, out_time_s: float, mono_now: float) -> None:
        """Record a progress report: stream time ``out_time_s`` seen at ``mono_now``."""
        if self.frozen or out_time_s is None:
            return
        candidate = mono_now - out_time_s
        if self.anchor_mono is None or candidate < self.anchor_mono:
            self.anchor_mono = candidate

    def freeze(self) -> None:
        """Fix the anchor once the first segment has closed so all segments share one anchor."""
        if self.anchor_mono is not None:
            self.frozen = True

    @property
    def has_anchor(self) -> bool:
        return self.anchor_mono is not None

    @property
    def anchor_epoch(self) -> float | None:
        if self.anchor_mono is None:
            return None
        return self.wall_launch + (self.anchor_mono - self.mono_launch)

    def epoch_for(self, stream_s: float, correction_s: float = 0.0) -> float | None:
        a = self.anchor_epoch
        return None if a is None else a + stream_s + correction_s

    def iso_for(self, stream_s: float, correction_s: float = 0.0) -> str | None:
        e = self.epoch_for(stream_s, correction_s)
        return None if e is None else iso_utc(e)


@dataclass(frozen=True)
class TimeKnot:
    """The correction fixed at one segment boundary (stream time ``stream_s``)."""
    stream_s: float
    correction_s: float              # applied = camera_applied_s + host_applied_s
    measured_s: float | None         # camera_s + host_s (None: no reports to measure from)
    camera_s: float | None           # estimated reception offset vs the run anchor (camera vs host mono)
    host_s: float                    # host wall-vs-monotonic gradual drift since run start (steps excluded)
    limited: bool                    # camera component held back by the drift-rate limit
    reports: int                     # progress reports the estimate used
    camera_applied_s: float = 0.0
    host_applied_s: float = 0.0
    host_limited: bool = False       # wall-vs-monotonic changed faster than drift: clock anomaly

    def detail(self) -> dict[str, Any]:
        r = (lambda v: None if v is None else round(v, 6))
        return {"method": TIME_METHOD, "stream_s": r(self.stream_s), "applied_s": r(self.correction_s),
                "measured_s": r(self.measured_s), "camera_s": r(self.camera_s), "host_s": r(self.host_s),
                "camera_applied_s": r(self.camera_applied_s), "host_applied_s": r(self.host_applied_s),
                "limited": self.limited, "host_limited": self.host_limited, "reports": self.reports}


def interpolate_correction(stream_s: float, k0_s: float | None, c0: float, k1_s: float | None, c1: float) -> float:
    """Linear interpolation of the correction between two knots, held constant outside them."""
    if k0_s is None:
        return c1
    if k1_s is None:
        return c0
    if k1_s <= k0_s:
        return c1
    f = (stream_s - k0_s) / (k1_s - k0_s)
    if f <= 0.0:
        return c0
    if f >= 1.0:
        return c1          # exactly the knot value, so the next segment's start matches bit for bit
    return c0 + (c1 - c0) * f


def theil_sen(points: list[tuple[float, float]], min_dx: float) -> tuple[float, float] | None:
    """Robust line (slope, intercept) through ``points``; None with fewer than MIN_FIT_POINTS.
    Pairs closer than ``min_dx`` in x are skipped (noise amplification)."""
    if len(points) < MIN_FIT_POINTS:
        return None
    slopes = [(y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(points) for (x2, y2) in points[i + 1:]
              if x2 - x1 >= min_dx]
    slope = statistics.median(slopes) if slopes else 0.0
    intercept = statistics.median(y - slope * x for x, y in points)
    return slope, intercept


def _clamp(prev: float, target: float, allow: float) -> tuple[float, bool]:
    delta = target - prev
    if delta > allow:
        return prev + allow, True
    if delta < -allow:
        return prev - allow, True
    return target, False


@dataclass
class DriftTracker:
    """Rate-limited per-segment correction of the run anchor toward actual reception time.

    ``observe`` is fed every advancing progress report ``(t, m)``. Because a packet with stream
    time ``t`` cannot be reported before it was received, ``m - t`` is an upper bound of the
    reception offset. The camera component at a boundary is a robust line through the per-bucket
    minima of ``m - t`` (see ``BUCKET_S``), evaluated at the boundary, minus the run anchor. The
    host component is the gradual wall-vs-monotonic drift (steps excluded) from
    ``ClockStepDetector.drift_s``.

    Each component may change between consecutive knots by at most
    ``max_drift_ppm * Δt + NON_DRIFT_ALLOWANCE_S``. Anything faster is not drift and is not
    followed: the knot is flagged (``limited`` for the camera component — a segment warning;
    ``host_limited`` for the PC clock — a clock anomaly). If the difference persists it is
    caught up at the limit rate only.
    """
    max_drift_ppm: float
    window_before_s: float = 8.0      # fallback estimator while there are too few buckets
    window_after_s: float = 2.0
    history_s: float = FIT_HISTORY_S
    _reports: deque = field(default_factory=lambda: deque(maxlen=16384))   # (t, m)
    knots: list[TimeKnot] = field(default_factory=list)
    limited_count: int = 0
    host_limited_count: int = 0
    _break: tuple[float, float] | None = None                              # (stream_s, shift_s)

    def observe(self, out_time_s: float, mono: float) -> None:
        self._reports.append((out_time_s, mono))
        cutoff = out_time_s - self.history_s
        while self._reports and self._reports[0][0] < cutoff:
            self._reports.popleft()

    def set_break(self, stream_s: float, shift_s: float) -> None:
        """Real time lost at ``stream_s`` (absorbed discontinuity, measured by the supervisor).
        Reports after it are compared on the corrected timeline; the shift itself is applied
        by the finaliser as an explicit gap, never smeared into the drift correction."""
        self._break = (stream_s, shift_s)

    def _value(self, t: float, m: float) -> float:
        v = m - t
        if self._break is not None and t > self._break[0]:
            v -= self._break[1]
        return v

    def measure_jump(self, break_stream_s: float) -> float | None:
        """Real time lost at ``break_stream_s``: min(m - t) over reports after the break minus
        min(m - t) over the 4 s before it. Unlike a slowly-following baseline this is not
        biased by clock drift (at 1 % drift an exponential baseline lags by ~0.2-0.3 s).
        None if either side has no reports."""
        pre = post = None
        lo = break_stream_s - 4.0      # short: under drift a longer window biases the minimum
        for t, m in self._reports:
            v = m - t
            if lo <= t <= break_stream_s:
                pre = v if pre is None or v < pre else pre
            elif t > break_stream_s:
                post = v if post is None or v < post else post
        if pre is None or post is None:
            return None
        return post - pre

    def offset_at(self, stream_s: float) -> tuple[float | None, int]:
        """min(m - t) over reports within the short window around ``stream_s``; (None, 0) if none."""
        lo, hi = stream_s - self.window_before_s, stream_s + self.window_after_s
        best: float | None = None
        n = 0
        for t, m in self._reports:
            if lo <= t <= hi:
                v = self._value(t, m)
                n += 1
                if best is None or v < best:
                    best = v
        return best, n

    @staticmethod
    def bucket_width(upto_s: float) -> float:
        """BUCKET_S once a run is long enough; shorter (>= 2 s, ~10 buckets) early in a run so the
        same robust fit is used from the first boundaries (no switch between estimators)."""
        return min(BUCKET_S, max(2.0, upto_s / 10.0))

    def bucket_minima(self, upto_s: float) -> tuple[list[tuple[float, float]], int]:
        """(t at the minimum, min(m - t)) per bucket of stream time, over reports up to
        ``upto_s + window_after_s``; and the number of reports used."""
        hi = upto_s + self.window_after_s
        width = self.bucket_width(upto_s)
        best: dict[int, tuple[float, float]] = {}
        n = 0
        for t, m in self._reports:
            if t > hi or t < upto_s - self.history_s:
                continue
            n += 1
            v = self._value(t, m)
            b = int(t // width)
            if b not in best or v < best[b][1]:
                best[b] = (t, v)
        return [best[k] for k in sorted(best)], n

    def estimate_offset(self, stream_s: float) -> tuple[float | None, int]:
        """Reception offset (m - t) at ``stream_s`` from the robust fit; falls back to the
        short-window minimum while fewer than MIN_FIT_POINTS buckets exist."""
        points, n = self.bucket_minima(stream_s)
        fit = theil_sen(points, self.bucket_width(stream_s) / 2)
        if fit is None:
            return self.offset_at(stream_s)
        slope, intercept = fit
        return intercept + slope * stream_s, n

    @property
    def last(self) -> TimeKnot | None:
        return self.knots[-1] if self.knots else None

    def correction_at(self, stream_s: float) -> float:
        """Applied correction at ``stream_s``: 0 at stream time 0, interpolated between knots,
        held at the last knot's value beyond it."""
        prev_s, prev_c = 0.0, 0.0
        for k in self.knots:
            if stream_s <= k.stream_s:
                return interpolate_correction(stream_s, prev_s, prev_c, k.stream_s, k.correction_s)
            prev_s, prev_c = k.stream_s, k.correction_s
        return prev_c

    def knot(self, stream_s: float, anchor_mono: float, host_drift_s: float) -> TimeKnot:
        """Fix the correction at segment boundary ``stream_s`` (boundaries must be increasing;
        a boundary at or before the last knot returns that knot unchanged)."""
        last = self.last
        if last is not None and stream_s <= last.stream_s:
            return last
        prev_s = last.stream_s if last is not None else 0.0
        prev_cam = last.camera_applied_s if last is not None else 0.0
        prev_host = last.host_applied_s if last is not None else 0.0
        offset, n = self.estimate_offset(stream_s)
        camera = None if offset is None else offset - anchor_mono
        allow = self.max_drift_ppm * 1e-6 * (stream_s - prev_s) + NON_DRIFT_ALLOWANCE_S
        if camera is None:
            cam_applied, limited = prev_cam, False          # no evidence: hold
        else:
            cam_applied, limited = _clamp(prev_cam, camera, allow)
        host_applied, host_limited = _clamp(prev_host, host_drift_s, allow)
        measured = None if camera is None else camera + host_drift_s
        k = TimeKnot(stream_s, cam_applied + host_applied, measured, camera, host_drift_s, limited, n,
                     cam_applied, host_applied, host_limited)
        self.limited_count += int(limited)
        self.host_limited_count += int(host_limited)
        self.knots.append(k)
        if len(self.knots) > 4096:
            del self.knots[:1024]
        return k

    def camera_drift_ppm(self) -> tuple[float | None, float | None]:
        """(camera clock rate vs host monotonic in ppm, positive = camera fast; span in stream s).

        From the latest measured knot: the reception offset changes by ``-rate * t``. None
        until measured over at least ``MIN_PPM_SPAN_S``."""
        for k in reversed(self.knots):
            if k.camera_s is not None:
                if k.stream_s < MIN_PPM_SPAN_S:
                    return None, k.stream_s
                return -k.camera_s / k.stream_s * 1e6, k.stream_s
        return None, None


class ClockStepDetector:
    """Detects *sudden* changes of the wall clock relative to the monotonic clock, and measures
    the gradual drift between them.

    Compares consecutive samples (taken every poll interval). Gradual drift — e.g. Windows'
    monotonic clock is not disciplined by time synchronisation, so wall-minus-monotonic slowly
    changes over days — is not a step; only a jump larger than ``tolerance_s`` between two
    samples counts. ``total_s`` is the sum of detected steps. ``drift_s`` is the gradual change
    since the first sample with the steps removed: the median of the last
    ``HOST_MEDIAN_SAMPLES`` *tight* samples (wall read between two monotonic reads no more than
    ``MAX_PAIR_SPREAD_S`` apart), so a sample taken while the reading thread was delayed does not
    leak into segment times. The first sample must itself be tight (use ``read_clock_pair``).
    """

    def __init__(self, wall: float, mono: float, tolerance_s: float) -> None:
        self._offset = wall - mono
        self._initial = self._offset
        self._mono0 = self._mono_last = mono
        self.tolerance_s = tolerance_s
        self.total_s = 0.0
        self._recent: deque[float] = deque([self._offset], maxlen=HOST_MEDIAN_SAMPLES)

    def update(self, wall: float, mono: float, spread_s: float = 0.0) -> float:
        offset = wall - mono
        jump = offset - self._offset
        self._offset = offset
        self._mono_last = mono
        step = 0.0
        if abs(jump) > self.tolerance_s:
            self.total_s += jump
            step = jump
        if spread_s <= MAX_PAIR_SPREAD_S:
            self._recent.append(offset - self.total_s)
        return step

    @property
    def offset_s(self) -> float:
        """Latest raw wall-minus-monotonic offset."""
        return self._offset

    @property
    def drift_s(self) -> float:
        """Gradual wall-vs-monotonic change since the first sample, steps excluded (median of
        recent tight samples)."""
        return statistics.median(self._recent) - self._initial

    @property
    def elapsed_s(self) -> float:
        return self._mono_last - self._mono0

    @property
    def drift_ppm(self) -> float | None:
        """Wall clock rate vs monotonic clock in ppm (positive = wall clock runs faster);
        None until sampled over at least ``MIN_PPM_SPAN_S``."""
        if self.elapsed_s < MIN_PPM_SPAN_S:
            return None
        return self.drift_s / self.elapsed_s * 1e6


