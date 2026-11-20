"""Where removal of archived recordings from this computer starts (0.3.26, owner decision 2026-11-20).

Until 0.3.25 it started at the storage warning level itself. A computer whose drive had filled up therefore
lived on that line: free space had to touch the warning level before anything was removed, the status showed
"Storage warning" until the next pass (about every 10 GB of recording), and it said "about a day of space
left" all the time, although days of archived recordings could still be removed.

Now: 150 GB unless a level is set (less on a small drive, always clear of the warning level), a field for it
on the Storage page, and a time-left figure that counts what can still be removed. What is removed, and
when it may be removed, is unchanged: tests/unit/test_retention_units.py.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from archive_helpers import SwitchLocator, drain, make_drive
from conftest import make_config
from sentinel.db import Ledger
from sentinel.models import StorageLevel
from sentinel.retention.manager import (DEFAULT_FLOOR_ABOVE_WARNING_GB, DEFAULT_FLOOR_BYTES, RECLAIM_EVERY_PASSES,
                                        RetentionManager, default_floor_bytes, floor_bytes_for)
from sentinel.storage.monitor import GB, DiskUsage, StorageMonitor
from test_retention_units import DAY, SIZE, Env, archive_then_remove_drive


class FixedDisk:
    """A recording drive of a given size with a given amount free."""

    def __init__(self, total: float, free: float) -> None:
        self.total, self.free = int(total), int(free)

    def __call__(self, _p: Path) -> DiskUsage:
        return DiskUsage(self.total, self.total - self.free, self.free)


def _deployed(tmp: Path, **retention: Any):  # noqa: ANN202
    """Thresholds as first-run setup leaves them (storage warning 50, critical 5; no level for removal)."""
    return make_config(tmp, storage={"warning_free_gb": 50.0, "critical_free_gb": 5.0}, archive={"enabled": True},
                       retention={"enabled": True, **retention})


def test_removal_starts_at_150_gb_unless_the_drive_is_small_or_a_level_is_set(tmp_path: Path) -> None:
    cfg = _deployed(tmp_path)
    lowest = int((50.0 + DEFAULT_FLOOR_ABOVE_WARNING_GB) * GB)          # always clear of the warning level
    assert DEFAULT_FLOOR_BYTES == 150 * 10**9                           # "150 GB" as the window shows sizes
    assert default_floor_bytes(cfg, 512 * 10**9) == 150 * 10**9         # the deployed laptop
    assert default_floor_bytes(cfg, 2000 * 10**9) == 150 * 10**9        # never more, however big the drive
    assert default_floor_bytes(cfg, 256 * 10**9) == int(256e9 * 0.40)   # a smaller drive: 40 % of it
    assert default_floor_bytes(cfg, 128 * 10**9) == lowest              # a small one: just clear of the warning
    assert default_floor_bytes(cfg, None) == lowest == default_floor_bytes(cfg, 0)   # size not readable
    assert lowest > int(50.0 * GB)
    high_warning = make_config(tmp_path / "w", storage={"warning_free_gb": 200.0, "critical_free_gb": 5.0})
    assert default_floor_bytes(high_warning, 512 * 10**9) == int(210.0 * GB)
    assert floor_bytes_for(cfg, 512 * 10**9) == 150 * 10**9
    assert floor_bytes_for(_deployed(tmp_path / "set", floor_free_gb=80.0), 512 * 10**9) == int(80.0 * GB)   # as set
    assert floor_bytes_for(_deployed(tmp_path / "zero", floor_free_gb=0.0), 512 * 10**9) == 0                 # also 0


def test_the_recorder_uses_that_level_and_settles_it_once_the_drive_can_be_read(tmp_path: Path) -> None:
    cfg = _deployed(tmp_path)
    led = Ledger(cfg.data_path / "sentinel.db")
    try:
        m = RetentionManager(cfg, led, disk_usage=FixedDisk(512e9, 480e9))
        assert m.floor_bytes == 150 * 10**9 and m.target_bytes == 150 * 10**9 + int(10.0 * GB)
        rep = m.run_once()
        assert rep.floor_bytes == 150 * 10**9 and rep.deleted == 0 and not rep.blocked     # 480 GB free: nothing to do
        assert m.status()["floor_bytes"] == 150 * 10**9

        # The recording folder may not exist at the very first start.
        failures: list[Exception] = [OSError("not there yet")]

        def disk(p: Path) -> DiskUsage:
            if failures:
                raise failures.pop()
            return FixedDisk(512e9, 480e9)(p)

        late = RetentionManager(cfg, led, disk_usage=disk)
        assert late.floor_bytes == int((50.0 + DEFAULT_FLOOR_ABOVE_WARNING_GB) * GB)
        late.run_once()
        assert late.floor_bytes == 150 * 10**9

        # Below the level with nothing that may be removed: that is L-002's case, as before.
        low = RetentionManager(cfg, led, disk_usage=FixedDisk(512e9, 120e9))
        assert low.run_once().blocked
    finally:
        led.close()


def _run_a_full_computer(tmp: Path, floor_mb: float, passes: int = 60) -> tuple[int, int, int]:
    """A computer whose recording drive has filled up, at 1/1000 scale (MB for GB): storage warning at 50,
    critical at 5, removal frees 10 at a time; 120 archived recordings on a drive that was taken out. Recording
    goes on, 2 per removal pass, and the status is read four times between passes. Returns (status readings at
    warning or worse, readings in all, recordings removed)."""
    mb = SIZE / GB
    e = Env(tmp, retention={"floor_free_gb": floor_mb * mb, "margin_gb": 10 * mb},
            storage={"warning_free_gb": 50 * mb, "critical_free_gb": 5 * mb})
    try:
        e.segments(120)
        archive_then_remove_drive(e)
        m = e.manager(base_free=int((floor_mb + 12) * SIZE))        # just above the level, as after a pass
        assert e.disk is not None
        mon = StorageMonitor(e.cfg.spool_path, e.cfg.storage.warning_free_gb, e.cfg.storage.critical_free_gb,
                             disk_usage=e.disk)
        low = readings = 0
        for _ in range(passes):
            for _ in range(4):
                e.disk.base_free -= SIZE // 2                        # recording writes
                readings += 1
                low += mon.sample().level is not StorageLevel.OK
            m.run_once()
        return low, readings, len(e.deleted())
    finally:
        e.close()


def test_a_full_computer_does_not_live_on_the_storage_warning_line(tmp_path: Path) -> None:
    """With removal starting AT the warning level, free space has to touch that level before anything is
    removed, and the status shows "Storage warning" until the next pass. With removal starting well above it,
    the warning is never reached while there is something to remove."""
    low, readings, removed = _run_a_full_computer(tmp_path / "at-the-warning-level", floor_mb=50)
    assert low > 0 and removed > 0, "the old rule did not show the warning: this test no longer tests anything"
    low2, readings2, removed2 = _run_a_full_computer(tmp_path / "above-it", floor_mb=150)
    assert (low2, readings2) == (0, readings) and removed2 > 0
    assert abs(removed2 - removed) <= 12              # the same recordings go; they only go sooner


def test_what_could_still_be_removed_is_counted_for_the_status(tmp_path: Path) -> None:
    e = Env(tmp_path)
    try:
        ids = e.segments(8)
        m = e.manager(base_free=10 * SIZE)                           # above the level (3 recordings' worth)
        m.run_once()
        assert m.reclaimable_bytes == 0 and m.status()["reclaimable_bytes"] == 0     # nothing is archived yet
        archive_then_remove_drive(e)
        assert m.count_reclaimable() == 8 * SIZE                     # all of it is on a drive that was taken out
        calls: list[int] = []
        real = m.count_reclaimable
        m.count_reclaimable = lambda: calls.append(1) or real()      # type: ignore[method-assign]
        for _ in range(RECLAIM_EVERY_PASSES - 1):
            m.run_once()
        assert calls == [] and m.reclaimable_bytes == 0              # not recounted on every pass...
        m.run_once()
        assert calls == [1] and m.reclaimable_bytes == 8 * SIZE      # ...but every fifth
        assert e.disk is not None
        e.disk.base_free = 0                                         # space is needed now
        rep = m.run_once()
        assert rep.deleted == 3 and e.deleted() == ids[:3]
        assert m.reclaimable_bytes == 5 * SIZE and len(calls) == 2   # and at once after a removal
    finally:
        e.close()


@pytest.mark.parametrize(("name", "retention", "drive_stays"), [
    ("attached", {}, True), ("recent", {"keep_recent_hours": 24.0}, False), ("off", {"enabled": False}, False)])
def test_only_what_the_rule_would_remove_is_counted(tmp_path: Path, name: str, retention: dict[str, Any],
                                                    drive_stays: bool) -> None:
    e = Env(tmp_path, retention=retention)
    try:
        ids = e.segments(4)
        if drive_stays:
            drain(e.worker(SwitchLocator(make_drive(e.tmp, "A"))))   # verified, but the drive is still plugged in
        else:
            archive_then_remove_drive(e)
        m = e.manager(base_free=10 * SIZE)
        m.run_once()
        assert m.count_reclaimable() == 0
        assert m.status()["reclaimable_bytes"] == (None if name == "off" else 0)
        assert e.deleted() == [] and len(ids) == 4                   # and counting removes nothing
    finally:
        e.close()


@pytest.mark.parametrize("enabled", [True, False])
def test_time_left_counts_what_can_still_be_removed(tmp_path: Path, enabled: bool) -> None:
    """A computer that holds 150 GB free by removing archived recordings would otherwise read "about 3 days
    left" for ever."""
    from sentinel.service import RecorderService
    from sentinel.status import StatusDocument

    cfg = make_config(tmp_path, archive={"enabled": True}, retention={"enabled": enabled})
    svc = RecorderService(cfg)
    led = Ledger(cfg.data_path / "sentinel.db")
    try:
        svc.retention = RetentionManager(cfg, led, disk_usage=FixedDisk(512e9, 160e9))
        svc.retention.reclaimable_bytes = 300 * 10**9
        doc = StatusDocument("RECORDING", "connected", [], "CAM01", "", 0.0)
        doc.storage = {"free_bytes": 160 * 10**9, "seconds_until_attention": 3 * DAY}
        svc._retention_status(doc, SimpleNamespace(seconds_until_critical=3 * DAY, rate_bytes_per_s=500_000.0))
        if enabled:
            assert doc.storage["reclaimable_bytes"] == 300 * 10**9
            assert doc.storage["seconds_until_attention"] == pytest.approx(3 * DAY + 300e9 / 500_000.0)
            assert doc.retention["floor_bytes"] == 150 * 10**9 and doc.retention["reclaimable_bytes"] == 300 * 10**9
        else:
            assert doc.storage == {"free_bytes": 160 * 10**9, "seconds_until_attention": 3 * DAY}
            assert doc.retention["reclaimable_bytes"] is None
        # No rate measured yet: the figure is left as it is, not invented.
        doc.storage = {"seconds_until_attention": None}
        svc._retention_status(doc, SimpleNamespace(seconds_until_critical=None, rate_bytes_per_s=None))
        assert doc.storage == {"seconds_until_attention": None}
    finally:
        led.close()
