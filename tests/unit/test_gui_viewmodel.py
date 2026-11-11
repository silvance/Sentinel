"""Operator GUI decisions (Qt-free): status mapping, alert banner text, X-001, timeline, exports, PIN, report."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import make_config
from gui_support import base_measurements, populate_ledger, status_doc, write_doc
from sentinel.alerts import CATALOG, Severity, make_alert
from sentinel.clock import iso_utc
from sentinel.db import Ledger
from sentinel.gui import textfmt as tf
from sentinel.gui.ledger_view import (SpanKind, build_timeline, load_exports, load_timeline, open_ledger,
                                      recording_since, snapshot)
from sentinel.gui.report import save_diagnostic_report
from sentinel.gui.viewmodel import (HeartbeatWatch, ReadKind, StatusRead, Tone, build_dashboard,
                                    read_status_robust)
from sentinel.models import OverallStatus, StorageLevel
from sentinel.status import StatusDocument, apply_alerts, write_status

TZ = ZoneInfo("America/New_York")
NOW = 1_762_700_000.0


def fresh(doc: dict) -> StatusRead:
    return StatusRead(doc, None, ReadKind.OK, 0.5)


# Measurements that make the recorder's own evaluate() produce each overall status.
OVERALL_CASES: dict[OverallStatus, dict] = {
    OverallStatus.RECORDING: {},
    OverallStatus.STARTING: dict(media_seen_this_run=False, seconds_since_media_advance=None, now_mono=5.0,
                                 seconds_since_last_seal=None, attempts=1, ever_recorded=False),
    OverallStatus.INTERRUPTED: dict(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                                    in_backoff=True, outage_seconds=40.0, last_end_reason="ffmpeg_error", attempts=3),
    OverallStatus.STORAGE_WARNING: dict(storage_level=StorageLevel.WARNING),
    OverallStatus.STORAGE_FULL: dict(ffmpeg_running=False, media_seen_this_run=False,
                                     seconds_since_media_advance=None, storage_level=StorageLevel.CRITICAL),
    OverallStatus.ERROR: dict(component_errors=["C-001"]),
    OverallStatus.STOPPED: dict(stopping=True),
    OverallStatus.PAUSED: dict(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                               scheduled_pause=True, outage_seconds=7200.0, attempts=3),
    OverallStatus.CAMERA_NOT_SET_UP: dict(ffmpeg_running=False, media_seen_this_run=False,
                                          seconds_since_media_advance=None, camera_pending=True,
                                          outage_seconds=86_400.0),
    OverallStatus.WATCHING: dict(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                                 motion_idle=True, outage_seconds=36_000.0, attempts=3),
}
EXPECTED_HEADLINE: dict[OverallStatus, tuple[str, Tone]] = {
    OverallStatus.RECORDING: ("Recording normally", Tone.OK),
    OverallStatus.STARTING: ("Starting recording", Tone.INFO),
    OverallStatus.INTERRUPTED: ("Recording interrupted", Tone.WARNING),
    OverallStatus.STORAGE_WARNING: ("Storage warning", Tone.WARNING),
    OverallStatus.STORAGE_FULL: ("Recording stopped", Tone.CRITICAL),
    OverallStatus.ERROR: ("Recording stopped", Tone.CRITICAL),     # C-001 is critical + stops recording
    OverallStatus.STOPPED: ("Not recording", Tone.CRITICAL),
    OverallStatus.PAUSED: ("Paused by schedule", Tone.INFO),       # planned: no outage alert, no escalation
    OverallStatus.CAMERA_NOT_SET_UP: ("Not recording: camera not added yet", Tone.WARNING),
    OverallStatus.WATCHING: ("Watching for motion", Tone.INFO),                 # planned: no outage alert
}


def test_every_overall_status_is_covered() -> None:
    assert set(OVERALL_CASES) == set(OverallStatus) == set(EXPECTED_HEADLINE)


@pytest.mark.parametrize("overall", list(OverallStatus))
def test_status_mapping_for_every_overall_status(overall: OverallStatus) -> None:
    doc = status_doc(base_measurements(**OVERALL_CASES[overall]), NOW)
    assert doc["overall"] == str(overall)          # the recorder's own logic produced this state
    v = build_dashboard(fresh(doc), tz=TZ, now=NOW, recording_since_epoch=NOW - 3600)
    assert (v.headline, v.tone) == EXPECTED_HEADLINE[overall]
    assert not v.stale
    assert v.live == (overall in (OverallStatus.RECORDING, OverallStatus.STORAGE_WARNING))
    if overall is OverallStatus.RECORDING:
        assert v.supporting.startswith("Since ") and "No action needed" in v.supporting
        assert v.banner is None and v.notice is None


def test_error_without_stopping_alert_reads_system_error() -> None:
    # Flowing but not sealing: R-005 (critical, stops recording) -> "Recording stopped".
    doc = status_doc(base_measurements(seconds_since_last_seal=10_000.0), NOW)
    assert doc["overall"] == "ERROR"
    assert build_dashboard(fresh(doc), tz=TZ, now=NOW).headline == "Recording stopped"
    # An ERROR whose top alert does not stop recording reads "System error".
    doc2 = dict(doc, alert=make_alert("V-001", Severity.CRITICAL).to_dict(),
                alerts=[make_alert("V-001", Severity.CRITICAL).to_dict()])
    v = build_dashboard(fresh(doc2), tz=TZ, now=NOW)
    assert v.headline == "System error" and v.tone is Tone.CRITICAL


def test_unknown_overall_is_a_system_error() -> None:
    doc = status_doc(base_measurements(), NOW)
    doc["overall"] = "SOMETHING_NEW"
    v = build_dashboard(fresh(doc), tz=TZ, now=NOW)
    assert v.headline == "System error" and v.tone is Tone.CRITICAL


def _doc_with(alert_code: str, severity: Severity, recovering: bool = False) -> tuple[dict, str]:
    base = StatusDocument(overall="INTERRUPTED" if recovering else "RECORDING", camera_link="connected", messages=[],
                          camera_id="CAM01", generated_utc=iso_utc(NOW), heartbeat_epoch=NOW)
    a = make_alert(alert_code, severity, recovering=recovering, attempt=3)
    apply_alerts(base, [a])
    return asdict(base), a.message


@pytest.mark.parametrize("severity", list(Severity))
@pytest.mark.parametrize("code", sorted(CATALOG))
def test_alert_text_is_exactly_alert_message_for_every_code_and_severity(code: str, severity: Severity) -> None:
    doc, message = _doc_with(code, severity, recovering=code.startswith("R-") and severity is not Severity.WARNING)
    v = build_dashboard(fresh(doc), tz=TZ, now=NOW)
    shown = v.banner if doc["banner"] else v.notice
    assert shown is not None and shown.message == message == doc["alert"]["message"]
    assert shown.code == code and shown.code_text == f"Error {code}"
    assert shown.label                                   # words always accompany the icon
    expected_tone = {Severity.NOTICE: Tone.INFO, Severity.WARNING: Tone.WARNING,
                     Severity.CRITICAL: Tone.CRITICAL}[severity]
    assert shown.tone is expected_tone
    if severity >= Severity.WARNING:
        assert v.banner is shown and v.notice is None
        assert v.banner_takes_over == (severity is Severity.CRITICAL)
    else:
        assert v.banner is None and v.supporting == message  # notice explains the state in the status area


def test_r003_escalation_wording() -> None:
    notice, msg_n = _doc_with("R-003", Severity.NOTICE, recovering=True)
    crit, msg_c = _doc_with("R-003", Severity.CRITICAL, recovering=True)
    vn = build_dashboard(fresh(notice), tz=TZ, now=NOW)
    vc = build_dashboard(fresh(crit), tz=TZ, now=NOW)
    assert vn.notice and vn.notice.message == msg_n and "Reconnecting automatically" in msg_n
    assert vc.banner and vc.banner.message == msg_c and vc.banner_takes_over
    assert msg_c.startswith("Recording stopped. Error R-003: Camera connection lost. Automatic recovery unsuccessful.")
    assert vc.headline == "Recording stopped" and vc.banner.offer_report


def test_test_camera_is_always_visible() -> None:
    top = build_dashboard(fresh(status_doc(base_measurements(simulated=True), NOW)), tz=TZ, now=NOW)
    assert top.banner is not None and top.banner.code == "Z-001" and top.test_camera is top.banner
    assert top.headline == "Recording normally" and not top.banner_takes_over
    # Under a more severe alert, Z-001 is still reported separately (never hidden).
    m = base_measurements(simulated=True, storage_level=StorageLevel.CRITICAL, ffmpeg_running=False,
                          media_seen_this_run=False, seconds_since_media_advance=None)
    v = build_dashboard(fresh(status_doc(m, NOW)), tz=TZ, now=NOW)
    assert v.banner is not None and v.banner.code == "S-002"
    assert v.test_camera is not None and v.test_camera.code == "Z-001"
    assert all(a.code != "Z-001" for a in v.other_alerts)


def test_configured_test_camera_is_flagged_even_without_status() -> None:
    v = build_dashboard(StatusRead(None, "missing", ReadKind.MISSING, None), tz=TZ, now=NOW,
                        config_test_camera=True)
    assert v.test_camera is not None and v.test_camera.message == make_alert("Z-001", Severity.WARNING).message
    assert build_dashboard(StatusRead(None, "missing", ReadKind.MISSING, None), tz=TZ, now=NOW).test_camera is None


def test_alert_title_is_catalogue_headline() -> None:
    doc, _ = _doc_with("R-003", Severity.CRITICAL, recovering=True)
    v = build_dashboard(fresh(doc), tz=TZ, now=NOW)
    assert v.banner is not None and v.banner.title == "Camera connection lost"


# ---------------------------------------------------------------------------- X-001
X001 = make_alert("X-001", Severity.CRITICAL).message


def test_stale_heartbeat_is_x001(tmp_path: Path) -> None:
    p = tmp_path / "status.json"
    write_doc(p, status_doc(base_measurements(), time.time(), heartbeat_age_s=120.0))
    r = read_status_robust(p, max_age_s=15.0)
    assert r.kind is ReadKind.STALE
    v = build_dashboard(r, tz=TZ)
    assert v.stale and v.banner is not None and v.banner.code == "X-001"
    assert v.banner.message == X001 and v.banner_takes_over and v.banner.offer_report
    assert v.headline == "System error" and "ago" in v.detail
    assert v.camera.value == "Unknown" and not v.live


def test_heartbeat_in_the_future_is_x001(tmp_path: Path) -> None:
    """Wall clock set backwards while the recorder is dead: negative age must not look fresh."""
    p = tmp_path / "status.json"
    write_doc(p, status_doc(base_measurements(), time.time(), heartbeat_age_s=-3600.0))
    r = read_status_robust(p, max_age_s=15.0)
    assert r.kind is ReadKind.STALE and r.age_s is not None and r.age_s < 0
    v = build_dashboard(r, tz=TZ)
    assert v.banner and v.banner.message == X001 and v.stale
    assert "later than this computer's clock" in v.detail
    # a heartbeat slightly in the future (within the limit) is normal clock jitter: still fresh
    write_doc(p, status_doc(base_measurements(), time.time(), heartbeat_age_s=-2.0))
    assert read_status_robust(p, max_age_s=15.0).kind is ReadKind.OK


def test_malformed_heartbeat_is_x001_not_a_crash(tmp_path: Path) -> None:
    p = tmp_path / "status.json"
    doc = status_doc(base_measurements(), time.time())
    doc["heartbeat_epoch"] = "not a number"
    write_doc(p, doc)
    r = read_status_robust(p, retry_sleep_s=0.0)
    assert r.kind in (ReadKind.UNREADABLE, ReadKind.STALE)
    assert build_dashboard(r, tz=TZ).banner.code == "X-001"


def test_heartbeat_that_stops_changing_is_x001_on_monotonic_clock() -> None:
    """Independent of any wall clock: the value simply stops moving."""
    t = [0.0]
    watch = HeartbeatWatch(15.0, mono=lambda: t[0])
    doc = status_doc(base_measurements(), NOW)              # wall-clock fresh at every read
    assert watch.observe(fresh(doc)).kind is ReadKind.OK
    t[0] = 10.0
    assert watch.observe(fresh(doc)).kind is ReadKind.OK
    t[0] = 16.0
    r = watch.observe(fresh(doc))
    assert r.kind is ReadKind.STALE and r.frozen_s == pytest.approx(16.0)
    v = build_dashboard(r, tz=TZ, now=NOW)
    assert v.banner and v.banner.message == X001 and "has not changed for 16 s" in v.detail
    doc2 = dict(doc, heartbeat_epoch=doc["heartbeat_epoch"] + 2)   # recorder alive again
    t[0] = 17.0
    assert watch.observe(fresh(doc2)).kind is ReadKind.OK
    # a heartbeat that moves BACKWARDS (wall clock stepped back on a live recorder) is still a change
    t[0] = 30.0
    assert watch.observe(fresh(dict(doc, heartbeat_epoch=doc["heartbeat_epoch"] - 3600))).kind is ReadKind.OK


def test_missing_and_unreadable_status_are_x001(tmp_path: Path) -> None:
    p = tmp_path / "status.json"
    r = read_status_robust(p)
    assert r.kind is ReadKind.MISSING
    v = build_dashboard(r, tz=TZ)
    assert v.banner and v.banner.message == X001
    p.write_text("{ not json", "utf-8")
    r = read_status_robust(p, retry_sleep_s=0.0)
    assert r.kind is ReadKind.UNREADABLE
    v = build_dashboard(r, tz=TZ)
    assert v.banner and v.banner.code == "X-001"
    # The raw problem text (which can contain a path) is kept for administrators only.
    assert r.problem and str(tmp_path) not in v.detail and str(tmp_path) not in v.banner.message


def test_stopped_service_then_stale_reads_shut_down(tmp_path: Path) -> None:
    p = tmp_path / "status.json"
    write_doc(p, status_doc(base_measurements(stopping=True), time.time(), heartbeat_age_s=60.0))
    v = build_dashboard(read_status_robust(p), tz=TZ)
    assert v.banner and v.banner.code == "X-001" and "shut down" in v.detail


def test_status_replaced_while_reading_never_flashes_x001(tmp_path: Path) -> None:
    """The recorder replaces status.json atomically every 2 s; reads must never fail meanwhile.
    Here it is replaced every ~5 ms (400x the production rate) while the GUI reads continuously."""
    p = tmp_path / "status.json"
    stop = threading.Event()

    def writer() -> None:
        i = 0
        while not stop.is_set():
            d = StatusDocument(overall="RECORDING", camera_link="connected", messages=[], camera_id="CAM01",
                               generated_utc=iso_utc(time.time()), heartbeat_epoch=time.time(),
                               storage={"pad": "x" * (2000 + i % 7000)})
            write_status(p, d)
            i += 1
            time.sleep(0.005)

    write_status(p, StatusDocument(overall="RECORDING", camera_link="connected", messages=[], camera_id="CAM01",
                                   generated_utc=iso_utc(time.time()), heartbeat_epoch=time.time()))
    t = threading.Thread(target=writer, daemon=True)
    t.start()
    kinds = []
    try:
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            kinds.append(read_status_robust(p).kind)
    finally:
        stop.set()
        t.join(10)
    assert len(kinds) > 50
    assert set(kinds) == {ReadKind.OK}, {k: kinds.count(k) for k in set(kinds)}


# ---------------------------------------------------------------------------- tiles
def test_storage_and_camera_tiles() -> None:
    doc = status_doc(base_measurements(), NOW, free_gb=412, seconds_until_attention=6.2 * 86400,
                     last_segment_end=NOW - 65)
    v = build_dashboard(fresh(doc), tz=TZ, now=NOW)
    assert v.storage.value == "412 GB free" and "about 6 days" in v.storage.detail
    assert v.camera.value == "Connected" and "UTC" in v.camera.detail
    full = status_doc(base_measurements(storage_level=StorageLevel.CRITICAL, ffmpeg_running=False,
                                        media_seen_this_run=False, seconds_since_media_advance=None), NOW,
                      free_gb=4.6, seconds_until_attention=0.0)
    vf = build_dashboard(fresh(full), tz=TZ, now=NOW)
    assert vf.storage.tone is Tone.CRITICAL and vf.camera.value == "Not in use"   # camera not blamed
    unknown = status_doc(base_measurements(), NOW, seconds_until_attention=None)
    assert "being measured" in build_dashboard(fresh(unknown), tz=TZ, now=NOW).storage.detail


def test_drive_tile_is_honest_when_export_missing() -> None:
    v = build_dashboard(fresh(status_doc(base_measurements(), NOW)), tz=TZ, now=NOW, export_available=False)
    assert v.drive.value == "Not available" and "not installed" in v.drive.detail


# ---------------------------------------------------------------------------- timeline
def _seg(a: float, b: float, state: str = "FINALIZED") -> dict:
    return {"start_utc": iso_utc(a), "end_utc": iso_utc(b), "state": state}


def test_timeline_spans_and_plain_reasons() -> None:
    w0, now = NOW - 3600, NOW
    segs = [_seg(w0 + 600, w0 + 900), _seg(w0 + 900, w0 + 1200), _seg(w0 + 1440, w0 + 3300)]
    gaps = [{"start_utc": iso_utc(w0 + 1200), "end_utc": iso_utc(w0 + 1440), "reason": "ffmpeg_error"},
            {"start_utc": iso_utc(w0 + 2000), "end_utc": iso_utc(w0 + 2020), "reason": "source_discontinuity_absorbed"}]
    tl = build_timeline(segs, gaps, w0, now, live=True, live_slack_s=600)
    kinds = [(s.kind, s.reason) for s in tl.spans]
    assert kinds[0] == (SpanKind.NO_INFO, "no record of the system running")
    assert (SpanKind.GAP, "camera disconnected") in kinds
    assert (SpanKind.GAP, "camera video froze") in kinds               # within-segment gap cut out
    assert kinds[-1][0] is SpanKind.LIVE                                # current segment not yet saved
    gap = next(s for s in tl.spans if s.reason == "camera disconnected")
    assert gap.seconds == pytest.approx(240)
    assert tl.recorded_s == pytest.approx(600 - 20 + 1860 - 0 + 300, abs=1)


def test_timeline_open_gap_is_ongoing_and_never_live() -> None:
    w0, now = NOW - 3600, NOW
    segs = [_seg(w0, now - 300)]
    gaps = [{"start_utc": iso_utc(now - 300), "end_utc": None, "reason": "ffmpeg_error"}]
    tl = build_timeline(segs, gaps, w0, now, live=True, live_slack_s=600)
    last = tl.spans[-1]
    assert last.kind is SpanKind.GAP and last.ongoing and last.reason == "camera disconnected"


def test_timeline_from_real_ledger(tmp_path: Path) -> None:
    db = tmp_path / "sentinel.db"
    now = time.time()
    populate_ledger(db, now)
    tl = load_timeline(db, "CAM01", now, live=True)
    reasons = {s.reason for s in tl.interruptions}
    assert {"camera disconnected", "camera video froze",
            "recorder stopped unexpectedly, for example a power cut"} <= reasons
    assert 23 * 3600 < tl.recorded_s < 24 * 3600
    snap = snapshot(db, "CAM01")
    assert snap.available and snap.last_saved_epoch and recording_since(snap)
    assert load_timeline(tmp_path / "missing.db", "CAM01", now).available is False


# ---------------------------------------------------------------------------- read-only ledger
def test_gui_ledger_connection_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "sentinel.db"
    populate_ledger(db, time.time())
    before = db.read_bytes()
    with open_ledger(db) as led:
        assert led is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            led.query("INSERT INTO events(ts_utc, severity, category, code, message) VALUES ('x','x','x','x','x')")
    snapshot(db, "CAM01")
    load_timeline(db, "CAM01", time.time())
    load_exports(db)
    assert db.read_bytes() == before


# ---------------------------------------------------------------------------- exports
def test_exports_none_yet_and_known_table(tmp_path: Path) -> None:
    db = tmp_path / "sentinel.db"
    populate_ledger(db, time.time())
    ex = load_exports(db)
    # Integration: every ledger now has the export tables (db.EXPORT_DDL), empty until an export.
    assert ex.table == "export_batches" and ex.rows == () and ex.available and ex.readable
    led = Ledger(db)
    with led.tx() as c:
        _insert_drive(c, "d1", "SENTINEL_1")
        _insert_batch(c, "b1", "d1", "COMPLETE", finished=iso_utc(NOW))
    led.close()
    ex = load_exports(db)
    assert ex.table == "export_batches" and ex.rows[0].outcome == "Completed" and ex.rows[0].drive == "SENTINEL_1"


# Integration: the export branch's schema is now the real one in every ledger (db.EXPORT_DDL), so
# the tests insert into it instead of creating a reduced copy (which now fails: table exists).
def _insert_drive(c: sqlite3.Connection, drive_id: str, label: str) -> None:
    c.execute("INSERT INTO drives (drive_id, label, identity, first_seen_utc, last_seen_utc) VALUES (?,?,'{}',?,?)",
              (drive_id, label, iso_utc(NOW), iso_utc(NOW)))


def _insert_batch(c: sqlite3.Connection, batch_id: str, drive_id: str, state: str, *, finished: str | None = None,
                  error_code: str | None = None, start: str | None = None, end: str | None = None,
                  planned: str | None = None, updated: str | None = None) -> None:
    c.execute("INSERT INTO export_batches (batch_id, purpose, camera_id, drive_id, state, interval_start_utc, "
              "interval_end_utc, display_timezone, app_version, stamp, planned_utc, finished_utc, error_code, "
              "error_detail, updated_utc) VALUES (?, 'review', 'CAM01', ?, ?, ?, ?, 'UTC', 'test', 'stamp', ?, ?, ?, "
              "'raw detail with C:\\path', ?)",
              (batch_id, drive_id, state, start or iso_utc(NOW - 7200), end or iso_utc(NOW - 3600),
               planned or iso_utc(NOW - 600), finished, error_code, updated or iso_utc(NOW - 30)))


EXPORT_STATES = ["PLANNED", "COPYING", "VERIFYING", "MANIFEST", "EJECTING", "COMPLETE", "FAILED", "INTERRUPTED",
                 "SOMETHING_NEW"]


def test_exports_states_always_shown_in_plain_words(tmp_path: Path) -> None:
    from sentinel.gui.ledger_view import EXPORT_OUTCOMES, OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_UNKNOWN
    db = tmp_path / "sentinel.db"
    led = Ledger(db)
    with led.tx() as c:
        _insert_drive(c, "d1", "SENTINEL_1")
        for i, st in enumerate(EXPORT_STATES):
            _insert_batch(c, f"b{i}", "d1", st, finished=iso_utc(NOW - 60) if st in ("COMPLETE", "FAILED") else None,
                          error_code="E-005" if st == "FAILED" else ("junk; C:\\x" if st == "INTERRUPTED" else None))
    led.close()
    ex = load_exports(db)
    assert ex.table == "export_batches" and len(ex.rows) == len(EXPORT_STATES)
    by_state = dict(zip(reversed(EXPORT_STATES), ex.rows))         # newest first
    for st, row in by_state.items():
        assert row.outcome in EXPORT_OUTCOMES and row.outcome.upper() != st
        assert row.drive == "SENTINEL_1" and row.when_epoch is not None
    assert by_state["COMPLETE"].outcome == OUTCOME_COMPLETED
    assert by_state["FAILED"].outcome == OUTCOME_FAILED and by_state["FAILED"].error_code == "E-005"
    assert by_state["INTERRUPTED"].error_code is None                # malformed code is not shown
    assert by_state["SOMETHING_NEW"].outcome == OUTCOME_UNKNOWN


def test_exports_unknown_schema_is_reported_not_guessed(tmp_path: Path) -> None:
    # Integration: a ledger written by this software always has export_batches now, so the
    # "unknown export records" case is a database from other software (plain sqlite3, no Ledger).
    db = tmp_path / "sentinel.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE export_things (x TEXT)")
    c.commit()
    c.close()
    ex = load_exports(db)
    assert ex.table == "export_things" and not ex.readable


# ---------------------------------------------------------------------------- admin PIN
def test_save_report_never_prunes_or_overwrites_user_files(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.data_path.mkdir(parents=True)
    write_doc(cfg.data_path / "status.json", status_doc(base_measurements(), time.time()))
    dest = tmp_path / "Desktop"
    dest.mkdir()
    mine = [dest / f"Sentinel_Diagnostics_OLD_{i:02d}.zip" for i in range(cfg.service.diagnostics_keep + 5)]
    for f in mine:
        f.write_bytes(b"user file")
    out1 = save_diagnostic_report(cfg, dest)
    out2 = save_diagnostic_report(cfg, dest)
    assert out1.exists() and out2.exists() and out1 != out2
    assert all(f.read_bytes() == b"user file" for f in mine)
    assert not list(dest.glob("*.partial"))
    import zipfile
    with zipfile.ZipFile(out1) as z:
        assert "summary.txt" in z.namelist() and json.loads(z.read("status.json"))["overall"] == "RECORDING"


# ---------------------------------------------------------------------------- formatting
def test_text_formatting() -> None:
    assert tf.duration(52) == "52 s" and tf.duration(240) == "4 min" and tf.duration(3900) == "1 h 05 min"
    assert tf.approx_remaining(6.2 * 86400) == "about 6 days"
    assert tf.approx_remaining(30) == "less than a minute"
    assert tf.size(412e9) == "412 GB" and tf.size(4.6e9) == "4.6 GB" and tf.size(2.5e12) == "2.5 TB"
    assert tf.day_prefix(NOW - 86400, TZ, NOW) == "Yesterday "
    assert tf.hm_utc(0) == "00:00 UTC"


def test_status_doc_helper_is_consistent() -> None:
    doc = status_doc(replace(base_measurements(), simulated=False), NOW)
    assert doc["alert"] is None and doc["banner"] is False and doc["overall"] == "RECORDING"


def test_camera_not_added_with_a_warning_keeps_its_explanation_under_the_headline() -> None:
    """Owner review 2026-11-10 (development PC, no camera, no time server, no archive drive):
    T-002 held the banner, so P-002 appeared again as an "Also:" line, A-001 claimed
    "Recording continues", and the storage tile waited for a measurement that never comes."""
    base = StatusDocument(overall="CAMERA_NOT_SET_UP", camera_link="not_set_up", messages=[], camera_id="CAM01",
                          generated_utc=iso_utc(NOW), heartbeat_epoch=NOW)
    base.storage = {"free_bytes": 810 * 10**9, "level": "ok"}
    p002 = make_alert("P-002", Severity.NOTICE)
    apply_alerts(base, [make_alert("T-002", Severity.WARNING), make_alert("A-001", Severity.WARNING), p002])
    v = build_dashboard(fresh(asdict(base)), tz=TZ, now=NOW)
    assert v.headline == "Not recording: camera not added yet"
    assert v.banner is not None and v.banner.code == "T-002"
    assert v.supporting == p002.message
    assert [a.code for a in v.other_alerts] == ["A-001"]
    assert "Recording continues" not in v.other_alerts[0].message
    assert v.storage.detail == "Recording time left is measured once recording starts."
