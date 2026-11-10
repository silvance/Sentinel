"""Tamper-evident hash chain over the evidence ledger, its external anchors, and verification.

    python -m sentinel.ledgerchain verify --config sentinel.toml [--check-files] [--expect-head SEQ:HASH]
    python -m sentinel.ledgerchain head   --config sentinel.toml

What it does (docs/07 "Ledger tamper evidence" is the full statement, including limits):

* Every change the recorder makes to a security-relevant ledger row (recording runs, sealed
  segments, recording gaps, events) appends an entry to the table ``ledger_chain`` in the SAME
  transaction as the change. Entry ``n`` stores a canonical JSON *record* holding a snapshot of
  the covered columns, and ``h_n = SHA-256(hex(h_{n-1}) || record_n)``.
* Before each write the recorder also compares the row's current values with the latest chain
  entry for that row. A difference means the row was changed outside the recorder; the recorder
  then appends a ``discrepancy`` entry (so the finding itself becomes part of the chain and cannot
  be laundered by a later legitimate update) before recording the new values.
* The chain head (sequence, hash, UTC) is copied out ("anchored") to ``chain_heads.log``,
  ``status.json``, every diagnostic report and (via :func:`current_chain_head`) export manifests.
* :func:`verify_ledger` recomputes the chain, compares every covered row with its latest entry,
  compares the chain with every anchored head it can find, and optionally re-hashes the
  segment files. It reports the first position at which the log cannot be confirmed.

This makes tampering DETECTABLE, not impossible: anyone who can rewrite the database can
recompute a consistent chain. Only a comparison with a head copied off the machine earlier
(review drive manifest, diagnostic report sent to support, a head written down) exposes that.

Chain format 1 (normative; an examiner can re-implement verification from this alone)
-------------------------------------------------------------------------------------
Table ``ledger_chain(seq, kind, ref_table, ref_key, record, prev_hash, entry_hash)``.
``record`` is the exact text that was hashed. ``kind``/``ref_table``/``ref_key`` are lookup copies
of values inside ``record``; only ``record``, ``prev_hash`` and ``entry_hash`` carry evidence.

1. Sequence numbers start at 0 and increase by exactly 1. Entry 0 is ``kind = "genesis"``.
2. ``prev_hash`` of entry 0 is 64 ASCII zeros; of entry n > 0 it is ``entry_hash`` of entry n-1.
3. ``entry_hash = lowercase_hex(SHA-256(ASCII(prev_hash) || UTF-8(record)))`` where ``prev_hash``
   is the 64-character lowercase hex string (not the raw 32 bytes).
4. ``record`` is the canonical JSON of the object
   ``{"v": 1, "seq": n, "utc": <ISO-8601 UTC of writing>, "kind": <kind>, "table": <table|null>,
   "key": <primary key as a string|null>, "data": <object|null>}``.
5. Canonical JSON = RFC 8785 (JSON Canonicalization Scheme): no whitespace; object members
   sorted by key (UTF-16 code units; all Sentinel keys are ASCII); strings escaped as in
   RFC 8785 (``\\" \\\\ \\b \\f \\n \\r \\t``, other U+0000..U+001F as ``\\u00xx`` lowercase, everything
   else literal UTF-8); numbers in the ECMAScript Number-to-String form RFC 8785 mandates
   (shortest round-trip digits; e.g. ``5.0 -> 5``, ``0.000001``, ``1e-7``, ``1e+21``; -0 -> ``0``).
   Two documented deviations, neither reachable from Sentinel's own writes: integers are
   written as exact decimal digits even beyond 2^53, and the non-finite floats are written as the
   JSON strings ``"NaN"``, ``"Infinity"``, ``"-Infinity"``. SQLite BLOBs are written as the string
   ``x'<lowercase hex>'``.
6. ``data`` for a row entry is the row's *covered columns* (see ``COVERED``) with their SQLite
   values (NULL -> null, INTEGER -> integer, REAL -> number, TEXT -> string). Columns that hold
   JSON (``detail``, ``warnings``...) are hashed as the TEXT they are stored as.
7. A row's current covered values must equal ``data`` of the latest entry naming that
   ``table``/``key``. Kinds with ``table = null`` (``genesis``, ``discrepancy``, ``protection_gap``)
   describe the log itself, not a row.
8. Between consecutive entries of the same row only the recorder's own transitions are allowed
   (:func:`illegal_changes`: sealed segments' integrity fields are write-once and their state is
   final, events never change, closed gaps and ended runs never change). ``genesis`` appears only
   at 0 and ``adopted`` only directly after it. This is what stops a well-formed APPENDED entry from
   changing history the recorder treats as final; a key-less chain cannot stop rule-compliant
   appends after the last head copied off the machine (docs/07).
Anchor log line: ``<anchored UTC> seq=<n> hash=<64 hex> entry_utc=<UTC of entry n> reason=<word>``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from json.encoder import encode_basestring as _escape   # C-accelerated; = json.dumps(s, ensure_ascii=False)
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .clock import iso_utc

if TYPE_CHECKING:
    from .db import Ledger

log = logging.getLogger(__name__)

CHAIN_FORMAT = 1
ZERO_HASH = "0" * 64
ANCHOR_LOG_NAME = "chain_heads.log"
TERMINAL = frozenset({"FINALIZED", "INTERRUPTED", "CORRUPT"})
STRUCTURAL_KINDS = frozenset({"genesis", "discrepancy", "protection_gap"})   # describe the log, not a row

# ---------------------------------------------------------------------- covered columns
# Columns NOT listed are bookkeeping that legitimately changes without evidential meaning
# (retry counters, last audit time, updated_utc, gap_recorded). Never remove a column from
# these lists in a released format: that would make old entries unverifiable. Adding coverage
# for a new column requires a new CHAIN_FORMAT.
RUN_FIELDS = ("run_id", "camera_id", "directory", "launched_utc", "launch_mono", "anchor_utc", "anchor_method",
              "timeline_break_stream_s", "timeline_shift_s", "display_timezone", "ended_utc", "end_reason",
              "exit_code", "end_detail", "clock_anomaly", "app_version", "ffmpeg_version", "host",
              "command_redacted",
              # Integration with design/time-accuracy (ledger_timing.RUN_COLUMNS), added to format 1
              # before its first release: how the run's segment times were derived and measured drift.
              "time_method", "time_correction_s", "camera_drift_ppm", "host_clock_drift_ppm",
              "drift_measured_over_s", "time_correction_limited_count")
RUN_END_FIELDS = ("last_stream_time_s",)      # persisted every ~10 s while running; covered once ended
SEGMENT_FIELDS = ("segment_id", "camera_id", "run_id", "seq", "rel_path", "state", "stream_start_s",
                  "stream_end_s", "start_utc", "end_utc", "time_source", "observed_first_utc",
                  "observed_closed_utc", "duration_s", "size_bytes", "sha256", "content_sha256",
                  "content_hash_method", "decoded_frame_count", "framehash_rel_path", "framehash_sha256",
                  "pix_fmt", "video_codec", "width", "height", "packet_count", "keyframe_count",
                  "max_pts_gap_s", "interrupted", "clock_anomaly", "warnings", "validation_detail",
                  "sealed_utc", "audit_status", "created_utc",
                  # Integration with design/time-accuracy (ledger_timing.SEGMENT_COLUMNS): the
                  # correction that went into start_utc/end_utc, so the derivation is protected too.
                  "time_correction_start_s", "time_correction_end_s", "time_correction_stream_s",
                  "time_correction_limited", "time_correction_detail",
                  # Integration with design/local-retention: when the local original was removed
                  # (set once, on a sealed segment, by the retention rule).
                  "local_deleted_utc")
GAP_FIELDS = ("gap_id", "camera_id", "kind", "start_utc", "end_utc", "duration_s", "reason", "prev_segment_id",
              "next_segment_id", "ended_run_id", "start_basis", "attempts", "clock_anomaly", "detail",
              "opened_utc", "closed_utc")
EVENT_FIELDS = ("event_id", "ts_utc", "severity", "category", "code", "message", "detail")
# Integration with design/time-accuracy: clock observations are E1 evidence (how accurate the
# PC clock was); insert-only, like events.
CLOCK_OBSERVATION_FIELDS = ("obs_id", "observed_utc", "mono_s", "reason", "platform", "sync_tool", "synchronized",
                            "sync_source", "last_sync_utc", "os_offset_s", "os_detail", "wall_minus_mono_s",
                            "host_clock_drift_s", "host_clock_drift_ppm", "clock_steps_total_s", "run_id",
                            "stream_time_s", "camera_drift_ppm", "time_correction_s", "ref_server", "ref_offset_s",
                            "ref_delay_s", "ref_detail")
INSERT_ONLY = frozenset({"events", "clock_observations"})   # rows never change once written
# Integration with design/archive-drive + design/local-retention (security-relevant: what is
# archived where, when an archive drive left the machine, and every local deletion). Each table
# may change only in the one way its writer changes it (see illegal_changes).
ARCHIVE_COPY_FIELDS = ("copy_id", "segment_id", "drive_id", "kind", "path", "sha256", "size_bytes", "verify_method",
                       "already_present", "verified_utc", "state", "lost_utc", "lost_reason")
ARCHIVE_PROBLEM_FIELDS = ("problem_id", "drive_id", "kind", "segment_id", "path", "detail", "created_utc",
                          "acknowledged_utc", "acknowledged_by")
DRIVE_SESSION_FIELDS = ("session_id", "drive_id", "seen_utc", "gone_utc", "gone_how")
RETENTION_FIELDS = ("deletion_id", "segment_id", "state", "rel_path", "sha256", "size_bytes", "framehash_rel_path",
                    "archive_copies", "reason", "free_before", "free_after", "decided_utc", "deleted_utc")
# table -> (kind for the first entry, kind for later entries)
_TABLE_KINDS = {"archive_copies": ("archive_copy_recorded", "archive_copy_updated"),
                "archive_problems": ("archive_problem_recorded", "archive_problem_updated"),
                "archive_drive_sessions": ("drive_session_opened", "drive_session_closed"),
                "retention_deletions": ("retention_decided", "retention_updated")}
# table -> (columns that may go from NULL to a value once, {column: (from, to)} allowed transitions)
_SET_ONCE_RULES: dict[str, tuple[frozenset[str], dict[str, tuple[Any, Any]]]] = {
    "archive_copies": (frozenset({"lost_utc", "lost_reason"}), {"state": ("VERIFIED", "LOST")}),
    "archive_problems": (frozenset({"acknowledged_utc", "acknowledged_by"}), {}),
    "archive_drive_sessions": (frozenset({"gone_utc", "gone_how"}), {}),
    "retention_deletions": (frozenset({"deleted_utc", "free_after"}), {"state": ("DELETING", "DONE")}),
}

# table -> (primary key column, covered columns). Segments are covered once sealed (terminal state).
COVERED: dict[str, tuple[str, tuple[str, ...]]] = {
    "recording_runs": ("run_id", RUN_FIELDS),
    "segments": ("segment_id", SEGMENT_FIELDS),
    "recording_gaps": ("gap_id", GAP_FIELDS),
    "events": ("event_id", EVENT_FIELDS),
    "clock_observations": ("obs_id", CLOCK_OBSERVATION_FIELDS),
    "archive_copies": ("copy_id", ARCHIVE_COPY_FIELDS),
    "archive_problems": ("problem_id", ARCHIVE_PROBLEM_FIELDS),
    "archive_drive_sessions": ("session_id", DRIVE_SESSION_FIELDS),
    "retention_deletions": ("deletion_id", RETENTION_FIELDS),
}
_LATER_TABLES = frozenset({"clock_observations", *_TABLE_KINDS})   # may be absent from older ledgers
_NOUN = {"recording_runs": "recording run", "segments": "recording segment", "recording_gaps": "recording gap",
         "events": "log event", "clock_observations": "clock observation", "archive_copies": "archive copy",
         "archive_problems": "archive problem record", "archive_drive_sessions": "archive drive presence record",
         "retention_deletions": "local removal record"}


class ChainError(RuntimeError):
    pass


# ---------------------------------------------------------------------- canonical JSON (RFC 8785)
_SURROGATE = re.compile("[\ud800-\udfff]")


def _jcs_string(s: str) -> str:
    out = _escape(s)                                 # json.dumps(s, ensure_ascii=False): RFC 8785 escaping
    if not out.isascii():
        try:
            out.encode("utf-8")
        except UnicodeEncodeError:                   # lone surrogates: escape like ES2019 JSON.stringify
            out = _SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", out)
    return out


def _jcs_number(x: float) -> str:
    """ECMAScript Number::toString (RFC 8785 §3.2.2.3) from Python's shortest round-trip repr."""
    if math.isnan(x):
        return '"NaN"'
    if math.isinf(x):
        return '"Infinity"' if x > 0 else '"-Infinity"'
    if x == 0:
        return "0"
    r = repr(x)
    sign = ""
    if r[0] == "-":
        sign, r = "-", r[1:]
    mant, _, exp_s = r.partition("e")
    exp = int(exp_s) if exp_s else 0
    ip, _, fp = mant.partition(".")
    digits = (ip + fp).lstrip("0")
    stripped = digits.rstrip("0")
    k = len(stripped)
    n = exp - len(fp) + (len(digits) - k) + k   # value = 0.<stripped> * 10^n
    if k <= n <= 21:
        out = stripped + "0" * (n - k)
    elif 0 < n <= 21:
        out = stripped[:n] + "." + stripped[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + stripped
    else:
        e = n - 1
        out = stripped[0] + ("." + stripped[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + out


def _enc(v: Any, out: list[str]) -> None:
    t = type(v)
    if t is str:
        out.append(_jcs_string(v))
    elif v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, int):
        out.append(str(int(v)))
    elif isinstance(v, float):
        out.append(_jcs_number(v))
    elif isinstance(v, str):
        out.append(_jcs_string(v))
    elif isinstance(v, (bytes, bytearray, memoryview)):
        out.append(_jcs_string("x'" + bytes(v).hex() + "'"))
    elif isinstance(v, dict):
        if not all(type(k) is str for k in v):
            raise TypeError("canonical JSON keys must be strings")
        keys = sorted(v)
        if not all(k.isascii() for k in keys):       # RFC 8785: order by UTF-16 code units
            keys = sorted(v, key=lambda k: k.encode("utf-16-be"))
        out.append("{")
        for i, k in enumerate(keys):
            if i:
                out.append(",")
            out.append(_jcs_string(k))
            out.append(":")
            _enc(v[k], out)
        out.append("}")
    elif isinstance(v, (list, tuple)):
        out.append("[")
        for i, item in enumerate(v):
            if i:
                out.append(",")
            _enc(item, out)
        out.append("]")
    else:
        raise TypeError(f"cannot canonicalise {type(v).__name__}")


def canonical_json(value: Any) -> str:
    """RFC 8785 canonical JSON text (see module docstring for the two documented deviations)."""
    out: list[str] = []
    _enc(value, out)
    return "".join(out)


def make_record(seq: int, utc: str, kind: str, table: str | None, key: str | None, data_c: str) -> str:
    """Assemble the canonical record from already-canonical ``data`` text. Equivalent to
    ``canonical_json({"v":1,"seq":..,"utc":..,"kind":..,"table":..,"key":..,"data":data})``
    (members in sorted order: data, key, kind, seq, table, utc, v)."""
    def s(x: Any) -> str:
        return _jcs_string(x) if type(x) is str else canonical_json(x)
    return ('{"data":' + data_c + ',"key":' + s(key) + ',"kind":' + s(kind)
            + ',"seq":' + str(int(seq)) + ',"table":' + s(table) + ',"utc":' + s(utc)
            + ',"v":' + str(CHAIN_FORMAT) + "}")


def entry_hash(prev_hash: str, record: str) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + record.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- projections
def projection(table: str, row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    """Covered values of ``row`` (None if the row is absent or not yet covered)."""
    if row is None:
        return None
    _, fields = COVERED[table]
    if table == "segments" and row["state"] not in TERMINAL:
        return None
    d = {f: row[f] for f in fields}
    if table == "recording_runs" and row["ended_utc"] is not None:
        for f in RUN_END_FIELDS:
            d[f] = row[f]
    return d


def _derive_kind(table: str, prev: dict[str, Any] | None, post: dict[str, Any] | None) -> str:
    if post is None:
        return "row_uncovered"
    if table == "events":
        return "event"
    if table == "clock_observations":
        return "clock_observation"
    if table in _TABLE_KINDS:
        return _TABLE_KINDS[table][0 if prev is None else 1]
    if table == "recording_runs":
        if prev is None:
            return "run_started"
        return "run_ended" if prev.get("ended_utc") is None and post.get("ended_utc") is not None else "run_updated"
    if table == "segments":
        return "segment_sealed" if prev is None else "segment_updated"
    if prev is None:
        within = post.get("kind") == "within_segment"
        return "gap_recorded" if within or post.get("end_utc") is not None else "gap_opened"
    return "gap_closed" if prev.get("end_utc") is None and post.get("end_utc") is not None else "gap_updated"


# ---------------------------------------------------------------------- writer (inside the caller's transaction)
@dataclass(frozen=True)
class ChainHead:
    seq: int
    hash: str
    utc: str            # when entry `seq` was written (host clock)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Tracked:
    table: str
    key: Any
    pre: dict[str, Any] | None
    latest_seq: int | None
    latest_data_c: str | None


@dataclass(frozen=True)
class Discrepancy:
    table: str
    key: str
    expected_entry: int | None
    seq: int


def _fetch_row(c: sqlite3.Connection, table: str, key: Any) -> sqlite3.Row | None:
    pk, _ = COVERED[table]
    return c.execute(f"SELECT * FROM {table} WHERE {pk}=?", (key,)).fetchone()


def _data_of(record: str) -> Any:
    return json.loads(record)["data"]


def _latest_entry(c: sqlite3.Connection, table: str, key: Any) -> tuple[int, str] | None:
    row = c.execute("SELECT seq, record FROM ledger_chain WHERE ref_table=? AND ref_key=? "
                    "ORDER BY seq DESC LIMIT 1", (table, str(key))).fetchone()
    if row is None:
        return None
    return int(row[0]), canonical_json(_data_of(row[1]))


def head_in(c: sqlite3.Connection) -> ChainHead | None:
    row = c.execute("SELECT seq, entry_hash, record FROM ledger_chain ORDER BY seq DESC LIMIT 1").fetchone()
    if row is None:
        return None
    try:
        utc = json.loads(row[2]).get("utc", "")
    except (ValueError, AttributeError):
        utc = ""
    return ChainHead(int(row[0]), str(row[1]), str(utc))


def append_entry(c: sqlite3.Connection, kind: str, table: str | None, key: str | None, data: Any,
                 utc: str) -> ChainHead:
    """Append one entry. If the chain is empty (it was emptied while the recorder was running) a
    genesis entry marking the restart is written first, so recording never stops over it."""
    head = c.execute("SELECT seq, entry_hash FROM ledger_chain ORDER BY seq DESC LIMIT 1").fetchone()
    if head is None:
        if kind != "genesis":
            log.critical("ledger chain is empty while the recorder is running; starting a marked new chain")
            append_entry(c, "genesis", None, None, _genesis_data(c, previous_missing=True, adopted=0,
                                                                   last_anchor=None), utc)
            head = c.execute("SELECT seq, entry_hash FROM ledger_chain ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = int(head[0]) + 1, str(head[1])
        else:
            seq, prev = 0, ZERO_HASH
    else:
        seq, prev = int(head[0]) + 1, str(head[1])
    record = make_record(seq, utc, kind, table, key, canonical_json(data))
    h = entry_hash(prev, record)
    c.execute("INSERT INTO ledger_chain(seq, kind, ref_table, ref_key, record, prev_hash, entry_hash) "
              "VALUES (?,?,?,?,?,?,?)", (seq, kind, table, key, record, prev, h))
    return ChainHead(seq, h, utc)


def track(c: sqlite3.Connection, table: str, key: Any) -> Tracked:
    """Snapshot a row's covered values and its latest chain entry BEFORE a write."""
    latest = _latest_entry(c, table, key)
    return Tracked(table, key, projection(table, _fetch_row(c, table, key)),
                   latest[0] if latest else None, latest[1] if latest else None)


def record_change(c: sqlite3.Connection, t: Tracked, utc: str) -> Discrepancy | None:
    """AFTER a write: append a discrepancy entry if the row had been changed outside the recorder
    since its latest entry, then an entry for the new covered values if they changed."""
    found_c = canonical_json(t.pre) if t.pre is not None else None
    disc = None
    if found_c != t.latest_data_c:
        h = append_entry(c, "discrepancy", None, None,
                         {"table": t.table, "key": str(t.key), "expected_entry": t.latest_seq,
                          "expected": json.loads(t.latest_data_c) if t.latest_data_c else None,
                          "found": t.pre}, utc)
        disc = Discrepancy(t.table, str(t.key), t.latest_seq, h.seq)
    post = projection(t.table, _fetch_row(c, t.table, t.key))
    post_c = canonical_json(post) if post is not None else None
    if post_c != found_c or disc is not None:
        if post is not None or t.latest_seq is not None:
            append_entry(c, _derive_kind(t.table, t.pre, post), t.table, str(t.key), post, utc)
    return disc


def record_insert(c: sqlite3.Connection, table: str, key: Any, utc: str) -> None:
    """Entry for a freshly inserted row (no pre-image to check)."""
    post = projection(table, _fetch_row(c, table, key))
    if post is not None:
        append_entry(c, _derive_kind(table, None, post), table, str(key), post, utc)


def repair_pending(c: sqlite3.Connection, utc: str) -> int:
    """Re-record rows whose chain entry could not be written earlier (``ledger_chain_pending``).

    For each: a structural ``protection_gap`` entry (table/key, when and why the entry failed),
    then a ``chain_repair`` snapshot of the row as it is now. Verification reports the gap as a
    protection fault (V-004), not as tampering, but still checks that the change across the gap
    is one the recorder makes (``illegal_changes``). Returns the number of rows repaired."""
    rows = c.execute("SELECT ref_table, ref_key, failed_utc, error FROM ledger_chain_pending ORDER BY rowid").fetchall()
    for table, key, failed, err in rows:
        if table in COVERED:
            append_entry(c, "protection_gap", None, None,
                         {"table": table, "key": key, "failed_utc": failed, "error": err}, utc)
            post = projection(table, _fetch_row(c, table, key))
            latest = _latest_entry(c, table, key)
            post_c = canonical_json(post) if post is not None else None
            if (latest is None and post is not None) or (latest is not None and post_c != latest[1]):
                append_entry(c, "chain_repair", table, str(key), post, utc)
        c.execute("DELETE FROM ledger_chain_pending WHERE ref_table=? AND ref_key=?", (table, key))
    return len(rows)


def _genesis_data(c: sqlite3.Connection, *, previous_missing: bool, adopted: int,
                  last_anchor: dict[str, Any] | None) -> dict[str, Any]:
    from . import __version__
    counts = {t: int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in COVERED}
    return {"chain_format": CHAIN_FORMAT, "software_version": __version__, "pre_existing_rows": counts,
            "adopted_rows": adopted, "previous_chain_missing": previous_missing,
            "last_anchor_before_restart": last_anchor,
            "note": ("Rows that already existed when this entry was written were adopted into the chain "
                     "at this time. Changes made to them before this time cannot be detected.")}


def ensure_genesis(c: sqlite3.Connection, anchor_log: Path | None) -> bool:
    """Start the chain if it is empty (new ledger, or a ledger created before this feature).
    Must be called with the connection in autocommit mode; runs its own transaction.
    Returns True if a genesis entry was written."""
    if c.execute("SELECT 1 FROM ledger_chain LIMIT 1").fetchone() is not None:
        return False
    c.execute("BEGIN IMMEDIATE")
    try:
        if c.execute("SELECT 1 FROM ledger_chain LIMIT 1").fetchone() is not None:
            c.execute("ROLLBACK")
            return False
        had = c.execute("SELECT value FROM schema_meta WHERE key='chain_genesis_utc'").fetchone()
        anchors = read_anchor_log(anchor_log)[0] if anchor_log is not None else []
        last = anchors[-1] if anchors else None
        previous_missing = had is not None or last is not None
        utc = iso_utc(time.time())
        to_adopt: list[tuple[str, str, dict[str, Any]]] = []
        for table, (pk, _) in COVERED.items():
            for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                p = projection(table, row)
                if p is not None:
                    to_adopt.append((table, str(row[pk]), p))
        append_entry(c, "genesis", None, None,
                     _genesis_data(c, previous_missing=previous_missing, adopted=len(to_adopt),
                                   last_anchor=last.to_dict() if last else None), utc)
        for table, key, p in to_adopt:
            append_entry(c, "adopted", table, key, p, utc)
        c.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('chain_genesis_utc', ?)", (utc,))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    if previous_missing:
        log.critical("ledger chain was missing although an earlier chain existed; a marked new chain was started")
    return True


# ---------------------------------------------------------------------- anchoring
@dataclass(frozen=True)
class Anchor:
    seq: int
    hash: str
    source: str                 # where this copy of the head came from
    anchored_utc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_ANCHOR_RE = re.compile(r"^(?P<utc>\S+) seq=(?P<seq>\d+) hash=(?P<hash>[0-9a-f]{64}) entry_utc=(?P<eutc>\S*) "
                        r"reason=(?P<reason>\S+)$")


def current_chain_head(source: Ledger | sqlite3.Connection | Path | str) -> ChainHead | None:
    """The latest chain entry (sequence, hash, UTC written). For export manifests, status and
    diagnostics. Returns None if the ledger has no chain."""
    try:
        if isinstance(source, sqlite3.Connection):
            return head_in(source)
        if isinstance(source, (str, Path)):
            conn = _ro_connect(Path(source))
            try:
                return head_in(conn)
            finally:
                conn.close()
        return source.chain_head()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise


def append_anchor(log_path: Path, head: ChainHead, reason: str,
                  fsync_dir: Callable[[Path], None] | None = None) -> str:
    """Append one line to the append-only anchor log and fsync it. Returns the line."""
    if not re.fullmatch(r"[a-z0-9_]+", reason):
        raise ValueError(f"anchor reason must be a lowercase word: {reason!r}")
    line = f"{iso_utc(time.time())} seq={head.seq} hash={head.hash} entry_utc={head.utc} reason={reason}\n"
    new = not log_path.exists()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="ascii", newline="\n") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    if new and fsync_dir is not None:
        fsync_dir(log_path.parent)
    return line.rstrip("\n")


def read_anchor_log(path: Path | None) -> tuple[list[Anchor], list[str]]:
    """Parse the anchor log. Returns (anchors, problems); unreadable lines are reported, not fatal
    (a power cut can leave a partial last line)."""
    if path is None or not path.exists():
        return [], []
    anchors: list[Anchor] = []
    problems: list[str] = []
    for i, raw in enumerate(path.read_text("ascii", errors="replace").splitlines(), 1):
        if not raw.strip():
            continue
        m = _ANCHOR_RE.match(raw.strip())
        if m is None:
            problems.append(f"line {i} of the saved-fingerprint log could not be read")
            continue
        anchors.append(Anchor(int(m["seq"]), m["hash"], f"in {path.name} line {i} ({m['reason']})", m["utc"]))
    return anchors, problems


def parse_expected_head(text: str, source: str = "given on the command line") -> Anchor:
    m = re.fullmatch(r"(\d+):([0-9a-fA-F]{64})", text.strip())
    if m is None:
        raise ValueError(f"expected SEQ:HASH (64 hex digits), got {text!r}")
    return Anchor(int(m[1]), m[2].lower(), source)


# ---------------------------------------------------------------------- recorder semantics (format 1)
# What the recorder legitimately does to a covered row AFTER its first chain entry. Verification
# flags every later entry that does anything else. This matters because the chain has no key:
# someone who edits the database can APPEND a well-formed entry for the edit. Hash links and
# anchors only prove that history was not REWRITTEN; these rules make an appended entry that
# changes something the recorder treats as final (a sealed segment's hash, size, times or state,
# a written event, a closed gap, an ended run) a finding too. docs/07 states what remains possible.
SEGMENT_WRITE_ONCE = frozenset({          # must equal db.WRITE_ONCE (unit-tested)
    "sha256", "size_bytes", "rel_path", "start_utc", "end_utc", "duration_s",
    "content_sha256", "content_hash_method", "decoded_frame_count", "framehash_rel_path", "framehash_sha256",
    # integration: ledger_timing.TIMING_WRITE_ONCE (db.WRITE_ONCE includes it)
    "time_correction_start_s", "time_correction_end_s", "time_correction_stream_s",
})
_SEG_FREE = frozenset({"audit_status"})                  # rolling audit result: may change any time
_SEG_SET_ONCE = frozenset({"local_deleted_utc"})         # retention: NULL -> time, once (integration)
_RAISE_ONLY = frozenset({"clock_anomaly"})               # 0 -> 1 only
_RUN_END = frozenset({"ended_utc", "end_reason", "exit_code", "end_detail", "last_stream_time_s",
                      # integration (time-accuracy): updated at every segment boundary while running
                      "time_correction_s", "camera_drift_ppm", "host_clock_drift_ppm", "drift_measured_over_s",
                      "time_correction_limited_count"})
_RUN_WRITE_ONCE = frozenset({"anchor_utc", "anchor_method", "timeline_break_stream_s", "timeline_shift_s",
                             "time_method"})
_GAP_WHILE_OPEN = frozenset({"ended_run_id", "end_utc", "duration_s", "next_segment_id", "closed_utc"})
VALID_KINDS = frozenset({"genesis", "adopted", "run_started", "run_updated", "run_ended", "segment_sealed",
                         "segment_updated", "gap_opened", "gap_recorded", "gap_updated", "gap_closed", "event",
                         "clock_observation", "discrepancy", "protection_gap", "chain_repair",
                         *(k for pair in _TABLE_KINDS.values() for k in pair)})


def _same(a: Any, b: Any) -> bool:
    return canonical_json(a) == canonical_json(b)


def _raised(a: Any, b: Any) -> bool:
    return a in (0, None) and b == 1


def illegal_changes(table: str, prev: dict[str, Any], cur: dict[str, Any]) -> list[str]:
    """Why the change ``prev -> cur`` between two entries of one row is NOT something the recorder
    does (empty list = a legitimate change)."""
    if table in INSERT_ONLY:
        changed = _diff(prev, cur) if not _same(prev, cur) else "entered twice"
        return [f"{_NOUN[table]}s are never changed once written ({changed})"]
    out: list[str] = []
    for f in sorted(set(prev) | set(cur)):
        a, b = prev.get(f), cur.get(f)
        if _same(a, b) or (f in _RAISE_ONLY and _raised(a, b)):
            continue
        if table == "segments":
            if f in _SEG_FREE or ((f in SEGMENT_WRITE_ONCE or f in _SEG_SET_ONCE) and a is None):
                continue
            when = "after the segment was sealed"
        elif table in _SET_ONCE_RULES:
            once, transitions = _SET_ONCE_RULES[table]
            if (f in once and a is None) or transitions.get(f) == (a, b):
                continue
            when = "after it was recorded"
        elif table == "recording_gaps":
            if prev.get("closed_utc") is None and (f in _GAP_WHILE_OPEN or (
                    f == "attempts" and isinstance(a, int) and isinstance(b, int) and b >= a)):
                continue
            when = "after the gap was closed" if prev.get("closed_utc") is not None else "after the gap was opened"
        elif table == "recording_runs":
            ended = prev.get("ended_utc") is not None
            if not ended and (f in _RUN_END or (f in _RUN_WRITE_ONCE and a is None)):
                continue
            when = "after the run ended" if ended else "after the run started"
        else:
            when = ""
        out.append(f"{f} changed from '{_short(a)}' to '{_short(b)}' {when}".rstrip())
    return out


# ---------------------------------------------------------------------- verification
@dataclass
class Finding:
    position: int | None        # chain sequence number at which the log stops being confirmable
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerifyReport:
    checked_utc: str
    entries: int = 0
    head: ChainHead | None = None
    findings: list[Finding] = field(default_factory=list)   # signs of alteration -> V-003
    faults: list[Finding] = field(default_factory=list)     # protection gaps (system faults) -> V-004
    notes: list[str] = field(default_factory=list)
    anchors_checked: int = 0
    rows_checked: int = 0
    files_checked: int = 0
    seconds: float = 0.0
    transactions: int = 0
    longest_read_s: float = 0.0     # longest single read snapshot held (checkpoint / WAL impact)

    @property
    def ok(self) -> bool:
        return not self.findings

    @staticmethod
    def _sorted(items: list[Finding]) -> list[Finding]:
        return sorted(items, key=lambda f: (f.position is None, f.position or 0))

    def sorted_findings(self) -> list[Finding]:
        return self._sorted(self.findings)

    @property
    def first(self) -> Finding | None:
        s = self.sorted_findings()
        return s[0] if s else None

    def to_dict(self, max_findings: int = 200) -> dict[str, Any]:
        return {"ok": self.ok, "checked_utc": self.checked_utc, "entries": self.entries,
                "head": self.head.to_dict() if self.head else None,
                "first_problem": self.first.to_dict() if self.first else None,
                "findings": [f.to_dict() for f in self.sorted_findings()[:max_findings]],
                "findings_total": len(self.findings),
                "protection_gaps": [f.to_dict() for f in self._sorted(self.faults)[:max_findings]],
                "protection_gaps_total": len(self.faults), "notes": self.notes,
                "anchors_checked": self.anchors_checked, "rows_checked": self.rows_checked,
                "files_checked": self.files_checked, "seconds": round(self.seconds, 3),
                "transactions": self.transactions, "longest_read_s": round(self.longest_read_s, 3)}

    def render_text(self, max_findings: int = 50) -> str:
        lines = ["RECORDING LOG CHECK", "==================="]
        if not self.ok:
            f = self.first
            assert f is not None
            where = f"at log entry {f.position}" if f.position is not None else "(position unknown)"
            lines += ["RESULT: ALTERED - the recording log has been altered.",
                      f"First problem {where}: {f.message}"]
        elif self.faults:
            lines.append(f"RESULT: INTACT, WITH {len(self.faults)} PROTECTION GAP(S) - no sign of alteration, but "
                         "some log entries could not be written when they should have been (system fault).")
        else:
            lines.append("RESULT: INTACT - no sign that the recording log was altered.")
        lines.append(f"Checked: {self.entries} log entries, {self.rows_checked} records, "
                     f"{self.anchors_checked} saved copies of the log fingerprint, {self.files_checked} recording files.")
        if self.head:
            lines.append(f"Log head: entry {self.head.seq}, fingerprint {self.head.hash}, written {self.head.utc}")
        for title, items in (("All problems", self.findings), ("Protection gaps", self.faults)):
            if len(items) > (1 if items is self.findings else 0):
                lines += ["", f"{title} ({len(items)}):"]
                for x in self._sorted(items)[:max_findings]:
                    pos = f"entry {x.position}" if x.position is not None else "-"
                    lines.append(f"  [{pos}] {x.message}")
                if len(items) > max_findings:
                    lines.append(f"  ... and {len(items) - max_findings} more")
        if self.notes:
            lines += ["", "Notes:"] + [f"  - {n}" for n in self.notes]
        lines += ["", "This check shows whether the log is consistent with itself, with the rules the recording "
                      "software follows, and with saved copies of its fingerprint. It cannot rule out entries added "
                      "after the last fingerprint saved off this computer (review drive, diagnostic report) that "
                      "follow those rules; see the security documentation."]
        return "\n".join(lines)


def _ro_connect(db_path: Path, retry_seconds: float | None = None) -> sqlite3.Connection:
    """Read-only connection for verification / head reads.

    Integration with fix/ledger-reader-retry: right after a recorder process is killed, opening
    or first querying the WAL ledger can fail for about a second with transient SQLite errors
    ("disk I/O error", "database is locked", "unable to open database file"). The open and a first
    probe query are retried with the same rule and budget as ``db.Ledger`` readers
    (``db.READ_RETRY_SECONDS``); other errors are raised at once."""
    from .db import READ_RETRY_SECONDS, _is_transient

    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    deadline = time.monotonic() + (READ_RETRY_SECONDS if retry_seconds is None else retry_seconds)
    delay = 0.1
    while True:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            if not _is_transient(exc) or time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def _label(table: str, key: str, data: dict[str, Any] | None) -> str:
    d = data or {}
    if table == "segments" and d.get("rel_path"):
        return f"{d['rel_path']}"
    if table == "events" and d.get("code"):
        return f"#{key} ({d['code']})"
    return f"{key}" if table == "recording_runs" else f"#{key}"


def _short(v: Any) -> str:
    s = "empty" if v is None else str(v)
    return s if len(s) <= 70 else s[:67] + "..."


def _diff(expected: dict[str, Any] | None, found: dict[str, Any] | None) -> str:
    e, f = expected or {}, found or {}
    parts = []
    for k in sorted(set(e) | set(f)):
        if canonical_json(e.get(k)) != canonical_json(f.get(k)):
            parts.append(f"{k} was '{_short(e.get(k))}' and is now '{_short(f.get(k))}'")
    return "; ".join(parts) if parts else "values differ"


def status_anchor(status_path: Path) -> Anchor | None:
    try:
        doc = json.loads(status_path.read_text("utf-8"))
        h = (doc.get("integrity") or {}).get("ledger_chain") or {}
        if isinstance(h.get("seq"), int) and isinstance(h.get("hash"), str):
            return Anchor(h["seq"], h["hash"], "in the status file", doc.get("generated_utc"))
    except (OSError, ValueError, AttributeError):
        pass
    return None


MAX_MARKERS = 10_000            # bound on remembered discrepancy / protection-gap positions


def verify_ledger(db_path: Path | str, *, spool_path: Path | None = None, check_files: bool = False,
                  anchors: Iterable[Anchor] = (), use_local_anchors: bool = True, chunk: int = 2000,
                  pause_s: float = 0.0, status_path: Path | None = None) -> VerifyReport:
    """Verify the chain of the ledger at ``db_path`` (opened read-only; safe while recording).

    Works in many SHORT read transactions of at most ``chunk`` entries/keys/rows each (with
    ``pause_s`` between them), so it never holds one snapshot for long (the recorder's WAL
    checkpoint is not starved) and memory stays bounded. Correctness across snapshots: the chain
    is append-only, so it is verified up to the head seen at the start; each row is compared with
    its entries inside one snapshot. Local anchors (``chain_heads.log``, ``status.json``) are read
    BEFORE the starting head, so an anchor can never be newer than the part verified.

    ``status_path``: where the recorder writes ``status.json`` (``cfg.status_path``; with the
    Windows service layout it is in ``[storage] status_dir``, not beside the ledger). Default:
    beside the ledger, as before (integration, docs/INTEGRATION.md)."""
    t0 = time.monotonic()
    db_path = Path(db_path)
    rep = VerifyReport(checked_utc=iso_utc(time.time()))
    all_anchors = list(anchors)
    if use_local_anchors:
        logged, problems = read_anchor_log(db_path.parent / ANCHOR_LOG_NAME)
        all_anchors += logged
        rep.notes += problems
        st = status_anchor(status_path or (db_path.parent / "status.json"))
        if st is not None:
            all_anchors.append(st)
    if not db_path.exists():
        rep.findings.append(Finding(0, "ledger_missing", "The recording database was not found."))
        return rep
    conn = _ro_connect(db_path)
    try:
        _Verifier(conn, rep, all_anchors, max(1, chunk), pause_s).run(spool_path, check_files)
    finally:
        conn.close()
    rep.seconds = time.monotonic() - t0
    return rep


class _Verifier:
    def __init__(self, conn: sqlite3.Connection, rep: VerifyReport, anchors: list[Anchor], chunk: int,
                 pause_s: float) -> None:
        self.conn, self.rep, self.anchors, self.chunk, self.pause_s = conn, rep, anchors, chunk, pause_s
        self.disc: dict[tuple[str, str], list[int]] = {}      # discrepancy entry positions per row
        self.pending: dict[tuple[str, str], tuple[str, str]] = {}
        self.end = -1
        self.broken = False

    # ---------------------------------------------------------------- helpers
    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        t = time.monotonic()
        try:
            yield self.conn
        finally:
            self.conn.execute("ROLLBACK")
            self.rep.transactions += 1
            self.rep.longest_read_s = max(self.rep.longest_read_s, time.monotonic() - t)
            if self.pause_s:
                time.sleep(self.pause_s)

    def find(self, pos: int | None, code: str, msg: str) -> None:
        self.rep.findings.append(Finding(pos, code, msg))

    def fault(self, pos: int | None, code: str, msg: str) -> None:
        self.rep.faults.append(Finding(pos, code, msg))

    # ---------------------------------------------------------------- passes
    def run(self, spool_path: Path | None, check_files: bool) -> None:
        with self.snapshot() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_chain'").fetchone() is None:
                self.find(0, "chain_missing", "The recording log has no tamper-evidence chain (it was made by "
                                              "older software, or the chain was removed).")
                return
            row = c.execute("SELECT MAX(seq) FROM ledger_chain").fetchone()
            self.end = int(row[0]) if row[0] is not None else -1
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                         "name='ledger_chain_pending'").fetchone() is not None:
                self.pending = {(str(r[0]), str(r[1])): (str(r[2]), str(r[3])) for r in c.execute(
                    "SELECT ref_table, ref_key, failed_utc, error FROM ledger_chain_pending")}
        expected = self.chain_pass()
        self.rep.entries = expected
        if expected == 0 and self.end < 0:
            self.find(0, "chain_empty", "The recording log's tamper-evidence chain is empty: its entries were removed.")
        for a in self.anchors:
            if a.seq >= expected:
                self.rep.anchors_checked += 1
                when = f" at {a.anchored_utc}" if a.anchored_utc else ""
                self.find(expected, "log_truncated",
                          f"The log ends at entry {expected - 1} but a copy of its fingerprint saved {a.source}{when} "
                          f"shows it had reached entry {a.seq}: entries were removed from the end.")
        if self.broken:
            self.rep.notes.append("Checks after the first broken entry are reported but cannot be fully trusted.")
        files: list[tuple[int, str, str, int | None]] = []
        for table in COVERED:
            self.key_pass(table, files if check_files else None, spool_path)
            self.unlogged_pass(table, expected)
        if check_files:
            if spool_path is None:
                self.rep.notes.append("Recording files were not checked (recording storage location unknown).")
            else:
                self.check_files(files, spool_path)

    def chain_pass(self) -> int:
        """Structure, links, anchors, kind rules. Returns the number of confirmed-contiguous entries."""
        by_seq: dict[int, list[Anchor]] = {}
        for a in self.anchors:
            by_seq.setdefault(a.seq, []).append(a)
        last_confirmed: int | None = None
        prev, expected, prev_kind = ZERO_HASH, 0, None
        while expected <= self.end and not self.broken:
            with self.snapshot() as c:
                rows = c.execute("SELECT seq, kind, ref_table, ref_key, record, prev_hash, entry_hash FROM ledger_chain "
                                 "WHERE seq >= ? AND seq <= ? ORDER BY seq LIMIT ?",
                                 (expected, self.end, self.chunk)).fetchall()
            if not rows:
                self.find(expected, "entry_missing", f"Log entry {expected} is missing (the log should reach entry "
                                                     f"{self.end}).")
                self.broken = True
                break
            for row in rows:
                seq = int(row[0])
                if seq != expected:
                    self.find(expected, "entry_missing",
                              f"Log entry {expected} is missing (the next entry present is {seq}).")
                    self.broken = True
                    break
                try:
                    rec = json.loads(row[4])
                    data = rec.get("data")
                    well_formed = (rec.get("v") == CHAIN_FORMAT and make_record(
                        rec["seq"], rec["utc"], rec["kind"], rec["table"], rec["key"], canonical_json(data)) == row[4])
                except (ValueError, KeyError, TypeError, AttributeError):
                    rec, data, well_formed = None, None, False
                if rec is None or not well_formed:
                    self.find(seq, "entry_unreadable", f"Log entry {seq} is not in the recorded format: it was altered.")
                    self.broken = True
                    break
                if rec["seq"] != seq:
                    self.find(seq, "entry_out_of_place", f"Log entry {seq} holds the record written as entry "
                                                         f"{rec['seq']}: entries were reordered or replaced.")
                    self.broken = True
                    break
                if row[5] != prev:
                    pos = max(seq - 1, 0)
                    self.find(pos, "link_broken", f"Log entry {pos} is not the entry that entry {seq} was linked to "
                                                  f"when it was written: entry {pos} was altered or replaced.")
                    self.broken = True
                    break
                h = entry_hash(prev, row[4])
                if h != row[6]:
                    self.find(seq, "entry_altered", f"Log entry {seq} was altered after it was written (its "
                                                    "fingerprint no longer matches its contents).")
                    self.broken = True
                    break
                kind, table, key = rec["kind"], rec["table"], rec["key"]
                if (row[1], row[2], row[3]) != (kind, table, key):
                    self.find(seq, "entry_index_altered", f"The look-up columns of log entry {seq} were altered.")
                self.kind_rules(seq, kind, table, key, data, prev_kind, rec.get("utc"))
                for a in by_seq.get(seq, []):
                    self.rep.anchors_checked += 1
                    if a.hash != h:
                        since = f"after entry {last_confirmed}" if last_confirmed is not None else "anywhere"
                        when = f" at {a.anchored_utc}" if a.anchored_utc else ""
                        self.find(seq, "anchor_mismatch", f"Log entry {seq} differs from the copy of the log fingerprint "
                                                          f"saved {a.source}{when}: the log was rewritten ({since} up "
                                                          f"to entry {seq}).")
                    else:
                        last_confirmed = seq if last_confirmed is None else max(last_confirmed, seq)
                prev, prev_kind, expected = h, kind, seq + 1
                self.rep.head = ChainHead(seq, h, str(rec.get("utc", "")))
        return expected

    def kind_rules(self, seq: int, kind: str, table: Any, key: Any, data: Any, prev_kind: str | None,
                   utc: Any) -> None:
        if kind not in VALID_KINDS or (kind in STRUCTURAL_KINDS) != (table is None):
            self.find(seq, "illegal_entry", f"Log entry {seq} is of a kind the recording software never writes.")
        elif (kind == "genesis") != (seq == 0) or (kind == "adopted" and prev_kind not in ("genesis", "adopted")):
            self.find(seq, "illegal_entry", f"Log entry {seq} ({kind}) is out of place: such entries are written only "
                                            "when the log is first started.")
        elif kind == "genesis" and isinstance(data, dict):
            if data.get("previous_chain_missing"):
                self.find(seq, "chain_restarted", f"The log's tamper-evidence chain was missing when the recorder "
                                                  f"started at {utc} and had to be started again; what happened "
                                                  "before that cannot be checked.")
            pre = data.get("pre_existing_rows") or {}
            if any(pre.values()):
                self.rep.notes.append(f"{sum(pre.values())} records created before {utc} (before tamper evidence "
                                      "was introduced) were adopted into the log at that time; changes made to them "
                                      "before then cannot be detected.")
        elif kind == "discrepancy" and isinstance(data, dict):
            t = str(data.get("table"))
            self.find(seq, "discrepancy_recorded",
                      f"At {utc} the recording software found that the {_NOUN.get(t, t)} "
                      f"{_label(t, str(data.get('key')), data.get('found') or data.get('expected'))} had been changed "
                      f"outside it ({_diff(data.get('expected'), data.get('found'))}).")
            if sum(map(len, self.disc.values())) < MAX_MARKERS:
                self.disc.setdefault((t, str(data.get("key"))), []).append(seq)
        elif kind == "protection_gap" and isinstance(data, dict):
            t = str(data.get("table"))
            self.fault(seq, "protection_gap",
                       f"The log entry for the {_NOUN.get(t, t)} #{data.get('key')} could not be written at "
                       f"{data.get('failed_utc')} ({_short(data.get('error'))}); it was recorded again at entry "
                       f"{seq + 1}. Only changes the recording software would make were found across the gap.")
        elif kind == "row_uncovered" or (table in COVERED and data is None):
            self.find(seq, "illegal_entry", f"Log entry {seq} removes a record from protection, which the recording "
                                            "software never does.")

    def key_pass(self, table: str, files: list[tuple[int, str, str, int | None]] | None,
                 spool_path: Path | None) -> None:
        noun, last = _NOUN[table], ""
        while True:
            with self.snapshot() as c:
                keys = [r[0] for r in c.execute("SELECT DISTINCT ref_key FROM ledger_chain WHERE ref_table=? AND "
                                                "ref_key > ? ORDER BY ref_key LIMIT ?", (table, last, self.chunk))]
                for key in keys:
                    self.check_key(c, table, noun, str(key), files)
            if not keys:
                return
            last = keys[-1]

    def check_key(self, c: sqlite3.Connection, table: str, noun: str, key: str,
                  files: list[tuple[int, str, str, int | None]] | None) -> None:
        pk, _ = COVERED[table]
        prev_seq, prev = None, None
        last_seq, last = None, None
        for seq, kind, record in c.execute("SELECT seq, kind, record FROM ledger_chain WHERE ref_table=? AND ref_key=? "
                                           "ORDER BY seq", (table, key)):
            try:
                data = _data_of(record)
            except (ValueError, KeyError, TypeError):
                continue                                   # reported by the chain pass
            if not isinstance(data, dict):
                continue
            if prev is not None and not any(prev_seq < d < seq for d in self.disc.get((table, key), ())):
                why = illegal_changes(table, prev, data)
                if why:
                    self.find(seq, "illegal_change",
                              f"Log entry {seq} records a change the recording software never makes to the {noun} "
                              f"{_label(table, key, data)}: {'; '.join(why)}.")
            prev_seq, prev = seq, data
            last_seq, last = seq, data
        if last is None or last_seq is None:
            return
        try:
            row = c.execute(f"SELECT * FROM {table} WHERE {pk}=?", (key,)).fetchone()
            p = projection(table, row)
        except (sqlite3.OperationalError, IndexError, KeyError) as exc:
            self.find(None, "table_altered", f"The {noun} table cannot be read as recorded ({exc}).")
            return
        if row is None:
            self.find(last_seq, "record_deleted", f"The {noun} {_label(table, key, last)} recorded in log entry "
                                                  f"{last_seq} has been deleted from the database.")
            return
        if p is None:
            self.find(last_seq, "record_unsealed", f"The {noun} {_label(table, key, last)} was sealed in log entry "
                                                   f"{last_seq} but is no longer marked sealed (state is now "
                                                   f"{row['state']}).")
            return
        if not _same(p, last):
            pend = self.pending.get((table, key))
            if pend is not None and not illegal_changes(table, last, p):
                self.fault(last_seq, "not_yet_protected",
                           f"The latest change to the {noun} {_label(table, key, p)} could not be entered in the log "
                           f"at {pend[0]} ({_short(pend[1])}); it will be re-recorded at the next write.")
            else:
                self.find(last_seq, "record_altered", f"The {noun} {_label(table, key, last)} was changed after log "
                                                      f"entry {last_seq} recorded it: {_diff(last, p)}.")
        if files is not None and table == "segments" and last.get("sha256"):
            files.append((last_seq, str(last.get("rel_path")), str(last["sha256"]), last.get("size_bytes")))

    def unlogged_pass(self, table: str, end_pos: int) -> None:
        pk, _ = COVERED[table]
        noun, last_rowid = _NOUN[table], 0
        while True:
            with self.snapshot() as c:
                try:
                    rows = c.execute(f"SELECT rowid AS _rowid_, * FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?",
                                     (last_rowid, self.chunk)).fetchall()
                except sqlite3.OperationalError as exc:
                    if table in _LATER_TABLES and "no such table" in str(exc):
                        # A ledger last opened by software before these features were integrated.
                        self.rep.notes.append(f"This recording log has no {_NOUN[table]} table.")
                        return
                    self.find(None, "table_missing", f"The {noun} table cannot be read ({exc}).")
                    return
                if not rows:
                    return
                keys = [str(r[pk]) for r in rows]
                have: set[str] = set()
                for i in range(0, len(keys), 500):
                    part = keys[i:i + 500]
                    marks = ",".join("?" for _ in part)
                    have.update(str(r[0]) for r in c.execute(
                        f"SELECT DISTINCT ref_key FROM ledger_chain WHERE ref_table=? AND ref_key IN ({marks})",
                        (table, *part)))
                for r, key in zip(rows, keys):
                    self.rep.rows_checked += 1
                    try:
                        p = projection(table, r)
                    except (IndexError, KeyError):
                        self.find(None, "table_altered", f"The {noun} table's structure was changed.")
                        return
                    if p is None or key in have:
                        continue
                    pend = self.pending.get((table, key))
                    if pend is not None:
                        self.fault(end_pos, "not_yet_protected",
                                   f"The {noun} {_label(table, key, p)} could not be entered in the log at {pend[0]} "
                                   f"({_short(pend[1])}); it will be recorded at the next write.")
                    else:
                        self.find(end_pos, "record_unlogged",
                                  f"The {noun} {_label(table, key, p)} is in the database but was never entered in "
                                  "the log: it was added or changed outside the recording software, or the end of "
                                  "the log was removed.")
                last_rowid = int(rows[-1]["_rowid_"])

    def check_files(self, files: list[tuple[int, str, str, int | None]], spool_path: Path) -> None:
        from .hashing import sha256_file
        for seq, rel, sha, size in sorted(files):          # outside any read snapshot: hashing is slow
            p = spool_path / rel
            self.rep.files_checked += 1
            if not p.exists():
                self.find(seq, "file_missing", f"The recording file {rel} recorded in log entry {seq} is missing.")
                continue
            got, n = sha256_file(p)
            if got != sha or (size is not None and n != size):
                self.find(seq, "file_altered", f"The recording file {rel} no longer matches the fingerprint recorded "
                                               f"in log entry {seq}: the file was changed.")


# ---------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    from .config import ConfigError, load_config

    ap = argparse.ArgumentParser(prog="python -m sentinel.ledgerchain",
                                 description="Check the recording log for alteration. Exit status: 0 intact, "
                                             "1 altered, 2 cannot check, 3 intact with protection gaps.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="recompute and check the recording log's hash chain")
    v.add_argument("--config", required=True)
    v.add_argument("--check-files", action="store_true",
                   help="also re-read every sealed recording file and compare its SHA-256 (slow)")
    v.add_argument("--expect-head", action="append", default=[], metavar="SEQ:HASH",
                   help="a chain head saved elsewhere (export manifest, diagnostic report); repeatable")
    v.add_argument("--no-local-anchors", action="store_true",
                   help="ignore chain_heads.log and status.json beside the ledger")
    v.add_argument("--json", action="store_true", help="print the full report as JSON")
    h = sub.add_parser("head", help="print the current chain head (write it down or send it to support)")
    h.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    db = cfg.data_path / "sentinel.db"
    if not db.exists():
        print("The recording database was not found; nothing to check.", file=sys.stderr)
        return 2
    if args.cmd == "head":
        head = current_chain_head(db)
        if head is None:
            print("This recording log has no tamper-evidence chain.")
            return 1
        print(f"entry {head.seq}  fingerprint {head.hash}  written {head.utc}")
        print(f"(use with: --expect-head {head.seq}:{head.hash})")
        return 0
    try:
        expected = [parse_expected_head(x) for x in args.expect_head]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    rep = verify_ledger(db, spool_path=cfg.spool_path, check_files=args.check_files, anchors=expected,
                        use_local_anchors=not args.no_local_anchors, status_path=cfg.status_path)
    print(json.dumps(rep.to_dict(), indent=2) if args.json else rep.render_text())
    return 1 if not rep.ok else (3 if rep.faults else 0)   # 0 intact, 1 altered, 2 cannot check, 3 gaps


if __name__ == "__main__":
    sys.exit(main())
