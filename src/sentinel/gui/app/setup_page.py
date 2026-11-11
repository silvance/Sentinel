"""First-run setup in the window (replaces the console questions when a screen is available)."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QSpinBox)

from ...config import load_config
from ...paths import default_layout
from .base import Page, capped, muted
from .procs import Task

# Windows time zone names (time.tzname) -> IANA names, for the default answer only.
WINDOWS_ZONES = {
    "Eastern Standard Time": "America/New_York", "Eastern Daylight Time": "America/New_York",
    "Central Standard Time": "America/Chicago", "Central Daylight Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "Mountain Daylight Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix", "Pacific Standard Time": "America/Los_Angeles",
    "Pacific Daylight Time": "America/Los_Angeles", "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu", "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin", "Romance Standard Time": "Europe/Paris",
    "UTC": "UTC", "Coordinated Universal Time": "UTC",
}
COMMON_ZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles",
                "America/Anchorage", "Pacific/Honolulu", "Europe/London", "Europe/Berlin", "UTC"]


def local_zone_guess() -> str:
    for name in time.tzname:
        if name in WINDOWS_ZONES:
            return WINDOWS_ZONES[name]
    return "UTC"


class SetupPage(Page):
    key = "setup"
    title = "Set up this computer"
    description = "First run: the camera and where recordings are kept. Everything else uses tested defaults."

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self.layout_ = default_layout()
        if not ctx.admin:
            bar = QFrame()
            bar.setObjectName("noticeWarning")
            bl = QHBoxLayout(bar)
            bl.addWidget(QLabel("Setting up needs Windows administrator rights."), 1)
            b = QPushButton("Continue as administrator…")
            b.clicked.connect(ctx.unlock)
            bl.addWidget(b)
            self.body.addWidget(bar)

        cam = QGroupBox("Camera")
        f = QFormLayout(cam)
        self.later = QCheckBox("Add the camera later (on site, in Camera)")
        self.later.toggled.connect(self._later)
        f.addRow("", self.later)
        self.host = QLineEdit()
        self.host.setPlaceholderText("IP address or host name")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(554)
        hp = QHBoxLayout()
        hp.addWidget(self.host, 1)
        hp.addWidget(QLabel("Port"))
        hp.addWidget(self.port)
        f.addRow("Address", hp)
        self.path = QLineEdit()
        self.path.setPlaceholderText("from the camera documentation, e.g. Streaming/Channels/101")
        f.addRow("Stream path", self.path)
        self.user = QLineEdit()
        self.user.setPlaceholderText("view-only account; empty if none")
        f.addRow("Camera user name", self.user)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Camera password", self.password)
        trow = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test)
        trow.addWidget(self.test_btn)
        self.test_result = QLabel()
        self.test_result.setWordWrap(True)
        trow.addWidget(self.test_result, 1)
        f.addRow("", trow)
        self.body.addWidget(capped(cam))

        gen = QGroupBox("This computer")
        g = QFormLayout(gen)
        self.name = QLineEdit("Camera 1")
        g.addRow("Camera label for reports", self.name)
        self.tz = QComboBox()
        self.tz.setEditable(True)
        self.tz.addItems(COMMON_ZONES)
        self.tz.setEditText(local_zone_guess())
        g.addRow("Local time zone", self.tz)
        srow = QHBoxLayout()
        self.storage = QLineEdit(str(self.layout_.spool_dir.parent))
        srow.addWidget(self.storage, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        srow.addWidget(b)
        g.addRow("Recordings folder", srow)
        self.body.addWidget(capped(gen))
        self.body.addWidget(muted("Archive copying and retention are on (deployment defaults). After setup, install "
                                  "the recorder service in System › Service so recording starts with Windows."))
        row = QHBoxLayout()
        self.go = QPushButton("Set up this computer")
        self.go.setDefault(True)
        self.go.clicked.connect(self._setup)
        row.addWidget(self.go)
        row.addStretch(1)
        self.body.addLayout(row)
        self.body.addStretch(1)
        self._tasks: list[Task] = []
        for w in (self.later, self.host, self.port, self.path, self.user, self.password, self.test_btn, self.name,
                  self.tz, self.storage, b, self.go):
            w.setEnabled(ctx.admin)

    def _later(self, on: bool) -> None:
        for w in (self.host, self.port, self.path, self.user, self.password, self.test_btn):
            w.setEnabled(self.ctx.admin and not on)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder for recordings and the recording database",
                                             self.storage.text())
        if d:
            self.storage.setText(d)

    def _answers(self):  # noqa: ANN202
        from ...launcher.setup import SetupAnswers, check_timezone

        tz = check_timezone(self.tz.currentText())
        root = Path(self.storage.text().strip() or str(self.layout_.spool_dir.parent))
        default_root = self.layout_.spool_dir.parent
        common = dict(camera_name=self.name.text().strip() or "Camera 1", timezone=tz,
                      storage_root=None if root == default_root else root)
        if self.later.isChecked():
            return SetupAnswers("", "", camera_later=True, **common)
        return SetupAnswers(self.host.text().strip(), self.path.text().strip(), port=self.port.value(),
                            username=self.user.text().strip(), password=self.password.text(), **common)

    def _test(self) -> None:
        from ...credentials import Credentials
        from ...launcher.setcamera import test_camera
        from ...launcher.setup import SetupError, camera_url
        from ...paths import default_tool

        try:
            ans = self._answers()
            url = camera_url(ans)
            ffprobe = default_tool("ffprobe")
        except (SetupError, Exception) as exc:  # noqa: BLE001
            self.test_result.setText(str(exc))
            return
        creds = Credentials(ans.username, ans.password) if ans.username else None
        self.test_btn.setEnabled(False)
        self.test_result.setText("Asking the camera for video (up to 30 seconds)…")
        t = Task(lambda: test_camera(ffprobe, url, creds), self)

        def done(res: object, err: object) -> None:
            self.test_btn.setEnabled(True)
            if err is not None:
                self.test_result.setText(f"The test could not run: {err}")
            else:
                ok, msg = res  # type: ignore[misc]
                self.test_result.setText(("✔ " if ok else "✖ ") + msg)

        t.done.connect(done)
        self._tasks.append(t)
        t.start()

    def _setup(self) -> None:
        from ...launcher.setup import SetupError, write_setup

        try:
            ans = self._answers()
            target = getattr(self.window(), "setup_target", None)
            path, warnings = write_setup(ans, self.layout_, Path(target) if target else None)
            cfg = load_config(path)
        except (SetupError, Exception) as exc:  # noqa: BLE001
            self.problem(f"Setup was not completed:\n\n{exc}")
            return
        self.password.clear()
        note = "\n".join(f"Note: {w}" for w in warnings)
        self.info(f"This computer is set up.\n\nSettings: {path}\n"
                  + ("No camera yet: add it in Camera when on site.\n" if ans.camera_later else "")
                  + "Next: System › Service › Install the recorder service.\n" + (f"\n{note}" if note else ""))
        self.ctx.config_changed(cfg)
