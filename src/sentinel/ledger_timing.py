"""Ledger schema additions for time accuracy (requirement E1), kept apart from ``db.py``.

Append-only and idempotent: a separate DDL block for the ``clock_observations`` table and
``ALTER TABLE ... ADD COLUMN`` for new run/segment columns, applied by ``Ledger._migrate`` on
every start. Old ledgers gain the columns (NULL for existing rows, which means "legacy single
anchor, no correction"); nothing is dropped or rewritten. The version of this block is kept
under its own ``schema_meta`` key so it does not collide with the core ``schema_version``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

TIMING_SCHEMA_KEY = "timing_schema_version"
TIMING_SCHEMA_VERSION = 1

TIMING_DDL = """
CREATE TABLE IF NOT EXISTS clock_observations (
    obs_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_utc        TEXT NOT NULL,        -- host wall clock (UTC) when sampled
    mono_s              REAL NOT NULL,        -- host monotonic clock at the same instant
    reason              TEXT NOT NULL,        -- startup | periodic | clock_step
    platform            TEXT,
    sync_tool           TEXT,                 -- w32tm | timedatectl | chronyc | NULL (none usable)
    synchronized        INTEGER,              -- 1 / 0 as reported by the OS; NULL = not reported
    sync_source         TEXT,                 -- time source the OS names
    last_sync_utc       TEXT,                 -- last successful synchronisation, if reported
    os_offset_s         REAL,                 -- local clock minus reference (s), only where the tool's
                                              -- sign convention is documented (chrony); else NULL
    os_detail           TEXT,                 -- JSON: parsed fields, raw excerpt, errors
    wall_minus_mono_s   REAL,                 -- raw host wall - monotonic offset
    host_clock_drift_s  REAL,                 -- gradual wall-vs-monotonic change since service start (steps excluded)
    host_clock_drift_ppm REAL,
    clock_steps_total_s REAL,                 -- sum of wall-clock steps detected since service start
    run_id              TEXT,                 -- recording run active when sampled
    stream_time_s       REAL,
    camera_drift_ppm    REAL,                 -- camera stream clock vs host monotonic (positive = camera fast)
    time_correction_s   REAL,                 -- correction currently applied to that run's segment times
    ref_server          TEXT,                 -- configured reference time server queried by SNTP (E1)
    ref_offset_s        REAL,                 -- measured: this PC's clock minus the reference (s)
    ref_delay_s         REAL,                 -- round-trip delay of that measurement (uncertainty <= delay/2)
    ref_detail          TEXT                  -- JSON: stratum, leap, errors
);
CREATE INDEX IF NOT EXISTS ix_clock_obs_utc ON clock_observations(observed_utc);
"""

RUN_COLUMNS: dict[str, str] = {
    "time_method": "TEXT",                         # NULL = legacy single anchor; else e.g. reception_tracking_v1
    "time_correction_s": "REAL",                   # correction at the latest segment boundary
    "camera_drift_ppm": "REAL",                    # measured stream clock rate vs host monotonic
    "host_clock_drift_ppm": "REAL",                # measured host wall rate vs monotonic (steps excluded)
    "drift_measured_over_s": "REAL",               # stream seconds the ppm figures are measured over
    "time_correction_limited_count": "INTEGER NOT NULL DEFAULT 0",
}

SEGMENT_COLUMNS: dict[str, str] = {
    "time_correction_start_s": "REAL",             # correction applied at the segment's start boundary
    "time_correction_end_s": "REAL",               # ... at its end boundary (linear in between)
    "time_correction_stream_s": "REAL",            # stream time of the end boundary's measurement
    "time_correction_limited": "INTEGER NOT NULL DEFAULT 0",
    "time_correction_detail": "TEXT",              # JSON of the end-boundary measurement
}

OBSERVATION_COLUMNS: dict[str, str] = {   # added after the table was first defined
    "ref_server": "TEXT", "ref_offset_s": "REAL", "ref_delay_s": "REAL", "ref_detail": "TEXT",
}

# Write-once once a segment is sealed (same rule as its start/end times).
TIMING_WRITE_ONCE = frozenset({"time_correction_start_s", "time_correction_end_s", "time_correction_stream_s"})


def existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> list[str]:
    """Add any missing columns (never alters or drops existing ones). Returns those added."""
    have = existing_columns(conn, table)
    added = []
    for name, decl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
    return added


def apply_timing_schema(conn: sqlite3.Connection) -> None:
    """Idempotent; called with the ledger lock held on an autocommit connection."""
    conn.executescript(TIMING_DDL)
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_columns(conn, "recording_runs", RUN_COLUMNS)
        ensure_columns(conn, "segments", SEGMENT_COLUMNS)
        ensure_columns(conn, "clock_observations", OBSERVATION_COLUMNS)
        row = conn.execute("SELECT value FROM schema_meta WHERE key=?", (TIMING_SCHEMA_KEY,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                         (TIMING_SCHEMA_KEY, str(TIMING_SCHEMA_VERSION)))
        elif int(row[0]) > TIMING_SCHEMA_VERSION:
            raise RuntimeError(f"ledger timing schema {row[0]} is newer than this software "
                               f"({TIMING_SCHEMA_VERSION})")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


OBSERVATION_FIELDS = (
    "observed_utc", "mono_s", "reason", "platform", "sync_tool", "synchronized", "sync_source",
    "last_sync_utc", "os_offset_s", "os_detail", "wall_minus_mono_s", "host_clock_drift_s",
    "host_clock_drift_ppm", "clock_steps_total_s", "run_id", "stream_time_s", "camera_drift_ppm",
    "time_correction_s", "ref_server", "ref_offset_s", "ref_delay_s", "ref_detail",
)


def insert_clock_observation(ledger: Any, cols: dict[str, Any]) -> int:
    """Insert one ``clock_observations`` row via the ledger's transaction helper."""
    unknown = set(cols) - set(OBSERVATION_FIELDS)
    if unknown:
        raise ValueError(f"unknown clock observation field(s): {', '.join(sorted(unknown))}")
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    with ledger.tx() as c:
        cur = c.execute(f"INSERT INTO clock_observations({keys}) VALUES ({marks})", tuple(cols.values()))
        obs_id = int(cur.lastrowid or 0)
        # Integration with the tamper-evident ledger: the observation is chained in the same
        # transaction (ledgerchain.COVERED["clock_observations"], insert-only).
        chain_insert = getattr(ledger, "_chain_insert", None)
        if chain_insert is not None:
            chain_insert(c, "clock_observations", obs_id)
        return obs_id
