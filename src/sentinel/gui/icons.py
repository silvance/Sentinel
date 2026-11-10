"""Vector icons drawn with QPainter on a 24-unit grid (no emoji, no stock Qt icons).

Status icons differ by SHAPE so they work without colour:
ok = circle with check, info = circle with "i", warning = triangle with "!",
critical = octagon with "!", neutral = circle with a dash.
"""

from __future__ import annotations

import math
from enum import StrEnum

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .viewmodel import Tone


class Icon(StrEnum):
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    NEUTRAL = "neutral"
    CAMERA = "camera"
    STORAGE = "storage"
    DRIVE = "drive"
    PREPARE = "prepare"
    TIMELINE = "timeline"
    HISTORY = "history"
    LOCK = "lock"
    BACK = "back"
    REPORT = "report"
    TEST = "test"


TONE_ICON = {Tone.OK: Icon.OK, Tone.INFO: Icon.INFO, Tone.WARNING: Icon.WARNING,
             Tone.CRITICAL: Icon.CRITICAL, Tone.NEUTRAL: Icon.NEUTRAL}


def _pen(color: QColor, w: float) -> QPen:
    p = QPen(color, w)
    p.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return p


def _octagon(cx: float, cy: float, r: float) -> QPainterPath:
    path = QPainterPath()
    for i in range(8):
        a = math.radians(22.5 + 45 * i)
        pt = QPointF(cx + r * math.cos(a), cy + r * math.sin(a))
        path.moveTo(pt) if i == 0 else path.lineTo(pt)
    path.closeSubpath()
    return path


def _triangle() -> QPainterPath:
    path = QPainterPath()
    path.moveTo(12, 2.6)
    path.lineTo(22.4, 20.6)
    path.lineTo(1.6, 20.6)
    path.closeSubpath()
    return path


def paint_icon(p: QPainter, kind: Icon, rect: QRectF, color: QColor, inner: QColor | None = None) -> None:
    """Draw ``kind`` into ``rect``. ``inner`` is the glyph colour on filled status shapes."""
    inner = inner or QColor("#FFFFFF")
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = min(rect.width(), rect.height()) / 24.0
    p.translate(rect.x() + (rect.width() - 24 * s) / 2, rect.y() + (rect.height() - 24 * s) / 2)
    p.scale(s, s)
    stroke = _pen(color, 1.8)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if kind in (Icon.OK, Icon.INFO, Icon.NEUTRAL):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(12, 12), 11, 11)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(_pen(inner, 2.4))
        if kind is Icon.OK:
            path = QPainterPath(QPointF(7, 12.4))
            path.lineTo(10.6, 16)
            path.lineTo(17.2, 8.6)
            p.drawPath(path)
        elif kind is Icon.INFO:
            p.drawLine(QPointF(12, 11), QPointF(12, 17))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(inner)
            p.drawEllipse(QPointF(12, 7.4), 1.45, 1.45)
        else:
            p.drawLine(QPointF(7.5, 12), QPointF(16.5, 12))
    elif kind in (Icon.WARNING, Icon.CRITICAL):
        p.setPen(_pen(color, 1.4))
        p.setBrush(color)
        if kind is Icon.WARNING:
            p.drawPath(_triangle())
            top, bottom, dot = 8.6, 14.4, 17.6
        else:
            p.drawPath(_octagon(12, 12, 11.4))
            top, bottom, dot = 6.6, 13.6, 17.2
        p.setPen(_pen(inner, 2.4))
        p.drawLine(QPointF(12, top), QPointF(12, bottom))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(inner)
        p.drawEllipse(QPointF(12, dot), 1.45, 1.45)
    elif kind is Icon.CAMERA:
        p.setPen(stroke)
        p.drawRoundedRect(QRectF(2.5, 7, 15, 9.5), 2.2, 2.2)          # body
        p.drawEllipse(QPointF(8.5, 11.75), 2.4, 2.4)                    # lens
        path = QPainterPath(QPointF(17.5, 10))
        path.lineTo(21.5, 8)
        path.lineTo(21.5, 15.5)
        path.lineTo(17.5, 13.5)
        p.drawPath(path)                                               # hood
        p.drawLine(QPointF(7, 16.5), QPointF(7, 20.5))                  # mount
        p.drawLine(QPointF(4.5, 20.5), QPointF(10, 20.5))
    elif kind is Icon.STORAGE:
        p.setPen(stroke)
        for y in (3.5, 10, 16.5):
            p.drawRoundedRect(QRectF(3, y, 18, 4.8), 1.5, 1.5)
            p.setBrush(color)
            p.drawEllipse(QPointF(17.6, y + 2.4), 0.9, 0.9)
            p.setBrush(Qt.BrushStyle.NoBrush)
    elif kind in (Icon.DRIVE, Icon.PREPARE):
        p.setPen(stroke)
        p.drawRoundedRect(QRectF(3, 12.5, 18, 8), 2, 2)
        p.setBrush(color)
        p.drawEllipse(QPointF(17.2, 16.5), 1.0, 1.0)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if kind is Icon.PREPARE:            # arrow into the drive
            p.drawLine(QPointF(12, 2.5), QPointF(12, 9.5))
            path = QPainterPath(QPointF(8.8, 6.6))
            path.lineTo(12, 9.8)
            path.lineTo(15.2, 6.6)
            p.drawPath(path)
        else:                               # eject mark above the drive
            path = QPainterPath(QPointF(12, 3))
            path.lineTo(16, 7.6)
            path.lineTo(8, 7.6)
            path.closeSubpath()
            p.drawPath(path)
            p.drawLine(QPointF(8, 10), QPointF(16, 10))
    elif kind is Icon.TIMELINE:
        p.setPen(stroke)
        p.drawEllipse(QPointF(12, 12), 9.5, 9.5)
        p.drawLine(QPointF(12, 12), QPointF(12, 6.5))
        p.drawLine(QPointF(12, 12), QPointF(16, 14.2))
    elif kind is Icon.HISTORY:
        p.setPen(stroke)
        p.drawRoundedRect(QRectF(4, 2.8, 16, 18.4), 2, 2)
        for y in (8, 12, 16):
            p.drawLine(QPointF(8, y), QPointF(16, y))
    elif kind is Icon.LOCK:
        p.setPen(stroke)
        p.drawRoundedRect(QRectF(4.5, 10.5, 15, 10), 2, 2)
        path = QPainterPath(QPointF(8, 10.5))
        path.lineTo(8, 7.5)
        path.arcTo(QRectF(8, 3.5, 8, 8), 180, -180)
        path.lineTo(16, 10.5)
        p.drawPath(path)
        p.drawLine(QPointF(12, 14.5), QPointF(12, 16.5))
    elif kind is Icon.BACK:
        p.setPen(_pen(color, 2.0))
        path = QPainterPath(QPointF(14.5, 5.5))
        path.lineTo(8, 12)
        path.lineTo(14.5, 18.5)
        p.drawPath(path)
    elif kind is Icon.REPORT:
        p.setPen(stroke)
        path = QPainterPath(QPointF(14, 2.8))
        path.lineTo(5, 2.8)
        path.lineTo(5, 21.2)
        path.lineTo(19, 21.2)
        path.lineTo(19, 7.8)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(12, 9.5), QPointF(12, 16.5))
        arrow = QPainterPath(QPointF(9, 13.8))
        arrow.lineTo(12, 16.8)
        arrow.lineTo(15, 13.8)
        p.drawPath(arrow)
    elif kind is Icon.TEST:
        p.setPen(stroke)
        path = QPainterPath(QPointF(9, 2.8))
        path.lineTo(15, 2.8)
        p.drawPath(path)
        flask = QPainterPath(QPointF(10, 2.8))
        flask.lineTo(10, 9)
        flask.lineTo(4.5, 19)
        flask.quadTo(4, 21.2, 6.5, 21.2)
        flask.lineTo(17.5, 21.2)
        flask.quadTo(20, 21.2, 19.5, 19)
        flask.lineTo(14, 9)
        flask.lineTo(14, 2.8)
        p.drawPath(flask)
        p.drawLine(QPointF(7.2, 15), QPointF(16.8, 15))
    p.restore()


class IconWidget(QWidget):
    """Fixed-size icon. Purely decorative: the accompanying words carry the meaning."""

    def __init__(self, kind: Icon, color: str, size: int = 24, inner: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind, self._color, self._inner = kind, QColor(color), QColor(inner) if inner else None
        self._size = size
        self.setFixedSize(QSize(size, size))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_icon(self, kind: Icon, color: str, inner: str | None = None) -> None:
        self._kind, self._color = kind, QColor(color)
        self._inner = QColor(inner) if inner else None
        self.update()

    @property
    def kind(self) -> Icon:
        return self._kind

    def paintEvent(self, _e: object) -> None:  # noqa: N802 (Qt API)
        p = QPainter(self)
        paint_icon(p, self._kind, QRectF(0, 0, self._size, self._size), self._color, self._inner)
        p.end()
