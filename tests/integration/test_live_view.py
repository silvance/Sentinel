"""Live view from the real recorder service (sentinel.preview): on request it publishes camera frames
while recording continues; with no viewer it stops and removes them."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import Harness, wait_for
from sentinel import preview
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]


def test_live_view_on_request_while_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preview, "REQUEST_TTL_S", 4.0)
    with SimulatedCamera(fps=15) as cam:
        h = Harness(tmp_path, cam.url).start()
        try:
            h.wait_sealed(1, timeout=40)
            assert preview.read_latest(h.cfg) is None                       # nobody asked: nothing runs
            deadline = time.time() + 30
            doc = None
            while time.time() < deadline:
                preview.request_preview(h.cfg)
                doc = preview.read_latest(h.cfg)
                if doc and doc.get("state") == "live" and doc.get("seq", 0) >= 4:
                    break
                time.sleep(0.5)
            assert doc and doc["state"] == "live" and doc["seq"] >= 4, doc
            jpeg = (preview.preview_dir(h.cfg) / doc["frame"]).read_bytes()
            assert jpeg[:2] == b"\xff\xd8" and len(jpeg) > 1000
            assert h.status().get("overall") == "RECORDING"                 # recording unaffected
            wait_for(lambda: preview.read_latest(h.cfg) is None, 20, what="live view stopped without a viewer")
        finally:
            h.stop()
    assert h.exit_code == 0
