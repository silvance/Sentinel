"""SQLite evidence ledger.

One connection guarded by a lock (the recorder is the only writer). WAL journal and
``synchronous=FULL`` so a committed state change survives power loss.

Integrity rules enforced here, not by callers:
* segment state transitions follow ``models.ALLOWED_TRANSITIONS``;
* sealing a segment (state change + its internal gap rows + closing the preceding outage gap +
  events) is ONE transaction, so a crash can never leave a sealed segment whose gaps are lost;
* integrity fields of a sealed segment are write-once (NULL -> value only);
* "latest" / ordering queries use insertion order (rowid), never wall-clock text, so a clock
  change cannot reorder history;
* every change to a covered row (runs, sealed segments, gaps, events) appends a hash-chain
  entry in the same transaction (``ledgerchain``), so edits made outside this class are
  detectable. Every write path below goes through ``_chained`` / ``_chain_insert``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ledgerchain as chain
from .clock import iso_utc, parse_iso_utc
from .ledger_timing import TIMING_WRITE_ONCE, apply_timing_schema
from .models import ALLOWED_TRANSITIONS, TERMINAL_STATES, GapKind, SegmentState

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS recording_runs (
    run_id            TEXT PRIMARY KEY,
    camera_id         TEXT NOT NULL,
    directory         TEXT NOT NULL,          -- relative to spool root
    launched_utc      TEXT NOT NULL,          -- host wall clock at FFmpeg launch
    launch_mono       REAL NOT NULL,          -- host monotonic clock at launch (same boot only)
    anchor_utc        TEXT,                   -- derived wall time of stream time 0 (docs/03 §4)
    anchor_method     TEXT,
    last_stream_time_s REAL,                  -- last progress out_time persisted
    timeline_break_stream_s REAL,             -- stream time at which real time was lost (see supervisor)
    timeline_shift_s  REAL,                   -- seconds of real time missing from the stream timeline
    display_timezone  TEXT NOT NULL,
    ended_utc         TEXT,
    end_reason        TEXT,
    exit_code         INTEGER,
    end_detail        TEXT,
    clock_anomaly     INTEGER NOT NULL DEFAULT 0,
    gap_recorded      INTEGER NOT NULL DEFAULT 0, -- 1 once the gap following this run is in the ledger
    app_version       TEXT NOT NULL,
    ffmpeg_version    TEXT,
    host              TEXT,
    command_redacted  TEXT                    -- FFmpeg argv with credentials removed
);

CREATE TABLE IF NOT EXISTS segments (
    segment_id        TEXT PRIMARY KEY,
    camera_id         TEXT NOT NULL,
    run_id            TEXT NOT NULL REFERENCES recording_runs(run_id),
    seq               INTEGER NOT NULL,
    rel_path          TEXT NOT NULL UNIQUE,
    state             TEXT NOT NULL,
    stream_start_s    REAL,
    stream_end_s      REAL,
    start_utc         TEXT,
    end_utc           TEXT,
    time_source       TEXT,                   -- run_anchor | run_anchor_end | file_mtime
    observed_first_utc  TEXT,
    observed_closed_utc TEXT,
    duration_s        REAL,
    size_bytes        INTEGER,
    sha256            TEXT,                   -- file bytes, original container (set at sealing)
    content_sha256    TEXT,                   -- decoded frames, container-independent (deferred)
    content_hash_method TEXT,
    decoded_frame_count INTEGER,
    framehash_rel_path TEXT,                  -- per-frame hash sidecar (optional)
    framehash_sha256  TEXT,
    pix_fmt           TEXT,
    video_codec       TEXT,
    width             INTEGER,
    height            INTEGER,
    packet_count      INTEGER,
    keyframe_count    INTEGER,
    max_pts_gap_s     REAL,
    interrupted       INTEGER NOT NULL DEFAULT 0,
    clock_anomaly     INTEGER NOT NULL DEFAULT 0,
    warnings          TEXT,                   -- JSON list of warning codes
    validation_detail TEXT,                   -- JSON
    sealed_utc        TEXT,
    finalize_attempts INTEGER NOT NULL DEFAULT 0,
    last_finalize_error TEXT,
    content_hash_attempts INTEGER NOT NULL DEFAULT 0,
    last_audit_utc    TEXT,
    audit_status      TEXT,                   -- ok | hash_mismatch | missing | not_read_only
    created_utc       TEXT NOT NULL,
    updated_utc       TEXT NOT NULL,
    UNIQUE (run_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_segments_state ON segments(state);
CREATE INDEX IF NOT EXISTS ix_segments_cam_start ON segments(camera_id, start_utc);

CREATE TABLE IF NOT EXISTS recording_gaps (
    gap_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id         TEXT NOT NULL,
    kind              TEXT NOT NULL,
    start_utc         TEXT,
    end_utc           TEXT,                   -- NULL while the gap is still open
    duration_s        REAL,
    reason            TEXT NOT NULL,
    prev_segment_id   TEXT,
    next_segment_id   TEXT,
    ended_run_id      TEXT,                   -- run whose end opened this gap
    start_basis       TEXT,                   -- how start_utc was determined
    attempts          INTEGER NOT NULL DEFAULT 1,
    clock_anomaly     INTEGER NOT NULL DEFAULT 0,
    detail            TEXT,
    opened_utc        TEXT NOT NULL,
    closed_utc        TEXT
);
CREATE INDEX IF NOT EXISTS ix_gaps_open ON recording_gaps(camera_id, end_utc);

CREATE TABLE IF NOT EXISTS events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc            TEXT NOT NULL,
    severity          TEXT NOT NULL,          -- info | warning | error | audit
    category          TEXT NOT NULL,          -- recorder | storage | clock | recovery | config | integrity
    code              TEXT NOT NULL,
    message           TEXT NOT NULL,
    detail            TEXT
);
"""

# ---------------------------------------------------------------------------------------------
# Phase 3: review-drive export ledger (docs/04 §6). Kept as a separate block so other branches
# can append their own DDL without touching the recorder tables. State machine and all writes:
# ``sentinel.export.ledger``. Additive (CREATE ... IF NOT EXISTS), so SCHEMA_VERSION is unchanged.
EXPORT_DDL = """
CREATE TABLE IF NOT EXISTS drives (                -- docs/04 §5; owned by the export branch
    drive_id          TEXT PRIMARY KEY,       -- stable key derived from the identity (see export.target)
    drive_uuid        TEXT,                   -- from SENTINEL_DRIVE.json when present
    serial            TEXT,                   -- disk serial (may be the USB bridge's own serial: risk C2)
    model             TEXT,
    size_bytes        INTEGER,
    bus_type          TEXT,                   -- usb | sata | ...
    unique_id         TEXT,                   -- WWN / unique device id, if any
    volume_guid       TEXT,                   -- Windows volume GUID
    fs_uuid           TEXT,                   -- filesystem UUID (Linux)
    label             TEXT,
    identity          TEXT NOT NULL,          -- full identity JSON as observed (diagnostic fields included)
    first_seen_utc    TEXT NOT NULL,
    last_seen_utc     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS export_batches (
    batch_id          TEXT PRIMARY KEY,       -- export id (UUIDv4), printed in every manifest
    purpose           TEXT NOT NULL,          -- 'review'
    camera_id         TEXT NOT NULL,
    drive_id          TEXT NOT NULL REFERENCES drives(drive_id),
    state             TEXT NOT NULL,          -- PLANNED|COPYING|VERIFYING|MANIFEST|EJECTING|COMPLETE|FAILED|INTERRUPTED
    interval_start_utc TEXT NOT NULL,
    interval_end_utc  TEXT NOT NULL,
    display_timezone  TEXT NOT NULL,
    operator_id       TEXT,
    app_version       TEXT NOT NULL,
    ffmpeg_version    TEXT,
    stamp             TEXT NOT NULL,          -- filesystem-safe local time stamp used in document names
    plan              TEXT,                   -- JSON: gaps, coverage, warnings fixed at planning time
    planned_utc       TEXT NOT NULL,
    finished_utc      TEXT,
    attempts          INTEGER NOT NULL DEFAULT 1,
    error_code        TEXT,                   -- operator alert code (E-0xx) when FAILED/INTERRUPTED
    error_detail      TEXT,
    manifest_generated_utc TEXT,              -- frozen once, so regenerated documents are byte-identical
    manifest_rel_path TEXT,
    manifest_sha256   TEXT,
    sums_sha256       TEXT,
    ejected           INTEGER,                -- 1 drive ejected by software, 0 operator must remove it
    removal_detail    TEXT,
    updated_utc       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_export_batches_cam ON export_batches(camera_id, state);

CREATE TABLE IF NOT EXISTS export_items (
    item_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          TEXT NOT NULL REFERENCES export_batches(batch_id),
    ordinal           INTEGER NOT NULL,
    kind              TEXT NOT NULL,          -- original|framehash|mp4|quarantine|quarantine_framehash|document
    segment_id        TEXT,                   -- source segment (originals, sidecars, quarantine)
    sources           TEXT,                   -- JSON list of segment_ids (mp4 derivatives)
    source_rel_path   TEXT,                   -- relative to the spool (originals / sidecars)
    dest_rel_path     TEXT NOT NULL,          -- relative to the drive root, forward slashes
    state             TEXT NOT NULL,          -- PLANNED|COPIED|VERIFIED|FAILED
    expected_sha256   TEXT,                   -- known before copying (sealed hash), NULL for derivatives
    sha256            TEXT,                   -- measured by reading the destination back
    size_bytes        INTEGER,
    planned_bytes     INTEGER,
    verify_method     TEXT,
    written_sha256    TEXT,                   -- mp4/documents: SHA-256 of the file as written, before read-back
    content_check     TEXT,                   -- mp4: full | sampled | deferred (decoded-picture check level)
    content_verified_utc TEXT,
    content_sha256    TEXT,                   -- mp4: decoded-frame content hash, verified equal to sources
    frame_count       INTEGER,
    already_present   INTEGER NOT NULL DEFAULT 0,
    detail            TEXT,                   -- JSON
    error             TEXT,
    copied_utc        TEXT,
    verified_utc      TEXT,
    updated_utc       TEXT NOT NULL,
    UNIQUE (batch_id, dest_rel_path)
);
CREATE INDEX IF NOT EXISTS ix_export_items_seg ON export_items(segment_id, state);
"""

# Tamper-evidence hash chain (sentinel/ledgerchain.py; format in its docstring and docs/07).
# Append-only: one entry per change to a covered row, written in the SAME transaction as the
# change. Kept as its own block so other schema additions do not interleave with it.
CHAIN_DDL = """
CREATE TABLE IF NOT EXISTS ledger_chain (
    seq               INTEGER PRIMARY KEY,    -- 0 = genesis; contiguous
    kind              TEXT NOT NULL,          -- copy of record.kind (look-up only)
    ref_table         TEXT,                   -- copy of record.table (look-up only)
    ref_key           TEXT,                   -- copy of record.key (look-up only)
    record            TEXT NOT NULL,          -- canonical JSON: exactly the text that was hashed
    prev_hash         TEXT NOT NULL,          -- entry_hash of seq-1 (64 zeros for genesis)
    entry_hash        TEXT NOT NULL           -- SHA-256(prev_hash || record), lowercase hex
);
CREATE INDEX IF NOT EXISTS ix_chain_ref ON ledger_chain(ref_table, ref_key, seq);

-- Rows whose chain entry could not be written (system fault). Re-recorded, with a
-- protection_gap entry, at the next ledger write; then deleted. Not evidence in itself.
CREATE TABLE IF NOT EXISTS ledger_chain_pending (
    ref_table         TEXT NOT NULL,
    ref_key           TEXT NOT NULL,
    failed_utc        TEXT NOT NULL,
    error             TEXT,
    PRIMARY KEY (ref_table, ref_key)
);
"""
WAL_SIZE_LIMIT = 64 * 1024 * 1024

# Columns that may be written once (NULL -> value) on a sealed segment, never changed after.
WRITE_ONCE = frozenset({
    "sha256", "size_bytes", "rel_path", "start_utc", "end_utc", "duration_s",
    "content_sha256", "content_hash_method", "decoded_frame_count", "framehash_rel_path", "framehash_sha256",
}) | TIMING_WRITE_ONCE


class IllegalTransition(RuntimeError):
    pass


class ChainTransactionAborted(RuntimeError):
    """The transaction was rolled back completely while writing a chain entry; retry the write."""


def _now_iso() -> str:
    return iso_utc(time.time())


def _j(d: Any) -> str | None:
    return json.dumps(d, sort_keys=True) if d else None


@dataclass(frozen=True)
class InternalGap:
    start_utc: str | None
    end_utc: str | None
    duration_s: float | None
    reason: str
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class EventSpec:
    severity: str
    category: str
    code: str
    message: str
    detail: dict[str, Any] | None = None


# Readers (GUI, diagnostics, status tools) may open the ledger in the moment after the recorder
# process died. Measured on Windows: for ~1 s after a hard kill, opening or querying the WAL
# database can fail with "disk I/O error" (the dead process's file handles are still being
# released); a second later every access succeeds. Readers retry these transient errors.
_TRANSIENT_READ_ERRORS = ("disk i/o error", "database is locked", "unable to open database file")
READ_RETRY_SECONDS = 5.0


def _is_transient(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_READ_ERRORS)


class Ledger:
    def __init__(self, path: Path, readonly: bool = False, retry_seconds: float | None = None,
                 immutable: bool = False) -> None:
        """``retry_seconds`` (readers only): how long to retry transient errors. A UI thread
        that must never block can pass 0 and treat a failure as "temporarily unavailable".
        ``immutable`` (readers only; integration): open with ``mode=ro&immutable=1``, for the
        operator ledger snapshot written by :meth:`write_snapshot` (rollback journal, replaced
        atomically), so a reader without write access never creates -wal/-shm files."""
        self.path = Path(path)
        self.readonly = readonly
        self.immutable = bool(immutable and readonly)
        self.retry_seconds = READ_RETRY_SECONDS if retry_seconds is None else retry_seconds
        self._closed = False
        self._lock = threading.RLock()
        self.chain_discrepancies = 0     # rows found changed outside the recorder (this process)
        self.chain_failures = 0          # chain entries that could not be written (this process)
        self._pending_maybe = False      # ledger_chain_pending may hold rows to re-record
        if readonly:
            self._conn = self._retry_read(self._open_readonly)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._configure(self._conn)
        if not readonly:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._migrate()

    # ------------------------------------------------------------------ infrastructure
    @staticmethod
    def _configure(conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")

    def _open_readonly(self) -> sqlite3.Connection:
        uri = f"{self.path.resolve().as_uri()}?mode=ro{'&immutable=1' if self.immutable else ''}"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
        try:
            self._configure(conn)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()  # surface open-time errors now
        except BaseException:
            conn.close()
            raise
        return conn

    def _retry_read(self, fn):  # noqa: ANN001, ANN202 - small internal helper
        """Call ``fn`` (which takes the lock itself if it needs it); retry transient errors.
        Never sleeps while holding ``self._lock``."""
        deadline = time.monotonic() + self.retry_seconds
        delay = 0.1
        while True:
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if not _is_transient(exc) or time.monotonic() + delay > deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 1.0)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                if self._conn.in_transaction:      # SQLite may already have rolled back (e.g. I/O error)
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(DDL)
            self._conn.executescript(EXPORT_DDL)
            self._conn.executescript(CHAIN_DDL)
            row = self._conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                                   (str(SCHEMA_VERSION),))
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(f"ledger schema {row['value']} is newer than this software ({SCHEMA_VERSION})")
            apply_timing_schema(self._conn)  # time-accuracy additions (ledger_timing.py), append-only
            # New ledger, or one created before tamper evidence existed: start the chain with a
            # genesis entry and adopt the rows already present (docs/07).
            chain.ensure_genesis(self._conn, self.path.parent / chain.ANCHOR_LOG_NAME)
            # WAL is reset (and truncated to this size) after checkpoints; see checkpoint().
            self._conn.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT}")
            self._pending_maybe = self._conn.execute("SELECT 1 FROM ledger_chain_pending LIMIT 1").fetchone() is not None

    # ------------------------------------------------------------------ hash chain
    def chain_head(self) -> chain.ChainHead | None:
        with self._lock:
            return chain.head_in(self._conn)

    def _undo_savepoint(self, c: sqlite3.Connection) -> None:
        c.execute("ROLLBACK TO chain_entry")
        c.execute("RELEASE chain_entry")

    def _chain_fault(self, c: sqlite3.Connection, what: str, keys: list[tuple[str, Any]], exc: BaseException,
                     savepoint: bool) -> None:
        """A chain entry could not be written (system fault, NOT tampering): operator alert V-004.

        * If SQLite aborted the whole transaction, or the savepoint cannot be undone, raise
          ChainTransactionAborted: the caller's transaction is rolled back completely (nothing
          half-written is ever committed) and the finaliser retries the seal later.
        * Otherwise the evidence change is committed without its entry, and the affected rows are
          remembered in ``ledger_chain_pending`` (same transaction) so their next write records a
          ``protection_gap`` + re-snapshot instead of a false "changed outside the software"."""
        self.chain_failures += 1
        log.critical("could not write recording-log chain entry (%s)", what, exc_info=exc)
        if savepoint:
            try:
                self._undo_savepoint(c)
            except Exception as undo_exc:
                raise ChainTransactionAborted(f"chain savepoint could not be undone ({undo_exc})") from exc
        if not c.in_transaction:
            raise ChainTransactionAborted("SQLite aborted the transaction while writing a chain entry") from exc
        try:
            msg = f"{type(exc).__name__}: {exc}"[:300]
            for table, key in keys:
                c.execute("INSERT OR REPLACE INTO ledger_chain_pending(ref_table, ref_key, failed_utc, error) "
                          "VALUES (?,?,?,?)", (table, str(key), _now_iso(), msg))
            self._pending_maybe = True
        except Exception:
            log.critical("could not remember rows left without a chain entry", exc_info=True)
            if not c.in_transaction:
                raise ChainTransactionAborted("SQLite aborted the transaction while recording a chain fault") from exc

    def _chain_guard(self, c: sqlite3.Connection, what: str, fn: Any, keys: list[tuple[str, Any]]) -> None:
        """Run chain bookkeeping inside a savepoint; failures are handled by _chain_fault."""
        c.execute("SAVEPOINT chain_entry")
        try:
            fn()
        except Exception as exc:
            self._chain_fault(c, what, keys, exc, savepoint=True)
        else:
            c.execute("RELEASE chain_entry")

    def _repair_if_pending(self, c: sqlite3.Connection) -> None:
        if not self._pending_maybe:
            return
        done: list[int] = []
        self._chain_guard(c, "repair", lambda: done.append(chain.repair_pending(c, _now_iso())), [])
        if done:
            self._pending_maybe = False
            if done[0]:
                log.warning("re-recorded %d ledger row(s) whose chain entry had failed (protection gap)", done[0])

    @contextmanager
    def _chained(self, c: sqlite3.Connection, table: str, keys: Iterable[Any]) -> Iterator[None]:
        """Track covered rows around a write in the caller's transaction (see ledgerchain)."""
        keys = list(keys)
        self._repair_if_pending(c)
        tracked: list[chain.Tracked] | None = None
        try:
            tracked = [chain.track(c, table, k) for k in keys]
        except Exception as exc:
            self._chain_fault(c, f"{table} read", [(table, k) for k in keys], exc, savepoint=False)
        yield
        if tracked is None:
            return
        found: list[chain.Discrepancy] = []

        def work() -> None:
            now = _now_iso()
            for t in tracked:
                d = chain.record_change(c, t, now)
                if d is not None:
                    found.append(d)
            for d in found:
                log.critical("ledger %s row %s was changed outside the recorder (expected log entry %s)",
                             d.table, d.key, d.expected_entry)
                cur = c.execute("INSERT INTO events(ts_utc, severity, category, code, message, detail) "
                                "VALUES (?,?,?,?,?,?)",
                                (now, "error", "integrity", "ledger_discrepancy",
                                 "A recording log record was changed outside the recording software",
                                 _j({"table": d.table, "key": d.key, "expected_entry": d.expected_entry,
                                     "discrepancy_entry": d.seq})))
                chain.record_insert(c, "events", cur.lastrowid, now)

        self._chain_guard(c, f"{table} update", work, [(table, t.key) for t in tracked])
        self.chain_discrepancies += len(found)

    def _chain_insert(self, c: sqlite3.Connection, table: str, key: Any) -> None:
        self._repair_if_pending(c)
        self._chain_guard(c, f"{table} insert", lambda: chain.record_insert(c, table, key, _now_iso()),
                          [(table, key)])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        if not self.readonly:
            with self._lock:
                return self._conn.execute(sql, tuple(params)).fetchall()

        def attempt() -> list[sqlite3.Row]:
            with self._lock:  # held only for one attempt; released while _retry_read sleeps
                if self._closed:
                    raise sqlite3.ProgrammingError("Cannot operate on a closed ledger")
                if self._conn is None:
                    self._conn = self._open_readonly()
                try:
                    return self._conn.execute(sql, tuple(params)).fetchall()
                except sqlite3.OperationalError as exc:
                    if _is_transient(exc):  # reopen on the next attempt: this handle may be stale
                        try:
                            self._conn.close()
                        except sqlite3.Error:
                            pass
                        self._conn = None
                    raise
        return self._retry_read(attempt)

    def integrity_check(self) -> str:
        return self.query("PRAGMA integrity_check")[0][0]

    def checkpoint(self) -> None:
        """Fold the WAL back into the main file so it cannot grow without bound over long runs.

        PASSIVE never waits for readers (e.g. a chain verification or diagnostic report holding a
        read snapshot), so the ledger lock - and with it sealing - is never held waiting. Frames a
        reader still needs are copied by a later checkpoint; once the WAL is fully checkpointed it
        is reused from the start and truncated to ``journal_size_limit``."""
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def write_snapshot(self, dest: Path) -> None:
        """Consistent, self-contained copy of the ledger for read-only consumers (operator GUI).

        Operators get read access to this copy only, never to the live ledger: a WAL-mode SQLite
        reader needs *write* access to the ``-shm`` file, which would let any operator account
        disturb the recorder's database. The copy uses the rollback journal (no -wal/-shm), is
        written to a temporary file and atomically replaced, so readers (``mode=ro&immutable=1``)
        never see a partial file. A reader holding the old copy open on Windows makes the replace
        fail; the next interval retries.
        """
        import os

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
        tmp.unlink(missing_ok=True)
        out = sqlite3.connect(tmp, isolation_level=None)
        try:
            with self._lock:
                self._conn.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")
        finally:
            out.close()
        try:
            os.replace(tmp, dest)
        except PermissionError:
            tmp.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------ events
    def _event_in(self, c: sqlite3.Connection, e: EventSpec) -> None:
        cur = c.execute("INSERT INTO events(ts_utc, severity, category, code, message, detail) VALUES (?,?,?,?,?,?)",
                        (_now_iso(), e.severity, e.category, e.code, e.message, _j(e.detail)))
        self._chain_insert(c, "events", cur.lastrowid)

    def add_event(self, severity: str, category: str, code: str, message: str,
                  detail: dict[str, Any] | None = None) -> None:
        with self.tx() as c:
            self._event_in(c, EventSpec(severity, category, code, message, detail))

    # ------------------------------------------------------------------ runs
    def create_run(self, **cols: Any) -> None:
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        with self.tx() as c, self._chained(c, "recording_runs", [cols.get("run_id")]):
            c.execute(f"INSERT INTO recording_runs({keys}) VALUES ({marks})", tuple(cols.values()))

    def update_run(self, run_id: str, **cols: Any) -> None:
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.tx() as c, self._chained(c, "recording_runs", [run_id]):
            c.execute(f"UPDATE recording_runs SET {sets} WHERE run_id=?", (*cols.values(), run_id))

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM recording_runs WHERE run_id=?", (run_id,))
        return rows[0] if rows else None

    def unfinished_runs(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM recording_runs WHERE ended_utc IS NULL ORDER BY rowid")

    def runs_needing_gap(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM recording_runs WHERE ended_utc IS NOT NULL AND gap_recorded=0 "
                          "ORDER BY rowid")

    def mark_run_clock_anomaly(self, run_id: str) -> None:
        with self.tx() as c:
            seg_ids = [r[0] for r in c.execute("SELECT segment_id FROM segments WHERE run_id=?", (run_id,))]
            with self._chained(c, "recording_runs", [run_id]), self._chained(c, "segments", seg_ids):
                c.execute("UPDATE recording_runs SET clock_anomaly=1 WHERE run_id=?", (run_id,))
                c.execute("UPDATE segments SET clock_anomaly=1, updated_utc=? WHERE run_id=?", (_now_iso(), run_id))

    # ------------------------------------------------------------------ segments
    def insert_segment(self, *, camera_id: str, run_id: str, seq: int, rel_path: str,
                       state: SegmentState = SegmentState.ACTIVE, **cols: Any) -> str:
        seg_id = str(uuid.uuid4())
        now = _now_iso()
        base = dict(segment_id=seg_id, camera_id=camera_id, run_id=run_id, seq=seq, rel_path=rel_path,
                    state=str(state), created_utc=now, updated_utc=now)
        base.update(cols)
        keys = ", ".join(base)
        marks = ", ".join("?" for _ in base)
        with self.tx() as c, self._chained(c, "segments", [seg_id]):
            c.execute(f"INSERT INTO segments({keys}) VALUES ({marks})", tuple(base.values()))
        return seg_id

    def get_segment(self, segment_id: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM segments WHERE segment_id=?", (segment_id,))
        return rows[0] if rows else None

    def segment_by_path(self, rel_path: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM segments WHERE rel_path=?", (rel_path,))
        return rows[0] if rows else None

    def segments_in_states(self, states: Iterable[SegmentState]) -> list[sqlite3.Row]:
        st = [str(s) for s in states]
        marks = ",".join("?" for _ in st)
        return self.query(f"SELECT * FROM segments WHERE state IN ({marks}) ORDER BY rowid", st)

    def run_segments(self, run_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM segments WHERE run_id=? ORDER BY seq", (run_id,))

    def update_segment(self, segment_id: str, **cols: Any) -> None:
        """Update metadata. Integrity fields of a sealed segment are write-once."""
        if "state" in cols:
            raise ValueError("use transition() or seal() to change state")
        with self.tx() as c:
            row = c.execute("SELECT * FROM segments WHERE segment_id=?", (segment_id,)).fetchone()
            if row is None:
                raise KeyError(segment_id)
            if SegmentState(row["state"]) in TERMINAL_STATES:
                locked = [k for k in cols if k in WRITE_ONCE and row[k] is not None]
                if locked:
                    raise IllegalTransition(f"sealed segment field(s) are write-once: {', '.join(locked)}")
            cols["updated_utc"] = _now_iso()
            sets = ", ".join(f"{k}=?" for k in cols)
            with self._chained(c, "segments", [segment_id]):
                c.execute(f"UPDATE segments SET {sets} WHERE segment_id=?", (*cols.values(), segment_id))

    def transition(self, segment_id: str, new_state: SegmentState, **cols: Any) -> None:
        with self.tx() as c, self._chained(c, "segments", [segment_id]):
            self._transition_in(c, segment_id, new_state, cols)

    @staticmethod
    def _transition_in(c: sqlite3.Connection, segment_id: str, new_state: SegmentState, cols: dict[str, Any]) -> None:
        row = c.execute("SELECT state FROM segments WHERE segment_id=?", (segment_id,)).fetchone()
        if row is None:
            raise KeyError(segment_id)
        cur = SegmentState(row["state"])
        if new_state not in ALLOWED_TRANSITIONS[cur]:
            raise IllegalTransition(f"segment {segment_id}: {cur} -> {new_state} not allowed")
        cols = dict(cols, state=str(new_state), updated_utc=_now_iso())
        sets = ", ".join(f"{k}=?" for k in cols)
        c.execute(f"UPDATE segments SET {sets} WHERE segment_id=?", (*cols.values(), segment_id))

    def seal(self, segment_id: str, new_state: SegmentState, cols: dict[str, Any],
             internal_gaps: list[InternalGap], close_gap_at: str | None,
             events: list[EventSpec]) -> sqlite3.Row | None:
        """Atomically: transition to a terminal state, inherit the run's clock-anomaly flag,
        record internal gaps, close the preceding outage gap, and write events.
        Returns the closed between-runs gap row, if any."""
        if new_state not in TERMINAL_STATES:
            raise ValueError("seal() requires a terminal state")
        with self.tx() as c:
            seg = c.execute("SELECT camera_id, run_id FROM segments WHERE segment_id=?", (segment_id,)).fetchone()
            if seg is None:
                raise KeyError(segment_id)
            # The segment's chain entry (file SHA-256, times, state) is part of THIS transaction.
            with self._chained(c, "segments", [segment_id]):
                self._transition_in(c, segment_id, new_state, cols)
                c.execute("UPDATE segments SET clock_anomaly=1 WHERE segment_id=? AND "
                          "(SELECT clock_anomaly FROM recording_runs r WHERE r.run_id=segments.run_id)=1",
                          (segment_id,))
            now = _now_iso()
            for g in internal_gaps:
                cur = c.execute("INSERT INTO recording_gaps(camera_id, kind, start_utc, end_utc, duration_s, reason, "
                                "prev_segment_id, next_segment_id, detail, opened_utc, closed_utc) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (seg["camera_id"], str(GapKind.WITHIN_SEGMENT), g.start_utc, g.end_utc,
                                 g.duration_s, g.reason, segment_id, segment_id, _j(g.detail), now, now))
                self._chain_insert(c, "recording_gaps", cur.lastrowid)
            closed = None
            if close_gap_at:
                closed = self._close_gap_in(c, seg["camera_id"], close_gap_at, segment_id, seg["run_id"])
                if closed is not None:
                    self._event_in(c, EventSpec("warning", "recorder", "gap_closed",
                                                f"Recording resumed after a gap of {closed['duration_s'] or 0:.1f} s",
                                                {"gap_id": closed["gap_id"], "reason": closed["reason"]}))
            for e in events:
                self._event_in(c, e)
            return closed

    def record_finalize_failure(self, segment_id: str, error: str) -> int:
        with self.tx() as c:
            c.execute("UPDATE segments SET finalize_attempts=finalize_attempts+1, last_finalize_error=?, "
                      "updated_utc=? WHERE segment_id=?", (error[:500], _now_iso(), segment_id))
            row = c.execute("SELECT finalize_attempts FROM segments WHERE segment_id=?", (segment_id,)).fetchone()
            return int(row["finalize_attempts"]) if row else 0

    def unsealed_segments(self, exclude_run_id: str | None = None) -> list[sqlite3.Row]:
        """Segments that should be sealed but are not: any CLOSED segment, and ACTIVE segments of
        runs that have ended. ACTIVE segments of the run currently recording are excluded."""
        return self.query("SELECT s.* FROM segments s JOIN recording_runs r ON r.run_id=s.run_id "
                          "WHERE s.state='CLOSED' OR (s.state='ACTIVE' AND r.ended_utc IS NOT NULL "
                          "AND s.run_id IS NOT ?) ORDER BY s.rowid", (exclude_run_id,))

    def latest_recorded_segment(self, camera_id: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM segments WHERE camera_id=? AND end_utc IS NOT NULL "
                          "ORDER BY rowid DESC LIMIT 1", (camera_id,))
        return rows[0] if rows else None

    def latest_sealed_segment(self, camera_id: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM segments WHERE camera_id=? AND state IN ('FINALIZED','INTERRUPTED') "
                          "AND end_utc IS NOT NULL ORDER BY rowid DESC LIMIT 1", (camera_id,))
        return rows[0] if rows else None

    def segments_needing_content_hash(self, max_attempts: int = 3) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM segments WHERE state IN ('FINALIZED','INTERRUPTED') "
                          "AND content_sha256 IS NULL AND content_hash_attempts < ? ORDER BY rowid", (max_attempts,))

    def segments_for_audit(self, limit: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM segments WHERE state IN ('FINALIZED','INTERRUPTED','CORRUPT') "
                          "AND sha256 IS NOT NULL ORDER BY (last_audit_utc IS NOT NULL), last_audit_utc, rowid "
                          "LIMIT ?", (limit,))

    def state_counts(self) -> dict[str, int]:
        return {r["state"]: r["n"] for r in self.query("SELECT state, COUNT(*) n FROM segments GROUP BY state")}

    # ------------------------------------------------------------------ gaps
    def open_gap(self, camera_id: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM recording_gaps WHERE camera_id=? AND kind=? AND end_utc IS NULL "
                          "ORDER BY gap_id DESC LIMIT 1", (camera_id, str(GapKind.BETWEEN_RUNS)))
        return rows[0] if rows else None

    def begin_gap(self, camera_id: str, start_utc: str | None, reason: str, prev_segment_id: str | None,
                  ended_run_id: str, start_basis: str, detail: dict[str, Any] | None = None) -> int:
        """Open a between-runs gap, or bump the attempt counter of the one already open.

        A gap that is already open (e.g. camera offline across several retry runs) keeps its
        original start and reason; the latest failed run becomes ``ended_run_id``.
        """
        with self.tx() as c:
            row = c.execute("SELECT gap_id FROM recording_gaps WHERE camera_id=? AND kind=? AND end_utc IS NULL",
                            (camera_id, str(GapKind.BETWEEN_RUNS))).fetchone()
            if row is not None:
                with self._chained(c, "recording_gaps", [row["gap_id"]]):
                    c.execute("UPDATE recording_gaps SET attempts=attempts+1, ended_run_id=? WHERE gap_id=?",
                              (ended_run_id, row["gap_id"]))
                c.execute("UPDATE recording_runs SET gap_recorded=1 WHERE run_id=?", (ended_run_id,))
                return int(row["gap_id"])
            cur = c.execute(
                "INSERT INTO recording_gaps(camera_id, kind, start_utc, reason, prev_segment_id, ended_run_id, "
                "start_basis, detail, opened_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (camera_id, str(GapKind.BETWEEN_RUNS), start_utc, reason, prev_segment_id, ended_run_id,
                 start_basis, _j(detail), _now_iso()))
            self._chain_insert(c, "recording_gaps", cur.lastrowid)
            c.execute("UPDATE recording_runs SET gap_recorded=1 WHERE run_id=?", (ended_run_id,))
            return int(cur.lastrowid or 0)

    def _close_gap_in(self, c: sqlite3.Connection, camera_id: str, end_utc: str, next_segment_id: str,
                      next_run_id: str) -> sqlite3.Row | None:
        row = c.execute("SELECT * FROM recording_gaps WHERE camera_id=? AND kind=? AND end_utc IS NULL",
                        (camera_id, str(GapKind.BETWEEN_RUNS))).fetchone()
        if row is None or row["ended_run_id"] == next_run_id:
            return None
        if row["ended_run_id"] is not None:
            order = {r["run_id"]: r["rowid"] for r in c.execute(
                "SELECT run_id, rowid FROM recording_runs WHERE run_id IN (?, ?)", (row["ended_run_id"], next_run_id))}
            if order.get(next_run_id, -1) <= order.get(row["ended_run_id"], -1):
                return None  # footage from an earlier run (e.g. sealed late) cannot end a later gap
        duration = None
        anomaly = int(row["clock_anomaly"])
        if row["start_utc"]:
            duration = parse_iso_utc(end_utc) - parse_iso_utc(row["start_utc"])
            if duration < 0:
                anomaly = 1  # time went backwards between runs: clock was changed
        with self._chained(c, "recording_gaps", [row["gap_id"]]):
            c.execute("UPDATE recording_gaps SET end_utc=?, duration_s=?, next_segment_id=?, closed_utc=?, "
                      "clock_anomaly=? WHERE gap_id=?",
                      (end_utc, duration, next_segment_id, _now_iso(), anomaly, row["gap_id"]))
        return c.execute("SELECT * FROM recording_gaps WHERE gap_id=?", (row["gap_id"],)).fetchone()

    def close_gap(self, camera_id: str, end_utc: str, next_segment_id: str, next_run_id: str) -> sqlite3.Row | None:
        with self.tx() as c:
            return self._close_gap_in(c, camera_id, end_utc, next_segment_id, next_run_id)

    def flag_open_gap_clock_anomaly(self, camera_id: str) -> None:
        with self.tx() as c:
            ids = [r[0] for r in c.execute("SELECT gap_id FROM recording_gaps WHERE camera_id=? AND end_utc IS NULL",
                                           (camera_id,))]
            with self._chained(c, "recording_gaps", ids):
                c.execute("UPDATE recording_gaps SET clock_anomaly=1 WHERE camera_id=? AND end_utc IS NULL",
                          (camera_id,))

    def add_internal_gap(self, camera_id: str, segment_id: str, start_utc: str | None, end_utc: str | None,
                         duration_s: float | None, reason: str = "timestamp_discontinuity",
                         detail: dict[str, Any] | None = None) -> None:
        now = _now_iso()
        with self.tx() as c:
            cur = c.execute("INSERT INTO recording_gaps(camera_id, kind, start_utc, end_utc, duration_s, reason, "
                            "prev_segment_id, next_segment_id, detail, opened_utc, closed_utc) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (camera_id, str(GapKind.WITHIN_SEGMENT), start_utc, end_utc, duration_s,
                             reason, segment_id, segment_id, _j(detail), now, now))
            self._chain_insert(c, "recording_gaps", cur.lastrowid)

    def gaps(self, camera_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM recording_gaps WHERE camera_id=? ORDER BY gap_id", (camera_id,))
