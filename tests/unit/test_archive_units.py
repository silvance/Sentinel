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
from sentinel.archive.bench import add_synthetic_run
from sentinel.archive.index import parse_journal
from sentinel.archive.io import ArchiveIO, Throttle
from sentinel.archive.ledger import ArchiveLedger
from sentinel.archive.locator import ConfiguredPathLocator
from sentinel.archive.status import (Condition, backlog_text, evaluate_alerts, read_archive_status, recorder_view)
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


def test_unprepared_drive_is_refused(env: Env, tmp_path: Path) -> None:
    env.segments(1)
    plain = tmp_path / "plain_drive"
    plain.mkdir()
    w = env.worker(ConfiguredPathLocator(plain, require_marker=True))
    assert w.step() == "unusable"
    assert env.status()["state"] == "UNUSABLE" and "A-005" in env.codes()
    assert list(plain.iterdir()) == []                      # nothing written
    marked = make_drive(tmp_path, "M", marker=True)
    w2 = env.worker(ConfiguredPathLocator(marked.root, require_marker=True))
    assert drain(w2)[-1] == "idle"
    assert len(env.ledger.query("SELECT * FROM archive_copies")) == 1


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
        gaps_csv = (b.root / layout.GAPS_CSV).read_text("utf-8")
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
        journal = parse_journal((a.root / layout.JOURNAL).read_bytes())
        assert len(journal) == 7
    finally:
        env.close()


def test_journal_ignores_torn_last_line() -> None:
    sha = "a" * 64
    good = f"2026-11-09T00:00:00.000000Z,id1,original,{sha},10,Sentinel/x.ts\n"
    data = ("verified_utc,segment_id,kind,sha256,size_bytes,path\n" + good + good[:30]).encode()
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
    assert msg == ("Error A-002: The archive drive is full. Recording continues on this system. "
                   "Replace it with an empty archive drive.")


def test_recorder_view_reports_stale_archive_status(tmp_path: Path) -> None:
    view, alerts = recorder_view(tmp_path, 5.0)
    assert not view["running"] and [a.code for a in alerts] == ["A-007"]
    (tmp_path / "archive_status.json").write_text(json.dumps({
        "heartbeat_epoch": time.time(), "state": "FULL", "alerts": [{"code": "A-002", "severity": 2}],
        "backlog": {"text": "Not yet archived: 3.0 h"}}), "utf-8")
    view, alerts = recorder_view(tmp_path, 5.0)
    assert view["running"] and [(a.code, a.severity) for a in alerts] == [("A-002", 2)]
    view, alerts = recorder_view(tmp_path, 5.0, now=time.time() + 3600)
    assert [a.code for a in alerts] == ["A-007"]


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
        make_config(tmp_path, archive={"enabled": True})
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
