"""Archive drive (S1, docs/04 §2-3) without FFmpeg: synthetic sealed segments in a real ledger,
temporary folders as drives, fault-injecting drive I/O."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from archive_helpers import FakeEjector, FaultyIO, SwitchLocator, check_drive, claimed, drain, make_drive, sha_file
from conftest import make_config
from sentinel.alerts import CATALOG, Severity
from sentinel.archive import layout
from tools.archive_bench import add_synthetic_run
from sentinel.archive.index import parse_journal
from sentinel.archive.io import ArchiveIO, Throttle
from sentinel.archive.ledger import ArchiveLedger
from sentinel.archive.locator import VolumeScanLocator
from sentinel.archive.status import (Condition, RecorderArchiveView, backlog_text, evaluate_alerts,
                                     read_archive_status)
from sentinel.archive.worker import ArchiveWorker, request_swap_file
from sentinel.config import ConfigError
from sentinel.db import Ledger
from sentinel.models import SegmentState
from sentinel.export.target import ManualRemovalFallback

MIB = 1024 * 1024


class Env:
    def __init__(self, tmp: Path, **archive: Any) -> None:
        self.tmp = tmp
        a = {"enabled": True, "drive_path": str(tmp / "unused"), "require_drive_marker": False,
             "reserve_free_mb": 0.0, "poll_interval_s": 0.05, "chunk_mib": 1, "fsync_every_mib": 2,
             "max_rate_mb_s": 1000.0}
        a.update(archive)
        self.cfg = make_config(tmp, archive=a)
        self.ledger = Ledger(self.cfg.data_path / "sentinel.db")

    def segments(self, n: int, size: int = 300_000, **kw: Any) -> list[str]:
        return add_synthetic_run(self.ledger, self.cfg, n, size, **kw)

    def worker(self, locator: Any, io: ArchiveIO | None = None, **kw: Any) -> ArchiveWorker:
        return ArchiveWorker(self.cfg, ledger=self.ledger, locator=locator, io=io, allow_same_volume=True,
                             lower_own_priority=False, **kw)

    def status(self) -> dict[str, Any]:
        doc = read_archive_status(self.cfg.data_path / "archive_status.json")
        assert doc is not None
        return doc

    def codes(self) -> dict[str, int]:
        return {a["code"]: a["severity"] for a in self.status()["alerts"]}

    def close(self) -> None:
        self.ledger.close()


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    e = Env(tmp_path)
    yield e
    e.close()


def _spool_snapshot(env: Env) -> dict[str, tuple[str, bool]]:
    out = {}
    for p in env.cfg.spool_path.rglob("*"):
        if p.is_file():
            out[str(p)] = (sha_file(p), not (os.stat(p).st_mode & stat.S_IWRITE))
    return out


# ------------------------------------------------------------------ basic copy
def test_backlog_drains_oldest_first_verified_and_local_untouched(env: Env) -> None:
    ids = env.segments(5, size=3 * MIB + 17, sidecars=True)
    before = _spool_snapshot(env)
    a = make_drive(env.tmp, "A")
    io = FaultyIO()
    w = env.worker(SwitchLocator(a, io), io=io)
    assert drain(w)[-1] == "idle"
    rows = env.ledger.query("SELECT * FROM archive_copies ORDER BY copy_id")
    assert [r["segment_id"] for r in rows if r["kind"] == "original"] == ids          # oldest first
    assert len([r for r in rows if r["kind"] == "framehash"]) == 5
    assert all(r["verify_method"].startswith("windows-unbuffered") or os.name != "nt" for r in rows)
    drive_id = rows[0]["drive_id"]
    doc = check_drive(a.root, env.ledger, drive_id, complete=False)   # journal covers copies since the attach index
    assert doc["coverage"]["media_files"] == 0
    st = env.status()
    assert st["state"] == "UP_TO_DATE" and st["backlog"]["seconds"] == 0
    assert "Not yet archived: 0 h" in st["message"] and st["alerts"] == []
    w.request_swap()
    assert w.step() == "swapped"
    doc = check_drive(a.root, env.ledger, drive_id, complete=True)
    assert doc["coverage"]["media_files"] == 5 and doc["coverage"]["sidecar_files"] == 5
    assert io.fsyncs >= 5                    # periodic fsync (every 2 MiB in this test) happened
    assert _spool_snapshot(env) == before    # local recordings unchanged, still read-only, none deleted
    for r in rows:                           # archive copies sealed read-only
        assert not os.stat(a.root / r["path"]).st_mode & stat.S_IWRITE
    assert (a.root / layout.README).is_file()
    # quarantine / originals in the documented layout, UTC names
    assert all(r["path"].startswith(f"Sentinel/{env.cfg.camera.id}/") for r in rows)
    assert all("Z_" in Path(r["path"]).name for r in rows)


def test_corrupt_segment_goes_to_quarantine(env: Env) -> None:
    env.segments(1)
    bad = env.segments(1, state=SegmentState.CORRUPT)
    a = make_drive(env.tmp, "A")
    w = env.worker(SwitchLocator(a))
    drain(w)
    rows = {r["segment_id"]: r for r in env.ledger.query("SELECT * FROM archive_copies")}
    assert rows[bad[0]]["kind"] == "quarantine"
    assert rows[bad[0]]["path"].startswith(f"Sentinel/{env.cfg.camera.id}/Quarantine/")
    check_drive(a.root, env.ledger, rows[bad[0]]["drive_id"], complete=False)


def test_drive_absent_then_inserted(env: Env) -> None:
    env.segments(3)
    loc = SwitchLocator(None)
    w = env.worker(loc)
    assert w.step() == "absent"
    st = env.status()
    assert st["state"] == "ABSENT" and st["drive"] == "absent"
    assert "A-001" in env.codes() and st["backlog"]["segments"] == 3
    assert "Not yet archived: 0.2 h" in st["message"]          # 3 x 300 s = 0.25 h
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == 0
    a = make_drive(env.tmp, "A")
    loc.target = a
    assert drain(w)[-1] == "idle"
    assert len(claimed(env.ledger, w.al.ledger.query("SELECT drive_id FROM drives")[0]["drive_id"])) == 3
    assert env.codes() == {}


# ------------------------------------------------------------------ removal / faults
def test_unplugged_mid_write_never_claimed_and_resumes(env: Env) -> None:
    ids = env.segments(4, size=3 * MIB)
    a = make_drive(env.tmp, "A")
    io = FaultyIO(unplug_on_write_file=3)
    w = env.worker(SwitchLocator(a, io), io=io)
    drain(w)
    drive_id = env.ledger.query("SELECT drive_id FROM drives")[0]["drive_id"]
    got = claimed(env.ledger, drive_id)
    assert len(got) == 2                                         # the third file was in flight
    partials = list(a.root.rglob("*.partial"))
    assert len(partials) == 1                                    # left behind by the "unplug"
    assert w.step() == "absent"
    assert "A-003" in env.codes()
    assert "removed without preparing" in env.status()["message"]
    check_drive(a.root, env.ledger, drive_id, complete=False)    # what was verified is documented
    io.unplugged = False                                         # re-inserted
    io.unplug_on_write_file = None
    assert drain(w)[-1] == "idle"
    assert not list(a.root.rglob("*.partial"))
    rows = env.ledger.query("SELECT * FROM archive_copies ORDER BY copy_id")
    assert [r["segment_id"] for r in rows] == ids and all(r["already_present"] == 0 for r in rows)
    check_drive(a.root, env.ledger, drive_id, complete=False)
    assert env.codes() == {}
    ev = [r["code"] for r in env.ledger.query("SELECT code FROM events WHERE category='archive'")]
    assert "archive_drive_removed" in ev


def test_unplugged_during_verification_reverified_on_return(env: Env) -> None:
    a = make_drive(env.tmp, "A")
    io = FaultyIO(unplug_on_read_file=None)
    w = env.worker(SwitchLocator(a, io), io=io)
    assert w.step() == "idle"                # attach (index read-backs happen here), nothing to copy yet
    env.segments(2, size=2 * MIB)
    io.unplug_on_read_file = io.read_files + 1
    w.step()                                 # copy 1: read-back interrupted
    drive_id = env.ledger.query("SELECT drive_id FROM drives")[0]["drive_id"]
    assert claimed(env.ledger, drive_id) == {}
    finals = [p for p in a.root.rglob("*.ts")]
    assert len(finals) == 1                  # renamed into place but NOT claimed
    io.unplugged = False
    io.unplug_on_read_file = None
    drain(w)
    rows = env.ledger.query("SELECT * FROM archive_copies ORDER BY copy_id")
    assert len(rows) == 2 and rows[0]["already_present"] == 1 and rows[1]["already_present"] == 0
    check_drive(a.root, env.ledger, drive_id, complete=False)


def test_io_error_mid_write_with_drive_present_is_retried(env: Env) -> None:
    env.segments(2, size=3 * MIB)
    a = make_drive(env.tmp, "A")
    io = FaultyIO(eio_on_write_file=1)
    w = env.worker(SwitchLocator(a, io), io=io)
    with pytest.raises(OSError):
        w.step()                              # attach, then copy 1 fails: unexpected drive error surfaces (run loop logs and retries)
    drive_id = env.ledger.query("SELECT drive_id FROM drives")[0]["drive_id"]
    assert claimed(env.ledger, drive_id) == {}
    assert not list(a.root.rglob("*.partial"))      # discarded by write_new
    drain(w)
    assert len(claimed(env.ledger, drive_id)) == 2
    check_drive(a.root, env.ledger, drive_id, complete=False)


def test_drive_full_before_copy(env: Env) -> None:
    env.segments(2, size=2 * MIB)
    a = make_drive(env.tmp, "A")
    io = FaultyIO(free=1 * MIB)
    w = env.worker(SwitchLocator(a, io), io=io)
    out = drain(w)
    assert out[-1] == "full"
    assert w.step() == "full"
    st = env.status()
    assert st["state"] == "FULL" and st["drive"] == "full"
    assert env.codes().get("A-002") == Severity.WARNING
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == 0
    assert not list(a.root.rglob("*.ts")) and not list(a.root.rglob("*.partial"))
    io.free = None                            # space freed (or: a different drive) -> continues
    assert drain(w)[-1] == "idle"
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == 2


def test_enospc_mid_copy_no_partial_claimed_then_next_drive(env: Env) -> None:
    ids = env.segments(3, size=3 * MIB)
    a = make_drive(env.tmp, "A")
    io = FaultyIO(quota=8 * MIB)              # fits two segments, fails inside the third
    loc = SwitchLocator(a, io)
    w = env.worker(loc, io=io)
    drain(w)
    assert w.state == "FULL" and "A-002" in env.codes()
    da = env.ledger.query("SELECT drive_id FROM drives")[0]["drive_id"]
    assert len(claimed(env.ledger, da)) == 2
    assert not list(a.root.rglob("*.partial"))
    check_drive(a.root, env.ledger, da, complete=False)
    b = make_drive(env.tmp, "B")
    loc.io, loc.target = None, b
    w.io = ArchiveIO(read_chunk=MIB)
    drain(w)
    db = [r["drive_id"] for r in env.ledger.query("SELECT drive_id FROM drives") if r["drive_id"] != da][0]
    assert [r["segment_id"] for r in env.ledger.query("SELECT * FROM archive_copies WHERE drive_id=? ORDER BY "
                                                      "copy_id", (db,))] == ids   # this_drive: from the oldest
    check_drive(b.root, env.ledger, db, complete=False)


def test_full_and_backlog_over_threshold_escalate(tmp_path: Path) -> None:
    env = Env(tmp_path, backlog_alert_hours=0.1)
    try:
        env.segments(3)                       # 0.25 h of footage
        a = make_drive(env.tmp, "A")
        w = env.worker(SwitchLocator(a, FaultyIO(free=0)), io=FaultyIO(free=0))
        drain(w)
        codes = env.codes()
        assert codes["A-004"] == Severity.CRITICAL and codes["A-002"] == Severity.CRITICAL
        assert env.status()["alert"]["code"] in ("A-004", "A-002")
        assert not any(a["recording_stopped"] for a in env.status()["alerts"])
    finally:
        env.close()


def test_read_back_mismatch_moved_aside_never_claimed(tmp_path: Path) -> None:
    env = Env(tmp_path, verify_attempts=2)
    try:
        env.segments(2, size=MIB)
        a = make_drive(env.tmp, "A")
        io = FaultyIO(corrupt_writes=99)
        w = env.worker(SwitchLocator(a, io), io=io)
        drain(w, 50)
        assert env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == 0
        assert env.codes().get("A-006") == Severity.CRITICAL
        aside = list(a.root.rglob("*.unverified-*"))
        assert len(aside) == 4                # 2 segments x 2 attempts, kept (never deleted), never claimed
        did = env.ledger.query("SELECT drive_id FROM drives")[0]["drive_id"]
        doc = check_drive(a.root, env.ledger, did)
        assert doc["copies"] == []
        assert w.state == "ATTENTION"
    finally:
        env.close()


def test_changed_local_recording_is_not_archived(env: Env) -> None:
    ids = env.segments(2, size=MIB)
    seg = env.ledger.get_segment(ids[0])
    p = env.cfg.spool_path / seg["rel_path"]
    os.chmod(p, stat.S_IREAD | stat.S_IWRITE)          # simulate tampering / bit rot (test data only)
    with open(p, "r+b") as fh:
        fh.write(b"X")
    a = make_drive(env.tmp, "A")
    w = env.worker(SwitchLocator(a))
    drain(w)
    rows = env.ledger.query("SELECT segment_id FROM archive_copies")
    assert [r["segment_id"] for r in rows] == [ids[1]]
    assert env.codes().get("A-008") == Severity.CRITICAL
    assert not list(a.root.rglob("*.partial")) and len(list(a.root.rglob("*.ts"))) == 1


def test_existing_identical_file_reverified_not_copied(env: Env) -> None:
    env.segments(1, size=MIB)
    a = make_drive(env.tmp, "A")
    drain(env.worker(SwitchLocator(a)))
    env.ledger.query("DELETE FROM archive_copies")          # e.g. ledger restored from an older backup
    drain(env.worker(SwitchLocator(a)))
    rows = env.ledger.query("SELECT * FROM archive_copies")
    assert len(rows) == 1 and rows[0]["already_present"] == 1


def test_unprepared_or_non_archive_drive_is_never_written(env: Env, tmp_path: Path) -> None:
    """Review finding 3: only a marker with role 'archive' is used; blank / role-less / review drives never."""
    env.segments(1)
    plain = tmp_path / "plain_drive"
    plain.mkdir()
    w = env.worker(VolumeScanLocator(roots=lambda: [str(plain)]))
    assert w.step() == "absent"                             # a blank drive is not an archive drive
    assert list(plain.iterdir()) == []
    norole = make_drive(tmp_path, "N", marker=True, role=None)
    review = make_drive(tmp_path, "R", marker=True, role="review")
    w = env.worker(VolumeScanLocator(roots=lambda: [str(norole.root), str(review.root)]))
    assert w.step() == "unusable"
    assert env.status()["state"] == "UNUSABLE" and "A-005" in env.codes()
    assert sorted(p.name for p in norole.root.iterdir()) == ["SENTINEL_DRIVE.json"]   # nothing written
    assert sorted(p.name for p in review.root.iterdir()) == ["SENTINEL_DRIVE.json"]
    marked = make_drive(tmp_path, "M", marker=True)
    (marked.root / "README_FIRST.txt").write_text("review drive", "utf-8")     # review layout on an 'archive' drive
    w = env.worker(VolumeScanLocator(roots=lambda: [str(marked.root)]))
    assert w.step() == "unusable" and "review-drive layout" in env.status()["problems"]["unusable_reason"]
    (marked.root / "README_FIRST.txt").unlink()
    w2 = env.worker(VolumeScanLocator(roots=lambda: [str(marked.root)]))
    assert drain(w2)[-1] == "idle"
    assert len(env.ledger.query("SELECT * FROM archive_copies")) == 1


def test_designate_adds_archive_role_and_refuses_review_drives(env: Env, tmp_path: Path) -> None:
    from sentinel.archive.locator import DriveUnusable, designate_archive, read_marker

    d = make_drive(tmp_path, "D", marker=True, role=None)
    designate_archive(d.root, "admin-1")
    m = read_marker(d.root)
    assert m["role"] == "archive" and m["role_set_by"] == "admin-1" and m["drive_uuid"]
    r = make_drive(tmp_path, "R2", marker=True, role=None)
    (r.root / "Manifests").mkdir()
    with pytest.raises(DriveUnusable):
        designate_archive(r.root, "admin-1")
    x = make_drive(tmp_path, "X", marker=True, role=None)
    with pytest.raises(DriveUnusable):
        designate_archive(x.root, "admin-1", has_review_batches=lambda key: True)
    with pytest.raises(DriveUnusable):
        designate_archive(make_drive(tmp_path, "E", marker=True, role="review").root, "admin-1")


def test_drive_found_by_marker_not_by_letter_new_drive_before_old_removed(env: Env, tmp_path: Path) -> None:
    """Review finding 3: B inserted before A is removed (would get another letter) is still found, the
    swapped A is never written again, and a different disk at A's old location is not mistaken for A."""
    env.segments(2)
    a, b = make_drive(tmp_path, "A", marker=True), make_drive(tmp_path, "B", marker=True)
    roots = [str(a.root)]
    w = env.worker(VolumeScanLocator(roots=lambda: list(roots)), remover=FakeEjector())
    drain(w)
    w.request_swap()
    assert w.step() == "swapped"
    roots.append(str(b.root))                     # B inserted while A is still connected
    env.segments(1)
    drain(w)
    ids = _drive_ids(env)
    assert len(ids) == 2
    assert len(claimed(env.ledger, ids[0])) == 2 and len(claimed(env.ledger, ids[1])) == 3
    roots[:] = [str(b.root)]
    w.step()
    w.step()
    assert w.att is not None and w.att.drive_id == ids[1]


def test_live_volume_enumeration_lists_the_volume_of_this_folder(tmp_path: Path) -> None:
    """Read-only enumeration (no file on any real volume is opened)."""
    if os.name != "nt":
        pytest.skip("Windows volume enumeration")
    import ctypes

    from sentinel.archive.locator import windows_volume_roots
    k32 = ctypes.WinDLL("kernel32")
    buf, gbuf = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(64)
    k32.GetVolumePathNameW(str(tmp_path), buf, len(buf))
    k32.GetVolumeNameForVolumeMountPointW(buf.value, gbuf, len(gbuf))
    assert gbuf.value in windows_volume_roots()


def test_rename_rolled_back_by_unplug_is_restored_not_deleted(env: Env) -> None:
    """Review finding 1 (repro): a verified copy whose rename was rolled back to '.partial' by an
    unclean unplug must be re-verified and restored on attach — never deleted with the strays."""
    env.segments(2, size=2 * MIB)
    a = make_drive(env.tmp, "A")
    drain(env.worker(SwitchLocator(a)))
    did = _drive_ids(env)[0]
    rows = env.ledger.query("SELECT * FROM archive_copies ORDER BY copy_id")
    p0 = a.root / rows[0]["path"]
    os.chmod(p0, stat.S_IREAD | stat.S_IWRITE)
    os.rename(p0, p0.with_name(p0.name + ".partial"))          # what the file system rolled back to
    p1 = a.root / rows[1]["path"]
    os.chmod(p1, stat.S_IREAD | stat.S_IWRITE)
    os.rename(p1, p1.with_name(p1.name + ".partial"))
    with open(p1.with_name(p1.name + ".partial"), "r+b") as fh:  # ...and this one was damaged too
        fh.write(b"\xff")
    w = env.worker(SwitchLocator(a))
    w.step()                                                    # attach: reconcile, then copy again
    assert p0.is_file() and sha_file(p0) == rows[0]["sha256"]   # restored
    states = {r["copy_id"]: r["state"] for r in env.ledger.query("SELECT * FROM archive_copies")}
    assert states[rows[0]["copy_id"]] == "VERIFIED" and states[rows[1]["copy_id"]] == "LOST"
    drain(w)
    assert len(claimed(env.ledger, did)) == 2                   # the lost one was copied again
    assert not list(a.root.rglob("*.partial"))
    assert env.codes().get("A-006") == Severity.CRITICAL        # loss is reported and stays reported
    w.request_swap()
    w.step()
    check_drive(a.root, env.ledger, did, complete=True)


def test_missing_copy_is_marked_lost_requeued_and_counted(env: Env) -> None:
    """Review finding 1c: claimed-but-missing → LOST (row kept), back in the backlog, not in the index,
    persistent A-006."""
    env.segments(2, size=MIB)
    a = make_drive(env.tmp, "A")
    drain(env.worker(SwitchLocator(a)))
    did = _drive_ids(env)[0]
    row = env.ledger.query("SELECT * FROM archive_copies ORDER BY copy_id")[0]
    p = a.root / row["path"]
    os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
    p.unlink()                                                  # deleted elsewhere / lost on the drive
    loc = SwitchLocator(None)
    w = env.worker(loc)
    w.step()
    assert env.status()["backlog"]["segments"] == 0
    loc.target = a
    w.step()                                                    # attach + reconcile + recopy
    lost = env.ledger.query("SELECT * FROM archive_copies WHERE copy_id=?", (row["copy_id"],))[0]
    assert lost["state"] == "LOST" and lost["lost_reason"]
    assert len(env.ledger.query("SELECT * FROM archive_copies WHERE segment_id=?", (row["segment_id"],))) == 2
    assert p.is_file()                                          # copied again
    assert env.codes().get("A-006") == Severity.CRITICAL


def test_lost_copy_counts_in_backlog_until_recopied(env: Env) -> None:
    ids = env.segments(1)
    a = make_drive(env.tmp, "A")
    drain(env.worker(SwitchLocator(a)))
    al = ArchiveLedger(env.ledger, env.cfg.camera.id)
    assert al.backlog(None).segments == 0
    al.mark_lost(env.ledger.query("SELECT copy_id FROM archive_copies")[0]["copy_id"], "test")
    assert al.backlog(None).segments == 1 and al.pending_segments(_drive_ids(env)[0])[0]["segment_id"] == ids[0]


def test_directory_flush_refused_falls_back_to_fresh_listing(env: Env) -> None:
    """Review finding 1a: if the directory flush is refused, the copy is only recorded after a fresh
    directory listing shows the final name (and no .partial)."""
    env.segments(1, size=MIB)
    a = make_drive(env.tmp, "A")
    io = FaultyIO(refuse_dir_flush=True)
    drain(env.worker(SwitchLocator(a, io), io=io))
    assert len(env.ledger.query("SELECT * FROM archive_copies")) == 1
    assert io.dir_flushes >= 2


def test_directory_flush_really_runs_on_windows(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows directory flush")
    from sentinel.platform.windows import flush_directory
    assert flush_directory(tmp_path) is True
    assert ArchiveIO().fsync_dir(tmp_path) is True


def test_buffered_read_back_is_never_recorded_as_verified(env: Env) -> None:
    """Review finding 5."""
    from sentinel.archive.ledger import UnverifiableCopy
    from sentinel.export.fileio import M_BUFFERED

    ids = env.segments(2, size=MIB)
    a = make_drive(env.tmp, "A")
    io = FaultyIO(buffered_reads=True)
    w = env.worker(SwitchLocator(a, io), io=io)
    drain(w)
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == 0
    assert env.codes().get("A-006") == Severity.CRITICAL
    al = ArchiveLedger(env.ledger, env.cfg.camera.id)
    seg = env.ledger.get_segment(ids[0])
    with pytest.raises(UnverifiableCopy):
        al.record_verified(segment_id=ids[0], drive_id=w.att.drive_id, kind="original", path="x",
                           sha256=seg["sha256"], size_bytes=1, verify_method=M_BUFFERED)


def test_verify_alert_persists_after_removal_until_acknowledged(tmp_path: Path) -> None:
    """Review finding 7 (repro): A-006 must not clear when the drive is removed."""
    env = Env(tmp_path, verify_attempts=1)
    try:
        env.segments(1, size=MIB)
        a = make_drive(env.tmp, "A")
        loc = SwitchLocator(a, None)
        w = env.worker(loc, io=FaultyIO(corrupt_writes=99))
        drain(w)
        assert env.codes().get("A-006") == Severity.CRITICAL
        loc.target = None                                     # drive removed
        w.step()
        assert env.codes().get("A-006") == Severity.CRITICAL  # still shown
        w2 = env.worker(SwitchLocator(None))                  # even after a restart of the worker
        w2.step()
        assert env.codes().get("A-006") == Severity.CRITICAL
        al = ArchiveLedger(env.ledger, env.cfg.camera.id)
        with pytest.raises(ValueError):
            al.acknowledge("  ")
        assert al.acknowledge("admin-7") == 2                 # verify_failed + set_aside
        w2.step()
        w2._publish(force=True)
        assert "A-006" not in env.codes()
        ev = env.ledger.query("SELECT * FROM events WHERE code='archive_problems_acknowledged'")
        assert ev and ev[0]["severity"] == "audit" and "admin-7" in ev[0]["detail"]
    finally:
        env.close()


def test_set_aside_files_are_listed_in_the_index(tmp_path: Path) -> None:
    """Review finding 9: *.unverified-* files are listed deterministically, not left unexplained."""
    env = Env(tmp_path)
    try:
        env.segments(1, size=MIB)
        a = make_drive(env.tmp, "A")
        io = FaultyIO(corrupt_writes=1)                       # first write damaged, second fine
        w = env.worker(SwitchLocator(a, io), io=io, remover=FakeEjector())
        drain(w)
        w.request_swap()
        w.step()
        did = _drive_ids(env)[0]
        doc = check_drive(a.root, env.ledger, did, complete=True)
        aside = list(a.root.rglob("*.unverified-*"))
        assert len(aside) == 1
        assert [s["path"] for s in doc["set_aside_files"]] == [aside[0].relative_to(a.root).as_posix()]
        assert "A-006" not in env.codes()                     # one transient failure, then verified
    finally:
        env.close()


def test_index_carries_forward_entries_unknown_to_this_ledger(tmp_path: Path) -> None:
    """Review finding 4: a reinstalled ledger must not drop entries the drive already documents;
    other cameras' indexes are untouched (per-camera INDEX)."""
    first = Env(tmp_path / "one")
    a = make_drive(tmp_path, "A")
    try:
        first.segments(2, size=MIB)
        drain(first.worker(SwitchLocator(a)))
        w = first.worker(SwitchLocator(a), remover=FakeEjector())
        w.step()
        w.request_swap()
        w.step()
        old = json.loads((a.root / layout.index_files("CAM01")["json"]).read_bytes())
        assert len(old["copies"]) == 2
    finally:
        first.close()
    other = a.root / "Sentinel" / "CAM02" / "INDEX"
    other.mkdir(parents=True)
    (other / "archive_index.json").write_text("{}", "utf-8")
    second = Env(tmp_path / "two")                            # new, empty ledger (reinstall)
    try:
        ids = second.segments(1, size=MIB)
        w = second.worker(SwitchLocator(a), remover=FakeEjector())
        drain(w)
        w.request_swap()
        w.step()
        did = _drive_ids(second)[0]
        doc = check_drive(a.root, second.ledger, did, complete=True)
        assert {c["path"] for c in doc["carried_forward"]} == {c["path"] for c in old["copies"]}
        assert doc["carried_forward"][0]["ledger_instance"] == old["ledger_instance"] != doc["ledger_instance"]
        assert [c["segment_id"] for c in doc["copies"]] == ids
        assert (other / "archive_index.json").read_text("utf-8") == "{}"
        journal = parse_journal((a.root / layout.index_files("CAM01")["journal"]).read_bytes())
        assert {j["camera_id"] for j in journal} == {"CAM01"} and len(journal) == 3
    finally:
        second.close()


def test_journal_written_before_the_claim(env: Env) -> None:
    """Review finding 9: an unplug between the journal line and the ledger row leaves a journal line for a
    copy that IS verified on the drive (never a claim without documentation on the drive)."""
    env.segments(1, size=MIB)
    a = make_drive(env.tmp, "A")
    w = env.worker(SwitchLocator(a))
    order: list[str] = []
    real_append, real_record = w.io.append_line, None

    def append(path: Path, line: str) -> None:
        order.append("journal")
        real_append(path, line)
    w.io.append_line = append  # type: ignore[method-assign]
    w._open()
    real_record = w.al.record_verified

    def record(**kw: Any) -> str:
        order.append("ledger")
        return real_record(**kw)
    w.al.record_verified = record  # type: ignore[method-assign]
    drain(w)
    assert order[-2:] == ["journal", "ledger"]


def test_worker_survives_errors_inside_its_error_handler(env: Env) -> None:
    """Review finding 2 (repro): status write / ledger failing inside the error path must not end the worker."""
    env.segments(1)
    a = make_drive(env.tmp, "A")
    w = env.worker(SwitchLocator(a))
    calls = {"n": 0}
    real_step = w.step

    def bad_step() -> str:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("step failed")
        return real_step()
    w.step = bad_step  # type: ignore[method-assign]
    real_write = w.status.write

    def bad_write(**kw: Any) -> dict[str, Any]:
        if calls["n"] <= 2:
            raise OSError("status file unwritable")
        return real_write(**kw)
    w.status.write = bad_write  # type: ignore[method-assign]
    w.acfg = type(w.acfg)(**{**w.acfg.__dict__, "poll_interval_s": 0.01})
    w.start()
    end = time.monotonic() + 20
    while time.monotonic() < end and env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] < 1:
        time.sleep(0.05)
    assert w.alive
    w.stop(10)
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == 1


def test_recorder_view_computes_backlog_itself_when_archive_is_dead(env: Env) -> None:
    """Review finding 2: A-004 must fire from the recorder even if the archive process never ran."""
    env.segments(3)                                         # 0.25 h
    v = RecorderArchiveView(env.cfg.data_path, env.cfg.camera.id, 5.0, threshold_s=600, critical_after_s=1800)
    view, alerts = v.evaluate(env.ledger)
    codes = {a.code: a.severity for a in alerts}
    assert codes["A-004"] == Severity.CRITICAL and "A-007" in codes
    assert view["backlog"]["segments"] == 3 and view["backlog"]["source"] == "ledger"
    assert "Not yet archived: 0.2 h" in view["message"]


def test_ledger_writes_leave_background_mode() -> None:
    """Review finding 8: normal_priority() ends Windows background mode around a ledger write lock."""
    if os.name != "nt":
        pytest.skip("Windows background mode")
    from sentinel.archive import priority

    out: dict[str, Any] = {}

    def body() -> None:
        out["lower"] = priority.lower_priority(whole_process=False)
        with priority.normal_priority():
            out["inside"] = getattr(priority._bg, "depth", 0)
            with priority.normal_priority():       # nested: no second END
                out["nested"] = getattr(priority._bg, "depth", 0)
        out["after"] = getattr(priority._bg, "depth", 0)
    t = threading.Thread(target=body)
    t.start()
    t.join(10)
    assert out == {"lower": "windows-thread-background-mode", "inside": 1, "nested": 2, "after": 0}


def test_ledger_is_in_wal_mode(env: Env) -> None:
    assert env.ledger.query("PRAGMA journal_mode")[0][0].lower() == "wal"


def test_windows_service_plan_commands() -> None:
    from sentinel.archive.hosting import ACCOUNT, windows_service_commands

    cmds = windows_service_commands('"C:\\Sentinel\\host.exe" archive', Path("D:/spool"), Path("D:/data"),
                                    Path("D:/logs"))
    create = cmds[0]
    assert create[:3] == ["sc.exe", "create", "SentinelArchive"] and "delayed-auto" in create
    assert ACCOUNT in create and any(c[:2] == ["sc.exe", "failure"] for c in cmds)
    assert ["icacls", str(Path("D:/spool")), "/grant", f"{ACCOUNT}:(OI)(CI)RX"] in cmds   # spool read-only


def test_archive_onto_recording_or_system_volume_is_refused(env: Env) -> None:
    env.segments(1)
    a = make_drive(env.tmp, "A")             # same volume as spool, ledger and logs in this test
    w = ArchiveWorker(env.cfg, ledger=env.ledger, locator=SwitchLocator(a), lower_own_priority=False)
    assert w.step() == "unusable"
    st = env.status()
    assert st["state"] == "UNUSABLE" and "A-005" in env.codes()
    assert "recording storage" in st["problems"]["unusable_reason"]
    assert list(a.root.iterdir()) == []      # nothing written, not even the probe or the index
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == 0


# ------------------------------------------------------------------ swap
def _drive_ids(env: Env) -> list[str]:
    return [r["drive_id"] for r in env.ledger.query("SELECT drive_id FROM drives ORDER BY rowid")]


@pytest.mark.parametrize("policy", ["this_drive", "any_drive"])
def test_swap_then_next_drive_resumes_by_policy(tmp_path: Path, policy: str) -> None:
    env = Env(tmp_path, resume_policy=policy)
    try:
        first = env.segments(3)
        a, b = make_drive(env.tmp, "A"), make_drive(env.tmp, "B")
        loc = SwitchLocator(a)
        ej = FakeEjector()
        w = env.worker(loc, remover=ej)
        drain(w)
        w.request_swap()
        assert w.step() == "swapped"
        st = env.status()
        assert st["state"] == "SAFE_TO_REMOVE" and "SAFE TO REMOVE ARCHIVE DRIVE" in st["message"]
        assert ej.flushed == 1 and ej.ejected == 1
        da = _drive_ids(env)[0]
        idx_gen = env.ledger.query("SELECT MAX(generation) g FROM archive_indexes WHERE drive_id=?", (da,))[0]["g"]
        check_drive(a.root, env.ledger, da, complete=True)
        newer = env.segments(2)                      # recording continued meanwhile
        assert w.step() == "waiting_for_removal"     # the swapped drive is not written again
        assert len(claimed(env.ledger, da)) == 3
        loc.target = None
        assert w.step() == "waiting_for_removal"     # one empty scan is not yet proof that it was removed
        assert w.step() == "absent"
        assert env.codes().get("A-001") == Severity.NOTICE and "Insert the next archive drive" in env.status()["message"]
        loc.target = b
        drain(w)
        db = _drive_ids(env)[1]
        on_b = [r["segment_id"] for r in env.ledger.query(
            "SELECT segment_id FROM archive_copies WHERE drive_id=? ORDER BY copy_id", (db,))]
        if policy == "this_drive":
            assert on_b == first + newer
        check_drive(b.root, env.ledger, db, complete=False)
        w.request_swap()
        assert w.step() == "swapped"
        doc = check_drive(b.root, env.ledger, db, complete=True)
        if policy == "any_drive":
            assert on_b == newer
            assert doc["not_on_this_drive"] == []    # span on B starts at its first segment
        assert env.ledger.query("SELECT MAX(generation) g FROM archive_indexes WHERE drive_id=?", (da,))[0]["g"] == idx_gen
    finally:
        env.close()


def test_any_drive_index_names_segments_held_elsewhere(tmp_path: Path) -> None:
    env = Env(tmp_path, resume_policy="any_drive")
    try:
        ids = env.segments(4)
        a, b = make_drive(env.tmp, "A"), make_drive(env.tmp, "B")
        loc = SwitchLocator(a)
        w = env.worker(loc)
        w.step()                           # attach A + first segment to A
        loc.target = b                     # drive changed without a swap (counts as removed from A)
        w.step()                           # attach B + copy segment 2
        loc.target = a
        w.step()                           # back to A: attach + copy segment 3
        loc.target = b
        drain(w)                           # segment 4 to B
        w.request_swap()
        assert w.step() == "swapped"
        da, db = _drive_ids(env)
        doc_b = check_drive(b.root, env.ledger, db)
        held = {n["segment_id"]: n for n in doc_b["not_on_this_drive"]}
        assert held[ids[2]]["what"] == "on_another_archive_drive" and held[ids[2]]["other_drives"] == ["ARCHIVE_A"]
        gaps_csv = (b.root / layout.index_files("CAM01")["gaps"]).read_text("utf-8")
        assert "on_another_archive_drive" in gaps_csv and "ARCHIVE_A" in gaps_csv
    finally:
        env.close()


def test_swap_with_manual_removal_fallback(env: Env) -> None:
    env.segments(1)
    a = make_drive(env.tmp, "A")
    w = env.worker(SwitchLocator(a), remover=ManualRemovalFallback())
    drain(w)
    request_swap_file(env.cfg)                       # as the CLI / GUI would, from another process
    assert w.step() == "swapped"
    st = env.status()
    assert st["state"] == "REMOVE_MANUALLY" and "Safely Remove Hardware" in st["message"]
    assert st["message"].startswith("Do not unplug the archive drive yet")
    assert "A-0" not in json.dumps(st["alerts"])
    w.resume()                                       # operator decides to keep the drive in
    env.segments(1)
    assert drain(w)[-1] == "idle"
    assert len(env.ledger.query("SELECT * FROM archive_copies")) == 2


def test_swap_during_copy_finishes_file_in_flight(tmp_path: Path) -> None:
    env = Env(tmp_path, max_rate_mb_s=4.0, swap_max_wait_s=30.0)
    try:
        a = make_drive(env.tmp, "A")
        w = env.worker(SwitchLocator(a), remover=FakeEjector())
        assert w.step() == "idle"                    # attach
        env.segments(3, size=4 * MIB)               # ~1 s per file at 4 MB/s
        t = threading.Thread(target=w.step)
        t.start()
        time.sleep(0.3)                              # mid-copy of the first file
        w.request_swap()
        t.join(30)
        assert w.step() == "swapped"
        rows = env.ledger.query("SELECT * FROM archive_copies")
        assert len(rows) == 1                        # in-flight file finished and verified, nothing more
        assert not list(a.root.rglob("*.partial"))
        check_drive(a.root, env.ledger, rows[0]["drive_id"])
    finally:
        env.close()


def test_swap_during_copy_abandons_slow_file(tmp_path: Path) -> None:
    env = Env(tmp_path, max_rate_mb_s=2.0, swap_max_wait_s=0.2)
    try:
        a = make_drive(env.tmp, "A")
        w = env.worker(SwitchLocator(a), remover=FakeEjector())
        assert w.step() == "idle"                    # attach
        env.segments(1, size=8 * MIB)                # ~4 s at 2 MB/s, longer than the swap allows
        t = threading.Thread(target=w.step)
        t.start()
        time.sleep(0.3)
        w.request_swap()
        t.join(30)
        assert w.step() == "swapped"
        assert env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == 0
        assert not list(a.root.rglob("*.partial")) and not list(a.root.rglob("*.ts"))
        assert env.status()["state"] == "SAFE_TO_REMOVE"
        did = _drive_ids(env)[0]
        check_drive(a.root, env.ledger, did)
    finally:
        env.close()


def test_worker_thread_runs_and_stops_cleanly(env: Env) -> None:
    env.segments(3)
    a = make_drive(env.tmp, "A")
    w = env.worker(SwitchLocator(a)).start()
    end = time.monotonic() + 20
    while time.monotonic() < end and env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] < 3:
        time.sleep(0.05)
    w.stop(10)
    assert not w.alive
    assert env.status()["state"] == "STOPPED"
    assert env.ledger.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] == 3


# ------------------------------------------------------------------ index cadence
def test_index_rewritten_after_an_hour_of_footage(tmp_path: Path) -> None:
    env = Env(tmp_path, index_every_s=900.0)          # 3 segments of 300 s
    try:
        env.segments(7)
        a = make_drive(env.tmp, "A")
        w = env.worker(SwitchLocator(a))
        drain(w)
        did = _drive_ids(env)[0]
        gens = env.ledger.query("SELECT generation, reason, copies FROM archive_indexes WHERE drive_id=? "
                                "ORDER BY generation", (did,))
        assert [(g["reason"], g["copies"]) for g in gens] == [("attach", 0), ("hourly", 3), ("hourly", 6)]
        check_drive(a.root, env.ledger, did, complete=False)      # 7th documented by the journal
        journal = parse_journal((a.root / layout.index_files("CAM01")["journal"]).read_bytes())
        assert len(journal) == 7
    finally:
        env.close()


def test_journal_ignores_torn_last_line() -> None:
    sha = "a" * 64
    good = f"2026-11-09T00:00:00.000000Z,CAM01,id1,original,{sha},10,Sentinel/x.ts\n"
    data = ("verified_utc,camera_id,segment_id,kind,sha256,size_bytes,path\n" + good + good[:30]).encode()
    assert len(parse_journal(data)) == 1


# ------------------------------------------------------------------ status, alerts, throttle, config
def test_backlog_text() -> None:
    assert backlog_text(0) == "Not yet archived: 0 h"
    assert backlog_text(1800) == "Not yet archived: 0.5 h"
    assert backlog_text(26 * 3600) == "Not yet archived: 26 h"


def test_alert_rules_and_catalog() -> None:
    for code in ("A-001", "A-002", "A-003", "A-004", "A-005", "A-006", "A-007", "A-008"):
        d = CATALOG[code]
        assert not d.stops_recording
        assert "\\" not in d.headline + d.action and "/" not in d.headline + d.action
    c = Condition(state="ABSENT", backlog_s=10, threshold_s=100)
    assert [(a.code, a.severity) for a in evaluate_alerts(c)] == [("A-001", Severity.WARNING)]
    c = Condition(state="ABSENT", backlog_s=200, threshold_s=100, removed_unexpectedly=True)
    assert [a.code for a in evaluate_alerts(c)] == ["A-004", "A-003"]
    c = Condition(state="UP_TO_DATE", backlog_s=0, threshold_s=100)
    assert evaluate_alerts(c) == []
    msg = evaluate_alerts(Condition(state="FULL", backlog_s=0, threshold_s=1, full=True))[0].message
    assert msg == ("Error A-002: The archive drive is full. Recording continues on this system. Do not unplug the "
                   "archive drive yet: press Swap Archive Drive, wait for SAFE TO REMOVE, then insert an empty "
                   "archive drive.")


def test_recorder_view_reports_stale_archive_status(tmp_path: Path) -> None:
    v = RecorderArchiveView(tmp_path, "CAM01", 5.0, threshold_s=1e9, critical_after_s=1800)
    t0 = time.time()
    view, alerts = v.evaluate(None, now=t0)
    assert not view["running"] and [(a.code, a.severity) for a in alerts] == [("A-007", Severity.WARNING)]
    (tmp_path / "archive_status.json").write_text(json.dumps({
        "heartbeat_epoch": t0, "state": "FULL", "alerts": [{"code": "A-002", "severity": 2}],
        "backlog": {"text": "Not yet archived: 3.0 h"}}), "utf-8")
    view, alerts = v.evaluate(None, now=t0)
    assert view["running"] and [(a.code, a.severity) for a in alerts] == [("A-002", 2)]
    view, alerts = v.evaluate(None, now=t0 + 600)
    assert [(a.code, a.severity) for a in alerts] == [("A-007", Severity.WARNING)]
    view, alerts = v.evaluate(None, now=t0 + 2000)       # down for more than 30 min: critical
    assert [(a.code, a.severity) for a in alerts] == [("A-007", Severity.CRITICAL)]

def test_throttle_limits_rate() -> None:
    t = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        t[0] += s
    th = Throttle(10e6, mono=lambda: t[0], sleep=sleep)
    for _ in range(10):
        th.consume(1_000_000)
    assert abs(t[0] - 1.0) < 1e-6          # 10 MB at 10 MB/s takes 1 s
    t[0] += 5.0                            # idle: no credit carried over
    th.consume(1_000_000)
    assert abs(slept[-1] - 0.1) < 1e-6


def test_config_validation(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        make_config(tmp_path, archive={"resume_policy": "sometimes"})
    with pytest.raises(ConfigError):
        make_config(tmp_path, archive={"chunk_mib": 8, "fsync_every_mib": 4})
    with pytest.raises(ConfigError):
        make_config(tmp_path, archive={"unknown_key": 1})
    cfg = make_config(tmp_path, archive={"enabled": True, "drive_path": "arch"})
    assert Path(cfg.archive.drive_path).is_absolute() and cfg.archive.chunk_mib == 8
    assert cfg.archive.fsync_every_mib == 64 and cfg.archive.resume_policy == "this_drive"


def test_ledger_refuses_unverified_hash(env: Env) -> None:
    ids = env.segments(1)
    a = make_drive(env.tmp, "A")
    w = env.worker(SwitchLocator(a))
    w.step()
    al = ArchiveLedger(env.ledger, env.cfg.camera.id)
    with pytest.raises(ValueError):
        al.record_verified(segment_id=ids[0], drive_id=w.att.drive_id, kind="original", path="x", sha256="0" * 64,
                           size_bytes=1, verify_method="test")


def test_hash_of_spool_matches_archive_bytes(env: Env) -> None:
    ids = env.segments(1, size=5 * MIB + 3)
    a = make_drive(env.tmp, "A")
    drain(env.worker(SwitchLocator(a)))
    seg = env.ledger.get_segment(ids[0])
    row = env.ledger.query("SELECT * FROM archive_copies")[0]
    assert (a.root / row["path"]).read_bytes() == (env.cfg.spool_path / seg["rel_path"]).read_bytes()
    assert hashlib.sha256((a.root / row["path"]).read_bytes()).hexdigest() == seg["sha256"]
