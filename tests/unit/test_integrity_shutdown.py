"""Shutdown while the video-fingerprint worker is busy (found by the load test 2026-11-10: on a fully
loaded machine one fingerprint outlived the 60 s shutdown wait and then wrote to the closed
recording database, leaving an AttributeError traceback). Now: a result finished during shutdown
is discarded and redone at the next start, and a closed database is a clear LedgerClosed."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_config
from sentinel.db import Ledger, LedgerClosed
from sentinel.platform import get_platform
from sentinel.recorder import integrity as integrity_mod
from sentinel.recorder.integrity import IntegrityWorker


def test_closed_ledger_says_so(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.db")
    led.close()
    with pytest.raises(LedgerClosed):
        led.query("SELECT 1")
    with pytest.raises(LedgerClosed):
        with led.tx():
            pass


def test_fingerprint_finished_during_shutdown_is_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    led = Ledger(tmp_path / "s.db")
    iw = IntegrityWorker(cfg, led, get_platform(), "ffmpeg-test")
    seg = {"content_sha256": None, "sha256": "ab" * 32, "rel_path": "CAM01/r/seg_000000.ts", "pix_fmt": "yuv420p",
           "content_hash_attempts": 0}
    writes: list[dict] = []
    monkeypatch.setattr(led, "get_segment", lambda _sid: seg)
    monkeypatch.setattr(led, "update_segment", lambda _sid, **kw: writes.append(kw))

    def slow_hash(*_a, **_k):  # noqa: ANN202 - the service is stopped while FFmpeg decodes
        iw._stop.set()
        return SimpleNamespace(sha256="cd" * 32, method="m", frame_count=10)

    monkeypatch.setattr(integrity_mod, "compute_content_hash", slow_hash)
    assert iw.hash_segment("seg1") is False
    assert writes == []                                  # redone at the next start instead
    led.close()
