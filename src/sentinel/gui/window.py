"""Main window: reads measured state every 2 s and renders it. Holds no handle to the recorder."""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import QRectF, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import (QCloseEvent, QColor, QFont, QGuiApplication, QKeySequence, QPainter, QPaintEvent,
                           QPen, QShortcut)
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout,
                               QWidget)

from .. import __version__
from ..config import AppConfig
from . import textfmt as tf
from .admin_pin import AdminGate
from .ledger_view import (load_admin, load_exports, load_timeline, pretty_json, recording_since, snapshot)
from .pages import AdminLockPage, AdminPage, DashboardPage, ExportsPage, TimelinePage, band_alert
from .report import ReportError, save_diagnostic_report
from .prefs import load_appearance, save_appearance
from .theme import APPEARANCES, BODY_PX, CAPTION_PX, Palette, font, pick_palette, stylesheet
from .viewmodel import (DashboardView, HeartbeatWatch, ReadKind, StatusRead, build_dashboard,
                        read_status_robust)
from .widgets import TestCameraStrip, label

W = QFont.Weight
REFRESH_MS = 2000
TIMELINE_EVERY = 5          # refresh the 24 h timeline every 5th tick (10 s) while it is open
PREPARE_DISABLED_TEXT = {
    False: "Not installed in this version.",
    True: "Not yet available from this screen. Contact technical support.",
}


def export_installed() -> bool:
    """True only if a real ``sentinel.export`` module is importable. Never faked."""
    try:
        return importlib.util.find_spec("sentinel.export") is not None
    except (ImportError, ValueError):
        return False


def zone_words(tz_name: str) -> str:
    if tz_name.upper() in ("UTC", "ETC/UTC", "Z"):
        return "UTC"
    return tz_name.rsplit("/", 1)[-1].replace("_", " ")


class BrandMark(QWidget):
    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        self.setFixedSize(28, 28)

    def paintEvent(self, _e: QPaintEvent) -> None:  # noqa: N802
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setPen(QPen(QColor(self.p.accent), 2.6))
        qp.drawEllipse(QRectF(2, 2, 24, 24))
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(QColor(self.p.accent))
        qp.drawEllipse(QRectF(9, 9, 10, 10))
        qp.end()


class TopBar(QWidget):
    def __init__(self, palette: Palette, camera_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("topbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(68)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(28, 0, 28, 0)
        lay.setSpacing(12)
        lay.addWidget(BrandMark(palette))
        lay.addWidget(label("Sentinel", 21, W.DemiBold, wrap=False))
        sep = label("|", 21, W.Light, palette.border, wrap=False)
        lay.addWidget(sep)
        lay.addWidget(label(camera_text, BODY_PX + 1, W.Normal, palette.text2, wrap=False))
        lay.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.setContentsMargins(0, 10, 0, 10)
        self.clock = label("", 21, W.DemiBold, wrap=False)
        self.clock.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.updated = label("", CAPTION_PX - 1, color=palette.text3, wrap=False)
        self.updated.setAlignment(Qt.AlignmentFlag.AlignRight)
        col.addWidget(self.clock)
        col.addWidget(self.updated)
        lay.addLayout(col)

    def apply(self, now: float, tz: ZoneInfo, updated: str) -> None:
        dt = tf.local_dt(now, tz)
        self.clock.setText(f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}  {dt.strftime('%H:%M')}")
        self.updated.setText(updated)


class MainWindow(QMainWindow):
    _report_done = Signal(str, bool)

    def __init__(self, cfg: AppConfig, palette: Palette | None = None, *, export_available: bool | None = None,
                 clock: Callable[[], float] = time.time, auto_refresh: bool = True,
                 mono: Callable[[], float] = time.monotonic, appearance: str | None = None,
                 prefs_path: Path | None = None) -> None:
        """``palette`` fixes the theme (tests, screenshots). Otherwise ``appearance`` ("auto" =
        Follow Windows, "light", "dark") is used, or the user's remembered preference."""
        super().__init__()
        self.cfg = cfg
        self.clock = clock
        self.tz = ZoneInfo(cfg.recording.display_timezone)
        self.status_path = cfg.data_path / "status.json"
        self.db_path = cfg.data_path / "sentinel.db"
        self.max_age_s = max(15.0, 5 * cfg.service.status_interval_s)
        self.export_available = export_installed() if export_available is None else export_available
        self.gate = AdminGate(cfg.data_path)
        self.view: DashboardView | None = None
        self.read: StatusRead | None = None
        self._tick = 0
        self._report_busy = False
        self.last_report_path: str | None = None
        self._last_good: StatusRead | None = None
        self._unreadable_streak = 0
        self.heartbeat_watch = HeartbeatWatch(self.max_age_s, mono)
        self.prefs_path = prefs_path
        if appearance in APPEARANCES:
            self.appearance = appearance
        elif palette is not None:
            self.appearance = "dark" if palette.dark else "light"
        else:
            self.appearance = load_appearance(prefs_path)
        palette = palette or pick_palette(self.appearance)

        self.cam_text = f"{cfg.camera.name} ({cfg.camera.id})" if cfg.camera.name else f"Camera {cfg.camera.id}"
        self.setWindowTitle(f"Sentinel — {self.cam_text}")
        self.setMinimumSize(1024, 640)
        self._build_ui(palette)

        self._report_done.connect(self._on_report_done)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.show_dashboard)
        QShortcut(QKeySequence("F5"), self, activated=self.refresh)
        try:   # Follow Windows: re-theme live when the operating system switches light/dark
            QGuiApplication.styleHints().colorSchemeChanged.connect(self._system_scheme_changed)
        except AttributeError:   # Qt < 6.5
            pass

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.refresh()
        if auto_refresh:
            self.timer.start()

    def _build_ui(self, palette: Palette) -> None:
        """(Re)create every widget for ``palette``. Called at start and when the appearance changes."""
        cfg = self.cfg
        self.p = palette
        self.setStyleSheet(stylesheet(palette))
        root = QWidget()
        root.setObjectName("root")
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.topbar = TopBar(palette, self.cam_text)
        v.addWidget(self.topbar)
        self.test_strip = TestCameraStrip(palette)      # Z-001: outside the page stack, always visible
        v.addWidget(self.test_strip)
        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)
        self.setCentralWidget(root)                     # deletes the previous widget tree

        now = self.clock()
        tz_text = (f"Times shown in {zone_words(cfg.recording.display_timezone)} time "
                   f"({tf.utc_offset_text(self.tz, now)})")
        self.dashboard = DashboardPage(palette, tz_text, self.appearance)
        self.timeline_page = TimelinePage(palette)
        self.exports_page = ExportsPage(palette)
        self.lock_page = AdminLockPage(palette)
        self.admin_page = AdminPage(palette)
        for w in (self.dashboard, self.timeline_page, self.exports_page, self.lock_page, self.admin_page):
            self.stack.addWidget(w)

        # Prepare Review Drive stays DISABLED in this build, also when ``sentinel.export`` is installed:
        # the GUI<->export interface (who selects the drive, how the request is made, how progress is
        # shown) is not defined yet, and launching the export command line from here cannot work
        # (it needs a target drive, prints only to a console, and a frozen build has no ``python -m``).
        # See docs/branches/req-operator-gui.md "Prepare Review Drive: intended interface".
        self.dashboard.set_prepare_available(False, PREPARE_DISABLED_TEXT[self.export_available])

        d = self.dashboard
        d.timeline.connect(self.show_timeline)
        d.exports.connect(self.show_exports)
        d.admin.connect(self.show_admin)
        d.appearance_selected.connect(self.set_appearance)
        d.save_report.connect(lambda: self.save_report(None))
        d.save_report_elsewhere.connect(self.save_report_elsewhere)
        self.timeline_page.back.connect(self.show_dashboard)
        self.exports_page.back.connect(self.show_dashboard)
        self.lock_page.back.connect(self.show_dashboard)
        self.lock_page.unlock.connect(self._try_unlock)
        self.admin_page.lock.connect(self.show_dashboard)
        self.admin_page.refresh_requested.connect(self.refresh_admin)
        self.admin_page.save_report.connect(lambda: self.save_report(None))

    # ------------------------------------------------------------------ appearance
    def set_appearance(self, mode: str) -> None:
        """Operator's display choice: remembered in their own profile, applied immediately.
        Purely visual; it never changes whether the window or its taskbar entry is visible."""
        if mode not in APPEARANCES:
            return
        self.appearance = mode
        save_appearance(mode, self.prefs_path)          # best effort; failure only means "not remembered"
        self._retheme(pick_palette(mode))

    def _system_scheme_changed(self, *_: object) -> None:
        if self.appearance == "auto":
            self._retheme(pick_palette("auto"))

    def _retheme(self, palette: Palette) -> None:
        if palette == self.p:
            self.dashboard.set_appearance_value(self.appearance)
            return
        current = self.stack.currentWidget()
        page = ("timeline" if current is self.timeline_page else
                "exports" if current is self.exports_page else "dashboard")   # admin re-locks
        self._build_ui(palette)
        self.refresh()
        if page == "timeline":
            self.show_timeline()
        elif page == "exports":
            self.show_exports()
        else:
            self.stack.setCurrentWidget(self.dashboard)
            self.dashboard.appearance.setFocus()

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> None:
        """Re-read status.json and the ledger (read-only) and re-render. Never raises."""
        self._tick += 1
        now = self.clock()
        test_cam = self.cfg.camera.development_source     # the recorder's own Z-001 condition
        try:
            read = self.heartbeat_watch.observe(read_status_robust(self.status_path, self.max_age_s))
            # One unreadable read (file being replaced at that instant, after in-call retries) is
            # tolerated once by keeping the previous good reading; two in a row (>= 2 s) is X-001.
            if read.kind is ReadKind.UNREADABLE and self._last_good is not None and self._unreadable_streak == 0:
                self._unreadable_streak = 1
                read = self._last_good
            elif read.kind is ReadKind.UNREADABLE:
                self._unreadable_streak += 1
            else:
                self._unreadable_streak = 0
            if read.kind is ReadKind.OK:
                self._last_good = read
            self.read = read
            snap = snapshot(self.db_path, self.cfg.camera.id)
            self.view = build_dashboard(self.read, tz=self.tz, now=now, last_saved_epoch=snap.last_saved_epoch,
                                        recording_since_epoch=recording_since(snap) if snap.available else None,
                                        export_available=self.export_available, config_test_camera=test_cam)
        except Exception as exc:  # a GUI bug must never look like "all is well": show X-001
            self.read = StatusRead(None, f"GUI error: {exc!r}", ReadKind.UNREADABLE, None)
            self.view = build_dashboard(self.read, tz=self.tz, now=now, export_available=self.export_available,
                                        config_test_camera=test_cam)
        self.dashboard.apply(self.view)
        self.test_strip.apply(self.view.test_camera)
        self.topbar.apply(now, self.tz, self.view.updated)
        current = self.stack.currentWidget()
        if current is self.timeline_page and self._tick % TIMELINE_EVERY == 0:
            self._refresh_timeline()
        elif current is self.timeline_page:
            self.timeline_page.band.apply(band_alert(self.view))
        elif current is self.exports_page:
            self.exports_page.band.apply(band_alert(self.view))

    def _refresh_timeline(self) -> None:
        assert self.view is not None
        now = self.clock()
        slack = self.cfg.recording.segment_seconds * 1.5 + 120.0
        tl = load_timeline(self.db_path, self.cfg.camera.id, now, live=self.view.live, live_slack_s=slack)
        self.timeline_page.apply(tl, self.tz, band_alert(self.view), now)

    # ------------------------------------------------------------------ navigation
    def show_dashboard(self) -> None:
        self.stack.setCurrentWidget(self.dashboard)
        self.dashboard.btn_timeline.setFocus()

    def show_timeline(self) -> None:
        self._refresh_timeline()
        self.stack.setCurrentWidget(self.timeline_page)
        self.timeline_page.header.back_btn.setFocus()

    def show_exports(self) -> None:
        assert self.view is not None
        self.exports_page.apply(load_exports(self.db_path), self.tz, self.export_available, band_alert(self.view),
                                self.clock())
        self.stack.setCurrentWidget(self.exports_page)
        self.exports_page.header.back_btn.setFocus()

    def show_admin(self) -> None:
        self.lock_page.reset(self.gate.configured())
        self.stack.setCurrentWidget(self.lock_page)
        self.lock_page.pin.setFocus()

    def _try_unlock(self, pin: str) -> None:
        wait = self.gate.seconds_locked()
        if wait > 0:
            self.lock_page.message.setText(f"Too many attempts. Try again in {int(wait) + 1} seconds.")
            return
        if self.gate.check(pin):
            self.refresh_admin()
            self.stack.setCurrentWidget(self.admin_page)
            self.admin_page.tabs.setFocus()
        else:
            wait = self.gate.seconds_locked()
            self.lock_page.message.setText(f"Too many attempts. Try again in {int(wait) + 1} seconds." if wait > 0
                                           else "That PIN is not correct.")

    def admin_unlocked(self) -> bool:
        return self.stack.currentWidget() is self.admin_page

    def refresh_admin(self) -> None:
        cfg = self.cfg
        read = read_status_robust(self.status_path, self.max_age_s)
        data = load_admin(self.db_path, cfg.camera.id)
        doc = read.doc or {}
        lines = [
            f"Sentinel GUI version   {__version__}",
            f"Configuration file     {cfg.source_path or '(none)'}",
            f"Data folder            {cfg.data_path}",
            f"Recording folder       {cfg.spool_path}",
            f"Log folder             {cfg.log_path}",
            f"Status file            {self.status_path}",
            f"Ledger                 {self.db_path}  (opened read-only)",
            f"Display timezone       {cfg.recording.display_timezone}",
            "",
            f"Status read            {read.kind}" + (f"  (heartbeat age {read.age_s:.1f} s)" if read.age_s is not None
                                                     else ""),
            f"Problem                {read.problem or 'none'}",
            f"Overall                {doc.get('overall', 'unknown')}",
            f"Camera link            {doc.get('camera_link', 'unknown')}",
            f"Status generated (UTC) {doc.get('generated_utc', 'never')}",
            "",
            "Segments by state      " + (pretty_json(data.state_counts) if data.available else "unavailable"),
            "Integrity counters     " + pretty_json(doc.get("integrity", {})),
            "Open gaps              " + (pretty_json(data.open_gaps) if data.open_gaps else "none"),
            "Recording (live)       " + pretty_json(doc.get("recording", {})),
            "Storage                " + pretty_json(doc.get("storage", {})),
        ]
        if data.error:
            lines.append(f"Ledger error           {data.error}")
        self.admin_page.apply(lines, pretty_json(read.doc) if read.doc else "(no status document)", data)
        self.admin_page.feedback.setText(f"Refreshed {tf.hm(self.clock(), self.tz, seconds=True)}")

    # ------------------------------------------------------------------ actions
    def desktop_dir(self) -> Path:
        d = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        p = Path(d) if d else Path.home()
        return p if p.is_dir() else Path.home()

    def save_report_elsewhere(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose where to save the diagnostic report",
                                             str(self.desktop_dir()))
        if d:
            self.save_report(Path(d))

    def save_report(self, dest: Path | None) -> None:
        if self._report_busy:
            return
        self._report_busy = True
        on_desktop = dest is None
        target = self.desktop_dir() if dest is None else dest
        self._set_report_feedback("Saving the diagnostic report\u2026")

        def work() -> None:
            try:
                path = save_diagnostic_report(self.cfg, target)
                where = "on the Desktop" if on_desktop else "in the folder you chose"
                self._report_done.emit(f"Report saved {where}. Its name starts with \u201cSentinel_Diagnostics\u201d. "
                                       f"Send it to technical support.|{path}", True)
            except ReportError as exc:
                self._report_done.emit(str(exc), False)
            except Exception:  # never crash the GUI thread over a report
                self._report_done.emit("The report could not be saved. Try another location.", False)

        threading.Thread(target=work, name="gui-report", daemon=True).start()

    def _on_report_done(self, text: str, ok: bool) -> None:
        self._report_busy = False
        msg, _, path = text.partition("|")
        self.last_report_path = path if ok else None
        self._set_report_feedback(msg)
        self.admin_page.feedback.setText(msg + (f"  ({path})" if ok else ""))

    def _set_report_feedback(self, text: str) -> None:
        self.dashboard.set_report_feedback(text)

    # ------------------------------------------------------------------ lifecycle
    def closeEvent(self, e: QCloseEvent) -> None:  # noqa: N802
        """Closing only stops this window's timer. The recorder is a separate program and is
        not contacted, signalled or waited for (requirement U6)."""
        self.timer.stop()
        e.accept()


def prepare_qt_environment() -> None:
    """Offscreen rendering on Windows has no font database unless pointed at the system fonts."""
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and sys.platform == "win32" \
            and not os.environ.get("QT_QPA_FONTDIR"):
        os.environ["QT_QPA_FONTDIR"] = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")


def apply_app_style(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(font(BODY_PX))
