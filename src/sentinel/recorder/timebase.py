"""Per-run wall-clock anchoring, reception-time drift tracking and clock-step detection
(docs/03 §4).

Three clocks are involved:

* the camera's clock, which stamps the stream (stream time ``t``, from FFmpeg ``-progress``);
* the host monotonic clock ``m`` (never steps; on Windows it is *not* disciplined by time
  synchronisation, so it drifts slowly against UTC);
* the host wall clock ``w`` (UTC, disciplined by the OS time service; may step).

``RunTimebase`` fixes one anchor per run: the monotonic time at which stream time 0 was
received. ``DriftTracker`` then measures, at every segment boundary, how far the
anchor-plus-stream-time estimate has drifted from the actual reception time (camera clock
rate vs host monotonic) and how far the monotonic clock has drifted from the wall clock
(steps excluded), and turns that into a *rate-limited* correction. Corrections are fixed at
segment boundaries ("knots") and interpolated linearly in between, so consecutive segments stay
contiguous and a sudden jump (a wall-clock step, or real time FFmpeg removed from the stream)
is never followed — it is left to the step and discontinuity detectors, which flag it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..clock import iso_utc

TIME_METHOD = "reception_tracking_v1"
# Jitter allowance per knot: how much the measured correction may move between two knots on
# top of the drift-rate limit, so measurement noise (scheduling, progress-report timing) does
# not register as rate-limiting. Large jumps are still never followed.
JITTER_ALLOWANCE_S = 0.1
# Drift rates (ppm) are only reported once measured over at least this much time; over shorter
# spans measurement noise of a few tens of milliseconds dominates.
MIN_PPM_SPAN_S = 60.0


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
    correction_s: float              # applied: rate-limited (camera + host components)
    measured_s: float | None         # what the measurements alone said (None: no reports in window)
    camera_s: float | None           # measured reception offset vs the run anchor (camera vs host mono)
    host_s: float                    # host wall-vs-monotonic gradual drift since run start (steps excluded)
    limited: bool                    # True if the rate limit held the correction back
    reports: int                     # progress reports in the measurement window

    def detail(self) -> dict[str, Any]:
        return {"method": TIME_METHOD, "stream_s": round(self.stream_s, 6),
                "applied_s": round(self.correction_s, 6),
                "measured_s": None if self.measured_s is None else round(self.measured_s, 6),
                "camera_s": None if self.camera_s is None else round(self.camera_s, 6),
                "host_s": round(self.host_s, 6), "limited": self.limited, "reports": self.reports}


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


@dataclass
class DriftTracker:
    """Rate-limited per-segment correction of the run anchor toward actual reception time.

    ``observe`` is fed every advancing progress report ``(t, m)``. Because a packet with stream
    time ``t`` cannot be reported before it was received, ``m - t`` is an upper bound of the
    reception offset; its minimum over a short window of reports around a segment boundary
    filters scheduling and reporting jitter (the same estimator as the run anchor, applied
    locally). The camera component of the correction is that local minimum minus the run
    anchor; the host component is the gradual wall-vs-monotonic drift (steps excluded) supplied
    by ``ClockStepDetector.drift_s``.

    The applied correction may change between consecutive knots by at most
    ``max_drift_ppm * Δt + JITTER_ALLOWANCE_S``. Anything faster is not drift and is not
    followed: the knot is marked ``limited`` (a warning on the segment) and the remainder is
    caught up at the limit rate only if it persists.
    """
    max_drift_ppm: float
    window_before_s: float = 8.0
    window_after_s: float = 2.0
    history_s: float = 180.0
    _reports: deque = field(default_factory=lambda: deque(maxlen=8192))   # (t, m)
    knots: list[TimeKnot] = field(default_factory=list)
    limited_count: int = 0
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

    def measure_jump(self, break_stream_s: float) -> float | None:
        """Real time lost at ``break_stream_s``: min(m - t) over reports after the break minus
        min(m - t) over the window before it. Unlike a slowly-following baseline this is not
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
        """min(m - t) over reports within the window around ``stream_s``; (None, 0) if none."""
        lo, hi = stream_s - self.window_before_s, stream_s + self.window_after_s
        best: float | None = None
        n = 0
        for t, m in self._reports:
            if lo <= t <= hi:
                v = m - t
                if self._break is not None and t > self._break[0]:
                    v -= self._break[1]
                n += 1
                if best is None or v < best:
                    best = v
        return best, n

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
        prev_c = last.correction_s if last is not None else 0.0
        offset, n = self.offset_at(stream_s)
        camera = None if offset is None else offset - anchor_mono
        measured = None if camera is None else camera + host_drift_s
        allow = self.max_drift_ppm * 1e-6 * (stream_s - prev_s) + JITTER_ALLOWANCE_S
        limited = False
        if measured is None:
            applied = prev_c                     # no evidence: hold the last correction
        else:
            delta = measured - prev_c
            if delta > allow:
                applied, limited = prev_c + allow, True
            elif delta < -allow:
                applied, limited = prev_c - allow, True
            else:
                applied = measured
        k = TimeKnot(stream_s, applied, measured, camera, host_drift_s, limited, n)
        if limited:
            self.limited_count += 1
        self.knots.append(k)
        if len(self.knots) > 4096:
            del self.knots[:1024]
        return k

    def camera_drift_ppm(self) -> tuple[float | None, float | None]:
        """(camera clock rate vs host monotonic in ppm, positive = camera fast; span in stream s).

        Derived from the latest measured knot: the reception offset changes by ``-rate * t``.
        None until measured over at least ``MIN_PPM_SPAN_S``."""
        for k in reversed(self.knots):
            if k.camera_s is not None:
                if k.stream_s < MIN_PPM_SPAN_S:
                    return None, k.stream_s
                return -k.camera_s / k.stream_s * 1e6, k.stream_s
        return None, None


class ClockStepDetector:
    """Detects *sudden* changes of the wall clock relative to the monotonic clock.

    Compares consecutive samples (taken every poll interval). Gradual drift — e.g. Windows'
    monotonic clock is not disciplined by time synchronisation, so wall-minus-monotonic slowly
    changes over days — is followed silently; only a jump larger than ``tolerance_s`` between
    two samples counts as a step. ``total_s`` is the sum of detected steps; ``drift_s`` is the
    gradual change since the first sample with the steps removed.
    """

    def __init__(self, wall: float, mono: float, tolerance_s: float) -> None:
        self._offset = wall - mono
        self._initial = self._offset
        self._mono0 = self._mono_last = mono
        self.tolerance_s = tolerance_s
        self.total_s = 0.0

    def update(self, wall: float, mono: float) -> float:
        offset = wall - mono
        jump = offset - self._offset
        self._offset = offset
        self._mono_last = mono
        if abs(jump) > self.tolerance_s:
            self.total_s += jump
            return jump
        return 0.0

    @property
    def offset_s(self) -> float:
        """Latest raw wall-minus-monotonic offset."""
        return self._offset

    @property
    def drift_s(self) -> float:
        """Gradual wall-vs-monotonic change since the first sample, steps excluded."""
        return (self._offset - self._initial) - self.total_s

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
