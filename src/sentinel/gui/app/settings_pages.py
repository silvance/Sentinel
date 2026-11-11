"""Camera, Schedule and Motion pages: settings that are saved to the configuration file."""

from __future__ import annotations

import re
import time
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import QDate, QDateTime, QTime
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDateTimeEdit, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTimeEdit,
                               QToolButton, QVBoxLayout, QWidget)

from ...config import AppConfig
from .base import CommandPanel, OutputLog, Page, ReadOnlyBar, capped, icon_button, muted
from .configedit import ConfigEditError, check_text, replace_sections, save_sections
from .procs import Task, sentinel_argv


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
        f = QFormLayout(box)
        self.pending = self.editor(QCheckBox("No camera yet (set everything else up; nothing is recorded)"))
        self.pending.toggled.connect(self._pending_changed)
        f.addRow("", self.pending)
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
        self.transport = self.editor(QComboBox())
        self.transport.addItem("TCP (recommended)", "tcp")
        self.transport.addItem("UDP", "udp")
        f.addRow("Transport", self.transport)
        self.secure = self.editor(QCheckBox("Encrypted stream (rtsps://; only if the camera supports it)"))
        f.addRow("", self.secure)
        self.body.addWidget(capped(box))

        acc = QGroupBox("Camera account (use a view-only account)")
        f2 = QFormLayout(acc)
        self.user = self.editor(QLineEdit())
        self.user.setPlaceholderText("empty if the camera needs no login")
        f2.addRow("User name", self.user)
        self.password = self.editor(QLineEdit())
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        f2.addRow("Password", self.password)
        self.cred_note = muted()
        f2.addRow("", self.cred_note)
        self.body.addWidget(capped(acc))

        row = QHBoxLayout()
        self.test_btn = self.editor(icon_button("Test connection", "test", ctx.palette.text))
        self.test_btn.clicked.connect(self.test)
        row.addWidget(self.test_btn)
        self.test_result = QLabel()
        self.test_result.setWordWrap(True)
        row.addWidget(self.test_result, 1)
        self.body.addLayout(row)
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

    def _pending_changed(self, on: bool) -> None:
        for w in (self.host, self.port, self.path, self.transport, self.user, self.password, self.test_btn):
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
                tmp = path.with_name(path.name + ".restore")
                tmp.write_bytes(data)
                tmp.replace(path)
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


def _time_edit(text: str) -> QTimeEdit:
    t = QTimeEdit()
    t.setDisplayFormat("HH:mm")
    hh, _, mm = (text or "00:00").partition(":")
    try:
        t.setTime(QTime(int(hh) % 24, int(mm[:2] or 0)))
    except ValueError:
        t.setTime(QTime(0, 0))
    return t


def _date_time_edit(value: QDateTime) -> QDateTimeEdit:
    e = QDateTimeEdit(value)
    e.setCalendarPopup(True)
    e.setDisplayFormat("ddd d MMM yyyy  HH:mm")
    return e


def parse_date_value(text: str, is_end: bool) -> QDateTime:
    """``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM``; a date alone as an end means the END of that day."""
    text = (text or "").strip()
    if len(text) <= 10:
        d = QDate.fromString(text, "yyyy-MM-dd")
        if not d.isValid():
            d = QDate.currentDate()
        dt = QDateTime(d, QTime(0, 0))
        return dt.addDays(1) if is_end else dt
    dt = QDateTime.fromString(text[:16], "yyyy-MM-dd HH:mm")
    return dt if dt.isValid() else QDateTime.currentDateTime()


def date_value(dt: QDateTime) -> str:
    return dt.toString("yyyy-MM-dd HH:mm")


class WeeklyTable(QWidget):
    """Weekly periods: action, days (toggle buttons), start and end time (time pickers)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.t = QTableWidget(0, 4)
        self.t.setHorizontalHeaderLabels(["Action", "Days", "From", "Until"])
        self.t.verticalHeader().setVisible(False)
        self.t.verticalHeader().setDefaultSectionSize(34)
        self.t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.t.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.t)
        row = QHBoxLayout()
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(lambda: self.add("record", set(DAY_KEYS[:5]), "07:00", "18:00"))
        self.del_btn = QPushButton("Remove selected")
        self.del_btn.clicked.connect(self._remove)
        row.addWidget(self.add_btn)
        row.addWidget(self.del_btn)
        row.addWidget(muted("Until 00:00 means until midnight. An end before the start runs past midnight."), 1)
        lay.addLayout(row)

    def clear(self) -> None:
        self.t.setRowCount(0)

    def add(self, action: str, days: set[str], start: str, end: str) -> None:
        r = self.t.rowCount()
        self.t.insertRow(r)
        self.t.setCellWidget(r, 0, _action_combo(action))
        self.t.setCellWidget(r, 1, DaysPicker(days))
        self.t.setCellWidget(r, 2, _time_edit(start))
        self.t.setCellWidget(r, 3, _time_edit("00:00" if end == "24:00" else end))
        self.t.resizeColumnsToContents()

    def _remove(self) -> None:
        rows = sorted({i.row() for i in self.t.selectedIndexes()} | ({self.t.currentRow()} - {-1}), reverse=True)
        for r in rows:
            self.t.removeRow(r)

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for r in range(self.t.rowCount()):
            action, days = self.t.cellWidget(r, 0), self.t.cellWidget(r, 1)
            start, end = self.t.cellWidget(r, 2), self.t.cellWidget(r, 3)
            picked = days.days() if isinstance(days, DaysPicker) else set()
            if not picked:
                continue
            e = end.time().toString("HH:mm") if isinstance(end, QTimeEdit) else "00:00"
            out.append({"action": str(action.currentData()) if isinstance(action, QComboBox) else "record",
                        "days": days_to_config(picked),
                        "start": start.time().toString("HH:mm") if isinstance(start, QTimeEdit) else "00:00",
                        "end": "24:00" if e == "00:00" else e})
        return out

    def set_enabled(self, on: bool) -> None:
        self.t.setEnabled(on)
        self.add_btn.setEnabled(on)
        self.del_btn.setEnabled(on)


class DatesTable(QWidget):
    """One-off periods (holidays, leave): action, from, until (date-and-time pickers with a calendar)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.t = QTableWidget(0, 3)
        self.t.setHorizontalHeaderLabels(["Action", "From", "Until"])
        self.t.verticalHeader().setVisible(False)
        self.t.verticalHeader().setDefaultSectionSize(34)
        self.t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        lay.addWidget(self.t)
        row = QHBoxLayout()
        self.leave_btn = QPushButton("Days off…")
        self.leave_btn.setToolTip("Stop recording for whole days (leave, holidays); recording starts again by itself")
        self.add_btn = QPushButton("Add period")
        self.add_btn.clicked.connect(self._add_default)
        self.del_btn = QPushButton("Remove selected")
        self.del_btn.clicked.connect(self._remove)
        for b in (self.leave_btn, self.add_btn, self.del_btn):
            row.addWidget(b)
        row.addWidget(muted("These take priority over the weekly periods. The recorder keeps running and goes "
                            "back to the weekly plan by itself when a period ends."), 1)
        lay.addLayout(row)
        self.t.horizontalHeader().setStretchLastSection(True)

    def _add_default(self) -> None:
        start = QDateTime(QDate.currentDate().addDays(1), QTime(0, 0))
        self.add("pause", start, start.addDays(1))

    def clear(self) -> None:
        self.t.setRowCount(0)

    def add(self, action: str, start: QDateTime, end: QDateTime) -> None:
        r = self.t.rowCount()
        self.t.insertRow(r)
        self.t.setCellWidget(r, 0, _action_combo(action))
        self.t.setCellWidget(r, 1, _date_time_edit(start))
        self.t.setCellWidget(r, 2, _date_time_edit(end))
        self.t.resizeColumnsToContents()

    def _remove(self) -> None:
        rows = sorted({i.row() for i in self.t.selectedIndexes()} | ({self.t.currentRow()} - {-1}), reverse=True)
        for r in rows:
            self.t.removeRow(r)

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for r in range(self.t.rowCount()):
            action, start, end = (self.t.cellWidget(r, c) for c in range(3))
            if not isinstance(start, QDateTimeEdit) or not isinstance(end, QDateTimeEdit):
                continue
            out.append({"action": str(action.currentData()) if isinstance(action, QComboBox) else "pause",
                        "start": date_value(start.dateTime()), "end": date_value(end.dateTime())})
        return out

    def set_enabled(self, on: bool) -> None:
        self.t.setEnabled(on)
        for b in (self.leave_btn, self.add_btn, self.del_btn):
            b.setEnabled(on)


class DaysOffDialog(QDialog):
    """"He is on leave for 6 days in December": no recording from the first day off until the day he
    is back; nothing is shut down, recording starts again by itself (owner request 2026-11-10)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Days off")
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
        top = QFormLayout()
        self.enabled = self.editor(QCheckBox("Use a recording schedule (off = record all the time)"))
        top.addRow("", self.enabled)
        self.default = self.editor(_action_combo("record"))
        top.addRow("Outside the periods below", self.default)
        self.tz = self.editor(QComboBox())
        self.tz.setEditable(True)
        from .setup_page import COMMON_ZONES

        self.tz.addItem("", "")
        self.tz.addItems(COMMON_ZONES)
        self.tz.setToolTip("Empty: the time zone on the General page")
        top.addRow("Time zone", self.tz)
        preset = self.editor(icon_button("Work hours, motion at other times", "time", ctx.palette.text))
        preset.setToolTip("Record all the time Monday to Friday 07:00–18:00; at other times record only on motion")
        preset.clicked.connect(self._preset)
        top.addRow("Quick setup", preset)
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
        self.default.setCurrentIndex(max(0, self.default.findData(raw.get("default", "record"))))
        self.tz.setEditText(str(raw.get("timezone", "") or ""))
        self.weekly.clear()
        for w in raw.get("weekly", []) or []:
            self.weekly.add(str(w.get("action", "record")), days_from_config(w.get("days", [])),
                            str(w.get("start", "00:00")), str(w.get("end", "24:00")))
        self.dates.clear()
        for d in raw.get("dates", []) or []:
            self.dates.add(str(d.get("action", "pause")), parse_date_value(str(d.get("start", "")), False),
                           parse_date_value(str(d.get("end", "")), True))
        self.result.clear()
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
        weekly, dates = self.weekly.rows(), self.dates.rows()
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
            sc = parse_schedule(self.tables()["schedule"], cfg.recording.display_timezone if cfg else "UTC")
            self.plan.setPlainText(render_plan(RecordingSchedule(sc), time.time(), days=14))
        except ScheduleError as exc:
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
        f = QFormLayout(box)
        self.markers = self.editor(QCheckBox("Mark motion in recordings for reviewers (timeline and export summary)"))
        f.addRow("", self.markers)
        self.stream = self.editor(QLineEdit())
        self.stream.setPlaceholderText("empty = key frames of the main stream")
        f.addRow("Second stream path", self.stream)
        self.keep = self._spin(10, 3600, " s", "Keep recording this long after the last motion (each motion restarts it)")
        f.addRow("Keep recording after motion", self.keep)
        self.percent = self.editor(QDoubleSpinBox())
        self.percent.setRange(0.05, 50)
        self.percent.setDecimals(2)
        self.percent.setSuffix(" %")
        f.addRow("Share of picture that must change", self.percent)
        self.threshold = self._spin(5, 200, "", "Brightness change (0–255) that counts as a change")
        f.addRow("Brightness change that counts", self.threshold)
        self.frames = self._spin(1, 20, "", "Analysed frames in a row that must show change")
        f.addRow("Frames in a row", self.frames)
        self.fps = self.editor(QDoubleSpinBox())
        self.fps.setRange(0.5, 15)
        self.fps.setDecimals(1)
        self.fps.setSuffix(" per second")
        f.addRow("Analysis rate (second stream)", self.fps)
        self.unhealthy = self._spin(5, 300, " s", "Record all the time if the detector gets no pictures this long")
        f.addRow("Fail-safe after", self.unhealthy)
        self.merge = self._spin(1, 300, " s", "Motion closer together than this is one marker")
        f.addRow("Join markers closer than", self.merge)
        self.body.addWidget(capped(box))

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
        self.stop_btn.clicked.connect(lambda: self.log.current.kill() if self.log.current else None)
        row.addWidget(self.secs)
        row.addWidget(self.test_btn)
        row.addWidget(self.stop_btn)
        row.addStretch(1)
        tl.addLayout(row)
        self.log = OutputLog()
        tl.addWidget(CommandPanel(self.log, ctx.palette))
        self.body.addWidget(test, 1)
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
        self.result.clear()

    def tables(self) -> dict[str, dict[str, Any]]:
        return {"motion": {"markers": self.markers.isChecked(), "stream_path": self.stream.text().strip(),
                           "keep_recording_s": self.keep.value(), "changed_percent": round(self.percent.value(), 2),
                           "pixel_threshold": self.threshold.value(), "min_frames": self.frames.value(),
                           "analysis_fps": round(self.fps.value(), 1), "unhealthy_after_s": self.unhealthy.value(),
                           "marker_merge_s": self.merge.value()}}

    def _test(self) -> None:
        if self.ctx.cfg_path is None:
            return
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
        f = QFormLayout(arc)
        self.arc_on = self.editor(QCheckBox("Copy every recording to the archive drive"))
        f.addRow("", self.arc_on)
        self.backlog = self._dspin(1, 720, " hours", "Alarm (A-004) when recordings wait longer than this to be copied")
        f.addRow("Alarm when not copied for", self.backlog)
        self.body.addWidget(capped(arc))
        ret = QGroupBox("Recordings on this computer")
        r = QFormLayout(ret)
        self.ret_on = self.editor(QCheckBox("When space runs low, delete recordings already copied to a REMOVED "
                                            "archive drive"))
        r.addRow("", self.ret_on)
        self.keep = self._dspin(1, 24 * 365, " hours", "Never delete recordings newer than this")
        r.addRow("Always keep the newest", self.keep)
        self.swap_days = self._dspin(1, 60, " days", "Warn (L-001) if space will run out before the next swap")
        r.addRow("Days between drive swaps", self.swap_days)
        self.margin = self._dspin(1, 10_000, " GB", "Free only this much above the low-space level")
        r.addRow("Free up to", self.margin)
        self.body.addWidget(capped(ret))
        drv = QGroupBox("Erasing and setting up drives")
        d = QFormLayout(drv)
        self.erase_on = self.editor(QCheckBox("Allow 'Erase and set up' (Drives page; administrator only)"))
        d.addRow("", self.erase_on)
        self.serial_chars = self.editor(QSpinBox())
        self.serial_chars.setRange(4, 12)
        self.serial_chars.setToolTip("How many last characters of the serial number must be typed to erase")
        d.addRow("Serial characters to type", self.serial_chars)
        self.min_size = self._dspin(1, 100_000, " GB", "Smaller drives are refused")
        d.addRow("Smallest drive accepted", self.min_size)
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
        self.keep.setValue(cfg.retention.keep_recent_hours)
        self.swap_days.setValue(cfg.retention.expected_swap_days)
        self.margin.setValue(cfg.retention.margin_gb)
        self.erase_on.setChecked(cfg.drives.erase_enabled)
        self.serial_chars.setValue(cfg.drives.serial_confirm_chars)
        self.min_size.setValue(cfg.drives.min_size_gb)
        self.result.clear()

    def tables(self) -> dict[str, dict[str, Any]]:
        raw = self.raw()
        archive = dict(raw.get("archive", {}))
        archive.update(enabled=self.arc_on.isChecked(), backlog_alert_hours=round(self.backlog.value(), 1))
        retention = dict(raw.get("retention", {}))
        retention.update(enabled=self.ret_on.isChecked(), keep_recent_hours=round(self.keep.value(), 1),
                         expected_swap_days=round(self.swap_days.value(), 1), margin_gb=round(self.margin.value(), 1))
        drives = dict(raw.get("drives", {}))
        drives.update(erase_enabled=self.erase_on.isChecked(), serial_confirm_chars=self.serial_chars.value(),
                      min_size_gb=round(self.min_size.value(), 1))
        return {"archive": archive, "retention": retention, "drives": drives}


# --------------------------------------------------------------------------- general
class GeneralPage(SettingsPage):
    """The organisation using this recorder (optional) and the time zone times are shown in."""

    key = "general"
    title = "General"
    description = "Organisation and time zone."
    what = "these settings"

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        from .setup_page import COMMON_ZONES

        box = QGroupBox("This recorder")
        f = QFormLayout(box)
        self.org = self.editor(QLineEdit())
        self.org.setMaxLength(100)
        self.org.setPlaceholderText("optional; leave empty to name no organisation")
        f.addRow("Organisation", self.org)
        f.addRow("", muted("Shown in the window, on diagnostic reports and on review-drive summaries. "
                           "Empty: the organisation is not named anywhere."))
        self.tz = self.editor(QComboBox())
        self.tz.setEditable(True)
        self.tz.addItems(COMMON_ZONES)
        f.addRow("Time zone for times shown", self.tz)
        self.body.addWidget(capped(box))
        self.body.addStretch(1)
        self.finish_layout()

    def load(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        self.org.setText(cfg.site.organization)
        self.tz.setEditText(cfg.recording.display_timezone)
        self.result.clear()

    def tables(self) -> dict[str, dict[str, Any]]:
        raw = self.raw()
        recording = dict(raw.get("recording", {}))
        recording["display_timezone"] = self.tz.currentText().strip() or "UTC"
        return {"site": {"organization": self.org.text().strip()}, "recording": recording}
