"""The launcher end to end: start the recorder as a detached background process, record from
the simulated camera, refuse a second instance, stop it gracefully through the stop channel.

By default this runs the launcher from source (``python -m sentinel.launcher``). To test a
*built* program instead, point ``SENTINEL_FROZEN_EXE`` at it; the launcher is then started with
a PATH from which every FFmpeg/FFprobe directory has been removed, so recording only works if
the bundled copy next to the executable is used::

    $env:SENTINEL_FROZEN_EXE = "dist\\Sentinel\\Sentinel.exe"
    $env:SENTINEL_LAUNCHER_RUN_S = "30"
    python -m pytest tests/integration/test_launcher_process.py -q -p no:cacheprovider
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import wait_for
from sentinel.db import Ledger
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]
ROOT = Path(__file__).resolve().parents[2]
FROZEN = os.environ.get("SENTINEL_FROZEN_EXE")
RUN_S = float(os.environ.get("SENTINEL_LAUNCHER_RUN_S", "8"))
TOOLS = ("ffmpeg.exe", "ffprobe.exe") if sys.platform == "win32" else ("ffmpeg", "ffprobe")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("SENTINEL_CONFIG", None)
    if FROZEN:
        keep = [d for d in env.get("PATH", "").split(os.pathsep)
                if d and not any((Path(d) / t).exists() for t in TOOLS)]
        env["PATH"] = os.pathsep.join(keep)
    else:
        env["PYTHONPATH"] = str(ROOT / "src")
    return env


def _launcher(*args: str) -> list[str]:
    return [FROZEN, *args] if FROZEN else [sys.executable, "-m", "sentinel.launcher", *args]


def _run(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(_launcher(*args), env=_env(), capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def _write_config(tmp: Path, url: str) -> Path:
    cfg = tmp / "sentinel.toml"
    # No [tools]: a built program must find its bundled FFmpeg; a source run uses PATH.
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
display_timezone = "America/New_York"

[storage]
spool_dir = "spool"
data_dir = "data"
log_dir = "logs"
warning_free_gb = 0
critical_free_gb = 0

[service]
status_interval_s = 0.5
""", encoding="utf-8")
    return cfg


def _q(tmp: Path, sql: str) -> list[dict]:
    db = tmp / "data" / "sentinel.db"
    if not db.exists():
        return []
    led = Ledger(db, readonly=True)
    try:
        return [dict(r) for r in led.query(sql)]
    finally:
        led.close()


def _background_pid(tmp: Path) -> int | None:
    try:
        text = (tmp / "logs" / "launcher.log").read_text("utf-8")
    except OSError:
        return None
    found = re.findall(r"recorder started in the background \(pid (\d+)\)", text)
    return int(found[-1]) if found else None


def test_launcher_starts_records_and_stops_gracefully(tmp_path: Path) -> None:
    with SimulatedCamera(fps=15) as cam:
        cfg = _write_config(tmp_path, cam.url)
        t0 = time.monotonic()
        first = _run("launch", "--once", "--config", str(cfg))
        launch_s = time.monotonic() - t0
        pid = _background_pid(tmp_path)
        try:
            assert pid is not None, first.stdout + first.stderr
            assert "C-00" not in first.stdout, first.stdout
            wait_for(lambda: len(_q(tmp_path, "SELECT 1 FROM segments WHERE state='FINALIZED'")) >= 2, 45,
                     what="sealed segments from the background recorder")
            # Opening Sentinel again must not start a second recorder.
            again = _run("launch", "--once", "--config", str(cfg))
            assert _background_pid(tmp_path) == pid, again.stdout
            assert "Error Z-001" in again.stdout             # simulated camera is always flagged
            time.sleep(max(0.0, RUN_S - (time.monotonic() - t0)))
            stop = _run("stop", "--config", str(cfg))
            assert stop.returncode == 0, stop.stdout + stop.stderr
            assert "has stopped" in stop.stdout
        finally:
            if pid is not None and _run("stop", "--config", str(cfg), "--timeout", "30").returncode != 0:
                os.kill(pid, signal.SIGTERM)  # last resort so a failed test leaves nothing running
    assert _run("stop", "--config", str(cfg)).stdout.strip() == "The recorder is not running."
    status = json.loads((tmp_path / "data" / "status.json").read_text("utf-8"))
    assert status["overall"] == "STOPPED"
    assert not _q(tmp_path, "SELECT 1 FROM segments WHERE state IN ('ACTIVE','CLOSED')")
    last = _q(tmp_path, "SELECT state FROM segments ORDER BY rowid DESC LIMIT 1")[0]
    assert last["state"] == "FINALIZED", "graceful stop must close the final segment cleanly"
    sealed = _q(tmp_path, "SELECT COUNT(*) AS n FROM segments WHERE state='FINALIZED'")[0]["n"]
    commands = [r["command_redacted"] for r in _q(tmp_path, "SELECT command_redacted FROM recording_runs")]
    if FROZEN:
        bundled = str(Path(FROZEN).resolve().parent / "ffmpeg")
        assert commands and all(c.startswith(bundled) or c.startswith(f'"{bundled}') for c in commands), commands
    print(f"\nlauncher returned in {launch_s:.2f} s; {sealed} segments sealed; ffmpeg: {commands[0].split()[0]}")
