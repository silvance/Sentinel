"""H.265 (HEVC) cameras (owner request 2026-11-10): review copies are tagged hvc1; H.264 is unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.export.media import remux_argv
from sentinel.sim.camera import SimulatedCamera


def test_hevc_review_copies_are_tagged_hvc1() -> None:
    hevc = remux_argv("ffmpeg", Path("l.txt"), Path("o.mp4"), "hevc")
    assert hevc[hevc.index("-tag:v") + 1] == "hvc1"
    for codec in ("h264", None):
        assert "-tag:v" not in remux_argv("ffmpeg", Path("l.txt"), Path("o.mp4"), codec)


def test_test_camera_offers_h265() -> None:
    assert SimulatedCamera(codec="hevc").codec == "hevc"
    with pytest.raises(ValueError):
        SimulatedCamera(codec="mjpeg")
