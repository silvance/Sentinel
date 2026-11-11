"""Archive drive end to end with the real recorder (RecorderService + SimulatedCamera + FFmpeg):
archiving never disturbs recording, swap while recording, the separate-process CLI.

Drives are temporary folders on the development host's internal disk (no USB platter drive).
Docs: docs/04 §2-3. Requirement: S1 (also E2, E3, E7).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from archive_helpers import FakeEjector, SwitchLocator, check_drive, make_drive
from conftest import requires_ffmpeg
from harness import Harness, contiguous, wait_for
from tools.archive_bench import add_synthetic_run
from sentinel.archive.status import read_archive_status
from sentinel.archive.worker import ArchiveWorker
from sentinel.db import Ledger
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]
ROOT = Path(__file__).resolve().parents[2]
MB = 1_000_000


class LagSampler:
    """Samples the recorder's own measurements every 50 ms: stream lag behind real time and the
    time since FFmpeg's output last advanced (a stall shows up as a growing value)."""

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.samples: list[tuple[float, float, float]] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self) -> LagSampler:
        self._t.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            sup = self.h.svc.supervisor
            if sup is not None:
                with sup.live.lock:
                    lv = sup.live
                    if lv.ffmpeg_running and lv.media_seen and lv.last_advance_mono is not None:
                        now = time.monotonic()
                        self.samples.append((now, lv.lag_s, now - lv.last_advance_mono))
            time.sleep(0.05)

    def stop(self) -> None:
        self._stop.set()
        self._t.join(5)

    def window(self, t0: float, t1: float) -> dict[str, float]:
        s = [x for x in self.samples if t0 <= x[0] <= t1]
        if not s:
            return {"samples": 0}
        lags = [x[1] for x in s]
        return {"samples": len(s), "lag_min_s": round(min(lags), 3), "lag_max_s": round(max(lags), 3),
                "lag_spread_s": round(max(lags) - min(lags), 3),
                "max_s_since_progress": round(max(x[2] for x in s), 3)}


def _gaps_while_recording(h: Harness) -> list[dict[str, Any]]:
    """Every gap except the one opened by the final, deliberate stop (still open: no next run)."""
    return [g for g in h.gaps() if g["end_utc"] is not None or g["kind"] != "between_runs"]


def _seal_latency(h: Harness, run_id: str) -> list[float]:
    from sentinel.clock import parse_iso_utc
    out = []
    for s in h.q("SELECT * FROM segments WHERE run_id=? AND sealed_utc IS NOT NULL AND observed_closed_utc IS NOT NULL",
                 (run_id,)):
        out.append(parse_iso_utc(s["sealed_utc"]) - parse_iso_utc(s["observed_closed_utc"]))
    return out


@pytest.mark.slow
def test_recording_unaffected_while_archive_drains_large_backlog(tmp_path: Path) -> None:
    """A 1.2 GB backlog is archived at the default 40 MB/s throttle, then 4 GB unthrottled, while the
    simulated camera records; the recorder must show no gap, no extra run and no stall."""
    cam = SimulatedCamera(fps=30, size="1280x720", bitrate="4M").start()
    h = Harness(tmp_path, cam.url, archive={"enabled": True, "drive_path": str(tmp_path / "unused"),
                                            "require_drive_marker": False, "poll_interval_s": 0.2})
    pre = Ledger(h.cfg.data_path / "sentinel.db")
    synth = add_synthetic_run(pre, h.cfg, 10, 120 * MB, seconds_each=300.0)
    pre.close()
    drive = make_drive(tmp_path, "A")
    sampler = None
    w: ArchiveWorker | None = None
    try:
        h.start()
        h.wait_sealed(len(synth) + 3, timeout=60)
        sampler = LagSampler(h).start()
        time.sleep(8)                                        # baseline: recording only
        t_base0, t_base1 = sampler.samples[0][0], time.monotonic()
        w = ArchiveWorker(h.cfg, locator=SwitchLocator(drive), remover=FakeEjector(), allow_same_volume=True)
        w.start()
        t_arch0 = time.monotonic()
        led = Ledger(h.cfg.data_path / "sentinel.db", readonly=True)
        try:
            def synth_done() -> bool:
                return led.query(f"SELECT COUNT(*) n FROM archive_copies WHERE segment_id IN "
                                 f"({','.join('?' for _ in synth)})", synth)[0]["n"] == len(synth)
            wait_for(synth_done, 240, interval=0.5, what="synthetic backlog archived")
            t_arch1 = time.monotonic()
            throttled = dict(w.stats.__dict__)
            # second phase: worst case, no throttle
            w.read_throttle.rate = w.verify_throttle.rate = 1e12
            extra = Ledger(h.cfg.data_path / "sentinel.db")
            more = add_synthetic_run(extra, h.cfg, 16, 250 * MB)
            extra.close()
            t_un0 = time.monotonic()
            wait_for(lambda: led.query(f"SELECT COUNT(*) n FROM archive_copies WHERE segment_id IN "
                                       f"({','.join('?' for _ in more)})", more)[0]["n"] == len(more),
                     240, interval=0.2, what="unthrottled backlog archived")
            t_un1 = time.monotonic()
        finally:
            led.close()
        time.sleep(3)
    finally:
        if sampler is not None:
            sampler.stop()
        h.stop()
        cam.stop()
    assert w is not None
    # drain the real segments sealed at shutdown, then swap so the index is complete
    wait_for(lambda: w.state == "UP_TO_DATE", 60, what="archive up to date")
    w.request_swap()
    wait_for(lambda: w.state == "SAFE_TO_REMOVE", 60, what="safe to remove")
    w.stop(30)

    runs = h.runs()
    real = [r for r in runs if r["app_version"] != "synthetic"]
    assert len(real) == 1, "recording restarted while archiving"
    assert _gaps_while_recording(h) == [], f"gap recorded while archiving: {h.gaps()}"
    segs = h.q("SELECT * FROM segments WHERE run_id=? ORDER BY seq", (real[0]["run_id"],))
    assert len(segs) >= 10
    for a, b in zip(segs, segs[1:]):
        assert contiguous(a, b), f"discontinuity between {a['rel_path']} and {b['rel_path']}"
    assert all(s["state"] == "FINALIZED" for s in segs[:-1])
    base = sampler.window(t_base0, t_base1)
    thr = sampler.window(t_arch0, t_arch1)
    unthr = sampler.window(t_un0, t_un1)
    lat = _seal_latency(h, real[0]["run_id"])
    metrics = {"baseline": base, "archiving_throttled_40MBps": thr, "archiving_unthrottled": unthr,
               "throttled_copy": {k: throttled[k] for k in ("files", "bytes", "copy_s", "verify_s")},
               "throttled_window_s": round(t_arch1 - t_arch0, 1), "unthrottled_window_s": round(t_un1 - t_un0, 1),
               "seal_latency_s_max": round(max(lat), 3), "real_segments": len(segs)}
    print("ARCHIVE_WHILE_RECORDING_METRICS " + json.dumps(metrics))
    # no stall: FFmpeg output kept advancing (progress is reported ~2x per second)
    for wnd in (thr, unthr):
        assert wnd["samples"] > 20
        assert wnd["max_s_since_progress"] < 2.5, metrics
        assert wnd["lag_max_s"] - base["lag_min_s"] < 1.5, metrics      # far below the 3 s gap tolerance
    led = Ledger(h.cfg.data_path / "sentinel.db", readonly=True)
    try:
        did = led.query("SELECT drive_id FROM drives")[0]["drive_id"]
        sealed = led.query("SELECT COUNT(*) n FROM segments WHERE state IN ('FINALIZED','INTERRUPTED','CORRUPT')")[0]["n"]
        assert led.query("SELECT COUNT(*) n FROM archive_copies WHERE kind IN ('original','quarantine')")[0]["n"] == sealed
        check_drive(drive.root, led, did, complete=True)
    finally:
        led.close()


def test_swap_during_active_recording_then_next_drive(tmp_path: Path) -> None:
    cam = SimulatedCamera(fps=15).start()
    h = Harness(tmp_path, cam.url, archive={"enabled": True, "drive_path": str(tmp_path / "unused"),
                                            "require_drive_marker": False, "poll_interval_s": 0.2,
                                            # integration: default is now "any_drive"; this test checks this_drive
                                            "resume_policy": "this_drive"})
    a, b = make_drive(tmp_path, "A"), make_drive(tmp_path, "B")
    loc = SwitchLocator(a)
    w = ArchiveWorker(h.cfg, locator=loc, remover=FakeEjector(), allow_same_volume=True)
    try:
        h.start()
        w.start()
        h.wait_sealed(3, timeout=60)
        led = Ledger(h.cfg.data_path / "sentinel.db", readonly=True)
        wait_for(lambda: led.query("SELECT COUNT(*) n FROM archive_copies")[0]["n"] >= 2, 30, what="copies on A")
        w.request_swap()
        wait_for(lambda: w.state == "SAFE_TO_REMOVE", 30, what="SAFE TO REMOVE")
        da = led.query("SELECT drive_id FROM drives")[0]["drive_id"]
        on_a = led.query("SELECT COUNT(*) n FROM archive_copies WHERE drive_id=?", (da,))[0]["n"]
        check_drive(a.root, led, da, complete=True)
        # the recorder's own status shows the archive state (one status for the operator)
        wait_for(lambda: (h.status().get("archive") or {}).get("state") == "SAFE_TO_REMOVE", 15,
                 what="archive state in recorder status")
        sealed_at_swap = h.sealed_count()
        h.wait_sealed(sealed_at_swap + 2, timeout=30)        # recording continues while the drive waits
        time.sleep(1.0)
        assert led.query("SELECT COUNT(*) n FROM archive_copies WHERE drive_id=?", (da,))[0]["n"] == on_a
        loc.target = None                                    # operator unplugs A
        wait_for(lambda: any(x["code"] == "A-001" for x in h.status().get("alerts", [])), 15,
                 what="A-001 in recorder status")
        msg = (h.status().get("archive") or {}).get("message", "")
        assert "Insert the next archive drive" in msg and "Not yet archived:" in msg
        loc.target = b                                       # inserts B
        wait_for(lambda: w.state == "UP_TO_DATE", 60, what="B up to date")
        h.wait_sealed(h.sealed_count() + 1, timeout=30)
    finally:
        h.stop()
        cam.stop()
    wait_for(lambda: w.state == "UP_TO_DATE", 60, what="archive caught up after stop")
    w.request_swap()
    wait_for(lambda: w.state == "SAFE_TO_REMOVE", 30, what="B safe to remove")
    w.stop(30)
    led2 = Ledger(h.cfg.data_path / "sentinel.db", readonly=True)
    try:
        ids = [r["drive_id"] for r in led2.query("SELECT drive_id FROM drives ORDER BY rowid")]
        db = ids[1]
        sealed = [r["segment_id"] for r in led2.query(
            "SELECT segment_id FROM segments WHERE state IN ('FINALIZED','INTERRUPTED','CORRUPT') ORDER BY rowid")]
        on_b = [r["segment_id"] for r in led2.query(
            "SELECT segment_id FROM archive_copies WHERE drive_id=? AND kind IN ('original','quarantine') "
            "ORDER BY copy_id", (db,))]
        assert on_b == sealed               # this_drive policy: B starts again from the oldest
        check_drive(b.root, led2, db, complete=True)
        check_drive(a.root, led2, ids[0], complete=True)
        # Each drive carries the recording log's fingerprint from its index writes (attach, swap...), and
        # the log on the laptop still matches every one of them (tamper evidence, 2026-11-10).
        from sentinel.ledgerchain import anchors_from, verify_ledger

        for drive_root in (a.root, b.root):
            found, _notes = anchors_from(Path(drive_root))
            assert found, f"no recording-log fingerprint on {drive_root}"
            rep = verify_ledger(h.cfg.data_path / "sentinel.db", anchors=found, use_local_anchors=False)
            assert rep.ok, rep.render_text()
    finally:
        led2.close()
        led.close()
    assert len(h.runs()) == 1 and _gaps_while_recording(h) == []


def _write_config(tmp: Path, drive: Path) -> Path:
    cfg = tmp / "sentinel.toml"
    cfg.write_text(f"""
[camera]
id = "CAM01"
url = "tcp://127.0.0.1:9"
input_format = "mpegts"
development_source = true

[storage]
spool_dir = "spool"
data_dir = "data"
log_dir = "logs"
warning_free_gb = 0
critical_free_gb = 0

[archive]
enabled = true
drive_path = "{drive.as_posix()}"
require_drive_marker = true
scan_volumes = false               # tests never look at real volumes
poll_interval_s = 0.2
""", encoding="utf-8")
    return cfg


def test_archive_as_separate_process_with_swap_cli(tmp_path: Path) -> None:
    drive = make_drive(tmp_path, "P", marker=True)
    cfg_path = _write_config(tmp_path, drive.root)
    from sentinel.config import load_config
    cfg = load_config(cfg_path)
    led = Ledger(cfg.data_path / "sentinel.db")
    ids = add_synthetic_run(led, cfg, 3, 2 * MB, sidecars=True)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    kw: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}
    p = subprocess.Popen([sys.executable, "-m", "sentinel.archive", "run", "--config", str(cfg_path),
                          "--allow-same-volume"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    status = cfg.data_path / "archive_status.json"
    try:
        wait_for(lambda: (read_archive_status(status) or {}).get("state") == "UP_TO_DATE", 60, what="process up to date")
        doc = read_archive_status(status) or {}
        assert doc["backlog"]["text"] == "Not yet archived: 0 h"
        assert doc["pid"] != os.getpid()          # another process (venv launcher pid may differ from Popen's)
        assert doc["priority"].startswith("windows-process-background-mode") or sys.platform != "win32"
        r = subprocess.run([sys.executable, "-m", "sentinel.archive", "swap", "--config", str(cfg_path),
                            "--timeout", "60"], env=env, capture_output=True, text=True, timeout=90)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Safely Remove Hardware" in r.stdout or "Eject" in r.stdout
        assert (read_archive_status(status) or {}).get("state") == "REMOVE_MANUALLY"
        r = subprocess.run([sys.executable, "-m", "sentinel.archive", "status", "--config", str(cfg_path)],
                           env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "REMOVE_MANUALLY" in r.stdout
    finally:
        if sys.platform == "win32":
            p.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            p.send_signal(signal.SIGTERM)
        p.wait(60)
    assert (read_archive_status(status) or {}).get("state") == "STOPPED"
    did = led.query("SELECT drive_id FROM drives")[0]["drive_id"]
    rows = led.query("SELECT segment_id, kind FROM archive_copies WHERE drive_id=?", (did,))
    assert sorted((r["segment_id"], r["kind"]) for r in rows) == sorted(
        [(i, "original") for i in ids] + [(i, "framehash") for i in ids])
    check_drive(drive.root, led, did, complete=True)
    led.close()
