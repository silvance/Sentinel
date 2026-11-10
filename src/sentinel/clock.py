"""Clock abstraction so time-dependent logic is testable and explicit.

All persisted times are UTC. Monotonic time is used for intervals and anchoring so a
wall-clock step does not corrupt derived timestamps (see docs/03 §4).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def wall(self) -> float: ...       # seconds since epoch (UTC), may step
    def mono(self) -> float: ...       # monotonic seconds, never steps


class SystemClock:
    def wall(self) -> float:
        return time.time()

    def mono(self) -> float:
        return time.monotonic()


def iso_utc(epoch_s: float) -> str:
    """Unambiguous ISO-8601 UTC string with microseconds and 'Z'."""
    return datetime.fromtimestamp(epoch_s, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso_utc(text: str) -> float:
    if not text.endswith("Z"):
        raise ValueError(f"not a UTC 'Z' timestamp: {text!r}")
    dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return dt.timestamp()


def compact_utc(epoch_s: float) -> str:
    """Filesystem-safe UTC stamp, e.g. 20261109T201500Z."""
    return datetime.fromtimestamp(epoch_s, tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def add_seconds_iso(iso: str, seconds: float) -> str:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return (dt + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
