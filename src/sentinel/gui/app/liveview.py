"""Live view panel: shows the frames the recorder service publishes (sentinel.preview).

While the panel is visible it keeps the preview request fresh; the service then sends about two
frames a second. The window never connects to the camera itself. The caption always says how old the
picture is, so a frozen picture can never be mistaken for a live one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from ...clock import parse_iso_utc
from .. import textfmt as tf
from .base import Ctx

TICK_MS = 500
PICTURE_W = 560          # logical pixels; Windows display scaling enlarges it on high-DPI screens
REQUEST_EVERY_S = 5.0
STALE_S = 6.0            # older than this: the caption says the picture is old
NO_SERVICE_S = 15.0      # nothing at all after this long: the recorder is not providing a live view


class LiveView(QWidget):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None, clock: Callable[[], float] = time.time, *,
                 fixed: bool = True) -> None:
        """``fixed``: a 16:9 picture of PICTURE_W; False: the caller sizes the picture (Status page)."""
        super().__init__(parent)
        self.ctx, self.clock = ctx, clock
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if fixed:   # field report 2026-11-10: a stretching picture squeezed the camera form
            self.picture.setFixedSize(PICTURE_W, PICTURE_W * 9 // 16)
            self.picture.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        else:
            self.picture.setMinimumSize(320, 180)
            self.picture.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.picture.setStyleSheet(f"background: #000; color: #ccc; border-radius: 4px;")
        if fixed:
            lay.addWidget(self.picture, 0, Qt.AlignmentFlag.AlignLeft)
        else:
            lay.addWidget(self.picture, 1)
        self.caption = QLabel()
        self.caption.setObjectName("muted")
        self.caption.setWordWrap(True)
        lay.addWidget(self.caption)
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self._tick)
        self._pixmap: QPixmap | None = None
        self._seq = -1
        self._last_request = 0.0
        self._started = 0.0
        self.message = ""

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        cfg = self.ctx.cfg
        if cfg is None or cfg.camera.pending:
            self._show_text("No camera has been added yet.")
            return
        self._started = self.clock()
        self._last_request = 0.0
        self._show_text("Starting the live view…")
        self.timer.start()
        self._tick()

    def stop(self) -> None:
        self.timer.stop()               # the service stops the camera connection ~20 s later

    def running(self) -> bool:
        return self.timer.isActive()

    # ---------------------------------------------------------------- frames
    def _tick(self) -> None:
        from ...preview import preview_dir, read_latest, request_preview

        cfg = self.ctx.cfg
        if cfg is None:
            return
        now = self.clock()
        if now - self._last_request >= REQUEST_EVERY_S:
            self._last_request = now
            try:
                request_preview(cfg, self.clock)
            except OSError:
                self._show_text("The live view cannot be requested from this account (no access to the "
                                "operators' request folder). Contact technical support.")
                return
        doc = read_latest(cfg)
        if doc is None:
            if now - self._started > NO_SERVICE_S:
                self._show_text("No live view: the recorder is not running or is still starting.")
            return
        state = doc.get("state")
        frame = doc.get("frame")
        if state == "no_picture" and not frame:
            self._show_text("No picture from the camera. Check the camera address, power and network cable.")
            return
        if frame and doc.get("seq") != self._seq:
            pm = QPixmap()
            if pm.load(str(Path(preview_dir(cfg)) / str(frame))):
                self._seq = doc.get("seq", -1)
                self._pixmap = pm
                self._fit()
        if self._pixmap is None:
            self._show_text("Connecting to the camera…")
            return
        try:
            taken = parse_iso_utc(str(doc.get("taken_utc")))
        except ValueError:
            taken = None
        age = now - taken if taken is not None else None
        when = tf.hm(taken, self.ctx.tz, seconds=True) if taken is not None else "unknown"
        if age is not None and age > STALE_S:
            self.message = (f"Picture from {when} ({tf.duration(age)} ago) — the live view is not updating; "
                            "recording is not affected.")
            self.caption.setObjectName("warning")
        else:
            self.message = f"Live · {when} · about {doc.get('fps', 5)} pictures a second ({doc.get('source', '')}). " \
                           "For checking the picture only; it is not the recording."
            self.caption.setObjectName("muted")
        self.caption.setText(self.message)
        self.caption.style().polish(self.caption)

    def _show_text(self, text: str) -> None:
        self._pixmap = None
        self._seq = -1
        self.picture.setPixmap(QPixmap())
        self.picture.setText(text)
        self.caption.setText("")
        self.message = text

    def _fit(self) -> None:
        if self._pixmap is None:
            return
        self.picture.setPixmap(self._pixmap.scaled(self.picture.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                   Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e) -> None:  # noqa: ANN001, N802
        super().resizeEvent(e)
        self._fit()
