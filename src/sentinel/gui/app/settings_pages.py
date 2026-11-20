"""Camera, Schedule and Motion pages: settings that are saved to the configuration file."""

from __future__ import annotations

import re
import time
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import QDate, QDateTime, Qt, QTime
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QTableWidget,
                               QToolButton, QVBoxLayout, QWidget)

from ...config import AppConfig
from .. import textfmt as tf
from .base import AdvancedRows, CommandPanel, Note, OutputLog, Page, ReadOnlyBar, capped, icon_button, muted
from .configedit import ConfigEditError, check_text, replace_sections, save_sections
from .procs import Task, sentinel_argv


NUMBER_FIELD_W = 170


def form(parent: QWidget | None = None) -> QFormLayout:
    """A settings form: fields start at the same left edge, and a short field (a number, a tick box) stays short
    and at that edge instead of being stretched or centred."""
    f = QFormLayout(parent) if parent is not None else QFormLayout()
    f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    f.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    f.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return f


def open_with_its_program(path: Any) -> bool:
    """Open a file with whatever program this computer uses for it (a video player for a clip)."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))


class SettingsPage(Page):
    """A page whose fields are saved to the configuration, then the recorder is restarted."""

    what = "these settings"

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self.body.addWidget(ReadOnlyBar(ctx, self.what))
        self.editors: list[QWidget] = []
        self.buttons = QHBoxLayout()
        self.save_btn = icon_button("Save and restart recorder", "save", ctx.palette.text)
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.save)
        self.revert_btn = icon_button("Revert", "refresh", ctx.palette.text)
        self.revert_btn.setToolTip("Show the saved settings again")
        self.revert_btn.clicked.connect(self.load)
        self.result = muted()
        self._tasks: list[Task] = []

    def finish_layout(self) -> None:
        self.buttons.addWidget(self.result, 1)
        self.buttons.addWidget(self.revert_btn)
        self.buttons.addWidget(self.save_btn)
        self.body.addLayout(self.buttons)
        for w in self.editors + [self.save_btn, self.revert_btn]:
            w.setEnabled(self.ctx.admin)
        self.load()

    def editor(self, w: QWidget) -> QWidget:
        if isinstance(w, QAbstractSpinBox):
            # A number is a short field (GUI review 2026-11-17: "2" sat in a box as wide as the form).
            w.setFixedWidth(NUMBER_FIELD_W)
        self.editors.append(w)
        return w

    def load(self) -> None:
        raise NotImplementedError

    def tables(self) -> dict[str, dict[str, Any]]:
        raise NotImplementedError

    def raw(self) -> dict[str, Any]:
        path = self.ctx.cfg_path
        try:
            return tomllib.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    def validate(self) -> AppConfig | None:
        path = self.ctx.cfg_path
        if path is None:
            return None
        try:
            new = replace_sections(Path(path).read_text(encoding="utf-8"), self.tables())
            return check_text(path, new)
        except (ConfigEditError, ValueError, OSError) as exc:
            self.problem(f"These settings cannot be used:\n\n{exc}\n\nNothing was changed.")
            return None

    def before_save(self, new_cfg: AppConfig) -> bool:
        """Last step before writing (e.g. credentials). False = stop, nothing written."""
        return True

    def save_failed(self) -> None:
        """Undo what before_save() did when the configuration could not be written."""

    def save(self) -> None:
        path = self.ctx.cfg_path
        new_cfg = self.validate()
        if new_cfg is None or path is None:
            return
        tables = self.tables()                   # before before_save() clears any typed password
        if not self.before_save(new_cfg):
            return
        try:
            new_cfg, backup = save_sections(path, tables)
        except (ConfigEditError, ValueError) as exc:
            self.save_failed()
            self.problem(f"The settings could not be saved:\n\n{exc}")
            return
        self.ctx.config_changed(new_cfg)
        self.load()                              # show what is saved now (review 2026-11-10)
        self.result.setText(f"Saved (previous settings kept as {backup.name}). Restarting the recorder…")
        # One restart at a time for the whole window (review 2026-11-10); then check that the recorder
        # works with the new settings, and put the last working ones back if not.
        self.ctx.restart(on_done=self.result.setText)
        self.ctx.after_change()


# --------------------------------------------------------------------------- camera
class CameraPage(SettingsPage):
    key = "camera"
    title = "Camera"
    description = "Camera address, stream and account."
    what = "the camera"

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        box = QGroupBox("Camera")
        f = form(box)
        self.pending = self.editor(QCheckBox("No camera yet (set everything else up; nothing is recorded)"))
        self.pending.toggled.connect(self._pending_changed)
        f.addRow(self.pending)
        self.name = self.editor(QLineEdit())
        f.addRow("Label for reports", self.name)
        self.cam_id = QLabel()
        f.addRow("Camera ID", self.cam_id)
        self.host = self.editor(QLineEdit())
        self.host.setPlaceholderText("IP address or host name, e.g. 192.168.1.64")
        self.port = self.editor(QSpinBox())
        self.port.setRange(1, 65535)
        hp = QHBoxLayout()
        hp.addWidget(self.host, 1)
        hp.addWidget(QLabel("Port"))
        hp.addWidget(self.port)
        f.addRow("Address", hp)
        self.path = self.editor(QLineEdit())
        self.path.setPlaceholderText("from the camera documentation, e.g. Streaming/Channels/101")
        f.addRow("Stream path", self.path)
        # Requirement change 2026-11-18: sound may be recorded. Off unless switched on here; whether it MAY be
        # recorded is the deployment's decision (docs/00 R5), which is why saving asks once.
        self.audio = self.editor(QCheckBox("Record the camera's sound with the video"))
        f.addRow(self.audio)
        self.audio_note = Note("Only where recording sound is authorised. The video is stored exactly as the camera "
                               "sends it; the sound is stored as AAC. Test connection says whether the camera sends "
                               "sound, and Status says whether it is being recorded.")
        f.addRow("", self.audio_note)
        self.audio_format = self.editor(QComboBox())
        self.audio_format.addItem("Convert to AAC (works with every camera)", "aac")
        self.audio_format.addItem("Keep the camera's own format (only cameras that send AAC)", "copy")
        self.transport = self.editor(QComboBox())
        self.transport.addItem("TCP (recommended)", "tcp")
        self.transport.addItem("UDP", "udp")
        self.secure = self.editor(QCheckBox("Encrypted stream (rtsps://; only if the camera supports it)"))
        # GUI review 2026-11-17: how the stream is carried is set once, if ever; opened by load() when it is not
        # the default, so a setting that matters is never out of sight.
        self.advanced = AdvancedRows(f, "How the video is carried and the sound stored; the defaults suit most cameras.")
        self.advanced.add("Transport", self.transport)
        self.advanced.add("", self.secure)
        self.advanced.add("Sound format", self.audio_format)
        self.body.addWidget(capped(box))

        acc = QGroupBox("Camera account (use a view-only account)")
        f2 = form(acc)
        self.user = self.editor(QLineEdit())
        self.user.setPlaceholderText("empty if the camera needs no login")
        f2.addRow("User name", self.user)
        self.password = self.editor(QLineEdit())
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        f2.addRow("Password", self.password)
        self.cred_note = Note()
        f2.addRow("", self.cred_note)
        self.acc_form = f2
        self.body.addWidget(capped(acc))

        row = QHBoxLayout()
        self.test_btn = self.editor(icon_button("Test connection", "test", ctx.palette.text))
        self.test_btn.clicked.connect(self.test)
        row.addWidget(self.test_btn)
        self.test_result = QLabel()
        self.test_result.setWordWrap(True)
        row.addWidget(self.test_result, 1)
        self.body.addLayout(row)
        # Owner request 2026-11-18: the live view's still pictures say nothing about sound. Ten seconds
        # straight from the camera, what arrived, and the clip to listen to (sentinel.avtest).
        av = QHBoxLayout()
        self.av_btn = self.editor(icon_button("Test picture and sound", "play", ctx.palette.text))
        self.av_btn.setToolTip("Records about ten seconds straight from the camera, says what picture and sound "
                               "arrived, and lets you play it. Not saved with the recordings.")
        self.av_btn.clicked.connect(self.av_test)
        av.addWidget(self.av_btn)
        self.av_play = QPushButton("Play the test clip")
        self.av_play.setVisible(False)
        self.av_play.clicked.connect(self._av_play)
        av.addWidget(self.av_play)
        self.av_result = QLabel()
        self.av_result.setWordWrap(True)
        self.av_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        av.addWidget(self.av_result, 1)
        self.body.addLayout(av)
        self.av_clip: Any = None
        self._av_forget()                        # a clip left by an earlier session goes now
        from .liveview import LiveView

        lbox = QGroupBox("Live view (main stream, from the recorder, with the saved settings)")
        ll = QVBoxLayout(lbox)
        self.live = LiveView(ctx)
        ll.addWidget(self.live)
        self.body.addWidget(capped(lbox))
        self.body.addStretch(1)
        self.tested_ok_for: str | None = None
        self.saved_user = ""
        self.finish_layout()

    def on_show(self) -> None:
        self.live.start()

    def on_hide(self) -> None:
        self.live.stop()
        self._av_forget()

    def _av_forget(self) -> None:
        """The test clip shows the scene, with sound, outside the recordings: it goes when the page is left."""
        from ... import avtest

        if self.av_clip is not None or avtest.clip_path().exists():
            avtest.delete_clip()
        self.av_clip = None
        self.av_play.setVisible(False)

    def av_test(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        try:
            url, creds = self._url(), self._creds()
        except Exception as exc:  # noqa: BLE001
            self.av_result.setText(str(exc))
            return
        from ... import avtest
        from ...credentials import url_with_credentials

        self._av_forget()
        self.av_btn.setEnabled(False)
        self.av_result.setText("Recording about ten seconds from the camera for the test. Make some noise in "
                               "front of it now…")
        transport = str(self.transport.currentData())
        lines: list[str] = []
        task = Task(lambda: avtest.av_test(cfg.tools.ffmpeg, cfg.tools.ffprobe, url_with_credentials(url, creds),
                                           transport, cfg.camera.input_format, avtest.DEFAULT_SECONDS, lines.append,
                                           self.audio.isChecked()), self)

        def done(res: object, err: object) -> None:
            self.av_btn.setEnabled(self.ctx.admin and not self.pending.isChecked())
            if err is not None:
                self.av_result.setText(f"The test could not run: {err}")
                return
            code, clip = res  # type: ignore[misc]
            # what the camera sent, without the two progress lines and the file's place
            shown = [ln for ln in lines if ln.startswith(("Picture:", "Sound:", "Sentinel is", "The camera", "No test",
                                                          "The test"))]
            self.av_result.setText(("✔ " if code == 0 else "✖ ") + "\n".join(shown or lines))
            self.av_result.setObjectName("ok" if code == 0 else "critical")
            self.av_result.style().polish(self.av_result)
            self.av_clip = clip
            self.av_play.setVisible(clip is not None)

        task.done.connect(done)
        self._tasks.append(task)
        task.start()

    def _av_play(self) -> None:
        if self.av_clip is None:
            return
        if not open_with_its_program(self.av_clip):
            self.av_result.setText(self.av_result.text() + f"\nNo program opened the clip. It is at {self.av_clip}")

    def _pending_changed(self, on: bool) -> None:
        for w in (self.host, self.port, self.path, self.transport, self.user, self.password, self.test_btn,
                  self.audio, self.audio_format, self.av_btn):
            w.setEnabled(self.ctx.admin and not on)

    def load(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        from ...launcher.setcamera import current_address

        host, path, port = current_address(cfg)
        scheme = urlsplit(cfg.camera.url).scheme if cfg.camera.url else "rtsp"
        if scheme == "rtsps" and urlsplit(cfg.camera.url).port is None:
            port = 322                           # RTSPS default port (review 2026-11-10)
        self.secure.setChecked(scheme == "rtsps")
        self.audio.setChecked(cfg.camera.include_audio)
        self.audio_format.setCurrentIndex(max(0, self.audio_format.findData(cfg.camera.audio_format)))
        self.advanced.set_open(self.advanced.is_open() or scheme == "rtsps" or cfg.camera.transport != "tcp"
                               or cfg.camera.audio_format != "aac")
        self.name.setText(cfg.camera.name)
        self.cam_id.setText(cfg.camera.id)
        self.host.setText(host)
        self.port.setValue(port)
        self.path.setText(path)
        self.transport.setCurrentIndex(max(0, self.transport.findData(cfg.camera.transport)))
        self.password.clear()
        self.saved_user = ""
        self.creds_unreadable = False
        if cfg.camera.credentials_file:
            try:
                from ...credentials import load_credentials

                creds = load_credentials(cfg.camera.credentials_file)
                self.saved_user = creds.username if creds else ""
                self.cred_note.setText("A password is saved. Leave Password empty to keep it.")
            except Exception:  # noqa: BLE001 - only administrators can read the saved account
                self.creds_unreadable = True
                self.cred_note.setText("A camera account is saved but cannot be read here; it is kept "
                                       "unless you enter a new user name and password.")
            self.password.setPlaceholderText("unchanged")
        else:
            self.cred_note.setText("")
            self.password.setPlaceholderText("")
        self.acc_form.setRowVisible(self.cred_note, bool(self.cred_note.text()))
        self.user.setText(self.saved_user)
        self.pending.setChecked(cfg.camera.pending)
        self._pending_changed(cfg.camera.pending)
        self.test_result.clear()
        self.result.clear()

    def _url(self) -> str:
        from ...launcher.setup import SetupError, check_host, check_stream_path

        try:
            host, path = check_host(self.host.text()), check_stream_path(self.path.text())
        except SetupError as exc:
            raise ValueError(str(exc)) from None
        scheme = "rtsps" if self.secure.isChecked() else "rtsp"
        return f"{scheme}://{f'[{host}]' if ':' in host else host}:{self.port.value()}/{path}"

    def _creds(self) -> Any:
        from ...credentials import Credentials, load_credentials

        user = self.user.text().strip()
        if not user:
            return None
        if self.password.text():
            return Credentials(user, self.password.text())
        cfg = self.cfg
        if cfg is not None and cfg.camera.credentials_file and user == self.saved_user:
            return load_credentials(cfg.camera.credentials_file)
        raise ValueError("Enter the password for this camera account.")

    def test(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        try:
            url, creds = self._url(), self._creds()
        except Exception as exc:  # noqa: BLE001
            self.test_result.setText(str(exc))
            return
        from ...launcher.setcamera import test_camera

        self.test_btn.setEnabled(False)
        self.test_result.setText("Asking the camera for video (up to 30 seconds)…")
        transport = str(self.transport.currentData())
        task = Task(lambda: test_camera(cfg.tools.ffprobe, url, creds, transport), self)

        def done(res: object, err: object) -> None:
            self.test_btn.setEnabled(True)
            if err is not None:
                self.test_result.setText(f"The test could not run: {err}")
                return
            ok, msg = res  # type: ignore[misc]
            self.test_result.setText(("✔ " if ok else "✖ ") + msg)
            self.test_result.setObjectName("ok" if ok else "critical")
            self.test_result.style().polish(self.test_result)
            self.tested_ok_for = url if ok else None

        task.done.connect(done)
        self._tasks.append(task)
        task.start()

    def tables(self) -> dict[str, dict[str, Any]]:
        from ...launcher.setcamera import new_camera_table
        from ...paths import SECRET_NAME

        cfg, path = self.cfg, self.ctx.cfg_path
        old = dict(self.raw().get("camera", {}))
        name = self.name.text().strip() or "Camera 1"
        if self.pending.isChecked():
            return {"camera": {"id": old.get("id", "CAM01"), "name": name, "url": "", "pending": True}}
        url = self._url()
        cred_path = None
        keep_unreadable = getattr(self, "creds_unreadable", False) and not self.user.text().strip()
        if self.user.text().strip() or keep_unreadable:
            # A saved account this window cannot read is kept, never dropped (review 2026-11-10).
            cred_path = Path(cfg.camera.credentials_file) if cfg and cfg.camera.credentials_file else \
                Path(path).parent / SECRET_NAME
        table = new_camera_table(old, url, cred_path, name)
        table["transport"] = str(self.transport.currentData())
        table["include_audio"] = self.audio.isChecked()
        # The default is not written: a settings file without the key is read by every version (0.3.19 and
        # earlier refuse a key they do not know, which would stop "go back to the previous version").
        table.pop("audio_format", None)
        if str(self.audio_format.currentData()) != "aac":
            table["audio_format"] = str(self.audio_format.currentData())
        return {"camera": table}

    def before_save(self, new_cfg: AppConfig) -> bool:
        self._old_secret: tuple[Path, bytes | None] | None = None
        if self.pending.isChecked():
            return True
        if getattr(self, "creds_unreadable", False) and not self.user.text().strip():
            creds_needed = False
        else:
            creds_needed = True
        try:
            url = self._url()
            creds = self._creds() if creds_needed else None
        except Exception as exc:  # noqa: BLE001
            self.problem(str(exc))
            return False
        if self.tested_ok_for != url and QMessageBox.question(
                self, "Sentinel", "The camera has not answered a connection test with these settings.\n\n"
                "Save anyway?") != QMessageBox.StandardButton.Yes:
            return False
        cfg = self.cfg
        if self.audio.isChecked() and not (cfg is not None and cfg.camera.include_audio) and QMessageBox.question(
                self, "Sentinel", "Switch sound recording ON?\n\nFrom now on the camera's sound is recorded with the "
                "video. Do this only where recording sound is authorised for this deployment. The change is "
                "written to the recording log.") != QMessageBox.StandardButton.Yes:
            return False
        if creds is not None and self.password.text():
            from ...launcher.control import select_control
            from ...launcher.setcamera import SetCameraError, write_credentials

            cfg_path = self.ctx.cfg_path
            assert cfg_path is not None
            service = bool(getattr(select_control(cfg_path, new_cfg), "service_managed", False))
            secret = Path(new_cfg.camera.credentials_file or "")
            try:   # kept so a failed configuration save can put the old account back (review 2026-11-10)
                self._old_secret = (secret, secret.read_bytes() if secret.is_file() else None)
            except OSError:
                self._old_secret = None
            try:
                write_credentials(secret, creds.username, creds.password, service)
            except SetCameraError as exc:
                self.problem(f"{exc}\n\nNothing was changed.")
                return False
        self.password.clear()
        return True

    def save_failed(self) -> None:
        old = getattr(self, "_old_secret", None)
        if not old:
            return
        path, data = old
        try:
            if data is None:
                path.unlink(missing_ok=True)
            else:
                from ...selfcheck import write_secret_like     # its own permissions, not the folder's

                write_secret_like(path, data, path if path.exists() else None)
        except OSError as exc:
            self.problem(f"The previous camera account could not be put back ({exc}). Enter the camera "
                         "account again and save.")


# --------------------------------------------------------------------------- schedule
ACTIONS = [("record", "Record"), ("pause", "Pause (do not record)"), ("motion", "Record on motion")]
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
GROUPS = {"weekdays": set(DAY_KEYS[:5]), "weekend": set(DAY_KEYS[5:]), "daily": set(DAY_KEYS)}


def _action_combo(value: str) -> QComboBox:
    c = QComboBox()
    # Wide enough for the longest choice (GUI review 2026-11-17: "Pause (do not recorc").
    c.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    c.setMinimumContentsLength(max(len(t) for _k, t in ACTIONS) + 2)
    for k, t in ACTIONS:
        c.addItem(t, k)
    c.setCurrentIndex(max(0, c.findData(value)))
    return c


def days_from_config(value: Any) -> set[str]:
    items = [value] if isinstance(value, str) else list(value or [])
    out: set[str] = set()
    for it in items:
        k = str(it).strip().lower()
        out |= GROUPS.get(k, {k[:3]} if k[:3] in DAY_KEYS else set())
    return out


def days_to_config(days: set[str]) -> list[str]:
    """Compact form for the file: "daily", "weekdays", "weekend", else the day names in order."""
    for name, group in GROUPS.items():
        if days == group:
            return [name]
    return [d for d in DAY_KEYS if d in days]


class DaysPicker(QWidget):
    """Seven toggle buttons, Mon to Sun: no typing of day names (owner request 2026-11-10)."""

    def __init__(self, days: set[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(2)
        self.buttons: dict[str, QToolButton] = {}
        for key, text in zip(DAY_KEYS, DAY_LABELS):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setChecked(key in days)
            b.setAutoRaise(False)
            b.setToolTip(f"{text}: on or off")
            lay.addWidget(b)
            self.buttons[key] = b

    def days(self) -> set[str]:
        return {k for k, b in self.buttons.items() if b.isChecked()}


TIME_STEP_MIN = 15
MIDNIGHT = "24:00"
MIDNIGHT_TEXT = "Midnight"
_TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3])[:.]?([0-5]\d)(?::00)?\s*$")


def normal_time(text: str) -> str | None:
    """'7:30' / '0730' / '07.30' / '07:30:00' -> '07:30'; None if it is not a time of day (to the minute)."""
    m = _TIME_RE.match(text or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


class TimeCombo(QComboBox):
    """A time of day picked from a list (every 15 minutes) instead of a spin box (owner request
    2026-11-11). Any other time can still be typed (07:43). ``end=True``: an "until" time, whose list
    ends with "Midnight", the end of the day (stored as 24:00)."""

    def __init__(self, value: str = "00:00", *, end: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.end = end
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMaxVisibleItems(12)
        self.setMinimumContentsLength(8)
        self.setToolTip("Pick a time from the list, or type one (for example 07:45)")
        for m in range(0, 24 * 60, TIME_STEP_MIN):
            t = f"{m // 60:02d}:{m % 60:02d}"
            if not (end and t == "00:00"):
                self.addItem(t, t)
        if end:
            self.addItem(MIDNIGHT_TEXT, MIDNIGHT)
        self.set_value(value)

    def set_value(self, text: str) -> None:
        t = (text or "").strip()
        if self.end and t in (MIDNIGHT, "00:00", MIDNIGHT_TEXT):
            self.setCurrentIndex(self.findData(MIDNIGHT))
            return
        norm = normal_time(t)
        if norm is None:
            # Review 2026-11-12: keep what the settings file says (shown as is); it used to become 00:00.
            self.setEditText(t)
            return
        i = self.findData(norm)
        if i >= 0:
            self.setCurrentIndex(i)
        else:
            self.setEditText(norm)

    def value(self) -> str:
        text = self.currentText().strip()
        if self.end and text in (MIDNIGHT_TEXT, MIDNIGHT, "00:00"):
            return MIDNIGHT
        norm = normal_time(text)
        if norm is not None:
            return norm
        # Review 2026-11-12: never put another time in its place without saying so.
        raise ValueError(f"'{text}' is not a time of day: use the 24-hour clock, for example 07:45")


class DateTimePicker(QWidget):
    """A date from a calendar and a time from the list (owner request 2026-11-11: no spin boxes)."""

    def __init__(self, value: QDateTime, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(4)
        self.date = QDateEdit(value.date())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("ddd d MMM yyyy")
        self.time = TimeCombo(value.time().toString("HH:mm"))
        lay.addWidget(self.date, 1)
        lay.addWidget(self.time)

    def dateTime(self) -> QDateTime:  # noqa: N802 - same name as QDateTimeEdit
        hh, _, mm = self.time.value().partition(":")
        return QDateTime(self.date.date(), QTime(int(hh) % 24, int(mm)))


def _row_remove_button(table: QTableWidget, column: int, tip: str) -> QToolButton:
    """A Remove button on its own row: it is always clear which period goes (owner request
    2026-11-11: with "Remove selected" it was not visible which row was selected)."""
    b = QToolButton()
    b.setText("✕")                      # a cross: remove this row
    b.setToolTip(tip)
    b.setAccessibleName(tip)
    b.setAutoRaise(True)

    def remove() -> None:
        for r in range(table.rowCount()):
            if table.cellWidget(r, column) is b:
                table.removeRow(r)
                return

    b.clicked.connect(remove)
    return b


def parse_date_value(text: str, is_end: bool) -> QDateTime:
    """A schedule date as the recorder reads it (``YYYY-MM-DD``, ``YYYY-MM-DD HH:MM``, also with ``T``, one-digit
    hours, seconds, and 24:00 as an end); a date alone as an end means the END of that day. Raises ValueError
    for anything else or for seconds the page cannot show (review 2026-11-12: such values became "now")."""
    from ...schedule import ScheduleError, _datetime

    try:
        d = _datetime((text or "").strip(), "date", is_end)
    except ScheduleError as exc:
        raise ValueError(str(exc)) from None
    if d.second:
        raise ValueError(f"{text!r} has seconds; this page shows dates to the minute")
    return QDateTime(QDate(d.year, d.month, d.day), QTime(d.hour, d.minute))


def date_value(dt: QDateTime) -> str:
    return dt.toString("yyyy-MM-dd HH:mm")


def _schedule_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(36)
    # Rows are made of controls; there is nothing to select (each row has its own Remove button).
    t.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    t.setShowGrid(False)
    return t


def _empty_note(t: QTableWidget, text: str) -> QLabel:
    """Shown in place of a schedule table that has no rows (GUI review 2026-11-17: an empty grid said nothing)."""
    note = muted(text)

    def sync(*_a: object) -> None:
        empty = t.rowCount() == 0
        t.setVisible(not empty)
        note.setVisible(empty)

    t.model().rowsInserted.connect(sync)
    t.model().rowsRemoved.connect(sync)
    sync()
    return note


class WeeklyTable(QWidget):
    """Weekly periods: action, days (toggle buttons), from and until (time lists), Remove."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.t = _schedule_table(["Action", "Days", "From", "Until", ""])
        lay.addWidget(self.t)
        self.empty_note = _empty_note(self.t, "No weekly periods yet. Press Add, or use Quick setup above.")
        lay.addWidget(self.empty_note)
        row = QHBoxLayout()
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(lambda: self.add("record", set(DAY_KEYS[:5]), "07:00", "18:00"))
        row.addWidget(self.add_btn)
        row.addWidget(muted("✕ removes a period. An end before the start runs past midnight "
                            "(for example 22:00 until 06:00)."), 1)
        lay.addLayout(row)

    def clear(self) -> None:
        self.t.setRowCount(0)

    def add(self, action: str, days: set[str], start: str, end: str) -> None:
        r = self.t.rowCount()
        self.t.insertRow(r)
        self.t.setCellWidget(r, 0, _action_combo(action))
        self.t.setCellWidget(r, 1, DaysPicker(days))
        self.t.setCellWidget(r, 2, TimeCombo(start))
        self.t.setCellWidget(r, 3, TimeCombo(end, end=True))
        self.t.setCellWidget(r, 4, _row_remove_button(self.t, 4, "Remove this weekly period"))
        self.t.resizeColumnsToContents()

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for r in range(self.t.rowCount()):
            action, days = self.t.cellWidget(r, 0), self.t.cellWidget(r, 1)
            start, end = self.t.cellWidget(r, 2), self.t.cellWidget(r, 3)
            picked = days.days() if isinstance(days, DaysPicker) else set()
            if not picked:
                continue
            out.append({"action": str(action.currentData()) if isinstance(action, QComboBox) else "record",
                        "days": days_to_config(picked),
                        "start": start.value() if isinstance(start, TimeCombo) else "00:00",
                        "end": end.value() if isinstance(end, TimeCombo) else MIDNIGHT})
        return out

    def set_enabled(self, on: bool) -> None:
        self.t.setEnabled(on)
        self.add_btn.setEnabled(on)


class DatesTable(QWidget):
    """One-off periods (holidays, leave): action, from, until (calendar date and time list), Remove."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.t = _schedule_table(["Action", "From", "Until", ""])
        lay.addWidget(self.t)
        self.empty_note = _empty_note(self.t, "No particular dates yet. Press Days off… for leave or holidays.")
        lay.addWidget(self.empty_note)
        row = QHBoxLayout()
        self.leave_btn = QPushButton("Days off…")
        self.leave_btn.setToolTip("Stop recording for whole days (leave, holidays); recording starts again by itself")
        self.add_btn = QPushButton("Add period")
        self.add_btn.clicked.connect(self._add_default)
        for b in (self.leave_btn, self.add_btn):
            row.addWidget(b)
        row.addWidget(muted("These take priority over the weekly periods. The recorder keeps running and goes "
                            "back to the weekly plan by itself when a period ends."), 1)
        lay.addLayout(row)

    def _add_default(self) -> None:
        start = QDateTime(QDate.currentDate().addDays(1), QTime(0, 0))
        self.add("pause", start, start.addDays(1))

    def clear(self) -> None:
        self.t.setRowCount(0)

    def add(self, action: str, start: QDateTime, end: QDateTime) -> None:
        r = self.t.rowCount()
        self.t.insertRow(r)
        self.t.setCellWidget(r, 0, _action_combo(action))
        self.t.setCellWidget(r, 1, DateTimePicker(start))
        self.t.setCellWidget(r, 2, DateTimePicker(end))
        self.t.setCellWidget(r, 3, _row_remove_button(self.t, 3, "Remove this period"))
        self.t.resizeColumnsToContents()

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for r in range(self.t.rowCount()):
            action, start, end = (self.t.cellWidget(r, c) for c in range(3))
            if not isinstance(start, DateTimePicker) or not isinstance(end, DateTimePicker):
                continue
            out.append({"action": str(action.currentData()) if isinstance(action, QComboBox) else "pause",
                        "start": date_value(start.dateTime()), "end": date_value(end.dateTime())})
        return out

    def set_enabled(self, on: bool) -> None:
        self.t.setEnabled(on)
        for b in (self.leave_btn, self.add_btn):
            b.setEnabled(on)


class DaysOffDialog(QDialog):
    """"He is on leave for 6 days in December": no recording from the first day off until the day he
    is back; nothing is shut down, recording starts again by itself (owner request 2026-11-10)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Days off")
        self.setMinimumWidth(460)               # "Wednesday 18 November 2026" was cut off (GUI review 2026-11-17)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        today = QDate.currentDate()
        self.first = QDateEdit(today.addDays(1))
        self.back = QDateEdit(today.addDays(7))
        for d in (self.first, self.back):
            d.setCalendarPopup(True)
            d.setDisplayFormat("dddd d MMMM yyyy")
        f.addRow("First day off", self.first)
        f.addRow("Back on", self.back)
        self.action = _action_combo("pause")
        f.addRow("While away", self.action)
        lay.addLayout(f)
        self.summary = muted()
        lay.addWidget(self.summary)
        for d in (self.first, self.back):
            d.dateChanged.connect(self._update)
        self.action.currentIndexChanged.connect(self._update)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        lay.addWidget(bb)
        self._update()

    def period(self) -> tuple[QDateTime, QDateTime]:
        return QDateTime(self.first.date(), QTime(0, 0)), QDateTime(self.back.date(), QTime(0, 0))

    def _update(self) -> None:
        start, end = self.period()
        n = start.daysTo(end)
        ok = n >= 1
        self.ok.setEnabled(ok)
        what = {"pause": "Nothing is recorded", "motion": "Recording only when there is motion",
                "record": "Recording all the time"}[str(self.action.currentData())]
        self.summary.setText(
            f"{what} from {start.toString('dddd d MMMM')} 00:00 until {end.toString('dddd d MMMM')} 00:00 "
            f"({n} day{'s' if n != 1 else ''}). Then the normal schedule applies again by itself. The laptop and "
            "Sentinel keep running." if ok else "'Back on' must be after the first day off.")


class SchedulePage(SettingsPage):
    key = "schedule"
    title = "Schedule"
    description = "When to record, pause, or record only on motion."
    what = "the schedule"

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        top = form()
        self.top_form = top
        self.enabled = self.editor(QCheckBox("Use a recording schedule (off = record all the time)"))
        top.addRow(self.enabled)
        # GUI review 2026-11-17: with the schedule off the page looked exactly as if it were in use.
        self.off_note = muted("The schedule is off: Sentinel records all the time. The periods below are kept, "
                              "but not used until the box above is ticked.")
        top.addRow(self.off_note)
        self.enabled.toggled.connect(lambda on: top.setRowVisible(self.off_note, not on))
        self.default = self.editor(_action_combo("record"))
        top.addRow("Outside the periods below", self.default)
        self.tz = self.editor(QComboBox())
        self.tz.setEditable(True)
        from .setup_page import COMMON_ZONES

        self.tz.addItem("", "")
        self.tz.addItems(COMMON_ZONES)
        self.tz.setToolTip("Empty: the time zone on the General tab")
        top.addRow("Time zone", self.tz)
        preset = self.editor(icon_button("Work hours, motion at other times", "time", ctx.palette.text))
        preset.setToolTip("Record all the time Monday to Friday 07:00–18:00; at other times record only on motion")
        preset.clicked.connect(self._preset)
        prow = QHBoxLayout()
        prow.addWidget(preset)
        prow.addStretch(1)
        top.addRow("Quick setup", prow)
        topw = QWidget()
        topw.setLayout(top)
        top.setContentsMargins(0, 0, 0, 0)
        self.body.addWidget(capped(topw))
        self.weekly = WeeklyTable()
        self.dates = DatesTable()
        self.dates.leave_btn.clicked.connect(self._days_off)
        g1 = QGroupBox("Every week")
        QVBoxLayout(g1).addWidget(self.weekly)
        g2 = QGroupBox("Particular dates: days off, holidays")
        QVBoxLayout(g2).addWidget(self.dates)
        self.body.addWidget(g1, 3)
        self.body.addWidget(g2, 2)
        prev = QHBoxLayout()
        b = QPushButton("Preview the next 14 days")
        b.clicked.connect(self.preview)
        prev.addWidget(b)
        prev.addStretch(1)
        self.body.addLayout(prev)
        self.plan = QPlainTextEdit()
        self.plan.setObjectName("log")
        self.plan.setReadOnly(True)
        self.plan.setMaximumHeight(150)
        self.body.addWidget(self.plan)
        self.finish_layout()
        self.weekly.set_enabled(ctx.admin)
        self.dates.set_enabled(ctx.admin)

    def load(self) -> None:
        raw = self.raw().get("schedule", {}) or {}
        self.enabled.setChecked(bool(raw.get("enabled", False)))
        self.top_form.setRowVisible(self.off_note, not self.enabled.isChecked())
        self.default.setCurrentIndex(max(0, self.default.findData(raw.get("default", "record"))))
        self.tz.setEditText(str(raw.get("timezone", "") or ""))
        cfg = self.cfg
        if cfg is not None and self.tz.lineEdit() is not None:     # an empty box said nothing
            self.tz.lineEdit().setPlaceholderText(f"same as the General tab ({cfg.recording.display_timezone})")
        self.weekly.clear()
        for w in raw.get("weekly", []) or []:
            self.weekly.add(str(w.get("action", "record")), days_from_config(w.get("days", [])),
                            str(w.get("start", "00:00")), str(w.get("end", "24:00")))
        self.dates.clear()
        self._raw_dates = None
        try:
            parsed = [(str(d.get("action", "pause")), parse_date_value(str(d.get("start", "")), False),
                       parse_date_value(str(d.get("end", "")), True)) for d in raw.get("dates", []) or []]
        except (ValueError, AttributeError) as exc:
            # Review 2026-11-12: keep the dates exactly as written; never replace them by "now".
            parsed = []
            self._raw_dates = list(raw.get("dates", []) or [])
            self.dates.set_enabled(False)
            self.result.setText(f"The particular dates in the settings file cannot be shown here ({exc}). They are "
                                "kept unchanged when you save. To change them, use System › Configuration file.")
        for action, start, end in parsed:
            self.dates.add(action, start, end)
        if self._raw_dates is None:
            self.result.clear()
            self.dates.set_enabled(self.ctx.admin)
        self.preview()

    def _preset(self) -> None:
        self.enabled.setChecked(True)
        self.default.setCurrentIndex(self.default.findData("motion"))
        self.weekly.clear()
        self.weekly.add("record", set(DAY_KEYS[:5]), "07:00", "18:00")
        self.preview()

    def _days_off(self) -> None:
        dlg = DaysOffDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        start, end = dlg.period()
        self.enabled.setChecked(True)
        self.dates.add(str(dlg.action.currentData()), start, end)
        self.preview()
        self.result.setText("Days off added. Press Save and restart recorder to use them.")

    def tables(self) -> dict[str, dict[str, Any]]:
        weekly = self.weekly.rows()
        raw_dates = getattr(self, "_raw_dates", None)
        dates = raw_dates if raw_dates is not None else self.dates.rows()
        table: dict[str, Any] = {"enabled": self.enabled.isChecked(), "default": str(self.default.currentData()),
                                 "timezone": self.tz.currentText().strip()}
        if weekly:
            table["weekly"] = weekly
        if dates:
            table["dates"] = dates
        return {"schedule": table}

    def preview(self) -> None:
        from ...schedule import RecordingSchedule, ScheduleError, parse_schedule, render_plan

        cfg = self.cfg
        try:
            sc = parse_schedule(self.tables()["schedule"], cfg.recording.display_timezone if cfg else "UTC",
                                saving=True)
            self.plan.setPlainText(render_plan(RecordingSchedule(sc), time.time(), days=14))
        except (ScheduleError, ValueError) as exc:
            self.plan.setPlainText(f"Not valid yet: {exc}")


# --------------------------------------------------------------------------- motion
class MotionPage(SettingsPage):
    key = "motion"
    title = "Motion"
    description = "Motion detection for 'Record on motion' and the reviewers' motion markers."
    what = "motion detection"

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        box = QGroupBox("Detection")
        f = form(box)
        self.markers = self.editor(QCheckBox("Mark motion in recordings for reviewers (timeline and export summary)"))
        f.addRow(self.markers)
        self.keep = self._spin(10, 3600, " s", "Keep recording this long after the last motion (each motion restarts it)")
        f.addRow("Keep recording after motion", self.keep)
        self.percent = self.editor(QDoubleSpinBox())
        self.percent.setRange(0.05, 50)
        self.percent.setDecimals(2)
        self.percent.setSuffix(" %")
        self.percent.setToolTip("How much of the picture must change to count as motion")
        f.addRow("Share of picture that must change", self.percent)
        f.addRow("", Note("A smaller share notices smaller movements (more sensitive); a larger one ignores them. "
                          "Try it with the live test below."))
        # Field report 2026-11-18: this is set per camera at installation, so it is not behind Advanced; the
        # whole address was typed here and accepted, so the line below says which address will be read.
        self.stream = self.editor(QLineEdit())
        self.stream.setPlaceholderText("for example stream2; empty = key frames of the main stream")
        self.stream.setToolTip("Only the path of the camera's low-resolution stream, written like the Stream path on "
                               "the Camera tab. A whole address is reduced to its path when you save.")
        f.addRow("Second stream path", self.stream)
        self.stream_note = Note()
        f.addRow("", self.stream_note)
        self.stream.textChanged.connect(lambda _t: self._show_detection_address())
        # Owner request 2026-11-18 (the room has one light switch and windows): the light being switched is
        # someone in the room; daylight getting brighter or dimmer is not.
        self.light = self.editor(QCheckBox("Lighting: the room light being switched counts as motion at once; "
                                           "even changes of brightness are ignored"))
        f.addRow(self.light)
        f.addRow("", Note("A change of most of the picture at once is the light being switched: recording starts "
                          "without waiting for more frames. A smaller, even brightening or darkening (daylight, the "
                          "camera adjusting) is cancelled out. The live test shows LIGHT when the whole picture "
                          "changed."))
        # GUI review 2026-11-17: the settings above are the ones a site adjusts; the rest is tuning.
        self.advanced = AdvancedRows(f, "Fine tuning of the detector; the defaults suit most scenes.")
        self.light_percent = self._spin(20, 100, " %", "A change of at least this share of the picture at once is "
                                                        "the light being switched")
        self.advanced.add("Share of picture that means the light", self.light_percent)
        self.threshold = self._spin(5, 200, "", "Brightness change (0–255) that counts as a change")
        self.advanced.add("Brightness change that counts", self.threshold)
        self.frames = self._spin(1, 20, "", "Analysed frames in a row that must show change")
        self.advanced.add("Frames in a row", self.frames)
        self.fps = self.editor(QDoubleSpinBox())
        self.fps.setRange(0.5, 15)
        self.fps.setDecimals(1)
        self.fps.setSuffix(" per second")
        self.advanced.add("Analysis rate (second stream)", self.fps)
        self.unhealthy = self._spin(5, 300, " s", "Record all the time if the detector gets no pictures this long")
        self.advanced.add("Fail-safe after", self.unhealthy)
        self.merge = self._spin(1, 300, " s", "Motion closer together than this is one marker")
        self.advanced.add("Join markers closer than", self.merge)
        self.body.addWidget(capped(box))

        # Owner request 2026-11-18: a detector that gets pictures but no longer notices movement records
        # nothing and shows no fault. After a set time without any motion, record all the time instead.
        quiet = QGroupBox("If no motion is seen for a long time")
        q = form(quiet)
        self.quiet_on = self.editor(QCheckBox("Record all the time when no motion has been seen for the time below"))
        q.addRow(self.quiet_on)
        self.quiet_days = self.editor(QDoubleSpinBox())
        self.quiet_days.setRange(0.5, 30)
        self.quiet_days.setDecimals(1)
        self.quiet_days.setSingleStep(0.5)
        self.quiet_days.setSuffix(" days")
        self.quiet_days.setToolTip("Days of watching without any motion before the recorder records all the time")
        q.addRow("Time without motion", self.quiet_days)
        self.quiet_count = self.editor(DaysPicker(set(DAY_KEYS[:5])))
        q.addRow("Days that count", self.quiet_count)
        q.addRow("", Note("For a camera that was moved or covered, or settings that no longer notice movement: "
                          "nothing looks broken, but nothing is recorded. Days off and other particular dates on the "
                          "Schedule tab are not counted, nor are schedule pauses or the days not picked here. "
                          "Recording goes back to motion only by itself when motion is seen; until then the screen "
                          "shows W-003."))
        self.quiet_on.toggled.connect(self._quiet_enable)
        self.body.addWidget(capped(quiet))

        test = QGroupBox("Live test (walk through the scene while it runs; records nothing)")
        tl = QVBoxLayout(test)
        row = QHBoxLayout()
        self.secs = QSpinBox()
        self.secs.setRange(10, 600)
        self.secs.setValue(60)
        self.secs.setSuffix(" s")
        self.test_btn = self.editor(icon_button("Start test with the saved settings", "play", ctx.palette.text))
        self.test_btn.clicked.connect(self._test)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(lambda: self.log.stop())
        row.addWidget(self.secs)
        row.addWidget(self.test_btn)
        row.addWidget(self.stop_btn)
        row.addStretch(1)
        tl.addLayout(row)
        tl.addWidget(Note("One line a second: how much of the picture changed, and MOTION when that counts as "
                          "motion. Ten # marks are exactly the share set above."))
        self.log = OutputLog()
        self.panel = CommandPanel(self.log, ctx.palette)
        tl.addWidget(self.panel)
        test.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.body.addWidget(capped(test))  # same width as the form above; no empty box filling the page
        self.body.addStretch(1)
        self.finish_layout()

    def _spin(self, lo: int, hi: int, suffix: str, tip: str) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setSuffix(suffix)
        s.setToolTip(tip)
        self.editor(s)
        return s

    def load(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        m = cfg.motion
        self.markers.setChecked(m.markers)
        self.stream.setText(m.stream_path)
        self.keep.setValue(int(m.keep_recording_s))
        self.percent.setValue(float(m.changed_percent))
        self.threshold.setValue(int(m.pixel_threshold))
        self.frames.setValue(int(m.min_frames))
        self.fps.setValue(float(m.analysis_fps))
        self.unhealthy.setValue(int(m.unhealthy_after_s))
        self.merge.setValue(int(m.marker_merge_s))
        self.light.setChecked(m.light_aware)
        self.light_percent.setValue(int(m.light_change_percent))
        self.quiet_on.setChecked(m.no_motion_days > 0)
        self.quiet_days.setValue(float(m.no_motion_days) if m.no_motion_days > 0 else 2.0)
        picked = days_from_config(list(m.no_motion_count_days))
        for key, b in self.quiet_count.buttons.items():
            b.setChecked(key in picked)
        self._quiet_enable(self.quiet_on.isChecked())
        self._show_detection_address()
        self.result.clear()

    def _quiet_enable(self, on: bool) -> None:
        for w in (self.quiet_days, self.quiet_count):
            w.setEnabled(bool(on) and self.ctx.admin)

    def _show_detection_address(self) -> None:
        """Say which address the detector will read, so a path that is not one shows before it is saved."""
        from ...credentials import redact
        from ...motion import detection_url, stream_path_only

        cfg = self.cfg
        typed = self.stream.text().strip()
        path = stream_path_only(typed)
        if cfg is None or cfg.camera.pending or not cfg.camera.url:
            text = "" if not path else f"Detection will read the path “{path}” on the camera."
        elif not path:
            text = ("Detection reads only the key frames of the main stream. With the camera's low-resolution "
                    "stream here, motion is noticed sooner.")
        else:
            text = f"Detection will read {redact(detection_url(cfg.camera.url, path))}"
            if path != typed:
                text += f" (only the path, “{path}”, is saved)"
        self.stream_note.setText(text)

    def tables(self) -> dict[str, dict[str, Any]]:
        from ...motion import stream_path_only

        table: dict[str, Any] = {
            "markers": self.markers.isChecked(), "stream_path": stream_path_only(self.stream.text()),
            "keep_recording_s": self.keep.value(), "changed_percent": round(self.percent.value(), 2),
            "pixel_threshold": self.threshold.value(), "min_frames": self.frames.value(),
            "analysis_fps": round(self.fps.value(), 1), "unhealthy_after_s": self.unhealthy.value(),
            "marker_merge_s": self.merge.value()}
        # Written only while they are on or changed: earlier versions refuse keys they do not know.
        if self.light.isChecked():
            table["light_aware"] = True
        if self.light_percent.value() != 60:
            table["light_change_percent"] = float(self.light_percent.value())
        if self.quiet_on.isChecked():
            table["no_motion_days"] = round(self.quiet_days.value(), 1)
            table["no_motion_count_days"] = days_to_config(self.quiet_count.days())
        return {"motion": table}

    def _test(self) -> None:
        if self.ctx.cfg_path is None:
            return
        # The lines ARE the test (field report 2026-11-18: with them behind Details it looked as if nothing ran).
        self.panel.show_output()
        self.log.run(sentinel_argv("motion-test", "--config", str(self.ctx.cfg_path), "--seconds",
                                   str(self.secs.value())), "Motion test", busy=[self.test_btn])



# --------------------------------------------------------------------------- storage, archive, drives
class StoragePage(SettingsPage):
    """Archive copying, local retention and the drive-erase policy (review 2026-11-10: these were
    only reachable in Notepad, including [drives] erase_enabled that bench step 5.2 needs)."""

    key = "storage"
    title = "Storage"
    description = "Archive drive copying, keeping recordings on this computer, erasing drives."
    what = "storage settings"

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        arc = QGroupBox("Archive drive")
        f = form(arc)
        self.arc_on = self.editor(QCheckBox("Copy every recording to the archive drive"))
        f.addRow(self.arc_on)
        self.backlog = self._dspin(1, 720, " hours", "Alarm (A-004) when recordings wait longer than this to be copied")
        f.addRow("Alarm when not copied for", self.backlog)
        self.body.addWidget(capped(arc))
        ret = QGroupBox("Recordings on this computer")
        r = form(ret)
        self.ret_on = self.editor(QCheckBox("When space runs low, delete recordings already copied to an archive "
                                            "drive that has been taken out"))
        r.addRow(self.ret_on)
        # Owner decision 2026-11-20 (0.3.26): the level was not on this page, and "Free up to 10 GB" below read
        # as if deleting waited until 10 GB were left. It waited for the storage warning level; the level is now
        # a field of its own, in the GB the rest of the window shows.
        self.floor = self._dspin(1, 100_000, " GB", "Recordings are deleted from this computer only while less than "
                                 "this is free: the oldest first, and only as many as needed")
        self.floor.valueChanged.connect(lambda _v: self._floor_facts())
        r.addRow("Start deleting when free space is below", self.floor)
        self.floor_note = Note()
        r.addRow("", self.floor_note)
        self._floor_loaded = 0.0
        self._floor_default = True
        self.keep = self._dspin(1, 24 * 365, " hours", "Never delete recordings newer than this")
        r.addRow("Always keep the newest", self.keep)
        self.swap_days = self._dspin(1, 60, " days", "Warn (L-001) if space will run out before the next swap")
        r.addRow("Days between drive swaps", self.swap_days)
        # GUI review 2026-11-17: set once by a technician, if ever.
        self.ret_advanced = AdvancedRows(r)
        self.margin = self._dspin(1, 10_000, " GB", "Deleting stops again once this much more than the level above "
                                  "is free")
        self.ret_advanced.add("Each time, free", self.margin)
        self.body.addWidget(capped(ret))
        drv = QGroupBox("Erasing and setting up drives")
        d = form(drv)
        self.erase_on = self.editor(QCheckBox("Allow 'Erase and set up' (Drives page; administrator only)"))
        d.addRow(self.erase_on)
        self.drv_advanced = AdvancedRows(d, "The checks before a drive is erased.")
        self.serial_chars = self.editor(QSpinBox())
        self.serial_chars.setRange(4, 12)
        self.serial_chars.setToolTip("How many last characters of the serial number must be typed to erase")
        self.drv_advanced.add("Serial characters to type", self.serial_chars)
        self.min_size = self._dspin(1, 100_000, " GB", "Smaller drives are refused")
        self.drv_advanced.add("Smallest drive accepted", self.min_size)
        self.body.addWidget(capped(drv))
        self.body.addStretch(1)
        self.finish_layout()

    def _dspin(self, lo: float, hi: float, suffix: str, tip: str) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(1)
        s.setSuffix(suffix)
        s.setToolTip(tip)
        self.editor(s)
        return s

    def load(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        self.arc_on.setChecked(cfg.archive.enabled)
        self.backlog.setValue(cfg.archive.backlog_alert_hours)
        self.ret_on.setChecked(cfg.retention.enabled)
        from ...retention.manager import floor_bytes_for

        total, _free = self._recording_drive()
        self._floor_default = cfg.retention.floor_free_gb < 0
        self._floor_loaded = round(floor_bytes_for(cfg, total) / 1e9, 1)
        self.floor.setValue(self._floor_loaded)
        self._floor_facts()
        self.keep.setValue(cfg.retention.keep_recent_hours)
        self.swap_days.setValue(cfg.retention.expected_swap_days)
        self.margin.setValue(cfg.retention.margin_gb)
        self.erase_on.setChecked(cfg.drives.erase_enabled)
        self.serial_chars.setValue(cfg.drives.serial_confirm_chars)
        self.min_size.setValue(cfg.drives.min_size_gb)
        self.result.clear()

    # ---- the level at which archived recordings start to be deleted
    def _recording_drive(self) -> tuple[int | None, int | None]:
        """(size, free space) of the drive the recordings are on, or (None, None) when it cannot be read."""
        import shutil

        cfg = self.cfg
        if cfg is None:
            return None, None
        try:
            du = shutil.disk_usage(cfg.spool_path)
        except OSError:
            return None, None
        return int(du.total), int(du.free)

    def _floor_changed(self) -> bool:
        return abs(round(self.floor.value(), 1) - self._floor_loaded) > 0.05

    def _floor_facts(self) -> None:
        """Under the field: how big the recording drive is, and how long the entered room lasts at the rate
        the recorder measures now, so the number is not chosen blind."""
        total, free = self._recording_drive()
        parts = []
        if total is not None and free is not None:
            parts.append(f"The recording drive holds {tf.size(total)}; {tf.size(free)} are free now.")
        read = self.ctx.read
        rate = ((read.doc or {}).get("storage") or {}).get("rate_bytes_per_s") if read is not None else None
        if isinstance(rate, (int, float)) and rate > 0:
            parts.append(f"At the present recording rate, {self.floor.value():g} GB is room for "
                         f"{tf.approx_remaining(self.floor.value() * 1e9 / rate)}.")
        if self._floor_default and not self._floor_changed():
            parts.append("This is the built-in level for a drive of this size; nothing is set in the settings file.")
        self.floor_note.setText(" ".join(parts))

    def validate(self) -> AppConfig | None:
        cfg = self.cfg
        if cfg is not None and self.ret_on.isChecked() and self._floor_changed():
            from ...storage.monitor import GB

            level = self.floor.value() * 1e9
            warning = cfg.storage.warning_free_gb * GB
            total, _free = self._recording_drive()
            if level <= warning:
                self.problem(f"Start deleting when free space is below: {self.floor.value():g} GB is not above this "
                             f"computer's storage warning level ({tf.size(warning)}). Status would show a storage "
                             "warning before anything is deleted. Enter a larger number.\n\nNothing was changed.")
                return None
            if total is not None and level >= total:
                self.problem(f"Start deleting when free space is below: {self.floor.value():g} GB is as much as the "
                             f"whole recording drive holds ({tf.size(total)}). That much can never be free, so every "
                             "archived recording would be deleted as soon as its drive is taken out. Enter a smaller "
                             "number.\n\nNothing was changed.")
                return None
        return super().validate()

    def tables(self) -> dict[str, dict[str, Any]]:
        raw = self.raw()
        archive = dict(raw.get("archive", {}))
        archive.update(enabled=self.arc_on.isChecked(), backlog_alert_hours=round(self.backlog.value(), 1))
        retention = dict(raw.get("retention", {}))
        retention.update(enabled=self.ret_on.isChecked(), keep_recent_hours=round(self.keep.value(), 1),
                         expected_swap_days=round(self.swap_days.value(), 1), margin_gb=round(self.margin.value(), 1))
        if self._floor_changed():
            # The file keeps this level in the unit of its other storage levels (1024^3 bytes); the window shows
            # sizes in GB of 10^9 bytes everywhere, and so does this field. An untouched field writes nothing,
            # so a level that was never set stays the built-in one.
            from ...storage.monitor import GB

            retention["floor_free_gb"] = round(self.floor.value() * 1e9 / GB, 2)
        drives = dict(raw.get("drives", {}))
        drives.update(erase_enabled=self.erase_on.isChecked(), serial_confirm_chars=self.serial_chars.value(),
                      min_size_gb=round(self.min_size.value(), 1))
        return {"archive": archive, "retention": retention, "drives": drives}


# --------------------------------------------------------------------------- general
SEGMENT_CHOICES = (("15 minutes", 900), ("30 minutes", 1800), ("1 hour", 3600))


class GeneralPage(SettingsPage):
    """The organisation using this recorder (optional) and the time zone times are shown in."""

    key = "general"
    title = "General"
    description = "Organisation, time zone and recording file length."
    what = "these settings"

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        from .setup_page import COMMON_ZONES

        box = QGroupBox("This recorder")
        f = form(box)
        self.org = self.editor(QLineEdit())
        self.org.setMaxLength(100)
        self.org.setPlaceholderText("optional; leave empty to name no organisation")
        f.addRow("Organisation", self.org)
        f.addRow("", Note("Shown in the window, on diagnostic reports and on review-drive summaries. "
                          "Empty: the organisation is not named anywhere."))
        self.tz = self.editor(QComboBox())
        self.tz.setEditable(True)
        self.tz.addItems(COMMON_ZONES)
        f.addRow("Time zone for times shown", self.tz)
        self.seglen = self.editor(QComboBox())
        f.addRow("Length of each recording file", self.seglen)
        f.addRow("", Note("Each recording file covers this much time and starts on the clock (for one hour: "
                          "14:00 to 15:00). A file is copied to the archive drive once it is finished."))
        self.body.addWidget(capped(box))
        self.body.addStretch(1)
        self.finish_layout()

    def load(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        self.org.setText(cfg.site.organization)
        self.tz.setEditText(cfg.recording.display_timezone)
        self.seglen.clear()
        current = int(round(cfg.recording.segment_seconds))
        for text, secs in SEGMENT_CHOICES:
            self.seglen.addItem(text, secs)
        if current not in [secs for _t, secs in SEGMENT_CHOICES]:
            words = f"{current // 60} minutes" if current % 60 == 0 else f"{current} seconds"
            self.seglen.addItem(f"{words} (current)", current)
        self.seglen.setCurrentIndex(self.seglen.findData(current))
        self.result.clear()

    def tables(self) -> dict[str, dict[str, Any]]:
        raw = self.raw()
        recording = dict(raw.get("recording", {}))
        recording["display_timezone"] = self.tz.currentText().strip() or "UTC"
        secs = self.seglen.currentData()
        if isinstance(secs, int):
            recording["segment_seconds"] = secs
        return {"site": {"organization": self.org.text().strip()}, "recording": recording}
