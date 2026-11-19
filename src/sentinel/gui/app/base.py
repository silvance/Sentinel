"""Pieces shared by the window's pages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from ...config import AppConfig
from ..theme import Palette
from ..viewmodel import StatusRead
from .procs import Command


@dataclass
class Ctx:
    """What every page may use. Owned by the window; pages never replace it."""
    cfg: AppConfig | None
    cfg_path: Path | None
    admin: bool
    palette: Palette
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    read: StatusRead | None = None
    navigate: Callable[[str], None] = lambda _k: None
    unlock: Callable[[], None] = lambda: None
    config_changed: Callable[[AppConfig], None] = lambda _c: None
    clock: Callable[[], float] | None = None
    restart: Callable[..., None] = lambda on_done=None: None   # AppWindow.restart_recorder
    start: Callable[..., None] = lambda on_done=None: None     # AppWindow.start_recorder
    after_change: Callable[[], None] = lambda: None             # AppWindow.watch_after_change


class Page(QWidget):
    key = ""
    title = ""
    description = ""
    # Everything below the title scrolls when the window is too short for it, instead of controls
    # being squeezed on top of each other (field report 2026-11-10: laptop at 150 % scaling). A page
    # with its own scroll area sets this to False.
    scrolls = True

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 10, 16, 10)
        outer.setSpacing(8)
        # Header: title and one-line description on the left, the page's commands on the right.
        head = QHBoxLayout()
        head.setSpacing(8)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        t = QLabel(self.title)
        self.title_label = t                  # hidden when the page is a tab (SettingsHost)
        t.setObjectName("pageTitle")
        f = t.font()
        f.setPointSizeF(f.pointSizeF() + 4)
        t.setFont(f)
        titles.addWidget(t)
        if self.description:
            d = QLabel(self.description)
            d.setObjectName("muted")
            titles.addWidget(d)
        head.addLayout(titles)
        head.addStretch(1)
        self.actions = QHBoxLayout()           # page commands (see command())
        self.actions.setSpacing(6)
        head.addLayout(self.actions)
        self.head = head
        outer.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        if self.scrolls:
            content = QWidget()
            content.setLayout(self.body)
            self.body.setContentsMargins(0, 0, 6, 0)          # room for the scroll bar
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.Shape.NoFrame)
            self.scroll.setWidget(content)
            outer.addWidget(self.scroll, 1)
        else:
            outer.addLayout(self.body, 1)

    def command(self, text: str, icon: str, slot: Callable[[], Any], tip: str = "",
                enabled: bool = True) -> QPushButton:
        """A button in the page's command bar (top right)."""
        b = icon_button(text, icon, self.ctx.palette.text)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(slot)
        b.setEnabled(enabled)
        self.actions.addWidget(b)
        return b

    # Called by the window.
    def on_show(self) -> None:
        """The page was opened (load what it shows)."""

    def on_status(self, read: StatusRead | None) -> None:
        """New recorder status (every few seconds)."""

    def on_hide(self) -> None:
        """Another page was opened (stop what only this page needs, e.g. the live view)."""

    @property
    def cfg(self) -> AppConfig | None:
        return self.ctx.cfg

    def info(self, text: str) -> None:
        QMessageBox.information(self, "Sentinel", text)

    def problem(self, text: str) -> None:
        QMessageBox.warning(self, "Sentinel", text)


FORM_MAX_W = 760


def capped(w: QWidget, width: int = FORM_MAX_W) -> QWidget:
    """Forms do not stretch across a wide screen."""
    w.setMaximumWidth(width)
    return w


def muted(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


class Note(QLabel):
    """Muted help text under a field in a form. A wrapped QLabel there asks for the height its text would have
    in a narrow column, which left a wide gap above and below the words (GUI review 2026-11-17); this one asks
    for the height its text has at the width it was given."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("muted")
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

    def _height(self) -> int:
        w = self.width() if self.width() > 120 else 480
        return max(self.heightForWidth(w), self.fontMetrics().height())

    def sizeHint(self):  # noqa: ANN201, N802
        from PySide6.QtCore import QSize

        return QSize(super().sizeHint().width(), self._height())

    def minimumSizeHint(self):  # noqa: ANN201, N802
        from PySide6.QtCore import QSize

        return QSize(120, self._height())

    def resizeEvent(self, e) -> None:  # noqa: ANN001, N802
        super().resizeEvent(e)
        if e.oldSize().width() != e.size().width():
            self.updateGeometry()


class ElidedLabel(QLabel):
    """One line of text; when it does not fit it ends in "…" instead of being cut off mid-word (GUI review
    2026-11-17). The full text is the caller's tooltip. Text that fits is drawn (and can be selected) as usual."""

    def paintEvent(self, e) -> None:  # noqa: ANN001, N802
        from PySide6.QtGui import QPainter, QPalette

        rect = self.contentsRect()
        fm = self.fontMetrics()
        if fm.horizontalAdvance(self.text()) <= rect.width():
            super().paintEvent(e)
            return
        qp = QPainter(self)
        qp.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        qp.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width()))
        qp.end()


def toned(text: str, tone: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName(tone)
    lab.setWordWrap(True)
    return lab


def icon_button(text: str, icon: str, color: str) -> QPushButton:
    from .icons import glyph

    b = QPushButton(glyph(icon, color, 16), f" {text}" if text else "")
    return b


def disclosure_button(text: str) -> QToolButton:
    """A small arrow-and-word toggle ("Details", "Advanced") that opens what is below it. Drawn flat in every
    state (style.py, #disclosure): the native checked look is a solid accent block that hid the word (GUI
    review 2026-11-17)."""
    b = QToolButton()
    b.setObjectName("disclosure")
    b.setText(text)
    b.setCheckable(True)
    b.setAutoRaise(True)
    b.setArrowType(Qt.ArrowType.RightArrow)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.toggled.connect(lambda on: b.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow))
    return b


class AdvancedRows:
    """Rows of a form that stay hidden until "Advanced" is opened (GUI review 2026-11-17): the settings a
    technician changes once, if ever, are one click away and out of the way of the person running the laptop.
    Hidden fields keep their values and are saved like the others."""

    def __init__(self, form: QFormLayout, note: str = "Rarely changed; the defaults suit most sites.") -> None:
        self.form = form
        self.fields: list[QWidget] = []
        self.toggle = disclosure_button("Advanced")
        head = QWidget()
        row = QHBoxLayout(head)
        row.setContentsMargins(0, 2, 0, 0)
        row.addWidget(self.toggle)
        row.addWidget(muted(note), 1)
        form.addRow(head)
        self.toggle.toggled.connect(self._show)

    def add(self, label: str, field: QWidget) -> QWidget:
        self.form.addRow(label, field)
        self.fields.append(field)
        self.form.setRowVisible(field, self.toggle.isChecked())
        return field

    def _show(self, on: bool) -> None:
        for f in self.fields:
            self.form.setRowVisible(f, on)

    def set_open(self, on: bool) -> None:
        self.toggle.setChecked(on)

    def is_open(self) -> bool:
        return self.toggle.isChecked()


class ReadOnlyBar(QFrame):
    """Shown on settings pages when the window is not running as administrator."""

    def __init__(self, ctx: Ctx, what: str = "these settings", parent: QWidget | None = None, *,
                 text: str = "") -> None:
        super().__init__(parent)
        from .icons import glyph_pixmap

        self.setObjectName("notice")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 8, 6)
        ic = QLabel()
        ic.setPixmap(glyph_pixmap("lock", ctx.palette.text2, 16))
        lay.addWidget(ic)
        words = QLabel(text or f"Read-only. Changing {what} needs Windows administrator rights.")
        words.setWordWrap(True)
        lay.addWidget(words, 1)
        b = QPushButton("Unlock…")
        b.setToolTip("Reopen Sentinel as administrator (Windows asks for approval)")
        b.clicked.connect(ctx.unlock)
        lay.addWidget(b)
        self.setVisible(not ctx.admin)


class OutputLog(QPlainTextEdit):
    """Output of the commands a page runs. Shown inside a CommandPanel: one result line, with the
    full output behind "Details" (opened automatically when a command fails)."""

    started = Signal(str)
    ended = Signal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("log")
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)   # long messages are read whole
        self.setPlaceholderText("Command output appears here.")
        self.current: Command | None = None
        self.last_stopped = False       # the last command ended because the person pressed Stop

    def run(self, argv: list[str], title: str, on_done: Callable[[int, Command], None] | None = None,
            busy: list[QWidget] | None = None, show_output: bool = True,
            stop_note: str | None = None) -> Command | None:
        """``stop_note``: the command only waits for something (it changes nothing itself), so the
        person may stop it when another one is wanted; the note is logged when they do."""
        if self.current is not None and self.current.running():
            if not self._offer_stop():
                return None
        self.appendPlainText(f"── {title}")
        self.started.emit(title)
        cmd = Command(argv, self)
        cmd.title = title  # type: ignore[attr-defined]
        cmd.stop_note = stop_note  # type: ignore[attr-defined]
        if show_output:
            cmd.output.connect(self.appendPlainText)
        before = [(w, w.isEnabled()) for w in busy or []]
        for w, _ in before:
            w.setEnabled(False)

        def finished(code: int) -> None:
            for w, was in before:          # restore, never enable what was disabled (review 2026-11-10)
                w.setEnabled(was)
            # Stopped on request is not a failure (field report 2026-11-18: "failed (exit code 62097)").
            self.last_stopped = bool(getattr(cmd, "stopped", False))
            self.appendPlainText("── stopped\n" if self.last_stopped
                                 else f"── finished ({'OK' if code == 0 else f'exit code {code}'})\n")
            self.ended.emit(title, code)
            if on_done is not None:
                on_done(code, cmd)

        cmd.finished.connect(finished)
        self.current = cmd
        return cmd.start()

    def stop(self) -> None:
        """Stop the running command because the person asked for it (a Stop button)."""
        cur = self.current
        if cur is not None and cur.running():
            cur.stopped = True  # type: ignore[attr-defined]
            cur.kill()

    def _offer_stop(self) -> bool:
        """Another command is running. Field report 2026-11-12: "Wait until the current command has
        finished" named no command, and a swap waiting for a stopped archive copier blocked the page
        for 10 minutes. Name it; a command that only waits may be stopped. True once it has ended."""
        cur = self.current
        assert cur is not None
        title = getattr(cur, "title", "A command")
        note = getattr(cur, "stop_note", None)
        if note is None:
            QMessageBox.information(self, "Sentinel", f"“{title}” is still running. Wait until it has "
                                    "finished (see the line under the list), then try again.")
            return False
        box = QMessageBox(QMessageBox.Icon.Question, "Sentinel",
                          f"“{title}” is still running. Stop it and go on?",
                          QMessageBox.StandardButton.Cancel, self)
        stop = box.addButton("Stop it and go on", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(stop)
        box.exec()
        if box.clickedButton() is not stop:
            return False
        cur.kill()
        if not cur.proc.waitForFinished(5000):
            QMessageBox.information(self, "Sentinel", f"“{title}” did not stop. Try again in a moment.")
            return False
        cur._done(-1)
        self.appendPlainText(f"── {title} stopped. {note}\n")
        return True


class CommandPanel(QWidget):
    """Result of the last command in one line (running / done / failed), the output behind Details."""

    def __init__(self, log: OutputLog, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.log, self.p = log, palette
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        row = QHBoxLayout()
        self.icon = QLabel()
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.toggle = disclosure_button("Details")
        self.toggle.toggled.connect(self._show)
        row.addWidget(self.icon)
        row.addWidget(self.text, 1)
        row.addWidget(self.toggle)
        lay.addLayout(row)
        lay.addWidget(log, 1)
        log.setVisible(False)
        log.setMinimumHeight(160)
        log.started.connect(self._started)
        log.ended.connect(self._ended)
        self.setVisible(False)                 # appears with the first command
        self._set("info", "", muted=True)

    def _set(self, icon: str, text: str, muted: bool = False) -> None:
        from .icons import glyph_pixmap

        color = {"ok": self.p.ok, "critical": self.p.critical, "info": self.p.text2}.get(icon, self.p.text2)
        self.icon.setPixmap(glyph_pixmap(icon, color, 16))
        self.text.setText(text)
        self.text.setObjectName("muted" if muted else "")
        self.text.style().polish(self.text)

    def _show(self, on: bool) -> None:
        self.log.setVisible(on)

    def show_output(self) -> None:
        """Open Details: for a command whose lines are what the person is waiting for (the live motion test)."""
        self.toggle.setChecked(True)

    def _started(self, title: str) -> None:
        self.setVisible(True)
        self._set("refresh", f"{title}: running…")

    def _ended(self, title: str, code: int) -> None:
        if self.log.last_stopped:
            self._set("info", f"{title}: stopped.", muted=True)
        elif code == 0:
            self._set("ok", f"{title}: done.")
        else:
            self._set("critical", f"{title}: failed (exit code {code}). See Details.")
            self.toggle.setChecked(True)


def table(headers: list[str], stretch_last: bool = True) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(26)
    t.setMinimumHeight(30 + 4 * 26)          # header and four rows, even in a short window
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    t.horizontalHeader().setMinimumSectionSize(70)
    if stretch_last:
        t.horizontalHeader().setStretchLastSection(True)
        # long values are cut with the full text in the tooltip (fill), never a sideways scroll bar
        t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    t.horizontalHeader().setHighlightSections(False)
    # An empty table keeps these widths: wide enough for each heading (GUI review 2026-11-17: "Saved (replaced a").
    fm = t.horizontalHeader().fontMetrics()
    for c, h in enumerate(headers):
        t.setColumnWidth(c, max(t.columnWidth(c), fm.horizontalAdvance(h) + 28))
    return t


def fill(t: QTableWidget, rows: list[list[Any]], data: list[Any] | None = None, empty: str = "") -> None:
    """Fill a table; ``data[i]`` is stored on row i's first cell (Qt.UserRole). The selection is
    cleared: a row number may now be a different item (review 2026-11-10)."""
    t.clearSelection()
    t.setCurrentCell(-1, -1)
    if not rows and empty:            # an empty table says why instead of being a blank box
        t.setRowCount(1)
        t.setSpan(0, 0, 1, t.columnCount())
        item = QTableWidgetItem(empty)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(t.palette().placeholderText())
        t.setItem(0, 0, item)
        return
    t.clearSpans()
    t.setRowCount(len(rows))
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            item = QTableWidgetItem("" if v is None else str(v))
            if len(item.text()) > 40:
                item.setToolTip(item.text())
            if j == 0 and data is not None:
                item.setData(Qt.ItemDataRole.UserRole, data[i])
            t.setItem(i, j, item)
    t.resizeColumnsToContents()
    for c in range(t.columnCount() - 1):      # long values are cut, with the full text in the tooltip
        t.setColumnWidth(c, min(t.columnWidth(c) + 12, 240))
    if t.horizontalHeader().stretchLastSection():
        t.horizontalHeader().setSectionResizeMode(t.columnCount() - 1, QHeaderView.ResizeMode.Stretch)


def selected_data(t: QTableWidget) -> Any:
    rows = t.selectionModel().selectedRows()
    if not rows:
        return None
    item = t.item(rows[0].row(), 0)
    return item.data(Qt.ItemDataRole.UserRole) if item is not None else None


STARTED = "The recorder was started."
RUNNING_ALREADY = "The recorder is running already."


def start_recorder(cfg_path: Path, cfg: AppConfig, admin: bool = True) -> str:
    """Start a recorder that is stopped (worker thread). Never stops one: Status › Start recording, after
    someone used System › Stop recorder (field report 2026-11-19: the only ways back were the System
    page's Restart recorder or restarting the computer). A sentence for the person; it starts with
    :data:`STARTED` or :data:`RUNNING_ALREADY` when recording is on its way."""
    from ...config import ConfigError, load_config
    from ...launcher.control import select_control

    try:
        cfg = load_config(cfg_path)
    except (ConfigError, OSError) as exc:
        return (f"The recorder was not started: the settings file cannot be used ({exc}). Correct the file, or "
                "go back to working settings in System › Check and history.")
    control = select_control(cfg_path, cfg)
    if control.is_running():
        return RUNNING_ALREADY
    result = control.start()
    if not getattr(result, "ok", False):
        return (f"The recorder could not be started ({getattr(result, 'code', '?')}). "
                + ("See System › Service." if admin else
                   "This may need an administrator: press Unlock settings…, then Start recording again."))
    fn = getattr(control, "start_archive", None)       # no-op when the archive copy already runs
    if callable(fn):
        fn()
    return STARTED


def restart_recorder(cfg_path: Path, cfg: AppConfig) -> str:
    """Stop and start the recorder (worker thread) so new settings take effect."""
    from ...config import ConfigError, load_config
    from ...launcher.control import select_control

    # Review 2026-11-12: never stop a working recorder to start it with a settings file that does not load
    # (e.g. edited in Notepad): it would stop with C-001.
    try:
        cfg = load_config(cfg_path)
    except (ConfigError, OSError) as exc:
        return (f"The recorder was not restarted: the settings file cannot be used ({exc}). Recording continues "
                "with the settings it started with. Correct the file, or go back to working settings in "
                "System › Check and history.")
    control = select_control(cfg_path, cfg)
    if control.is_running() and not control.stop(90.0):
        return "The recorder did not stop within 90 seconds. Restart this computer to apply the settings."
    result = control.start()
    if not getattr(result, "ok", False):
        return (f"The recorder could not be started ({getattr(result, 'code', '?')}). "
                "See System › Service.")
    # The archive copier reads its settings when it starts: restart it too (review 2026-11-12).
    for name in ("restart_archive", "start_archive"):
        fn = getattr(control, name, None)
        if callable(fn):
            fn()
            break
    return "The recorder was restarted with the saved settings."
