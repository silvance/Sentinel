"""Export batch / item state machine on top of the evidence ledger (tables in ``db.EXPORT_DDL``).

Batch::

    PLANNED ─► COPYING ─► VERIFYING ─► MANIFEST ─► EJECTING ─► COMPLETE
       any non-terminal ─► FAILED            (needs attention; a new batch is planned next time)
       any non-terminal ─► INTERRUPTED       (drive vanished; resumed when the same drive returns)
       INTERRUPTED / crashed mid-phase ─► COPYING   (resume)

Item::

    PLANNED ─► COPIED ─► VERIFIED
    PLANNED ─► VERIFIED        (file already on the drive with the expected content, re-verified)
    PLANNED|COPIED ─► FAILED
    COPIED ─► PLANNED          (resume found the copied file missing: copy again)
    VERIFIED ─► FAILED         (resume found a verified file missing or changed)

A batch may become COMPLETE only when every item is VERIFIED (checked here, not by callers).
Illegal transitions raise :class:`sentinel.db.IllegalTransition`.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

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
    B_FAILED: frozenset(),
}
ITEM_TRANSITIONS: dict[str, frozenset[str]] = {
    I_PLANNED: frozenset({I_COPIED, I_VERIFIED, I_FAILED}),
    I_COPIED: frozenset({I_VERIFIED, I_FAILED, I_PLANNED}),
    I_VERIFIED: frozenset({I_FAILED}),
    I_FAILED: frozenset(),
}

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

    # ------------------------------------------------------------------ drives
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
        rows = self.ledger.query("SELECT * FROM export_batches WHERE batch_id=?", (batch_id,))
        return rows[0] if rows else None

    def batches(self, camera_id: str) -> list[sqlite3.Row]:
        return self.ledger.query("SELECT * FROM export_batches WHERE camera_id=? ORDER BY rowid", (camera_id,))

    def resumable_batch(self, camera_id: str, drive_id: str) -> sqlite3.Row | None:
        marks = ",".join("?" for _ in NON_TERMINAL_BATCH)
        rows = self.ledger.query(f"SELECT * FROM export_batches WHERE camera_id=? AND drive_id=? AND purpose='review' "
                                 f"AND state IN ({marks}) ORDER BY rowid DESC LIMIT 1",
                                 (camera_id, drive_id, *NON_TERMINAL_BATCH))
        return rows[0] if rows else None

    def latest_complete_end(self, camera_id: str) -> str | None:
        rows = self.ledger.query("SELECT MAX(interval_end_utc) e FROM export_batches WHERE camera_id=? "
                                 "AND purpose='review' AND state=?", (camera_id, B_COMPLETE))
        return rows[0]["e"] if rows else None

    def items(self, batch_id: str) -> list[sqlite3.Row]:
        return self.ledger.query("SELECT * FROM export_items WHERE batch_id=? ORDER BY ordinal, item_id", (batch_id,))

    def item(self, item_id: int) -> sqlite3.Row:
        rows = self.ledger.query("SELECT * FROM export_items WHERE item_id=?", (item_id,))
        if not rows:
            raise KeyError(item_id)
        return rows[0]

    def known_sha_on_drive(self, drive_id: str, dest_rel_path: str) -> str | None:
        """SHA-256 a previous VERIFIED item recorded for this path on this drive (any batch)."""
        rows = self.ledger.query("SELECT i.sha256 FROM export_items i JOIN export_batches b ON b.batch_id=i.batch_id "
                                 "WHERE b.drive_id=? AND i.dest_rel_path=? AND i.state=? AND i.sha256 IS NOT NULL "
                                 "ORDER BY i.item_id DESC LIMIT 1", (drive_id, dest_rel_path, I_VERIFIED))
        return rows[0]["sha256"] if rows else None

    # ------------------------------------------------------------------ batch writes
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

    def update_batch(self, batch_id: str, **cols: Any) -> None:
        if "state" in cols:
            raise ValueError("use set_batch_state()")
        cols["updated_utc"] = _now()
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.ledger.tx() as c:
            c.execute(f"UPDATE export_batches SET {sets} WHERE batch_id=?", (*cols.values(), batch_id))

    def bump_attempts(self, batch_id: str) -> None:
        with self.ledger.tx() as c:
            c.execute("UPDATE export_batches SET attempts=attempts+1, updated_utc=? WHERE batch_id=?",
                      (_now(), batch_id))

    # ------------------------------------------------------------------ item writes
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

    def update_item(self, item_id: int, **cols: Any) -> None:
        if "state" in cols:
            raise ValueError("use set_item_state()")
        if "detail" in cols and isinstance(cols["detail"], dict):
            cols["detail"] = json.dumps(cols["detail"], sort_keys=True)
        cols["updated_utc"] = _now()
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.ledger.tx() as c:
            c.execute(f"UPDATE export_items SET {sets} WHERE item_id=?", (*cols.values(), item_id))
