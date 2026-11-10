"""The recorder as a real OS process: hard kill + restart recovery, graceful stop by signal,
single-instance lock, CLI status banner, diagnostics, and credential redaction end to end."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import wait_for
from sentinel.db import Ledger
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]
ROOT = Path(__file__).resolve().parents[2]


def _write_config(tmp: Path, url: str, extra_camera: str = "") -> Path:
    cfg = tmp / "sentinel.toml"
    cfg.write_text(f"""
[camera]
id = "CAM01"
url = "{url}"
input_format = "mpegts"
development_source = true
{extra_camera}

[recording]
segment_seconds = 2
align_to_clock = false
connect_timeout_s = 10
stall_timeout_s = 6
io_timeout_s = 4
backoff_initial_s = 0.5
backoff_max_s = 2

[storage]
spool_dir = "spool"
data_dir = "data"
log_dir = "logs"
warning_free_gb = 0
critical_free_gb = 0

[service]
status_interval_s = 0.5
poll_interval_s = 0.2
""", encoding="utf-8")
    return cfg


def _start(cfg: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    kw: dict = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen([sys.executable, "-m", "sentinel.service", "run", "--config", str(cfg)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def _cli(cfg: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.run([sys.executable, "-m", "sentinel.service", *args, "--config", str(cfg)],
                          env=env, capture_output=True, text=True, timeout=60)


def _graceful_stop(p: subprocess.Popen[bytes]) -> int:
    if sys.platform == "win32":
        p.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        p.send_signal(signal.SIGTERM)
    return p.wait(90)


def _q(tmp: Path, sql: str, params: tuple = ()) -> list[dict]:
    db = tmp / "data" / "sentinel.db"
    if not db.exists():
        return []
    led = Ledger(db, readonly=True)
    try:
        return [dict(r) for r in led.query(sql, params)]
    finally:
        led.close()


def _sealed(tmp: Path) -> int:
    return len(_q(tmp, "SELECT 1 FROM segments WHERE state IN ('FINALIZED','INTERRUPTED','CORRUPT')"))


def test_hard_kill_restart_recovery_and_graceful_stop(tmp_path: Path) -> None:
    with SimulatedCamera(fps=15) as cam:
        cfg = _write_config(tmp_path, cam.url)
        p1 = _start(cfg)
        try:
            wait_for(lambda: _sealed(tmp_path) >= 2, 45, what="segments from first process")
            # A second instance must refuse to run (exit code 3) rather than double-record.
            second = _start(cfg)
            assert second.wait(30) == 3
            time.sleep(1.0)  # be mid-segment
        finally:
            p1.kill()        # power-loss-like: no shutdown code runs
            p1.wait(30)
        crashed_run = _q(tmp_path, "SELECT run_id FROM recording_runs ORDER BY rowid DESC LIMIT 1")[0]["run_id"]
        active = _q(tmp_path, "SELECT rel_path FROM segments WHERE state='ACTIVE'")
        # FFmpeg must not outlive the service (Windows job object / Linux PDEATHSIG + systemd).
        if active:
            f = tmp_path / "spool" / active[-1]["rel_path"]
            s1 = f.stat().st_size
            time.sleep(3)
            assert f.stat().st_size == s1, "FFmpeg kept writing after the service was killed"

        p2 = _start(cfg)
        try:
            wait_for(lambda: _q(tmp_path, "SELECT 1 FROM recording_runs WHERE run_id=? AND end_reason="
                                          "'service_interrupted'", (crashed_run,)), 30, what="crashed run closed")
            wait_for(lambda: _q(tmp_path, "SELECT 1 FROM recording_gaps WHERE reason='service_interrupted' "
                                          "AND end_utc IS NOT NULL"), 45, what="gap closed by new footage")
            assert _q(tmp_path, "SELECT 1 FROM events WHERE code='startup_recovery'")
            out = _cli(cfg, "status")
            assert "#" * 20 in out.stdout and "RECORDING NORMALLY" not in out.stdout  # Z-001 always shown
            assert "Error Z-001" in out.stdout
        finally:
            rc = _graceful_stop(p2)
        assert rc == 0
    crashed = _q(tmp_path, "SELECT state FROM segments WHERE run_id=?", (crashed_run,))
    assert crashed and all(s["state"] in ("FINALIZED", "INTERRUPTED") for s in crashed), crashed
    assert not _q(tmp_path, "SELECT 1 FROM segments WHERE state IN ('ACTIVE','CLOSED')")
    last = _q(tmp_path, "SELECT state FROM segments ORDER BY rowid DESC LIMIT 1")[0]
    assert last["state"] == "FINALIZED", "graceful stop must close the final segment cleanly"
    import json
    assert json.loads((tmp_path / "data" / "status.json").read_text())["overall"] == "STOPPED"


def test_credentials_never_leak(tmp_path: Path) -> None:
    secret_user, secret_pw = "viewerX", "S3cr3t!pw@x"
    cred = tmp_path / "camera.secret"
    cred.write_text(f'username = "{secret_user}"\npassword = "{secret_pw}"\n', encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(cred, 0o600)
    # Nothing listens on this port: FFmpeg fails and prints its (credentialed) URL in the error.
    cfg = _write_config(tmp_path, "tcp://127.0.0.1:9", f'credentials_file = "{cred.as_posix()}"')
    p = _start(cfg)
    try:
        wait_for(lambda: len(_q(tmp_path, "SELECT 1 FROM recording_runs WHERE ended_utc IS NOT NULL")) >= 2, 60,
                 what="failed connection attempts")
    finally:
        _graceful_stop(p)
    rep = _cli(cfg, "diagnostics", "--out", str(tmp_path / "report"))
    assert rep.returncode == 0
    zips = list((tmp_path / "report").glob("*.zip"))
    blobs = [f.read_bytes() for f in (tmp_path / "logs").glob("*")]
    blobs.append((tmp_path / "data" / "status.json").read_bytes())
    for row in _q(tmp_path, "SELECT command_redacted, end_detail FROM recording_runs"):
        blobs.append(str(row).encode())
    for row in _q(tmp_path, "SELECT message, detail FROM events"):
        blobs.append(str(row).encode())
    with zipfile.ZipFile(zips[0]) as zf:
        blobs.extend(zf.read(n) for n in zf.namelist())
    assert any(b"***:***@" in b for b in blobs), "expected redacted URLs to appear somewhere"
    from urllib.parse import quote
    for needle in (secret_pw, quote(secret_pw, safe=""), secret_user, "S3cr3t"):
        assert not any(needle.encode() in b for b in blobs), f"credential fragment {needle!r} leaked"
