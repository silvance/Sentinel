"""What a command prints reaches the Sentinel window line by line, while the command runs.

Field report 2026-11-18: the live motion test (Settings > Motion) showed nothing while it ran. The built program
collected its output in blocks when it was read through a pipe (it does not read PYTHONUNBUFFERED, which the
window sets), so every line arrived when the command ended, and none when it was stopped."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sentinel import logging_setup
from sentinel.launcher import cli

ROOT = Path(__file__).resolve().parents[2]


def test_printed_lines_are_written_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    piped = io.TextIOWrapper(raw, encoding="utf-8", line_buffering=False)      # how a pipe is opened
    monkeypatch.setattr(sys, "stdout", piped)
    print("held back")
    assert raw.getvalue() == b""
    logging_setup.line_buffered_output()
    print("   1s  changed   0.00 %")
    assert raw.getvalue().decode("utf-8").splitlines() == ["held back", "   1s  changed   0.00 %"]


def test_no_console_at_all_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)            # a program without a console
    monkeypatch.setattr(sys, "stderr", io.BytesIO())    # not a text stream
    logging_setup.line_buffered_output()


def test_both_programs_do_it_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.winsvc import __main__ as winsvc_main

    calls: list[str] = []
    monkeypatch.setattr(cli, "line_buffered_output", lambda: calls.append("Sentinel"))
    monkeypatch.setattr(logging_setup, "line_buffered_output", lambda: calls.append("SentinelService"))
    with pytest.raises(SystemExit):
        cli.main(["motion-test", "--help"])
    with pytest.raises(SystemExit):
        winsvc_main.main(["--help"])
    assert calls == ["Sentinel", "SentinelService"]


def test_motion_test_lines_arrive_while_it_runs(tmp_path: Path) -> None:
    """The command as the window starts it, read through a pipe, WITHOUT PYTHONUNBUFFERED: as in the built
    program. Its first line must be there well before it ends."""
    d = str(tmp_path).replace("\\", "\\\\")
    cfg = tmp_path / "sentinel.toml"
    cfg.write_text(f'[camera]\nid = "CAM01"\nname = "Camera 1"\nurl = "rtsp://127.0.0.1:9/stream0"\n'
                   f'transport = "tcp"\n\n[storage]\nspool_dir = "{d}\\\\spool"\ndata_dir = "{d}\\\\data"\n'
                   f'log_dir = "{d}\\\\logs"\n', "utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen([sys.executable, "-m", "sentinel.launcher", "motion-test", "--config", str(cfg),
                             "--seconds", "4"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    arrived: list[tuple[float, str]] = []

    def read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            arrived.append((time.monotonic(), line.decode("utf-8", "replace").strip()))

    t = threading.Thread(target=read, daemon=True)
    t.start()
    try:
        proc.wait(60)
    finally:
        if proc.poll() is None:
            proc.kill()
    ended = time.monotonic()
    t.join(10)
    assert arrived and arrived[0][1].startswith("Watching key frames of the main stream"), arrived[:2]
    assert arrived[-1][1].startswith("Done: motion seen 0 time(s)")
    assert ended - arrived[0][0] > 2.0, "the first line arrived only when the command ended"
    seconds = [a for a in arrived if a[1].endswith("connecting...") or "NO PICTURES" in a[1]]
    assert len(seconds) == 4 and seconds[-1][0] - seconds[0][0] > 2.0, seconds
