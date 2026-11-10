"""Startup reconciliation of the spool directory against the ledger.

Runs before recording starts. Handles: service crash / power loss mid-run, finaliser
interrupted before sealing, files written after the last ledger commit, and run
directories the ledger has no record of. Everything is routed through the finaliser
queue so ordering guarantees (segments before the gap that follows them) still hold.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from ..clock import iso_utc
from ..config import AppConfig
from ..db import Ledger
from ..models import RunEndReason, SegmentState
from .ffmpeg_cmd import SEGMENT_LIST_NAME
from .finalizer import Finalizer, GapJob, SegmentJob
from .segment_list import SegmentListReader

log = logging.getLogger(__name__)
_SEG_RE = re.compile(r"^seg_(\d{6,})\.ts$")
_RUN_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


@dataclass
class RecoveryReport:
    runs_closed: int = 0
    orphan_runs: int = 0
    files_registered: int = 0
    closed_resubmitted: int = 0
    interrupted_submitted: int = 0
    gaps_submitted: int = 0
    growing_files: int = 0

    def any_work(self) -> bool:
        return any(vars(self).values())


class Recovery:
    def __init__(self, cfg: AppConfig, ledger: Ledger, finalizer: Finalizer, stability_wait_s: float = 2.0) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.finalizer = finalizer
        self.stability_wait_s = stability_wait_s

    def run(self) -> RecoveryReport:
        rep = RecoveryReport()
        cam_dir = self.cfg.spool_path / self.cfg.camera.id
        cam_dir.mkdir(parents=True, exist_ok=True)
        self._register_orphan_dirs(cam_dir, rep)

        for run in self.ledger.unfinished_runs():
            self._close_crashed_run(run["run_id"], rep)

        # Work is queued run by run, in launch order: a run's unsealed segments, then the gap
        # that follows it. Queuing all segments first would let a later run's footage be
        # processed before an earlier run's gap is opened.
        pending = self.ledger.segments_in_states([SegmentState.CLOSED, SegmentState.ACTIVE])
        by_run: dict[str, list] = {}
        for seg in pending:
            by_run.setdefault(seg["run_id"], []).append(seg)
        need_gap = {r["run_id"] for r in self.ledger.runs_needing_gap()}
        runs = self.ledger.query("SELECT * FROM recording_runs WHERE ended_utc IS NOT NULL ORDER BY rowid")
        for run in runs:
            rid = run["run_id"]
            for seg in sorted(by_run.get(rid, []), key=lambda s: s["seq"]):
                if seg["state"] == str(SegmentState.CLOSED):
                    # Validation never completed (finaliser stopped before sealing).
                    self.finalizer.submit(SegmentJob(seg["segment_id"], "closed"))
                    rep.closed_resubmitted += 1
                    continue
                # ACTIVE in an ended run: the writer is gone, so the segment was interrupted.
                path = self.cfg.spool_path / seg["rel_path"]
                if self._still_growing(path):
                    rep.growing_files += 1
                    log.critical("file %s is still being written by an unknown process; not sealing",
                                 seg["rel_path"])
                    self.ledger.add_event("error", "recovery", "orphan_writer",
                                          "A recording file is still changing although no recorder owns it",
                                          {"segment_id": seg["segment_id"]})
                    continue
                self.finalizer.submit(SegmentJob(seg["segment_id"], "interrupted"))
                rep.interrupted_submitted += 1
            if rid in need_gap:
                self.finalizer.submit(GapJob(rid, run["end_reason"] or str(RunEndReason.SERVICE_INTERRUPTED),
                                             {"recovered": True, "exit_code": run["exit_code"]}))
                rep.gaps_submitted += 1

        self._check_sealed_files_exist()
        if rep.any_work():
            log.warning("recovery: %s", rep)
            self.ledger.add_event("warning", "recovery", "startup_recovery",
                                  "Recovered recording state after an unclean stop", vars(rep))
        return rep

    # ------------------------------------------------------------------
    def _close_crashed_run(self, run_id: str, rep: RecoveryReport) -> None:
        run = self.ledger.get_run(run_id)
        assert run is not None
        run_dir = self.cfg.spool_path / run["directory"]
        known = {Path(s["rel_path"]).name: s for s in self.ledger.run_segments(run_id)}
        self._register_files(run_id, run_dir, known, rep)
        known = {Path(s["rel_path"]).name: s for s in self.ledger.run_segments(run_id)}

        # Files FFmpeg listed as closed before the crash can be finalised normally.
        entries = SegmentListReader(run_dir / SEGMENT_LIST_NAME).read_all()
        listed = {e.filename: e for e in entries}
        anchor = run["anchor_utc"]
        from ..clock import add_seconds_iso

        for name, e in listed.items():
            seg = known.get(name)
            if seg is None or seg["state"] != str(SegmentState.ACTIVE):
                continue
            self.ledger.transition(
                seg["segment_id"], SegmentState.CLOSED, stream_start_s=e.start_s, stream_end_s=e.end_s,
                start_utc=add_seconds_iso(anchor, e.start_s) if anchor else None,
                end_utc=add_seconds_iso(anchor, e.end_s) if anchor else None,
                time_source="run_anchor" if anchor else None, observed_closed_utc=iso_utc(time.time()))

        # Estimate when the run died: latest file modification inside its directory.
        mtimes = [p.stat().st_mtime for p in run_dir.glob("seg_*.ts")] if run_dir.exists() else []
        ended = iso_utc(max(mtimes)) if mtimes else run["launched_utc"]
        self.ledger.update_run(run_id, ended_utc=ended, end_reason=str(RunEndReason.SERVICE_INTERRUPTED),
                               end_detail="recorder service stopped unexpectedly (crash, kill or power loss); "
                                          "ended_utc estimated from file modification time")
        prev_end: float | None = 0.0
        for seg in self.ledger.run_segments(run_id):
            if seg["state"] == str(SegmentState.ACTIVE):
                self.ledger.update_segment(seg["segment_id"], stream_start_s=prev_end)
                prev_end = None
            else:
                prev_end = seg["stream_end_s"]
        rep.runs_closed += 1
        log.warning("recovery: run %s was interrupted by a service failure", run_id)

    def _register_files(self, run_id: str, run_dir: Path, known: dict, rep: RecoveryReport) -> None:
        if not run_dir.exists():
            return
        for p in sorted(run_dir.iterdir()):
            m = _SEG_RE.match(p.name)
            if not m or p.name in known:
                continue
            rel = str(p.relative_to(self.cfg.spool_path)).replace("\\", "/")
            if self.ledger.segment_by_path(rel) is not None:
                continue
            self.ledger.insert_segment(camera_id=self.cfg.camera.id, run_id=run_id, seq=int(m.group(1)),
                                       rel_path=rel, observed_first_utc=iso_utc(time.time()))
            rep.files_registered += 1

    def _register_orphan_dirs(self, cam_dir: Path, rep: RecoveryReport) -> None:
        """Run directories with no ledger record (ledger lost or restored from an old backup)."""
        for d in sorted(cam_dir.iterdir()):
            if not d.is_dir() or not _RUN_RE.match(d.name) or self.ledger.get_run(d.name) is not None:
                continue
            if not any(d.glob("seg_*.ts")):
                continue  # a launch that failed before recording anything; nothing to recover
            rel = str(d.relative_to(self.cfg.spool_path)).replace("\\", "/")
            mtimes = [p.stat().st_mtime for p in d.glob("seg_*.ts")]
            launched = min(mtimes) if mtimes else d.stat().st_mtime
            self.ledger.create_run(
                run_id=d.name, camera_id=self.cfg.camera.id, directory=rel, launched_utc=iso_utc(launched),
                launch_mono=0.0, display_timezone=self.cfg.recording.display_timezone, app_version=__version__,
                command_redacted=None, end_detail="run directory found without ledger record")
            self.ledger.add_event("error", "recovery", "orphan_run_dir",
                                  "Found recordings the ledger had no record of; times from file metadata",
                                  {"run_id": d.name})
            rep.orphan_runs += 1
            # Leave ended_utc NULL so _close_crashed_run handles it like any interrupted run.

    def _still_growing(self, path: Path) -> bool:
        if not path.exists():
            return False
        a = path.stat().st_size
        time.sleep(self.stability_wait_s)
        return path.stat().st_size != a

    def _check_sealed_files_exist(self) -> None:
        for seg in self.ledger.segments_in_states([SegmentState.FINALIZED, SegmentState.INTERRUPTED]):
            if not (self.cfg.spool_path / seg["rel_path"]).exists():
                log.error("sealed recording missing from local storage: %s", seg["rel_path"])
                self.ledger.add_event("error", "recovery", "sealed_file_missing",
                                      "A sealed recording file is missing from local storage",
                                      {"segment_id": seg["segment_id"], "rel_path": seg["rel_path"]})
