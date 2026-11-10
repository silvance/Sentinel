"""Tamper-evident ledger hash chain (sentinel.ledgerchain): format, detection, anchoring, cost.

Every tamper test edits the database the way an attacker with file access would (plain
sqlite3, bypassing the recorder) and checks that verification reports the right first
position at which the log can no longer be confirmed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import struct
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sentinel import ledgerchain as lc
from sentinel.alerts import Severity
from sentinel.db import DDL, EventSpec, Ledger
from sentinel.models import SegmentState, StorageLevel
from sentinel.status import Measurements, evaluate

T0 = "2026-11-09T00:00:00.000000Z"


def _utc(s: float) -> str:
    from sentinel.clock import add_seconds_iso
    return add_seconds_iso(T0, s)


# ---------------------------------------------------------------------- helpers
def _run(led: Ledger, run: str) -> None:
    led.create_run(run_id=run, camera_id="CAM01", directory=f"CAM01/{run}", launched_utc=T0, launch_mono=12.25,
                   display_timezone="UTC", app_version="test")


def _sealed_segment(led: Ledger, run: str, i: int, close_gap: bool = False) -> str:
    s = led.insert_segment(camera_id="CAM01", run_id=run, seq=i, rel_path=f"CAM01/{run}/seg_{i:06d}.ts")
    led.transition(s, SegmentState.CLOSED, stream_start_s=2.0 * i, stream_end_s=2.0 * (i + 1),
                   start_utc=_utc(2.0 * i), end_utc=_utc(2.0 * (i + 1)), time_source="run_anchor")
    led.seal(s, SegmentState.FINALIZED,
             dict(sha256=hashlib.sha256(f"{run}{i}".encode()).hexdigest(), size_bytes=1000 + i, duration_s=2.0,
                  warnings="[]", sealed_utc=_utc(2.0 * (i + 1) + 0.5)),
             [], _utc(2.0 * i) if close_gap else None, [EventSpec("info", "recorder", "sealed", f"segment {i}")])
    return s


def _populate(led: Ledger) -> dict[str, Any]:
    """Two runs separated by an outage gap; 3 + 2 sealed segments; content hash added later."""
    _run(led, "R1")
    segs = [_sealed_segment(led, "R1", i) for i in range(3)]
    led.update_run("R1", ended_utc=_utc(6.5), end_reason="ffmpeg_error", exit_code=1, last_stream_time_s=6.0)
    gap = led.begin_gap("CAM01", _utc(6.0), "ffmpeg_error", segs[-1], "R1", "segment_end")
    _run(led, "R2")
    segs.append(_sealed_segment(led, "R2", 0, close_gap=True))
    segs.append(_sealed_segment(led, "R2", 1))
    led.update_segment(segs[0], content_sha256="ab" * 32, content_hash_method="test", decoded_frame_count=30)
    led.add_event("audit", "recorder", "service_stop", "stopped")
    return {"segs": segs, "gap": gap}


@pytest.fixture()
def db(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    led = Ledger(tmp_path / "sentinel.db")
    info = _populate(led)
    led.close()
    return tmp_path / "sentinel.db", info


def _sql(db: Path, sql: str, params: tuple = ()) -> None:
    c = sqlite3.connect(db)
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def _seq_of(db: Path, table: str, key: Any) -> int:
    c = sqlite3.connect(db)
    try:
        return int(c.execute("SELECT MAX(seq) FROM ledger_chain WHERE ref_table=? AND ref_key=?",
                             (table, str(key))).fetchone()[0])
    finally:
        c.close()


def _head(db: Path) -> lc.ChainHead:
    h = lc.current_chain_head(db)
    assert h is not None
    return h


def _verify(db: Path, **kw: Any) -> lc.VerifyReport:
    return lc.verify_ledger(db, **kw)


def _codes(rep: lc.VerifyReport) -> set[str]:
    return {f.code for f in rep.findings}


# ---------------------------------------------------------------------- canonical JSON
def _f(hexbits: str) -> float:
    return struct.unpack(">d", bytes.fromhex(hexbits))[0]


# RFC 8785 Appendix B number serialisation samples (IEEE-754 bits -> expected text).
JCS_VECTORS = [
    ("0000000000000000", "0"), ("8000000000000000", "0"), ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"), ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"), ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"), ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"), ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"), ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"), ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"), ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"), ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"), ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"), ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]


@pytest.mark.parametrize("bits,expected", JCS_VECTORS)
def test_numbers_follow_rfc8785(bits: str, expected: str) -> None:
    assert lc.canonical_json(_f(bits)) == expected


def test_canonical_json_shape() -> None:
    v = {"b": [1, 2.0, 0.5, None, True], "a": "é\"\\\n\x01", "c": {"z": 1e-7, "y": 1e21}}
    assert lc.canonical_json(v) == '{"a":"é\\"\\\\\\n\\u0001","b":[1,2,0.5,null,true],"c":{"y":1e+21,"z":1e-7}}'
    # Re-parsing canonical text and canonicalising again is stable (verification relies on it).
    for x in [v, {"t": 333333333.33333325, "u": -0.0, "w": 12.25}]:
        once = lc.canonical_json(x)
        assert lc.canonical_json(json.loads(once)) == once
    assert lc.canonical_json(float("nan")) == '"NaN"'
    assert lc.canonical_json(b"\x00\xff") == "\"x'00ff'\""


def test_make_record_equals_canonical_object() -> None:
    data = {"k": 1.5, "s": "x"}
    rec = lc.make_record(7, T0, "event", "events", "3", lc.canonical_json(data))
    assert rec == lc.canonical_json({"v": 1, "seq": 7, "utc": T0, "kind": "event", "table": "events", "key": "3",
                                     "data": data})


# ---------------------------------------------------------------------- growth and structure
def test_new_ledger_has_genesis_and_chain_covers_every_change(db: tuple[Path, dict[str, Any]]) -> None:
    path, info = db
    c = sqlite3.connect(path)
    rows = c.execute("SELECT seq, kind, ref_table, ref_key, record FROM ledger_chain ORDER BY seq").fetchall()
    c.close()
    assert rows[0][1] == "genesis" and json.loads(rows[0][4])["data"]["pre_existing_rows"]["segments"] == 0
    kinds = [r[1] for r in rows]
    assert kinds.count("run_started") == 2 and kinds.count("run_ended") == 1
    assert kinds.count("segment_sealed") == 5 and kinds.count("segment_updated") == 1
    assert kinds.count("gap_opened") == 1 and kinds.count("gap_closed") == 1
    assert kinds.count("event") >= 7                       # 5 seal events + gap_closed + service_stop
    assert [r[0] for r in rows] == list(range(len(rows)))
    seal = next(json.loads(r[4]) for r in rows if r[1] == "segment_sealed" and r[3] == info["segs"][1])
    assert seal["data"]["state"] == "FINALIZED" and len(seal["data"]["sha256"]) == 64
    assert seal["data"]["end_utc"] == _utc(4.0) and seal["data"]["start_utc"] == _utc(2.0)
    rep = _verify(path)
    assert rep.ok, rep.render_text()
    assert rep.entries == len(rows) and rep.head == _head(path)
    assert "INTACT" in rep.render_text()


def test_unchanged_bookkeeping_writes_add_no_entries(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.db")
    _run(led, "R1")
    n = led.chain_head().seq
    led.update_run("R1", last_stream_time_s=5.0)          # persisted every 10 s while recording: not covered
    s = led.insert_segment(camera_id="CAM01", run_id="R1", seq=0, rel_path="a.ts")   # not sealed: not covered
    led.record_finalize_failure(s, "boom")
    assert led.chain_head().seq == n
    led.close()


def test_seal_and_its_chain_entries_are_one_transaction(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.db")
    _run(led, "R1")
    s = led.insert_segment(camera_id="CAM01", run_id="R1", seq=0, rel_path="a.ts")
    led.transition(s, SegmentState.CLOSED)
    before = led.chain_head()
    with pytest.raises(TypeError):        # an event detail that cannot be stored aborts the whole seal
        led.seal(s, SegmentState.FINALIZED, {"sha256": "00" * 32}, [], None,
                 [EventSpec("info", "x", "y", "z", {"bad": object()})])
    assert led.chain_head() == before and led.get_segment(s)["state"] == "CLOSED"
    led.seal(s, SegmentState.FINALIZED, {"sha256": "00" * 32}, [], None, [])
    assert led.chain_head().seq == before.seq + 1
    led.close()


# ---------------------------------------------------------------------- tampering with records
@pytest.mark.parametrize("column,value", [("sha256", "ff" * 32), ("end_utc", "2026-11-09T00:00:05.000000Z"),
                                          ("state", "CORRUPT"), ("size_bytes", 999999)])
def test_edited_segment_is_detected_at_its_entry(db: tuple[Path, dict[str, Any]], column: str, value: Any) -> None:
    path, info = db
    seg = info["segs"][1]
    _sql(path, f"UPDATE segments SET {column}=? WHERE segment_id=?", (value, seg))
    rep = _verify(path)
    assert not rep.ok
    assert rep.first.code == "record_altered" and rep.first.position == _seq_of(path, "segments", seg)
    assert column in rep.first.message and "seg_000001.ts" in rep.first.message


def test_segment_set_back_to_unsealed_is_detected(db: tuple[Path, dict[str, Any]]) -> None:
    path, info = db
    seg = info["segs"][2]
    _sql(path, "UPDATE segments SET state='ACTIVE' WHERE segment_id=?", (seg,))
    rep = _verify(path)
    assert rep.first.code == "record_unsealed" and rep.first.position == _seq_of(path, "segments", seg)


def test_edited_gap_and_run_are_detected(db: tuple[Path, dict[str, Any]]) -> None:
    path, info = db
    _sql(path, "UPDATE recording_gaps SET duration_s=0.1 WHERE gap_id=?", (info["gap"],))
    _sql(path, "UPDATE recording_runs SET end_reason='graceful_stop' WHERE run_id='R1'")
    rep = _verify(path)
    pos = {f.code + f.message.split(" was changed")[0][-2:]: f.position for f in rep.findings}
    assert _codes(rep) == {"record_altered"} and len(rep.findings) == 2
    assert sorted(f.position for f in rep.findings) == sorted(
        [_seq_of(path, "recording_gaps", info["gap"]), _seq_of(path, "recording_runs", "R1")]), pos


@pytest.mark.parametrize("table,key_col", [("segments", "segment_id"), ("events", "event_id"),
                                           ("recording_gaps", "gap_id")])
def test_deleted_row_is_detected(db: tuple[Path, dict[str, Any]], table: str, key_col: str) -> None:
    path, info = db
    key = {"segments": info["segs"][3], "events": 2, "recording_gaps": info["gap"]}[table]
    _sql(path, "PRAGMA foreign_keys=OFF")
    _sql(path, f"DELETE FROM {table} WHERE {key_col}=?", (key,))
    rep = _verify(path)
    assert rep.first.code == "record_deleted" and rep.first.position == _seq_of(path, table, key)


def test_reordered_events_are_detected(db: tuple[Path, dict[str, Any]]) -> None:
    path, _ = db
    _sql(path, "UPDATE events SET event_id=-1 WHERE event_id=2")
    _sql(path, "UPDATE events SET event_id=2 WHERE event_id=3")
    _sql(path, "UPDATE events SET event_id=3 WHERE event_id=-1")
    rep = _verify(path)
    assert rep.first.code == "record_altered" and rep.first.position == _seq_of(path, "events", 2)


def test_row_added_outside_recorder_is_detected(db: tuple[Path, dict[str, Any]]) -> None:
    path, _ = db
    _sql(path, "INSERT INTO events(ts_utc, severity, category, code, message) VALUES (?,?,?,?,?)",
         (T0, "info", "recorder", "fake", "inserted by hand"))
    rep = _verify(path)
    assert rep.first.code == "record_unlogged" and rep.first.position == rep.entries


# ---------------------------------------------------------------------- tampering with the chain itself
def test_reordered_chain_entries_are_detected(db: tuple[Path, dict[str, Any]]) -> None:
    path, _ = db
    _sql(path, "UPDATE ledger_chain SET seq=-1 WHERE seq=5")
    _sql(path, "UPDATE ledger_chain SET seq=5 WHERE seq=9")
    _sql(path, "UPDATE ledger_chain SET seq=9 WHERE seq=-1")
    rep = _verify(path)
    assert rep.first.code == "entry_out_of_place" and rep.first.position == 5


def test_edited_chain_record_is_detected(db: tuple[Path, dict[str, Any]]) -> None:
    path, info = db
    seq = _seq_of(path, "segments", info["segs"][1])
    c = sqlite3.connect(path)
    rec = c.execute("SELECT record FROM ledger_chain WHERE seq=?", (seq,)).fetchone()[0]
    c.close()
    forged = rec.replace('"FINALIZED"', '"CORRUPT"')
    _sql(path, "UPDATE ledger_chain SET record=? WHERE seq=?", (forged, seq))
    rep = _verify(path)
    assert rep.first.code == "entry_altered" and rep.first.position == seq
    # ... and also when the attacker recomputes that entry's own hash: the next entry no longer links.
    prev = sqlite3.connect(path).execute("SELECT prev_hash FROM ledger_chain WHERE seq=?", (seq,)).fetchone()[0]
    _sql(path, "UPDATE ledger_chain SET entry_hash=? WHERE seq=?", (lc.entry_hash(prev, forged), seq))
    rep = _verify(path)
    assert rep.first.code == "link_broken" and rep.first.position == seq


def test_deleted_chain_entry_is_detected(db: tuple[Path, dict[str, Any]]) -> None:
    path, _ = db
    _sql(path, "DELETE FROM ledger_chain WHERE seq=6")
    rep = _verify(path)
    assert rep.first.code == "entry_missing" and rep.first.position == 6


def test_truncated_chain_tail_is_detected_at_cut(tmp_path: Path) -> None:
    path = tmp_path / "sentinel.db"
    led = Ledger(path)
    _run(led, "R1")
    _sealed_segment(led, "R1", 0)
    cut = led.chain_head().seq + 1
    for i in range(1, 4):                                  # later work: only new rows
        _sealed_segment(led, "R1", i)
    lc.append_anchor(tmp_path / lc.ANCHOR_LOG_NAME, led.chain_head(), "periodic")
    led.close()
    _sql(path, "DELETE FROM ledger_chain WHERE seq >= ?", (cut,))
    rep = _verify(path)
    assert rep.first.position == cut
    assert {"log_truncated", "record_unlogged"} <= _codes(rep)
    assert all(f.position == cut for f in rep.findings)
    # Without any saved head the cut is still visible through the rows it no longer covers.
    rep2 = _verify(path, use_local_anchors=False)
    assert rep2.first.position == cut and _codes(rep2) == {"record_unlogged"}


def test_emptied_chain_is_detected_at_start(db: tuple[Path, dict[str, Any]]) -> None:
    path, _ = db
    _sql(path, "DELETE FROM ledger_chain")
    rep = _verify(path)
    assert rep.first.position == 0 and "chain_empty" in _codes(rep)


def test_dropped_chain_table_is_detected(db: tuple[Path, dict[str, Any]]) -> None:
    path, _ = db
    _sql(path, "DROP TABLE ledger_chain")
    rep = _verify(path)
    assert rep.first.code == "chain_missing" and rep.first.position == 0


def _forge_from_scratch(path: Path, seg: str, new_sha: str) -> None:
    """What a capable attacker does: change the row AND rebuild a fully consistent chain."""
    _sql(path, "UPDATE segments SET sha256=? WHERE segment_id=?", (new_sha, seg))
    c = sqlite3.connect(path)
    prev = lc.ZERO_HASH
    for seq, rec in c.execute("SELECT seq, record FROM ledger_chain ORDER BY seq").fetchall():
        r = json.loads(rec)
        if r["table"] == "segments" and r["key"] == seg:
            r["data"]["sha256"] = new_sha
        text = lc.canonical_json(r)
        h = lc.entry_hash(prev, text)
        c.execute("UPDATE ledger_chain SET record=?, prev_hash=?, entry_hash=? WHERE seq=?", (text, prev, h, seq))
        prev = h
    c.commit()
    c.close()


def test_chain_recomputed_from_scratch_is_caught_only_by_an_earlier_anchor(tmp_path: Path) -> None:
    path = tmp_path / "sentinel.db"
    led = Ledger(path)
    _run(led, "R1")
    early = led.chain_head()                                 # anchored before the segment existed
    lc.append_anchor(tmp_path / lc.ANCHOR_LOG_NAME, early, "service_start")
    seg = _sealed_segment(led, "R1", 0)
    _sealed_segment(led, "R1", 1)
    anchored = led.chain_head()
    lc.append_anchor(tmp_path / lc.ANCHOR_LOG_NAME, anchored, "periodic")
    led.close()
    _forge_from_scratch(path, seg, "ee" * 32)
    # Honest limitation: with no saved head, a fully recomputed chain is self-consistent.
    assert _verify(path, use_local_anchors=False).ok
    rep = _verify(path)
    assert rep.first.code == "anchor_mismatch" and rep.first.position == anchored.seq
    assert f"after entry {early.seq}" in rep.first.message
    # A head saved elsewhere (export manifest, diagnostic report) works the same way.
    ext = lc.parse_expected_head(f"{anchored.seq}:{anchored.hash}", "in an export manifest")
    rep = _verify(path, anchors=[ext], use_local_anchors=False)
    assert rep.first.code == "anchor_mismatch" and "export manifest" in rep.first.message


# ---------------------------------------------------------------------- write-time detection
def test_change_outside_recorder_cannot_be_laundered_by_a_later_update(tmp_path: Path) -> None:
    path = tmp_path / "sentinel.db"
    led = Ledger(path)
    _run(led, "R1")
    seg = _sealed_segment(led, "R1", 0)
    seal_seq = led.chain_head().seq
    _sql(path, "UPDATE segments SET sha256=? WHERE segment_id=?", ("ee" * 32, seg))
    led.update_segment(seg, content_sha256="cd" * 32)       # legitimate later write snapshots the row...
    assert led.chain_discrepancies == 1
    led.close()
    rep = _verify(path)                                     # ...but the discrepancy is in the chain for good
    assert rep.first.code == "discrepancy_recorded" and rep.first.position > seal_seq
    assert "sha256" in rep.first.message
    ev = sqlite3.connect(path).execute("SELECT COUNT(*) FROM events WHERE code='ledger_discrepancy'").fetchone()[0]
    assert ev == 1


def test_chain_failure_never_blocks_sealing(tmp_path: Path) -> None:
    path = tmp_path / "sentinel.db"
    led = Ledger(path)
    _run(led, "R1")
    s = led.insert_segment(camera_id="CAM01", run_id="R1", seq=0, rel_path="a.ts")
    led.transition(s, SegmentState.CLOSED)
    _sql(path, "DROP TABLE ledger_chain")
    led.seal(s, SegmentState.FINALIZED, {"sha256": "00" * 32}, [], None, [EventSpec("info", "x", "y", "z")])
    assert led.get_segment(s)["state"] == "FINALIZED" and led.chain_failures >= 1
    led.close()
    assert not _verify(path).ok


def test_wiped_chain_restarts_marked_on_next_start(db: tuple[Path, dict[str, Any]]) -> None:
    path, _ = db
    _sql(path, "DELETE FROM ledger_chain")
    Ledger(path).close()                                    # recorder restarts
    rep = _verify(path)
    assert rep.first.code == "chain_restarted" and rep.first.position == 0


# ---------------------------------------------------------------------- ledgers created before this feature
def test_existing_ledger_gets_genesis_and_adopts_rows(tmp_path: Path) -> None:
    path = tmp_path / "sentinel.db"
    c = sqlite3.connect(path)
    c.executescript(DDL)                                    # schema as shipped before the chain existed
    c.execute("INSERT INTO schema_meta VALUES ('schema_version','1')")
    c.execute("INSERT INTO recording_runs(run_id, camera_id, directory, launched_utc, launch_mono, display_timezone,"
              " app_version, ended_utc) VALUES ('OLD','CAM01','CAM01/OLD',?,1.0,'UTC','0.1',?)", (T0, _utc(9)))
    c.execute("INSERT INTO segments(segment_id, camera_id, run_id, seq, rel_path, state, sha256, created_utc,"
              " updated_utc) VALUES ('S-OLD','CAM01','OLD',0,'CAM01/OLD/seg_000000.ts','FINALIZED',?,?,?)",
              ("aa" * 32, T0, T0))
    c.execute("INSERT INTO segments(segment_id, camera_id, run_id, seq, rel_path, state, created_utc, updated_utc)"
              " VALUES ('S-ACT','CAM01','OLD',1,'CAM01/OLD/seg_000001.ts','ACTIVE',?,?)", (T0, T0))
    c.execute("INSERT INTO events(ts_utc, severity, category, code, message) VALUES (?,?,?,?,?)",
              (T0, "audit", "recorder", "service_start", "old"))
    c.commit()
    c.close()
    led = Ledger(path)                                      # upgrade
    rows = led.query("SELECT kind, ref_key, record FROM ledger_chain ORDER BY seq")
    genesis = json.loads(rows[0]["record"])["data"]
    assert genesis["pre_existing_rows"] == {"recording_runs": 1, "segments": 2, "recording_gaps": 0, "events": 1}
    assert genesis["adopted_rows"] == 3 and not genesis["previous_chain_missing"]
    assert [r["kind"] for r in rows[1:]] == ["adopted"] * 3          # the ACTIVE segment is not yet covered
    led.transition("S-ACT", SegmentState.INTERRUPTED, sha256="bb" * 32)   # sealed after the upgrade: covered
    led.close()
    rep = _verify(path)
    assert rep.ok and any("before tamper evidence was introduced" in n for n in rep.notes)
    _sql(path, "UPDATE segments SET sha256=? WHERE segment_id='S-OLD'", ("cc" * 32,))
    rep = _verify(path)
    assert rep.first.code == "record_altered" and rep.first.position == _seq_of(path, "segments", "S-OLD")


# ---------------------------------------------------------------------- anchors, files, alert, CLI
def test_anchor_log_lines_round_trip(tmp_path: Path) -> None:
    p = tmp_path / lc.ANCHOR_LOG_NAME
    h = lc.ChainHead(12, "ab" * 32, T0)
    line = lc.append_anchor(p, h, "periodic")
    assert line.endswith(f"seq=12 hash={'ab' * 32} entry_utc={T0} reason=periodic")
    with open(p, "a", encoding="ascii") as fh:
        fh.write("2026-11-09T00:00:00.000000Z seq=13 hash=trunc")             # power cut mid-line
    anchors, problems = lc.read_anchor_log(p)
    assert [(a.seq, a.hash) for a in anchors] == [(12, "ab" * 32)] and len(problems) == 1
    with pytest.raises(ValueError):
        lc.append_anchor(p, h, "Not A Word")


def test_check_files_compares_sealed_file_hashes(tmp_path: Path) -> None:
    path, spool = tmp_path / "sentinel.db", tmp_path / "spool"
    f = spool / "CAM01" / "R1" / "seg_000000.ts"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x" * 1000)
    led = Ledger(path)
    _run(led, "R1")
    s = led.insert_segment(camera_id="CAM01", run_id="R1", seq=0, rel_path="CAM01/R1/seg_000000.ts")
    led.transition(s, SegmentState.CLOSED)
    led.seal(s, SegmentState.FINALIZED, {"sha256": hashlib.sha256(b"x" * 1000).hexdigest(), "size_bytes": 1000},
             [], None, [])
    led.close()
    rep = _verify(path, spool_path=spool, check_files=True)
    assert rep.ok and rep.files_checked == 1
    f.write_bytes(b"y" * 1000)
    rep = _verify(path, spool_path=spool, check_files=True)
    assert rep.first.code == "file_altered" and rep.first.position == _seq_of(path, "segments", s)


def test_v003_alert_wording() -> None:
    m = Measurements(now_mono=100.0, service_started_mono=0.0, ffmpeg_running=True, media_seen_this_run=True,
                     seconds_since_media_advance=1.0, seconds_since_last_seal=5.0, storage_level=StorageLevel.OK,
                     storage_error=None, component_errors=[], in_backoff=False, ledger_altered=True)
    d = evaluate(m, 6, 2, 10)
    a = next(x for x in d.alerts if x.code == "V-003")
    assert a.severity == Severity.CRITICAL and not a.recording_stopped
    assert a.message == ("Error V-003: The recording log has been altered. "
                         "Contact technical support and provide diagnostic report.")


def _write_toml(tmp: Path) -> Path:
    p = tmp / "sentinel.toml"
    p.write_text("\n".join([
        "[camera]", 'id = "CAM01"', 'url = "tcp://127.0.0.1:9"', 'input_format = "mpegts"',
        "development_source = true", "[storage]", f"spool_dir = '{tmp / 'spool'}'", f"data_dir = '{tmp}'",
        f"log_dir = '{tmp / 'logs'}'", ""]), "utf-8")
    return p


def test_cli_verify_and_head(db: tuple[Path, dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> None:
    path, info = db
    cfg = _write_toml(path.parent)
    assert lc.main(["head", "--config", str(cfg)]) == 0
    head = _head(path)
    assert f"--expect-head {head.seq}:{head.hash}" in capsys.readouterr().out
    assert lc.main(["verify", "--config", str(cfg), "--expect-head", f"{head.seq}:{head.hash}"]) == 0
    assert "INTACT" in capsys.readouterr().out
    _sql(path, "UPDATE segments SET end_utc=? WHERE segment_id=?", (_utc(100), info["segs"][0]))
    assert lc.main(["verify", "--config", str(cfg)]) == 1
    out = capsys.readouterr().out
    assert "ALTERED" in out and f"at log entry {_seq_of(path, 'segments', info['segs'][0])}" in out
    assert lc.main(["verify", "--config", str(cfg), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["first_problem"]["code"] == "record_altered"
    assert lc.main(["verify", "--config", str(cfg), "--expect-head", "nonsense"]) == 2


# ---------------------------------------------------------------------- independent re-implementation
def _ecma_number(x: float) -> str:
    """Independent of ledgerchain: shortest round-trip digits via '%.Ng' search + Decimal."""
    if x == 0:
        return "0"
    for p in range(1, 18):
        s = format(x, f".{p}g")
        if float(s) == x:
            break
    sign, digits, exp = Decimal(s).as_tuple()
    ds = "".join(map(str, digits)).rstrip("0") or "0"
    n = len(digits) + exp
    k = len(ds)
    if k <= n <= 21:
        out = ds + "0" * (n - k)
    elif 0 < n <= 21:
        out = ds[:n] + "." + ds[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * -n + ds
    else:
        out = ds[0] + ("." + ds[1:] if k > 1 else "") + "e" + ("+" if n - 1 >= 0 else "-") + str(abs(n - 1))
    return ("-" if sign else "") + out


def _indep_canon(v: Any) -> str:
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(k) + ":" + _indep_canon(v[k]) for k in sorted(v)) + "}"
    if isinstance(v, list):
        return "[" + ",".join(_indep_canon(x) for x in v) + "]"
    if isinstance(v, float):
        return _ecma_number(v)
    return json.dumps(v, ensure_ascii=False)


def test_an_examiner_can_verify_from_the_written_specification(db: tuple[Path, dict[str, Any]]) -> None:
    """Re-implements format 1 from the docstring/docs only (sqlite3 + hashlib + json)."""
    path, _ = db
    c = sqlite3.connect(path)
    prev = "0" * 64
    for seq, record, prev_hash, entry_hash in c.execute(
            "SELECT seq, record, prev_hash, entry_hash FROM ledger_chain ORDER BY seq"):
        r = json.loads(record)
        assert _indep_canon(r) == record and r["seq"] == seq and prev_hash == prev
        prev = hashlib.sha256((prev + record).encode("utf-8")).hexdigest()
        assert prev == entry_hash
        if r["table"] == "segments":                    # covered columns equal the database row
            cols = list(r["data"])
            row = c.execute(f"SELECT {', '.join(cols)} FROM segments WHERE segment_id=?", (r["key"],)).fetchone()
            latest = c.execute("SELECT MAX(seq) FROM ledger_chain WHERE ref_table='segments' AND ref_key=?",
                               (r["key"],)).fetchone()[0]
            if latest == seq:
                assert _indep_canon(dict(zip(cols, row))) == _indep_canon(r["data"])
    c.close()
    assert prev == _head(path).hash


# ---------------------------------------------------------------------- cost
def test_chain_cost_per_seal_is_negligible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    spent = {"t": 0.0}

    def timed(fn: Any) -> Any:
        def wrapper(*a: Any, **kw: Any) -> Any:
            t = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                spent["t"] += time.perf_counter() - t
        return wrapper

    for name in ("track", "record_change", "record_insert"):
        monkeypatch.setattr(lc, name, timed(getattr(lc, name)))
    led = Ledger(tmp_path / "s.db")
    _run(led, "R1")
    seal_total, chain_per_seal = [], []
    for i in range(200):
        s = led.insert_segment(camera_id="CAM01", run_id="R1", seq=i, rel_path=f"x/{i}.ts")
        led.transition(s, SegmentState.CLOSED, start_utc=_utc(i), end_utc=_utc(i + 1))
        spent["t"] = 0.0
        t = time.perf_counter()
        led.seal(s, SegmentState.FINALIZED, dict(sha256="ab" * 32, size_bytes=10 ** 9, duration_s=300.0,
                                                 validation_detail=json.dumps({"x": "y" * 400})),
                 [], None, [EventSpec("info", "recorder", "sealed", "s")])
        seal_total.append(time.perf_counter() - t)
        chain_per_seal.append(spent["t"])
    led.close()
    t = time.perf_counter()
    rep = _verify(tmp_path / "s.db")
    verify_s = time.perf_counter() - t
    assert rep.ok
    med_seal, med_chain = statistics.median(seal_total), statistics.median(chain_per_seal)
    with capsys.disabled():
        print(f"\n[chain cost] seal median {med_seal * 1e3:.2f} ms, of which chain {med_chain * 1e3:.3f} ms "
              f"({100 * med_chain / med_seal:.1f}%); verify {rep.entries} entries in {verify_s:.3f} s "
              f"({rep.entries / verify_s:.0f} entries/s)")
    assert med_chain < 0.002                      # < 2 ms per seal (a seal happens once per segment period)
