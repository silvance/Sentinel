"""Recording schedule end to end: the real recorder pauses on schedule (closing the current file
normally), records the pause as a planned gap, shows P-001 with the resume time instead of an
outage alert, and resumes by itself; the recording-log chain still verifies (Harness.stop)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import Harness, wait_for
from sentinel.clock import parse_iso_utc
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_scheduled_pause_and_automatic_resume(tmp_path: Path) -> None:
    t0 = time.time()
    pause_at, resume_at = t0 + 14.0, t0 + 32.0
    schedule = {"enabled": True, "timezone": "UTC",
                "dates": [{"action": "pause", "start": _utc(pause_at), "end": _utc(resume_at)}]}
    with SimulatedCamera(fps=15) as cam:
        h = Harness(tmp_path, cam.url, schedule=schedule).start()
        try:
            h.wait_sealed(2, timeout=40)
            st = wait_for(lambda: (s := h.status()).get("overall") == "PAUSED" and s, 40, what="PAUSED status")
            assert [a["code"] for a in st["alerts"] if a["code"] != "Z-001"] == ["P-001"]   # no outage alert
            p001 = next(a for a in st["alerts"] if a["code"] == "P-001")
            resume_text = datetime.fromtimestamp(int(resume_at), timezone.utc).strftime("%a %d %b %Y %H:%M")
            assert f"Recording resumes automatically on {resume_text}" in p001["message"]
            assert st["schedule"]["paused"] is True and st["schedule"]["next_change_is"] == "record"
            assert st["camera_link"] == "disconnected"
            runs_before = len(h.runs())
            time.sleep(4.0)
            # Nothing recorded while paused: no new run, no file that starts after the pause. (Files
            # recorded just before the pause may still be sealed now: sealing runs in the background.)
            assert h.status()["overall"] == "PAUSED" and len(h.runs()) == runs_before
            assert not [s for s in h.segments() if s["start_utc"] and parse_iso_utc(s["start_utc"]) > pause_at + 2.0]
            wait_for(lambda: h.status().get("overall") == "RECORDING", 60, what="recording resumed")
            wait_for(lambda: len([s for s in h.segments() if s["state"] == "FINALIZED" and s["start_utc"]
                                  and parse_iso_utc(s["start_utc"]) >= resume_at - 1.0]) >= 2, 40,
                     what="segments after resume")
        finally:
            h.stop()

    runs = h.runs()
    paused_runs = [r for r in runs if r["end_reason"] == "scheduled_pause"]
    assert len(paused_runs) == 1
    assert parse_iso_utc(paused_runs[0]["ended_utc"]) == pytest.approx(pause_at, abs=2.0)
    gaps = [g for g in h.gaps() if g["reason"] == "scheduled_pause"]
    assert len(gaps) == 1 and gaps[0]["end_utc"] is not None
    g = gaps[0]
    assert parse_iso_utc(g["start_utc"]) == pytest.approx(pause_at, abs=3.0)
    assert resume_at - 1.0 <= parse_iso_utc(g["end_utc"]) <= resume_at + 10.0          # + reconnect time
    # The file open at the pause was closed normally, never left "interrupted".
    assert {s["state"] for s in h.segments() if s["run_id"] == paused_runs[0]["run_id"]} == {"FINALIZED"}
    ev_pause, ev_resume = h.events("schedule_pause"), h.events("schedule_resume")
    assert len(ev_pause) == 1 and len(ev_resume) == 1
    assert json.loads(ev_pause[0]["detail"])["decided_by"] == "dates"
    assert not [e for e in h.events() if e["code"].startswith("run_end_") and e["severity"] != "info"
                and e["code"] != "run_end_graceful_stop"]


def test_service_started_inside_a_pause_waits_and_records_nothing(tmp_path: Path) -> None:
    """E.g. Windows restarted overnight during a scheduled pause."""
    t0 = time.time()
    schedule = {"enabled": True, "timezone": "UTC",
                "dates": [{"action": "pause", "start": _utc(t0 - 3600), "end": _utc(t0 + 3600)}]}
    with SimulatedCamera(fps=15) as cam:
        h = Harness(tmp_path, cam.url, schedule=schedule).start()
        try:
            st = wait_for(lambda: (s := h.status()).get("overall") == "PAUSED" and s, 30, what="PAUSED status")
            time.sleep(5.0)
            assert h.runs() == [] and h.sealed_count() == 0
            assert [a["code"] for a in st["alerts"] if a["code"] != "Z-001"] == ["P-001"]
        finally:
            h.stop()
    assert len(h.events("schedule_pause")) == 1 and h.events("schedule_resume") == []
