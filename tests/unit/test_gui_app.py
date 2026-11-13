"""The Sentinel window (owner request 2026-11-10: everything doable in one compact window).

Carried over from the old operator screen: the exact alert text is shown, the test camera is
always flagged, a status that stops updating is X-001, opening the window writes nothing.
New: settings are read-only without administrator rights; saving rewrites only the edited
section (comments kept, .bak written); erasing a drive needs the person's name and the serial
characters and goes through the same command as the command line; setup works in the window.
"""

from __future__ import annotations

from dataclasses import replace

import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from gui_support import base_measurements, status_doc, write_doc  # noqa: E402
from sentinel.alerts import Severity, make_alert  # noqa: E402
from sentinel.config import load_config  # noqa: E402
from sentinel.gui.__main__ import prepare_qt_environment  # noqa: E402
from sentinel.launcher import setup  # noqa: E402
from sentinel.paths import Layout  # noqa: E402

prepare_qt_environment()

from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton  # noqa: E402
from PySide6.QtWidgets import QMessageBox as QMessageBox_  # noqa: E402

from sentinel.gui.app import base, configedit, drive_pages, settings_pages  # noqa: E402
from sentinel.gui.app.window import AppWindow, page_for_code  # noqa: E402
from sentinel.gui.ledger_view import Span, SpanKind, Timeline  # noqa: E402


@pytest.fixture(autouse=True)
def _config_is_admin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test configurations live in a user-writable temp folder; the window would (rightly) refuse
    administrator actions on them. Tests of that refusal undo this."""
    from sentinel.gui.app import window as winmod

    monkeypatch.setattr(winmod, "admin_config_problem", lambda _p, _c: "")
    # The 3-minute check after a save has its own test; elsewhere it would hold up closing the window.
    monkeypatch.setattr(winmod.AppWindow, "watch_after_change", lambda self: None)


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    from sentinel.gui.app import style

    style.apply(a, "light")  # type: ignore[arg-type]
    return a  # type: ignore[return-value]


def _layout(tmp: Path) -> Layout:
    root = tmp / "machine"
    return Layout("machine", root / "config" / "sentinel.toml", root / "config" / "camera.secret", root / "spool",
                  root / "data", root / "logs", root / "status")


def _setup(tmp: Path, **kw: Any) -> Path:
    ans = setup.SetupAnswers(kw.pop("host", "192.0.2.64"), kw.pop("path", "Streaming/Channels/101"),
                             camera_name="Front door", timezone="America/New_York", **kw)
    path, _ = setup.write_setup(ans, _layout(tmp))
    return path


def _window(app: QApplication, tmp: Path, *, admin: bool, doc: dict | None = None, **kw: Any) -> AppWindow:
    path = _setup(tmp, **kw)
    cfg = load_config(path)
    if doc is not False:
        write_doc(cfg.status_path, doc or status_doc(base_measurements(), time.time(), last_segment_end=time.time()))
    w = AppWindow(cfg, path, admin=admin, appearance="light", auto_refresh=False, prefs_path=tmp / "prefs.json")
    w.resize(1280, 800)
    w.show()
    app.processEvents()
    return w


def _drain(app: QApplication, seconds: float = 0.2) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


# ---------------------------------------------------------------------- layout and status
def test_every_section_opens(app: QApplication, tmp_path: Path) -> None:
    w = _window(app, tmp_path, admin=True)
    names = w.section_names()
    assert names == ["Status", "Recordings", "Drives", "Export", "Settings", "System"]   # simplified 2026-11-13
    for key in ("recordings", "general", "camera", "schedule", "motion", "storage", "export", "system", "status"):
        w.navigate(key)
        app.processEvents()
        assert w.current_page().key == key
    assert w.view is not None and w.view.headline == "Recording normally"
    w.close()


def test_alert_text_is_shown_exactly_and_links_to_its_page(app: QApplication, tmp_path: Path) -> None:
    doc = status_doc(base_measurements(), time.time())
    a = make_alert("T-002", Severity.WARNING).to_dict()
    doc.update(alert=a, alerts=[a], banner=True)
    w = _window(app, tmp_path, admin=False, doc=doc)
    t = w.pages["status"].alerts
    assert t.topLevelItem(0).text(0) == "T-002" and t.topLevelItem(0).text(1) == a["message"]
    assert "1 alert: T-002" in w.sb_alerts.text()
    assert page_for_code("T-002") == "system" and page_for_code("P-002") == "camera"
    assert page_for_code("A-001") == "drives" and page_for_code("W-001") == "motion"
    w.pages["status"]._open_alert(0, 0)
    assert w.current_page().key == "system"
    w.close()


def test_test_camera_is_always_flagged(app: QApplication, tmp_path: Path) -> None:
    doc = status_doc(base_measurements(), time.time())
    z = make_alert("Z-001", Severity.WARNING).to_dict()
    doc.update(alerts=[z], alert=z)
    w = _window(app, tmp_path, admin=False, doc=doc)
    assert "TEST CAMERA" in w.state_label.text() and "TEST CAMERA" in w.sb_state.text()
    w.close()


def test_missing_status_is_x001_and_opening_writes_nothing(app: QApplication, tmp_path: Path) -> None:
    path = _setup(tmp_path)
    cfg = load_config(path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    w = AppWindow(cfg, path, admin=False, appearance="light", auto_refresh=False, prefs_path=None)
    w.show()
    for key in ("recordings", "camera", "schedule", "motion", "system", "status"):
        w.navigate(key)
        app.processEvents()
    assert "X-001" in w.sb_alerts.text()
    assert w.pages["status"].alerts.topLevelItem(0).text(0) == "X-001"
    w.close()
    # The only write: the live view's request to the recorder (the operators' request folder).
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    extra = [p for p in after if p not in before]
    assert all(p.name == "preview.request" or (tmp_path / p).is_dir() for p in extra), extra


def test_timeline_marks_gaps_and_motion(app: QApplication) -> None:
    from zoneinfo import ZoneInfo

    from sentinel.gui.app.timeline import MOTION_MIN_W, TimelineBar, tick_hours
    from sentinel.gui.theme import pick_palette

    now = 1_800_000_000.0
    tl = Timeline(now - 86400, now, (Span(now - 86400, now - 3600, SpanKind.RECORDED),
                                     Span(now - 3600, now - 3000, SpanKind.GAP, "camera unreachable"),
                                     Span(now - 3000, now, SpanKind.RECORDED)), motion=((now - 600, now - 590),))
    bar = TimelineBar(pick_palette("light"))
    bar.resize(1000, 60)
    bar.set_timeline(tl, ZoneInfo("UTC"))
    img = bar.grab().toImage()
    assert bar.ticks and len(bar.ticks) == 8                         # every 3 hours over 24 hours
    assert tick_hours(7 * 86400) == 24 and tick_hours(6 * 3600) == 1
    x = int(1 + (600 - 10) / 86400 * 998 * -1 + 998 * (1 - 600 / 86400))   # motion marker position
    blue = img.pixelColor(x, 4)
    assert blue.blue() > blue.red()                                  # motion strip drawn over recorded time
    assert MOTION_MIN_W >= 6


# ---------------------------------------------------------------------- read-only without administrator
def test_settings_are_read_only_without_administrator(app: QApplication, tmp_path: Path) -> None:
    w = _window(app, tmp_path, admin=False)
    assert "(read-only)" in w.windowTitle() and w.unlock_btn.isVisible()
    for key in ("camera", "schedule", "motion", "storage"):
        page = w.pages[key]
        assert not page.save_btn.isEnabled()
        assert all(not e.isEnabled() for e in page.editors)
    assert not w.pages["drives"].erase_btn.isEnabled()
    assert not w.pages["system"].b_install.isEnabled()
    w.close()


def test_unlock_asks_windows_and_closes_only_if_approved(app: QApplication, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.gui.app import window as winmod

    w = _window(app, tmp_path, admin=False)
    asked: list[list[str]] = []
    monkeypatch.setattr(winmod, "relaunch_elevated", lambda extra: asked.append(extra) or False)
    monkeypatch.setattr(winmod.QMessageBox, "information", lambda *a, **k: None)
    w.unlock()
    assert asked == [["--config", str(w.ctx.cfg_path), "--no-start"]] and w.isVisible()
    monkeypatch.setattr(winmod, "relaunch_elevated", lambda extra: True)
    w.unlock()
    assert not w.isVisible()


# ---------------------------------------------------------------------- saving settings
def test_replace_sections_keeps_comments_and_other_sections() -> None:
    text = ("# top\n[camera]\nid = \"CAM01\"\n\n# about the schedule\n[schedule]\nenabled = false\n\n"
            "# example\n[[schedule.weekly]]\naction = \"pause\"\ndays = [\"weekend\"]\nstart = \"00:00\"\n"
            "end = \"24:00\"\n\n# motion here\n[motion]\nmarkers = true\n")
    out = configedit.replace_sections(text, {"schedule": {"enabled": True, "default": "motion", "weekly": [
        {"action": "record", "days": ["weekdays"], "start": "07:00", "end": "18:00"}]}})
    d = tomllib.loads(out)
    assert d["schedule"] == {"enabled": True, "default": "motion",
                             "weekly": [{"action": "record", "days": ["weekdays"], "start": "07:00", "end": "18:00"}]}
    assert d["camera"] == {"id": "CAM01"} and d["motion"] == {"markers": True}
    assert "# top" in out and "# about the schedule" in out and "# motion here" in out
    assert "pause" not in out
    added = configedit.replace_sections("[camera]\nid = \"C\"\n", {"motion": {"markers": False}})
    assert tomllib.loads(added)["motion"] == {"markers": False}


def test_schedule_page_saves_work_hours_and_motion(app: QApplication, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    restarts: list[Any] = []
    monkeypatch.setattr(base, "restart_recorder", lambda p, c: restarts.append(p) or "Restarted.")
    w = _window(app, tmp_path, admin=True)
    w.navigate("schedule")
    page = w.pages["schedule"]
    page._preset()
    assert "motion" in page.plan.toPlainText().lower()
    page.save()
    _drain(app, 0.5)
    cfg = load_config(w.ctx.cfg_path)
    assert cfg.schedule.enabled and cfg.schedule.default == "motion"
    assert [(p.action, p.start_s, p.end_s) for p in cfg.schedule.weekly] == [("record", 7 * 3600, 18 * 3600)]
    assert list(Path(w.ctx.cfg_path).parent.glob("sentinel.toml.*.bak"))
    assert restarts == [w.ctx.cfg_path] and w.ctx.cfg.schedule.enabled
    w.close()


def test_invalid_schedule_is_refused_and_nothing_changes(app: QApplication, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    said: list[str] = []
    monkeypatch.setattr(base.QMessageBox, "warning", lambda _p, _t, text: said.append(text))
    w = _window(app, tmp_path, admin=True)
    page = w.pages["schedule"]
    before = Path(w.ctx.cfg_path).read_text(encoding="utf-8")
    page.enabled.setChecked(True)
    page.weekly.add("record", {"mon"}, "07:00", "18:00")      # overlapping periods with different actions
    page.weekly.add("pause", {"mon"}, "08:00", "09:00")
    page.save()
    assert said and "Nothing was changed" in said[0]
    assert Path(w.ctx.cfg_path).read_text(encoding="utf-8") == before
    w.close()


def test_motion_page_saves_settings(app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "restart_recorder", lambda p, c: "Restarted.")
    w = _window(app, tmp_path, admin=True)
    page = w.pages["motion"]
    page.keep.setValue(300)
    page.percent.setValue(2.5)
    page.stream.setText("Streaming/Channels/102")
    page.save()
    _drain(app, 0.3)
    m = load_config(w.ctx.cfg_path).motion
    assert m.keep_recording_s == 300 and m.changed_percent == 2.5 and m.stream_path == "Streaming/Channels/102"
    w.close()


def test_camera_page_saves_address_and_account(app: QApplication, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "restart_recorder", lambda p, c: "Restarted.")
    monkeypatch.setattr(settings_pages.QMessageBox, "question",
                        lambda *a, **k: settings_pages.QMessageBox.StandardButton.Yes)   # untested camera: save
    w = _window(app, tmp_path, admin=True, camera_later=True, host="", path="")
    page = w.pages["camera"]
    assert page.pending.isChecked() and not page.host.isEnabled()
    page.pending.setChecked(False)
    page.host.setText("192.0.2.80")
    page.path.setText("Streaming/Channels/101")
    page.user.setText("viewer")
    page.password.setText("s3cret!")
    page.save()
    _drain(app, 0.3)
    cfg = load_config(w.ctx.cfg_path)
    assert not cfg.camera.pending and cfg.camera.url == "rtsp://192.0.2.80:554/Streaming/Channels/101"
    assert cfg.camera.credentials_file and Path(cfg.camera.credentials_file).exists()
    assert "s3cret!" not in Path(w.ctx.cfg_path).read_text(encoding="utf-8") and page.password.text() == ""
    w.close()


# ---------------------------------------------------------------------- drives
def test_erasing_needs_name_and_serial_and_uses_the_checked_command(app: QApplication, tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    w = _window(app, tmp_path, admin=True)
    page = w.pages["drives"]
    runs: list[list[str]] = []
    monkeypatch.setattr(page.log, "run", lambda argv, *a, **k: runs.append(argv))
    disk = {"disk_id": "3", "class": "FOREIGN_DATA", "can_prepare": True, "serial": "ABCD1234WXYZ",
            "description": "USB disk", "size_bytes": 10**12, "mount_paths": ["E:\\"]}
    page.list.drives = [disk]
    page._show_drives()
    page.table.selectRow(0)

    def fake_exec(dlg: QDialog, who: str, tail: str, accept: bool = True) -> int:
        dlg.who.setText(who)
        dlg.tail.setText(tail)
        return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

    said: list[str] = []
    monkeypatch.setattr(base.QMessageBox, "warning", lambda _p, _t, text: said.append(text))
    monkeypatch.setattr(drive_pages.EraseDialog, "exec", lambda dlg: fake_exec(dlg, "Tech 7", "wx"))
    page._erase()
    assert runs == [] and "exactly 4 characters" in said[-1]                   # too short: nothing run
    monkeypatch.setattr(drive_pages.EraseDialog, "exec", lambda dlg: fake_exec(dlg, "Tech 7", "wxyz"))
    page._erase()
    argv = runs[-1]
    assert argv[argv.index("drives"):argv.index("drives") + 2] == ["drives", "prepare"]
    for flag, value in (("--disk", "3"), ("--who", "Tech 7"), ("--serial-tail", "WXYZ")):
        assert argv[argv.index(flag) + 1] == value
    assert "--admin" in argv and "--yes" in argv
    page.list.drives = [dict(disk, can_prepare=False, refusal="system disk")]
    page._show_drives()
    page.table.selectRow(0)
    n = len(runs)
    page._erase()
    assert len(runs) == n and "cannot be erased" in said[-1]
    w.close()


def test_hidden_serial_is_confirmed_with_the_word(app: QApplication, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Field report 2026-11-10: the dock reports 000000000000000000; the dialog asks for ERASE."""
    w = _window(app, tmp_path, admin=True)
    page = w.pages["drives"]
    runs: list[list[str]] = []
    monkeypatch.setattr(page.log, "run", lambda argv, *a, **k: runs.append(argv))
    page.list.drives = [{"disk_id": "3", "class": "BLANK", "can_prepare": True, "serial": "0" * 18,
                         "serial_hidden": True, "description": "Dual SATA Bridge", "size_bytes": 2 * 10**12}]
    page._show_drives()
    assert page.table.item(0, 5).text() == "hidden by the dock"
    page.table.selectRow(0)

    def fake_exec(dlg: QDialog) -> int:
        assert dlg.hidden and dlg.expected_len == 5
        dlg.who.setText("Tech 7")
        dlg.tail.setText("erase")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(drive_pages.EraseDialog, "exec", fake_exec)
    page._erase()
    argv = runs[-1]
    assert argv[argv.index("--serial-tail") + 1] == "ERASE"
    w.close()


def test_erase_turned_off_says_how_to_turn_it_on_and_rechecks_a_stale_row(app: QApplication, tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Field report 2026-11-10: the dialog said 'erase_disabled', and after turning erasing on the
    page still used the list it had read before."""
    w = _window(app, tmp_path, admin=True)
    page = w.pages["drives"]
    shown: list[str] = []
    monkeypatch.setattr(drive_pages.QMessageBox, "exec", lambda box: shown.append(box.text()) or 0)
    monkeypatch.setattr(page.list, "refresh", lambda: None)
    row = {"disk_id": "3", "class": "BLANK", "can_prepare": False, "refusal": "erase_disabled",
           "message": "This drive is empty. Setting up drives is not enabled.", "serial": "S1234"}
    page.list.drives = [row]
    page._show_drives()
    page.table.selectRow(0)
    page._erase()
    assert "Settings › Storage" in shown[-1] and "erase_disabled" not in shown[-1]
    # Erasing turned on since the list was read: the page reads it again before deciding.
    w.ctx.cfg = replace(w.ctx.cfg, drives=replace(w.ctx.cfg.drives, erase_enabled=True))
    refreshed: list[int] = []
    monkeypatch.setattr(page.list, "refresh", lambda: refreshed.append(1))
    opened: list[dict] = []
    monkeypatch.setattr(drive_pages, "EraseDialog", lambda d, *_a: opened.append(d) or _Rejecting())
    page._erase()
    assert refreshed and not opened                       # waits for the fresh list
    page.list.drives = [dict(row, can_prepare=True, refusal=None)]
    page._show_drives()                                   # the fresh list arrives
    assert opened and opened[0]["can_prepare"]
    w.close()


class _Rejecting:
    def exec(self) -> int:
        return QDialog.DialogCode.Rejected


def test_pages_scroll_instead_of_squeezing(app: QApplication, tmp_path: Path) -> None:
    w = _window(app, tmp_path, admin=True)
    for key in ("camera", "drives", "storage", "schedule"):
        page = w.pages[key]
        assert page.scrolls and page.scroll.widgetResizable()
    assert not w.pages["status"].scrolls
    cam = w.pages["camera"].live.picture
    assert cam.width() * 9 == cam.height() * 16
    w.close()


# ---------------------------------------------------------------------- first-run setup
def test_setup_in_the_window_without_a_camera(app: QApplication, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.gui.app import setup_page

    monkeypatch.setattr(setup_page, "default_layout", lambda: _layout(tmp_path))
    monkeypatch.setattr(base.QMessageBox, "information", lambda *a, **k: None)
    configured: list[Path] = []
    w = AppWindow(None, None, admin=True, appearance="light", auto_refresh=False,
                  on_configured=lambda cfg, p: configured.append(p))
    w.show()
    assert w.section_names() == ["Set up this computer"]
    page = w.pages["setup"]
    page.later.setChecked(True)
    assert not page.host.isEnabled()
    page.name.setText("Gate")
    page.tz.setEditText("America/Chicago")
    page._setup()
    _drain(app, 0.5)
    assert configured == [_layout(tmp_path).config_file]
    cfg = load_config(configured[0])
    assert cfg.camera.pending and cfg.camera.name == "Gate"
    assert len(w.section_names()) == 6 and w.ctx.cfg is not None                    # the full window now
    w.close()


def test_setup_needs_administrator(app: QApplication) -> None:
    w = AppWindow(None, None, admin=False, appearance="light", auto_refresh=False)
    page = w.pages["setup"]
    assert not page.go.isEnabled()
    assert any(b.text() == "Continue as administrator…" for b in page.findChildren(QPushButton))
    assert all(not e.isEnabled() for e in page.findChildren(QLineEdit))
    w.close()


def test_appearance_is_remembered(app: QApplication, tmp_path: Path) -> None:
    from sentinel.gui.prefs import load_appearance

    w = _window(app, tmp_path, admin=False)
    w.set_appearance("dark")
    assert load_appearance(tmp_path / "prefs.json") == "dark" and w.ctx.palette.dark
    w.set_appearance("light")
    w.close()


# ---------------------------------------------------------------------- window review fixes (2026-11-10)
def test_administrator_actions_refused_on_a_file_others_can_change(app: QApplication, tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.gui.app import window as winmod

    monkeypatch.setattr(winmod, "admin_config_problem", lambda _p, _c: "can be changed by non-administrators")
    w = _window(app, tmp_path, admin=True)
    assert not w.ctx.admin and not w.pages["camera"].save_btn.isEnabled()
    assert "non-administrators" in w.access_label.text() and not w.unlock_btn.isVisible()
    w.close()


def test_window_does_not_close_while_the_recorder_restarts(app: QApplication, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    release = threading.Event()
    monkeypatch.setattr(base, "restart_recorder", lambda p, c: release.wait(5) and "Restarted.")
    monkeypatch.setattr(QMessageBox_, "information", lambda *a, **k: None)
    w = _window(app, tmp_path, admin=True)
    w.restart_recorder()
    w.restart_recorder()                                  # merged, not a second overlapping restart
    assert w.busy_work() == ["restarting the recorder"]
    w.close()
    assert w.isVisible()                                  # refused
    release.set()
    _drain(app, 0.5)
    w.close()
    assert not w.isVisible()


def test_save_keeps_a_backup_and_never_moves_the_live_file(tmp_path: Path) -> None:
    TEXT = '[camera]' + chr(10) + 'id = "C"' + chr(10)
    p = tmp_path / "sentinel.toml"
    p.write_text(TEXT, encoding="utf-8")
    from sentinel.gui.app.configedit import ConfigEditError, save_sections

    with pytest.raises(ConfigEditError):                  # invalid: nothing written, original intact
        save_sections(p, {"motion": {"keep_recording_s": 1}})
    assert p.read_text(encoding="utf-8") == TEXT and not list(tmp_path.glob("*.bak"))


def test_rtsps_camera_stays_rtsps(app: QApplication, tmp_path: Path) -> None:
    w = _window(app, tmp_path, admin=True)
    page = w.pages["camera"]
    page.secure.setChecked(True)
    assert page.tables()["camera"]["url"].startswith("rtsps://")
    w.close()


def test_status_buttons_are_restored_not_enabled(app: QApplication, tmp_path: Path) -> None:
    w = _window(app, tmp_path, admin=False)
    page = w.pages["drives"]
    assert not page.erase_btn.isEnabled()
    page.log.run([sys.executable, "-c", "print('ok')"], "test", busy=[page.erase_btn])
    _drain(app, 3.0)
    assert not page.erase_btn.isEnabled()                 # stays disabled for an operator
    w.close()


def test_refreshing_a_table_clears_the_selection(app: QApplication) -> None:
    t = base.table(["A"])
    base.fill(t, [["1"], ["2"]], ["d1", "d2"])
    t.selectRow(1)
    base.fill(t, [["1"], ["3"]], ["d1", "d3"])
    assert base.selected_data(t) is None


def test_storage_page_saves_erase_policy(app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "restart_recorder", lambda p, c: "Restarted.")
    w = _window(app, tmp_path, admin=True)
    page = w.pages["storage"]
    page.erase_on.setChecked(True)
    page.serial_chars.setValue(6)
    page.save()
    _drain(app, 0.3)
    cfg = load_config(w.ctx.cfg_path)
    assert cfg.drives.erase_enabled and cfg.drives.serial_confirm_chars == 6 and cfg.archive.enabled
    w.close()


def test_operator_can_request_an_archive_swap(app: QApplication, tmp_path: Path) -> None:
    """Swapping the archive drive is an operator task: it works without administrator rights."""
    w = _window(app, tmp_path, admin=False)
    page = w.pages["drives"]
    assert page.swap_btn.isEnabled() and page.resume_btn.isEnabled()
    assert not page.erase_btn.isEnabled() and not page.eject_btn.isEnabled()
    page._swap()
    from sentinel.archive.worker import operator_request_dir

    assert (operator_request_dir(w.ctx.cfg) / "swap.request").is_file()
    assert "ready to be taken out" in page.log.toPlainText()     # the copier never shows SAFE TO REMOVE
    w.close()


def test_cancel_swap_is_shown_only_while_a_swap_is_under_way(app: QApplication, tmp_path: Path) -> None:
    """Owner request 2026-11-11: "Keep using this drive" was always shown though it only undoes a swap."""
    from types import SimpleNamespace

    w = _window(app, tmp_path, admin=True)
    page = w.pages["drives"]
    assert page.resume_btn.text().strip() == "Cancel swap"

    def shown(arc: dict) -> bool:
        page.on_status(SimpleNamespace(doc={"archive": arc}))
        return not page.resume_btn.isHidden()

    assert not shown({})
    assert not shown({"running": True, "state": "UP_TO_DATE", "drive_label": "SENTINEL_1"})
    assert not shown({"running": True, "state": "COPYING", "removal": None})
    assert shown({"running": True, "state": "SWAP_PENDING"})
    assert shown({"running": True, "state": "SAFE_TO_REMOVE"})
    assert shown({"running": True, "state": "REMOVE_MANUALLY"})
    # Two drives: the full one waits to be taken out while copying goes on to the other.
    assert shown({"running": True, "state": "COPYING", "removal": {"label": "SENTINEL_1", "safe": True}})
    assert not shown({"running": False, "state": "SWAP_PENDING"})   # copier not running: nothing to cancel
    w.close()


def test_return_to_service_needs_every_answer_and_the_office_check(app: QApplication, tmp_path: Path) -> None:
    """0.3.9: the agent (an administrator) returns a drive the office has copied and checked."""
    from sentinel.gui.app.drive_pages import ReturnDialog

    w = _window(app, tmp_path, admin=True)
    assert w.pages["drives"].return_btn.isEnabled()
    dlg = ReturnDialog({"label": "SENTINEL_1", "class": "READY", "role": "archive"})
    assert not dlg.ok.isEnabled()
    dlg.destination.setText("Office archive drive 3")
    dlg.copied_by.setText("Clerk A")
    dlg.who.setText("agent7")
    assert not dlg.ok.isEnabled()                # the office check must be confirmed
    dlg.checked.setChecked(True)
    assert dlg.ok.isEnabled()
    args = dlg.args()
    assert args[:6] == ["--admin-id", "agent7", "--destination", "Office archive drive 3", "--copied-by", "Clerk A"]
    assert args[-1] == "--checked" and len(args[-2]) == 10
    w.close()
    w2 = _window(app, tmp_path / "operator", admin=False)
    assert not w2.pages["drives"].return_btn.isEnabled()
    w2.close()


def test_refusing_a_drive_with_recordings_says_why(app: QApplication, tmp_path: Path) -> None:
    """Owner report 2026-11-12: a new SSD was refused with no reason given."""
    w = _window(app, tmp_path, admin=True)
    page = w.pages["drives"]
    shown: list[str] = []
    page.problem = shown.append
    page._cannot_erase({"refusal": "sentinel_evidence", "reasons": ["known_hardware"],
                        "message": "This drive holds Sentinel recordings and will not be erased."})
    assert "Why: Sentinel's records show it set up a drive with this serial number before." in shown[0]
    page._cannot_erase({"refusal": "lookalike", "reasons": ["empty_volume"], "message": "Another connected drive"})
    assert "Why:" not in shown[1] and "Nothing was erased." in shown[1]
    w.close()


def test_general_page_sets_and_clears_the_organisation(app: QApplication, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "restart_recorder", lambda p, c: "Restarted.")
    w = _window(app, tmp_path, admin=True)
    page = w.pages["general"]
    assert page.org.text() == ""                                   # unnamed by default
    page.org.setText("Bragg RA")
    page.save()
    _drain(app, 0.3)
    assert load_config(w.ctx.cfg_path).site.organization == "Bragg RA"
    page.org.setText("")
    page.save()
    _drain(app, 0.3)
    assert load_config(w.ctx.cfg_path).site.organization == ""
    w.close()


def test_general_page_sets_the_recording_file_length(app: QApplication, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner request 2026-11-11: one file per hour by default instead of hundreds of 5-minute files."""
    monkeypatch.setattr(base, "restart_recorder", lambda p, c: "Restarted.")
    w = _window(app, tmp_path, admin=True)
    page = w.pages["general"]
    assert page.seglen.currentText() == "1 hour" and page.seglen.currentData() == 3600
    assert [page.seglen.itemText(i) for i in range(page.seglen.count())] == ["15 minutes", "30 minutes", "1 hour"]
    page.seglen.setCurrentIndex(page.seglen.findData(900))
    page.save()
    _drain(app, 0.3)
    assert load_config(w.ctx.cfg_path).recording.segment_seconds == 900
    # a length set by hand in the file stays selectable and is not changed by saving
    text = w.ctx.cfg_path.read_text("utf-8").replace("segment_seconds = 900", "segment_seconds = 300")
    w.ctx.cfg_path.write_text(text, "utf-8")
    w.ctx.config_changed(load_config(w.ctx.cfg_path))
    page.load()
    assert page.seglen.currentText() == "5 minutes (current)"
    assert page.tables()["recording"]["segment_seconds"] == 300
    w.close()


def test_schedule_days_are_picked_not_typed_and_round_trip(app: QApplication, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.gui.app.settings_pages import days_from_config, days_to_config

    assert days_to_config({"mon", "tue", "wed", "thu", "fri"}) == ["weekdays"]
    assert days_to_config({"sat", "sun"}) == ["weekend"] and days_to_config({"wed", "mon"}) == ["mon", "wed"]
    assert days_from_config(["weekend", "Monday"]) == {"sat", "sun", "mon"}
    monkeypatch.setattr(base, "restart_recorder", lambda p, c: "Restarted.")
    w = _window(app, tmp_path, admin=True)
    page = w.pages["schedule"]
    page.enabled.setChecked(True)
    page.weekly.add("record", {"mon", "wed"}, "07:30", "00:00")              # until midnight
    page.save()
    _drain(app, 0.3)
    sch = load_config(w.ctx.cfg_path).schedule
    assert [(p.action, p.days, p.start_s, p.end_s) for p in sch.weekly] == [("record", (0, 2), 27000, 86400)]
    page.load()
    picker = page.weekly.t.cellWidget(0, 1)
    assert picker.days() == {"mon", "wed"}
    w.close()


def test_days_off_pauses_and_comes_back_by_itself(app: QApplication, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner request: on leave for 6 days in December; no recording, nothing shut down, recording
    resumes the day he is back."""
    from PySide6.QtCore import QDate

    from sentinel.gui.app import settings_pages as sp
    from sentinel.schedule import RecordingSchedule

    monkeypatch.setattr(base, "restart_recorder", lambda p, c: "Restarted.")

    def accept(dlg: QDialog) -> int:
        dlg.first.setDate(QDate(2026, 12, 14))
        dlg.back.setDate(QDate(2026, 12, 20))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(sp.DaysOffDialog, "exec", accept)
    w = _window(app, tmp_path, admin=True)
    page = w.pages["schedule"]
    page._days_off()
    assert page.enabled.isChecked() and "Days off added" in page.result.text()
    page.save()
    _drain(app, 0.3)
    cfg = load_config(w.ctx.cfg_path)
    assert [(d["action"], d["start"], d["end"]) for d in page.dates.rows()] == [
        ("pause", "2026-12-14 00:00", "2026-12-20 00:00")]
    s = RecordingSchedule(cfg.schedule)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(cfg.schedule.timezone)
    at = lambda d, h: datetime(2026, 12, d, h, tzinfo=tz).timestamp()    # noqa: E731
    assert s.mode_at(at(13, 23)) == "record" and s.mode_at(at(14, 9)) == "pause" and s.mode_at(at(19, 23)) == "pause"
    assert s.mode_at(at(20, 0)) == "record"                                # back by itself
    assert s.next_change(at(16, 12)) == at(20, 0)
    w.close()


def test_a_change_that_breaks_the_recorder_is_rolled_back(app: QApplication, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner request: if a change leaves the tool not working, put the last working settings back."""
    from sentinel.gui.app import window as winmod
    from sentinel.selfcheck import good_path

    monkeypatch.undo()                                       # this test wants the real watch ...
    monkeypatch.setattr(winmod, "admin_config_problem", lambda _p, _c: "")
    monkeypatch.setattr(base, "restart_recorder", lambda p, c: "Restarted.")
    told: list[str] = []
    monkeypatch.setattr(winmod.QMessageBox, "information", lambda _w, _t, text: told.append(text))
    w = _window(app, tmp_path, admin=True)
    assert w.check_now(require_saved_file=False).ok          # healthy: remembered as known good
    good = good_path(w.ctx.cfg_path)
    assert good.is_file()
    page = w.pages["motion"]
    page.keep.setValue(600)
    page.save()                                              # starts the watch
    _drain(app, 0.3)
    assert w.watching()
    bad = status_doc(base_measurements(component_errors=["C-001"]), time.time())
    write_doc(w.ctx.cfg.status_path, bad)                    # the recorder is not working now
    w._watch["started"] -= 1000                              # 3 minutes have passed
    w._watch_tick()
    _drain(app, 0.3)
    assert w.ctx.cfg_path.read_bytes() == good.read_bytes()  # last working settings are back
    assert load_config(w.ctx.cfg_path).motion.keep_recording_s != 600
    assert told and "put back" in told[0]
    w.watch_timer.stop()
    w._watch = None
    w.close()


def test_a_working_change_becomes_the_new_known_good(app: QApplication, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.gui.app import window as winmod
    from sentinel.selfcheck import good_path

    monkeypatch.undo()
    monkeypatch.setattr(winmod, "admin_config_problem", lambda _p, _c: "")
    monkeypatch.setattr(base, "restart_recorder", lambda p, c: "Restarted.")
    w = _window(app, tmp_path, admin=True)
    page = w.pages["motion"]
    page.keep.setValue(600)
    page.save()
    _drain(app, 0.3)
    for _ in range(3):
        w._watch_tick()
    assert not w.watching()
    assert load_config(good_path(w.ctx.cfg_path)).motion.keep_recording_s == 600
    w.close()


# ---------------------------------------------------------------------- updates (owner request 2026-11-10)
def test_update_file_is_checked_then_handed_to_its_own_window(app: QApplication, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.gui.app import update_page
    from sentinel.update import package
    from sentinel.update.package import UpdateRefused

    w = _window(app, tmp_path, admin=True)
    tab = w.pages["system"].updates
    monkeypatch.setattr(update_page, "app_dir", lambda: tmp_path / "Sentinel")
    started: list[list[str]] = []
    tab._popen = lambda argv, **kw: started.append(argv)
    said: list[str] = []
    monkeypatch.setattr(update_page.QMessageBox, "warning", lambda _p, _t, text: said.append(text))

    def refuse(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise UpdateRefused("bad_signature", "The update file is damaged.")

    monkeypatch.setattr(package, "read_package", refuse)
    tab.apply_file(str(tmp_path / "u.sntu"))
    assert started == [] and "damaged" in said[-1] and w.isVisible()

    class Pkg:
        def summary(self) -> str:
            return "Sentinel 0.3.1 (updates 0.3.0 or later)"

    monkeypatch.setattr(package, "read_package", lambda *_a, **_k: Pkg())
    monkeypatch.setattr(update_page.QMessageBox, "question", lambda *_a, **_k: update_page.QMessageBox.StandardButton.Yes)
    tab.apply_file(str(tmp_path / "u.sntu"))
    argv = started[-1]
    assert argv[argv.index("update"):argv.index("update") + 2] == ["update", "apply"] and "--yes" in argv
    assert argv[argv.index("--config") + 1] == str(w.ctx.cfg_path)
    app.processEvents()
    assert not w.isVisible()                               # closed without asking, for the swap


def test_after_an_update_the_window_reports_once_and_goes_back_if_the_recorder_does_not_run(
        app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.selfcheck import Check, Report
    from sentinel.gui.app import window as winmod
    import json

    from sentinel.update import apply as ap

    monkeypatch.undo()                                     # the real watch_after_change
    monkeypatch.setattr(winmod, "admin_config_problem", lambda _p, _c: "")
    w = _window(app, tmp_path, admin=True)
    prog = tmp_path / "Sentinel"
    folders = ap.Folders(prog)
    (folders.previous).mkdir(parents=True)
    (folders.previous / "BUILD-MANIFEST.json").write_text('{"version": "0.3.0"}', "utf-8")
    ap.write_result(folders, "updated", None, "Sentinel updated from 0.3.0 to 0.3.1.", watch=True)
    doc = json.loads(folders.result.read_text("utf-8"))
    doc["action"] = "update"
    folders.result.write_text(json.dumps(doc), "utf-8")
    monkeypatch.setattr(w, "_update_folders", lambda: folders)
    shown: list[str] = []
    monkeypatch.setattr(w, "_notice", lambda text, warning=False: shown.append(("WARN " if warning else "") + text))
    w.show_update_result()
    assert shown == ["Sentinel updated from 0.3.0 to 0.3.1."] and w.watching() and w._watch["mode"] == "update"
    assert ap.read_result(folders)["shown"]
    w.show_update_result()
    assert len(shown) == 1                                 # only once
    went_back: list[tuple[str, bool]] = []
    monkeypatch.setattr(w.pages["system"].updates, "go_back", lambda reason="", ask=True: went_back.append((reason, ask)))
    bad = Report([Check("Settings file", True, "valid"), Check("Recorder running", False, "no status")])
    monkeypatch.setattr(w, "check_now", lambda require_saved_file=True, ignore_alerts=frozenset(): bad)
    t = [0.0]
    w.clock = lambda: t[0]
    w._watch["started"] = 0.0
    t[0] = winmod.WATCH_LIMIT_S + 1
    w._watch_tick()
    assert went_back and not went_back[0][1] and "did not work" in went_back[0][0]
    assert shown[-1].startswith("WARN") and "goes back to version 0.3.0" in shown[-1]
    assert not ap.read_result(folders)["watch"]
    w._closing_for_update = True
    w.close()


def test_camera_problems_after_an_update_do_not_roll_back_by_themselves(app: QApplication, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.selfcheck import Check, Report
    from sentinel.gui.app import window as winmod

    w = _window(app, tmp_path, admin=True)
    went_back: list[str] = []
    monkeypatch.setattr(w.pages["system"].updates, "go_back", lambda reason="", ask=True: went_back.append(reason))
    shown: list[str] = []
    monkeypatch.setattr(w, "_notice", lambda text, warning=False: shown.append(text))
    w._update_watch_failed(Report([Check("Recorder running", True, "answering"),
                                   Check("Camera sends video", False, "no signal")]))
    assert not went_back and "usually not caused by the update" in shown[-1]
    w.close()


# ---------------------------------------------------------------------- field report 2026-11-11 (laptop)
def test_times_are_picked_from_a_list_and_can_be_typed(app: QApplication) -> None:
    from sentinel.gui.app.settings_pages import MIDNIGHT, TimeCombo, normal_time

    start = TimeCombo("07:00")
    assert start.isEditable() and start.findData("07:15") >= 0 and start.value() == "07:00"
    start.setEditText("7:43")                                    # a time not in the list, typed
    assert start.value() == "07:43"
    end = TimeCombo("00:00", end=True)
    assert end.value() == MIDNIGHT and end.currentText() == "Midnight" and end.findData("00:00") == -1
    end.set_value("24:00")
    assert end.value() == MIDNIGHT
    end.set_value("18:00")
    assert end.value() == "18:00"
    assert normal_time("0730") == "07:30" and normal_time("25:00") is None


def test_each_schedule_row_has_its_own_remove_button(app: QApplication) -> None:
    from PySide6.QtCore import QDate, QDateTime, QTime

    from sentinel.gui.app.settings_pages import DatesTable, WeeklyTable

    wt = WeeklyTable()
    wt.add("record", {"mon"}, "07:00", "18:00")
    wt.add("motion", {"sat"}, "13:03", "24:00")
    wt.add("pause", {"sun"}, "01:00", "02:00")
    wt.t.cellWidget(1, 4).click()                                # the middle row's cross
    assert [(r["action"], r["start"], r["end"]) for r in wt.rows()] == [("record", "07:00", "18:00"),
                                                                       ("pause", "01:00", "02:00")]
    dt = DatesTable()
    a = QDateTime(QDate(2026, 11, 12), QTime(0, 0))
    dt.add("pause", a, a.addDays(6))
    dt.add("pause", a.addDays(47), a.addDays(48))
    dt.t.cellWidget(0, 3).click()
    assert dt.rows() == [{"action": "pause", "start": "2026-12-29 00:00", "end": "2026-12-30 00:00"}]


def test_highlighted_rows_keep_readable_text() -> None:
    from PySide6.QtGui import QColor

    from sentinel.gui.app.style import selection_colour
    from sentinel.gui.theme import DARK, LIGHT

    def lum(c: str) -> float:
        q = QColor(c)
        ch = [x / 255 for x in (q.red(), q.green(), q.blue())]
        ch = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]

    for p in (LIGHT, DARK):
        a, b = sorted((lum(selection_colour(p)), lum(p.text)), reverse=True)
        assert (a + 0.05) / (b + 0.05) >= 7.0, p.dark                # WCAG AAA for normal text


def test_the_apply_button_itself_opens_the_file_browser(app: QApplication, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Field report 2026-11-11: pressing 'Apply update file…' did nothing (clicked(bool) passed False
    as the path, so no file browser opened). Press the real buttons."""
    from sentinel.gui.app import update_page

    w = _window(app, tmp_path, admin=True)
    tab = w.pages["system"].updates
    monkeypatch.setattr(update_page, "app_dir", lambda: tmp_path / "Sentinel")
    asked: list[str] = []
    monkeypatch.setattr(update_page.QFileDialog, "getOpenFileName",
                        lambda *a, **k: (asked.append(a[3] if len(a) > 3 else k.get("filter", "")), ("", ""))[1])
    tab.b_apply.click()
    assert asked and ".sntu" in asked[0]                     # the browser opened, filtered on update files
    went: list[str] = []
    monkeypatch.setattr(tab, "go_back", lambda reason="", ask=True: went.append(reason))
    tab.b_back.setEnabled(True)
    tab.b_back.click()
    assert went == [""]
    w.close()


def test_while_updating_the_window_says_so_instead_of_system_error(app: QApplication, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Field report 2026-11-11: during the update the window showed 'System error' with no explanation."""
    from dataclasses import asdict

    from sentinel.update import apply as ap

    w = _window(app, tmp_path, admin=True, doc=False)          # no status from the recorder: it is stopped
    folders = ap.Folders(tmp_path / "Sentinel")
    ap.Pending("update", "0.3.3", "0.3.4", str(folders.app), "s", "p", None,
               asdict(ap.RecordingState())).write(folders.state)
    monkeypatch.setattr(w, "_update_folders", lambda: folders)
    w.refresh()
    assert w.sb_state.text().startswith("Updating to Sentinel 0.3.4: recording restarts by itself")
    # Review 2026-11-12: a state file left by a failed or cancelled update never hides a running recorder.
    write_doc(w.ctx.cfg.status_path, status_doc(base_measurements(), time.time(), last_segment_end=time.time()))
    w.refresh()
    assert "Updating" not in w.sb_state.text()
    folders.state.unlink()
    w.refresh()
    assert "Updating" not in w.sb_state.text()
    w.close()


def test_swap_with_the_copier_stopped_says_so_and_a_waiting_command_can_be_stopped(
        app: QApplication, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Field report 2026-11-12: Swap ran 10 minutes against a stopped archive copier, and every other
    button only said "Wait until the current command has finished"."""
    import sys

    from sentinel.archive.status import COPIER_NOT_RUNNING
    from sentinel.gui.app import base

    w = _window(app, tmp_path, admin=True)
    page = w.pages["drives"]
    shown: list[str] = []
    page.problem = shown.append
    page._arc_running = False
    page._swap()
    assert shown == [COPIER_NOT_RUNNING] and page.log.current is None

    # A command that only waits may be stopped from the "still running" question; others may not.
    log = page.log
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    assert log.run(sleeper, "Archive swap", stop_note="The copier carries on.") is not None
    told: list[str] = []
    monkeypatch.setattr(base.QMessageBox, "exec", lambda box: told.append(box.text()) or 0)
    monkeypatch.setattr(base.QMessageBox, "clickedButton",
                        lambda box: next(b for b in box.buttons() if b.text() == "Stop it and go on"))
    assert log.run([sys.executable, "-c", "pass"], "Refresh") is not None
    assert "“Archive swap” is still running" in told[0]
    assert "Archive swap stopped. The copier carries on." in log.toPlainText()
    _drain(app, 1.0)

    assert log.run(sleeper, "Erase and set up disk 1") is not None      # no stop_note: never stopped
    infos: list[str] = []
    monkeypatch.setattr(base.QMessageBox, "information", lambda _p, _t, text: infos.append(text))
    assert log.run([sys.executable, "-c", "pass"], "Refresh") is None
    assert "“Erase and set up disk 1” is still running" in infos[0]
    assert log.current is not None and log.current.running()
    log.current.kill()
    log.current.proc.waitForFinished(5000)
    w.close()


def test_rename_is_for_administrators_and_runs_the_rename_command(app: QApplication, tmp_path: Path,
                                                                  monkeypatch) -> None:  # noqa: ANN001
    """Owner request 2026-11-12: drives carry a sticker with a permanent name such as SSD_1."""
    from sentinel.gui.app import drive_pages

    w = _window(app, tmp_path, admin=True)
    page = w.pages["drives"]
    assert page.rename_btn.isEnabled()
    ran: list[list[str]] = []
    monkeypatch.setattr(page.log, "run", lambda argv, *a, **k: ran.append(argv))
    page._selected = lambda: {"class": "READY", "label": "SENTINEL_3", "volumes": [{"mount_paths": ["E:\\"]}]}
    monkeypatch.setattr(drive_pages, "_letters", lambda _d: ["E:"])
    monkeypatch.setattr(drive_pages.QInputDialog, "getText", lambda *a, **k: ("ssd_1", True))
    page._rename()
    assert ran and ran[0][-6:-2] == ["--drive", "E:", "--name", "SSD_1"]
    shown: list[str] = []
    page.problem = shown.append
    page._selected = lambda: {"class": "BLANK"}
    page._rename()
    assert "Only a drive Sentinel has set up can be renamed" in shown[0]
    w.close()
    w2 = _window(app, tmp_path / "operator", admin=False)
    assert not w2.pages["drives"].rename_btn.isEnabled()
    w2.close()


def test_the_hour_being_recorded_counts_as_live_on_the_timeline(app, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Review 2026-11-12: with one-hour files the open hour was shown as 'no information'."""
    from sentinel.gui.app import status_pages

    seen: dict[str, Any] = {}

    def fake(*_a: Any, **kw: Any) -> Timeline:
        seen.update(kw)
        raise OSError("stop here")
    monkeypatch.setattr(status_pages, "load_timeline", fake)
    w = _window(app, tmp_path, admin=True)
    try:
        w.pages["recordings"].on_show()
    except OSError:
        pass
    assert seen["live_slack_s"] >= w.ctx.cfg.recording.segment_seconds
    w.close()


def test_a_status_that_stopped_updating_shows_nothing_old_as_current(app, tmp_path: Path) -> None:  # noqa: ANN001
    """Review 2026-11-12: a 10-minute-old document still showed the camera 'connected' and its old alerts."""
    now = time.time()
    doc = status_doc(base_measurements(), now, heartbeat_age_s=600.0, last_segment_end=now - 700)
    doc["camera_link"] = "connected"
    doc["alerts"] = [make_alert("R-001", Severity.WARNING).to_dict()]
    w = _window(app, tmp_path, admin=True, doc=doc)
    w.refresh()
    page = w.pages["status"]
    page.on_status(w.ctx.read)
    codes = [page.alerts.topLevelItem(i).text(0) for i in range(page.alerts.topLevelItemCount())]
    assert codes == ["X-001"]
    assert "unknown" in page.facts["camera"].text() and "unknown" in page.items["link"].text(1)
    w.close()


def test_restart_refuses_a_settings_file_that_does_not_load(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Review 2026-11-12: Restart recorder stopped a working recorder to start it with a broken file (C-001)."""
    path = _setup(tmp_path)
    cfg = load_config(path)
    path.write_text("[camera\nbroken", encoding="utf-8")
    import sentinel.launcher.control as control_mod

    monkeypatch.setattr(control_mod, "select_control", lambda *_a: pytest.fail("the recorder was touched"))
    msg = base.restart_recorder(path, cfg)
    assert msg.startswith("The recorder was not restarted") and "settings file cannot be used" in msg


def test_restart_also_restarts_the_archive_copier(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    path = _setup(tmp_path)
    cfg = load_config(path)
    calls: list[str] = []

    class Ctl:
        def is_running(self) -> bool:
            return True

        def stop(self, _t: float) -> bool:
            calls.append("stop")
            return True

        def start(self):  # noqa: ANN202
            calls.append("start")
            return type("R", (), {"ok": True})()

        def restart_archive(self) -> None:
            calls.append("archive")
    import sentinel.launcher.control as control_mod

    monkeypatch.setattr(control_mod, "select_control", lambda *_a: Ctl())
    assert base.restart_recorder(path, cfg).startswith("The recorder was restarted")
    assert calls == ["stop", "start", "archive"]


def test_the_schedule_page_never_rewrites_times_or_dates_it_cannot_show(app, tmp_path: Path) -> None:  # noqa: ANN001
    """Review 2026-11-12: '2026-12-24T18:00' loaded as 'now', '07:30:00' as 00:00, and a typed '25:00' saved as
    the time picked before."""
    from PySide6.QtCore import QDate, QTime

    assert settings_pages.normal_time("07:30:00") == "07:30"
    for text, end, want in (("2026-12-24T18:00", False, (2026, 12, 24, 18, 0)),
                            ("2026-12-24 8:00", False, (2026, 12, 24, 8, 0)),
                            ("2026-12-24 24:00", True, (2026, 12, 25, 0, 0)),
                            ("2026-12-24", True, (2026, 12, 25, 0, 0))):
        dt = settings_pages.parse_date_value(text, end)
        assert dt.date() == QDate(*want[:3]) and dt.time() == QTime(*want[3:]), text
    with pytest.raises(ValueError):
        settings_pages.parse_date_value("next tuesday", False)
    combo = settings_pages.TimeCombo("07:00")
    combo.setEditText("25:00")
    with pytest.raises(ValueError, match="not a time of day"):
        combo.value()
    combo.set_value("7:3")
    assert combo.currentText() == "7:3"                     # shown as written, not 00:00

    w = _window(app, tmp_path, admin=True)
    path = w.ctx.cfg_path
    text = path.read_text("utf-8")
    text += ('\n[schedule]\nenabled = true\ndefault = "record"\n\n[[schedule.dates]]\naction = "pause"\n'
             'start = "2026-12-24 18:00:30"\nend = "2026-12-26"\n')
    path.write_text(text, "utf-8")
    page = w.pages["schedule"]
    page.load()
    assert "cannot be shown here" in page.result.text()
    assert page.tables()["schedule"]["dates"][0]["start"] == "2026-12-24 18:00:30"   # kept exactly
    w.close()


def test_settings_pages_are_tabs_of_one_section(app: QApplication, tmp_path: Path) -> None:
    """GUI simplification 2026-11-13: six sections; links to a settings page open its tab."""
    w = _window(app, tmp_path, admin=True)
    w.navigate("motion")
    app.processEvents()
    assert w.current_page().key == "motion" and w.nav.currentItem().text() == "Settings"
    w.settings_host.select("camera")
    assert w.current_page().key == "camera"
    w.navigate("status")
    assert w.current_page().key == "status"
    w.close()


def test_drives_list_hides_what_cannot_be_used_and_shows_only_actions_that_apply(
        app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Field report 2026-11-13: the computer's own disk and an empty card reader were listed, and every button
    was shown whatever drive was selected."""
    w = _window(app, tmp_path, admin=True)
    page = w.pages["drives"]
    monkeypatch.setattr(page.list, "refresh", lambda: None)
    system = {"disk_id": "0", "class": "UNSUITABLE", "reasons": ["system_disk"], "size_bytes": 5 * 10**11,
              "mount_paths": ["C:\\"]}
    reader = {"disk_id": "2", "class": "UNSUITABLE", "reasons": ["not_ready"], "size_bytes": 0}
    archive = {"disk_id": "1", "class": "READY", "role": "archive", "label": "SSD_1", "size_bytes": 2 * 10**12,
               "mount_paths": ["E:\\"], "serial": "V469"}
    blank = {"disk_id": "3", "class": "BLANK", "can_prepare": True, "size_bytes": 10**12, "mount_paths": ["F:\\"]}
    page.list.drives = [system, archive, reader, blank]
    page._show_drives()
    assert page.table.rowCount() == 2 and page.table.item(0, 0).text() == "E:"
    assert page.table.item(0, 1).text() == "SSD_1" and page.table.item(0, 2).text() == "archive drive"
    assert all(b.isHidden() for b in page.action_btns)            # nothing selected: no actions
    page.table.selectRow(0)
    visible = {b.text().strip() for b in page.action_btns if not b.isHidden()}
    assert visible == {"Return to service…", "Rename…", "Safely remove…"}
    page.table.selectRow(1)
    assert {b.text().strip() for b in page.action_btns if not b.isHidden()} == {"Erase and set up…", "Safely remove…"}
    page.show_all.setChecked(True)
    assert page.table.rowCount() == 4
    # A drive plugged in while the page is open: the list is read again by itself.
    refreshed: list[int] = []
    monkeypatch.setattr(page.list, "refresh", lambda: refreshed.append(1))
    monkeypatch.setattr(drive_pages, "drive_letters_now", lambda: frozenset({"C:\\", "Z:\\"}))
    page._poll_drives()
    page._poll_drives()
    assert refreshed == [1]
    w.close()


def test_the_drive_a_swap_could_not_eject_gets_its_own_safely_remove_button(
        app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Field report 2026-11-13: the swap ended with "remove it with Windows"; the page now offers the removal."""
    from types import SimpleNamespace

    w = _window(app, tmp_path, admin=True)
    page = w.pages["drives"]
    monkeypatch.setattr(page.list, "refresh", lambda: None)
    runs: list[list[str]] = []
    monkeypatch.setattr(page.log, "run", lambda argv, *a, **k: runs.append(argv))
    page.list.drives = [{"disk_id": "1", "class": "READY", "role": "archive", "label": "SENTINEL_4",
                         "drive_uuid": "u4", "fingerprint": "fp4", "size_bytes": 2 * 10**12, "mount_paths": ["E:\\"]}]
    page._show_drives()
    arc = {"running": True, "state": "REMOVE_MANUALLY", "heartbeat_epoch": time.time(),
           "removal": {"drive_id": "drive_uuid:u4", "label": "SENTINEL_4", "safe": False}}
    page.on_status(SimpleNamespace(doc={"archive": arc}))
    assert not page.remove_btn.isHidden() and page.remove_btn.text().strip() == "Safely remove SENTINEL_4"
    assert page.swap_btn.isHidden()
    page.remove_btn.click()
    assert runs and runs[0][runs[0].index("--disk") + 1] == "1" and "fp4" in runs[0]
    page.on_status(SimpleNamespace(doc={"archive": dict(arc, removal={"drive_id": "drive_uuid:u4", "safe": True})}))
    assert page.remove_btn.isHidden() and not page.swap_btn.isHidden()
    # Two-bay dock: the copier says to take it out as it is (a removal could stop the other drive): no button.
    two = dict(arc, state="COPYING", removal={"drive_id": "drive_uuid:u4", "safe": False,
                                              "text": "SENTINEL_4 is ready to be taken out"})
    page.on_status(SimpleNamespace(doc={"archive": two}))
    assert page.remove_btn.isHidden()
    w.close()


def test_swap_and_safely_remove_ejects_the_drive_the_copier_could_not(
        app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner request 2026-11-13: one button swaps and safely removes; the page ejects the drive when the copier
    reports it finished but could not make it safe to remove by itself."""
    from types import SimpleNamespace

    w = _window(app, tmp_path, admin=True)
    page = w.pages["drives"]
    assert page.swap_btn.text().strip() == "Swap and safely remove"
    monkeypatch.setattr(page.list, "refresh", lambda: None)
    runs: list[list[str]] = []
    monkeypatch.setattr(page.log, "run", lambda argv, *a, **k: runs.append(argv))
    page.list.drives = [{"disk_id": "1", "class": "READY", "role": "archive", "label": "SENTINEL_4",
                         "drive_uuid": "u4", "fingerprint": "fp4", "size_bytes": 2 * 10**12, "mount_paths": ["E:\\"]}]
    page._show_drives()
    page._arc_running = True
    page._swap()
    assert runs[-1][runs[-1].index("archive") + 1] == "swap"
    copying = {"running": True, "state": "SWAP_PENDING", "heartbeat_epoch": time.time()}
    page.on_status(SimpleNamespace(doc={"archive": copying}))
    assert len(runs) == 1                                       # still copying: nothing removed yet
    waiting = {"running": True, "state": "REMOVE_MANUALLY", "heartbeat_epoch": time.time(),
               "removal": {"drive_id": "drive_uuid:u4", "label": "SENTINEL_4", "safe": False}}
    page.on_status(SimpleNamespace(doc={"archive": waiting}))
    assert len(runs) == 2 and runs[1][runs[1].index("drives") + 1] == "eject" and "fp4" in runs[1]
    page.on_status(SimpleNamespace(doc={"archive": waiting}))
    assert len(runs) == 2                                       # once only (the button stays for a retry)
    # Cancel swap: no removal afterwards.
    page._swap()
    page._resume = page._resume                                 # noqa: PLW0127
    monkeypatch.setattr(drive_pages, "Command", lambda *a, **k: SimpleNamespace(
        output=SimpleNamespace(connect=lambda *_: None), finished=SimpleNamespace(connect=lambda *_: None),
        start=lambda: None))
    page._resume()
    page.on_status(SimpleNamespace(doc={"archive": waiting}))
    assert len(runs) == 3                                       # the swap command only
    w.close()
