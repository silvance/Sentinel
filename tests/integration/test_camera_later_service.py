"""The real recorder set up with "camera later": it runs, records nothing, starts no FFmpeg,
shows P-002 (never an outage alarm), and stops cleanly with a verifiable recording log."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import Harness, wait_for

pytestmark = [pytest.mark.integration, requires_ffmpeg]


def test_recorder_without_a_camera_waits_and_says_so(tmp_path: Path) -> None:
    h = Harness(tmp_path, "tcp://127.0.0.1:9", camera_pending=True).start()
    try:
        st = wait_for(lambda: (s := h.status()).get("overall") == "CAMERA_NOT_SET_UP" and s, 30,
                      what="camera-not-added status")
        assert [a["code"] for a in st["alerts"]] == ["P-002"]
        time.sleep(4.0)
        st = h.status()
        assert st["overall"] == "CAMERA_NOT_SET_UP" and [a["code"] for a in st["alerts"]] == ["P-002"]
        assert h.runs() == [] and h.sealed_count() == 0
    finally:
        h.stop()
    assert h.exit_code == 0
    assert len(h.events("camera_pending")) == 1
