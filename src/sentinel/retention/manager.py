"""Admin-enabled local retention: remove the OLDEST local originals that are safely archived, only when
internal storage runs low (owner decision pending; ``[retention] enabled = false`` by default).

Rule (all must hold for a segment to be removed from this computer):

* retention is enabled by an administrator, and free space on the recording storage is below the
  floor (default: the storage warning threshold); removal stops as soon as free space is back to
  floor + margin — never more than needed;
* the segment is FINALIZED or INTERRUPTED (never CORRUPT, never unsealed), sealed at least
  ``keep_recent_hours`` ago (never the current / recent footage), not already removed;
* it has at least ``min_verified_copies`` VERIFIED archive copies of the original (LOST copies do not
  count) — and of its per-frame sidecar, if it has one — and with ``require_removed_drive`` (default)
  only copies on archive drives that have physically left the machine count (their last presence
  session is closed: evidence has left the building). A drive whose presence is unknown counts as
  still present (fail-safe);
* oldest first (ledger insertion order); ineligible segments are skipped, never forced.

Every removal: (1) one transaction re-checks the archive record, writes a ``retention_deletions`` row
(DELETING) and sets ``segments.local_deleted_utc`` (the segment keeps all metadata and hashes); (2) the
files are deleted; (3) the row becomes DONE and an ``audit`` event is written. A crash between (1) and
(3) is completed on the next pass. Deleted segments are skipped by the integrity audit, the content
hasher, startup recovery and the review export (which names them as "kept only on archive drives").
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..clock import iso_utc, parse_iso_utc
from ..config import AppConfig
from ..db import EventSpec, Ledger
from ..storage.monitor import GB, DiskUsage

log = logging.getLogger(__name__)


@dataclass
class RetentionReport:
    ran_utc: str
    enabled: bool
    free_before: int | None = None
    free_after: int | None = None
    floor_bytes: int = 0
    target_bytes: int = 0
    deleted: int = 0
    freed_bytes: int = 0
    completed_interrupted: int = 0
    blocked: bool = False            # below the floor and nothing (more) could be removed
    skipped: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class RetentionManager:
    def __init__(self, cfg: AppConfig, ledger: Ledger, *, disk_usage: Callable[[Path], DiskUsage] | None = None,
                 now: Callable[[], float] = time.time, interval_s: float = 60.0) -> None:
        import shutil

        self.cfg = cfg
        self.rcfg = cfg.retention
        self.ledger = ledger
        self.spool = cfg.spool_path
        self._disk_usage = disk_usage or (lambda p: DiskUsage(*shutil.disk_usage(p)))
        self.now = now
        self.interval_s = interval_s
        floor_gb = self.rcfg.floor_free_gb if self.rcfg.floor_free_gb >= 0 else cfg.storage.warning_free_gb
        self.floor_bytes = int(floor_gb * GB)
        self.target_bytes = self.floor_bytes + int(self.rcfg.margin_gb * GB)
        self.last: RetentionReport | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ thread
    def start(self) -> RetentionManager:
        self._thread = threading.Thread(target=self._loop, name="retention", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # never let retention take anything else down
                log.exception("retention pass failed")
                self.last = RetentionReport(iso_utc(self.now()), self.rcfg.enabled, error=f"{type(exc).__name__}: {exc}")
            self._stop.wait(self.interval_s)

    # ------------------------------------------------------------------ queries
    def _free(self) -> int:
        return int(self._disk_usage(self.spool).free)

    def removed_drives(self) -> set[str]:
        """Archive drives that have been seen and are now gone (no open presence session)."""
        rows = self.ledger.query("SELECT drive_id FROM archive_drive_sessions GROUP BY drive_id "
                                 "HAVING SUM(CASE WHEN gone_utc IS NULL THEN 1 ELSE 0 END)=0")
        return {r["drive_id"] for r in rows}

    def _copies(self, c: Any, segment_id: str, kind: str, eligible: set[str] | None) -> list[dict[str, Any]]:
        rows = c.execute("SELECT copy_id, drive_id, path, sha256 FROM archive_copies WHERE segment_id=? AND kind=? "
                         "AND state='VERIFIED' ORDER BY copy_id", (segment_id, kind)).fetchall()
        return [dict(r) for r in rows if eligible is None or r["drive_id"] in eligible]

    def _eligible(self, c: Any, seg: Any, eligible_drives: set[str] | None, now: float) -> tuple[bool, str, list[Any]]:
        if seg["state"] not in ("FINALIZED", "INTERRUPTED"):
            return False, "not_finalized", []
        if seg["local_deleted_utc"]:
            return False, "already_removed", []
        if not seg["sha256"] or not seg["sealed_utc"]:
            return False, "not_sealed", []
        if now - parse_iso_utc(seg["sealed_utc"]) < self.rcfg.keep_recent_hours * 3600:
            return False, "recent", []
        copies = self._copies(c, seg["segment_id"], "original", eligible_drives)
        if len({x["drive_id"] for x in copies}) < self.rcfg.min_verified_copies:
            return False, "not_enough_verified_copies", []
        if any(x["sha256"] != seg["sha256"] for x in copies):
            return False, "copy_hash_mismatch", []
        if seg["framehash_rel_path"]:
            side = self._copies(c, seg["segment_id"], "framehash", eligible_drives)
            if len({x["drive_id"] for x in side}) < self.rcfg.min_verified_copies:
                return False, "sidecar_not_archived", []
            copies = copies + side
        return True, "ok", copies

    def oldest_kept(self) -> str | None:
        rows = self.ledger.query("SELECT start_utc FROM segments WHERE camera_id=? AND local_deleted_utc IS NULL "
                                 "AND start_utc IS NOT NULL ORDER BY rowid LIMIT 1", (self.cfg.camera.id,))
        return rows[0]["start_utc"] if rows else None

    # ------------------------------------------------------------------ the pass
    def run_once(self) -> RetentionReport:
        rep = RetentionReport(iso_utc(self.now()), self.rcfg.enabled, floor_bytes=self.floor_bytes,
                              target_bytes=self.target_bytes)
        rep.completed_interrupted = self._finish_interrupted()
        if not self.rcfg.enabled:
            self.last = rep
            return rep
        free = rep.free_before = rep.free_after = self._free()
        if free >= self.floor_bytes:
            self.last = rep
            return rep
        eligible_drives = self.removed_drives() if self.rcfg.require_removed_drive else None
        now = self.now()
        last_rowid = 0
        while free < self.target_bytes and rep.deleted < self.rcfg.max_deletions_per_pass:
            batch = self.ledger.query("SELECT s.*, s.rowid AS rid FROM segments s WHERE s.camera_id=? AND s.rowid>? "
                                      "AND s.local_deleted_utc IS NULL AND s.state IN ('FINALIZED','INTERRUPTED') "
                                      "ORDER BY s.rowid LIMIT 200", (self.cfg.camera.id, last_rowid))
            if not batch:
                break
            for seg in batch:
                last_rowid = seg["rid"]
                if free >= self.target_bytes or rep.deleted >= self.rcfg.max_deletions_per_pass:
                    break
                done = self._remove(seg, eligible_drives, now, free, rep)
                if done:
                    free = self._free()
        rep.free_after = free
        rep.blocked = free < self.floor_bytes
        if rep.blocked:
            log.warning("retention: below the floor and nothing more can be removed (%s)", rep.skipped)
        self.last = rep
        return rep

    def _remove(self, seg: Any, eligible_drives: set[str] | None, now: float, free: int, rep: RetentionReport) -> bool:
        decided = iso_utc(self.now())
        with self.ledger.tx() as c:
            # Re-check inside the transaction: the archive record must still hold right now.
            cur = c.execute("SELECT * FROM segments WHERE segment_id=?", (seg["segment_id"],)).fetchone()
            drives = (self._removed_in(c) if self.rcfg.require_removed_drive else None)
            if drives is not None and eligible_drives is not None:
                drives &= eligible_drives
            ok, why, copies = self._eligible(c, cur, drives, now)
            if not ok:
                rep.skipped[why] = rep.skipped.get(why, 0) + 1
                return False
            c.execute("INSERT INTO retention_deletions(segment_id, state, rel_path, sha256, size_bytes, "
                      "framehash_rel_path, archive_copies, reason, free_before, decided_utc) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (cur["segment_id"], "DELETING", cur["rel_path"], cur["sha256"], cur["size_bytes"],
                       cur["framehash_rel_path"], json.dumps(copies, sort_keys=True),
                       f"free space {free} below floor {self.floor_bytes}", free, decided))
            c.execute("UPDATE segments SET local_deleted_utc=?, updated_utc=? WHERE segment_id=?",
                      (decided, decided, cur["segment_id"]))
        freed = self._delete_files(cur["rel_path"], cur["framehash_rel_path"])
        self._mark_done(cur["segment_id"], freed)
        rep.deleted += 1
        rep.freed_bytes += freed
        return True

    def _removed_in(self, c: Any) -> set[str]:
        rows = c.execute("SELECT drive_id FROM archive_drive_sessions GROUP BY drive_id "
                         "HAVING SUM(CASE WHEN gone_utc IS NULL THEN 1 ELSE 0 END)=0").fetchall()
        return {r["drive_id"] for r in rows}

    def _delete_files(self, rel_path: str, framehash_rel: str | None) -> int:
        freed = 0
        for rel in (rel_path, framehash_rel):
            if not rel:
                continue
            p = self.spool / rel
            try:
                size = p.stat().st_size
                os.chmod(p, stat.S_IREAD | stat.S_IWRITE)   # sealed originals are read-only
                p.unlink()
                freed += size
            except FileNotFoundError:
                continue
        return freed

    def _mark_done(self, segment_id: str, freed: int) -> None:
        free_after = self._free()
        with self.ledger.tx() as c:
            row = c.execute("SELECT * FROM retention_deletions WHERE segment_id=? AND state='DELETING' "
                            "ORDER BY deletion_id DESC LIMIT 1", (segment_id,)).fetchone()
            if row is None:
                return
            now = iso_utc(self.now())
            c.execute("UPDATE retention_deletions SET state='DONE', deleted_utc=?, free_after=? WHERE deletion_id=?",
                      (now, free_after, row["deletion_id"]))
            Ledger._event_in(c, EventSpec("audit", "retention", "retention_deleted",
                                          "Archived recording removed from this computer to free space",
                                          {"segment_id": segment_id, "rel_path": row["rel_path"],
                                           "sha256": row["sha256"], "freed_bytes": freed,
                                           "archive_copies": json.loads(row["archive_copies"]),
                                           "deletion_id": row["deletion_id"]}))

    def _finish_interrupted(self) -> int:
        """Complete removals decided before a crash (row DELETING): the decision and the segment mark are
        committed, so the files are removed and the row closed."""
        n = 0
        for row in self.ledger.query("SELECT * FROM retention_deletions WHERE state='DELETING' ORDER BY deletion_id"):
            freed = self._delete_files(row["rel_path"], row["framehash_rel_path"])
            self._mark_done(row["segment_id"], freed)
            n += 1
        return n

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        oldest = self.oldest_kept()
        return {"enabled": self.rcfg.enabled, "floor_bytes": self.floor_bytes, "target_bytes": self.target_bytes,
                "oldest_kept_utc": oldest, "last_pass": asdict(self.last) if self.last else None}


def oldest_kept_text(oldest_utc: str | None, tz_name: str) -> str:
    """'Oldest footage kept on this computer: 2026-11-02' (local date in the display time zone)."""
    if not oldest_utc:
        return "Oldest footage kept on this computer: none"
    from ..export.layout import to_local
    from zoneinfo import ZoneInfo

    return f"Oldest footage kept on this computer: {to_local(parse_iso_utc(oldest_utc), ZoneInfo(tz_name)):%Y-%m-%d}"
