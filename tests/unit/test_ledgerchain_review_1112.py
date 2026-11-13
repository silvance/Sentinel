"""Recording-log verification fixes from the 2026-11-12 review (sentinel.ledgerchain).

* drive fingerprints of ANOTHER recording log (shared drive, drive reused after a fresh installation)
  are not reported as alteration, and are never counted as confirming this log;
* ``--anchors-from`` on a folder inside a drive finds the drive's fingerprints, and a drive with nothing
  to compare gives "NOT CONFIRMED" (exit 2), never INTACT;
* a well-hashed entry whose kind / table / values have types the recorder never writes is a finding,
  never a crash;
* ``--check-files`` reports originals removed by the retention rule as removed, not as missing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sentinel import ledgerchain as lc
from sentinel.archive import layout
from sentinel.db import Ledger
from sentinel.models import SegmentState

OLD = "2020-01-01T00:00:00.000000Z"          # long before any ledger in these tests was started


def _ledger(path: Path, events: int = 5) -> Ledger:
    led = Ledger(path)
    for i in range(events):
        led.add_event("info", "recorder", f"e{i}", "something happened")
    return led


def _head(led: Ledger) -> lc.ChainHead:
    h = lc.current_chain_head(led)
    assert h is not None
    return h


def _write_toml(tmp: Path, camera: str = "CAM01") -> Path:
    p = tmp / "sentinel.toml"
    p.write_bytes("\n".join([
        "[camera]", f'id = "{camera}"', 'url = "tcp://127.0.0.1:9"', 'input_format = "mpegts"',
        "development_source = true", "[storage]", f"spool_dir = '{tmp / 'spool'}'", f"data_dir = '{tmp}'",
        f"log_dir = '{tmp / 'logs'}'", ""]).encode("utf-8"))
    return p


def _legacy_line(path: Path, seq: int, h: str, anchored: str, reason: str = "archive_swap") -> None:
    """An anchor line as written before the log identity was added (no chain=)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="ascii", newline="\n") as fh:
        fh.write(f"{anchored} seq={seq} hash={h} entry_utc={anchored} reason={reason}\n")


# ---------------------------------------------------------------------- log identity in anchors
def test_heads_and_anchor_lines_name_their_log(tmp_path: Path) -> None:
    led = _ledger(tmp_path / "a.db")
    head = _head(led)
    first = led.query("SELECT entry_hash FROM ledger_chain WHERE seq=0")[0][0]
    assert head.chain == first and head.to_dict()["chain"] == first
    line = lc.append_anchor(tmp_path / "chain_heads.log", head, "periodic")
    assert line.endswith(f" chain={first}")
    anchors, problems = lc.read_anchor_log(tmp_path / "chain_heads.log")
    assert not problems and anchors[0].chain == first and anchors[0].hash == head.hash
    _legacy_line(tmp_path / "chain_heads.log", 3, "ab" * 32, OLD)            # older lines still read
    anchors, problems = lc.read_anchor_log(tmp_path / "chain_heads.log")
    assert not problems and anchors[1].chain is None and anchors[1].seq == 3
    led.close()


def test_another_cameras_log_on_a_shared_drive_is_not_reported_altered(tmp_path: Path) -> None:
    """The reviewer's reproduction: ledger A (4 entries) and ledger B (9 entries) share an archive drive."""
    a = _ledger(tmp_path / "A" / "sentinel.db", 3)
    b = _ledger(tmp_path / "B" / "sentinel.db", 8)
    drive = tmp_path / "drive"
    lc.append_anchor(drive / layout.chain_heads_path("camB"), _head(b), "archive_attach")
    lc.append_anchor(drive / layout.chain_heads_path("camA"), _head(a), "archive_attach")
    a.close()
    b.close()
    found, _ = lc.anchors_from(drive)
    rep = lc.verify_ledger(tmp_path / "A" / "sentinel.db", anchors=found, use_local_anchors=False,
                           drives=[str(drive)], camera_id="camA")
    assert rep.ok and not rep.unconfirmed, rep.render_text()
    assert rep.drive_anchors_compared == {str(drive): 1}
    assert any("another camera" in n for n in rep.notes)
    assert lc.exit_status(rep) == 0 and "RESULT: INTACT" in rep.render_text()


def test_a_drive_reused_after_a_fresh_installation_is_not_reported_altered(tmp_path: Path) -> None:
    """Old (pre-fix) lines written before this log was started, plus this log's own head."""
    led = _ledger(tmp_path / "sentinel.db")
    drive = tmp_path / "drive"
    log_path = drive / layout.chain_heads_path("CAM01")
    _legacy_line(log_path, 40, "cd" * 32, OLD)                       # longer than this log: would be "truncated"
    _legacy_line(log_path, 2, "ef" * 32, OLD)                        # different hash: would be "rewritten"
    lc.append_anchor(log_path, _head(led), "archive_attach")
    led.close()
    found, _ = lc.anchors_from(drive)
    rep = lc.verify_ledger(tmp_path / "sentinel.db", anchors=found, use_local_anchors=False,
                           drives=[str(drive)], camera_id="CAM01")
    assert rep.ok and not rep.unconfirmed, rep.render_text()
    assert rep.drive_anchors_compared[str(drive)] == 1
    assert any("before this log was started" in n for n in rep.notes)


def test_another_logs_fingerprint_saved_after_this_log_started_is_not_confirmed(tmp_path: Path) -> None:
    """The same camera's folder holds a head of a different log saved while this log existed: either
    another computer uses the drive, or this log was rebuilt from entry 0. Never INTACT, never ignored."""
    led = _ledger(tmp_path / "sentinel.db")
    head = _head(led)
    led.close()
    drive = tmp_path / "drive"
    lc.append_anchor(drive / layout.chain_heads_path("CAM01"),
                     lc.ChainHead(head.seq, "11" * 32, head.utc, "22" * 32), "archive_swap")
    found, _ = lc.anchors_from(drive)
    rep = lc.verify_ledger(tmp_path / "sentinel.db", anchors=found, use_local_anchors=False,
                           drives=[str(drive)], camera_id="CAM01")
    assert rep.ok and {f.code for f in rep.unconfirmed} == {"anchor_other_log", "drive_not_compared"}
    assert lc.exit_status(rep) == 2 and "NOT CONFIRMED" in rep.render_text() and "INTACT" not in rep.render_text()


def test_a_rewritten_log_is_still_altered_against_its_own_fingerprints(tmp_path: Path) -> None:
    led = _ledger(tmp_path / "sentinel.db")
    head = _head(led)
    led.close()
    drive = tmp_path / "drive"
    lc.append_anchor(drive / layout.chain_heads_path("CAM01"),
                     lc.ChainHead(head.seq, "33" * 32, head.utc, head.chain), "archive_swap")   # same log, other hash
    lc.append_anchor(drive / layout.chain_heads_path("CAM01"),
                     lc.ChainHead(head.seq + 50, "44" * 32, head.utc, head.chain), "archive_swap")
    found, _ = lc.anchors_from(drive)
    rep = lc.verify_ledger(tmp_path / "sentinel.db", anchors=found, use_local_anchors=False,
                           drives=[str(drive)], camera_id="CAM01")
    assert not rep.ok and {f.code for f in rep.findings} == {"anchor_mismatch", "log_truncated"}
    assert lc.exit_status(rep) == 1


def test_an_old_fingerprint_that_does_not_match_after_this_log_started_is_not_confirmed(tmp_path: Path) -> None:
    led = _ledger(tmp_path / "sentinel.db")
    head = _head(led)
    led.close()
    drive = tmp_path / "drive"
    _legacy_line(drive / layout.chain_heads_path("CAM01"), head.seq, "55" * 32, "2099-01-01T00:00:00.000000Z")
    found, _ = lc.anchors_from(drive)
    rep = lc.verify_ledger(tmp_path / "sentinel.db", anchors=found, use_local_anchors=False,
                           drives=[str(drive)], camera_id="CAM01")
    assert rep.ok and "anchor_mismatch" in {f.code for f in rep.unconfirmed}
    assert lc.exit_status(rep) == 2


def test_fingerprints_kept_on_this_computer_are_always_compared(tmp_path: Path) -> None:
    """Local anchors (and --expect-head) are not subject to the drive rules: a mismatch stays ALTERED."""
    led = _ledger(tmp_path / "sentinel.db")
    head = _head(led)
    led.close()
    lc.append_anchor(tmp_path / lc.ANCHOR_LOG_NAME, lc.ChainHead(head.seq, "66" * 32, head.utc, "77" * 32), "periodic")
    rep = lc.verify_ledger(tmp_path / "sentinel.db")
    assert not rep.ok and "anchor_mismatch" in {f.code for f in rep.findings}


def test_review_manifest_head_carries_the_log_identity(tmp_path: Path) -> None:
    led = _ledger(tmp_path / "sentinel.db")
    head = _head(led)
    led.close()
    m = tmp_path / "review" / "Manifests" / "x" / "Export_Manifest.json"
    m.parent.mkdir(parents=True)
    m.write_bytes(json.dumps({"camera_id": "CAM01", "recording_log_head": head.to_dict()}).encode("utf-8"))
    found, _ = lc.anchors_from(tmp_path / "review")
    assert [(a.seq, a.hash, a.chain, a.camera) for a in found] == [(head.seq, head.hash, head.chain, "CAM01")]


# ---------------------------------------------------------------------- --anchors-from on a sub-folder
@pytest.mark.parametrize("pick", ["Sentinel", "Sentinel/CAM01", "Sentinel/CAM01/INDEX"])
def test_anchors_from_a_folder_inside_the_drive_reads_the_drive(tmp_path: Path, pick: str) -> None:
    led = _ledger(tmp_path / "sentinel.db")
    head = _head(led)
    led.close()
    drive = tmp_path / "drive"
    lc.append_anchor(drive / layout.chain_heads_path("CAM01"), head, "archive_attach")
    found, notes = lc.anchors_from(drive / pick)
    assert [(a.seq, a.hash) for a in found] == [(head.seq, head.hash)]
    assert found[0].drive == str(drive / pick) and any("top folder" in n for n in notes)
    rep = lc.verify_ledger(tmp_path / "sentinel.db", anchors=found, use_local_anchors=False,
                           drives=[str(drive / pick)], camera_id="CAM01")
    assert rep.ok and not rep.unconfirmed and lc.exit_status(rep) == 0


def test_cli_with_a_drive_that_has_nothing_to_compare_is_not_intact(tmp_path: Path,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    led = _ledger(tmp_path / "sentinel.db")
    head = _head(led)
    led.close()
    cfg = _write_toml(tmp_path)
    empty = tmp_path / "not_a_drive"
    empty.mkdir()
    assert lc.main(["verify", "--config", str(cfg), "--anchors-from", str(empty)]) == 2
    out = capsys.readouterr().out
    assert "Nothing to compare on" in out and "NOT CONFIRMED" in out and "RESULT: INTACT" not in out
    # A drive whose chain_heads.log exists but is empty compares nothing either.
    blank = tmp_path / "blank_drive" / layout.chain_heads_path("CAM01")
    blank.parent.mkdir(parents=True)
    blank.write_bytes(b"")
    assert lc.main(["verify", "--config", str(cfg), "--anchors-from", str(tmp_path / "blank_drive")]) == 2
    capsys.readouterr()
    # The same check against a real drive picked by its camera folder is intact.
    drive = tmp_path / "drive"
    lc.append_anchor(drive / layout.chain_heads_path("CAM01"), head, "archive_attach")
    assert lc.main(["verify", "--config", str(cfg), "--anchors-from", str(drive / "Sentinel" / "CAM01")]) == 0
    assert "RESULT: INTACT" in capsys.readouterr().out


# ---------------------------------------------------------------------- malformed entries never crash
def _append_raw(db: Path, kind: Any, table: Any, key: Any, data: Any) -> int:
    """Append one well-linked, correctly hashed entry with arbitrary JSON member values, as someone with
    access to the database file could (the chain has no key)."""
    c = sqlite3.connect(db)
    try:
        seq, prev = c.execute("SELECT seq, entry_hash FROM ledger_chain ORDER BY seq DESC LIMIT 1").fetchone()
        seq += 1
        record = lc.make_record(seq, "2026-11-12T00:00:00.000000Z", kind, table, key, lc.canonical_json(data))
        ref = (kind if isinstance(kind, str) else "event", table if isinstance(table, str) else "events",
               key if isinstance(key, str) else "999")
        c.execute("INSERT INTO ledger_chain(seq, kind, ref_table, ref_key, record, prev_hash, entry_hash) "
                  "VALUES (?,?,?,?,?,?,?)", (seq, *ref, record, prev, lc.entry_hash(prev, record)))
        c.commit()
        return int(seq)
    finally:
        c.close()


@pytest.mark.parametrize("kind,table,key", [
    (["event"], "events", "999"),
    ("event", {}, "999"),
    ("event", "events", ["999"]),
    ("event", ["events"], "999"),
])
def test_entry_with_list_or_object_members_is_reported_not_a_crash(tmp_path: Path, kind: Any, table: Any,
                                                                   key: Any) -> None:
    led = _ledger(tmp_path / "sentinel.db")
    ev = led.query("SELECT event_id FROM events ORDER BY event_id LIMIT 1")[0][0]
    led.close()
    c = sqlite3.connect(tmp_path / "sentinel.db")
    c.execute("UPDATE events SET message='changed' WHERE event_id=?", (ev,))     # an earlier alteration
    c.commit()
    c.close()
    seq = _append_raw(tmp_path / "sentinel.db", kind, table, key, {"event_id": 999})
    rep = lc.verify_ledger(tmp_path / "sentinel.db")
    codes = {f.code for f in rep.findings}
    assert not rep.ok and "entry_unreadable" in codes and "record_altered" in codes, rep.render_text()
    assert any(f.position == seq for f in rep.findings)


def test_row_values_of_unexpected_types_are_reported_not_a_crash(tmp_path: Path) -> None:
    """A later entry for an archive copy whose state is a JSON list: illegal_changes cannot compare it."""
    led = _ledger(tmp_path / "sentinel.db")
    led.close()
    row = {f: None for f in lc.ARCHIVE_COPY_FIELDS}
    row.update(copy_id=1, segment_id="s", drive_id="d", kind="original", path="p", sha256="0" * 64, state="VERIFIED")
    _append_raw(tmp_path / "sentinel.db", "archive_copy_recorded", "archive_copies", "1", row)
    seq = _append_raw(tmp_path / "sentinel.db", "archive_copy_updated", "archive_copies", "1",
                      dict(row, state=["VERIFIED", "LOST"]))
    rep = lc.verify_ledger(tmp_path / "sentinel.db")
    assert not rep.ok
    assert any(f.code == "illegal_change" and f.position == seq for f in rep.findings), rep.render_text()


def test_genesis_with_unexpected_counts_is_reported_not_a_crash(tmp_path: Path) -> None:
    """The whole chain recomputed with a genesis whose pre_existing_rows is a list."""
    led = _ledger(tmp_path / "sentinel.db")
    led.close()
    c = sqlite3.connect(tmp_path / "sentinel.db")
    rows = c.execute("SELECT seq, record FROM ledger_chain ORDER BY seq").fetchall()
    prev = lc.ZERO_HASH
    for seq, record in rows:
        if seq == 0:
            rec = json.loads(record)
            rec["data"]["pre_existing_rows"] = [1, 2]
            record = lc.make_record(0, rec["utc"], rec["kind"], rec["table"], rec["key"],
                                    lc.canonical_json(rec["data"]))
        h = lc.entry_hash(prev, record)
        c.execute("UPDATE ledger_chain SET record=?, prev_hash=?, entry_hash=? WHERE seq=?", (record, prev, h, seq))
        prev = h
    c.commit()
    c.close()
    rep = lc.verify_ledger(tmp_path / "sentinel.db", use_local_anchors=False)
    assert not rep.ok and any(f.code == "illegal_entry" and f.position == 0 for f in rep.findings), rep.render_text()


def test_cli_prints_altered_not_a_traceback_for_a_poisoned_entry(tmp_path: Path,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    led = _ledger(tmp_path / "sentinel.db")
    led.close()
    _append_raw(tmp_path / "sentinel.db", ["event"], "events", "999", {"event_id": 999})
    assert lc.main(["verify", "--config", str(_write_toml(tmp_path))]) == 1
    assert "RESULT: ALTERED" in capsys.readouterr().out


# ---------------------------------------------------------------------- --check-files and retention
def _sealed_with_file(tmp: Path) -> tuple[Path, Path, str, Path]:
    path, spool = tmp / "sentinel.db", tmp / "spool"
    f = spool / "CAM01" / "R1" / "seg_000000.ts"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x" * 1000)
    led = Ledger(path)
    led.create_run(run_id="R1", camera_id="CAM01", directory="CAM01/R1", launched_utc="2026-11-09T00:00:00.000000Z",
                   launch_mono=1.0, display_timezone="UTC", app_version="test")
    s = led.insert_segment(camera_id="CAM01", run_id="R1", seq=0, rel_path="CAM01/R1/seg_000000.ts")
    led.transition(s, SegmentState.CLOSED)
    led.seal(s, SegmentState.FINALIZED, {"sha256": hashlib.sha256(b"x" * 1000).hexdigest(), "size_bytes": 1000},
             [], None, [])
    led.close()
    return path, spool, s, f


def _mark_removed(path: Path, s: str, with_retention_record: bool) -> None:
    """What RetentionManager._remove records (or only the segment mark, without the removal record)."""
    led = Ledger(path)
    try:
        with led.tx() as c:
            cur = c.execute("SELECT * FROM segments WHERE segment_id=?", (s,)).fetchone()
            if with_retention_record:
                ins = c.execute("INSERT INTO retention_deletions(segment_id, state, rel_path, sha256, size_bytes, "
                                "framehash_rel_path, archive_copies, reason, free_before, decided_utc) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (s, "DELETING", cur["rel_path"], cur["sha256"], cur["size_bytes"], None, "[]",
                                 "test", 1, "2026-11-12T00:00:00.000000Z"))
                led.chain_insert(c, "retention_deletions", int(ins.lastrowid or 0))
            with led.chained(c, "segments", [s]):
                c.execute("UPDATE segments SET local_deleted_utc=? WHERE segment_id=?",
                          ("2026-11-12T00:00:00.000000Z", s))
    finally:
        led.close()


def test_check_files_reports_originals_removed_by_retention_as_removed(tmp_path: Path) -> None:
    path, spool, s, f = _sealed_with_file(tmp_path)
    _mark_removed(path, s, with_retention_record=True)
    rep = lc.verify_ledger(path, spool_path=spool, check_files=True)
    assert rep.ok and rep.files_checked == 1 and rep.files_removed_by_retention == 0   # not yet deleted: checked
    f.unlink()
    rep = lc.verify_ledger(path, spool_path=spool, check_files=True)
    assert rep.ok, rep.render_text()
    assert rep.files_removed_by_retention == 1 and rep.files_checked == 0
    assert "removed by the retention rule" in rep.render_text() and lc.exit_status(rep) == 0


def test_check_files_still_reports_a_missing_file_without_a_retention_record(tmp_path: Path) -> None:
    path, spool, s, f = _sealed_with_file(tmp_path)
    f.unlink()
    rep = lc.verify_ledger(path, spool_path=spool, check_files=True)
    assert [x.code for x in rep.findings] == ["file_missing"]
    _mark_removed(path, s, with_retention_record=False)        # the segment mark alone is not enough
    rep = lc.verify_ledger(path, spool_path=spool, check_files=True)
    assert "file_missing" in {x.code for x in rep.findings}
