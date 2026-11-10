"""The GUI next to a real recording (RecorderService + FFmpeg + SimulatedCamera).

Requirement U6: closing or misusing the GUI cannot interrupt collection. Checked two ways:
* in-process: open the real MainWindow on the running recorder's data, refresh, close it;
* separate process (as deployed): start ``python -m sentinel.gui``, then close it hard (kill).
In both cases segments keep being sealed, the status heartbeat stays fresh, and the run never ends.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from conftest import base_config_dict, requires_ffmpeg  # noqa: E402
from gui_support import write_toml  # noqa: E402
from harness import Harness, wait_for  # noqa: E402
from sentinel.gui.window import prepare_qt_environment  # noqa: E402
from sentinel.sim.camera import SimulatedCamera  # noqa: E402

prepare_qt_environment()

from PySide6.QtWidgets import QApplication  # noqa: E402

from sentinel.gui.theme import LIGHT  # noqa: E402
from sentinel.gui.window import MainWindow  # noqa: E402

pytestmark = [pytest.mark.integration, requires_ffmpeg]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def cam():
    c = SimulatedCamera(fps=15).start()
    yield c
    c.stop()


def _recording_continues(h: Harness, after_s: float = 6.0) -> None:
    n0 = h.sealed_count()
    runs0 = len(h.runs())
    time.sleep(after_s)
    wait_for(lambda: h.sealed_count() >= n0 + 2, 20, what="segments still being sealed after the GUI closed")
    # Harness.status() returns {} on a transient Windows sharing violation while the file is replaced.
    st = wait_for(lambda: h.status() or None, 5, interval=0.05, what="a readable status document")
    assert st.get("overall") == "RECORDING", st.get("overall")
    assert time.time() - float(st["heartbeat_epoch"]) < 5.0
    assert len(h.runs()) == runs0, "the recording run was restarted"
    assert not h.svc.stop_event.is_set()
    assert all(r["ended_utc"] is None for r in h.runs()[-1:])


def test_closing_gui_window_leaves_recording_running(tmp_path: Path, cam: SimulatedCamera) -> None:
    app = QApplication.instance() or QApplication([])
    h = Harness(tmp_path, cam.url).start()
    try:
        h.wait_sealed(2, timeout=40)
        wait_for(lambda: h.status().get("overall") == "RECORDING", 15, what="RECORDING")
        win = MainWindow(h.cfg, LIGHT, export_available=False)       # auto refresh on (2 s timer)
        win.resize(1366, 768)
        win.show()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.05)
        v = win.view
        assert v is not None and not v.stale and v.headline == "Recording normally"
        assert v.test_camera is not None and v.test_camera.code == "Z-001"   # simulated camera is flagged
        assert v.camera.value == "Connected"
        win.show_timeline()
        win.show_exports()
        win.close()
        win.deleteLater()
        app.processEvents()
        _recording_continues(h)
    finally:
        h.stop()
    assert h.exit_code == 0


def test_killing_gui_process_leaves_recording_running(tmp_path: Path, cam: SimulatedCamera) -> None:
    d = base_config_dict(tmp_path, cam.url)
    toml = write_toml(tmp_path / "sentinel.toml", d)
    h = Harness(tmp_path, cam.url).start()
    proc = None
    try:
        h.wait_sealed(2, timeout=40)
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                   PYTHONPATH=os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
        proc = subprocess.Popen([sys.executable, "-m", "sentinel.gui", "--config", str(toml), "--windowed"],
                                env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(5.0)
        assert proc.poll() is None, proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        proc.kill()
        proc.wait(10)
        _recording_continues(h)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        h.stop()
    assert h.exit_code == 0
