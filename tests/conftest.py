from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from sentinel.config import AppConfig, load_config_dict

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
# Run the recorder under test with a specific FFmpeg (e.g. the LGPL build to be shipped) while the
# simulated camera keeps using the FFmpeg on PATH (it needs libx264, which LGPL builds lack).
TEST_TOOLS_DIR = os.environ.get("SENTINEL_TEST_TOOLS_DIR")
# Tests never open the operator screen from the launcher (it would block on a real window); the
# GUI view is tested directly in tests/unit/test_gui_launch.py.
os.environ.setdefault("SENTINEL_TEXT_STATUS", "1")


def tools_for_tests() -> dict[str, str]:
    """[tools] section for test configurations ({} = default resolution)."""
    if not TEST_TOOLS_DIR:
        return {}
    from sentinel.paths import tool_filename
    return {t: str(Path(TEST_TOOLS_DIR) / tool_filename(t)) for t in ("ffmpeg", "ffprobe")}


def tools_toml_for_tests() -> str:
    tools = tools_for_tests()
    return "" if not tools else "[tools]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in tools.items())


def base_config_dict(tmp: Path, url: str = "tcp://127.0.0.1:9", **sections: dict[str, Any]) -> dict[str, Any]:
    d: dict[str, Any] = {
        "camera": {"id": "CAM01", "url": url, "input_format": "mpegts", "development_source": True},
        "recording": {"segment_seconds": 2, "align_to_clock": False, "connect_timeout_s": 10,
                      "stall_timeout_s": 6, "io_timeout_s": 4, "backoff_initial_s": 0.5, "backoff_max_s": 2,
                      "stop_grace_s": 5},
        "storage": {"spool_dir": str(tmp / "spool"), "data_dir": str(tmp / "data"), "log_dir": str(tmp / "logs"),
                    "warning_free_gb": 0.0, "critical_free_gb": 0.0},
        "service": {"status_interval_s": 0.5, "poll_interval_s": 0.2},
    }
    if tools_for_tests():
        d["tools"] = tools_for_tests()
    for name, values in sections.items():
        d.setdefault(name, {}).update(values)
    return copy.deepcopy(d)


def make_config(tmp: Path, url: str = "tcp://127.0.0.1:9", **sections: dict[str, Any]) -> AppConfig:
    return load_config_dict(base_config_dict(tmp, url, **sections), base_dir=tmp)


requires_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="FFmpeg/FFprobe not on PATH")
