"""Local retention (admin-enabled, default OFF): removes only archived, old, sealed originals, and only
as much as needed. Synthetic sealed segments, temporary folders as archive drives, a fake disk whose
free space grows as files are deleted."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from archive_helpers import FakeEjector, SwitchLocator, drain, make_drive, sha_file
from conftest import make_config
from sentinel.alerts import Severity
from sentinel.archive.ledger import ArchiveLedger
from sentinel.archive.worker import ArchiveWorker
from sentinel.config import ConfigError
from sentinel.db import Ledger
from sentinel.models import SegmentState
from sentinel.platform import get_platform
from sentinel.recorder.integrity import IntegrityWorker
from sentinel.recorder.recovery import Recovery
from sentinel.retention.manager import RetentionManager, RetentionReport, oldest_kept_text
from sentinel.storage.monitor import GB, DiskUsage
from tools.archive_bench import add_synthetic_run

SIZE = 1_000_000                 # bytes per synthetic segment
DAY = 86400.0


class SpoolDisk:
    """Free space = base + bytes deleted from the spool since creation."""

    def __init__(self, spool: Path, base_free: int) -> None:
        self.spool = spool
        self.base_free = base_free
        self.initial = self._used()

    def _used(self) -> int:
        return sum(p.stat().st_size for p in self.spool.rglob("*") if p.is_file())

    def __call__(self, _p: Path) -> DiskUsage:
        free = self.base_free + (self.initial - self._used())
        return DiskUsage(1000 * GB, 1000 * GB - free, free)


class Env:
    def __init__(self, tmp: Path, retention: dict[str, Any] | None = None, **archive: Any) -> None:
        self.tmp = tmp
        r = {"enabled": True, "floor_free_gb": 3 * SIZE / GB, "margin_gb": 0.0, "keep_recent_hours": 0.0}
        r.update(retention or {})
        a = {"enabled": True, "require_drive_marker": False, "reserve_free_mb": 0.0, "poll_interval_s": 0.05,
             "chunk_mib": 1, "fsync_every_mib": 2, "max_rate_mb_s": 1000.0,
             "resume_policy": "this_drive"}   # integration: explicit (the default became "any_drive")
        a.update(archive)
        self.cfg = make_config(tmp, archive=a, retention=r)
        self.ledger = Ledger(self.cfg.data_path / "sentinel.db")
        self.disk: SpoolDisk | None = None

    def segments(self, n: int, **kw: Any) -> list[str]:
        return add_synthetic_run(self.ledger, self.cfg, n, SIZE, seconds_each=300.0, **kw)

    def worker(self, loc: Any) -> ArchiveWorker:
        return ArchiveWorker(self.cfg, ledger=self.ledger, locator=loc, allow_same_volume=True,
                             lower_own_priority=False, remover=FakeEjector())

    def manager(self, base_free: int, now: float | None = None) -> RetentionManager:
        self.disk = SpoolDisk(self.cfg.spool_path, base_free)
        return RetentionManager(self.cfg, self.ledger, disk_usage=self.disk,
                                now=(lambda: now) if now is not None else time.time)

    def deleted(self) -> list[str]:
        return [r["segment_id"] for r in self.ledger.query(
            "SELECT segment_id FROM segments WHERE local_deleted_utc IS NOT NULL ORDER BY rowid")]

    def close(self) -> None:
        self.ledger.close()


def archive_then_remove_drive(env: Env, name: str = "A", swap: bool = True) -> str:
    loc = SwitchLocator(make_drive(env.tmp, name))
    w = env.worker(loc)
    drain(w)
    if swap:
        w.request_swap()
        assert w.step() == "swapped"
    loc.target = None
    w.step()
    w.step()
    return [r["drive_id"] for r in env.ledger.query("SELECT drive_id FROM drives ORDER BY rowid")][-1]


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    e = Env(tmp_path)
    yield e
    e.close()


def test_disabled_by_default(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert cfg.retention.enabled is False and cfg.retention.require_removed_drive is True
    assert cfg.retention.min_verified_copies == 1
    e = Env(tmp_path / "x", retention={"enabled": False})
    try:
        e.segments(5)
        archive_then_remove_drive(e)
        rep = e.manager(base_free=0).run_once()
        assert rep.deleted == 0 and e.deleted() == []
    finally:
        e.close()
    with pytest.raises(ConfigError):     # retention without archiving makes no sense: refused
        make_config(tmp_path / "y", retention={"enabled": True})


def test_removes_oldest_archived_until_floor_then_stops(env: Env) -> None:
    ids = env.segments(8)
    did = archive_then_remove_drive(env)
    before = {i: env.ledger.get_segment(i)["sha256"] for i in ids}
    rep = env.manager(base_free=0).run_once()        # floor = 3 segments' worth, margin 0
    assert rep.deleted == 3 and env.deleted() == ids[:3]          # oldest first, exactly enough
    for i in ids[:3]:
        s = env.ledger.get_segment(i)
        assert s["local_deleted_utc"] and s["sha256"] == before[i]  # metadata and hashes kept
        assert not (env.cfg.spool_path / s["rel_path"]).exists()
    for i in ids[3:]:
        assert (env.cfg.spool_path / env.ledger.get_segment(i)["rel_path"]).exists()
    rows = env.ledger.query("SELECT * FROM retention_deletions ORDER BY deletion_id")
    assert [r["segment_id"] for r in rows] == ids[:3] and all(r["state"] == "DONE" for r in rows)
    assert all(did in r["archive_copies"] for r in rows)
    ev = env.ledger.query("SELECT * FROM events WHERE code='retention_deleted'")
    assert len(ev) == 3 and all(e["severity"] == "audit" for e in ev)
    # above the floor now: a second pass removes nothing
    m = RetentionManager(env.cfg, env.ledger, disk_usage=env.disk)
    assert m.run_once().deleted == 0


def test_margin_limits_how_much_is_removed(tmp_path: Path) -> None:
    e = Env(tmp_path, retention={"floor_free_gb": 2 * SIZE / GB, "margin_gb": 2 * SIZE / GB})
    try:
        ids = e.segments(8)
        archive_then_remove_drive(e)
        assert e.manager(base_free=0).run_once().deleted == 4 and e.deleted() == ids[:4]
    finally:
        e.close()


def test_never_unarchived_corrupt_lost_or_recent(tmp_path: Path) -> None:
    e = Env(tmp_path, retention={"floor_free_gb": 100 * SIZE / GB})   # wants far more than allowed
    try:
        archived = e.segments(3)
        corrupt = e.segments(1, state=SegmentState.CORRUPT)
        did = archive_then_remove_drive(e)
        lost_one = archived[1]
        ArchiveLedger(e.ledger, e.cfg.camera.id).mark_lost(
            e.ledger.query("SELECT copy_id FROM archive_copies WHERE segment_id=? AND kind='original'",
                           (lost_one,))[0]["copy_id"], "test")
        unarchived = e.segments(2)                                      # never copied anywhere
        rep = e.manager(base_free=0).run_once()
        assert e.deleted() == [archived[0], archived[2]]
        assert rep.blocked and rep.skipped.get("not_enough_verified_copies", 0) >= 3
        for keep in [lost_one, *unarchived, *corrupt]:
            assert e.ledger.get_segment(keep)["local_deleted_utc"] is None
            assert (e.cfg.spool_path / e.ledger.get_segment(keep)["rel_path"]).exists()
        assert did
    finally:
        e.close()
    e = Env(tmp_path / "recent", retention={"keep_recent_hours": 24.0})
    try:
        e.segments(4)                                                   # sealed just now
        archive_then_remove_drive(e)
        assert e.manager(base_free=0).run_once().deleted == 0
        later = e.manager(base_free=0, now=time.time() + 2 * DAY).run_once()
        assert later.deleted == 3
    finally:
        e.close()


def test_drive_still_attached_does_not_count_by_default(tmp_path: Path) -> None:
    e = Env(tmp_path)
    try:
        e.segments(5)
        loc = SwitchLocator(make_drive(e.tmp, "A"))
        drain(e.worker(loc))                                            # verified, but drive still connected
        assert e.manager(base_free=0).run_once().deleted == 0
        sessions = e.ledger.query("SELECT * FROM archive_drive_sessions")
        assert len(sessions) == 1 and sessions[0]["gone_utc"] is None
    finally:
        e.close()
    e = Env(tmp_path / "opt", retention={"require_removed_drive": False})
    try:
        e.segments(5)
        drain(e.worker(SwitchLocator(make_drive(e.tmp, "A"))))
        assert e.manager(base_free=0).run_once().deleted == 3           # policy option: attached drive counts
    finally:
        e.close()


def test_min_verified_copies_two_needs_two_removed_drives(tmp_path: Path) -> None:
    e = Env(tmp_path, retention={"min_verified_copies": 2})
    try:
        e.segments(4)
        archive_then_remove_drive(e, "A")
        assert e.manager(base_free=0).run_once().deleted == 0
        archive_then_remove_drive(e, "B")                               # this_drive policy: B gets everything too
        assert e.manager(base_free=0).run_once().deleted == 3
    finally:
        e.close()


def test_sessions_record_swap_and_removal_and_unreadable_keeps_open(env: Env) -> None:
    env.segments(1)
    archive_then_remove_drive(env)
    s = env.ledger.query("SELECT * FROM archive_drive_sessions")
    assert len(s) == 1 and s[0]["gone_utc"] and s[0]["gone_how"] == "swapped"   # review 2026-11-10
    al = ArchiveLedger(env.ledger, env.cfg.camera.id)
    did = s[0]["drive_id"]
    al.sync_sessions({did}, keep_open=False)                            # back again
    al.sync_sessions(set(), keep_open=True)                             # unreadable drive present: not proven gone
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_drive_sessions WHERE gone_utc IS NULL")[0]["n"] == 1


def test_interrupted_removal_is_completed(env: Env) -> None:
    ids = env.segments(4)
    archive_then_remove_drive(env)
    m = env.manager(base_free=0)
    real = m._delete_files
    m._delete_files = lambda *a: (_ for _ in ()).throw(RuntimeError("power cut"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        m.run_once()
    row = env.ledger.query("SELECT * FROM retention_deletions")[0]
    assert row["state"] == "DELETING" and env.ledger.get_segment(ids[0])["local_deleted_utc"]
    assert (env.cfg.spool_path / row["rel_path"]).exists()              # decided, not yet deleted
    m._delete_files = real  # type: ignore[method-assign]
    rep = m.run_once()
    assert rep.completed_interrupted == 1
    assert not (env.cfg.spool_path / row["rel_path"]).exists()
    assert env.ledger.query("SELECT state FROM retention_deletions WHERE deletion_id=?",
                            (row["deletion_id"],))[0]["state"] == "DONE"


def test_integrity_audit_and_recovery_do_not_report_removed_as_missing(env: Env) -> None:
    ids = env.segments(4)
    archive_then_remove_drive(env)
    env.manager(base_free=0).run_once()
    assert len(env.deleted()) == 3
    iw = IntegrityWorker(env.cfg, env.ledger, get_platform(), None)
    assert iw.audit_once() == []
    assert iw.outstanding_audit_failures() == 0
    audited = {r["segment_id"] for r in env.ledger.query("SELECT segment_id FROM segments WHERE last_audit_utc "
                                                         "IS NOT NULL AND audit_status='ok'")}
    assert ids[3] in audited
    assert not {r["segment_id"] for r in env.ledger.segments_needing_content_hash(999)} & set(env.deleted())
    Recovery(env.cfg, env.ledger, None)._check_sealed_files_exist()     # type: ignore[arg-type]
    assert env.ledger.query("SELECT COUNT(*) n FROM events WHERE code='sealed_file_missing'")[0]["n"] == 0


def test_review_export_planner_skips_removed_originals(env: Env) -> None:
    from zoneinfo import ZoneInfo

    from sentinel.export.planner import Planner

    ids = env.segments(4, start_epoch=time.time() - 3 * DAY)
    archive_then_remove_drive(env)
    env.manager(base_free=0).run_once()
    plan = Planner(env.ledger, env.cfg.spool_path, env.cfg.camera.id, ZoneInfo("UTC")).plan("review-x", None, 10**12)
    assert plan is not None
    planned = {i.segment_id for i in plan.items if i.segment_id}
    assert planned == {ids[3]}
    assert any(w["code"] == "footage_only_on_archive_drive" and set(w["segment_ids"]) == set(ids[:3])
               for w in plan.to_json()["warnings"])


def test_oldest_kept_text_and_status(env: Env) -> None:
    ids = env.segments(4, start_epoch=1_762_000_000.0)                   # 2025-11-01 UTC
    archive_then_remove_drive(env)
    m = env.manager(base_free=0)
    m.run_once()
    st = m.status()
    assert st["oldest_kept_utc"] == env.ledger.get_segment(ids[3])["start_utc"]
    assert oldest_kept_text(st["oldest_kept_utc"], "UTC") == "Oldest footage kept on this computer: 2025-11-01"
    assert oldest_kept_text(None, "UTC") == "Oldest footage kept on this computer: none"


def test_recorder_status_alerts_l001_l002(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from sentinel.service import RecorderService
    from sentinel.status import StatusDocument

    for enabled, blocked, expect in ((False, False, "L-001"), (True, True, "L-002")):
        cfg = make_config(tmp_path / str(enabled), archive={"enabled": True}, retention={"enabled": enabled})
        svc = RecorderService(cfg)
        led = Ledger(cfg.data_path / "sentinel.db")
        try:
            svc.retention = RetentionManager(cfg, led)
            svc.retention.last = RetentionReport("t", enabled, blocked=blocked)
            doc = StatusDocument("RECORDING", "connected", [], "CAM01", "", 0.0)
            sample = SimpleNamespace(seconds_until_critical=2 * DAY)      # < 5-day swap horizon
            alerts = svc._retention_status(doc, sample)
            assert [a.code for a in alerts] == [expect] and alerts[0].severity == Severity.WARNING
            assert doc.retention["text"].startswith("Oldest footage kept on this computer:")
            assert not alerts[0].recording_stopped
            far = SimpleNamespace(seconds_until_critical=30 * DAY)
            svc.retention.last = RetentionReport("t", enabled, blocked=False)
            assert svc._retention_status(doc, far) == []
        finally:
            led.close()


def test_archive_drive_capacity_warning(tmp_path: Path) -> None:
    from archive_helpers import FaultyIO

    e = Env(tmp_path)
    try:
        e.segments(3)                       # 1 MB per 300 s ≈ 3.3 kB/s → 5 days ≈ 1.44 GB
        io = FaultyIO(free=500_000_000)
        w = ArchiveWorker(e.cfg, ledger=e.ledger, locator=SwitchLocator(make_drive(e.tmp, "A"), io), io=io,
                          allow_same_volume=True, lower_own_priority=False)
        drain(w)
        w._publish(force=True)
        doc = w.status.doc
        codes = {a["code"]: a["severity"] for a in doc["alerts"]}
        assert codes.get("A-009") == Severity.WARNING
        io.free = 10_000_000_000
        w._publish(force=True)
        assert "A-009" not in {a["code"] for a in w.status.doc["alerts"]}
    finally:
        e.close()


def test_retention_only_deletes_inside_the_spool(env: Env) -> None:
    ids = env.segments(4, sidecars=True)
    archive_then_remove_drive(env)
    drive_files = {p: sha_file(p) for p in (env.tmp / "archive_A").rglob("*") if p.is_file()}
    env.manager(base_free=0).run_once()
    assert {p: sha_file(p) for p in (env.tmp / "archive_A").rglob("*") if p.is_file()} == drive_files
    s = env.ledger.get_segment(ids[0])
    assert not (env.cfg.spool_path / s["framehash_rel_path"]).exists()   # sidecar removed with its original
    assert os.path.isdir(env.cfg.spool_path)
    assert stat.S_ISDIR(os.stat(env.cfg.spool_path).st_mode)
