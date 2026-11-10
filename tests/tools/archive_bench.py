"""Throughput / CPU measurement of the real archive worker, and synthetic sealed segments.

Development tool (not part of the product): ``python tests/tools/archive_bench.py --dir <empty temp folder>
[--segments 8] [--segment-mb 256] [--rate-mb-s 0]`` with ``PYTHONPATH=src``. builds a throw-away spool + ledger inside ``--dir`` with synthetic sealed
segments (random bytes, hashed, read-only), then runs the actual :class:`ArchiveWorker` (same
code path as production: throttle, 8 MiB chunks, fsync every 64 MiB, no-replace rename,
uncached read-back, ledger rows, journal, index) to a "drive" folder inside ``--dir`` and
reports wall time, MB/s and CPU seconds. ``--rate-mb-s 0`` disables the throttle to find the
ceiling of the machine; the default configuration value is reported for comparison.

What this does NOT measure: a real USB-attached 3.5" platter drive. The folder is wherever
``--dir`` lives (on the development host: the internal NVMe SSD).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from sentinel.archive.locator import DriveScan
from sentinel.clock import iso_utc
from sentinel.config import AppConfig, ArchiveConfig, load_config_dict
from sentinel.db import Ledger
from sentinel.export.target import ExportTarget
from sentinel.models import SegmentState

MB = 1_000_000


class FixedLocator:
    """A locator for tests and benchmarks: the 'attached' drive is whatever ``target`` is."""

    def __init__(self, target: ExportTarget | None = None) -> None:
        self.target = target

    def scan(self) -> DriveScan:
        if self.target is None or not Path(self.target.root).is_dir():
            return DriveScan()
        return DriveScan([self.target])


def add_synthetic_run(ledger: Ledger, cfg: AppConfig, n: int, size: int, *, seconds_each: float = 300.0,
                      start_epoch: float | None = None, state: SegmentState = SegmentState.FINALIZED,
                      sidecars: bool = False) -> list[str]:
    """Create one ended recording run with ``n`` sealed segments of ``size`` random bytes each,
    exactly as the recorder would leave them (files read-only, SHA-256 in the ledger)."""
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    t0 = start_epoch if start_epoch is not None else time.time() - n * seconds_each - 60
    rel_dir = f"{cfg.camera.id}/{run_id}"
    d = cfg.spool_path / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    ledger.create_run(run_id=run_id, camera_id=cfg.camera.id, directory=rel_dir, launched_utc=iso_utc(t0),
                      launch_mono=0.0, display_timezone=cfg.recording.display_timezone, app_version="synthetic",
                      ended_utc=iso_utc(t0 + n * seconds_each), end_reason="graceful_stop", gap_recorded=1)
    ids = []
    for i in range(n):
        name = f"seg_{i:06d}.ts"
        p = d / name
        data = os.urandom(size)
        p.write_bytes(data)
        os.chmod(p, stat.S_IREAD)
        cols: dict[str, Any] = dict(start_utc=iso_utc(t0 + i * seconds_each), end_utc=iso_utc(t0 + (i + 1) * seconds_each),
                                    duration_s=seconds_each, size_bytes=size, sha256=hashlib.sha256(data).hexdigest(),
                                    sealed_utc=iso_utc(time.time()), time_source="synthetic",
                                    content_hash_attempts=99, last_audit_utc=iso_utc(time.time()), audit_status="ok")
        if state is SegmentState.INTERRUPTED:
            cols["interrupted"] = 1
        if sidecars:
            fh = d / f"seg_{i:06d}.framehash.txt"
            fdata = f"#synthetic per-frame list {i}\n".encode("ascii")
            fh.write_bytes(fdata)
            os.chmod(fh, stat.S_IREAD)
            cols.update(framehash_rel_path=f"{rel_dir}/{fh.name}", framehash_sha256=hashlib.sha256(fdata).hexdigest())
        ids.append(ledger.insert_segment(camera_id=cfg.camera.id, run_id=run_id, seq=i, rel_path=f"{rel_dir}/{name}",
                                         state=state, **cols))
    return ids


def bench_config(root: Path, **archive: Any) -> AppConfig:
    cfg = load_config_dict({
        "camera": {"id": "BENCH", "url": "tcp://127.0.0.1:9", "input_format": "mpegts", "development_source": True},
        "storage": {"spool_dir": str(root / "spool"), "data_dir": str(root / "data"), "log_dir": str(root / "logs"),
                    "warning_free_gb": 0.0, "critical_free_gb": 0.0},
        "archive": dict({"enabled": True, "drive_path": str(root / "drive"), "require_drive_marker": False,
                         "reserve_free_mb": 64.0}, **archive),
    }, base_dir=root)
    return cfg


def run_bench(root: Path, segments: int, segment_mb: float, rate_mb_s: float) -> dict[str, Any]:
    from sentinel.archive.worker import ArchiveWorker

    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"--dir must be empty or missing: {root}")
    size = int(segment_mb * MB)
    default_rate = ArchiveConfig().max_rate_mb_s
    cfg = bench_config(root, max_rate_mb_s=rate_mb_s if rate_mb_s > 0 else 1e9)
    ledger = Ledger(cfg.data_path / "sentinel.db")
    try:
        add_synthetic_run(ledger, cfg, segments, size)
        drive = root / "drive"
        drive.mkdir()
        target = ExportTarget(drive, {"drive_uuid": str(uuid.uuid4()), "label": "BENCH"})
        w = ArchiveWorker(cfg, ledger=ledger, locator=FixedLocator(target), allow_same_volume=True,
                          lower_own_priority=False)
        cpu0, t0 = time.process_time(), time.perf_counter()
        w.step()                                   # attach (+ empty index)
        while w.step() == "copied":
            pass
        wall, cpu = time.perf_counter() - t0, time.process_time() - cpu0
        total = segments * size
        st = w.stats
        mbps = total / MB / wall
        result = {
            "segments": segments, "segment_mb": segment_mb, "total_mb": round(total / MB, 1),
            "throttle_mb_s": rate_mb_s if rate_mb_s > 0 else None, "default_throttle_mb_s": default_rate,
            "wall_s": round(wall, 2), "end_to_end_mb_s": round(mbps, 1),
            "copy_mb_s": round(st.bytes / MB / st.copy_s, 1) if st.copy_s else None,
            "verify_mb_s": round(st.bytes / MB / st.verify_s, 1) if st.verify_s else None,
            "cpu_s": round(cpu, 2), "cpu_percent_of_one_core": round(100 * cpu / wall, 1),
            "cpu_s_per_gb": round(cpu / (total / 1e9), 2),
            "verified_copies": len(ledger.query("SELECT 1 FROM archive_copies")),
            "margin_vs_camera": {f"{m} Mbit/s": round(mbps / (m / 8), 1) for m in (4, 8, 12)},
            "hours_to_drain_24h_at_12_mbit_s": round(24 * 3600 * 1.5 / mbps / 3600, 2),
            "note": "Measured to a folder on this machine's own disk; a USB 3.5-inch drive was NOT tested.",
        }
        return result
    finally:
        ledger.close()


def main_bench(root: Path, segments: int, segment_mb: float, rate_mb_s: float) -> int:
    print(json.dumps(run_bench(root, segments, segment_mb, rate_mb_s), indent=2))
    return 0


__all__ = ["FixedLocator", "add_synthetic_run", "bench_config", "run_bench"]


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="archive copy+verify throughput and CPU on this machine")
    ap.add_argument("--dir", required=True, help="empty folder (created) on the disk to measure")
    ap.add_argument("--segments", type=int, default=8)
    ap.add_argument("--segment-mb", type=float, default=256.0)
    ap.add_argument("--rate-mb-s", type=float, default=0.0, help="throttle; 0 = unthrottled")
    a = ap.parse_args()
    sys.exit(main_bench(Path(a.dir), a.segments, a.segment_mb, a.rate_mb_s))
