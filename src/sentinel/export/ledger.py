"""Export batch / item state machine on top of the evidence ledger (tables in ``db.EXPORT_DDL``).

Batch::

    PLANNED ─► COPYING ─► VERIFYING ─► MANIFEST ─► EJECTING ─► COMPLETE
       any non-terminal ─► FAILED            (needs attention)
       any non-terminal ─► INTERRUPTED       (drive vanished; resumed when the same drive returns)
       INTERRUPTED / crashed mid-phase ─► COPYING   (resume)
       FAILED ─► COPYING                     (retry on the same drive, only while no later export
                                              has moved past it; already-verified items are kept)

Item::

    PLANNED ─► COPIED ─► VERIFIED
    PLANNED ─► VERIFIED        (file already on the drive with the expected content, re-verified)
    PLANNED|COPIED ─► FAILED
    COPIED ─► PLANNED          (resume found the copied file missing: copy again)
    VERIFIED ─► FAILED         (resume re-hash found a verified file missing or changed)
    FAILED ─► PLANNED          (retry of a FAILED batch)

A batch may become COMPLETE only when every item is VERIFIED (checked here, not by callers).
Illegal transitions raise :class:`sentinel.db.IllegalTransition`. Every method is one
transaction and is retried when SQLite reports the database locked/busy (the recorder writes
the same ledger), so a momentary lock never fails an export.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

log = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])
LOCK_RETRIES = 8


def retry_locked(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*a: Any, **kw: Any) -> Any:
        for attempt in range(LOCK_RETRIES):
            try:
                return fn(*a, **kw)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if ("locked" not in msg and "busy" not in msg) or attempt == LOCK_RETRIES - 1:
                    raise
                log.warning("ledger busy (%s); retrying %s", exc, fn.__name__)
                time.sleep(min(0.5 * 2 ** attempt, 8.0))
        raise AssertionError("unreachable")
    return wrapper  # type: ignore[return-value]

from ..clock import iso_utc
from ..db import IllegalTransition, Ledger
from .target import ExportTarget

B_PLANNED, B_COPYING, B_VERIFYING, B_MANIFEST, B_EJECTING = "PLANNED", "COPYING", "VERIFYING", "MANIFEST", "EJECTING"
B_COMPLETE, B_FAILED, B_INTERRUPTED = "COMPLETE", "FAILED", "INTERRUPTED"
NON_TERMINAL_BATCH = (B_PLANNED, B_COPYING, B_VERIFYING, B_MANIFEST, B_EJECTING, B_INTERRUPTED)

I_PLANNED, I_COPIED, I_VERIFIED, I_FAILED = "PLANNED", "COPIED", "VERIFIED", "FAILED"

_ACTIVE = (B_PLANNED, B_COPYING, B_VERIFYING, B_MANIFEST, B_EJECTING)
BATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    B_PLANNED: frozenset({B_COPYING, B_FAILED, B_INTERRUPTED}),
    B_COPYING: frozenset({B_COPYING, B_VERIFYING, B_FAILED, B_INTERRUPTED}),
    B_VERIFYING: frozenset({B_COPYING, B_MANIFEST, B_FAILED, B_INTERRUPTED}),
    B_MANIFEST: frozenset({B_COPYING, B_EJECTING, B_FAILED, B_INTERRUPTED}),
    B_EJECTING: frozenset({B_COPYING, B_COMPLETE, B_FAILED, B_INTERRUPTED}),
    B_INTERRUPTED: frozenset({B_COPYING, B_FAILED}),
    B_COMPLETE: frozenset(),
    B_FAILED: frozenset({B_COPYING}),
}
ITEM_TRANSITIONS: dict[str, frozenset[str]] = {
    I_PLANNED: frozenset({I_COPIED, I_VERIFIED, I_FAILED}),
    I_COPIED: frozenset({I_VERIFIED, I_FAILED, I_PLANNED}),
    I_VERIFIED: frozenset({I_FAILED}),
    I_FAILED: frozenset({I_PLANNED}),
}
# Columns added after the first version of EXPORT_DDL (ledgers created by commit 8daf276).
_ADDED_ITEM_COLUMNS = {"written_sha256": "TEXT", "content_check": "TEXT", "content_verified_utc": "TEXT"}

EVIDENCE_KINDS = ("original", "framehash", "mp4", "quarantine", "quarantine_framehash")


def _now() -> str:
    return iso_utc(time.time())


@dataclass(frozen=True)
class NewItem:
    kind: str
    dest_rel_path: str
    segment_id: str | None = None
    sources: list[str] | None = None
    source_rel_path: str | None = None
    expected_sha256: str | None = None
    planned_bytes: int = 0
    detail: dict[str, Any] | None = None


class ExportLedger:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self._add_missing_columns()

    @retry_locked
    def _add_missing_columns(self) -> None:
        have = {r["name"] for r in self.ledger.query("PRAGMA table_info(export_items)")}
        missing = {k: v for k, v in _ADDED_ITEM_COLUMNS.items() if k not in have}
        if missing:
            with self.ledger.tx() as c:
                for k, v in missing.items():
                    c.execute(f"ALTER TABLE export_items ADD COLUMN {k} {v}")

    # ------------------------------------------------------------------ generic (retrying)
    @retry_locked
    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.ledger.query(sql, params)

    @retry_locked
    def event(self, severity: str, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.ledger.add_event(severity, "export", code, message, detail)

    @retry_locked
    def segment(self, segment_id: str) -> sqlite3.Row | None:
        return self.ledger.get_segment(segment_id)

    # ------------------------------------------------------------------ drives
    @retry_locked
    def upsert_drive(self, target: ExportTarget) -> str:
        key = target.drive_key
        now = _now()
        idn = target.identity
        size = idn.get("size_bytes")
        cols = {"drive_uuid": idn.get("drive_uuid"), "serial": idn.get("serial"), "model": idn.get("model"),
                "size_bytes": int(size) if size not in (None, "") else None, "bus_type": idn.get("bus_type"),
                "unique_id": idn.get("unique_id"), "volume_guid": idn.get("volume_guid"),
                "fs_uuid": idn.get("fs_uuid"), "label": idn.get("label"),
                "identity": json.dumps(idn, sort_keys=True), "last_seen_utc": now}
        with self.ledger.tx() as c:
            row = c.execute("SELECT drive_id FROM drives WHERE drive_id=?", (key,)).fetchone()
            if row is None:
                full = dict(cols, drive_id=key, first_seen_utc=now)
                c.execute(f"INSERT INTO drives({', '.join(full)}) VALUES ({', '.join('?' for _ in full)})",
                          tuple(full.values()))
            else:
                sets = ", ".join(f"{k}=?" for k in cols)
                c.execute(f"UPDATE drives SET {sets} WHERE drive_id=?", (*cols.values(), key))
        return key

    # ------------------------------------------------------------------ queries
    def batch(self, batch_id: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM export_batches WHERE batch_id=?", (batch_id,))
        return rows[0] if rows else None

    def batches(self, camera_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM export_batches WHERE camera_id=? ORDER BY rowid", (camera_id,))

    def resumable_batch(self, camera_id: str, drive_id: str) -> sqlite3.Row | None:
        """The batch to continue on this drive: an unfinished one, or the most recent FAILED one
        as long as no COMPLETE export (on any drive) has moved past its start since."""
        marks = ",".join("?" for _ in NON_TERMINAL_BATCH)
        rows = self.query(f"SELECT * FROM export_batches WHERE camera_id=? AND drive_id=? AND purpose='review' "
                          f"AND state IN ({marks}) ORDER BY rowid DESC LIMIT 1",
                          (camera_id, drive_id, *NON_TERMINAL_BATCH))
        if rows:
            return rows[0]
        last = self.query("SELECT * FROM export_batches WHERE camera_id=? AND purpose='review' "
                          "ORDER BY rowid DESC LIMIT 1", (camera_id,))
        if last and last[0]["state"] == B_FAILED and last[0]["drive_id"] == drive_id \
                and last[0]["interval_start_utc"] == (self.latest_complete_end(camera_id) or last[0]["interval_start_utc"]):
            return last[0]
        return None

    def latest_complete_end(self, camera_id: str) -> str | None:
        rows = self.query("SELECT MAX(interval_end_utc) e FROM export_batches WHERE camera_id=? "
                          "AND purpose='review' AND state=?", (camera_id, B_COMPLETE))
        return rows[0]["e"] if rows else None

    def items(self, batch_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM export_items WHERE batch_id=? ORDER BY ordinal, item_id", (batch_id,))

    def item(self, item_id: int) -> sqlite3.Row:
        rows = self.query("SELECT * FROM export_items WHERE item_id=?", (item_id,))
        if not rows:
            raise KeyError(item_id)
        return rows[0]

    def known_verified_on_drive(self, drive_id: str, dest_rel_path: str) -> sqlite3.Row | None:
        """The latest VERIFIED item for this path on this drive (any batch), or None."""
        rows = self.query("SELECT i.* FROM export_items i JOIN export_batches b ON b.batch_id=i.batch_id "
                          "WHERE b.drive_id=? AND i.dest_rel_path=? AND i.state=? AND i.sha256 IS NOT NULL "
                          "ORDER BY i.item_id DESC LIMIT 1", (drive_id, dest_rel_path, I_VERIFIED))
        return rows[0] if rows else None

    def known_sha_on_drive(self, drive_id: str, dest_rel_path: str) -> str | None:
        row = self.known_verified_on_drive(drive_id, dest_rel_path)
        return row["sha256"] if row is not None else None

    def deferred_mp4s(self, drive_id: str) -> list[sqlite3.Row]:
        """MP4s on this drive (COMPLETE batches) whose decoded-picture check is not yet 'full'."""
        return self.query("SELECT i.* FROM export_items i JOIN export_batches b ON b.batch_id=i.batch_id "
                          "WHERE b.drive_id=? AND b.state=? AND i.kind='mp4' AND i.state=? "
                          "AND COALESCE(i.content_check, 'full') != 'full' ORDER BY i.item_id",
                          (drive_id, B_COMPLETE, I_VERIFIED))

    # ------------------------------------------------------------------ batch writes
    @retry_locked
    def create_batch(self, *, camera_id: str, drive_id: str, interval_start_utc: str, interval_end_utc: str,
                     display_timezone: str, operator_id: str | None, app_version: str, ffmpeg_version: str | None,
                     stamp: str, plan: dict[str, Any], items: list[NewItem], batch_id: str | None = None) -> str:
        bid = batch_id or str(uuid.uuid4())
        now = _now()
        with self.ledger.tx() as c:
            c.execute("INSERT INTO export_batches(batch_id, purpose, camera_id, drive_id, state, interval_start_utc, "
                      "interval_end_utc, display_timezone, operator_id, app_version, ffmpeg_version, stamp, plan, "
                      "planned_utc, updated_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (bid, "review", camera_id, drive_id, B_PLANNED, interval_start_utc, interval_end_utc,
                       display_timezone, operator_id, app_version, ffmpeg_version, stamp,
                       json.dumps(plan, sort_keys=True), now, now))
            for n, it in enumerate(items):
                self._insert_item_in(c, bid, n, it, now)
        return bid

    @staticmethod
    def _insert_item_in(c: sqlite3.Connection, batch_id: str, ordinal: int, it: NewItem, now: str) -> int:
        cur = c.execute("INSERT INTO export_items(batch_id, ordinal, kind, segment_id, sources, source_rel_path, "
                        "dest_rel_path, state, expected_sha256, planned_bytes, detail, updated_utc) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, ordinal, it.kind, it.segment_id,
                         json.dumps(it.sources) if it.sources is not None else None, it.source_rel_path,
                         it.dest_rel_path, I_PLANNED, it.expected_sha256, it.planned_bytes,
                         json.dumps(it.detail, sort_keys=True) if it.detail else None, now))
        return int(cur.lastrowid or 0)

    @retry_locked
    def add_item(self, batch_id: str, it: NewItem) -> int:
        """Add an item (documents are added at the MANIFEST stage). Idempotent per destination."""
        with self.ledger.tx() as c:
            row = c.execute("SELECT item_id FROM export_items WHERE batch_id=? AND dest_rel_path=?",
                            (batch_id, it.dest_rel_path)).fetchone()
            if row is not None:
                return int(row["item_id"])
            n = c.execute("SELECT COALESCE(MAX(ordinal), -1) + 1 n FROM export_items WHERE batch_id=?",
                          (batch_id,)).fetchone()["n"]
            return self._insert_item_in(c, batch_id, int(n), it, _now())

    @retry_locked
    def set_batch_state(self, batch_id: str, new_state: str, **cols: Any) -> None:
        with self.ledger.tx() as c:
            row = c.execute("SELECT state FROM export_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError(batch_id)
            cur = row["state"]
            if new_state not in BATCH_TRANSITIONS[cur]:
                raise IllegalTransition(f"export batch {batch_id}: {cur} -> {new_state} not allowed")
            if new_state == B_COMPLETE:
                bad = c.execute("SELECT COUNT(*) n FROM export_items WHERE batch_id=? AND state!=?",
                                (batch_id, I_VERIFIED)).fetchone()["n"]
                docs = c.execute("SELECT COUNT(*) n FROM export_items WHERE batch_id=? AND kind='document' "
                                 "AND state=?", (batch_id, I_VERIFIED)).fetchone()["n"]
                if bad or not docs:
                    raise IllegalTransition(f"export batch {batch_id}: cannot complete ({bad} item(s) not verified, "
                                            f"{docs} verified manifest document(s))")
            cols = dict(cols, state=new_state, updated_utc=_now())
            if new_state in (B_COMPLETE, B_FAILED):
                cols.setdefault("finished_utc", cols["updated_utc"])
            sets = ", ".join(f"{k}=?" for k in cols)
            c.execute(f"UPDATE export_batches SET {sets} WHERE batch_id=?", (*cols.values(), batch_id))

    @retry_locked
    def update_batch(self, batch_id: str, **cols: Any) -> None:
        if "state" in cols:
            raise ValueError("use set_batch_state()")
        cols["updated_utc"] = _now()
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.ledger.tx() as c:
            c.execute(f"UPDATE export_batches SET {sets} WHERE batch_id=?", (*cols.values(), batch_id))

    @retry_locked
    def bump_attempts(self, batch_id: str) -> None:
        with self.ledger.tx() as c:
            c.execute("UPDATE export_batches SET attempts=attempts+1, updated_utc=? WHERE batch_id=?",
                      (_now(), batch_id))

    # ------------------------------------------------------------------ item writes
    @retry_locked
    def set_item_state(self, item_id: int, new_state: str, **cols: Any) -> None:
        with self.ledger.tx() as c:
            row = c.execute("SELECT state FROM export_items WHERE item_id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            cur = row["state"]
            if new_state not in ITEM_TRANSITIONS[cur]:
                raise IllegalTransition(f"export item {item_id}: {cur} -> {new_state} not allowed")
            if "detail" in cols and isinstance(cols["detail"], dict):
                cols["detail"] = json.dumps(cols["detail"], sort_keys=True)
            cols = dict(cols, state=new_state, updated_utc=_now())
            sets = ", ".join(f"{k}=?" for k in cols)
            c.execute(f"UPDATE export_items SET {sets} WHERE item_id=?", (*cols.values(), item_id))

    @retry_locked
    def update_item(self, item_id: int, **cols: Any) -> None:
        if "state" in cols:
            raise ValueError("use set_item_state()")
        if "detail" in cols and isinstance(cols["detail"], dict):
            cols["detail"] = json.dumps(cols["detail"], sort_keys=True)
        cols["updated_utc"] = _now()
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.ledger.tx() as c:
            c.execute(f"UPDATE export_items SET {sets} WHERE item_id=?", (*cols.values(), item_id))

    @retry_locked
    def reset_failed_items(self, batch_id: str) -> int:
        """Retrying a FAILED batch: its FAILED items are planned again (VERIFIED ones are kept and
        re-hashed by the engine; COPIED ones are verified)."""
        with self.ledger.tx() as c:
            cur = c.execute("UPDATE export_items SET state=?, error=NULL, sha256=NULL, updated_utc=? "
                            "WHERE batch_id=? AND state=?", (I_PLANNED, _now(), batch_id, I_FAILED))
            return int(cur.rowcount or 0)
