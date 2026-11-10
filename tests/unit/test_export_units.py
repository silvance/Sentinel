"""Unit tests for the review-drive export package (no FFmpeg needed)."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from sentinel.alerts import CATALOG, Severity, make_alert
from sentinel.clock import iso_utc
from sentinel.db import IllegalTransition, Ledger
from sentinel.export import layout
from sentinel.export.fileio import M_WIN_UNBUFFERED, SourceMismatch, TargetIO, open_unbuffered, partial_path
from sentinel.export.ledger import ExportLedger, NewItem
from sentinel.export.media import remux_argv, write_concat_list
from sentinel.export.planner import group_segments, unaccounted
from sentinel.export.target import ExportTarget, TargetError, identify_target

NY = ZoneInfo("America/New_York")


def _ep(y: int, mo: int, d: int, h: int, mi: int = 0, s: float = 0.0) -> float:
    return datetime(y, mo, d, h, mi, int(s), tzinfo=UTC).timestamp() + (s - int(s))


# ------------------------------------------------------------------ layout
def test_tz_labels_are_unambiguous() -> None:
    assert layout.tz_label(layout.to_local(_ep(2026, 11, 9, 12), ZoneInfo("UTC"))) == "UTC"
    assert layout.tz_label(layout.to_local(_ep(2026, 11, 9, 12), NY)) == "EST_UTC-0500"
    assert layout.tz_label(layout.to_local(_ep(2026, 7, 9, 12), NY)) == "EDT_UTC-0400"
    assert layout.tz_label(layout.to_local(_ep(2026, 7, 9, 12), ZoneInfo("Asia/Kolkata"))) == "IST_UTC+0530"
    assert layout.tz_label(layout.to_local(_ep(2026, 7, 9, 12), ZoneInfo("Asia/Dubai"))) == "UTC+0400"


def test_mp4_name_has_local_date_seconds_and_zone() -> None:
    p = layout.mp4_rel_path(_ep(2026, 11, 9, 19, 0, 0.2), _ep(2026, 11, 9, 19, 59, 59.8), NY)
    assert p == "Recordings/2026-11-09/140000-150000_EST_UTC-0500.mp4"
    # local date differs from the UTC date late in the evening
    p2 = layout.mp4_rel_path(_ep(2026, 11, 10, 3, 30), _ep(2026, 11, 10, 3, 40), NY)
    assert p2.startswith("Recordings/2026-11-09/223000-224000_EST")


def test_dst_fall_back_hour_gets_two_distinct_names_and_keys() -> None:
    # 2026-11-01 01:30 local happens twice in New York (EDT, then EST).
    first, second = _ep(2026, 11, 1, 5, 30), _ep(2026, 11, 1, 6, 30)
    assert layout.hour_key(first, NY) != layout.hour_key(second, NY)
    a = layout.mp4_rel_path(first, first + 60, NY)
    b = layout.mp4_rel_path(second, second + 60, NY)
    assert a != b and "EDT_UTC-0400" in a and "EST_UTC-0500" in b
    assert a.split("/")[-1].startswith("013000-013100") and b.split("/")[-1].startswith("013000-013100")


# ------------------------------------------------------------------ grouping
def _seg(sid: str, start: float, dur: float = 300.0, run: str = "R1", seq: int = 0, **kw: Any) -> dict[str, Any]:
    d = {"segment_id": sid, "run_id": run, "seq": seq, "start_utc": iso_utc(start), "end_utc": iso_utc(start + dur),
         "video_codec": "h264", "width": 2592, "height": 1944, "pix_fmt": "yuv420p", "state": "FINALIZED"}
    d.update(kw)
    return d


def _chain(t0: float, n: int, run: str = "R1", dur: float = 300.0, seq0: int = 0) -> list[dict[str, Any]]:
    return [_seg(f"{run}-{i}", t0 + i * dur, dur, run, seq0 + i) for i in range(n)]


def test_contiguous_segments_form_hourly_files() -> None:
    segs = _chain(_ep(2026, 11, 9, 14), 24)            # 14:00-16:00 UTC, 5-minute segments
    groups = group_segments(segs, [], ZoneInfo("UTC"))
    assert [len(g.segments) for g in groups] == [12, 12]
    assert iso_utc(groups[1].start) == iso_utc(_ep(2026, 11, 9, 15))


def test_gap_row_splits_file_even_if_times_touch() -> None:
    segs = _chain(_ep(2026, 11, 9, 14), 6)
    gap = {"kind": "within_segment", "prev_segment_id": "other", "start_utc": segs[2]["end_utc"],
           "end_utc": iso_utc(_ep(2026, 11, 9, 14, 15, 0.4)), "reason": "x"}
    groups = group_segments(segs, [gap], ZoneInfo("UTC"))
    assert [len(g.segments) for g in groups] == [3, 3]
    between = {"kind": "between_runs", "prev_segment_id": "R1-3", "next_segment_id": None,
               "start_utc": None, "end_utc": None, "reason": "x"}
    assert [len(g.segments) for g in group_segments(segs, [between], ZoneInfo("UTC"))] == [4, 2]


def test_short_last_segment_followed_by_gap_stays_in_file() -> None:
    # Regression (seen in integration): a 1-frame final segment, then the graceful-stop gap.
    segs = _chain(_ep(2026, 11, 9, 14), 3, dur=2.0)
    segs.append(_seg("R1-3", _ep(2026, 11, 9, 14, 0, 6.0), 0.033, "R1", 3))
    gap = {"kind": "between_runs", "prev_segment_id": "R1-3", "next_segment_id": None,
           "start_utc": segs[-1]["end_utc"], "end_utc": None, "reason": "graceful_stop"}
    assert [len(g.segments) for g in group_segments(segs, [gap], ZoneInfo("UTC"))] == [4]


@pytest.mark.parametrize("change",["run", "seq", "time", "resolution", "codec"])
def test_non_contiguous_segments_start_new_file(change: str) -> None:
    segs = _chain(_ep(2026, 11, 9, 14), 4)
    s = segs[2]
    if change == "run":
        s["run_id"] = "R2"
    elif change == "seq":
        s["seq"] = 7
    elif change == "time":
        for x in segs[2:]:
            x["start_utc"] = iso_utc(_ep(2026, 11, 9, 14, 10, 2.0))
    elif change == "resolution":
        s["width"] = 1920
    else:
        s["video_codec"] = "hevc"
    groups = group_segments(segs, [], ZoneInfo("UTC"))
    assert len(groups) >= 2 and len(groups[0].segments) == 2


def test_segment_with_internal_gap_is_isolated() -> None:
    segs = _chain(_ep(2026, 11, 9, 14), 5)
    gap = {"kind": "within_segment", "prev_segment_id": "R1-2", "next_segment_id": "R1-2",
           "start_utc": iso_utc(_ep(2026, 11, 9, 14, 11)), "end_utc": iso_utc(_ep(2026, 11, 9, 14, 11, 5)),
           "reason": "timestamp_discontinuity"}
    groups = group_segments(segs, [gap], ZoneInfo("UTC"))
    assert [len(g.segments) for g in groups] == [2, 1, 2]
    assert groups[1].internal_gap


def test_hour_split_follows_display_timezone_offset() -> None:
    # India is UTC+05:30: local hours start at hh:30 UTC.
    segs = _chain(_ep(2026, 11, 9, 14), 12)
    groups = group_segments(segs, [], ZoneInfo("Asia/Kolkata"))
    assert [len(g.segments) for g in groups] == [6, 6]


def test_unaccounted_time() -> None:
    assert unaccounted(0, 100, [(0, 50), (50, 100)]) == []
    assert unaccounted(0, 100, [(0, 40), (60, 100)]) == [(40, 60)]
    assert unaccounted(0, 100, [(10, 20)]) == [(0, 10), (20, 100)]
    assert unaccounted(0, 100, [(0, 49.5), (50, 100)]) == []      # < 1 s ignored (rounding)


# ------------------------------------------------------------------ file I/O
@pytest.mark.parametrize("size", [0, 1, 4095, 4096, 4097, 3 * 1024 * 1024 + 5])
def test_unbuffered_read_returns_exact_bytes(tmp_path: Path, size: int) -> None:
    data = os.urandom(size)
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    chunks, method = open_unbuffered(p)
    assert b"".join(chunks) == data
    if sys.platform == "win32":
        assert method == M_WIN_UNBUFFERED


def test_write_new_never_replaces_and_leaves_no_partial(tmp_path: Path) -> None:
    io = TargetIO()
    dest = tmp_path / "a" / "x.ts"
    sha, n = io.write_new(dest, [b"hello", b" world"])
    assert dest.read_bytes() == b"hello world" and n == 11 and not partial_path(dest).exists()
    with pytest.raises(FileExistsError):
        io.write_new(dest, [b"other"])
    assert dest.read_bytes() == b"hello world" and not partial_path(dest).exists()


def test_write_new_refuses_source_that_does_not_match_sealed_hash(tmp_path: Path) -> None:
    dest = tmp_path / "y.ts"
    with pytest.raises(SourceMismatch):
        TargetIO().write_new(dest, [b"tampered"], expected_sha256="00" * 32)
    assert not dest.exists() and not partial_path(dest).exists()


# ------------------------------------------------------------------ target identity
def test_drive_key_priority_and_subdir() -> None:
    assert ExportTarget(Path("."), {"volume_guid": "{g}", "drive_uuid": "u1"}).drive_key == "drive_uuid:u1"
    assert ExportTarget(Path("."), {"volume_guid": "{g}", "subdir": "a/b"}).drive_key == "volume_guid:{g}|subdir:a/b"
    assert ExportTarget(Path("."), {"serial": "S1", "model": "M"}).drive_key == "serial:S1|model:M"
    with pytest.raises(TargetError):
        _ = ExportTarget(Path("."), {"label": "X"}).drive_key


def test_identify_target_reads_volume_and_marker(tmp_path: Path) -> None:
    (tmp_path / "SENTINEL_DRIVE.json").write_text('{"drive_uuid": "abc-123", "label": "SENTINEL_7"}', "utf-8")
    t = identify_target(tmp_path)
    assert t.identity["drive_uuid"] == "abc-123" and t.drive_key.startswith("drive_uuid:abc-123|subdir:")
    if sys.platform == "win32":
        assert t.identity.get("volume_guid", "").startswith("{")
        assert t.identity.get("filesystem")


# ------------------------------------------------------------------ ledger state machine
def test_export_ledger_state_machine(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.db")
    try:
        xl = ExportLedger(led)
        drive = xl.upsert_drive(ExportTarget(tmp_path, {"drive_uuid": "d1", "label": "L"}))
        bid = xl.create_batch(camera_id="CAM01", drive_id=drive, interval_start_utc="a", interval_end_utc="b",
                              display_timezone="UTC", operator_id=None, app_version="t", ffmpeg_version=None,
                              stamp="s", plan={}, items=[NewItem("original", "Originals/x.ts", expected_sha256="00")])
        with pytest.raises(IllegalTransition):
            xl.set_batch_state(bid, "COMPLETE")
        xl.set_batch_state(bid, "COPYING")
        item = xl.items(bid)[0]
        with pytest.raises(IllegalTransition):
            xl.set_item_state(item["item_id"], "PLANNED")
        xl.set_item_state(item["item_id"], "COPIED")
        for s in ("VERIFYING", "MANIFEST", "EJECTING"):
            xl.set_batch_state(bid, s)
        with pytest.raises(IllegalTransition, match="not verified"):
            xl.set_batch_state(bid, "COMPLETE")
        xl.set_item_state(item["item_id"], "VERIFIED", sha256="00")
        with pytest.raises(IllegalTransition, match="manifest"):
            xl.set_batch_state(bid, "COMPLETE")            # no verified manifest documents yet
        doc = xl.add_item(bid, NewItem("document", "Manifests/m.json"))
        assert xl.add_item(bid, NewItem("document", "Manifests/m.json")) == doc   # idempotent
        xl.set_item_state(doc, "VERIFIED", sha256="11")
        xl.set_batch_state(bid, "COMPLETE")
        with pytest.raises(IllegalTransition):
            xl.set_batch_state(bid, "COPYING")              # terminal
        assert xl.latest_complete_end("CAM01") == "b"
        assert xl.known_sha_on_drive(drive, "Originals/x.ts") == "00"
        assert xl.resumable_batch("CAM01", drive) is None
    finally:
        led.close()


def test_recorder_ledger_still_opens_and_has_export_tables(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.db")
    names = {r["name"] for r in led.query("SELECT name FROM sqlite_master WHERE type='table'")}
    led.close()
    assert {"drives", "export_batches", "export_items", "segments", "recording_gaps"} <= names


# ------------------------------------------------------------------ alerts / media argv
@pytest.mark.parametrize("code", ["E-001", "E-002", "E-003", "E-004", "E-005", "E-006", "E-007"])
def test_export_alerts_plain_language(code: str) -> None:
    a = make_alert(code, Severity.CRITICAL)
    assert a.message.startswith(f"Error {code}: ") and not a.recording_stopped
    for word in ("ffmpeg", "sha", "/", "\\", ".ts", "mp4", "partial"):
        assert word not in a.message.lower(), (code, word)
    assert not CATALOG[code].stops_recording


def test_concat_list_quotes_paths(tmp_path: Path) -> None:
    lst = tmp_path / "l.ffconcat"
    write_concat_list(lst, [tmp_path / "it's here" / "a.ts"])
    text = lst.read_text("utf-8")
    assert text.startswith("ffconcat version 1.0\n") and "it'\\''s here/a.ts'" in text
    argv = remux_argv("ffmpeg", lst, tmp_path / "o.mp4.partial")
    assert argv[argv.index("-movflags") + 1] == "+faststart" and "-n" in argv and argv[argv.index("-c") + 1] == "copy"
