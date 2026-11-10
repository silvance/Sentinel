"""Per-run wall-clock anchoring and clock-step detection (docs/03 §4)."""

from __future__ import annotations

from dataclasses import dataclass

from ..clock import iso_utc


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

    def epoch_for(self, stream_s: float) -> float | None:
        a = self.anchor_epoch
        return None if a is None else a + stream_s

    def iso_for(self, stream_s: float) -> str | None:
        e = self.epoch_for(stream_s)
        return None if e is None else iso_utc(e)



class ClockStepDetector:
    """Detects *sudden* changes of the wall clock relative to the monotonic clock.

    Compares consecutive samples (taken every poll interval). Gradual drift — e.g. Windows'
    monotonic clock is not disciplined by time synchronisation, so wall-minus-monotonic slowly
    changes over days — is followed silently; only a jump larger than ``tolerance_s`` between
    two samples counts as a step. ``total_s`` is the sum of detected steps.
    """

    def __init__(self, wall: float, mono: float, tolerance_s: float) -> None:
        self._offset = wall - mono
        self.tolerance_s = tolerance_s
        self.total_s = 0.0

    def update(self, wall: float, mono: float) -> float:
        offset = wall - mono
        jump = offset - self._offset
        self._offset = offset
        if abs(jump) > self.tolerance_s:
            self.total_s += jump
            return jump
        return 0.0
