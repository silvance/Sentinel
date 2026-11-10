"""Recording-log hash chain with the real recorder (simulated camera, real FFmpeg)."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import statistics
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from conftest import requires_ffmpeg
from harness import Harness, wait_for
from sentinel import ledgerchain as lc
from sentinel.diagnostics import build_report
from sentinel.recorder.finalizer import Finalizer
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]


@pytest.fixture()
def cam():
    c = SimulatedCamera(fps=15).start()
    yield c
    c.stop()


def _alert(h: Harness, code: str) -> dict | None:
    return next((a for a in h.status().get("alerts", []) if a["code"] == code), None)


def _toml(h: Harness) -> Path:
    p = h.tmp / "sentinel.toml"
    s = h.cfg.storage
    p.write_text("\n".join(["[camera]", 'id = "CAM01"', 'url = "tcp://127.0.0.1:9"', 'input_format = "mpegts"',
                            "development_source = true", "[storage]", f"spool_dir = '{s.spool_dir}'",
                            f"data_dir = '{s.data_dir}'", f"log_dir = '{s.log_dir}'", ""]), "utf-8")
    return p


def test_chain_grows_with_recordings_and_verifies(tmp_path: Path, cam: SimulatedCamera,
                                                  monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    # Measure the chain's share of the real per-segment sealing work (ffprobe, fsync, SHA-256, ledger).
    chain_s: list[float] = []
    seal_s: list[float] = []
    acc = {"t": 0.0}

    def timed(fn: Any, sink: list[float] | None) -> Any:
        def wrapper(*a: Any, **kw: Any) -> Any:
            t = time.perf_counter()
            if sink is seal_s:
                acc["t"] = 0.0
            try:
                return fn(*a, **kw)
            finally:
                dt = time.perf_counter() - t
                if sink is seal_s:
                    seal_s.append(dt)
                    chain_s.append(acc["t"])
                else:
                    acc["t"] += dt
        return wrapper

    for name in ("track", "record_change", "record_insert"):
        monkeypatch.setattr(lc, name, timed(getattr(lc, name), None))
    monkeypatch.setattr(Finalizer, "process_segment", timed(Finalizer.process_segment, seal_s))

    h = Harness(tmp_path, cam.url).start()
    try:
        h.wait_sealed(4)
        st = wait_for(lambda: (x := h.status()).get("integrity", {}).get("ledger_chain", {}).get("seq") and x, 10,
                      what="chain head in status.json")
        assert len(st["integrity"]["ledger_chain"]["hash"]) == 64
        rep = h.svc.integrity.verify_chain_once()            # live, while recording
        assert rep.ok, rep.render_text()
    finally:
        h.stop()
    db = h.cfg.data_path / "sentinel.db"
    entries = h.q("SELECT seq, kind, ref_table, ref_key, record FROM ledger_chain ORDER BY seq")
    assert entries[0]["kind"] == "genesis"
    sealed = {e["ref_key"]: json.loads(e["record"])["data"] for e in entries if e["kind"] == "segment_sealed"}
    for seg in h.segments():
        if seg["state"] in ("FINALIZED", "INTERRUPTED", "CORRUPT"):
            assert sealed[seg["segment_id"]]["sha256"] == seg["sha256"]
            assert sealed[seg["segment_id"]]["end_utc"] == seg["end_utc"]
    kinds = {e["kind"] for e in entries}
    assert {"run_started", "run_ended", "segment_sealed", "event"} <= kinds
    # Anchors: start and stop lines in the anchor log; status.json carries the final head.
    anchors, problems = lc.read_anchor_log(h.cfg.data_path / lc.ANCHOR_LOG_NAME)
    assert not problems and [a.source.split("(")[-1] for a in anchors][-1] == "service_stop)"
    final = lc.current_chain_head(db)
    assert anchors[-1].seq == final.seq and anchors[-1].hash == final.hash
    rep = lc.verify_ledger(db, spool_path=h.cfg.spool_path, check_files=True)
    assert rep.ok and rep.files_checked == len(sealed) and rep.anchors_checked >= 2, rep.render_text()
    assert lc.main(["verify", "--config", str(_toml(h)), "--check-files"]) == 0
    # Diagnostic report carries the head and a check result, and becomes an anchor itself.
    zpath = build_report(h.cfg, "test")
    with zipfile.ZipFile(zpath) as zf:
        doc = json.loads(zf.read("ledger_chain.json"))
        summary = zf.read("summary.txt").decode()
    assert doc["ok"] and doc["head_at_report"]["seq"] == final.seq
    assert f"entry {final.seq}, fingerprint {final.hash}" in summary and "Recording log check: intact" in summary
    assert lc.read_anchor_log(h.cfg.data_path / lc.ANCHOR_LOG_NAME)[0][-1].source.endswith("(diagnostic_report)")
    ratio = [c / s for c, s in zip(chain_s, seal_s) if s > 0]
    with capsys.disabled():
        print(f"\n[chain cost, real recorder] {len(seal_s)} segments sealed: process_segment median "
              f"{statistics.median(seal_s) * 1e3:.1f} ms, chain median {statistics.median(chain_s) * 1e3:.3f} ms "
              f"({100 * statistics.median(ratio):.2f}% of sealing work)")
    assert statistics.median(ratio) < 0.05


def test_edit_while_recording_raises_v003(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url, check_chain=False).start()
    db = h.cfg.data_path / "sentinel.db"
    try:
        h.wait_sealed(3)
        seg = h.segments("FINALIZED")[0]
        seal_seq = h.q("SELECT MIN(seq) s FROM ledger_chain WHERE ref_table='segments' AND ref_key=?",
                       (seg["segment_id"],))[0]["s"]
        c = sqlite3.connect(db, timeout=10)                  # someone with file access edits the ledger
        c.execute("UPDATE segments SET end_utc=? WHERE segment_id=?", ("2030-01-01T00:00:00.000000Z",
                                                                       seg["segment_id"]))
        c.commit()
        c.close()
        rep = h.svc.integrity.verify_chain_once()
        assert not rep.ok and rep.first.position >= seal_seq
        assert rep.first.code in ("record_altered", "discrepancy_recorded") and "end_utc" in rep.first.message
        a = wait_for(lambda: _alert(h, "V-003"), 10, what="V-003 alert")
        assert a["severity_name"] == "CRITICAL" and a["recording_stopped"] is False
        assert a["message"] == ("Error V-003: The recording log has been altered. "
                                "Contact technical support and provide diagnostic report.")
        assert h.status()["integrity"]["ledger_chain"]["last_check_ok"] is False
        n = h.sealed_count()
        h.wait_sealed(n + 1)                                 # recording continues
    finally:
        h.stop()
    assert h.events("ledger_chain_altered")
    assert not lc.verify_ledger(db).ok


def test_file_edit_is_found_by_check_files(tmp_path: Path, cam: SimulatedCamera) -> None:
    h = Harness(tmp_path, cam.url, recording={"content_hash": "none", "frame_hashes": False}).start()
    try:
        h.wait_sealed(2)
    finally:
        h.stop()
    seg = h.segments("FINALIZED")[0]
    p = h.path(seg)
    os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
    with open(p, "r+b") as fh:
        fh.seek(500)
        b = fh.read(1)
        fh.seek(500)
        fh.write(bytes([b[0] ^ 0x01]))
    db = h.cfg.data_path / "sentinel.db"
    assert lc.verify_ledger(db).ok                           # the log itself is untouched
    rep = lc.verify_ledger(db, spool_path=h.cfg.spool_path, check_files=True)
    assert rep.first.code == "file_altered" and seg["rel_path"] in rep.first.message
