"""Live view panel: shows the frames the recorder service publishes (sentinel.preview).

While the panel is visible it keeps the preview request fresh; the service then sends about two
frames a second. The window never connects to the camera itself. The caption always says how old the
picture is, so a frozen picture can never be mistaken for a live one.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ...clock import parse_iso_utc
from .. import textfmt as tf
from .base import Ctx

TICK_MS = 500
PICTURE_W = 560          # logical pixels; Windows display scaling enlarges it on high-DPI screens
MAX_PICTURE_H = 520      # tallest picture when the panel sizes itself (Status page)
REQUEST_EVERY_S = 5.0
STALE_S = 6.0            # older than this: the caption says the picture is old
NO_SERVICE_S = 15.0      # nothing at all after this long: the recorder is not providing a live view
STOPPED_TEXT = "No live view: the recorder is stopped, so nothing is read from the camera."


class LiveView(QWidget):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None, clock: Callable[[], float] = time.time, *,
                 fixed: bool = True) -> None:
        """``fixed``: a 16:9 picture of PICTURE_W; False: the caller sizes the picture (Status page)."""
        super().__init__(parent)
        self.ctx, self.clock = ctx, clock
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.fixed = fixed
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setWordWrap(True)
        if fixed:   # field report 2026-11-10: a stretching picture squeezed the camera form
            self.picture.setFixedSize(PICTURE_W, PICTURE_W * 9 // 16)
            self.picture.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        else:
            # As wide as the panel allows and 16:9 for that width, up to MAX_PICTURE_H (see resizeEvent).
            self.picture.setMinimumSize(320, 180)
            self.picture.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.picture.setStyleSheet("background: #000; color: #ccc; border-radius: 4px;")
        if fixed:
            lay.addWidget(self.picture, 0, Qt.AlignmentFlag.AlignLeft)
        else:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(self.picture, 1)
            row.addStretch(0)                # takes what is left when the picture is at its widest
            lay.addLayout(row)
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
        self._session: str | None = None     # one viewing session (the service pauses it after 10 minutes)
        self._hidden = False                 # stopped because the picture is not visible
        self._was_stopped = False            # the last tick found the recorder stopped
        self.picture.mousePressEvent = lambda _e: self._resume_if_paused()

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        cfg = self.ctx.cfg
        if cfg is None or cfg.camera.pending:
            self._show_text("No camera has been added yet.")
            return
        self._started = self.clock()
        self._last_request = 0.0
        self._session = secrets.token_hex(8)
        self._hidden = False
        self._show_text("Starting the live view…")
        self.timer.start()
        self._tick()

    def stop(self) -> None:
        self.timer.stop()
        self._stop_service()

    def _stop_service(self) -> None:
        """Tell the service the picture is no longer shown: it ends its camera decode within a second."""
        from ...preview import stop_preview

        if self.ctx.cfg is not None:
            try:
                stop_preview(self.ctx.cfg)
            except OSError:
                pass                        # best effort; it also stops ~20 s after the last request

    def _resume_if_paused(self) -> None:
        if self.running() and self.message.startswith("The live view was paused"):
            self._session = secrets.token_hex(8)     # a new viewing session
            self._last_request = 0.0
            self._started = self.clock()
            self._show_text("Starting the live view…")
            self._tick()

    def running(self) -> bool:
        return self.timer.isActive()

    # ---------------------------------------------------------------- frames
    def _tick(self) -> None:
        from ...preview import preview_dir, read_latest, request_preview

        cfg = self.ctx.cfg
        if cfg is None:
            return
        now = self.clock()
        win = self.window()
        if not self.isVisible() or (win is not None and win.isMinimized()):
            # Review 2026-11-12: minimized or locked (Lock computer minimizes the window): the service ends its
            # camera decode now; a new viewing session starts when the picture is shown again.
            if not self._hidden:
                self._hidden = True
                self._stop_service()
            return
        if self._hidden:
            self._hidden = False
            self._session = secrets.token_hex(8)
            self._last_request = 0.0
            self._started = now
        from ..viewmodel import recorder_stopped

        if recorder_stopped(self.ctx.read):
            # Field report 2026-11-19: after Stop recorder the black picture read as a lost camera. The
            # recorder is the only part that talks to the camera, so there is no picture while it is stopped.
            self._was_stopped = True
            if self.message != STOPPED_TEXT:
                self._show_text(STOPPED_TEXT)
            return
        if self._was_stopped:                    # started again: count the wait for a picture from now
            self._was_stopped = False
            self._started = now
            self._last_request = 0.0
            self._show_text("Starting the live view…")
        if now - self._last_request >= REQUEST_EVERY_S:
            self._last_request = now
            try:
                request_preview(cfg, self.clock, session=self._session)
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
        if state == "paused":
            self._show_text("The live view was paused after 10 minutes. Click here to show it again.")
            return
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
        if age is not None and (age > STALE_S or doc.get("live") is False):
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
        if not self.fixed:
            # GUI review 2026-11-17: the Status page set the height from the width the panel had BEFORE the new
            # layout, so the picture got black bars at the sides and the caption was drawn over it. The panel
            # sizes its own picture from the width it really has: 16:9, never wider than that height allows.
            h = max(180, min(int(self.width() * 9 / 16), MAX_PICTURE_H))
            if self.picture.minimumHeight() != h or self.picture.maximumHeight() != h:
                self.picture.setFixedHeight(h)
                self.picture.setMaximumWidth(max(320, int(h * 16 / 9)))
        self._fit()
