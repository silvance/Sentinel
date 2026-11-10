"""Shared fixtures for GUI tests and the screenshot renderer.

Status documents are produced by the recorder's own decision code (``sentinel.status.evaluate``
+ ``StatusDocument`` + ``apply_alerts``), so the GUI is exercised with exactly the alerts and
messages the recorder would write, not hand-typed imitations.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from sentinel.clock import iso_utc
from sentinel.db import Ledger
from sentinel.models import SegmentState, StorageLevel
from sentinel.status import Measurements, StatusDocument, apply_alerts, evaluate

GB = 1_000_000_000
SEG = 300.0


def base_measurements(**kw: Any) -> Measurements:
    m = Measurements(now_mono=5000.0, service_started_mono=0.0, ffmpeg_running=True, media_seen_this_run=True,
                     seconds_since_media_advance=0.4, seconds_since_last_seal=40.0, storage_level=StorageLevel.OK,
                     storage_error=None, component_errors=[], in_backoff=False, ever_recorded=True, attempts=1)
    return replace(m, **kw)


def status_doc(m: Measurements, now: float, *, free_gb: float = 412.0, total_gb: float = 2000.0,
               seconds_until_attention: float | None = 6.2 * 86400, heartbeat_age_s: float = 0.5,
               last_segment_end: float | None = None, open_gap: dict[str, Any] | None = None,
               camera_id: str = "CAM01") -> dict[str, Any]:
    d = evaluate(m, stall_timeout_s=15.0, segment_seconds=SEG, connect_timeout_s=20.0, escalate_after_s=180.0)
    level = str(m.storage_level)
    doc = StatusDocument(
        overall=str(d.overall), camera_link=str(d.link), messages=d.messages, camera_id=camera_id,
        generated_utc=iso_utc(now - heartbeat_age_s), heartbeat_epoch=now - heartbeat_age_s,
        service={"version": "test", "platform": "windows", "uptime_s": 5000.0,
                 "simulated_camera": m.simulated, "clock_synchronized": True},
        recording={"run_id": "run-1", "stream_time_s": 1234.5, "fps": 15.0,
                   "last_end_reason": m.last_end_reason, "attempts": m.attempts},
        storage={"level": level, "free_bytes": int(free_gb * GB), "total_bytes": int(total_gb * GB),
                 "rate_bytes_per_s": 1.1e6, "seconds_until_attention": seconds_until_attention},
        integrity={"verification_queue": 0, "content_hash_backlog": 1, "unsealed_segments": 0},
        last_segment=({"segment_id": "x", "start_utc": iso_utc(last_segment_end - SEG),
                       "end_utc": iso_utc(last_segment_end), "state": "FINALIZED"} if last_segment_end else None),
        open_gap=open_gap)
    apply_alerts(doc, d.alerts)
    return asdict(doc)


def write_doc(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2), "utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------- scenarios
def scenario(name: str, now: float) -> tuple[dict[str, Any], float | None]:
    """Return (status document, end of last saved segment) for a named owner screenshot scenario."""
    last_end = now - 65.0
    if name == "recording_test_camera":
        return status_doc(base_measurements(simulated=True), now, last_segment_end=last_end), last_end
    if name in ("r003_notice", "r003_critical", "r003_critical_test_camera"):
        outage = 52.0 if name == "r003_notice" else 7 * 60.0
        since = now - outage
        m = base_measurements(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                              seconds_since_last_seal=outage + 30, in_backoff=True, outage_seconds=outage,
                              outage_since_utc=iso_utc(since), last_end_reason="ffmpeg_error",
                              attempts=4 if name == "r003_notice" else 23,
                              simulated=name.endswith("_test_camera"))
        gap = {"since_utc": iso_utc(since), "reason": "ffmpeg_error", "attempts": m.attempts, "seconds": outage}
        return status_doc(m, now, last_segment_end=since, open_gap=gap), since
    if name == "s002_storage_full":
        since = now - 25 * 60.0
        m = base_measurements(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                              seconds_since_last_seal=1500.0, storage_level=StorageLevel.CRITICAL,
                              outage_seconds=1500.0, last_end_reason="storage_critical")
        return status_doc(m, now, free_gb=4.6, seconds_until_attention=0.0, last_segment_end=since,
                          open_gap={"since_utc": iso_utc(since), "reason": "storage_critical", "attempts": 1}), since
    if name == "x001_not_responding":
        return status_doc(base_measurements(), now, heartbeat_age_s=4 * 60 + 12.0,
                          last_segment_end=now - 5 * 60), now - 5 * 60
    raise KeyError(name)


SCENARIOS = ("recording_test_camera", "r003_notice", "r003_critical", "s002_storage_full", "x001_not_responding",
             "r003_critical_test_camera")


def is_test_camera(name: str) -> bool:
    return name.endswith("_test_camera")


def gui_config_dict(tmp: Path, test_camera: bool) -> dict[str, Any]:
    """Config for GUI tests/screenshots. Production-like scenarios use an RTSP camera address
    (documentation range, never contacted: the GUI does not talk to the camera); test-camera
    scenarios use the simulator settings, which is what makes the recorder raise Z-001."""
    from conftest import base_config_dict
    d = base_config_dict(tmp)
    d["camera"]["name"] = "Camera 1"
    d["recording"]["display_timezone"] = "America/New_York"
    d["storage"]["warning_free_gb"] = 50.0
    d["storage"]["critical_free_gb"] = 5.0
    if not test_camera:
        d["camera"] = {"id": "CAM01", "name": "Camera 1", "url": "rtsp://192.0.2.10:554/stream"}
    return d


# ---------------------------------------------------------------------------- ledger
def populate_ledger(db_path: Path, now: float, camera_id: str = "CAM01", *, hours: float = 24.0,
                    end_at: float | None = None, open_gap_reason: str | None = None) -> None:
    """24 h of 5-minute segments with a few realistic interruptions (written with the real Ledger)."""
    end_at = now - 65.0 if end_at is None else end_at
    start = end_at - hours * 3600.0
    # (gap start offset from window start in s, length s, reason)
    outages = [(3.6 * 3600, 11 * 60 + 40, "service_interrupted"), (15.9 * 3600, 4 * 60 + 10, "ffmpeg_error")]
    freeze = (9.25 * 3600, 22.0, "source_discontinuity_absorbed")
    led = Ledger(db_path)
    try:
        run_id = str(uuid.uuid4())
        led.create_run(run_id=run_id, camera_id=camera_id, directory="r", launched_utc=iso_utc(start),
                       launch_mono=0.0, display_timezone="UTC", app_version="test")
        t, seq = start, 0
        cut = [(start + a, start + a + b, r) for a, b, r in outages]
        while t < end_at - 1:
            nxt = min(t + SEG, end_at)
            hit = next((c for c in cut if t <= c[0] < nxt), None)
            seg_end = hit[0] if hit else nxt
            if seg_end > t:
                seq += 1
                led.insert_segment(camera_id=camera_id, run_id=run_id, seq=seq, rel_path=f"r/seg_{seq:06d}.ts",
                                   state=SegmentState.FINALIZED, start_utc=iso_utc(t), end_utc=iso_utc(seg_end),
                                   duration_s=seg_end - t, size_bytes=100, sha256="0" * 64)
            if hit:
                with led.tx() as c:
                    c.execute("INSERT INTO recording_gaps(camera_id, kind, start_utc, end_utc, duration_s, reason, "
                              "opened_utc, closed_utc) VALUES (?,?,?,?,?,?,?,?)",
                              (camera_id, "between_runs", iso_utc(hit[0]), iso_utc(hit[1]), hit[1] - hit[0], hit[2],
                               iso_utc(hit[0]), iso_utc(hit[1])))
                t = hit[1]
            else:
                t = nxt
        fs = start + freeze[0]
        with led.tx() as c:
            c.execute("INSERT INTO recording_gaps(camera_id, kind, start_utc, end_utc, duration_s, reason, "
                      "opened_utc, closed_utc) VALUES (?,?,?,?,?,?,?,?)",
                      (camera_id, "within_segment", iso_utc(fs), iso_utc(fs + freeze[1]), freeze[1], freeze[2],
                       iso_utc(fs), iso_utc(fs)))
            if open_gap_reason:
                c.execute("INSERT INTO recording_gaps(camera_id, kind, start_utc, reason, opened_utc, attempts) "
                          "VALUES (?,?,?,?,?,?)", (camera_id, "between_runs", iso_utc(end_at), open_gap_reason,
                                                   iso_utc(end_at), 3))
        led.add_event("warning", "recorder", "gap_closed", "Recording resumed after a gap of 250.0 s")
    finally:
        led.close()


def write_toml(path: Path, cfg_dict: dict[str, Any]) -> Path:
    """Minimal TOML writer for the flat config dictionaries used in tests."""
    lines: list[str] = []
    for section, values in cfg_dict.items():
        lines.append(f"[{section}]")
        for k, v in values.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            else:
                lines.append(f"{k} = {json.dumps(str(v))}")
        lines.append("")
    path.write_text("\n".join(lines), "utf-8")
    return path
