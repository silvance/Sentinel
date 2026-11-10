"""Pages of the operator GUI: overview, recording status, previous exports, administrator."""

from __future__ import annotations

import sys
import time
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLineEdit, QPlainTextEdit,
                               QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

from . import textfmt as tf
from .icons import TONE_ICON, Icon, IconWidget
from .ledger_view import OUTCOME_COMPLETED, AdminData, ExportList, SpanKind, Timeline
from .theme import (APPEARANCE_LABELS, APPEARANCES, BODY_PX, CAPTION_PX, SMALL_PX, TITLE_PX, VALUE_PX, Palette,
                    font)
from .viewmodel import AlertView, DashboardView
from .widgets import (ActionButton, AlertBand, InfoTile, LegendItem, SpanRow, StatusHero, TimelineBar, card,
                      clear_layout, label)

W = QFont.Weight
MARGIN = 28


def scroll(content: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setWidget(content)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return sa


class PageHeader(QWidget):
    back = Signal()

    def __init__(self, palette: Palette, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        self.back_btn = QPushButton("‹  Back to overview")
        self.back_btn.setObjectName("link")
        self.back_btn.setToolTip("Back to the overview (Esc)")
        self.back_btn.clicked.connect(self.back)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = label(title, TITLE_PX, W.DemiBold, name="pageTitle")
        col.addWidget(self.title)
        self.subtitle = label(subtitle, CAPTION_PX, name="secondary")
        self.subtitle.setVisible(bool(subtitle))
        col.addWidget(self.subtitle)
        lay.addLayout(col, 1)
        lay.addWidget(self.back_btn, 0, Qt.AlignmentFlag.AlignTop)


# --------------------------------------------------------------------------- overview
class DashboardPage(QWidget):
    timeline = Signal()
    exports = Signal()
    admin = Signal()
    save_report = Signal()
    save_report_elsewhere = Signal()
    appearance_selected = Signal(str)
    lock_computer = Signal()

    def __init__(self, palette: Palette, tz_text: str, appearance: str = "auto",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        content = QWidget()
        content.setObjectName("page")
        lay = QVBoxLayout(content)
        lay.setContentsMargins(MARGIN, 18, MARGIN, 14)
        lay.setSpacing(14)

        self.band = AlertBand(palette)
        self.band.save_report.connect(self.save_report)
        self.band.save_report_elsewhere.connect(self.save_report_elsewhere)
        self.hero = StatusHero(palette)
        self.hero.save_report.connect(self.save_report)
        self.hero.save_report_elsewhere.connect(self.save_report_elsewhere)
        self.others = QVBoxLayout()
        self.others.setSpacing(6)
        lay.addWidget(self.band)
        lay.addWidget(self.hero)
        lay.addLayout(self.others)

        tiles = QHBoxLayout()
        tiles.setSpacing(16)
        self.camera = InfoTile(palette, Icon.CAMERA)
        self.storage = InfoTile(palette, Icon.STORAGE)
        self.drive = InfoTile(palette, Icon.DRIVE)
        for t in (self.camera, self.storage, self.drive):
            tiles.addWidget(t, 1)
        lay.addLayout(tiles)
        # The three routine actions stay anchored at the bottom, in the same place whatever the
        # status area above shows, so they are always found in the same spot.
        lay.addStretch(1)

        actions = QHBoxLayout()
        actions.setSpacing(16)
        self.btn_prepare = ActionButton(palette, Icon.PREPARE, "Prepare Review Drive",
                                        "Copy recordings to a review drive.")
        self.btn_timeline = ActionButton(palette, Icon.TIMELINE, "View Recording Status",
                                         "What was recorded in the last 24 hours.")
        self.btn_exports = ActionButton(palette, Icon.HISTORY, "View Previous Exports",
                                        "Review drives prepared before.")
        self.btn_timeline.clicked.connect(self.timeline)
        self.btn_exports.clicked.connect(self.exports)
        for b in (self.btn_prepare, self.btn_timeline, self.btn_exports):
            actions.addWidget(b, 1)
        lay.addLayout(actions)

        foot = QHBoxLayout()
        self.tz_label = label(tz_text, CAPTION_PX, wrap=False, name="tertiary")
        foot.addWidget(self.tz_label)
        foot.addStretch(1)
        # Display preference: plainly labelled, on the main screen, keyboard reachable.
        disp = label("Display", CAPTION_PX, W.DemiBold, wrap=False, name="secondary")
        foot.addWidget(disp)
        self.appearance = QComboBox()
        for mode in APPEARANCES:
            self.appearance.addItem(APPEARANCE_LABELS[mode], mode)
        self.appearance.setAccessibleName("Display")
        self.appearance.setToolTip("Light, Dark, or follow the Windows setting. Remembered for you.")
        disp.setBuddy(self.appearance)
        self.set_appearance_value(appearance)
        self.appearance.activated.connect(
            lambda i: self.appearance_selected.emit(str(self.appearance.itemData(i))))
        foot.addWidget(self.appearance)
        foot.addSpacing(12)
        # Leaving the computer unattended (e.g. in a closet): lock Windows; the recorder is a service
        # and keeps recording while the computer is locked (sentinel.power).
        self.btn_lock = QPushButton("Lock this computer")
        self.btn_lock.setObjectName("link")
        self.btn_lock.setToolTip("Locks Windows, like Windows key + L. Recording continues while the computer is "
                                 "locked; the Windows password is needed to see this screen again.")
        self.btn_lock.setAccessibleDescription("Recording continues while the computer is locked.")
        self.btn_lock.clicked.connect(self.lock_computer)
        self.btn_lock.setVisible(sys.platform == "win32")
        foot.addWidget(self.btn_lock)
        foot.addSpacing(12)
        self.btn_admin = QPushButton("Administrator")
        self.btn_admin.setObjectName("link")
        self.btn_admin.setToolTip("Diagnostics for administrators (PIN required)")
        self.btn_admin.clicked.connect(self.admin)
        foot.addWidget(self.btn_admin)
        lay.addLayout(foot)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll(content))
        self.setTabOrder(self.btn_prepare, self.btn_timeline)
        self.setTabOrder(self.btn_timeline, self.btn_exports)
        self.setTabOrder(self.btn_exports, self.appearance)
        self.setTabOrder(self.appearance, self.btn_lock)
        self.setTabOrder(self.btn_lock, self.btn_admin)

    def set_appearance_value(self, mode: str) -> None:
        i = self.appearance.findData(mode)
        self.appearance.setCurrentIndex(i if i >= 0 else 0)

    def set_prepare_available(self, available: bool, reason: str = "") -> None:
        # Only ever called with False in this build (see window.py); kept so integration can enable it.
        self.btn_prepare.setEnabled(available)
        self.btn_prepare.set_description("Copy recordings to a review drive." if available else reason)

    def apply(self, v: DashboardView) -> None:
        # Z-001 lives in the window's persistent test-camera strip, never in this band (no duplicate).
        self.band.apply(band_alert(v) if not v.banner_takes_over else None)
        self.hero.apply(v)
        clear_layout(self.others)
        for a in v.other_alerts[:3]:
            self.others.addWidget(_other_alert_row(self.p, a))
        self.camera.apply(v.camera)
        self.storage.apply(v.storage)
        self.drive.apply(v.drive)

    def set_report_feedback(self, text: str) -> None:
        self.hero.set_feedback(text if self.hero.takeover else "")
        self.band.set_feedback(text if not self.hero.takeover else "")


def band_alert(v: DashboardView) -> AlertView | None:
    """The alert for a page's banner band: the current banner, except Z-001 (shown by the strip)."""
    return v.banner if v.banner is not None and v.banner.code != "Z-001" else None


def _other_alert_row(p: Palette, a: AlertView) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(6, 0, 6, 0)
    lay.setSpacing(10)
    lay.addWidget(IconWidget(TONE_ICON[a.tone], p.tone(a.tone), 18, inner=p.bg), 0, Qt.AlignmentFlag.AlignTop)
    lay.addWidget(label(f"Also: {a.message}", CAPTION_PX, name="secondary"), 1)
    return w


# --------------------------------------------------------------------------- recording status
class TimelinePage(QWidget):
    back = Signal()

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        lay = QVBoxLayout(self)
        lay.setContentsMargins(MARGIN, 22, MARGIN, 18)
        lay.setSpacing(16)
        self.header = PageHeader(palette, "Recording status", "The last 24 hours, newest first.")
        self.header.back.connect(self.back)
        lay.addWidget(self.header)
        self.band = AlertBand(palette, compact=True)
        lay.addWidget(self.band)

        summary = card()
        s = QVBoxLayout(summary)
        s.setContentsMargins(24, 20, 24, 18)
        s.setSpacing(12)
        row = QHBoxLayout()
        self.sum_icon = IconWidget(Icon.OK, palette.ok, 30, inner=palette.surface)
        row.addWidget(self.sum_icon)
        row.addSpacing(6)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.sum_title = label("", VALUE_PX, W.DemiBold)
        self.sum_detail = label("", CAPTION_PX, name="secondary")
        col.addWidget(self.sum_title)
        col.addWidget(self.sum_detail)
        row.addLayout(col, 1)
        s.addLayout(row)
        self.bar = TimelineBar(palette)
        s.addWidget(self.bar)
        legend = QHBoxLayout()
        legend.setSpacing(24)
        for kind, words in ((SpanKind.RECORDED, "Recorded"), (SpanKind.GAP, "No recording"),
                            (SpanKind.DAMAGED, "Damaged recording"), (SpanKind.NO_INFO, "No information")):
            legend.addWidget(LegendItem(self.bar, kind, words))
        legend.addWidget(LegendItem(self.bar, None, "Motion"))
        legend.addStretch(1)
        s.addLayout(legend)
        lay.addWidget(summary)

        lst = card()
        lv = QVBoxLayout(lst)
        lv.setContentsMargins(20, 8, 20, 8)
        inner = QWidget()
        self.rows = QVBoxLayout(inner)
        self.rows.setContentsMargins(0, 0, 8, 0)
        self.rows.setSpacing(0)
        lv.addWidget(scroll(inner))
        lay.addWidget(lst, 1)

    def apply(self, tl: Timeline, tz: ZoneInfo, banner: AlertView | None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.band.apply(banner)
        clear_layout(self.rows)
        if not tl.available:
            self.sum_icon.set_icon(Icon.NEUTRAL, self.p.neutral, inner=self.p.surface)
            self.sum_title.setText("No recording history is available yet")
            self.sum_detail.setText("The recording history will appear here once the recorder has saved recordings.")
            self.bar.set_timeline(tl, tz)
            self.rows.addStretch(1)
            return
        gaps = tl.interruptions
        if not gaps:
            self.sum_icon.set_icon(Icon.OK, self.p.ok, inner=self.p.surface)
        else:
            self.sum_icon.set_icon(Icon.WARNING, self.p.warning, inner=self.p.surface)
        self.sum_title.setText(f"{tf.duration(tl.recorded_s)} recorded in the last 24 hours")
        no_info = sum(s.seconds for s in tl.spans if s.kind is SpanKind.NO_INFO)
        bits = [f"{len(gaps)} interruption{'s' if len(gaps) != 1 else ''}"
                + (f", {tf.duration(tl.missing_s)} missing in total" if gaps else "")]
        if no_info >= 60:
            bits.append(f"{tf.duration(no_info)} with no information")
        if tl.motion:
            bits.append(f"motion seen {len(tl.motion)} time{'s' if len(tl.motion) != 1 else ''}")
        self.sum_detail.setText(" · ".join(bits) + ".")
        self.bar.set_timeline(tl, tz)
        for s in sorted(tl.spans, key=lambda x: x.start, reverse=True):
            self.rows.addWidget(SpanRow(self.p, s, tz, now))
        self.rows.addStretch(1)


# --------------------------------------------------------------------------- previous exports
class ExportsPage(QWidget):
    back = Signal()

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        lay = QVBoxLayout(self)
        lay.setContentsMargins(MARGIN, 22, MARGIN, 18)
        lay.setSpacing(16)
        self.header = PageHeader(palette, "Previous exports", "Review drives prepared on this system.")
        self.header.back.connect(self.back)
        lay.addWidget(self.header)
        self.band = AlertBand(palette, compact=True)
        lay.addWidget(self.band)
        box = card()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(24, 16, 24, 16)
        inner = QWidget()
        self.rows = QVBoxLayout(inner)
        self.rows.setContentsMargins(0, 0, 8, 0)
        self.rows.setSpacing(0)
        bl.addWidget(scroll(inner))
        lay.addWidget(box, 1)

    def apply(self, ex: ExportList, tz: ZoneInfo, export_installed: bool, banner: AlertView | None,
              now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.band.apply(banner)
        clear_layout(self.rows)

        def empty(title: str, detail: str) -> None:
            self.rows.addStretch(1)
            self.rows.addWidget(IconWidget(Icon.HISTORY, self.p.text3, 44), 0, Qt.AlignmentFlag.AlignHCenter)
            self.rows.addSpacing(10)
            t = label(title, VALUE_PX, W.DemiBold)
            t.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            d = label(detail, BODY_PX, name="secondary")
            d.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.rows.addWidget(t)
            self.rows.addWidget(d)
            self.rows.addStretch(2)

        if ex.table is None or not ex.rows:
            detail = ("Exports will be listed here after the first review drive has been prepared."
                      if export_installed else "The review-drive feature is not installed in this version.")
            if ex.table is not None and not ex.readable:
                empty("Previous exports cannot be shown",
                      "Export records exist, but this version of the screen cannot display them. "
                      "Contact technical support if you need this list.")
                return
            if not ex.available:
                detail = "The recording history is not available yet. " + detail
            empty("None yet", detail)
            return
        for r in ex.rows:
            ok = r.outcome == OUTCOME_COMPLETED
            w = QWidget()
            row = QHBoxLayout(w)
            row.setContentsMargins(0, 10, 0, 10)
            row.setSpacing(14)
            row.addWidget(IconWidget(Icon.OK if ok else Icon.WARNING, self.p.ok if ok else self.p.warning, 22,
                                     inner=self.p.surface), 0, Qt.AlignmentFlag.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(2)
            when = tf.when(r.when_epoch, tz, now) if r.when_epoch else "Time not recorded"
            col.addWidget(label(f"{when} — {r.outcome}", BODY_PX, W.DemiBold))
            bits = []
            if r.from_epoch and r.to_epoch:
                bits.append(f"Recordings from {tf.when(r.from_epoch, tz, now)} to {tf.when(r.to_epoch, tz, now)}")
            if r.drive:
                bits.append(f"Drive {r.drive}")
            if r.error_code:
                bits.append(f"Error {r.error_code}")
            if bits:
                col.addWidget(label(" · ".join(bits), CAPTION_PX, name="secondary"))
            row.addLayout(col, 1)
            self.rows.addWidget(w)
        self.rows.addStretch(1)


# --------------------------------------------------------------------------- administrator
class AdminLockPage(QWidget):
    back = Signal()
    unlock = Signal(str)

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(MARGIN, 22, MARGIN, 18)
        lay.setSpacing(16)
        self.header = PageHeader(palette, "Administrator", "Diagnostics for administrators and technical support.")
        self.header.back.connect(self.back)
        lay.addWidget(self.header)
        box = card()
        box.setMaximumWidth(560)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(32, 28, 32, 28)
        bl.setSpacing(12)
        bl.addWidget(IconWidget(Icon.LOCK, palette.text2, 36))
        self.title = label("Enter the administrator PIN", VALUE_PX, W.DemiBold)
        bl.addWidget(self.title)
        self.explain = label("Operators do not need this area. Nothing here changes the recording.",
                             CAPTION_PX, name="secondary")
        bl.addWidget(self.explain)
        self.pin = QLineEdit()
        self.pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin.setMaxLength(12)
        self.pin.setAccessibleName("Administrator PIN")
        self.pin.returnPressed.connect(self._submit)
        bl.addWidget(self.pin)
        row = QHBoxLayout()
        self.btn = QPushButton("Unlock")
        self.btn.setObjectName("primary")
        self.btn.clicked.connect(self._submit)
        row.addWidget(self.btn)
        row.addStretch(1)
        bl.addLayout(row)
        self.message = label("", CAPTION_PX)
        self.message.setStyleSheet(f"color: {palette.critical};")
        bl.addWidget(self.message)
        lay.addWidget(box, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(1)

    def reset(self, configured: bool) -> None:
        self.pin.clear()
        self.message.clear()
        self.pin.setEnabled(configured)
        self.btn.setEnabled(configured)
        if configured:
            self.title.setText("Enter the administrator PIN")
            self.explain.setText("Operators do not need this area. Nothing here changes the recording.")
        else:
            self.title.setText("Administrator access is not set up")
            self.explain.setText("An administrator PIN has not been set on this computer. "
                                 "See the administrator guide to set one.")

    def _submit(self) -> None:
        self.unlock.emit(self.pin.text())
        self.pin.clear()


class AdminPage(QWidget):
    lock = Signal()
    refresh_requested = Signal()
    save_report = Signal()

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        lay = QVBoxLayout(self)
        lay.setContentsMargins(MARGIN, 22, MARGIN, 18)
        lay.setSpacing(14)
        self.header = PageHeader(palette, "Administrator diagnostics",
                                 "Read-only. Nothing on this page changes the recording.")
        self.header.back_btn.setText("‹  Lock and go back")
        self.header.back.connect(self.lock)
        lay.addWidget(self.header)
        bar = QHBoxLayout()
        b1 = QPushButton("Refresh")
        b1.clicked.connect(self.refresh_requested)
        b2 = QPushButton("Save diagnostic report")
        b2.clicked.connect(self.save_report)
        bar.addWidget(b1)
        bar.addWidget(b2)
        self.feedback = label("", CAPTION_PX, name="secondary")
        bar.addWidget(self.feedback, 1)
        lay.addLayout(bar)
        self.tabs = QTabWidget()
        mono = font(SMALL_PX, mono=True)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setFont(mono)
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setFont(mono)
        self.events = self._table(["Time (UTC)", "Severity", "Category", "Code", "Message"])
        self.gaps = self._table(["Gap", "Kind", "Start (UTC)", "End (UTC)", "Seconds", "Reason", "Attempts"])
        self.tabs.addTab(self.summary, "Summary")
        self.tabs.addTab(self.raw, "Status data")
        self.tabs.addTab(self.events, "Recent events")
        self.tabs.addTab(self.gaps, "Gaps")
        lay.addWidget(self.tabs, 1)

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        t.setAlternatingRowColors(False)
        return t

    @staticmethod
    def _fill(t: QTableWidget, rows: list[list[str]]) -> None:
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, v in enumerate(r):
                t.setItem(i, j, QTableWidgetItem(v))
        t.resizeColumnsToContents()

    def apply(self, summary_lines: list[str], raw_json: str, data: AdminData) -> None:
        self.summary.setPlainText("\n".join(summary_lines))
        self.raw.setPlainText(raw_json)
        self._fill(self.events, [[str(e.get("ts_utc", "")), str(e.get("severity", "")), str(e.get("category", "")),
                                  str(e.get("code", "")), str(e.get("message", ""))] for e in data.events])
        self._fill(self.gaps, [[str(g.get("gap_id", "")), str(g.get("kind", "")), str(g.get("start_utc") or ""),
                                str(g.get("end_utc") or "open"),
                                f"{g['duration_s']:.1f}" if isinstance(g.get("duration_s"), (int, float)) else "",
                                str(g.get("reason", "")), str(g.get("attempts", ""))] for g in data.recent_gaps])

