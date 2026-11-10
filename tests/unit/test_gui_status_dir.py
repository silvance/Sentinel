"""Integration (req/operator-gui x design/windows-service, docs/INTEGRATION.md): under the Windows
service layout the GUI reads status.json and the ledger snapshot from [storage] status_dir, never
the service-only data folder; the snapshot is opened immutable; the admin PIN lives in the
configuration folder."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui_support import gui_config_dict, populate_ledger, scenario, write_doc  # noqa: E402
from sentinel.config import load_config_dict  # noqa: E402
from sentinel.db import Ledger  # noqa: E402
from sentinel.gui.admin_pin import admin_pin_dir  # noqa: E402
from sentinel.gui.ledger_view import load_exports, open_ledger, snapshot  # noqa: E402


def _cfg(tmp: Path, status_dir: bool):  # noqa: ANN202
    d = gui_config_dict(tmp, test_camera=False)
    if status_dir:
        d["storage"]["status_dir"] = str(tmp / "status")
    return load_config_dict(d, base_dir=tmp, source=str(tmp / "config" / "sentinel.toml"))


def _snapshot_from_live(cfg) -> Path:  # noqa: ANN001
    populate_ledger(cfg.data_path / "sentinel.db", time.time())
    led = Ledger(cfg.data_path / "sentinel.db")
    try:
        led.write_snapshot(cfg.ledger_snapshot_path)
    finally:
        led.close()
    return cfg.ledger_snapshot_path


def test_admin_pin_folder_follows_the_layout(tmp_path: Path) -> None:
    assert admin_pin_dir(_cfg(tmp_path, status_dir=False)) == _cfg(tmp_path, False).data_path
    assert admin_pin_dir(_cfg(tmp_path, status_dir=True)) == (tmp_path / "config").resolve()


def test_snapshot_is_read_immutable_and_creates_no_wal_files(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, status_dir=True)
    snap = _snapshot_from_live(cfg)
    before = snap.read_bytes()
    with open_ledger(snap) as led:
        assert led is not None and led.immutable
    assert snapshot(snap, "CAM01") is not None
    ex = load_exports(snap)
    assert ex.available and ex.table == "export_batches"
    assert sorted(p.name for p in snap.parent.iterdir()) == ["ledger-snapshot.db"]   # no -wal / -shm
    assert snap.read_bytes() == before


def test_window_reads_the_status_folder_not_the_data_folder(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from sentinel.gui.window import MainWindow, prepare_qt_environment

    prepare_qt_environment()
    from PySide6.QtWidgets import QApplication

    from sentinel.gui.theme import LIGHT

    app = QApplication.instance() or QApplication([])
    cfg = _cfg(tmp_path, status_dir=True)
    _snapshot_from_live(cfg)
    doc, _ = scenario("recording_test_camera", time.time())
    write_doc(cfg.status_path, doc)
    win = MainWindow(cfg, LIGHT, export_available=True, auto_refresh=False)
    try:
        assert win.status_path == tmp_path / "status" / "status.json"
        assert win.db_path == tmp_path / "status" / "ledger-snapshot.db"
        assert win.gate.data_dir == (tmp_path / "config").resolve()       # PIN folder (config)
        win.refresh()
        app.processEvents()
        assert win.read is not None and win.read.doc is not None    # status found in the status folder
    finally:
        win.close()
