"""Diagnostic report for technical support.

One ZIP file, readable without Sentinel installed:

    summary.txt            plain-language overview (start here)
    status.json            the recorder's latest measured status, including alerts
    events.csv             recent ledger events
    runs.csv / gaps.csv    recent recording runs and recording gaps
    segments.csv           recent segment metadata (states, times, hashes) - NO VIDEO
    ledger_chain.json      recording-log hash-chain head and a fresh tamper check (ledgerchain)
    logs/recorder.log*     tails of the recorder log (credentials redacted again on export)
    config.toml            the configuration file (contains no credentials by design; redacted anyway)
    MANIFEST.sha256        SHA-256 of every file above

Never included: video, the credentials file, or the ledger database itself.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import platform as pyplatform
import shutil
import socket
import time
import zipfile
from pathlib import Path

from . import __version__
from .clock import compact_utc, iso_utc
from .config import AppConfig
from .credentials import redact
from .db import Ledger
from .ledgerchain import ANCHOR_LOG_NAME, append_anchor, current_chain_head, verify_ledger
from .media.probe import ffmpeg_version

log = logging.getLogger(__name__)
LOG_TAIL_BYTES = 2 * 1024 * 1024


def _rows_csv(rows: list, columns: list[str] | None = None) -> str:
    buf = io.StringIO()
    if not rows:
        return ""
    cols = columns or list(rows[0].keys())
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([redact(str(r[c])) if r[c] is not None else "" for c in cols])
    return buf.getvalue()


def _tail(path: Path, nbytes: int) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - nbytes))
        return fh.read().decode("utf-8", "replace")


def build_report(cfg: AppConfig, reason: str = "requested", out_dir: Path | None = None) -> Path:
    out_dir = out_dir or (cfg.data_path / "diagnostics")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = compact_utc(time.time())
    target = out_dir / f"Sentinel_Diagnostics_{cfg.camera.id}_{stamp}.zip"
    files: dict[str, bytes] = {}

    status_path = cfg.status_path
    status_doc = None
    if status_path.exists():
        raw = status_path.read_text("utf-8", "replace")
        files["status.json"] = redact(raw).encode("utf-8")
        try:
            status_doc = json.loads(raw)
        except json.JSONDecodeError:
            pass

    db_path = cfg.data_path / "sentinel.db"
    summary: list[str] = []
    counts: dict[str, int] = {}
    integrity = "ledger not found"
    open_gap = None
    chain_head_line = chain_line = "ledger not found"
    if db_path.exists():
        led = Ledger(db_path, readonly=True)
        try:
            integrity = led.integrity_check()
            counts = led.state_counts()
            files["events.csv"] = _rows_csv(led.query(
                "SELECT * FROM events ORDER BY event_id DESC LIMIT 2000")).encode("utf-8")
            files["runs.csv"] = _rows_csv(led.query(
                "SELECT run_id, launched_utc, anchor_utc, ended_utc, end_reason, exit_code, end_detail, "
                "timeline_break_stream_s, timeline_shift_s, clock_anomaly, ffmpeg_version, app_version "
                "FROM recording_runs ORDER BY rowid DESC LIMIT 500")).encode("utf-8")
            files["gaps.csv"] = _rows_csv(led.query(
                "SELECT * FROM recording_gaps ORDER BY gap_id DESC LIMIT 1000")).encode("utf-8")
            files["segments.csv"] = _rows_csv(led.query(
                "SELECT segment_id, run_id, seq, rel_path, state, start_utc, end_utc, duration_s, size_bytes, "
                "sha256, content_sha256, warnings, finalize_attempts, last_finalize_error, audit_status "
                "FROM segments ORDER BY rowid DESC LIMIT 1000")).encode("utf-8")
            g = led.open_gap(cfg.camera.id)
            open_gap = dict(g) if g else None
            head = current_chain_head(led)
        finally:
            led.close()
        # Every report carries the recording-log chain head (an external anchor once the report
        # leaves the machine) and a fresh chain check. Files are not re-hashed here (slow).
        try:
            chain_rep = verify_ledger(db_path, status_path=cfg.status_path)
            chain_doc = chain_rep.to_dict(max_findings=100)
            chain_line = ((f"intact, {len(chain_rep.faults)} protection gap(s)" if chain_rep.faults else "intact") if chain_rep.ok else
                          f"ALTERED - first problem at entry {chain_rep.first.position}: {chain_rep.first.message}"
                          if chain_rep.first else "ALTERED")
        except Exception as exc:  # the report must still be written
            log.exception("chain verification failed while building diagnostic report")
            chain_doc, chain_line = {"error": str(exc)}, f"could not be checked ({exc})"
        chain_doc["head_at_report"] = head.to_dict() if head else None
        files["ledger_chain.json"] = json.dumps(chain_doc, indent=2, sort_keys=True).encode("utf-8")
        if head is not None:
            chain_head_line = f"entry {head.seq}, fingerprint {head.hash} (written {head.utc})"
            try:
                append_anchor(cfg.data_path / ANCHOR_LOG_NAME, head, "diagnostic_report")
            except OSError as exc:      # e.g. run by an administrator account without write access
                log.warning("could not append chain head to anchor log: %s", exc)
        else:
            chain_head_line = "none (this recording log has no tamper-evidence chain)"

    for name in ("recorder.log", "recorder.log.1"):
        p = cfg.log_path / name
        if p.exists():
            files[f"logs/{name}"] = redact(_tail(p, LOG_TAIL_BYTES)).encode("utf-8")
    if cfg.source_path and Path(cfg.source_path).exists():
        files["config.toml"] = redact(Path(cfg.source_path).read_text("utf-8", "replace")).encode("utf-8")

    try:
        du = shutil.disk_usage(cfg.spool_path)
        disk = f"{du.free / 1e9:.1f} GB free of {du.total / 1e9:.1f} GB"
    except OSError as exc:
        disk = f"unavailable ({exc})"

    alert = (status_doc or {}).get("alert") or {}
    summary += [
        "SENTINEL DIAGNOSTIC REPORT",
        "==========================",
        f"Created (UTC):      {iso_utc(time.time())}",
        f"Reason:             {reason}",
        f"Camera:             {cfg.camera.id} {cfg.camera.name}".rstrip(),
        f"Host:               {socket.gethostname()}  ({pyplatform.platform()})",
        f"Sentinel version:   {__version__}",
        f"FFmpeg:             {ffmpeg_version(cfg.tools.ffmpeg) or 'NOT FOUND'}",
        f"Recording storage:  {disk}",
        "",
        f"Overall status:     {(status_doc or {}).get('overall', 'unknown')}",
        f"Operator message:   {alert.get('message', '(none)')}",
        f"Status written:     {(status_doc or {}).get('generated_utc', 'never')}",
        f"Open recording gap: {json.dumps(open_gap, default=str) if open_gap else 'none'}",
        f"Segments by state:  {json.dumps(counts)}",
        f"Ledger integrity:   {integrity}",
        f"Recording log head: {chain_head_line}",
        f"Recording log check: {chain_line}",
        "",
        "All alerts:",
        *[f"  - {a.get('code')} [{a.get('severity_name')}] {a.get('message')}"
          for a in (status_doc or {}).get("alerts", [])],
        "",
        "This report contains no video and no camera credentials.",
    ]
    files["summary.txt"] = "\n".join(summary).encode("utf-8")
    manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items()))
    files["MANIFEST.sha256"] = manifest.encode("ascii")

    tmp = target.with_suffix(".zip.partial")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(files.items()):
            zf.writestr(name, data)
    tmp.replace(target)
    _prune(out_dir, cfg.service.diagnostics_keep)
    log.warning("diagnostic report written: %s (%s)", target, reason)
    return target


def _prune(out_dir: Path, keep: int) -> None:
    """Diagnostic reports are support material, not evidence; keep the newest ``keep``."""
    reports = sorted(out_dir.glob("Sentinel_Diagnostics_*.zip"))
    for old in reports[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass
