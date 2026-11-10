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
   ``table``/``key``. Kinds with ``table = null`` (``genesis``, ``discrepancy``) describe the log
   itself, not a row.
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
from collections.abc import Callable, Iterable
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
STRUCTURAL_KINDS = frozenset({"genesis", "discrepancy"})

# ---------------------------------------------------------------------- covered columns
# Columns NOT listed are bookkeeping that legitimately changes without evidential meaning
# (retry counters, last audit time, updated_utc, gap_recorded). Never remove a column from
# these lists in a released format: that would make old entries unverifiable. Adding coverage
# for a new column requires a new CHAIN_FORMAT.
RUN_FIELDS = ("run_id", "camera_id", "directory", "launched_utc", "launch_mono", "anchor_utc", "anchor_method",
              "timeline_break_stream_s", "timeline_shift_s", "display_timezone", "ended_utc", "end_reason",
              "exit_code", "end_detail", "clock_anomaly", "app_version", "ffmpeg_version", "host",
              "command_redacted")
RUN_END_FIELDS = ("last_stream_time_s",)      # persisted every ~10 s while running; covered once ended
SEGMENT_FIELDS = ("segment_id", "camera_id", "run_id", "seq", "rel_path", "state", "stream_start_s",
                  "stream_end_s", "start_utc", "end_utc", "time_source", "observed_first_utc",
                  "observed_closed_utc", "duration_s", "size_bytes", "sha256", "content_sha256",
                  "content_hash_method", "decoded_frame_count", "framehash_rel_path", "framehash_sha256",
                  "pix_fmt", "video_codec", "width", "height", "packet_count", "keyframe_count",
                  "max_pts_gap_s", "interrupted", "clock_anomaly", "warnings", "validation_detail",
                  "sealed_utc", "audit_status", "created_utc")
GAP_FIELDS = ("gap_id", "camera_id", "kind", "start_utc", "end_utc", "duration_s", "reason", "prev_segment_id",
              "next_segment_id", "ended_run_id", "start_basis", "attempts", "clock_anomaly", "detail",
              "opened_utc", "closed_utc")
EVENT_FIELDS = ("event_id", "ts_utc", "severity", "category", "code", "message", "detail")

# table -> (primary key column, covered columns). Segments are covered once sealed (terminal state).
COVERED: dict[str, tuple[str, tuple[str, ...]]] = {
    "recording_runs": ("run_id", RUN_FIELDS),
    "segments": ("segment_id", SEGMENT_FIELDS),
    "recording_gaps": ("gap_id", GAP_FIELDS),
    "events": ("event_id", EVENT_FIELDS),
}
_NOUN = {"recording_runs": "recording run", "segments": "recording segment", "recording_gaps": "recording gap",
         "events": "log event"}


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
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    anchors_checked: int = 0
    rows_checked: int = 0
    files_checked: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.findings

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (f.position is None, f.position or 0))

    @property
    def first(self) -> Finding | None:
        s = self.sorted_findings()
        return s[0] if s else None

    def to_dict(self, max_findings: int = 200) -> dict[str, Any]:
        return {"ok": self.ok, "checked_utc": self.checked_utc, "entries": self.entries,
                "head": self.head.to_dict() if self.head else None,
                "first_problem": self.first.to_dict() if self.first else None,
                "findings": [f.to_dict() for f in self.sorted_findings()[:max_findings]],
                "findings_total": len(self.findings), "notes": self.notes,
                "anchors_checked": self.anchors_checked, "rows_checked": self.rows_checked,
                "files_checked": self.files_checked, "seconds": round(self.seconds, 3)}

    def render_text(self, max_findings: int = 50) -> str:
        lines = ["RECORDING LOG CHECK", "==================="]
        if self.ok:
            lines.append("RESULT: INTACT - no sign that the recording log was altered.")
        else:
            f = self.first
            assert f is not None
            where = f"at log entry {f.position}" if f.position is not None else "(position unknown)"
            lines += ["RESULT: ALTERED - the recording log has been altered.",
                      f"First problem {where}: {f.message}"]
        lines.append(f"Checked: {self.entries} log entries, {self.rows_checked} records, "
                     f"{self.anchors_checked} saved copies of the log fingerprint, {self.files_checked} recording files.")
        if self.head:
            lines.append(f"Log head: entry {self.head.seq}, fingerprint {self.head.hash}, written {self.head.utc}")
        if len(self.findings) > 1:
            lines.append("")
            lines.append(f"All problems ({len(self.findings)}):")
            for x in self.sorted_findings()[:max_findings]:
                pos = f"entry {x.position}" if x.position is not None else "-"
                lines.append(f"  [{pos}] {x.message}")
            if len(self.findings) > max_findings:
                lines.append(f"  ... and {len(self.findings) - max_findings} more")
        if self.notes:
            lines.append("")
            lines.append("Notes:")
            lines += [f"  - {n}" for n in self.notes]
        lines.append("")
        lines.append("This check shows whether the log is consistent with itself and with saved copies of its "
                     "fingerprint. Someone able to rewrite the whole log can make it consistent again; only a "
                     "fingerprint saved off this computer earlier (review drive, diagnostic report) exposes that.")
        return "\n".join(lines)


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


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


def verify_ledger(db_path: Path | str, *, spool_path: Path | None = None, check_files: bool = False,
                  anchors: Iterable[Anchor] = (), use_local_anchors: bool = True) -> VerifyReport:
    """Verify the chain of the ledger at ``db_path`` (opened read-only; safe while recording).

    Local anchors (``chain_heads.log`` and ``status.json`` beside the ledger) are read BEFORE the
    database snapshot is taken, so an anchor can never be newer than the snapshot."""
    t0 = time.monotonic()
    db_path = Path(db_path)
    rep = VerifyReport(checked_utc=iso_utc(time.time()))
    all_anchors = list(anchors)
    if use_local_anchors:
        logged, problems = read_anchor_log(db_path.parent / ANCHOR_LOG_NAME)
        all_anchors += logged
        rep.notes += problems
        st = status_anchor(db_path.parent / "status.json")
        if st is not None:
            all_anchors.append(st)
    if not db_path.exists():
        rep.findings.append(Finding(0, "ledger_missing", "The recording database was not found."))
        return rep
    conn = _ro_connect(db_path)
    try:
        conn.execute("BEGIN")                 # one consistent snapshot; does not block the recorder
        try:
            _verify_snapshot(conn, rep, all_anchors, spool_path, check_files)
        finally:
            conn.execute("ROLLBACK")
    finally:
        conn.close()
    rep.seconds = time.monotonic() - t0
    return rep


def _verify_snapshot(conn: sqlite3.Connection, rep: VerifyReport, anchors: list[Anchor],
                     spool_path: Path | None, check_files: bool) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_chain'").fetchone() is None:
        rep.findings.append(Finding(0, "chain_missing",
                                    "The recording log has no tamper-evidence chain (it was made by older "
                                    "software, or the chain was removed)."))
        return
    by_seq: dict[int, list[Anchor]] = {}
    for a in anchors:
        by_seq.setdefault(a.seq, []).append(a)
    last_confirmed: int | None = None          # highest anchored position that matched

    latest: dict[tuple[str, str], tuple[int, bytes]] = {}
    seg_files: dict[str, tuple[int, str, str | None, int | None]] = {}
    expected_seq, prev = 0, ZERO_HASH
    broken = False
    count = 0
    for row in conn.execute("SELECT seq, kind, ref_table, ref_key, record, prev_hash, entry_hash "
                            "FROM ledger_chain ORDER BY seq"):
        seq = int(row[0])
        count += 1
        try:
            rec = json.loads(row[4])
            data = rec.get("data")
            data_c = canonical_json(data)
            well_formed = (rec.get("v") == CHAIN_FORMAT and make_record(
                rec["seq"], rec["utc"], rec["kind"], rec["table"], rec["key"], data_c) == row[4])
        except (ValueError, KeyError, TypeError, AttributeError):
            rec, data, data_c, well_formed = None, None, None, False
        if not broken:
            if seq != expected_seq:
                rep.findings.append(Finding(expected_seq, "entry_missing",
                                            f"Log entry {expected_seq} is missing (the next entry present is {seq})."))
                broken = True
            elif rec is None or not well_formed:
                rep.findings.append(Finding(seq, "entry_unreadable",
                                            f"Log entry {seq} is not in the recorded format: it was altered."))
                broken = True
            elif rec["seq"] != seq:
                rep.findings.append(Finding(seq, "entry_out_of_place",
                                            f"Log entry {seq} holds the record written as entry {rec['seq']}: "
                                            "entries were reordered or replaced."))
                broken = True
            elif seq == 0 and rec["kind"] != "genesis":
                rep.findings.append(Finding(0, "entry_unreadable", "The first log entry is not a starting entry."))
                broken = True
            elif row[5] != prev:
                pos = max(seq - 1, 0)
                rep.findings.append(Finding(pos, "link_broken",
                                            f"Log entry {pos} is not the entry that entry {seq} was linked to when "
                                            f"it was written: entry {pos} was altered or replaced."))
                broken = True
            else:
                h = entry_hash(prev, row[4])
                if h != row[6]:
                    rep.findings.append(Finding(seq, "entry_altered",
                                                f"Log entry {seq} was altered after it was written (its "
                                                "fingerprint no longer matches its contents)."))
                    broken = True
                else:
                    if (row[1], row[2], row[3]) != (rec["kind"], rec["table"], rec["key"]):
                        rep.findings.append(Finding(seq, "entry_index_altered",
                                                    f"The look-up columns of log entry {seq} were altered."))
                    for a in by_seq.get(seq, []):
                        rep.anchors_checked += 1
                        if a.hash != h:
                            since = f"after entry {last_confirmed}" if last_confirmed is not None else "anywhere"
                            when = f" at {a.anchored_utc}" if a.anchored_utc else ""
                            rep.findings.append(Finding(seq, "anchor_mismatch",
                                                        f"Log entry {seq} differs from the copy of the log "
                                                        f"fingerprint saved {a.source}{when}: the log was rewritten "
                                                        f"({since} up to entry {seq})."))
                        else:
                            last_confirmed = seq if last_confirmed is None else max(last_confirmed, seq)
                    prev = h
                    rep.head = ChainHead(seq, h, str(rec.get("utc", "")))
        expected_seq = seq + 1
        if rec is None:
            continue
        kind, table, key = rec.get("kind"), rec.get("table"), rec.get("key")
        if kind == "genesis" and isinstance(data, dict):
            if data.get("previous_chain_missing"):
                rep.findings.append(Finding(seq, "chain_restarted",
                                            f"The log's tamper-evidence chain was missing when the recorder started "
                                            f"at {rec.get('utc')} and had to be started again; what happened "
                                            "before that cannot be checked."))
            pre = data.get("pre_existing_rows") or {}
            if seq == 0 and any(pre.values()):
                rep.notes.append(f"{sum(pre.values())} records created before {rec.get('utc')} (before "
                                 "tamper evidence was introduced) were adopted into the log at that time; changes "
                                 "made to them before then cannot be detected.")
        elif kind == "discrepancy" and isinstance(data, dict):
            t = str(data.get("table"))
            rep.findings.append(Finding(seq, "discrepancy_recorded",
                                        f"At {rec.get('utc')} the recording software found that the "
                                        f"{_NOUN.get(t, t)} {_label(t, str(data.get('key')), data.get('found') or data.get('expected'))} "
                                        f"had been changed outside it ({_diff(data.get('expected'), data.get('found'))})."))
        elif table in COVERED and key is not None and data is None:
            latest.pop((table, str(key)), None)        # entry says: row no longer covered
            seg_files.pop(str(key), None)
        elif table in COVERED and key is not None and data_c is not None:
            latest[(table, str(key))] = (seq, hashlib.sha256(data_c.encode("utf-8")).digest())
            if check_files and table == "segments" and isinstance(data, dict):
                seg_files[str(key)] = (seq, str(data.get("rel_path")), data.get("sha256"), data.get("size_bytes"))
    rep.entries = count
    if count == 0:
        rep.findings.append(Finding(0, "chain_empty", "The recording log's tamper-evidence chain is empty: its "
                                                      "entries were removed."))
    for a in anchors:
        if a.seq >= expected_seq:
            rep.anchors_checked += 1
            when = f" at {a.anchored_utc}" if a.anchored_utc else ""
            rep.findings.append(Finding(expected_seq, "log_truncated",
                                        f"The log ends at entry {expected_seq - 1} but a copy of its fingerprint "
                                        f"saved {a.source}{when} shows it had reached entry {a.seq}: entries were "
                                        "removed from the end."))
    if broken:
        rep.notes.append("Checks after the first broken entry are reported but cannot be fully trusted.")
    _check_rows(conn, rep, latest, expected_seq)
    if check_files:
        _check_files(rep, seg_files, spool_path)


def _record_data(conn: sqlite3.Connection, seq: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT record FROM ledger_chain WHERE seq=?", (seq,)).fetchone()
    try:
        d = _data_of(row[0]) if row else None
    except (ValueError, KeyError):
        return None
    return d if isinstance(d, dict) else None


def _check_rows(conn: sqlite3.Connection, rep: VerifyReport, latest: dict[tuple[str, str], tuple[int, bytes]],
                end_seq: int) -> None:
    for table, (pk, _) in COVERED.items():
        noun = _NOUN[table]
        try:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        except sqlite3.OperationalError as exc:
            rep.findings.append(Finding(None, "table_missing", f"The {noun} table cannot be read ({exc})."))
            continue
        for row in rows:
            rep.rows_checked += 1
            key = str(row[pk])
            try:
                p = projection(table, row)
            except (IndexError, KeyError):
                rep.findings.append(Finding(None, "table_altered", f"The {noun} table's structure was changed."))
                break
            ent = latest.pop((table, key), None)
            if p is None:
                if ent is not None:
                    rep.findings.append(Finding(ent[0], "record_unsealed",
                                                f"The {noun} {_label(table, key, _record_data(conn, ent[0]))} was "
                                                f"sealed in log entry {ent[0]} but is no longer marked sealed "
                                                f"(state is now {row['state']})."))
                continue
            if ent is None:
                rep.findings.append(Finding(end_seq, "record_unlogged",
                                            f"The {noun} {_label(table, key, p)} is in the database but was never "
                                            "entered in the log: it was added or changed outside the recording "
                                            "software, or the end of the log was removed."))
                continue
            if hashlib.sha256(canonical_json(p).encode("utf-8")).digest() != ent[1]:
                exp = _record_data(conn, ent[0])
                rep.findings.append(Finding(ent[0], "record_altered",
                                            f"The {noun} {_label(table, key, exp)} was changed after log entry "
                                            f"{ent[0]} recorded it: {_diff(exp, p)}."))
    for (table, key), (seq, _) in latest.items():
        exp = _record_data(conn, seq)
        rep.findings.append(Finding(seq, "record_deleted",
                                    f"The {_NOUN.get(table, table)} {_label(table, key, exp)} recorded in log entry "
                                    f"{seq} has been deleted from the database."))


def _check_files(rep: VerifyReport, seg_files: dict[str, tuple[int, str, str | None, int | None]],
                 spool_path: Path | None) -> None:
    from .hashing import sha256_file
    if spool_path is None:
        rep.notes.append("Recording files were not checked (recording storage location unknown).")
        return
    for _key, (seq, rel, sha, size) in sorted(seg_files.items(), key=lambda kv: kv[1][0]):
        if not sha:
            continue                          # sealed without a file (file was already missing at sealing)
        p = spool_path / rel
        rep.files_checked += 1
        if not p.exists():
            rep.findings.append(Finding(seq, "file_missing",
                                        f"The recording file {rel} recorded in log entry {seq} is missing."))
            continue
        got, n = sha256_file(p)
        if got != sha or (size is not None and n != size):
            rep.findings.append(Finding(seq, "file_altered",
                                        f"The recording file {rel} no longer matches the fingerprint recorded in "
                                        f"log entry {seq}: the file was changed."))


# ---------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    from .config import ConfigError, load_config

    ap = argparse.ArgumentParser(prog="python -m sentinel.ledgerchain",
                                 description="Check the recording log for alteration.")
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
                        use_local_anchors=not args.no_local_anchors)
    print(json.dumps(rep.to_dict(), indent=2) if args.json else rep.render_text())
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
