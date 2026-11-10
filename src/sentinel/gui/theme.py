"""Visual system: one neutral palette, one accent, four status tones, one type scale.

Status colours follow the Okabe-Ito colour-blind-safe family (bluish green / blue / amber /
vermillion) and are never the only signal: every tone is paired with a distinct icon *shape*
(circle-check, circle-i, triangle, octagon) and with words.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtGui import QFont, QGuiApplication
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
    warning_banner_border: str = ""      # dark theme: the outline carries the colour, not a bright fill
    critical_banner_border: str = ""
    warning_banner_icon: str = ""        # icon colour on the banner (defaults to the banner text)
    critical_banner_icon: str = ""

    def tone(self, t: Tone) -> str:
        return {Tone.OK: self.ok, Tone.INFO: self.info, Tone.WARNING: self.warning,
                Tone.CRITICAL: self.critical, Tone.NEUTRAL: self.neutral}[t]

    def soft(self, t: Tone) -> str:
        """Tinted background for a tone (tone colour at low opacity over the surface)."""
        return mix(self.surface, self.tone(t), 0.16 if self.dark else 0.09)

    def banner(self, t: Tone) -> tuple[str, str]:
        if t is Tone.CRITICAL:
            return self.critical_banner_bg, self.critical_banner_text
        return self.warning_banner_bg, self.warning_banner_text

    def banner_border(self, t: Tone) -> str:
        b = self.critical_banner_border if t is Tone.CRITICAL else self.warning_banner_border
        return b or self.banner(t)[0]

    def banner_icon(self, t: Tone) -> str:
        i = self.critical_banner_icon if t is Tone.CRITICAL else self.warning_banner_icon
        return i or self.banner(t)[1]


def mix(base: str, over: str, a: float) -> str:
    b = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    o = [int(over[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * (1 - a) + y * a):02x}" for x, y in zip(b, o))


def relative_luminance(hex_color: str) -> float:
    """WCAG 2.x relative luminance of an sRGB colour '#rrggbb'."""
    h = hex_color.lstrip("#")
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG contrast ratio (1..21). AA for normal text is >= 4.5."""
    hi, lo = sorted((relative_luminance(fg), relative_luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def key_pairs(p: Palette) -> list[tuple[str, str, str]]:
    """(name, foreground, background) pairs that carry text or status meaning on screen."""
    pairs = [
        ("text on window", p.text, p.bg), ("text on card", p.text, p.surface),
        ("secondary text on card", p.text2, p.surface), ("secondary text on window", p.text2, p.bg),
        ("tertiary text on card", p.text3, p.surface), ("tertiary text on window", p.text3, p.bg),
        ("tertiary text on disabled button", p.text3, p.surface_alt),
        ("button text on accent", p.accent_text, p.accent), ("accent on card", p.accent, p.surface),
        ("warning banner text", p.warning_banner_text, p.warning_banner_bg),
        ("critical banner text", p.critical_banner_text, p.critical_banner_bg),
        ("warning banner icon", p.banner_icon(Tone.WARNING), p.warning_banner_bg),
        ("critical banner icon", p.banner_icon(Tone.CRITICAL), p.critical_banner_bg),
    ]
    for t in (Tone.OK, Tone.INFO, Tone.WARNING, Tone.CRITICAL, Tone.NEUTRAL):
        pairs.append((f"{t} status on card", p.tone(t), p.surface))
    for t in (Tone.OK, Tone.INFO, Tone.WARNING, Tone.CRITICAL):         # status area tints
        pairs += [(f"{t} icon on its tint", p.tone(t), p.soft(t)), (f"text on {t} tint", p.text, p.soft(t)),
                  (f"secondary text on {t} tint", p.text2, p.soft(t))]
    return pairs


LIGHT = Palette(
    dark=False, bg="#EDEFF2", surface="#FFFFFF", surface_alt="#F5F6F8", border="#D7DBE1",
    text="#14171B", text2="#4A5360", text3="#5F6875",
    accent="#2C56C9", accent_hover="#2349B0", accent_text="#FFFFFF", focus="#2C56C9",
    ok="#127A5A", info="#2160A6", warning="#8A5A00", critical="#B42318", neutral="#5F6875",
    warning_banner_bg="#F4BE45", warning_banner_text="#1D1708",
    critical_banner_bg="#B42318", critical_banner_text="#FFFFFF",
)

# Low-glare dark theme for dim rooms: dark neutral greys (not pure black), text at reduced luminance
# (~0.69 instead of white), muted status colours, and banners as dark tinted panels with a coloured
# outline instead of large bright fills. Every text/status pair meets WCAG AA (tests compute it).
DARK = Palette(
    dark=True, bg="#15181C", surface="#1C2026", surface_alt="#232830", border="#313842",
    text="#D4D9DF", text2="#A2ABB6", text3="#96A0AB",
    accent="#7C9CF0", accent_hover="#92ADF4", accent_text="#11141A", focus="#92ADF4",
    ok="#4FBF95", info="#79AEE6", warning="#E2AE4A", critical="#F07E72", neutral="#96A0AB",
    warning_banner_bg="#3A3016", warning_banner_text="#F4D38A", warning_banner_border="#B88A2E",
    warning_banner_icon="#E2AE4A",
    critical_banner_bg="#3D1D1B", critical_banner_text="#F5D0CB", critical_banner_border="#C2564B",
    critical_banner_icon="#F07E72",
)

APPEARANCES = ("auto", "light", "dark")          # "auto" = Follow Windows (the operating system setting)
APPEARANCE_LABELS = {"auto": "Follow Windows", "light": "Light", "dark": "Dark"}


def system_prefers_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # Qt older than 6.5
        return False


def pick_palette(mode: str = "auto", system_dark: bool | None = None) -> Palette:
    if mode == "dark":
        return DARK
    if mode == "light":
        return LIGHT
    return DARK if (system_prefers_dark() if system_dark is None else system_dark) else LIGHT


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
    QComboBox {{
        background: {p.surface}; color: {p.text}; border: 1px solid {p.border}; border-radius: 8px;
        padding: 6px 12px; font-size: {CAPTION_PX + 1}px; min-width: 150px;
    }}
    QComboBox:focus {{ border: 2px solid {p.focus}; padding: 5px 11px; }}
    QComboBox QAbstractItemView {{
        background: {p.surface}; color: {p.text}; border: 1px solid {p.border};
        selection-background-color: {p.accent}; selection-color: {p.accent_text}; padding: 4px;
    }}
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
