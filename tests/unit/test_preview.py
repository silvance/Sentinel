"""Live view (sentinel.preview, gui.app.liveview; owner request 2026-11-10): the recorder service
publishes frames on request; operators see them in the window without the camera password."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from conftest import make_config
from sentinel import preview

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


def test_frames_are_split_from_the_pipe() -> None:
    buf = bytearray(b"junk" + SOI + b"one" + EOI + SOI + b"two" + EOI + SOI + b"partial")
    assert preview.split_jpegs(buf) == [SOI + b"one" + EOI, SOI + b"two" + EOI]
    assert bytes(buf) == SOI + b"partial"                      # kept until the rest arrives
    buf += b"-end" + EOI
    assert preview.split_jpegs(buf) == [SOI + b"partial-end" + EOI] and not buf


def test_only_a_fresh_small_regular_file_turns_the_preview_on(tmp_path: Path) -> None:
    req = tmp_path / preview.REQUEST_NAME
    now = time.time()
    assert not preview.request_is_fresh(req, now)              # missing
    req.write_text("x")
    assert preview.request_is_fresh(req, now)
    os.utime(req, (now - 60, now - 60))
    assert not preview.request_is_fresh(req, now)              # stale: nobody is watching
    req.write_bytes(b"x" * (preview.REQUEST_MAX_BYTES + 1))
    assert not preview.request_is_fresh(req, time.time())      # too large: not ours
    req.unlink()
    req.mkdir()
    assert not preview.request_is_fresh(req, time.time())      # a folder


def test_command_uses_the_main_stream_even_with_a_second_stream(tmp_path: Path) -> None:
    """Owner decision: viewers see the full-quality main stream, not the motion detector's stream."""
    cfg = make_config(tmp_path, "rtsp://192.0.2.5:554/main", motion={"stream_path": "sub"})
    w = preview.PreviewWorker(cfg, "rtsp://viewer:pw@192.0.2.5:554/main")
    argv = w._argv()
    assert argv[argv.index("-i") + 1] == "rtsp://viewer:pw@192.0.2.5:554/main"
    assert "scale='min(1920,iw)':-2" in argv[argv.index("-vf") + 1]
    assert "mjpeg" in argv and "-rtsp_transport" in argv
    assert preview.preview_dir(cfg) == cfg.status_dir_path / "preview"


def test_published_frames_alternate_and_latest_names_the_newest(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    w = preview.PreviewWorker(cfg, "tcp://127.0.0.1:9", clock=lambda: 1_800_000_000.0)
    w.state = "live"
    w._publish(SOI + b"a" + EOI)
    w._publish(SOI + b"b" + EOI)
    doc = preview.read_latest(cfg)
    assert doc is not None and doc["seq"] == 2 and doc["state"] == "live"
    assert (preview.preview_dir(cfg) / doc["frame"]).read_bytes() == SOI + b"b" + EOI
    w._clear()
    assert preview.read_latest(cfg) is None


# ---------------------------------------------------------------------- window panel
def test_live_view_panel_requests_and_shows_frames(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from sentinel.gui.__main__ import prepare_qt_environment

    prepare_qt_environment()
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from sentinel.gui.app.base import Ctx
    from sentinel.gui.app.liveview import LiveView
    from sentinel.gui.theme import pick_palette

    cfg = make_config(tmp_path)
    t = {"now": 1_800_000_000.0}
    lv = LiveView(Ctx(cfg=cfg, cfg_path=None, admin=False, palette=pick_palette("light")), clock=lambda: t["now"])
    lv.resize(480, 320)
    lv.start()
    assert preview.request_is_fresh(preview.request_path(cfg), time.time())        # asked the recorder
    assert "Starting" in lv.message

    img = QImage(64, 36, QImage.Format.Format_RGB32)
    img.fill(QColor("green"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPEG")
    w = preview.PreviewWorker(cfg, "tcp://127.0.0.1:9", clock=lambda: t["now"])
    w.state = "live"
    w._publish(bytes(ba.data()))
    lv._tick()
    assert lv._pixmap is not None and lv.message.startswith("Live")
    t["now"] += 30                                                                  # no new frame for 30 s
    lv._tick()
    assert "not updating" in lv.message
    lv.stop()
    assert not lv.running()
    app.processEvents()


def test_no_camera_yet_says_so(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from dataclasses import replace

    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from sentinel.gui.app.base import Ctx
    from sentinel.gui.app.liveview import LiveView
    from sentinel.gui.theme import pick_palette

    cfg = make_config(tmp_path)
    cfg = replace(cfg, camera=replace(cfg.camera, pending=True, url=""))
    lv = LiveView(Ctx(cfg=cfg, cfg_path=None, admin=False, palette=pick_palette("light")))
    lv.start()
    assert "No camera" in lv.message and not lv.running()
