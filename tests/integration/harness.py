"""Test harness: run the real RecorderService in-process against a SimulatedCamera."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from conftest import make_config
from sentinel.clock import SystemClock, parse_iso_utc
from sentinel.config import AppConfig
from sentinel.db import Ledger
from sentinel.hashing import sha256_file
from sentinel.platform import get_platform
from sentinel.service import RecorderService
from sentinel.storage.monitor import DiskUsage

T = TypeVar("T")
GB = 1024**3


class ShiftableClock(SystemClock):
    """System clock whose *wall* time can be stepped, like an operator or NTP changing it."""

    def __init__(self) -> None:
        self.offset = 0.0

    def wall(self) -> float:
        return super().wall() + self.offset


class FakeDisk:
    def __init__(self, free_gb: float = 500.0) -> None:
        self.free_gb = free_gb

    def __call__(self, _p: Path) -> DiskUsage:
        free = int(self.free_gb * GB)
        return DiskUsage(1000 * GB, 1000 * GB - free, free)


def wait_for(pred: Callable[[], T], timeout: float, interval: float = 0.25, what: str = "condition") -> T:
    end = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < end:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}; last={last!r}")


class Harness:
    def __init__(self, tmp: Path, url: str, clock: SystemClock | None = None, disk: FakeDisk | None = None,
                 **sections: dict[str, Any]) -> None:
        self.tmp = tmp
        self.cfg: AppConfig = make_config(tmp, url, **sections)
        self.disk = disk
        self.svc = RecorderService(self.cfg, clock=clock, disk_usage=disk)
        self._thread: threading.Thread | None = None
        self.exit_code: int | None = None

    def start(self) -> Harness:
        def target() -> None:
            self.exit_code = self.svc.run()
        self._thread = threading.Thread(target=target, name="svc", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 120.0) -> None:
        self.svc.request_stop()
        if self._thread is not None:
            self._thread.join(timeout)
            assert not self._thread.is_alive(), "service did not stop"

    # --------------------------------------------------------------- inspection
    def q(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        db = self.cfg.data_path / "sentinel.db"
        if not db.exists():
            return []
        led = Ledger(db, readonly=True)
        try:
            return [dict(r) for r in led.query(sql, params)]
        finally:
            led.close()

    def segments(self, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            return self.q("SELECT * FROM segments WHERE state=? ORDER BY rowid", (state,))
        return self.q("SELECT * FROM segments ORDER BY rowid")

    def runs(self) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM recording_runs ORDER BY rowid")

    def gaps(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            return self.q("SELECT * FROM recording_gaps WHERE kind=? ORDER BY gap_id", (kind,))
        return self.q("SELECT * FROM recording_gaps ORDER BY gap_id")

    def events(self, code: str | None = None) -> list[dict[str, Any]]:
        if code:
            return self.q("SELECT * FROM events WHERE code=? ORDER BY event_id", (code,))
        return self.q("SELECT * FROM events ORDER BY event_id")

    def status(self) -> dict[str, Any]:
        try:
            return json.loads((self.cfg.data_path / "status.json").read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            return {}

    def sealed_count(self) -> int:
        return len(self.q("SELECT 1 FROM segments WHERE state IN ('FINALIZED','INTERRUPTED','CORRUPT')"))

    def wait_sealed(self, n: int, timeout: float = 40.0) -> None:
        wait_for(lambda: self.sealed_count() >= n, timeout, what=f"{n} sealed segments")

    def path(self, seg: dict[str, Any]) -> Path:
        return self.cfg.spool_path / seg["rel_path"]

    def assert_sealed_integrity(self, seg: dict[str, Any]) -> None:
        p = self.path(seg)
        sha, size = sha256_file(p)
        assert sha == seg["sha256"], f"file hash mismatch for {seg['rel_path']}"
        assert size == seg["size_bytes"]
        assert get_platform().is_read_only(p), f"{seg['rel_path']} not sealed read-only"
        if seg["framehash_rel_path"]:
            fh = self.cfg.spool_path / seg["framehash_rel_path"]
            assert sha256_file(fh)[0] == seg["framehash_sha256"]
            assert get_platform().is_read_only(fh)


def contiguous(a: dict[str, Any], b: dict[str, Any], tol: float = 0.15) -> bool:
    return abs(parse_iso_utc(b["start_utc"]) - parse_iso_utc(a["end_utc"])) <= tol
