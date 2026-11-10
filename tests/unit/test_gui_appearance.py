"""Display preference (Light / Dark / Follow Windows): persistence, mapping, contrast, low-glare dark."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from gui_support import base_measurements, gui_config_dict, scenario, status_doc, write_doc  # noqa: E402
from sentinel.config import load_config_dict  # noqa: E402
from sentinel.gui import prefs  # noqa: E402
from sentinel.gui import theme  # noqa: E402
from sentinel.gui.theme import DARK, LIGHT, contrast_ratio, key_pairs, pick_palette, relative_luminance  # noqa: E402
from sentinel.gui.window import prepare_qt_environment  # noqa: E402

prepare_qt_environment()

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sentinel.gui.window import MainWindow, apply_app_style  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    apply_app_style(a)  # type: ignore[arg-type]
    return a  # type: ignore[return-value]


def _cfg(tmp: Path, test_camera: bool = False):  # noqa: ANN202
    cfg = load_config_dict(gui_config_dict(tmp, test_camera), base_dir=tmp)
    write_doc(cfg.data_path / "status.json", status_doc(base_measurements(simulated=test_camera), time.time()))
    return cfg


# ---------------------------------------------------------------------------- persistence
def test_preference_defaults_to_follow_windows(tmp_path: Path) -> None:
    p = tmp_path / "prefs" / "gui-preferences.json"
    assert prefs.load_appearance(p) == "auto"                       # missing
    p.parent.mkdir()
    p.write_text("{ not json", "utf-8")
    assert prefs.load_appearance(p) == "auto"                       # unreadable
    p.write_text(json.dumps({"appearance": "hidden"}), "utf-8")
    assert prefs.load_appearance(p) == "auto"                       # unknown value
    p.write_text(json.dumps(["dark"]), "utf-8")
    assert prefs.load_appearance(p) == "auto"                       # wrong shape


@pytest.mark.parametrize("mode", ["auto", "light", "dark"])
def test_preference_round_trip(tmp_path: Path, mode: str) -> None:
    p = tmp_path / "sub" / "gui-preferences.json"
    assert prefs.save_appearance(mode, p)
    assert prefs.load_appearance(p) == mode
    assert not list(p.parent.glob("*.partial"))


def test_unwritable_preference_fails_safe(tmp_path: Path) -> None:
    p = tmp_path / "is_a_dir"
    p.mkdir()
    assert prefs.save_appearance("dark", p) is False                # nothing raised, nothing else changed
    assert prefs.load_appearance(p) == "auto"
    with pytest.raises(ValueError):
        prefs.save_appearance("invisible", tmp_path / "x.json")


def test_default_location_is_the_users_profile_not_the_data_folder(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    import sys
    if sys.platform == "win32":
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        assert prefs.default_prefs_path() == tmp_path / "Roaming" / "Sentinel" / "gui-preferences.json"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        assert prefs.default_prefs_path() == tmp_path / "cfg" / "sentinel" / "gui-preferences.json"


# ---------------------------------------------------------------------------- mapping
def test_follow_windows_mapping() -> None:
    assert pick_palette("auto", system_dark=True) is DARK
    assert pick_palette("auto", system_dark=False) is LIGHT
    assert pick_palette("light", system_dark=True) is LIGHT
    assert pick_palette("dark", system_dark=False) is DARK


def test_follow_windows_in_window_and_live_change(app: QApplication, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: True)
    win = MainWindow(_cfg(tmp_path), appearance="auto", prefs_path=tmp_path / "p.json", export_available=False,
                     auto_refresh=False)
    try:
        assert win.p is DARK and win.dashboard.appearance.currentData() == "auto"
        monkeypatch.setattr(theme, "system_prefers_dark", lambda: False)
        win._system_scheme_changed()                                # Windows switched to light
        assert win.p is LIGHT and win.dashboard.appearance.currentData() == "auto"
    finally:
        win.close()


# ---------------------------------------------------------------------------- the control
def test_display_control_switches_persists_and_restores(app: QApplication, tmp_path: Path) -> None:
    store = tmp_path / "profile" / "gui-preferences.json"
    cfg = _cfg(tmp_path)
    win = MainWindow(cfg, appearance="light", prefs_path=store, export_available=False, auto_refresh=False)
    win.resize(1366, 768)
    win.show()
    app.processEvents()
    try:
        combo = win.dashboard.appearance
        assert combo.isVisibleTo(win) and combo.accessibleName() == "Display"
        assert [combo.itemText(i) for i in range(combo.count())] == ["Follow Windows", "Light", "Dark"]
        flags_before = win.windowFlags()
        combo.activated.emit(combo.findData("dark"))                # operator picks Dark
        app.processEvents()
        assert win.p is DARK and win.appearance == "dark"
        assert json.loads(store.read_text("utf-8"))["appearance"] == "dark"
        # still an ordinary, visible window: same flags, shown, not minimised, not a tool window
        assert win.isVisible() and not win.isMinimized() and win.windowFlags() == flags_before
        assert win.windowType() == Qt.WindowType.Window             # an ordinary top-level window
        assert win.dashboard.appearance.currentData() == "dark"
        assert win.view is not None and win.dashboard.hero.headline.text() == "Recording normally"
    finally:
        win.close()
    again = MainWindow(cfg, prefs_path=store, export_available=False, auto_refresh=False)
    try:
        assert again.p is DARK and again.dashboard.appearance.currentData() == "dark"     # restored
    finally:
        again.close()
    store.write_text("garbage", "utf-8")
    third = MainWindow(cfg, prefs_path=store, export_available=False, auto_refresh=False)
    try:
        assert third.appearance == "auto"                           # fail safe: Follow Windows
    finally:
        third.close()


def test_switching_relocks_admin_and_keeps_page(app: QApplication, tmp_path: Path) -> None:
    from sentinel.gui import admin_pin
    cfg = _cfg(tmp_path)
    admin_pin.set_pin(cfg.data_path, "482915")
    win = MainWindow(cfg, appearance="light", prefs_path=tmp_path / "p.json", export_available=False,
                     auto_refresh=False)
    try:
        win.show_timeline()
        win.set_appearance("dark")
        assert win.stack.currentWidget() is win.timeline_page
        win.show_admin()
        win.lock_page.pin.setText("482915")
        win.lock_page.btn.click()
        assert win.admin_unlocked()
        win.set_appearance("light")
        assert not win.admin_unlocked()                             # never stays unlocked across a rebuild
    finally:
        win.close()


# ---------------------------------------------------------------------------- contrast and glare
@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_text_and_status_contrast_meets_wcag_aa(palette: theme.Palette) -> None:
    low = [(name, round(contrast_ratio(fg, bg), 2)) for name, fg, bg in key_pairs(palette)
           if contrast_ratio(fg, bg) < 4.5]
    assert not low, low


def test_contrast_formula() -> None:
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast_ratio("#777777", "#777777") == pytest.approx(1.0)


def test_dark_theme_is_low_glare() -> None:
    assert DARK.bg.lower() != "#000000" and relative_luminance(DARK.bg) > 0.0      # not pure black
    assert relative_luminance(DARK.text) < 0.75                                    # dimmer than white
    # no large bright fills: banners are dark tinted panels, the colour is in the outline
    for fill in (DARK.critical_banner_bg, DARK.warning_banner_bg, DARK.surface, DARK.bg, DARK.surface_alt):
        assert relative_luminance(fill) < 0.05, fill
    assert relative_luminance(LIGHT.critical_banner_bg) > relative_luminance(DARK.critical_banner_bg)


def test_dark_critical_takeover_and_test_camera_strip(app: QApplication, tmp_path: Path) -> None:
    cfg = load_config_dict(gui_config_dict(tmp_path, True), base_dir=tmp_path)
    doc, _ = scenario("r003_critical_test_camera", time.time())
    write_doc(cfg.data_path / "status.json", doc)
    win = MainWindow(cfg, appearance="dark", prefs_path=tmp_path / "p.json", export_available=False,
                     auto_refresh=False)
    win.resize(1366, 768)
    win.show()
    app.processEvents()
    try:
        hero = win.dashboard.hero
        assert hero.takeover and DARK.critical_banner_bg.lower() in hero.styleSheet().lower()
        assert hero.message.text() == doc["alert"]["message"] and hero.chip.text() == "Error R-003"
        assert hero.icon.kind.value == "critical"                   # octagon shape + words, not colour alone
        strip = win.test_strip
        assert strip.isVisibleTo(win) and "NOT EVIDENCE" in strip.word.text()
        assert DARK.warning_banner_bg.lower() in strip.styleSheet().lower()
        assert contrast_ratio(DARK.warning_banner_text, DARK.warning_banner_bg) >= 4.5
    finally:
        win.close()
