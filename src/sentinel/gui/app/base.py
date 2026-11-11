"""Pieces shared by the window's pages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

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


def toned(text: str, tone: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName(tone)
    lab.setWordWrap(True)
    return lab


def icon_button(text: str, icon: str, color: str) -> QPushButton:
    from .icons import glyph

    b = QPushButton(glyph(icon, color, 16), f" {text}" if text else "")
    return b


class ReadOnlyBar(QFrame):
    """Shown on settings pages when the window is not running as administrator."""

    def __init__(self, ctx: Ctx, what: str = "these settings", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .icons import glyph_pixmap

        self.setObjectName("notice")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 8, 6)
        ic = QLabel()
        ic.setPixmap(glyph_pixmap("lock", ctx.palette.text2, 16))
        lay.addWidget(ic)
        lay.addWidget(QLabel(f"Read-only. Changing {what} needs Windows administrator rights."), 1)
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
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setPlaceholderText("Command output appears here.")
        self.current: Command | None = None

    def run(self, argv: list[str], title: str, on_done: Callable[[int, Command], None] | None = None,
            busy: list[QWidget] | None = None, show_output: bool = True) -> Command | None:
        if self.current is not None and self.current.running():
            QMessageBox.information(self, "Sentinel", "Wait until the current command has finished.")
            return None
        self.appendPlainText(f"── {title}")
        self.started.emit(title)
        cmd = Command(argv, self)
        cmd.title = title  # type: ignore[attr-defined]
        if show_output:
            cmd.output.connect(self.appendPlainText)
        before = [(w, w.isEnabled()) for w in busy or []]
        for w, _ in before:
            w.setEnabled(False)

        def finished(code: int) -> None:
            for w, was in before:          # restore, never enable what was disabled (review 2026-11-10)
                w.setEnabled(was)
            self.appendPlainText(f"── finished ({'OK' if code == 0 else f'exit code {code}'})\n")
            self.ended.emit(title, code)
            if on_done is not None:
                on_done(code, cmd)

        cmd.finished.connect(finished)
        self.current = cmd
        return cmd.start()


class CommandPanel(QWidget):
    """Result of the last command in one line (running / done / failed), the output behind Details."""

    def __init__(self, log: OutputLog, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QToolButton

        self.log, self.p = log, palette
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        row = QHBoxLayout()
        self.icon = QLabel()
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.toggle = QToolButton()
        self.toggle.setText("Details")
        self.toggle.setCheckable(True)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setAutoRaise(True)
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
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)

    def _started(self, title: str) -> None:
        self.setVisible(True)
        self._set("refresh", f"{title}: running…")

    def _ended(self, title: str, code: int) -> None:
        if code == 0:
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
    t.horizontalHeader().setHighlightSections(False)
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


def restart_recorder(cfg_path: Path, cfg: AppConfig) -> str:
    """Stop and start the recorder (worker thread) so new settings take effect."""
    from ...launcher.control import select_control

    control = select_control(cfg_path, cfg)
    if control.is_running() and not control.stop(90.0):
        return "The recorder did not stop within 90 seconds. Restart this computer to apply the settings."
    result = control.start()
    if not getattr(result, "ok", False):
        return (f"The recorder could not be started ({getattr(result, 'code', '?')}). "
                "See System › Service.")
    if hasattr(control, "start_archive"):
        control.start_archive()
    return "The recorder was restarted with the saved settings."
