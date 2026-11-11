"""Icons: Windows' own icon font (Segoe Fluent Icons on Windows 11, Segoe MDL2 Assets on Windows 10),
so the window matches the system; and the Sentinel application icon, drawn in code (no image files).

Without the font (Linux, or a stripped system) the glyph icons are empty and text labels carry
the meaning: no icon ever stands alone.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPainterPath, QPen, QPixmap

# Code points shared by Segoe Fluent Icons and Segoe MDL2 Assets.
GLYPHS = {
    "status": "\ue80f",      # Home
    "recordings": "\ue714",  # Video
    "camera": "\ue722",      # Camera
    "schedule": "\ue787",    # Calendar
    "motion": "\ue805",      # Walk
    "storage": "\ueda2",     # HardDrive
    "drives": "\ue88e",      # USB
    "export": "\uede1",      # Export
    "system": "\ue713",      # Setting
    "ok": "\ue73e",          # CheckMark
    "warning": "\ue7ba",     # Warning
    "critical": "\uea39",    # ErrorBadge
    "info": "\ue946",        # Info
    "lock": "\ue72e",
    "save": "\ue74e",
    "refresh": "\ue72c",
    "shield": "\uea18",
    "play": "\ue768",
    "stop": "\ue71a",
    "add": "\ue710",
    "remove": "\ue738",
    "eject": "\ue74a",       # Up arrow: safe removal
    "document": "\ue8a5",
    "people": "\ue716",
    "swap": "\ue8ab",        # Switch
    "test": "\ue9d9",        # Diagnostic
    "erase": "\ue74d",       # Delete
    "time": "\ue823",        # Clock
}
FONT_FAMILIES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")


@lru_cache(maxsize=1)
def icon_family() -> str | None:
    families = set(QFontDatabase.families())
    return next((f for f in FONT_FAMILIES if f in families), None)


@lru_cache(maxsize=256)
def glyph_pixmap(name: str, color: str, size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    fam = icon_family()
    if fam is None or name not in GLYPHS:
        return pm
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    f = QFont(fam)
    f.setPixelSize(int(size * 0.8))
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, GLYPHS[name])
    p.end()
    return pm


def glyph(name: str, color: str, size: int = 16) -> QIcon:
    icon = QIcon()
    for s in (size, size * 2):
        icon.addPixmap(glyph_pixmap(name, color, s))
    return icon


# ---------------------------------------------------------------------- application icon
BRAND = "#1f5fbf"


def brand_pixmap(size: int) -> QPixmap:
    """A blue rounded square with a white camera lens: simple, neutral, readable at 16 px."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = size * 0.18
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(size * 0.04, size * 0.04, size * 0.92, size * 0.92), r, r)
    p.fillPath(bg, QColor(BRAND))
    c = QPointF(size / 2, size / 2)
    p.setPen(QPen(QColor("white"), max(1.5, size * 0.085)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(c, size * 0.27, size * 0.27)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("white"))
    p.drawEllipse(c, size * 0.11, size * 0.11)
    p.setBrush(QColor("#e5484d"))                     # recording dot
    p.drawEllipse(QPointF(size * 0.78, size * 0.22), size * 0.085, size * 0.085)
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(brand_pixmap(s))
    return icon


def write_ico(path: str) -> bool:
    """Write the application icon as a Windows .ico (used by the build for Sentinel.exe)."""
    return brand_pixmap(256).toImage().save(path, "ICO")
