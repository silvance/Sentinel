"""'Lock this computer' on the operator screen (sentinel.gui + sentinel.power.lock_workstation)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from gui_support import base_measurements, gui_config_dict, status_doc, write_doc  # noqa: E402
from sentinel.config import load_config_dict  # noqa: E402
from sentinel.gui.window import prepare_qt_environment  # noqa: E402

prepare_qt_environment()

from PySide6.QtWidgets import QApplication  # noqa: E402

from sentinel.gui.window import MainWindow, apply_app_style  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    apply_app_style(a)  # type: ignore[arg-type]
    return a  # type: ignore[return-value]


def _win(tmp: Path) -> MainWindow:
    cfg = load_config_dict(gui_config_dict(tmp, False), base_dir=tmp)
    write_doc(cfg.data_path / "status.json", status_doc(base_measurements(), time.time()))
    return MainWindow(cfg, appearance="light", prefs_path=tmp / "p.json", export_available=False, auto_refresh=False)


def test_lock_button_minimises_and_locks(app: QApplication, tmp_path: Path) -> None:
    win = _win(tmp_path)
    calls: list[bool] = []
    win._lock_workstation = lambda: calls.append(True) or True      # never lock the test machine
    win.show()
    app.processEvents()
    try:
        btn = win.dashboard.btn_lock
        assert btn.text() == "Lock this computer"
        assert "Recording continues" in btn.toolTip()
        assert btn.isVisibleTo(win) == (sys.platform == "win32")
        btn.click()
        app.processEvents()
        assert calls == [True] and win.isMinimized()
    finally:
        win.close()


def test_failed_lock_restores_the_window(app: QApplication, tmp_path: Path) -> None:
    win = _win(tmp_path)
    win._lock_workstation = lambda: False
    win.show()
    try:
        win.lock_computer()
        app.processEvents()
        assert not win.isMinimized()
    finally:
        win.close()
