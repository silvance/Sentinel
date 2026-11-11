"""Look: the native Windows 11 style where available (Qt's "windows11"), otherwise Fusion with a
matching light/dark palette. Only a few custom touches (the section list, status colours); every
control is the system's own, at the system font size."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from ..theme import Palette, pick_palette


def native_style_name() -> str:
    keys = [k.lower() for k in QStyleFactory.keys()]
    if sys.platform == "win32" and "windows11" in keys:
        return "windows11"
    return "Fusion"


def qpalette(p: Palette) -> QPalette:
    q = QPalette()
    roles = {
        QPalette.ColorRole.Window: p.bg, QPalette.ColorRole.WindowText: p.text,
        QPalette.ColorRole.Base: p.surface, QPalette.ColorRole.AlternateBase: p.surface_alt,
        QPalette.ColorRole.Text: p.text, QPalette.ColorRole.Button: p.surface_alt,
        QPalette.ColorRole.ButtonText: p.text, QPalette.ColorRole.Highlight: p.accent,
        QPalette.ColorRole.HighlightedText: p.accent_text, QPalette.ColorRole.ToolTipBase: p.surface,
        QPalette.ColorRole.ToolTipText: p.text, QPalette.ColorRole.PlaceholderText: p.text3,
        QPalette.ColorRole.Link: p.accent, QPalette.ColorRole.BrightText: p.critical,
    }
    for role, col in roles.items():
        q.setColor(role, QColor(col))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        q.setColor(QPalette.ColorGroup.Disabled, role, QColor(p.text3))
    return q


def stylesheet(p: Palette) -> str:
    return f"""
    QWidget#navPanel {{ background: {p.surface}; border-right: 1px solid {p.border}; }}
    QListWidget#nav {{ background: transparent; border: none; outline: 0; }}
    QListWidget#nav::item {{ padding: 2px 6px; min-height: 26px; border-radius: 4px; }}
    QListWidget#nav::item:selected {{ background: {p.surface_alt}; color: {p.text};
                                      border-left: 3px solid {p.accent}; }}
    QListWidget#nav::item:hover:!selected {{ background: {p.surface_alt}; }}
    QLabel#pageTitle {{ font-weight: 600; }}
    QLabel#sectionTitle {{ font-weight: 600; color: {p.text2}; }}
    QFrame#sep {{ color: {p.border}; }}
    QLabel#muted {{ color: {p.text2}; }}
    QLabel#ok {{ color: {p.ok}; }}
    QLabel#warning {{ color: {p.warning}; }}
    QLabel#critical {{ color: {p.critical}; }}
    QFrame#summary {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 6px; }}
    QFrame#notice {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 4px; }}
    QFrame#noticeWarning {{ background: {p.surface}; border: 1px solid {p.warning}; border-radius: 4px; }}
    QPlainTextEdit#log {{ font-family: Consolas, 'Cascadia Mono', monospace; }}
    QGroupBox {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 6px;
                 margin-top: 20px; padding: 6px 8px 6px 8px; }}
    QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 2px; top: 2px;
                        padding: 0; font-weight: 600; }}
    """


def apply(app: QApplication, appearance: str) -> Palette:
    p = pick_palette(appearance)
    name = native_style_name()
    hints = QGuiApplication.styleHints()
    try:   # Qt 6.8+: tell the native style which scheme to draw
        hints.setColorScheme({"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(
            appearance, Qt.ColorScheme.Unknown))
    except AttributeError:
        pass
    app.setStyle(name)
    if name == "Fusion":
        app.setPalette(qpalette(p))
    f = QFont("Segoe UI Variable Text") if sys.platform == "win32" else app.font()
    if sys.platform == "win32" and not f.exactMatch():
        f = QFont("Segoe UI")
    f.setPointSizeF(10.0)                     # layout refinement 2026-11-10: was 9 pt
    app.setFont(f)
    app.setStyleSheet(stylesheet(p))
    from .icons import app_icon

    app.setWindowIcon(app_icon())
    return p
