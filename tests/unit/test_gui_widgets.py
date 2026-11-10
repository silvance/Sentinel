"""Operator GUI rendering (PySide6, offscreen): exact banner text, no technical terms, read-only,
keyboard access, administrator gate, 1366x768 layout."""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from gui_support import (SCENARIOS, base_measurements, gui_config_dict, is_test_camera,  # noqa: E402
                         populate_ledger, scenario, status_doc, write_doc)
from sentinel.alerts import CATALOG, Severity, make_alert  # noqa: E402
from sentinel.config import load_config_dict  # noqa: E402
from sentinel.gui import admin_pin  # noqa: E402
from sentinel.gui.window import prepare_qt_environment  # noqa: E402

prepare_qt_environment()

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QWidget  # noqa: E402

from sentinel.gui.theme import DARK, LIGHT  # noqa: E402
from sentinel.gui.window import MainWindow, apply_app_style  # noqa: E402

# Terms that must never appear on operator screens (brief §4, requirement U1/U7).
FORBIDDEN = re.compile(
    r"rtsp|ffmpeg|ffprobe|codec|h\.?264|h\.?265|hevc|mpeg|\.ts\b|sqlite|\.db\b|\.json\b|\.toml\b|tcp://|udp://|"
    r"[A-Za-z]:\\|\\\\|/var/|/etc/|/home/|/tmp/|spool|stderr|exit code|traceback|exception|errno",
    re.IGNORECASE)


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    apply_app_style(a)  # type: ignore[arg-type]
    return a  # type: ignore[return-value]


def make_cfg(tmp: Path, test_camera: bool):  # noqa: ANN201
    return load_config_dict(gui_config_dict(tmp, test_camera), base_dir=tmp)


def open_window(app: QApplication, tmp: Path, name: str | None = None, doc: dict | None = None,
                dark: bool = False, test_camera: bool | None = None, export_available: bool = False) -> MainWindow:
    now = time.time()
    last_end = None
    if name is not None:
        doc, last_end = scenario(name, now)
    if test_camera is None:   # configure the camera the way the status says it is
        test_camera = is_test_camera(name) if name else any(a["code"] == "Z-001" for a in (doc or {}).get("alerts", []))
    cfg = make_cfg(tmp, test_camera)
    if doc is not None:
        write_doc(cfg.data_path / "status.json", doc)
    populate_ledger(cfg.data_path / "sentinel.db", now, end_at=last_end,
                    open_gap_reason=((doc or {}).get("open_gap") or {}).get("reason"))
    win = MainWindow(cfg, DARK if dark else LIGHT, export_available=export_available, auto_refresh=False)
    win.resize(1366, 768)
    win.show()
    app.processEvents()
    return win


def visible_texts(root: QWidget) -> list[str]:
    out: list[str] = []
    for w in root.findChildren(QWidget):
        if not w.isVisibleTo(root):
            continue
        if isinstance(w, QLabel):
            out.append(w.text())
        elif isinstance(w, QAbstractButton):
            out.append(w.text())
        if hasattr(w, "visible_texts"):
            out.extend(w.visible_texts())
        out.append(w.toolTip())
        out.append(w.accessibleName())
        out.append(w.accessibleDescription())
    return [t for t in out if t]


def operator_texts(win: MainWindow, app: QApplication) -> list[str]:
    texts = visible_texts(win.topbar) + visible_texts(win.test_strip) + visible_texts(win.dashboard)
    win.show_timeline()
    app.processEvents()
    win.timeline_page.bar.repaint()
    texts += visible_texts(win.timeline_page)
    win.show_exports()
    app.processEvents()
    texts += visible_texts(win.exports_page)
    win.show_admin()                       # the PIN screen is operator-reachable too
    app.processEvents()
    texts += visible_texts(win.lock_page)
    win.show_dashboard()
    texts.append(win.windowTitle())
    return texts


# ---------------------------------------------------------------------------- banner text
@pytest.mark.parametrize("name", SCENARIOS)
def test_banner_shows_exact_alert_message(app: QApplication, tmp_path: Path, name: str) -> None:
    win = open_window(app, tmp_path, name)
    try:
        v = win.view
        assert v is not None
        doc = win.read.doc if win.read else None
        d = win.dashboard
        if v.stale:
            expected = make_alert("X-001", Severity.CRITICAL).message
            assert d.hero.message.text() == expected and d.hero.message.isVisibleTo(d)
            assert d.hero.chip.text() == "Error X-001"
        elif doc["banner"] and doc["alert"]["code"] == "Z-001":
            # Z-001 is shown by the persistent strip, not repeated in the dashboard band.
            assert win.test_strip.isVisibleTo(win) and win.test_strip.message.text() == doc["alert"]["message"]
            assert not d.band.isVisibleTo(d)
            assert d.hero.headline.text() == "Recording normally"
        elif doc["banner"] and v.banner_takes_over:
            msg = doc["alert"]["message"]
            assert d.hero.message.text() == msg and d.hero.message.isVisibleTo(d)
            assert d.hero.chip.text() == f"Error {doc['alert']['code']}"
            assert d.hero.buttons.primary.isVisibleTo(d) and d.hero.buttons.primary.text() == "Save diagnostic report"
            # No visible repetition: the title is the catalogue headline, not the message's first words,
            # and the small state word is hidden when the message already starts with it.
            title = d.hero.headline.text()
            assert title == CATALOG[doc["alert"]["code"]].headline
            assert not msg.lower().startswith(title.lower())
            if d.hero.label_word.isVisibleTo(d):
                assert not msg.lower().startswith(d.hero.label_word.text().lower())
        elif doc["banner"]:
            assert d.band.isVisibleTo(d) and d.band.message.text() == doc["alert"]["message"]
            assert d.band.word.text()                       # words, not only colour
            assert d.band.icon.isVisibleTo(d)               # and an icon
        else:
            assert d.hero.supporting.text() == doc["alert"]["message"]
            assert d.hero.chip.text() == f"Error {doc['alert']['code']}"
        assert win.test_strip.isVisibleTo(win) == is_test_camera(name)
    finally:
        win.close()


def test_z001_strip_visible_under_more_severe_alert(app: QApplication, tmp_path: Path) -> None:
    from sentinel.models import StorageLevel
    m = base_measurements(simulated=True, storage_level=StorageLevel.CRITICAL, ffmpeg_running=False,
                          media_seen_this_run=False, seconds_since_media_advance=None)
    win = open_window(app, tmp_path, doc=status_doc(m, time.time()))
    try:
        d = win.dashboard
        assert d.hero.chip.text() == "Error S-002"
        s = win.test_strip
        assert s.isVisibleTo(win) and s.alert and s.alert.code == "Z-001"
        assert s.message.text() == make_alert("Z-001", Severity.WARNING).message
    finally:
        win.close()


def _strip_on_screen(win: MainWindow) -> bool:
    s = win.test_strip
    top_left = s.mapTo(win, s.rect().topLeft())
    return (s.isVisibleTo(win) and s.height() > 0 and 0 <= top_left.y() < 200
            and s.message.text() == make_alert("Z-001", Severity.WARNING).message
            and s.chip.text() == "Error Z-001" and "NOT EVIDENCE" in s.word.text())


def test_z001_stays_visible_while_r003_critical_takes_over(app: QApplication, tmp_path: Path) -> None:
    """Safety marker: test footage must never be mistaken for evidence, whatever else is shown."""
    win = open_window(app, tmp_path, "r003_critical_test_camera")
    try:
        d = win.dashboard
        assert win.view is not None and win.view.banner_takes_over and win.view.banner.code == "R-003"
        assert d.hero.chip.text() == "Error R-003"
        assert _strip_on_screen(win)
        win.show_timeline()
        app.processEvents()
        assert _strip_on_screen(win)
        win.show_exports()
        app.processEvents()
        assert _strip_on_screen(win)
        win.show_admin()
        app.processEvents()
        assert _strip_on_screen(win)
    finally:
        win.close()


def test_z001_from_configuration_when_status_unavailable(app: QApplication, tmp_path: Path) -> None:
    """No status at all (X-001): the configured test camera is still flagged."""
    win = open_window(app, tmp_path, doc=None, test_camera=True)
    try:
        assert win.view is not None and win.view.stale and win.dashboard.hero.chip.text() == "Error X-001"
        assert _strip_on_screen(win)
    finally:
        win.close()


def test_no_z001_for_a_production_camera(app: QApplication, tmp_path: Path) -> None:
    win = open_window(app, tmp_path, doc=None, test_camera=False)
    try:
        assert not win.test_strip.isVisibleTo(win)
    finally:
        win.close()


# ---------------------------------------------------------------------------- no technical terms
@pytest.mark.parametrize("name", SCENARIOS)
def test_operator_screens_have_no_technical_terms(app: QApplication, tmp_path: Path, name: str) -> None:
    win = open_window(app, tmp_path, name)
    try:
        texts = operator_texts(win, app)
        assert any("Prepare Review Drive" in t for t in texts)
        bad = [t for t in texts if FORBIDDEN.search(t)]
        assert not bad, bad
    finally:
        win.close()


def test_technical_detail_in_status_never_reaches_operator(app: QApplication, tmp_path: Path) -> None:
    """Paths, URLs and FFmpeg output in detail fields stay on the administrator page."""
    m = base_measurements(storage_error=r"[WinError 5] Access is denied: 'C:\\sentinel\\spool'",
                          ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                          last_end_reason="ffmpeg_error", outage_seconds=900.0)
    doc = status_doc(m, time.time())
    doc["recording"]["note"] = "rtsp://192.0.2.10/stream ffmpeg exited; segment seg_000001.ts codec h264"
    doc["integrity"]["ledger_error"] = "sqlite3.OperationalError: /var/lib/sentinel/data/sentinel.db locked"
    win = open_window(app, tmp_path, doc=doc)
    try:
        bad = [t for t in operator_texts(win, app) if FORBIDDEN.search(t)]
        assert not bad, bad
        assert win.dashboard.hero.chip.text() == "Error S-003"
    finally:
        win.close()


def test_x001_problem_text_with_path_is_not_shown(app: QApplication, tmp_path: Path) -> None:
    cfg_tmp = tmp_path
    win = open_window(app, cfg_tmp, doc=None)      # no status.json at all
    try:
        assert win.read is not None and win.read.problem
        texts = operator_texts(win, app)
        assert not [t for t in texts if FORBIDDEN.search(t)]
        assert win.dashboard.hero.chip.text() == "Error X-001"
    finally:
        win.close()


def test_single_unreadable_read_is_tolerated_once_then_x001(app: QApplication, tmp_path: Path) -> None:
    win = open_window(app, tmp_path, "recording_test_camera")
    try:
        assert win.view is not None and not win.view.stale
        (win.cfg.data_path / "status.json").write_text("{ half-written", "utf-8")
        win.refresh()
        assert not win.view.stale and win.view.headline == "Recording normally"
        win.refresh()
        assert win.view.stale and win.view.banner is not None and win.view.banner.code == "X-001"
    finally:
        win.close()


# ---------------------------------------------------------------------------- read-only
def _digest(paths: list[Path]) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}


def test_gui_never_writes_ledger_or_status(app: QApplication, tmp_path: Path) -> None:
    win = open_window(app, tmp_path, "r003_critical")
    data = win.cfg.data_path
    watched = [data / "sentinel.db", data / "sentinel.db-wal", data / "status.json"]
    before = _digest(watched)
    try:
        for _ in range(3):
            win.refresh()
        operator_texts(win, app)
        win.show_timeline()
        win.show_exports()
        app.processEvents()
        assert _digest(watched) == before
        # Only SQLite's own shared-memory/WAL companions may appear (created by any reader); the GUI
        # creates no other file in the data folder.
        names = {p.name for p in data.iterdir()}
        assert names <= {"sentinel.db", "sentinel.db-wal", "sentinel.db-shm", "status.json"}
    finally:
        win.close()


# ---------------------------------------------------------------------------- buttons, keyboard, layout
def test_exactly_three_routine_buttons_and_no_stop(app: QApplication, tmp_path: Path) -> None:
    win = open_window(app, tmp_path, "recording_test_camera")
    try:
        d = win.dashboard
        assert [b.title for b in (d.btn_prepare, d.btn_timeline, d.btn_exports)] == \
               ["Prepare Review Drive", "View Recording Status", "View Previous Exports"]
        texts = " ".join(visible_texts(d)).lower()
        assert "stop recording" not in texts and "stop" not in [t.lower() for t in visible_texts(d)]
        assert not d.btn_prepare.isEnabled() and "not installed" in d.btn_prepare.description.lower()
    finally:
        win.close()


def test_prepare_review_drive_disabled_even_when_export_is_installed(app: QApplication, tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Until the GUI<->export interface exists, nothing may be launched from the button."""
    import subprocess
    launched: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: launched.append(a))
    cfg = make_cfg(tmp_path, True)
    write_doc(cfg.data_path / "status.json", status_doc(base_measurements(simulated=True), time.time()))
    win = MainWindow(cfg, LIGHT, export_available=True, auto_refresh=False)
    try:
        b = win.dashboard.btn_prepare
        assert not b.isEnabled() and "not yet available" in b.description.lower()
        assert win.view is not None and win.view.drive.value == "Not available here yet"
        b.click()
        QTest.keyClick(b, Qt.Key.Key_Return)
        app.processEvents()
        assert launched == [] and not hasattr(win, "prepare_review_drive")
    finally:
        win.close()


def test_frozen_heartbeat_shows_x001_whatever_the_wall_clock(app: QApplication, tmp_path: Path) -> None:
    t = [0.0]
    cfg = make_cfg(tmp_path, False)
    write_doc(cfg.data_path / "status.json", status_doc(base_measurements(), time.time()))
    win = MainWindow(cfg, LIGHT, export_available=False, auto_refresh=False, mono=lambda: t[0])
    try:
        assert win.view is not None and not win.view.stale
        t[0] = win.max_age_s + 1                       # same heartbeat, monotonic time moved on
        win.refresh()
        assert win.view.stale and win.view.banner is not None and win.view.banner.code == "X-001"
        assert "has not changed" in win.dashboard.hero.detail.text()
    finally:
        win.close()


def test_exports_page_shows_plain_words_not_ledger_states(app: QApplication, tmp_path: Path) -> None:
    from sentinel.clock import iso_utc
    from sentinel.db import Ledger
    win = open_window(app, tmp_path, "r003_notice")
    try:
        led = Ledger(win.cfg.data_path / "sentinel.db")
        # Integration: the real export tables exist in every ledger now (db.EXPORT_DDL); the rows go
        # into them instead of into a reduced stand-in schema (CREATE TABLE would fail).
        now = time.time()
        with led.tx() as c:
            c.execute("INSERT INTO drives (drive_id, label, identity, first_seen_utc, last_seen_utc) "
                      "VALUES ('d1', 'SENTINEL_1', '{}', ?, ?)", (iso_utc(now), iso_utc(now)))
            for i, st in enumerate(("COMPLETE", "FAILED", "INTERRUPTED", "COPYING", "MYSTERY_STATE")):
                c.execute("INSERT INTO export_batches (batch_id, purpose, camera_id, drive_id, state, planned_utc, "
                          "finished_utc, interval_start_utc, interval_end_utc, error_code, display_timezone, "
                          "app_version, stamp, updated_utc) "
                          "VALUES (?, 'review', 'CAM01', 'd1', ?, ?, ?, ?, ?, ?, 'UTC', 'test', 'stamp', ?)",
                          (f"b{i}", st, iso_utc(now - 900), iso_utc(now - 60), iso_utc(now - 7200),
                           iso_utc(now - 3600), "E-005" if st == "FAILED" else None, iso_utc(now - 30)))
        led.close()
        win.show_exports()
        app.processEvents()
        texts = visible_texts(win.exports_page)
        joined = " | ".join(texts)
        for raw in ("COMPLETE", "FAILED", "INTERRUPTED", "COPYING", "MYSTERY_STATE", "_"):
            assert raw not in joined.replace("SENTINEL_1", ""), (raw, joined)
        assert "Completed" in joined and "Error E-005" in joined and "Drive SENTINEL_1" in joined
        assert not [t for t in texts if FORBIDDEN.search(t)]
    finally:
        win.close()


def test_keyboard_navigation(app: QApplication, tmp_path: Path) -> None:
    win = open_window(app, tmp_path, "recording_test_camera")
    try:
        win.activateWindow()
        win.dashboard.btn_timeline.setFocus()
        QTest.keyClick(win.dashboard.btn_timeline, Qt.Key.Key_Return)
        app.processEvents()
        QTest.qWait(250)
        assert win.stack.currentWidget() is win.timeline_page
        QTest.keyClick(win.timeline_page, Qt.Key.Key_Escape)
        app.processEvents()
        assert win.stack.currentWidget() is win.dashboard
        for b in (win.dashboard.btn_timeline, win.dashboard.btn_exports, win.dashboard.btn_admin):
            assert b.focusPolicy() & Qt.FocusPolicy.TabFocus
    finally:
        win.close()


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("export_available", [False, True])
def test_layout_fits_1366x768(app: QApplication, tmp_path: Path, name: str, dark: bool,
                              export_available: bool) -> None:
    """At 1366x768 the status area and all three routine buttons are fully visible without scrolling
    (both disabled-button wordings, light and dark)."""
    win = open_window(app, tmp_path, name, dark=dark, export_available=export_available)
    try:
        d = win.dashboard
        for w in (d.hero, d.btn_prepare, d.btn_timeline, d.btn_exports, d.appearance, d.btn_admin):
            r = w.rect()
            bottom_right = w.mapTo(win, r.bottomRight())
            assert w.isVisibleTo(win)
            assert bottom_right.y() <= win.height() and bottom_right.x() <= win.width(), (w, bottom_right)
        from PySide6.QtWidgets import QScrollArea
        sa = d.findChildren(QScrollArea)[0]
        assert not sa.verticalScrollBar().isVisible(), "overview needs scrolling at 1366x768"
        assert d.hero.headline.font().pixelSize() >= 30
        assert min(lbl.font().pixelSize() for lbl in d.findChildren(QLabel) if lbl.isVisibleTo(d)) >= 14
    finally:
        win.close()


# ---------------------------------------------------------------------------- timeline sentences
def test_timeline_sentences_are_plain_words() -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from sentinel.gui.ledger_view import Span, SpanKind
    from sentinel.gui.widgets import span_sentence
    t = datetime(2026, 11, 9, 14, 5, tzinfo=UTC).timestamp()
    now = t + 3 * 3600
    title, detail = span_sentence(Span(t, t + 240, SpanKind.GAP, "camera disconnected"), ZoneInfo("UTC"), now)
    assert title == "No recording 14:05 – 14:09"
    assert detail.startswith("Camera disconnected · 4 min")
    title, _ = span_sentence(Span(t, t + 22, SpanKind.GAP, "camera video froze"), ZoneInfo("UTC"), now)
    assert title == "No recording 14:05:00 – 14:05:22"
    title, detail = span_sentence(Span(t, now, SpanKind.GAP, "camera disconnected", ongoing=True),
                                  ZoneInfo("UTC"), now)
    assert title == "No recording since 14:05" and "so far" in detail


# ---------------------------------------------------------------------------- administrator area
def test_admin_area_requires_pin(app: QApplication, tmp_path: Path) -> None:
    win = open_window(app, tmp_path, "recording_test_camera")
    try:
        win.show_admin()
        assert win.stack.currentWidget() is win.lock_page and not win.lock_page.pin.isEnabled()
        assert not win.admin_unlocked()
        admin_pin.set_pin(win.cfg.data_path, "482915")
        win.show_admin()
        assert win.lock_page.pin.isEnabled()
        win.lock_page.pin.setText("000000")
        win.lock_page.btn.click()
        assert not win.admin_unlocked() and "not correct" in win.lock_page.message.text()
        win.lock_page.pin.setText("482915")
        win.lock_page.btn.click()
        assert win.admin_unlocked()
        summary = win.admin_page.summary.toPlainText()
        assert str(win.cfg.source_path or "(none)") in summary and "Open gaps" in summary
        assert '"overall"' in win.admin_page.raw.toPlainText()
        assert win.admin_page.events.rowCount() > 0
        win.show_dashboard()                      # leaving locks it again
        win.show_admin()
        assert win.stack.currentWidget() is win.lock_page
    finally:
        win.close()


def test_close_stops_only_the_window_timer(app: QApplication, tmp_path: Path) -> None:
    cfg_win = open_window(app, tmp_path, "recording_test_camera")
    cfg_win.timer.start()
    assert cfg_win.timer.isActive()
    cfg_win.close()
    assert not cfg_win.timer.isActive()
