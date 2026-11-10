"""Archive copies in the evidence ledger (tables in ``db.ARCHIVE_DDL``).

Invariant (enforced by the only writer, :meth:`ArchiveLedger.record_verified`): a VERIFIED
``archive_copies`` row means "this file was read back from that drive, bypassing the OS cache, and
its SHA-256 equals the hash recorded when the segment was sealed". Nothing is recorded before
verification, a buffered (cache-served) read-back is refused, and a copy later found missing or
damaged is marked LOST (row kept) so the segment is copied again and counts as not archived.

``archive_problems`` holds conditions that must stay visible to the operator (alert A-006) until an
administrator acknowledges them (audit event), even after the drive has been removed.

All writes leave Windows background mode while they hold the SQLite write lock
(:func:`sentinel.archive.priority.normal_priority`) and are retried on "database is locked".
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..clock import iso_utc
from ..db import Ledger
from ..export.fileio import M_BUFFERED
from ..export.ledger import retry_locked   # reused: retries a ledger call when SQLite reports locked/busy
from .layout import KIND_FRAMEHASH, MEDIA_KINDS
from .priority import normal_priority

SEALED = ("FINALIZED", "INTERRUPTED", "CORRUPT")
POLICY_THIS_DRIVE, POLICY_ANY_DRIVE = "this_drive", "any_drive"
VERIFIED, LOST = "VERIFIED", "LOST"
# Problems that keep alert A-006 up until acknowledged. "set_aside" (one failed read-back, file kept
# under a *.unverified-* name) is recorded and listed in the index but alerts only once it repeats
# (verify_failed) — a single transient failure that is then copied successfully is not an alarm.
ALERTING_PROBLEMS = ("verify_failed", "lost", "unverifiable")


class UnverifiableCopy(ValueError):
    """The read-back could not bypass the OS cache; the copy must not be recorded as verified."""


@dataclass(frozen=True)
class Backlog:
    segments: int               # sealed segments whose media file is not yet archived (per policy)
    seconds: float              # recorded duration of those segments
    bytes: int
    oldest_start_utc: str | None
    sidecars: int               # per-frame lists available locally but not yet archived


def ledger_instance(ledger: Ledger) -> str:
    """A random id for this ledger (stored once in ``schema_meta``), written into every drive index so
    entries made by another appliance / an earlier ledger can be told apart."""
    rows = ledger.query("SELECT value FROM schema_meta WHERE key='ledger_instance'")
    if rows:
        return str(rows[0]["value"])
    new = str(uuid.uuid4())
    with normal_priority(), ledger.tx() as c:
        c.execute("INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('ledger_instance', ?)", (new,))
    return str(ledger.query("SELECT value FROM schema_meta WHERE key='ledger_instance'")[0]["value"])


class ArchiveLedger:
    def __init__(self, ledger: Ledger, camera_id: str, policy: str = POLICY_THIS_DRIVE) -> None:
        if policy not in (POLICY_THIS_DRIVE, POLICY_ANY_DRIVE):
            raise ValueError(f"unknown resume policy {policy!r}")
        self.ledger = ledger
        self.camera_id = camera_id
        self.policy = policy

    # ------------------------------------------------------------------ helpers
    def _drive_clause(self, drive_id: str | None) -> tuple[str, tuple[Any, ...]]:
        """SQL fragment restricting archive_copies ``a`` to the drives that count under the policy."""
        if self.policy == POLICY_ANY_DRIVE or drive_id is None:
            return " AND a.state='VERIFIED'", ()
        return " AND a.state='VERIFIED' AND a.drive_id=?", (drive_id,)

    def _pending_where(self, drive_id: str | None) -> tuple[str, tuple[Any, ...]]:
        dc, dp = self._drive_clause(drive_id)
        kinds = ",".join(f"'{k}'" for k in MEDIA_KINDS)
        sql = (f"s.camera_id=? AND s.state IN ('FINALIZED','INTERRUPTED','CORRUPT') AND s.sha256 IS NOT NULL AND ("
               f"NOT EXISTS (SELECT 1 FROM archive_copies a WHERE a.segment_id=s.segment_id AND a.kind IN ({kinds}){dc})"
               f" OR (s.framehash_rel_path IS NOT NULL AND s.framehash_sha256 IS NOT NULL AND NOT EXISTS ("
               f"SELECT 1 FROM archive_copies a WHERE a.segment_id=s.segment_id AND a.kind='{KIND_FRAMEHASH}'{dc})))")
        return sql, (self.camera_id, *dp, *dp)

    # ------------------------------------------------------------------ queries
    @retry_locked
    def pending_segments(self, drive_id: str, limit: int = 50, exclude: frozenset[str] = frozenset()) -> list[sqlite3.Row]:
        """Oldest first (insertion order, never wall-clock text): sealed segments with a media file
        or an available per-frame list not yet verified on ``drive_id`` (or on any drive)."""
        where, params = self._pending_where(drive_id)
        rows = self.ledger.query(f"SELECT s.*, s.rowid AS _rowid FROM segments s WHERE {where} ORDER BY s.rowid "
                                 f"LIMIT ?", (*params, limit + len(exclude)))
        return [r for r in rows if r["segment_id"] not in exclude][:limit]

    @retry_locked
    def has_copy(self, segment_id: str, drive_id: str, kind: str) -> bool:
        """Does ``kind`` of this segment count as archived for ``drive_id`` under the policy?"""
        kinds: tuple[str, ...] = MEDIA_KINDS if kind in MEDIA_KINDS else (kind,)
        marks = ",".join("?" for _ in kinds)
        if self.policy == POLICY_ANY_DRIVE:
            rows = self.ledger.query(f"SELECT 1 FROM archive_copies WHERE segment_id=? AND kind IN ({marks}) "
                                     f"AND state='VERIFIED' LIMIT 1", (segment_id, *kinds))
        else:
            rows = self.ledger.query(f"SELECT 1 FROM archive_copies WHERE segment_id=? AND kind IN ({marks}) "
                                     f"AND drive_id=? AND state='VERIFIED' LIMIT 1", (segment_id, *kinds, drive_id))
        return bool(rows)

    @retry_locked
    def known_paths(self, drive_id: str) -> set[str]:
        """Every path this ledger ever recorded on the drive (VERIFIED or LOST)."""
        return {r["path"] for r in self.ledger.query("SELECT path FROM archive_copies WHERE drive_id=?", (drive_id,))}

    @retry_locked
    def copies_on(self, drive_id: str) -> list[sqlite3.Row]:
        return self.ledger.query(
            "SELECT a.*, s.run_id, s.seq, s.state, s.start_utc, s.end_utc, s.duration_s, s.interrupted, "
            "s.clock_anomaly, s.warnings, s.sealed_utc, s.content_sha256, s.content_hash_method, s.rel_path, "
            "s.video_codec, s.width, s.height, s.sha256 AS sealed_sha256, s.framehash_sha256, s.rowid AS seg_rowid "
            "FROM archive_copies a JOIN segments s ON s.segment_id=a.segment_id "
            "WHERE a.drive_id=? AND a.state='VERIFIED' AND s.camera_id=? ORDER BY s.rowid, a.kind",
            (drive_id, self.camera_id))

    @retry_locked
    def backlog(self, drive_id: str | None = None) -> Backlog:
        """``drive_id`` None: what is not verified on ANY archive drive (the operator's "Not yet
        archived" — footage that exists only on this system). With a drive: the work queue for that
        drive under the resume policy. LOST copies do not count."""
        dc, dp = self._drive_clause(drive_id)
        kinds = ",".join(f"'{k}'" for k in MEDIA_KINDS)
        media = self.ledger.query(
            f"SELECT COUNT(*) n, COALESCE(SUM(s.duration_s),0) d, COALESCE(SUM(s.size_bytes),0) b, MIN(s.rowid) r "
            f"FROM segments s WHERE s.camera_id=? AND s.state IN ('FINALIZED','INTERRUPTED','CORRUPT') "
            f"AND s.sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM archive_copies a WHERE a.segment_id=s.segment_id "
            f"AND a.kind IN ({kinds}){dc})", (self.camera_id, *dp))[0]
        oldest = None
        if media["r"] is not None:
            o = self.ledger.query("SELECT start_utc FROM segments WHERE rowid=?", (media["r"],))
            oldest = o[0]["start_utc"] if o else None
        side = self.ledger.query(
            f"SELECT COUNT(*) n FROM segments s WHERE s.camera_id=? AND s.framehash_rel_path IS NOT NULL "
            f"AND s.framehash_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM archive_copies a "
            f"WHERE a.segment_id=s.segment_id AND a.kind='{KIND_FRAMEHASH}'{dc})", (self.camera_id, *dp))[0]
        return Backlog(int(media["n"]), float(media["d"] or 0.0), int(media["b"] or 0), oldest, int(side["n"]))

    @retry_locked
    def other_drives_holding(self, segment_ids: list[str], drive_id: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        kinds = ",".join(f"'{k}'" for k in MEDIA_KINDS)
        for i in range(0, len(segment_ids), 500):
            chunk = segment_ids[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in self.ledger.query(f"SELECT segment_id, drive_id FROM archive_copies WHERE segment_id IN ({marks}) "
                                       f"AND kind IN ({kinds}) AND drive_id!=? AND state='VERIFIED'",
                                       (*chunk, drive_id)):
                out.setdefault(r["segment_id"], []).append(r["drive_id"])
        return out

    @retry_locked
    def segments_in_span(self, first_rowid: int, last_rowid: int) -> list[sqlite3.Row]:
        return self.ledger.query("SELECT s.*, s.rowid AS _rowid FROM segments s WHERE s.camera_id=? AND s.rowid "
                                 "BETWEEN ? AND ? ORDER BY s.rowid", (self.camera_id, first_rowid, last_rowid))

    @retry_locked
    def next_index_generation(self, drive_id: str) -> int:
        rows = self.ledger.query("SELECT COALESCE(MAX(generation),0)+1 g FROM archive_indexes WHERE drive_id=?",
                                 (drive_id,))
        return int(rows[0]["g"])

    @retry_locked
    def review_batches_on(self, drive_id: str) -> int:
        return int(self.ledger.query("SELECT COUNT(*) n FROM export_batches WHERE drive_id=?", (drive_id,))[0]["n"])

    # ------------------------------------------------------------------ problems (persistent A-006)
    @retry_locked
    def open_problems(self) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in ALERTING_PROBLEMS)
        return self.ledger.query(f"SELECT * FROM archive_problems WHERE acknowledged_utc IS NULL AND kind IN ({marks}) "
                                 f"ORDER BY problem_id", ALERTING_PROBLEMS)

    @retry_locked
    def set_aside_on(self, drive_id: str) -> list[sqlite3.Row]:
        return self.ledger.query("SELECT * FROM archive_problems WHERE drive_id=? AND kind='set_aside' "
                                 "ORDER BY problem_id", (drive_id,))

    @retry_locked
    def add_problem(self, drive_id: str | None, kind: str, detail: str, segment_id: str | None = None,
                    path: str | None = None) -> int:
        with normal_priority(), self.ledger.tx() as c:
            cur = c.execute("INSERT INTO archive_problems(drive_id, kind, segment_id, path, detail, created_utc) "
                            "VALUES (?,?,?,?,?,?)", (drive_id, kind, segment_id, path, detail[:1000],
                                                     iso_utc(time.time())))
            pid = int(cur.lastrowid or 0)
            self.ledger.chain_insert(c, "archive_problems", pid)     # integration: hash chain
            return pid

    @retry_locked
    def acknowledge(self, admin_id: str, problem_ids: list[int] | None = None) -> int:
        """Administrator acknowledges problems (all open ones if ``problem_ids`` is None). Audit event."""
        if not admin_id.strip():
            raise ValueError("an administrator id is required")
        now = iso_utc(time.time())
        with normal_priority(), self.ledger.tx() as c:
            if problem_ids is None:
                ids = [r["problem_id"] for r in c.execute(
                    "SELECT problem_id FROM archive_problems WHERE acknowledged_utc IS NULL")]
            else:
                ids = list(problem_ids)
            for pid in ids:
                with self.ledger.chained(c, "archive_problems", [pid]):   # integration: hash chain
                    c.execute("UPDATE archive_problems SET acknowledged_utc=?, acknowledged_by=? WHERE problem_id=? "
                              "AND acknowledged_utc IS NULL", (now, admin_id, pid))
            self.ledger._event_in(c, _spec("audit", "archive_problems_acknowledged",
                                      "Administrator acknowledged archive drive problems",
                                      {"admin_id": admin_id, "problem_ids": ids}))
        return len(ids)

    # ------------------------------------------------------------------ writes
    @retry_locked
    def event(self, severity: str, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        with normal_priority():
            self.ledger.add_event(severity, "archive", code, message, detail)

    @retry_locked
    def record_verified(self, *, segment_id: str, drive_id: str, kind: str, path: str, sha256: str,
                        size_bytes: int, verify_method: str, already_present: bool = False) -> str:
        """Record a copy that has ALREADY been verified by uncached read-back. Refuses a hash that
        differs from the sealed one and a read-back that was served through the OS cache."""
        if verify_method == M_BUFFERED or not verify_method:
            raise UnverifiableCopy(f"read-back of {path} could not bypass the operating-system cache")
        verified = iso_utc(time.time())
        with normal_priority(), self.ledger.tx() as c:
            seg = c.execute("SELECT sha256, framehash_sha256, state FROM segments WHERE segment_id=?",
                            (segment_id,)).fetchone()
            if seg is None:
                raise KeyError(segment_id)
            sealed = seg["framehash_sha256"] if kind == KIND_FRAMEHASH else seg["sha256"]
            if seg["state"] not in SEALED or not sealed or sealed != sha256:
                raise ValueError(f"refusing to record an archive copy of {segment_id} ({kind}) whose hash does not "
                                 f"equal the sealed hash")
            cur = c.execute("INSERT INTO archive_copies(segment_id, drive_id, kind, path, sha256, size_bytes, "
                            "verify_method, already_present, verified_utc, state) VALUES (?,?,?,?,?,?,?,?,?,'VERIFIED')",
                            (segment_id, drive_id, kind, path, sha256, size_bytes, verify_method, int(already_present),
                             verified))
            self.ledger.chain_insert(c, "archive_copies", int(cur.lastrowid or 0))   # integration: hash chain
        return verified

    @retry_locked
    def mark_lost(self, copy_id: int, reason: str) -> None:
        """A recorded copy is missing or damaged on its drive: keep the row as history, stop counting
        it, queue the segment again (it no longer has a VERIFIED copy) and raise a persistent alert."""
        now = iso_utc(time.time())
        with normal_priority(), self.ledger.tx() as c:
            row = c.execute("SELECT * FROM archive_copies WHERE copy_id=?", (copy_id,)).fetchone()
            if row is None or row["state"] != VERIFIED:
                return
            with self.ledger.chained(c, "archive_copies", [copy_id]):     # integration: hash chain
                c.execute("UPDATE archive_copies SET state='LOST', lost_utc=?, lost_reason=? WHERE copy_id=?",
                          (now, reason[:500], copy_id))
            cur = c.execute("INSERT INTO archive_problems(drive_id, kind, segment_id, path, detail, created_utc) "
                            "VALUES (?,?,?,?,?,?)", (row["drive_id"], "lost", row["segment_id"], row["path"],
                                                     reason[:1000], now))
            self.ledger.chain_insert(c, "archive_problems", int(cur.lastrowid or 0))
            self.ledger._event_in(c, _spec("error", "archive_copy_lost", "An archived copy is missing or damaged on its "
                                      "archive drive; it will be copied again",
                                      {"copy_id": copy_id, "drive_id": row["drive_id"], "path": row["path"],
                                       "reason": reason}))

    @retry_locked
    def sync_sessions(self, present: set[str], keep_open: bool, swapped: str | None = None) -> None:
        """Presence sessions of archive drives (used by the retention rule: "has this drive left the
        machine?"). Opens a session for each present drive known to the ledger; closes the open sessions
        of drives no longer present — unless ``keep_open`` (some drive is present but unreadable, so
        absence is not proven)."""
        open_rows = {r["drive_id"]: r["session_id"] for r in self.ledger.query(
            "SELECT drive_id, session_id FROM archive_drive_sessions WHERE gone_utc IS NULL")}
        to_open = [d for d in present if d not in open_rows]
        to_close = [] if keep_open else [(d, s) for d, s in open_rows.items() if d not in present]
        if to_open:
            known = {r["drive_id"] for r in self.ledger.query(
                f"SELECT drive_id FROM drives WHERE drive_id IN ({','.join('?' for _ in to_open)})", to_open)}
            to_open = [d for d in to_open if d in known]
        if not to_open and not to_close:
            return
        now = iso_utc(time.time())
        with normal_priority(), self.ledger.tx() as c:
            for d in to_open:
                cur = c.execute("INSERT INTO archive_drive_sessions(drive_id, seen_utc) VALUES (?,?)", (d, now))
                self.ledger.chain_insert(c, "archive_drive_sessions", int(cur.lastrowid or 0))   # integration
            for d, sid in to_close:
                with self.ledger.chained(c, "archive_drive_sessions", [sid]):
                    c.execute("UPDATE archive_drive_sessions SET gone_utc=?, gone_how=? WHERE session_id=?",
                              (now, "swapped" if d == swapped else "removed", sid))

    @retry_locked
    def recording_rate(self, window_s: float = 86400.0) -> float | None:
        """Measured recording rate (bytes per second of footage) over the segments sealed in the last
        ``window_s``; None if there are none."""
        since = iso_utc(time.time() - window_s)
        r = self.ledger.query("SELECT COALESCE(SUM(size_bytes),0) b, COALESCE(SUM(duration_s),0) d FROM segments "
                              "WHERE camera_id=? AND state IN ('FINALIZED','INTERRUPTED') AND sealed_utc >= ?",
                              (self.camera_id, since))[0]
        return float(r["b"]) / float(r["d"]) if r["d"] and float(r["d"]) > 0 else None

    @retry_locked
    def record_index(self, drive_id: str, generation: int, generated_utc: str, reason: str, copies: int,
                     json_sha256: str, sums_sha256: str) -> None:
        with normal_priority(), self.ledger.tx() as c:
            c.execute("INSERT INTO archive_indexes(drive_id, generation, generated_utc, reason, copies, json_sha256, "
                      "sums_sha256) VALUES (?,?,?,?,?,?,?)",
                      (drive_id, generation, generated_utc, reason, copies, json_sha256, sums_sha256))


def _spec(severity: str, code: str, message: str, detail: dict[str, Any]) -> Any:
    from ..db import EventSpec

    return EventSpec(severity, "archive", code, message, detail)
