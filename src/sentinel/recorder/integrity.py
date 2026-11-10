"""Low-priority integrity work that must never delay recording or sealing.

1. **Content hashing** of sealed segments (decoded-frame SHA-256 + per-frame sidecar). Runs
   one segment at a time at reduced CPU priority. If it cannot keep up (5MP@30 fps on a slow
   machine), the backlog is reported to administrators; originals are already sealed with
   their file SHA-256, so evidence integrity does not depend on this worker.
2. **Rolling audit**: periodically re-reads sealed segments and checks file SHA-256, presence
   and read-only state, so silent changes to stored originals are detected during long
   unattended operation.
3. **Recording-log chain verification** (``ledgerchain.verify_ledger``): periodically recomputes
   the ledger's hash chain on a separate read-only connection (one consistent snapshot; it never
   holds the recorder's ledger lock) and compares covered rows and saved chain heads. A failure
   raises operator alert V-003. Files are not re-hashed here: the rolling audit already compares
   files with their rows, and rows are compared with the chain, so files are covered transitively.
"""

from __future__ import annotations

import logging
import os
import queue
import stat
import threading
import time
from collections import deque
from pathlib import Path

from ..clock import iso_utc
from ..config import AppConfig
from ..db import Ledger
from ..hashing import sha256_file
from ..ledgerchain import VerifyReport, verify_ledger
from ..media.probe import ProbeError
from ..media.videohash import compute_content_hash
from ..platform.base import Platform

log = logging.getLogger(__name__)

FRAMEHASH_SUFFIX = ".framehash.txt"
MAX_HASH_ATTEMPTS = 3


class IntegrityWorker:
    def __init__(self, cfg: AppConfig, ledger: Ledger, platform: Platform, ffmpeg_version: str | None,
                 audit_interval_s: float = 600.0, audit_batch: int = 12,
                 chain_verify_interval_s: float = 3600.0) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.platform = platform
        self.ffmpeg_version = ffmpeg_version
        self.audit_interval_s = audit_interval_s
        self.audit_batch = audit_batch
        self.chain_verify_interval_s = chain_verify_interval_s
        self._q: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_audit = time.monotonic() + 60.0
        self._next_chain_verify = time.monotonic() + 90.0    # soon after start: re-raises V-003 after a restart
        self._last_chain_problem: str | None = None
        self.chain_report: VerifyReport | None = None
        self.recent_errors: deque[tuple[float, str]] = deque(maxlen=20)
        self.audit_failures: list[dict] = []
        self.hashed = 0

    @property
    def enabled(self) -> bool:
        return self.cfg.recording.content_hash == "deferred"

    def submit(self, segment_id: str) -> None:
        if self.enabled:
            self._q.put(segment_id)

    def backlog(self) -> int:
        return self._q.qsize()

    def start(self) -> None:
        if self.enabled:  # resume work left from before a restart, oldest first
            for row in self.ledger.segments_needing_content_hash(MAX_HASH_ATTEMPTS):
                self._q.put(row["segment_id"])
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="integrity", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = 60.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if time.monotonic() >= self._next_audit:
                    self.audit_once()
                    self._next_audit = time.monotonic() + self.audit_interval_s
                if time.monotonic() >= self._next_chain_verify:
                    self._next_chain_verify = time.monotonic() + self.chain_verify_interval_s
                    self.verify_chain_once()
                try:
                    seg_id = self._q.get(timeout=1.0)
                except queue.Empty:
                    continue
                self.hash_segment(seg_id)
            except Exception as exc:
                log.exception("integrity worker error")
                self.recent_errors.append((time.monotonic(), f"{type(exc).__name__}: {exc}"))
                self._stop.wait(5.0)

    # ------------------------------------------------------------------ content hash
    def hash_segment(self, segment_id: str) -> bool:
        seg = self.ledger.get_segment(segment_id)
        if seg is None or seg["content_sha256"] is not None or seg["sha256"] is None:
            return False
        path = self.cfg.spool_path / seg["rel_path"]
        frames_path = path.with_name(path.stem + FRAMEHASH_SUFFIX) if self.cfg.recording.frame_hashes else None
        tmp = frames_path.with_name(frames_path.name + ".partial") if frames_path else None
        try:
            ch = compute_content_hash(path, self.cfg.tools.ffmpeg, self.ffmpeg_version, seg["pix_fmt"],
                                      frames_out=tmp, popen_kwargs=self.platform.low_priority_popen_kwargs())
        except ProbeError as exc:
            self.ledger.update_segment(segment_id, content_hash_attempts=seg["content_hash_attempts"] + 1)
            self.ledger.add_event("warning", "integrity", "content_hash_failed",
                                  f"Video content fingerprint could not be computed: {exc}",
                                  {"segment_id": segment_id})
            self.recent_errors.append((time.monotonic(), str(exc)))
            return False
        fh_rel = fh_sha = None
        if frames_path is not None and tmp is not None:
            # A sidecar left by an interrupted earlier attempt was never recorded in the ledger
            # (the ledger write is last), so it may be replaced. Clear read-only first (Windows).
            if frames_path.exists():
                os.chmod(frames_path, stat.S_IREAD | stat.S_IWRITE)
            os.replace(tmp, frames_path)
            self.platform.fsync_file(frames_path)
            fh_sha, _ = sha256_file(frames_path)
            self.platform.seal_read_only(frames_path)
            self.platform.fsync_dir(frames_path.parent)
            fh_rel = str(frames_path.relative_to(self.cfg.spool_path)).replace("\\", "/")
        self.ledger.update_segment(segment_id, content_sha256=ch.sha256, content_hash_method=ch.method,
                                   decoded_frame_count=ch.frame_count, framehash_rel_path=fh_rel,
                                   framehash_sha256=fh_sha)
        self.hashed += 1
        log.info("content hash %s frames=%d sha256=%s", seg["rel_path"], ch.frame_count, ch.sha256[:16])
        return True

    # ------------------------------------------------------------------ audit
    def audit_once(self) -> list[dict]:
        """Re-verify the least recently audited sealed segments. Returns failures found."""
        failures = []
        for seg in self.ledger.segments_for_audit(self.audit_batch):
            path = self.cfg.spool_path / seg["rel_path"]
            status = "ok"
            if not path.exists():
                status = "missing"
            elif not self.platform.is_read_only(path):
                status = "not_read_only"
            else:
                sha, size = sha256_file(path)
                if sha != seg["sha256"] or size != seg["size_bytes"]:
                    status = "hash_mismatch"
            self.ledger.update_segment(seg["segment_id"], last_audit_utc=iso_utc(time.time()), audit_status=status)
            if status != "ok":
                f = {"segment_id": seg["segment_id"], "rel_path": seg["rel_path"], "status": status}
                failures.append(f)
                log.critical("integrity audit failed: %s", f)
                self.ledger.add_event("error", "integrity", f"audit_{status}",
                                      "A stored recording failed its integrity check", f)
        if failures:
            self.audit_failures.extend(failures)
        return failures

    # ------------------------------------------------------------------ recording-log chain
    def verify_chain_once(self) -> VerifyReport:
        """Verify the ledger's hash chain (read-only snapshot). Records an event when the first
        problem found changes, so an hourly check does not flood the ledger."""
        rep = verify_ledger(self.ledger.path)
        self.chain_report = rep
        first = rep.first
        problem = f"{first.code}@{first.position}" if first is not None else None
        if first is not None:
            log.critical("recording-log chain check FAILED at entry %s: %s (%d problems)",
                         first.position, first.message, len(rep.findings))
            if problem != self._last_chain_problem:
                self.ledger.add_event("error", "integrity", "ledger_chain_altered",
                                      "The recording log failed its tamper-evidence check",
                                      {"first_position": first.position, "first_code": first.code,
                                       "first_problem": first.message, "problems": len(rep.findings),
                                       "entries": rep.entries})
        else:
            log.info("recording-log chain verified: %d entries in %.2f s", rep.entries, rep.seconds)
        self._last_chain_problem = problem
        return rep

    @property
    def chain_altered(self) -> bool:
        return self.chain_report is not None and not self.chain_report.ok

    def outstanding_audit_failures(self) -> int:
        rows = self.ledger.query("SELECT COUNT(*) n FROM segments WHERE audit_status IS NOT NULL "
                                 "AND audit_status != 'ok'")
        return int(rows[0]["n"])


def framehash_path_for(segment_path: Path) -> Path:
    return segment_path.with_name(segment_path.stem + FRAMEHASH_SUFFIX)
