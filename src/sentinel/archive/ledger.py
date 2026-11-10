"""Archive copies in the evidence ledger (tables in ``db.ARCHIVE_DDL``).

Invariant (enforced by the only writer, :meth:`ArchiveLedger.record_verified`): an
``archive_copies`` row means "this file was read back from that drive, bypassing the OS cache,
and its SHA-256 equals the hash recorded when the segment was sealed". Nothing is recorded
before verification, so an interrupted or unverified copy can never be counted as archived.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from ..clock import iso_utc
from ..db import Ledger
from ..export.ledger import retry_locked   # reused: retries a ledger call when SQLite reports locked/busy
from .layout import KIND_FRAMEHASH, MEDIA_KINDS

SEALED = ("FINALIZED", "INTERRUPTED", "CORRUPT")
POLICY_THIS_DRIVE, POLICY_ANY_DRIVE = "this_drive", "any_drive"


@dataclass(frozen=True)
class Backlog:
    segments: int               # sealed segments whose media file is not yet archived (per policy)
    seconds: float              # recorded duration of those segments
    bytes: int
    oldest_start_utc: str | None
    sidecars: int               # per-frame lists available locally but not yet archived


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
            return "", ()
        return " AND a.drive_id=?", (drive_id,)

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
        if kind in MEDIA_KINDS:
            kinds: tuple[str, ...] = MEDIA_KINDS
        else:
            kinds = (kind,)
        marks = ",".join("?" for _ in kinds)
        if self.policy == POLICY_ANY_DRIVE:
            rows = self.ledger.query(f"SELECT 1 FROM archive_copies WHERE segment_id=? AND kind IN ({marks}) LIMIT 1",
                                     (segment_id, *kinds))
        else:
            rows = self.ledger.query(f"SELECT 1 FROM archive_copies WHERE segment_id=? AND kind IN ({marks}) "
                                     f"AND drive_id=? LIMIT 1", (segment_id, *kinds, drive_id))
        return bool(rows)

    @retry_locked
    def copy_at(self, drive_id: str, path: str) -> sqlite3.Row | None:
        rows = self.ledger.query("SELECT * FROM archive_copies WHERE drive_id=? AND path=?", (drive_id, path))
        return rows[0] if rows else None

    @retry_locked
    def copies_on(self, drive_id: str) -> list[sqlite3.Row]:
        return self.ledger.query(
            "SELECT a.*, s.run_id, s.seq, s.state, s.start_utc, s.end_utc, s.duration_s, s.interrupted, "
            "s.clock_anomaly, s.warnings, s.sealed_utc, s.content_sha256, s.content_hash_method, s.rel_path, "
            "s.video_codec, s.width, s.height, s.sha256 AS sealed_sha256, s.framehash_sha256, s.rowid AS seg_rowid "
            "FROM archive_copies a JOIN segments s ON s.segment_id=a.segment_id "
            "WHERE a.drive_id=? ORDER BY s.rowid, a.kind", (drive_id,))

    @retry_locked
    def backlog(self, drive_id: str | None = None) -> Backlog:
        """``drive_id`` None: what is not verified on ANY archive drive (the operator's "Not yet
        archived" — footage that exists only on this system). With a drive: the work queue for that
        drive under the resume policy."""
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
                                       f"AND kind IN ({kinds}) AND drive_id!=?", (*chunk, drive_id)):
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

    # ------------------------------------------------------------------ writes
    @retry_locked
    def record_verified(self, *, segment_id: str, drive_id: str, kind: str, path: str, sha256: str,
                        size_bytes: int, verify_method: str, already_present: bool = False) -> str:
        """Record a copy that has ALREADY been verified by read-back. Refuses a hash that differs
        from the sealed one (defence in depth: callers compare first)."""
        verified = iso_utc(time.time())
        with self.ledger.tx() as c:
            seg = c.execute("SELECT sha256, framehash_sha256, state FROM segments WHERE segment_id=?",
                            (segment_id,)).fetchone()
            if seg is None:
                raise KeyError(segment_id)
            sealed = seg["framehash_sha256"] if kind == KIND_FRAMEHASH else seg["sha256"]
            if seg["state"] not in SEALED or not sealed or sealed != sha256:
                raise ValueError(f"refusing to record an archive copy of {segment_id} ({kind}) whose hash does not "
                                 f"equal the sealed hash")
            c.execute("INSERT INTO archive_copies(segment_id, drive_id, kind, path, sha256, size_bytes, verify_method, "
                      "already_present, verified_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                      (segment_id, drive_id, kind, path, sha256, size_bytes, verify_method, int(already_present),
                       verified))
        return verified

    @retry_locked
    def record_index(self, drive_id: str, generation: int, generated_utc: str, reason: str, copies: int,
                     json_sha256: str, sums_sha256: str) -> None:
        with self.ledger.tx() as c:
            c.execute("INSERT INTO archive_indexes(drive_id, generation, generated_utc, reason, copies, json_sha256, "
                      "sums_sha256) VALUES (?,?,?,?,?,?,?)",
                      (drive_id, generation, generated_utc, reason, copies, json_sha256, sums_sha256))
