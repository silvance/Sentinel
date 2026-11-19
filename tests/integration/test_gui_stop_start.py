"""Stop recorder, wait, Start recording: the real window beside a real background recorder.

Field report 2026-11-19 (the end user's laptop): after System › Stop recorder the window said "Not recording"
for a few seconds and then "System error" (X-001, "recorder not responding"), with the camera "disconnected".
The recorder had done what it was told. The window treated every recorder that no longer writes its status
as one that stopped answering, although a recorder that shuts down in an orderly way says so in the last
thing it writes.

Here the recorder is the real one (``Sentinel launch`` starts it as a background process, ``Sentinel stop``
stops it through the stop channel, as the window's Stop recorder button does), the camera is the simulated
one, and the window is the real ``AppWindow``. The wait is longer than the 15 seconds after which the
window used to change its mind.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from conftest import requires_ffmpeg, tools_toml_for_tests  # noqa: E402
from harness import wait_for  # noqa: E402
from sentinel.config import load_config  # noqa: E402
from sentinel.gui.__main__ import prepare_qt_environment  # noqa: E402
from sentinel.sim.camera import SimulatedCamera  # noqa: E402

prepare_qt_environment()

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from sentinel.gui.app import base  # noqa: E402
from sentinel.gui.app.liveview import STOPPED_TEXT  # noqa: E402
from sentinel.gui.app.window import AppWindow  # noqa: E402
from sentinel.gui.viewmodel import ReadKind  # noqa: E402

pytestmark = [pytest.mark.integration, requires_ffmpeg]
ROOT = Path(__file__).resolve().parents[2]
STALE_AFTER_S = 15.0          # the window's limit for a status that stopped updating (AppWindow.refresh)


def _env() -> dict[str, str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    env.pop("SENTINEL_CONFIG", None)
    return env


def _sentinel(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "sentinel.launcher", *args], env=_env(), capture_output=True,
                          text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def _write_config(tmp: Path, url: str) -> Path:
    cfg = tmp / "sentinel.toml"
    cfg.write_text(f"""
[camera]
id = "CAM01"
name = "Test camera"
url = "{url}"
input_format = "mpegts"
development_source = true

[recording]
segment_seconds = 2
align_to_clock = false
connect_timeout_s = 10
stall_timeout_s = 6
io_timeout_s = 4
display_timezone = "America/Chicago"

[storage]
spool_dir = "spool"
data_dir = "data"
log_dir = "logs"
warning_free_gb = 0
critical_free_gb = 0

[service]
status_interval_s = 0.5

{tools_toml_for_tests()}""", encoding="utf-8")
    return cfg


def _overall(tmp: Path) -> str | None:
    try:
        return json.loads((tmp / "data" / "status.json").read_text("utf-8")).get("overall")
    except (OSError, ValueError):
        return None


def _pump(app: QApplication, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def test_stop_recorder_then_start_recording_from_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))      # the window starts the recorder as a child process
    monkeypatch.delenv("SENTINEL_CONFIG", raising=False)
    problems: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _s, _t, text, *a, **k: problems.append(text))
    win = None
    with SimulatedCamera(fps=15) as cam:
        cfg_path = _write_config(tmp_path, cam.url)
        first = _sentinel("launch", "--once", "--config", str(cfg_path))
        try:
            wait_for(lambda: _overall(tmp_path) == "RECORDING", 60, what="RECORDING: " + first.stdout[-300:])
            win = AppWindow(load_config(cfg_path), cfg_path, admin=False, appearance="light", auto_refresh=False,
                            prefs_path=tmp_path / "prefs.json")
            win.resize(1280, 800)
            win.show()
            win.navigate("status")
            _pump(app, 0.3)
            page = win.pages["status"]
            win.refresh()
            assert win.view is not None and win.view.headline == "Recording normally"
            assert page.start_box.isHidden()

            # ---- Stop recorder (what System › Stop recorder runs)
            stop = _sentinel("stop", "--config", str(cfg_path))
            assert stop.returncode == 0 and "has stopped" in stop.stdout, stop.stdout + stop.stderr
            stopped_at = time.monotonic()
            assert _overall(tmp_path) == "STOPPED"
            win.refresh()
            first_look = (win.view.headline, win.sb_alerts.text(), page.facts["camera"].text())
            assert first_look == ("Not recording", "Recorder stopped", "not in use (recorder stopped)")
            assert not page.start_box.isHidden()

            # ---- ...and it says the same after the 15 seconds at which it used to turn into "System error"
            _pump(app, max(0.0, STALE_AFTER_S + 3.0 - (time.monotonic() - stopped_at)))
            win.refresh()
            assert win.ctx.read is not None and win.ctx.read.kind is ReadKind.STOPPED
            assert win.ctx.read.age_s is not None and win.ctx.read.age_s > STALE_AFTER_S
            assert (win.view.headline, win.sb_alerts.text(), page.facts["camera"].text()) == first_look
            assert "System error" not in win.state_label.text() and "X-001" not in win.sb_alerts.text()
            assert page.supporting.text().startswith("The recorder was stopped at ")
            assert [page.alerts.topLevelItem(i).text(0) for i in range(page.alerts.topLevelItemCount())] == [""]
            page.live._tick()
            assert page.live.message == STOPPED_TEXT

            # ---- Start recording: the button under those words
            page.start_btn.click()
            assert win.recorder_busy()
            deadline = time.monotonic() + 60
            while win.recorder_busy() and time.monotonic() < deadline:
                _pump(app, 0.1)
            _pump(app, 0.2)                                   # the answer arrives on the window's own thread
            assert not win.recorder_busy() and problems == []
            assert win.statusBar().currentMessage() == base.STARTED
            wait_for(lambda: _overall(tmp_path) == "RECORDING", 60, what="RECORDING after Start recording")
            win.refresh()
            assert win.view.headline == "Recording normally" and page.start_box.isHidden()
            assert win.ctx.read.kind is ReadKind.OK and page.facts["camera"].text() == "connected"
        finally:
            if win is not None:
                win.close()
                win.deleteLater()
                app.processEvents()
            last = _sentinel("stop", "--config", str(cfg_path), "--timeout", "60")
    assert last.returncode == 0, last.stdout + last.stderr     # nothing is left running behind the test
    assert _overall(tmp_path) == "STOPPED"
