"""Recording timeline bar: recorded = solid green, not recorded = red hatching, damaged = cross-hatching,
no information = dots; motion periods as a blue strip along the top. Patterns, not only colour,
tell the kinds apart. Click a point to select the matching row in the table below it."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..ledger_view import SpanKind, Timeline
from ..theme import Palette

BAR_H = 30.0
MOTION_STRIP_PX = 9.0
MOTION_MIN_W = 6.0      # a minute of motion is < 1 px on a 24 h bar: always at least this wide


def tick_hours(span_s: float) -> int:
    hours = span_s / 3600
    return 1 if hours <= 8 else 3 if hours <= 26 else 12 if hours <= 80 else 24


class TimelineBar(QWidget):
    clicked_at = Signal(float)      # epoch under the mouse

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.p = palette
        self.tl: Timeline | None = None
        self.tz: ZoneInfo = ZoneInfo("UTC")
        self.setFixedHeight(int(BAR_H) + 26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ticks: list[str] = []

    def set_timeline(self, tl: Timeline, tz: ZoneInfo) -> None:
        self.tl, self.tz = tl, tz
        self.setAccessibleName("Recording timeline")
        self.update()

    def _bar(self) -> QRectF:
        return QRectF(1, 2, self.width() - 2, BAR_H)

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

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self.tl is None:
            return
        bar = self._bar()
        frac = min(1.0, max(0.0, (e.position().x() - bar.left()) / max(1.0, bar.width())))
        self.clicked_at.emit(self.tl.window_start + frac * (self.tl.window_end - self.tl.window_start))

    def paintEvent(self, _e: QPaintEvent) -> None:  # noqa: N802
        qp = QPainter(self)
        p = self.p
        bar = self._bar()
        qp.fillRect(bar, QColor(p.surface_alt))
        self.ticks = []
        if self.tl is not None and self.tl.window_end > self.tl.window_start:
            t0, t1 = self.tl.window_start, self.tl.window_end
            scale = bar.width() / (t1 - t0)
            qp.save()
            qp.setClipRect(bar)
            for s in self.tl.spans:
                x0 = bar.left() + (s.start - t0) * scale
                # interruptions stay visible however short
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
            mc, edge = QColor(p.info), QColor(p.surface)
            for a, b in self.tl.motion:
                w = max(MOTION_MIN_W, (b - a) * scale)
                x0 = bar.left() + (a - t0) * scale - (w - (b - a) * scale) / 2
                r = QRectF(x0, bar.top(), w, MOTION_STRIP_PX)
                qp.fillRect(r.adjusted(-1, 0, 1, 1), edge)
                qp.fillRect(r, mc)
            qp.restore()
            qp.setPen(QPen(QColor(p.border), 1))
            qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.drawRect(bar)
            step = tick_hours(t1 - t0)
            qp.setPen(QColor(p.text2))
            first = datetime.fromtimestamp(t0, self.tz).replace(minute=0, second=0, microsecond=0)
            ts = first.timestamp()
            while ts <= t1:
                dt = datetime.fromtimestamp(ts, self.tz)
                if ts >= t0 and dt.hour % step == 0:
                    x = bar.left() + (ts - t0) * scale
                    qp.drawLine(int(x), int(bar.bottom()) + 1, int(x), int(bar.bottom()) + 5)
                    txt = dt.strftime("%a %d" if step == 24 else "%a %H:%M" if step == 12 else "%H:%M")
                    self.ticks.append(txt)
                    qp.drawText(QRectF(x - 40, bar.bottom() + 6, 80, 18), Qt.AlignmentFlag.AlignHCenter, txt)
                ts += 3600
        qp.end()
