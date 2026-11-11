"""Recording-log fingerprints on drives that leave the building (owner decision 2026-11-10: close the
biggest tamper-evidence gap). Archive drives get the chain head at every index write; review exports
carry it in the manifest; ``ledger verify --anchors-from <drive>`` checks the laptop's log against them."""

from __future__ import annotations

import json
from pathlib import Path

from sentinel.archive import layout
from sentinel.db import Ledger
from sentinel.ledgerchain import Anchor, anchors_from, append_anchor, current_chain_head, verify_ledger


def _ledger(tmp: Path) -> Ledger:
    led = Ledger(tmp / "data" / "sentinel.db")
    for i in range(5):
        led.add_event("info", "recorder", f"e{i}", "something happened")
    return led


def test_fingerprints_on_archive_and_review_drives_are_found_and_match(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    head = current_chain_head(led)
    assert head is not None
    arch = tmp_path / "archive_drive"
    append_anchor(arch / layout.chain_heads_path("CAM01"), head, "archive_swap")
    review = tmp_path / "review_drive"
    m = review / "Manifests" / "20261110T1200Z_x" / "Export_Manifest.json"
    m.parent.mkdir(parents=True)
    m.write_text(json.dumps({"recording_log_head": {"seq": head.seq, "hash": head.hash, "utc": head.utc}}), "utf-8")
    a1, n1 = anchors_from(arch)
    a2, n2 = anchors_from(review)
    assert [(a.seq, a.hash) for a in a1 + a2] == [(head.seq, head.hash)] * 2 and not n1 and not n2
    led.close()
    rep = verify_ledger(tmp_path / "data" / "sentinel.db", anchors=a1 + a2, use_local_anchors=False)
    assert rep.ok and rep.anchors_checked == 2, rep.render_text()


def test_a_log_that_no_longer_matches_a_drive_is_reported_altered(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    head = current_chain_head(led)
    led.close()
    forged = Anchor(head.seq, "0" * 64, "on a test archive drive")             # what a rewritten log would face
    rep = verify_ledger(tmp_path / "data" / "sentinel.db", anchors=[forged], use_local_anchors=False)
    assert not rep.ok and "archive drive" in rep.render_text()


def test_a_folder_without_fingerprints_says_so(tmp_path: Path) -> None:
    anchors, notes = anchors_from(tmp_path)
    assert anchors == [] and "no recording-log fingerprints" in notes[0]


def test_archive_copier_writes_the_fingerprint_on_each_index_write(tmp_path: Path) -> None:
    from sentinel.archive.worker import ArchiveWorker

    led = _ledger(tmp_path)
    w = ArchiveWorker.__new__(ArchiveWorker)
    w.ledger, w.camera_id = led, "CAM01"
    drive = tmp_path / "drive"
    w._anchor_on_drive(drive, "attach")
    led.add_event("info", "recorder", "later", "more")
    w._anchor_on_drive(drive, "swap")
    found, _ = anchors_from(drive)
    assert len(found) == 2 and found[-1].seq == current_chain_head(led).seq
    assert "archive_swap" in (drive / layout.chain_heads_path("CAM01")).read_text("ascii")
    led.close()


def test_export_manifest_and_summary_carry_the_fingerprint() -> None:
    from zoneinfo import ZoneInfo

    from sentinel.export.manifest import ManifestBuilder

    batch = {"batch_id": "B1", "purpose": "review", "camera_id": "CAM01", "manifest_generated_utc": None,
             "planned_utc": None, "interval_start_utc": None, "interval_end_utc": None, "display_timezone": "UTC",
             "drive_id": "D1", "app_version": "t", "ffmpeg_version": "t", "operator_id": "x", "plan": None}
    head = {"seq": 41, "hash": "ab" * 32, "utc": "2026-11-10T12:00:00.000000Z"}
    mb = ManifestBuilder(batch, [], {}, {}, ZoneInfo("UTC"), "", head)
    assert json.loads(mb.manifest([]))["recording_log_head"] == head
    html = mb.html()
    html = html.decode("utf-8") if isinstance(html, bytes) else html
    assert "ab" * 32 in html and "fingerprint" in html
