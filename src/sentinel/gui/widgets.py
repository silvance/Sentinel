"""Reusable widgets of the operator GUI. They render view-model objects; they decide nothing."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeyEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import textfmt as tf
from .icons import TONE_ICON, Icon, IconWidget, paint_icon
from .ledger_view import Span, SpanKind, Timeline
from .theme import BANNER_PX, BODY_PX, CAPTION_PX, HERO_PX, RADIUS, SMALL_PX, VALUE_PX, Palette, font
from .viewmodel import AlertView, DashboardView, TileView, Tone

W = QFont.Weight


def label(text: str = "", px: int = BODY_PX, weight: QFont.Weight = W.Normal, color: str | None = None,
          wrap: bool = True, name: str | None = None) -> QLabel:
    lb = QLabel(text)
    f = font(px, weight)
    if name == "eyebrow":
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
    lb.setFont(f)
    lb.setWordWrap(wrap)
    lb.setTextFormat(Qt.TextFormat.PlainText)
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    if name:
        lb.setObjectName(name)
    if color:
        lb.setStyleSheet(f"color: {color};")
    return lb


def card(name: str = "card") -> QFrame:
    f = QFrame()
    f.setObjectName(name)
    return f


class CodeChip(QLabel):
    """'Error R-003' in a pill, letter-spaced so it is easy to read aloud on the phone."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        f = font(CAPTION_PX + 1, W.DemiBold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        self.setFont(f)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_code(self, text: str, fg: str, border: str, bg: str = "transparent") -> None:
        self.setText(text)
        self.setStyleSheet(f"color: {fg}; background: {bg}; border: 1.5px solid {border}; border-radius: 6px;"
                           f" padding: 3px 10px;")
        self.setAccessibleName(text)


# --------------------------------------------------------------------------- alert banner / hero
class ReportButtons(QWidget):
    """'Save diagnostic report' (to the Desktop, one click) + 'Choose another location…'."""

    save = Signal()
    save_elsewhere = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.primary = QPushButton("Save diagnostic report")
        self.primary.setAccessibleDescription("Saves a report for technical support on the Desktop.")
        self.primary.clicked.connect(self.save)
        self.other = QPushButton("Choose another location…")
        self.other.clicked.connect(self.save_elsewhere)
        lay.addWidget(self.primary)
        lay.addWidget(self.other)
        lay.addStretch(1)
        self.feedback = label("", CAPTION_PX)
        self.feedback.hide()

    def style_on(self, bg: str, fg: str) -> None:
        self.primary.setStyleSheet(
            f"QPushButton {{ background: {fg}; color: {bg}; border: 2px solid {fg}; border-radius: 8px;"
            f" padding: 8px 18px; font-weight: 600; }}"
            f" QPushButton:focus {{ border: 3px solid {bg}; outline: 2px solid {fg}; }}")
        self.other.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {fg}; border: 1.5px solid {fg}; border-radius: 8px;"
            f" padding: 8px 16px; font-weight: 500; }}"
            f" QPushButton:focus {{ border: 3px solid {fg}; }}")
        self.feedback.setStyleSheet(f"color: {fg};")


class StatusHero(QFrame):
    """The dominant element: the system state in plain words. Critical alerts take it over."""

    save_report = Signal()
    save_report_elsewhere = Signal()

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        self.setObjectName("hero")
        outer = QHBoxLayout(self)
        outer.setContentsMargins(28, 20, 28, 20)
        outer.setSpacing(22)
        self.icon = IconWidget(Icon.OK, palette.ok, 64)
        icol = QVBoxLayout()
        icol.addWidget(self.icon)
        icol.addStretch(1)
        outer.addLayout(icol)
        col = QVBoxLayout()
        col.setSpacing(8)
        self.label_word = label("", SMALL_PX, W.DemiBold, wrap=False)
        self.headline = label("", HERO_PX, W.DemiBold)
        self.message = label("", BANNER_PX, W.Medium)
        self.supporting = label("", BODY_PX + 1)
        self.detail = label("", BODY_PX)
        code_row = QHBoxLayout()
        code_row.setSpacing(12)
        self.chip = CodeChip()
        self.chip_hint = label("Quote this code when you call technical support.", CAPTION_PX, wrap=False)
        code_row.addWidget(self.chip)
        code_row.addWidget(self.chip_hint)
        code_row.addStretch(1)
        self.code_row = QWidget()
        self.code_row.setLayout(code_row)
        code_row.setContentsMargins(0, 4, 0, 0)
        self.buttons = ReportButtons()
        self.buttons.save.connect(self.save_report)
        self.buttons.save_elsewhere.connect(self.save_report_elsewhere)
        col.addWidget(self.label_word)
        col.addWidget(self.headline)
        col.addWidget(self.message)
        col.addWidget(self.supporting)
        col.addWidget(self.detail)
        col.addWidget(self.code_row)
        col.addSpacing(4)
        col.addWidget(self.buttons)
        col.addWidget(self.buttons.feedback)
        outer.addLayout(col, 1)
        self.takeover = False

    def apply(self, v: DashboardView) -> None:
        p = self.p
        alert = v.banner if v.banner_takes_over else v.notice
        self.takeover = v.banner_takes_over
        if self.takeover and v.banner is not None:
            bg, fg = p.banner(Tone.CRITICAL)
            self.setStyleSheet(f"#hero {{ background: {bg}; border-radius: {RADIUS}px;"
                               f" border: 2px solid {p.banner_border(Tone.CRITICAL)}; }}"
                               f" QLabel {{ color: {fg}; }}")
            self.icon.set_icon(Icon.CRITICAL, p.banner_icon(Tone.CRITICAL), inner=bg)
            # Title = what went wrong ("Camera connection lost"); the body is the exact alert message,
            # which already begins with the state ("Recording stopped. Error R-003: ..."). The state
            # word is shown small above the title only when the message does not already say it.
            state = v.headline
            self.label_word.setText(state.upper())
            self.label_word.setVisible(not v.banner.message.lower().startswith(state.lower()))
            for w in (self.label_word, self.headline, self.message, self.supporting, self.detail):
                w.setStyleSheet(f"color: {fg};")
            self.headline.setText(v.banner.title or state)
            self.message.setText(v.banner.message)
            self.message.show()
            self.supporting.hide()
            self.detail.setText(v.detail)
            self.detail.setVisible(bool(v.detail))
            self.chip.set_code(v.banner.code_text, fg, fg)
            self.chip_hint.setStyleSheet(f"color: {fg};")
            self.code_row.show()
            self.buttons.style_on(bg, fg)
            self.buttons.setVisible(v.banner.offer_report)
        else:
            tone_c = p.tone(v.tone)
            self.setStyleSheet(f"#hero {{ background: {p.soft(v.tone)}; border-radius: {RADIUS}px;"
                               f" border: 1px solid {p.border}; }}")
            self.icon.set_icon(TONE_ICON[v.tone], tone_c, inner=p.surface if not p.dark else p.bg)
            self.label_word.hide()
            self.headline.setText(v.headline)
            self.headline.setStyleSheet(f"color: {p.text};")
            self.message.hide()
            self.supporting.setText(v.supporting)
            self.supporting.setStyleSheet(f"color: {p.text2};")
            self.supporting.setVisible(bool(v.supporting))
            self.detail.setText(v.detail)
            self.detail.setStyleSheet(f"color: {p.text2};")
            self.detail.setVisible(bool(v.detail))
            if alert is not None:
                self.chip.set_code(alert.code_text, p.text, p.border, p.surface)
                self.chip_hint.setStyleSheet(f"color: {p.text2};")
                self.code_row.show()
            else:
                self.code_row.hide()
            self.buttons.hide()
            self.buttons.feedback.hide()

    def set_feedback(self, text: str) -> None:
        self.buttons.feedback.setText(text)
        self.buttons.feedback.setVisible(bool(text))


class AlertBand(QFrame):
    """Full-width, high-contrast banner for WARNING alerts (critical alerts take over the hero)."""

    save_report = Signal()
    save_report_elsewhere = Signal()

    def __init__(self, palette: Palette, compact: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        self.compact = compact
        self.setObjectName("band")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(22, 14 if compact else 18, 22, 14 if compact else 18)
        lay.setSpacing(16)
        self.icon = IconWidget(Icon.WARNING, palette.warning_banner_text, 30 if compact else 40)
        icol = QVBoxLayout()
        icol.addWidget(self.icon)
        icol.addStretch(1)
        lay.addLayout(icol)
        col = QVBoxLayout()
        col.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.word = label("", SMALL_PX, W.Bold, wrap=False)
        self.chip = CodeChip()
        top.addWidget(self.word)
        top.addStretch(1)
        top.addWidget(self.chip)
        col.addLayout(top)
        self.message = label("", BODY_PX + 1 if compact else BANNER_PX - 2, W.Medium)
        col.addWidget(self.message)
        self.buttons = ReportButtons()
        self.buttons.save.connect(self.save_report)
        self.buttons.save_elsewhere.connect(self.save_report_elsewhere)
        if not compact:
            col.addWidget(self.buttons)
            col.addWidget(self.buttons.feedback)
        lay.addLayout(col, 1)
        self.alert: AlertView | None = None

    def apply(self, a: AlertView | None) -> None:
        self.alert = a
        if a is None:
            self.hide()
            return
        t = a.tone if a.tone is Tone.CRITICAL else Tone.WARNING
        bg, fg = self.p.banner(t)
        self.setStyleSheet(f"#band {{ background: {bg}; border-radius: {RADIUS}px;"
                           f" border: 2px solid {self.p.banner_border(t)}; }} QLabel {{ color: {fg}; }}")
        shape = Icon.CRITICAL if a.tone is Tone.CRITICAL else (Icon.WARNING if a.severity >= 2 else Icon.INFO)
        self.icon.set_icon(shape, self.p.banner_icon(t), inner=bg)
        self.word.setText(a.label.upper())
        self.chip.set_code(a.code_text, fg, fg)
        self.message.setText(a.message)
        self.buttons.style_on(bg, fg)
        self.buttons.setVisible(a.offer_report and not self.compact)
        self.show()

    def set_feedback(self, text: str) -> None:
        self.buttons.feedback.setText(text)
        self.buttons.feedback.setVisible(bool(text))


class TestCameraStrip(QFrame):
    """Persistent full-width strip under the header for Z-001 (test camera, not evidence).

    It sits outside the page stack, so it stays visible on every page and in every state,
    including when a critical alert takes over the status area."""

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("teststrip")
        bg, fg = palette.warning_banner_bg, palette.warning_banner_text
        self._fg = fg
        self.setStyleSheet(f"#teststrip {{ background: {bg}; border-top: 1px solid {palette.banner_border(Tone.WARNING)};"
                           f" border-bottom: 2px solid {palette.banner_border(Tone.WARNING)}; }}"
                           f" QLabel {{ color: {fg}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(28, 9, 28, 9)
        lay.setSpacing(14)
        self.icon = IconWidget(Icon.WARNING, palette.banner_icon(Tone.WARNING), 24, inner=bg)
        lay.addWidget(self.icon)
        self.word = label("TEST SYSTEM — NOT EVIDENCE", SMALL_PX, W.Bold, wrap=False)
        lay.addWidget(self.word)
        self.message = label("", CAPTION_PX, W.Medium)
        lay.addWidget(self.message, 1)
        self.chip = CodeChip()
        lay.addWidget(self.chip)
        self.alert: AlertView | None = None
        self.hide()

    def apply(self, a: AlertView | None) -> None:
        self.alert = a
        if a is None:
            self.hide()
            return
        self.message.setText(a.message)
        self.chip.set_code(a.code_text, self._fg, self._fg)
        self.show()


# --------------------------------------------------------------------------- tiles
class InfoTile(QFrame):
    def __init__(self, palette: Palette, kind: Icon, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(IconWidget(kind, palette.text2, 22))
        self.title = label("", SMALL_PX, W.DemiBold, wrap=False, name="eyebrow")
        head.addWidget(self.title)
        head.addStretch(1)
        lay.addLayout(head)
        lay.addSpacing(4)
        val = QHBoxLayout()
        val.setSpacing(10)
        self.state_icon = IconWidget(Icon.OK, palette.ok, 22)
        self.value = label("", VALUE_PX, W.DemiBold, wrap=False)
        val.addWidget(self.state_icon)
        val.addWidget(self.value)
        val.addStretch(1)
        lay.addLayout(val)
        self.detail = label("", CAPTION_PX, name="secondary")
        self.detail2 = label("", CAPTION_PX, name="tertiary")
        lay.addWidget(self.detail)
        lay.addWidget(self.detail2)
        lay.addStretch(1)

    def apply(self, t: TileView) -> None:
        self.title.setText(t.title.upper())
        self.value.setText(t.value)
        self.state_icon.set_icon(TONE_ICON[t.tone], self.p.tone(t.tone), inner=self.p.surface)
        self.detail.setText(t.detail)
        self.detail.setVisible(bool(t.detail))
        self.detail2.setText(t.detail2)
        self.detail2.setVisible(bool(t.detail2))


# --------------------------------------------------------------------------- action buttons
class ActionButton(QAbstractButton):
    """Large, calm action button: icon, title and a one-line explanation. Keyboard: Tab, Space, Enter."""

    def __init__(self, palette: Palette, kind: Icon, title: str, description: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        self.kind = kind
        self.title = title
        self.description = description
        self.setText(title)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(104)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_description(self, text: str) -> None:
        self.description = text
        self.setAccessibleDescription(text)
        self.update()

    def visible_texts(self) -> list[str]:
        return [self.title, self.description]

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, 104)

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.animateClick()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e: QPaintEvent) -> None:  # noqa: N802
        p = self.p
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        enabled = self.isEnabled()
        hover = enabled and self.underMouse()
        pressed = self.isDown()
        bg = QColor(p.surface_alt if (hover or pressed) else p.surface)
        if not enabled:
            bg = QColor(p.surface_alt)
        qp.setBrush(bg)
        border = QColor(p.accent if (hover and enabled) else p.border)
        qp.setPen(QPen(border, 1))
        qp.drawRoundedRect(r, RADIUS, RADIUS)
        if self.hasFocus():
            qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.setPen(QPen(QColor(p.focus), 2.5))
            qp.drawRoundedRect(r.adjusted(1.5, 1.5, -1.5, -1.5), RADIUS - 1, RADIUS - 1)
        icon_c = QColor(p.accent if enabled else p.text3)
        # icon in a soft disc
        disc = QRectF(r.left() + 20, r.center().y() - 24, 48, 48)
        soft = QColor(icon_c)
        soft.setAlphaF(0.12)
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(soft)
        qp.drawEllipse(disc)
        paint_icon(qp, self.kind, disc.adjusted(11, 11, -11, -11), icon_c)
        text_x = disc.right() + 18
        text_w = r.right() - 18 - text_x
        qp.setPen(QColor(p.text if enabled else p.text3))
        qp.setFont(font(BODY_PX + 2, W.DemiBold))
        fm_title = qp.fontMetrics()
        qp.setFont(font(CAPTION_PX))
        fm_desc = qp.fontMetrics()
        desc_rect = fm_desc.boundingRect(0, 0, int(text_w), 400, int(Qt.TextFlag.TextWordWrap), self.description)
        total = fm_title.height() + 4 + desc_rect.height()
        y = r.center().y() - total / 2
        qp.setFont(font(BODY_PX + 2, W.DemiBold))
        qp.drawText(QRectF(text_x, y, text_w, fm_title.height()), Qt.AlignmentFlag.AlignLeft, self.title)
        qp.setFont(font(CAPTION_PX))
        qp.setPen(QColor(p.text2 if enabled else p.text3))
        qp.drawText(QRectF(text_x, y + fm_title.height() + 4, text_w, desc_rect.height() + 2),
                    int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap), self.description)
        qp.end()


# --------------------------------------------------------------------------- 24 h timeline bar
SPAN_WORDS = {SpanKind.RECORDED: "Recorded", SpanKind.LIVE: "Recording now", SpanKind.GAP: "No recording",
              SpanKind.DAMAGED: "Damaged recording", SpanKind.NO_INFO: "No information"}


class TimelineBar(QWidget):
    """24 h bar: recorded = solid, no recording = diagonal hatching, no information = dotted.
    Patterns (not only colour) distinguish the kinds; a legend with words sits beside it."""

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        self.tl: Timeline | None = None
        self.tz: ZoneInfo = ZoneInfo("UTC")
        self.setMinimumHeight(86)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._ticks: list[str] = []

    def set_timeline(self, tl: Timeline, tz: ZoneInfo) -> None:
        self.tl, self.tz = tl, tz
        self.setAccessibleName("Recording timeline for the last 24 hours")
        self.update()

    def visible_texts(self) -> list[str]:
        return list(self._ticks)

    def brush_for(self, kind: SpanKind) -> tuple[QBrush, QColor]:
        p = self.p
        if kind in (SpanKind.RECORDED, SpanKind.LIVE):
            c = QColor(p.ok)
            return QBrush(c), c
        if kind is SpanKind.GAP:
            c = QColor(p.critical)
            return QBrush(c, Qt.BrushStyle.BDiagPattern), c
        if kind is SpanKind.DAMAGED:
            c = QColor(p.warning)
            return QBrush(c, Qt.BrushStyle.DiagCrossPattern), c
        c = QColor(p.neutral)
        return QBrush(c, Qt.BrushStyle.Dense6Pattern), c

    def paintEvent(self, _e: QPaintEvent) -> None:  # noqa: N802
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        p = self.p
        bar = QRectF(1, 6, self.width() - 2, 44)
        clip = QPainterPath()
        clip.addRoundedRect(bar, 8, 8)
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(QColor(p.surface_alt))
        qp.drawPath(clip)
        self._ticks = []
        if self.tl is not None and self.tl.window_end > self.tl.window_start:
            t0, t1 = self.tl.window_start, self.tl.window_end
            scale = bar.width() / (t1 - t0)
            qp.save()
            qp.setClipPath(clip)
            for s in self.tl.spans:
                x0 = bar.left() + (s.start - t0) * scale
                # interruptions stay visible however short (a 20 s gap is < 1 px at full width)
                w = max(3.0 if s.kind is SpanKind.GAP else 1.0, (s.end - s.start) * scale)
                rect = QRectF(x0, bar.top(), w, bar.height())
                brush, edge = self.brush_for(s.kind)
                if s.kind not in (SpanKind.RECORDED, SpanKind.LIVE):
                    tint = QColor(edge)
                    tint.setAlphaF(0.14)
                    qp.fillRect(rect, tint)
                qp.fillRect(rect, brush)
                if s.kind is SpanKind.GAP:
                    qp.fillRect(QRectF(x0, bar.top(), max(2.0, min(3.0, w)), bar.height()), edge)
            # motion markers: a strip along the top of the bar (also visible over recorded time)
            mc, edge = QColor(p.info), QColor(p.surface)
            for a, b in self.tl.motion:
                # a minute of motion is < 1 px on a 24 h bar: always at least MOTION_MIN_W wide
                w = max(MOTION_MIN_W, (b - a) * scale)
                x0 = bar.left() + (a - t0) * scale - (w - (b - a) * scale) / 2
                r = QRectF(x0, bar.top(), w, MOTION_STRIP_PX)
                qp.fillRect(r.adjusted(-1, 0, 1, 1), edge)
                qp.fillRect(r, mc)
            qp.restore()
            qp.setPen(QPen(QColor(p.border), 1))
            qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.drawPath(clip)
            # hour ticks every 3 h in the display timezone
            qp.setFont(font(SMALL_PX - 1))
            qp.setPen(QColor(p.text3))
            first = datetime.fromtimestamp(t0, self.tz).replace(minute=0, second=0, microsecond=0)
            ts = first.timestamp()
            while ts <= t1:
                dt = datetime.fromtimestamp(ts, self.tz)
                if ts >= t0 and dt.hour % 3 == 0:
                    x = bar.left() + (ts - t0) * scale
                    qp.drawLine(int(x), int(bar.bottom()) + 2, int(x), int(bar.bottom()) + 7)
                    txt = dt.strftime("%H:%M")
                    self._ticks.append(txt)
                    qp.drawText(QRectF(x - 30, bar.bottom() + 9, 60, 20), Qt.AlignmentFlag.AlignHCenter, txt)
                ts += 3600
        qp.end()


MOTION_STRIP_PX = 18.0
MOTION_MIN_W = 6.0


class LegendItem(QWidget):
    """Legend entry; ``kind=None`` is the motion marker."""

    def __init__(self, bar: TimelineBar, kind: SpanKind | None, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bar, self.kind = bar, kind
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.sw = QWidget()
        self.sw.setFixedSize(26, 16)
        self.sw.paintEvent = self._paint_swatch  # type: ignore[method-assign]
        lay.addWidget(self.sw)
        lay.addWidget(label(text, CAPTION_PX, wrap=False, name="secondary"))

    def _paint_swatch(self, _e: QPaintEvent) -> None:
        qp = QPainter(self.sw)
        r = QRectF(0.5, 0.5, 25, 15)
        if self.kind is None:
            qp.fillRect(r, QColor(self.bar.p.surface_alt))
            qp.fillRect(QRectF(9.5, 0.5, 7, 9), QColor(self.bar.p.info))
            qp.setPen(QPen(QColor(self.bar.p.border), 1))
            qp.drawRect(r)
            qp.end()
            return
        brush, edge = self.bar.brush_for(self.kind)
        tint = QColor(edge)
        tint.setAlphaF(0.14 if self.kind not in (SpanKind.RECORDED, SpanKind.LIVE) else 1.0)
        qp.fillRect(r, tint)
        qp.fillRect(r, brush)
        qp.setPen(QPen(QColor(self.bar.p.border), 1))
        qp.drawRect(r)
        qp.end()


SPAN_ICON = {SpanKind.RECORDED: (Icon.OK, Tone.OK), SpanKind.LIVE: (Icon.OK, Tone.OK),
             SpanKind.GAP: (Icon.CRITICAL, Tone.CRITICAL), SpanKind.DAMAGED: (Icon.WARNING, Tone.WARNING),
             SpanKind.NO_INFO: (Icon.NEUTRAL, Tone.NEUTRAL)}


def span_sentence(s: Span, tz: ZoneInfo, now: float) -> tuple[str, str]:
    """('No recording 14:05 – 14:09', 'Camera disconnected · 4 min · 19:05–19:09 UTC')."""
    sec = s.seconds < 120          # short spans need seconds, or "08:25 – 08:25" says nothing
    a = tf.when(s.start, tz, now, seconds=sec)
    if s.ongoing:
        title = f"{SPAN_WORDS[s.kind]} since {a}"
        if s.kind is SpanKind.LIVE:
            title = f"Recording now (since {a}, not yet saved)"
    else:
        b = tf.hm(s.end, tz, sec) if tf.day_prefix(s.end, tz, now) == tf.day_prefix(s.start, tz, now) \
            else tf.when(s.end, tz, now, sec)
        title = f"{SPAN_WORDS[s.kind]} {a} – {b}"
    parts = []
    if s.reason and s.kind is not SpanKind.LIVE:
        parts.append(s.reason[:1].upper() + s.reason[1:])
    parts.append(tf.duration(s.seconds) + (" so far" if s.ongoing else ""))
    parts.append(f"{tf.hm_utc(s.start, sec).replace(' UTC', '')}–{tf.hm_utc(s.end, sec)}")
    return title, " · ".join(parts)


class SpanRow(QFrame):
    def __init__(self, palette: Palette, s: Span, tz: ZoneInfo, now: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 10, 4, 10)
        lay.setSpacing(14)
        icon, tone = SPAN_ICON[s.kind]
        lay.addWidget(IconWidget(icon, palette.tone(tone), 22, inner=palette.surface), 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        title, detail = span_sentence(s, tz, now)
        self.title = label(title, BODY_PX, W.DemiBold if s.kind is not SpanKind.RECORDED else W.Medium)
        self.detail = label(detail, CAPTION_PX, name="secondary")
        col.addWidget(self.title)
        col.addWidget(self.detail)
        lay.addLayout(col, 1)
        self.setStyleSheet(f"SpanRow {{ border-bottom: 1px solid {palette.border}; }}")


def clear_layout(lay: QVBoxLayout | QHBoxLayout, keep: Callable[[QWidget], bool] | None = None) -> None:
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None and (keep is None or not keep(w)):
            w.setParent(None)
            w.deleteLater()
