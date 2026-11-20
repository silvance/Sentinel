"""Admin-enabled local retention: remove the OLDEST local originals that are safely archived, only when
internal storage runs low (owner decision pending; ``[retention] enabled = false`` by default).

Rule (all must hold for a segment to be removed from this computer):

* retention is enabled by an administrator, and free space on the recording storage is below the
  floor (the level set in ``[retention] floor_free_gb``; when none is set, :func:`default_floor_bytes`:
  150 GB, less on a small drive, always above the storage warning level); removal stops as soon as free
  space is back to floor + margin — never more than needed;
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

# Where removal starts when ``[retention] floor_free_gb`` is not set (owner decision 2026-11-20, 0.3.26).
# Until then it was the storage warning level itself, so a computer that had filled up lived on that line:
# every 10 GB of recording its status showed "Storage warning" until the next pass, and "about a day of
# space left" all the time. The level is now well above the warning: "150 GB" as the window shows sizes
# (decimal), but never more than this share of the recording drive, so a small drive keeps most of its room
# for the second copy, and never less than this much above the warning level.
DEFAULT_FLOOR_BYTES = 150 * 10**9
DEFAULT_FLOOR_SHARE = 0.40           # a drive of 375 GB or more gets the full 150 GB
DEFAULT_FLOOR_ABOVE_WARNING_GB = 10.0
RECLAIM_EVERY_PASSES = 5        # the "what could be removed" figure is refreshed this often (and after removals)


def default_floor_bytes(cfg: AppConfig, total_bytes: int | None) -> int:
    """The start level for a configuration that does not set one. ``total_bytes``: the size of the
    recording drive, or None when it cannot be read (then only the distance to the warning level counts)."""
    lowest = int((cfg.storage.warning_free_gb + DEFAULT_FLOOR_ABOVE_WARNING_GB) * GB)
    if not total_bytes or total_bytes <= 0:
        return lowest
    return max(lowest, min(DEFAULT_FLOOR_BYTES, int(total_bytes * DEFAULT_FLOOR_SHARE)))


def floor_bytes_for(cfg: AppConfig, total_bytes: int | None) -> int:
    """Free space below which archived recordings are removed: the configured level, else the default."""
    if cfg.retention.floor_free_gb >= 0:
        return int(cfg.retention.floor_free_gb * GB)
    return default_floor_bytes(cfg, total_bytes)


class _Reader:
    """Lets the eligibility test run outside a transaction: every statement is one locked read."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def execute(self, sql: str, params: Any = ()) -> _Reader:
        self._rows = self.ledger.query(sql, params)
        return self

    def fetchall(self) -> list[Any]:
        return self._rows


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
    delete_failed: int = 0           # files of decided removals that could not be deleted yet (retried)
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
        self.floor_bytes = self.target_bytes = 0
        self._levels_final = False
        self._set_levels()
        self.last: RetentionReport | None = None
        self.reclaimable_bytes: int | None = None     # what could be removed now; None = not counted yet
        self._passes_since_count = RECLAIM_EVERY_PASSES
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

    # ------------------------------------------------------------------ levels
    def _set_levels(self) -> None:
        """The start level and the level removal stops at. A level that is not set in the configuration
        depends on the size of the recording drive; until that can be read (the folder may not exist yet at
        the very first start) the lower, size-independent level is used and the size is asked again."""
        total: int | None
        try:
            total = int(self._disk_usage(self.spool).total)
        except OSError:
            total = None
        self.floor_bytes = floor_bytes_for(self.cfg, total)
        self.target_bytes = self.floor_bytes + int(self.rcfg.margin_gb * GB)
        self._levels_final = total is not None or self.rcfg.floor_free_gb >= 0

    # ------------------------------------------------------------------ queries
    def _free(self) -> int:
        return int(self._disk_usage(self.spool).free)

    def count_reclaimable(self) -> int:
        """Bytes of recordings the rule would allow removing right now. Nothing is changed. The status adds
        this to the free space when it says how long recording can go on: a computer that keeps its free
        space at the start level by removing archived recordings has far more room than its free space."""
        if not self.rcfg.enabled:                  # the rule's first condition: nothing may be removed at all
            return 0
        eligible_drives = self.removed_drives() if self.rcfg.require_removed_drive else None
        now = self.now()
        reader = _Reader(self.ledger)
        total, last_rowid = 0, 0
        while True:
            batch = self.ledger.query("SELECT s.*, s.rowid AS rid FROM segments s WHERE s.camera_id=? AND s.rowid>? "
                                      "AND s.local_deleted_utc IS NULL AND s.state IN ('FINALIZED','INTERRUPTED') "
                                      "ORDER BY s.rowid LIMIT 500", (self.cfg.camera.id, last_rowid))
            if not batch:
                return total
            for seg in batch:
                last_rowid = seg["rid"]
                if self._eligible(reader, seg, eligible_drives, now)[0]:
                    total += int(seg["size_bytes"] or 0)

    def _refresh_reclaimable(self, force: bool = False) -> None:
        self._passes_since_count += 1
        if not force and self._passes_since_count < RECLAIM_EVERY_PASSES:
            return
        self._passes_since_count = 0
        try:
            self.reclaimable_bytes = self.count_reclaimable()
        except Exception:  # an estimate for the status; never a reason to stop the pass
            log.exception("retention: could not count what could be removed")
            self.reclaimable_bytes = None

    def removed_drives(self) -> set[str]:
        """Archive drives that have been seen and are now gone (no open presence session)."""
        rows = self.ledger.query("SELECT drive_id FROM archive_drive_sessions GROUP BY drive_id "
                                 "HAVING SUM(CASE WHEN gone_utc IS NULL THEN 1 ELSE 0 END)=0")
        return {r["drive_id"] for r in rows}

    def _copies(self, c: Any, segment_id: str, kind: str, eligible: set[str] | None) -> list[dict[str, Any]]:
        # A TRANSFERRED copy (0.3.9) is on the office drive an administrator recorded when the archive
        # drive was returned to service: it has left this computer whether or not that drive is inserted.
        rows = c.execute("SELECT copy_id, drive_id, path, sha256, state FROM archive_copies WHERE segment_id=? "
                         "AND kind=? AND state IN ('VERIFIED','TRANSFERRED') ORDER BY copy_id",
                         (segment_id, kind)).fetchall()
        return [dict(r) for r in rows if eligible is None or r["drive_id"] in eligible or r["state"] == "TRANSFERRED"]

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
        if not self._levels_final:
            self._set_levels()
        rep = RetentionReport(iso_utc(self.now()), self.rcfg.enabled, floor_bytes=self.floor_bytes,
                              target_bytes=self.target_bytes)
        rep.completed_interrupted = self._finish_interrupted(rep)
        if not self.rcfg.enabled:
            self.last = rep
            return rep
        free = rep.free_before = rep.free_after = self._free()
        if free >= self.floor_bytes:
            self._refresh_reclaimable()
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
        self._refresh_reclaimable(force=True)      # what is left to remove changed, or there is nothing left
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
            ins = c.execute("INSERT INTO retention_deletions(segment_id, state, rel_path, sha256, size_bytes, "
                            "framehash_rel_path, archive_copies, reason, free_before, decided_utc) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (cur["segment_id"], "DELETING", cur["rel_path"], cur["sha256"], cur["size_bytes"],
                             cur["framehash_rel_path"], json.dumps(copies, sort_keys=True),
                             f"free space {free} below floor {self.floor_bytes}", free, decided))
            # Integration with the tamper-evident ledger: the decision and the segment mark are
            # chained in the same transaction (segments.local_deleted_utc is a covered column).
            self.ledger.chain_insert(c, "retention_deletions", int(ins.lastrowid or 0))
            with self.ledger.chained(c, "segments", [cur["segment_id"]]):
                c.execute("UPDATE segments SET local_deleted_utc=?, updated_utc=? WHERE segment_id=?",
                          (decided, decided, cur["segment_id"]))
        freed, failed = self._delete_files(cur["rel_path"], cur["framehash_rel_path"])
        rep.freed_bytes += freed
        if failed:
            self._note_delete_failed(rep, failed)
            return True                                    # decided; completed by a later pass
        self._mark_done(cur["segment_id"], freed)
        rep.deleted += 1
        return True

    @staticmethod
    def _note_delete_failed(rep: RetentionReport, failed: list[str]) -> None:
        rep.delete_failed += len(failed)
        rep.error = (f"{rep.delete_failed} local recording file(s) could not be removed (in use or access "
                     f"denied); they will be tried again")

    def _removed_in(self, c: Any) -> set[str]:
        rows = c.execute("SELECT drive_id FROM archive_drive_sessions GROUP BY drive_id "
                         "HAVING SUM(CASE WHEN gone_utc IS NULL THEN 1 ELSE 0 END)=0").fetchall()
        return {r["drive_id"] for r in rows}

    def _delete_files(self, rel_path: str, framehash_rel: str | None) -> tuple[int, list[str]]:
        """Delete the local files of a decided removal. Returns (bytes freed, files that could not be
        deleted). Never raises for a single file (review 2026-11-10): a file locked by another program
        or with wrong permissions must not stop retention for every other segment; its row stays
        DELETING and is tried again on the next pass."""
        freed = 0
        failed: list[str] = []
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
            except OSError as exc:
                log.warning("retention: could not delete %s (%s); will retry on the next pass", rel,
                            type(exc).__name__)
                failed.append(rel)
        return freed, failed

    def _mark_done(self, segment_id: str, freed: int) -> None:
        free_after = self._free()
        with self.ledger.tx() as c:
            row = c.execute("SELECT * FROM retention_deletions WHERE segment_id=? AND state='DELETING' "
                            "ORDER BY deletion_id DESC LIMIT 1", (segment_id,)).fetchone()
            if row is None:
                return
            now = iso_utc(self.now())
            with self.ledger.chained(c, "retention_deletions", [row["deletion_id"]]):   # integration: hash chain
                c.execute("UPDATE retention_deletions SET state='DONE', deleted_utc=?, free_after=? WHERE deletion_id=?",
                          (now, free_after, row["deletion_id"]))
            self.ledger._event_in(c, EventSpec("audit", "retention", "retention_deleted",
                                          "Archived recording removed from this computer to free space",
                                          {"segment_id": segment_id, "rel_path": row["rel_path"],
                                           "sha256": row["sha256"], "freed_bytes": freed,
                                           "archive_copies": json.loads(row["archive_copies"]),
                                           "deletion_id": row["deletion_id"]}))

    def _finish_interrupted(self, rep: RetentionReport | None = None) -> int:
        """Complete removals decided before a crash (row DELETING): the decision and the segment mark are
        committed, so the files are removed and the row closed. A row whose files cannot be deleted yet
        is left DELETING and skipped, so it never blocks the other rows or new removals (review 2026-11-10)."""
        n = 0
        for row in self.ledger.query("SELECT * FROM retention_deletions WHERE state='DELETING' ORDER BY deletion_id"):
            freed, failed = self._delete_files(row["rel_path"], row["framehash_rel_path"])
            if failed:
                if rep is not None:
                    self._note_delete_failed(rep, failed)
                continue
            self._mark_done(row["segment_id"], freed)
            n += 1
        return n

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        oldest = self.oldest_kept()
        return {"enabled": self.rcfg.enabled, "floor_bytes": self.floor_bytes, "target_bytes": self.target_bytes,
                "oldest_kept_utc": oldest, "reclaimable_bytes": self.reclaimable_bytes if self.rcfg.enabled else None,
                "last_pass": asdict(self.last) if self.last else None}


def oldest_kept_text(oldest_utc: str | None, tz_name: str) -> str:
    """'Oldest footage kept on this computer: 2026-11-02' (local date in the display time zone)."""
    if not oldest_utc:
        return "Oldest footage kept on this computer: none"
    from ..export.layout import to_local
    from zoneinfo import ZoneInfo

    return f"Oldest footage kept on this computer: {to_local(parse_iso_utc(oldest_utc), ZoneInfo(tz_name)):%Y-%m-%d}"
