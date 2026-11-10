"""Unattended-operation behaviour: alerts and escalation, self-healing, integrity audit,
and footage lost inside FFmpeg's timeline (independent-review finding: a 10-15 s freeze of an
MPEG-TS source used to vanish without a gap)."""

from __future__ import annotations

import json
import os
import stat
import time
import zipfile
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from harness import FakeDisk, Harness, wait_for
from sentinel.clock import parse_iso_utc
from sentinel.media.probe import ProbeError
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]


@pytest.fixture()
def cam():
    c = SimulatedCamera(fps=15).start()
    yield c
    c.stop()


def _alert(h: Harness, code: str) -> dict | None:
    return next((a for a in h.status().get("alerts", []) if a["code"] == code), None)


# ------------------------------------------------------------------ lost real time
def test_freeze_absorbed_by_ffmpeg_is_recorded_as_gap(tmp_path: Path, cam: SimulatedCamera) -> None:
    # 12 s freeze: shorter than the stall timeout (15 s), longer than FFmpeg's MPEG-TS
    # discontinuity threshold (10 s), so FFmpeg silently closes the hole in its timeline.
    h = Harness(tmp_path, cam.url, recording={"stall_timeout_s": 15, "io_timeout_s": 30,
                                              "segment_seconds": 4}).start()
    try:
        h.wait_sealed(2)
        cam.freeze()
        t_freeze = time.time()
        time.sleep(12)
        cam.resume()
        t_resume = time.time()
        wait_for(lambda: any(r["end_reason"] == "timeline_discontinuity" for r in h.runs()), 30,
                 what="timeline discontinuity detected")
        wait_for(lambda: any(g["reason"] == "source_discontinuity_absorbed" for g in h.gaps()), 30,
                 what="absorbed-discontinuity gap row")
        n = h.sealed_count()
        h.wait_sealed(n + 2, timeout=40)
        t_end = time.time()
    finally:
        h.stop()
    g = [x for x in h.gaps() if x["reason"] == "source_discontinuity_absorbed"][0]
    assert g["duration_s"] == pytest.approx(12.0, abs=2.0)
    assert parse_iso_utc(g["start_utc"]) == pytest.approx(t_freeze, abs=2.5)
    assert parse_iso_utc(g["end_utc"]) == pytest.approx(t_resume, abs=2.5)
    segs = h.segments()
    starts = [parse_iso_utc(s["start_utc"]) for s in segs]
    assert starts == sorted(starts)
    # Footage after the freeze carries true times: the last segment ends close to "now".
    last = max(segs, key=lambda s: s["end_utc"])
    assert parse_iso_utc(last["end_utc"]) == pytest.approx(t_end, abs=6.0)


# ------------------------------------------------------------------ alerts and escalation
def test_outage_alert_escalates_with_operator_wording_and_report(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url, recording={"connect_timeout_s": 8, "escalate_after_s": 6}).start()
    try:
        h.wait_sealed(2)
        cam.go_offline()
        a = wait_for(lambda: _alert(h, "R-003"), 25, what="R-003 notice")
        assert a["severity_name"] == "NOTICE"
        assert a["message"].startswith("Recording interrupted. Error R-003: Camera connection lost. "
                                       "Reconnecting automatically")
        a = wait_for(lambda: (x := _alert(h, "R-003")) and x["severity_name"] == "CRITICAL" and x, 30,
                     what="R-003 escalated")
        assert a["message"] == ("Recording stopped. Error R-003: Camera connection lost. Automatic recovery "
                                "unsuccessful. Contact technical support and provide diagnostic report.")
        st = h.status()
        assert st["banner"] is True and st["alert"]["code"] == "R-003" and st["alert"]["recording_stopped"]
        reports = wait_for(lambda: list((h.cfg.data_path / "diagnostics").glob("*.zip")), 15,
                           what="automatic diagnostic report")
        with zipfile.ZipFile(reports[0]) as zf:
            names = set(zf.namelist())
            assert {"summary.txt", "status.json", "events.csv", "MANIFEST.sha256"} <= names
            assert not any(n.endswith(".ts") for n in names), "reports must never contain video"
            assert "R-003" in zf.read("summary.txt").decode()
        cam.go_online()
        wait_for(lambda: h.status().get("overall") == "RECORDING", 40, what="recovery")
        assert _alert(h, "R-003") is None, "alert must clear once recording resumes"
    finally:
        h.stop()


def test_camera_never_reachable_is_r001(tmp_path: Path) -> None:
    h = Harness(tmp_path, "tcp://127.0.0.1:9", recording={"connect_timeout_s": 2, "escalate_after_s": 4}).start()
    try:
        a = wait_for(lambda: (x := _alert(h, "R-001")) and x["severity_name"] == "CRITICAL" and x, 30,
                     what="R-001 escalated")
        assert a["message"].startswith("Recording stopped. Error R-001: Cannot connect to camera.")
        assert h.status()["camera_link"] == "disconnected"
        time.sleep(3)
    finally:
        h.stop()
    # Days of retries must not litter the spool: failed attempts leave no empty run folders.
    run_dirs = [p for p in (h.cfg.spool_path / "CAM01").iterdir() if p.is_dir()]
    assert run_dirs == [] and len(h.runs()) >= 3
    gaps = h.gaps("between_runs")
    assert len(gaps) == 1 and gaps[0]["attempts"] >= 3
    # Retry events are throttled while one gap is open.
    assert len(h.q("SELECT 1 FROM events WHERE code LIKE 'run_end_%'")) <= 2


def test_storage_full_message(tmp_path: Path, cam: SimulatedCamera) -> None:
    disk = FakeDisk(free_gb=500)
    h = Harness(tmp_path, cam.url, disk=disk, storage={"warning_free_gb": 100, "critical_free_gb": 10}).start()
    try:
        h.wait_sealed(1)
        disk.free_gb = 5
        a = wait_for(lambda: _alert(h, "S-002"), 15, what="S-002")
        assert a["message"] == ("Recording stopped. Error S-002: Recording storage is full. "
                                "Contact technical support and provide diagnostic report.")
        assert h.status()["overall"] == "STORAGE_FULL"
    finally:
        h.stop()


# ------------------------------------------------------------------ self-healing
def test_transient_validation_failure_is_retried_by_janitor(tmp_path: Path, cam: SimulatedCamera,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    import sentinel.recorder.finalizer as fin_mod

    real = fin_mod.probe_segment
    calls = {"n": 0}

    def flaky(*a, **kw):  # noqa: ANN002, ANN003
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProbeError("ffprobe timed out after 300s")   # e.g. a momentarily overloaded disk
        return real(*a, **kw)

    monkeypatch.setattr(fin_mod, "probe_segment", flaky)
    h = Harness(tmp_path, cam.url, service={"janitor_interval_s": 1.0}).start()
    try:
        wait_for(lambda: h.q("SELECT 1 FROM segments WHERE finalize_attempts=1"), 30, what="first failure")
        wait_for(lambda: h.q("SELECT 1 FROM segments WHERE finalize_attempts=1 AND state='FINALIZED'"), 30,
                 what="segment sealed on retry")
    finally:
        h.stop()
    assert all(s["state"] == "FINALIZED" for s in h.segments())
    assert h.events("finalize_failed")


def test_internal_error_is_retried_without_stopping_service(tmp_path: Path, cam: SimulatedCamera,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.recorder.supervisor import RecordingSupervisor

    real = RecordingSupervisor._register_new_files
    calls = {"n": 0}

    def flaky(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        calls["n"] += 1
        if calls["n"] == 25:            # a few seconds into the first run, FFmpeg already recording
            raise OSError("simulated transient failure")
        return real(self, *a, **kw)

    monkeypatch.setattr(RecordingSupervisor, "_register_new_files", flaky)
    h = Harness(tmp_path, cam.url).start()
    try:
        wait_for(lambda: h.events("internal_error"), 30, what="internal error")
        n = h.sealed_count()
        h.wait_sealed(n + 2)
    finally:
        h.stop()
    first = h.runs()[0]
    assert first["end_reason"] == "internal_error" and first["ended_utc"]
    assert all(s["state"] in ("FINALIZED", "INTERRUPTED") for s in h.segments() if s["run_id"] == first["run_id"])
    assert [g for g in h.gaps("between_runs") if g["reason"] == "internal_error" and g["end_utc"]]
    assert h.exit_code == 0


# ------------------------------------------------------------------ integrity audit
def test_audit_detects_modified_original(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url).start()
    try:
        h.wait_sealed(2)
        seg = h.segments("FINALIZED")[0]
        p = h.path(seg)
        os.chmod(p, stat.S_IREAD | stat.S_IWRITE)               # someone defeats the seal...
        with open(p, "r+b") as fh:                              # ...and alters one byte
            fh.seek(1000)
            b = fh.read(1)
            fh.seek(1000)
            fh.write(bytes([b[0] ^ 0xFF]))
        failures = h.svc.integrity.audit_once()
        assert any(f["segment_id"] == seg["segment_id"] for f in failures)
        a = wait_for(lambda: (x := _alert(h, "V-001")) and x["severity_name"] == "CRITICAL" and x, 10,
                     what="V-001 critical")
        assert "could not be verified" in a["message"]
    finally:
        h.stop()
    row = h.q("SELECT audit_status FROM segments WHERE segment_id=?", (seg["segment_id"],))[0]
    assert row["audit_status"] in ("hash_mismatch", "not_read_only")
    assert h.events("audit_hash_mismatch") or h.events("audit_not_read_only")
