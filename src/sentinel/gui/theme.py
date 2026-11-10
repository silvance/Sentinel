"""Visual system: one neutral palette, one accent, four status tones, one type scale.

Status colours follow the Okabe-Ito colour-blind-safe family (bluish green / blue / amber /
vermillion) and are never the only signal: every tone is paired with a distinct icon *shape*
(circle-check, circle-i, triangle, octagon) and with words.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtCore import Qt

from .viewmodel import Tone


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text2: str
    text3: str
    accent: str
    accent_hover: str
    accent_text: str
    focus: str
    ok: str
    info: str
    warning: str
    critical: str
    neutral: str
    warning_banner_bg: str
    warning_banner_text: str
    critical_banner_bg: str
    critical_banner_text: str

    def tone(self, t: Tone) -> str:
        return {Tone.OK: self.ok, Tone.INFO: self.info, Tone.WARNING: self.warning,
                Tone.CRITICAL: self.critical, Tone.NEUTRAL: self.neutral}[t]

    def soft(self, t: Tone) -> str:
        """Tinted background for a tone (tone colour at low opacity over the surface)."""
        base, over = QColor(self.surface), QColor(self.tone(t))
        a = 0.16 if self.dark else 0.09
        r = round(base.red() * (1 - a) + over.red() * a)
        g = round(base.green() * (1 - a) + over.green() * a)
        b = round(base.blue() * (1 - a) + over.blue() * a)
        return QColor(r, g, b).name()

    def banner(self, t: Tone) -> tuple[str, str]:
        if t is Tone.CRITICAL:
            return self.critical_banner_bg, self.critical_banner_text
        return self.warning_banner_bg, self.warning_banner_text


LIGHT = Palette(
    dark=False, bg="#EDEFF2", surface="#FFFFFF", surface_alt="#F5F6F8", border="#D7DBE1",
    text="#14171B", text2="#4A5360", text3="#677080",
    accent="#2C56C9", accent_hover="#2349B0", accent_text="#FFFFFF", focus="#2C56C9",
    ok="#127A5A", info="#2160A6", warning="#946000", critical="#B42318", neutral="#677080",
    warning_banner_bg="#F4BE45", warning_banner_text="#1D1708",
    critical_banner_bg="#B42318", critical_banner_text="#FFFFFF",
)

DARK = Palette(
    dark=True, bg="#0E1114", surface="#171B20", surface_alt="#1E232A", border="#2B3139",
    text="#E7EAEE", text2="#AEB6C0", text3="#8C95A1",
    accent="#7097FF", accent_hover="#86A7FF", accent_text="#0E1114", focus="#86A7FF",
    ok="#3CC394", info="#72AEEE", warning="#F2B641", critical="#FF6E61", neutral="#8C95A1",
    warning_banner_bg="#F2B641", warning_banner_text="#1D1708",
    critical_banner_bg="#B7322A", critical_banner_text="#FFFFFF",
)


def system_prefers_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # Qt older than 6.5
        return False


def pick_palette(mode: str = "auto") -> Palette:
    if mode == "dark":
        return DARK
    if mode == "light":
        return LIGHT
    return DARK if system_prefers_dark() else LIGHT


# Type scale (pixels). One dominant size for the system state; everything else subordinate.
HERO_PX = 36
BANNER_PX = 23
TITLE_PX = 26
VALUE_PX = 22
BODY_PX = 17
CAPTION_PX = 15
SMALL_PX = 14
RADIUS = 12


def ui_families() -> list[str]:
    if sys.platform == "win32":
        return ["Segoe UI Variable Text", "Segoe UI", "Arial"]
    if sys.platform == "darwin":
        return ["SF Pro Text", "Helvetica Neue", "Arial"]
    return ["Inter", "Noto Sans", "Cantarell", "DejaVu Sans", "Liberation Sans"]


def mono_families() -> list[str]:
    return ["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Liberation Mono", "monospace"]


def font(px: int, weight: QFont.Weight = QFont.Weight.Normal, mono: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(mono_families() if mono else ui_families())
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def stylesheet(p: Palette) -> str:
    # Labels get their font from theme.font() (setFont); a font rule here would override it.
    return f"""
    QWidget {{ color: {p.text}; }}
    QMainWindow, #root {{ background: {p.bg}; }}
    #topbar {{ background: {p.surface}; border-bottom: 1px solid {p.border}; }}
    #card {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: {RADIUS}px; }}
    #page {{ background: transparent; }}
    QLabel {{ background: transparent; }}
    QLabel#secondary {{ color: {p.text2}; }}
    QLabel#tertiary {{ color: {p.text3}; }}
    QLabel#eyebrow {{ color: {p.text2}; }}
    QPushButton {{
        background: {p.surface}; color: {p.text}; border: 1px solid {p.border};
        border-radius: 8px; padding: 9px 18px; font-size: {BODY_PX}px; font-weight: 600;
    }}
    QPushButton:hover {{ background: {p.surface_alt}; }}
    QPushButton:focus {{ border: 2px solid {p.focus}; padding: 8px 17px; }}
    QPushButton:disabled {{ color: {p.text3}; }}
    QPushButton#primary {{ background: {p.accent}; color: {p.accent_text}; border: 1px solid {p.accent}; }}
    QPushButton#primary:hover {{ background: {p.accent_hover}; }}
    QPushButton#primary:focus {{ border: 2px solid {p.text}; }}
    QPushButton#link {{
        background: transparent; border: 1px solid transparent; color: {p.text2};
        padding: 6px 10px; font-weight: 500;
    }}
    QPushButton#link:hover {{ color: {p.text}; background: {p.surface_alt}; }}
    QPushButton#link:focus {{ border: 2px solid {p.focus}; padding: 5px 9px; }}
    QLineEdit {{
        background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 10px 12px;
        font-size: {TITLE_PX}px;
    }}
    QLineEdit:focus {{ border: 2px solid {p.focus}; padding: 9px 11px; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 4px; min-height: 32px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QTabWidget::pane {{ border: 1px solid {p.border}; border-radius: 8px; background: {p.surface}; top: -1px; }}
    QTabBar::tab {{
        background: transparent; color: {p.text2}; padding: 8px 16px; border: none;
        border-bottom: 2px solid transparent; font-size: {CAPTION_PX}px; font-weight: 600;
    }}
    QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; }}
    QTabBar::tab:focus {{ color: {p.accent}; }}
    QPlainTextEdit, QTableWidget {{
        background: {p.surface}; border: none; font-size: {SMALL_PX}px;
        selection-background-color: {p.accent}; selection-color: {p.accent_text};
    }}
    QHeaderView::section {{
        background: {p.surface_alt}; color: {p.text2}; border: none; border-bottom: 1px solid {p.border};
        padding: 6px 8px; font-size: {SMALL_PX}px; font-weight: 600;
    }}
    QToolTip {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border}; padding: 6px; }}
    """
