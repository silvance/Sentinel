"""Local storage measurement: free space, observed recording rate, time-to-threshold."""

from __future__ import annotations

import shutil
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ..models import StorageLevel

GB = 1024**3


class DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


@dataclass(frozen=True)
class StorageSample:
    total_bytes: int
    free_bytes: int
    level: StorageLevel
    rate_bytes_per_s: float | None       # measured from sealed segments; None until known
    seconds_until_critical: float | None
    error: str | None = None


class StorageMonitor:
    def __init__(self, path: Path, warning_free_gb: float, critical_free_gb: float,
                 disk_usage: Callable[[Path], DiskUsage] | None = None, window: int = 24) -> None:
        self.path = path
        self.warning_bytes = int(warning_free_gb * GB)
        self.critical_bytes = int(critical_free_gb * GB)
        self._disk_usage = disk_usage or (lambda p: DiskUsage(*shutil.disk_usage(p)))
        self._recent: deque[tuple[int, float]] = deque(maxlen=window)  # (bytes, seconds)
        self._lock = threading.Lock()
        self.last: StorageSample | None = None

    def record_segment(self, size_bytes: int, duration_s: float | None) -> None:
        if duration_s and duration_s > 0.5 and size_bytes > 0:
            with self._lock:
                self._recent.append((size_bytes, duration_s))

    def rate(self) -> float | None:
        with self._lock:
            if not self._recent:
                return None
            b = sum(x for x, _ in self._recent)
            s = sum(y for _, y in self._recent)
        return b / s if s > 0 else None

    def sample(self) -> StorageSample:
        try:
            du = self._disk_usage(self.path)
        except OSError as exc:
            s = StorageSample(0, 0, StorageLevel.CRITICAL, None, None, error=f"cannot read storage: {exc}")
            self.last = s
            return s
        if du.free <= self.critical_bytes:
            level = StorageLevel.CRITICAL
        elif du.free <= self.warning_bytes:
            level = StorageLevel.WARNING
        else:
            level = StorageLevel.OK
        rate = self.rate()
        remaining = None
        if rate and rate > 0:
            remaining = max(0.0, (du.free - self.critical_bytes) / rate)
        s = StorageSample(du.total, du.free, level, rate, remaining)
        self.last = s
        return s
