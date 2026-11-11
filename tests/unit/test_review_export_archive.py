"""Regression tests for the export / archive / retention findings of the code review of 2026-11-10.
No FFmpeg needed: synthetic sealed segments in a real ledger, temporary folders as drives."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from archive_helpers import FakeEjector, SwitchLocator, drain, make_drive
from conftest import make_config
from sentinel.archive.ledger import ArchiveLedger
from sentinel.archive.locator import DriveScan, VolumeScanLocator
from sentinel.archive.worker import CONTROL_DIR, SWAP_REQUEST, SWAPPED_STATE, ArchiveWorker, request_swap_file
from sentinel.clock import iso_utc
from sentinel.db import Ledger
from sentinel.export.engine import ReviewExporter
from sentinel.export.ledger import ExportLedger, NewItem
from sentinel.export.manifest import GAP_REASONS
from sentinel.export.planner import Planner
from sentinel.export.target import ExportTarget, archive_drive_reason
from sentinel.retention.manager import RetentionManager
from sentinel.storage.monitor import GB, DiskUsage
from tools.archive_bench import add_synthetic_run

SIZE = 300_000
DAY = 86400.0


class Env:
    def __init__(self, tmp: Path, retention: bool = False, **archive: Any) -> None:
        self.tmp = tmp
        a = {"enabled": True, "require_drive_marker": False, "reserve_free_mb": 0.0, "poll_interval_s": 0.05,
             "chunk_mib": 1, "fsync_every_mib": 2, "max_rate_mb_s": 1000.0, "resume_policy": "this_drive"}
        a.update(archive)
        kw: dict[str, Any] = {"archive": a}
        if retention:
            kw["retention"] = {"enabled": True, "floor_free_gb": 100 * SIZE / GB, "margin_gb": 0.0,
                               "keep_recent_hours": 0.0}
        self.cfg = make_config(tmp, **kw)
        self.ledger = Ledger(self.cfg.data_path / "sentinel.db")

    def segments(self, n: int, **kw: Any) -> list[str]:
        return add_synthetic_run(self.ledger, self.cfg, n, SIZE, **kw)

    def worker(self, loc: Any, **kw: Any) -> ArchiveWorker:
        kw.setdefault("remover", FakeEjector())
        return ArchiveWorker(self.cfg, ledger=self.ledger, locator=loc, allow_same_volume=True,
                             lower_own_priority=False, **kw)

    def remove_locally(self, ids: list[str]) -> None:
        """What the retention rule does to a segment (mark + delete the files), without the archive part."""
        now = iso_utc(time.time())
        for sid in ids:
            seg = self.ledger.get_segment(sid)
            with self.ledger.tx() as c, self.ledger.chained(c, "segments", [sid]):
                c.execute("UPDATE segments SET local_deleted_utc=?, updated_utc=? WHERE segment_id=?", (now, now, sid))
            p = self.cfg.spool_path / seg["rel_path"]
            os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
            p.unlink()

    def events(self, code: str) -> list[Any]:
        return self.ledger.query("SELECT * FROM events WHERE code=? ORDER BY event_id", (code,))

    def close(self) -> None:
        self.ledger.close()


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    e = Env(tmp_path)
    yield e
    e.close()


def _review_drive(tmp: Path, name: str = "REV") -> ExportTarget:
    root = tmp / f"review_{name}"
    root.mkdir(exist_ok=True)
    return ExportTarget(root, {"drive_uuid": f"rev-{name}", "label": name})


def _exporter(env: Env, target: ExportTarget) -> ReviewExporter:
    return ReviewExporter(env.cfg, target, ledger=env.ledger, ffmpeg_version="test", allow_same_volume=True)


# ------------------------------------------------------------------ finding 1
def test_unfinished_export_whose_sources_were_removed_is_planned_again_not_failed_e006(env: Env) -> None:
    ids = env.segments(3, start_epoch=time.time() - 3 * DAY)
    target = _review_drive(env.tmp)
    xl = ExportLedger(env.ledger)
    drive = xl.upsert_drive(target)
    segs = [env.ledger.get_segment(s) for s in ids]
    bid = xl.create_batch(camera_id=env.cfg.camera.id, drive_id=drive, interval_start_utc=segs[0]["start_utc"],
                          interval_end_utc=segs[-1]["end_utc"], display_timezone="UTC", operator_id=None,
                          app_version="t", ffmpeg_version=None, stamp="s", plan={},
                          items=[NewItem("original", f"Originals/{s['rel_path']}", segment_id=s["segment_id"],
                                         source_rel_path=s["rel_path"], expected_sha256=s["sha256"],
                                         planned_bytes=SIZE) for s in segs])
    xl.set_batch_state(bid, "COPYING")
    xl.set_batch_state(bid, "INTERRUPTED")          # drive pulled mid-copy
    env.remove_locally(ids)                         # retention removed them meanwhile
    assert xl.removed_sources(bid) == ids
    for _ in range(2):                              # and it stays that way on every later attempt
        res = _exporter(env, target).run()
        assert res.state == "NOTHING_TO_EXPORT" and res.code is None, res
    b = xl.batch(bid)
    assert b["state"] == "FAILED" and "planned again" in b["error_detail"]
    assert env.events("export_replanned")


# ------------------------------------------------------------------ finding 2
@pytest.mark.parametrize("how", ["marker", "folder", "ledger"])
def test_review_export_refuses_an_archive_drive(env: Env, how: str) -> None:
    env.segments(2)
    target = _review_drive(env.tmp, "ARCH")
    root = Path(target.root)
    if how == "marker":
        (root / "SENTINEL_DRIVE.json").write_text(json.dumps({"drive_uuid": "rev-ARCH", "role": "archive"}), "utf-8")
    elif how == "folder":
        (root / "Sentinel" / "CAM01" / "INDEX").mkdir(parents=True)
    else:
        key = ExportLedger(env.ledger).upsert_drive(target)
        ArchiveLedger(env.ledger, env.cfg.camera.id).sync_sessions({key}, keep_open=False)
    before = sorted(p.name for p in root.iterdir())
    res = _exporter(env, target).run()
    assert res.state == "FAILED" and res.code == "E-005" and "archive drive" in (res.detail or "")
    assert sorted(p.name for p in root.iterdir()) == before          # nothing written
    assert env.ledger.query("SELECT COUNT(*) n FROM export_batches")[0]["n"] == 0
    if how != "ledger":
        assert archive_drive_reason(root) is not None


def test_review_export_still_accepts_a_review_drive(env: Env) -> None:
    root = Path(_review_drive(env.tmp).root)
    (root / "SENTINEL_DRIVE.json").write_text(json.dumps({"drive_uuid": "x"}), "utf-8")
    (root / "Sentinel").mkdir()                       # an unrelated empty folder of that name is not enough
    assert archive_drive_reason(root) is None


# ------------------------------------------------------------------ finding 3
def test_removed_segment_is_never_queued_for_archiving_and_lost_copy_says_so(env: Env) -> None:
    ids = env.segments(3)
    loc = SwitchLocator(make_drive(env.tmp, "A"))
    w = env.worker(loc)
    drain(w)
    env.remove_locally(ids[:2])
    al = ArchiveLedger(env.ledger, env.cfg.camera.id)
    # this_drive policy: a new drive B would otherwise queue all three, and the removed ones forever fail
    b = make_drive(env.tmp, "B")
    loc.target = b
    w.step()
    drain(w)
    assert w.source_failed == {}
    assert "A-008" not in [a.code for a in w.alerts()]
    on_b = {r["segment_id"] for r in env.ledger.query("SELECT a.segment_id FROM archive_copies a JOIN drives d "
                                                       "ON d.drive_id=a.drive_id WHERE d.label='ARCHIVE_B'")}
    assert on_b == {ids[2]}
    assert al.backlog(None).segments == 0
    assert al.pending_segments(w.att.drive_id) == []
    copy = env.ledger.query("SELECT copy_id FROM archive_copies WHERE segment_id=? AND kind='original'", (ids[0],))
    al.mark_lost(copy[0]["copy_id"], "file missing from the drive")
    ev = env.events("archive_copy_lost")[-1]
    assert "cannot be copied again" in ev["message"] and "no other checked copy" in ev["message"]
    assert al.pending_segments(w.att.drive_id) == [] and al.open_problems()      # A-006 stays up instead


# ------------------------------------------------------------------ finding 4
def test_swapped_drive_is_not_written_again_after_a_restart(env: Env) -> None:
    env.segments(2)
    a = make_drive(env.tmp, "A")
    loc = SwitchLocator(a)
    w = env.worker(loc)
    drain(w)
    w.request_swap()
    assert w.step() == "swapped"
    assert (env.cfg.data_path / CONTROL_DIR / SWAPPED_STATE).exists()
    env.segments(1)                                    # recording continues
    copies = env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"]
    w2 = env.worker(loc)                               # archive process restarted, drive still inserted
    assert w2.step() == "waiting_for_removal"
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == copies
    loc.target = None                                  # now unplugged
    assert w2.step() == "waiting_for_removal"
    assert w2.step() == "absent"
    assert not (env.cfg.data_path / CONTROL_DIR / SWAPPED_STATE).exists()
    s = env.ledger.query("SELECT gone_how FROM archive_drive_sessions")
    assert [r["gone_how"] for r in s] == ["swapped"]   # finding 7 as well


def test_stale_swap_request_is_ignored(env: Env) -> None:
    env.segments(1)
    loc = SwitchLocator(make_drive(env.tmp, "A"))
    p = request_swap_file(env.cfg)
    p.write_text(iso_utc(time.time() - 6 * 3600), "utf-8")   # pressed while the archive process was down
    w = env.worker(loc)
    assert drain(w)[-1] == "idle"                      # copied normally, not swapped
    assert not p.exists() and env.events("archive_swap_request_expired")
    request_swap_file(env.cfg)                         # a fresh request still works
    assert w.step() == "swapped"


# ------------------------------------------------------------------ finding 5
class FlakyLocator:
    def __init__(self, target: ExportTarget) -> None:
        self.target = target
        self.miss = 0

    def scan(self) -> DriveScan:
        if self.miss:
            self.miss -= 1
            return DriveScan()
        return DriveScan([self.target])


def test_one_missed_scan_does_not_detach_the_attached_drive(env: Env) -> None:
    env.segments(1)
    loc = FlakyLocator(make_drive(env.tmp, "A"))
    w = env.worker(loc)
    drain(w)
    loc.miss = 1
    assert w.step() == "rescan"
    assert not w.removed_unexpectedly and "A-003" not in [a.code for a in w.alerts()]
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_drive_sessions WHERE gone_utc IS NULL")[0]["n"] == 1
    assert w.step() == "idle"
    assert env.events("archive_drive_removed") == []
    loc.miss = 2                                       # really gone: two scans in a row
    assert w.step() == "rescan"
    assert w.step() == "absent" and w.removed_unexpectedly


def test_unreadable_volume_list_is_uncertain_not_empty(env: Env) -> None:
    def boom() -> list[str]:
        raise OSError(21, "device not ready")

    scan = VolumeScanLocator(roots=boom).scan()
    assert scan.candidates == [] and scan.uncertain
    env.segments(1)
    target = make_drive(env.tmp, "A")
    loc = FlakyLocator(target)
    w = env.worker(loc)
    drain(w)
    loc.scan = lambda: DriveScan(uncertain="volume list unavailable")     # type: ignore[method-assign]
    for _ in range(5):
        assert w.step() == "rescan"
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_drive_sessions WHERE gone_utc IS NULL")[0]["n"] == 1


# ------------------------------------------------------------------ finding 6
class _Disk:
    def __init__(self, spool: Path) -> None:
        self.spool = spool

    def __call__(self, _p: Path) -> DiskUsage:
        return DiskUsage(1000 * GB, 1000 * GB, 0)      # always below the floor


def test_one_undeletable_file_does_not_block_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    e = Env(tmp_path, retention=True)
    try:
        ids = e.segments(3)
        loc = SwitchLocator(make_drive(e.tmp, "A"))
        w = e.worker(loc)
        drain(w)
        loc.target = None
        w.step()
        w.step()                                       # drive left the machine
        locked = e.ledger.get_segment(ids[0])["rel_path"].rsplit("/", 1)[-1]
        real_unlink = pathlib.Path.unlink

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self.name == locked:
                raise PermissionError(13, "The process cannot access the file because it is being used")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(pathlib.Path, "unlink", unlink)
        m = RetentionManager(e.cfg, e.ledger, disk_usage=_Disk(e.cfg.spool_path))
        rep = m.run_once()                             # must not raise
        assert rep.deleted == 2 and rep.delete_failed == 1 and rep.error
        rep = m.run_once()                             # still locked: skipped again, not an exception
        assert rep.delete_failed == 1
        monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
        rep = m.run_once()
        assert rep.completed_interrupted == 1 and rep.error is None
        assert e.ledger.query("SELECT COUNT(*) n FROM retention_deletions WHERE state='DELETING'")[0]["n"] == 0
    finally:
        e.close()


# ------------------------------------------------------------------ finding 8
def test_swap_request_file_is_seen_during_a_copy_and_max_wait_applies(tmp_path: Path) -> None:
    e = Env(tmp_path, swap_max_wait_s=0.001)
    try:
        ticks = [0.0]

        def mono() -> float:
            ticks[0] += 1.0
            return ticks[0]

        e.segments(1)
        loc = SwitchLocator(make_drive(e.tmp, "A"))
        w = e.worker(loc, mono=mono)
        drain(w)
        e.segments(1, start_epoch=time.time())
        request_swap_file(e.cfg)
        seg = w.al.pending_segments(w.att.drive_id, limit=1)[0]
        n = e.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"]
        w._archive_segment(seg)                        # the copy in flight polls the request file
        assert e.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == n   # abandoned, not claimed
        assert not (e.cfg.data_path / CONTROL_DIR / SWAP_REQUEST).exists()
        assert w.state == "SWAP_PENDING"
        assert w.step() == "swapped"
    finally:
        e.close()


# ------------------------------------------------------------------ finding 9
def test_late_segment_removed_before_export_is_named_once(env: Env) -> None:
    late = env.segments(2, start_epoch=time.time() - 3 * DAY)
    env.segments(2, start_epoch=time.time() - DAY)
    prev_end = iso_utc(time.time() - 2 * DAY)          # an earlier export already passed the late ones' time
    env.remove_locally(late)
    planner = Planner(env.ledger, env.cfg.spool_path, env.cfg.camera.id, ZoneInfo("UTC"))
    xl = ExportLedger(env.ledger)
    drive = xl.upsert_drive(_review_drive(env.tmp))
    plan = planner.plan(drive, prev_end, 10**12)
    assert plan is not None
    w = [x for x in plan.warnings if x["code"] == "late_footage_only_on_archive_drive"]
    assert len(w) == 1 and w[0]["segment_ids"] == late
    bid = xl.create_batch(camera_id=env.cfg.camera.id, drive_id=drive, interval_start_utc=prev_end,
                          interval_end_utc=plan.interval_end_utc, display_timezone="UTC", operator_id=None,
                          app_version="t", ffmpeg_version=None, stamp="s", plan=plan.to_json(), items=[])
    with env.ledger.tx() as c:
        c.execute("UPDATE export_batches SET state='COMPLETE' WHERE batch_id=?", (bid,))
    env.segments(1, start_epoch=time.time() - 600)
    again = planner.plan(drive, plan.interval_end_utc, 10**12)
    assert again is not None
    assert not [x for x in again.warnings if x["code"] == "late_footage_only_on_archive_drive"]


# ------------------------------------------------------------------ finding 10
def test_review_drive_without_archive_drive_raises_no_archive_alert(env: Env) -> None:
    env.segments(1)
    review = make_drive(env.tmp, "R", marker=True, role=None)
    (Path(review.root) / "README_FIRST.txt").write_text("review drive", "utf-8")
    w = env.worker(VolumeScanLocator(roots=lambda: [str(review.root)]))
    assert w.step() == "absent"
    assert w.state == "ABSENT" and "A-005" not in [a.code for a in w.alerts()]
    scan = VolumeScanLocator(roots=lambda: [str(review.root)]).scan()
    assert scan.unusable == [] and len(scan.ignored) == 1


# ------------------------------------------------------------------ recorder gap reasons (coordinator)
def test_new_gap_reasons_have_plain_wording() -> None:
    assert GAP_REASONS["resuming"] == "Recording was starting again after a planned pause"
    assert GAP_REASONS["recorder_not_running"] == "The recorder was not running"


# ------------------------------------------------------------------ operator request folder (coordinator)
def _op_env(tmp: Path) -> Env:
    e = Env.__new__(Env)
    e.tmp = tmp
    a = {"enabled": True, "require_drive_marker": False, "reserve_free_mb": 0.0, "poll_interval_s": 0.05,
         "chunk_mib": 1, "fsync_every_mib": 2, "max_rate_mb_s": 1000.0, "resume_policy": "this_drive"}
    e.cfg = make_config(tmp, archive=a, storage={"status_dir": str(tmp / "status")})
    e.ledger = Ledger(e.cfg.data_path / "sentinel.db")
    return e


def test_operator_request_folder_swap_and_resume(tmp_path: Path) -> None:
    from sentinel.archive.worker import operator_request_dir, request_resume_file

    e = _op_env(tmp_path)
    try:
        assert operator_request_dir(e.cfg) == Path(e.cfg.storage.status_dir) / "requests"
        e.segments(1)
        loc = SwitchLocator(make_drive(e.tmp, "A"))
        w = e.worker(loc)
        drain(w)
        p = request_swap_file(e.cfg, operator=True)
        assert p.parent == operator_request_dir(e.cfg)
        assert w.step() == "swapped" and not p.exists()
        ev = e.events("archive_swap_requested")[-1]
        assert "operator_folder" in ev["detail"]
        r = request_resume_file(e.cfg, operator=True)
        w.step()
        assert not r.exists() and w._swapped_drive is None
        assert "operator_folder" in e.events("archive_resume_requested")[-1]["detail"]
    finally:
        e.close()


@pytest.mark.parametrize("bad", ["old", "future", "garbage", "large", "folder"])
def test_operator_request_folder_is_untrusted(tmp_path: Path, bad: str) -> None:
    from sentinel.archive.worker import operator_request_dir

    e = _op_env(tmp_path)
    try:
        e.segments(1)
        w = e.worker(SwitchLocator(make_drive(e.tmp, "A")))
        drain(w)
        d = operator_request_dir(e.cfg)
        d.mkdir(parents=True, exist_ok=True)
        p = d / SWAP_REQUEST
        if bad == "old":
            p.write_text(iso_utc(time.time() - 3600), "utf-8")
        elif bad == "future":
            p.write_text(iso_utc(time.time() + 3600), "utf-8")
        elif bad == "garbage":
            p.write_text("please delete everything", "utf-8")
        elif bad == "large":
            p.write_text(iso_utc(time.time()) + " " * 2000, "utf-8")
        else:
            p.mkdir()
        assert w.step() == "idle"                      # not swapped
        assert w._swapped_drive is None and not w._swap.is_set()
        assert e.events("archive_swap_request_ignored")
        if bad == "folder":
            assert p.is_dir()                          # a folder is left alone, never removed
        else:
            assert not p.exists()                      # consumed
    finally:
        e.close()


def test_operator_request_symlink_is_not_followed(tmp_path: Path) -> None:
    from sentinel.archive.worker import operator_request_dir

    e = _op_env(tmp_path)
    try:
        e.segments(1)
        w = e.worker(SwitchLocator(make_drive(e.tmp, "A")))
        drain(w)
        d = operator_request_dir(e.cfg)
        d.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "precious.txt"
        target.write_text(iso_utc(time.time()), "utf-8")
        try:
            os.symlink(target, d / SWAP_REQUEST)
        except OSError:
            pytest.skip("creating symbolic links needs a privilege on this system")
        assert w.step() == "idle" and not w._swap.is_set()
        assert target.exists() and not os.path.lexists(d / SWAP_REQUEST)
    finally:
        e.close()
