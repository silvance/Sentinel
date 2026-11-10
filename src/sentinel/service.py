"""Recorder service entry point.

    python -m sentinel.service run          --config /etc/sentinel/sentinel.toml
    python -m sentinel.service check-config --config ...
    python -m sentinel.service status       --config ...
    python -m sentinel.service diagnostics  --config ... [--out DIR]

The service runs until SIGTERM/SIGINT (or CTRL_BREAK on Windows). It never depends on a GUI;
closing any GUI has no effect on it. It is designed to run unattended for days:
* the supervisor retries the camera forever (see recorder/supervisor.py);
* a janitor periodically retries stuck segments, restarts dead worker threads and
  checkpoints the ledger;
* under systemd, a watchdog ping is sent only from the recording loop itself, so a hung
  recorder is killed and restarted by systemd (WatchdogSec in the unit file);
* on every platform an internal watchdog thread (sentinel.watchdog) ends the process with
  EXIT_WATCHDOG if the recording loop has not run for ``[service] watchdog_timeout_s``, so the
  Windows Service Control Manager (which has no watchdog) restarts it too;
* when any alert first becomes critical a diagnostic report is written automatically.

Exit codes: 0 stopped on request, 2 configuration/startup problem, 3 another instance running,
4 supervisor failed repeatedly, 5 watchdog: recording loop hung.
"""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import logging
import os
import signal
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .alerts import Severity
from .clock import Clock, SystemClock, iso_utc, parse_iso_utc
from .config import AppConfig, ConfigError, check_tools, load_config
from .credentials import CredentialError, load_credentials, register_secret, url_with_credentials
from .db import Ledger
from .diagnostics import build_report
from .ledgerchain import ANCHOR_LOG_NAME, ChainHead, append_anchor, current_chain_head
from .logging_setup import setup_logging
from .media.probe import ffmpeg_version
from .media.videohash import parse_version
from .models import SegmentState, StorageLevel
from .platform import InstanceLockError, Platform, get_platform
from .recorder.finalizer import MAX_FINALIZE_ATTEMPTS, Finalizer, SegmentJob
from .recorder.integrity import IntegrityWorker
from .recorder.recovery import Recovery
from .ledger_timing import insert_clock_observation
from .recorder.supervisor import RecordingSupervisor
from .recorder.timebase import ClockStepDetector, read_clock_pair
from .status import Measurements, StatusDocument, apply_alerts, evaluate, fatal_status_document, write_status
from .storage.monitor import DiskUsage, StorageMonitor
from .timesync import query_os_time_sync, sntp_offset
from .watchdog import LoopWatchdog

log = logging.getLogger("sentinel.service")

EXIT_OK, EXIT_CONFIG, EXIT_LOCKED, EXIT_RUNTIME, EXIT_WATCHDOG = 0, 2, 3, 4, 5
MIN_FFMPEG = (6, 0)          # tested on 9.0.1; RTSP -timeout semantics and -fps_mode need >= 5.1
WATCHDOG_STEP_TIMEOUT_S = 5.0  # each best-effort action before a watchdog exit is bounded
# Bounded stop when the operating system is shutting down. Worst case after the stop request:
# FFmpeg stop (stop_grace_s 8 + wait 30 + reader joins 10 ≈ 50 s) + drain 45 + current job 60 +
# integrity 10 ≈ 165 s, inside the 180 s Windows pre-shutdown window (winsvc.commands).
SHUTDOWN_DRAIN_S = 45.0
SHUTDOWN_JOB_S = 60.0
SHUTDOWN_INTEGRITY_S = 10.0
SHUTDOWN_BUDGET_S = 50.0 + SHUTDOWN_DRAIN_S + SHUTDOWN_JOB_S + SHUTDOWN_INTEGRITY_S
LEDGER_SNAPSHOT_INTERVAL_S = 300.0


def _bounded(name: str, fn: Callable[[], object], timeout_s: float) -> bool:
    """Run ``fn`` in a daemon thread and wait at most ``timeout_s``: a hung lock elsewhere in the
    process must not stop the watchdog from ending the process."""
    done = threading.Event()

    def target() -> None:
        try:
            fn()
        except Exception:
            pass
        finally:
            done.set()
    threading.Thread(target=target, name=f"watchdog-{name}", daemon=True).start()
    return done.wait(timeout_s)


class RecorderService:
    def __init__(self, cfg: AppConfig, platform: Platform | None = None, clock: Clock | None = None,
                 disk_usage: Callable[[Path], DiskUsage] | None = None,
                 watchdog_exit: Callable[[int], None] | None = None) -> None:
        self.cfg = cfg
        self.platform = platform or get_platform()
        self.clock = clock or SystemClock()
        self.stop_event = threading.Event()
        self._disk_usage = disk_usage
        # os._exit, not sys.exit: the main loop is hung, so the process cannot unwind normally.
        # FFmpeg dies with the process (Windows job object / Linux PDEATHSIG).
        self._watchdog_exit = watchdog_exit or os._exit
        self.watchdog = LoopWatchdog(cfg.service.watchdog_timeout_s, self._on_watchdog_expired,
                                     mono=time.monotonic)
        self._last_status_mono = 0.0
        self._started_mono = self.clock.mono()
        self._fatal: list[str] = []                  # alert codes that prevent recording
        self.status_path = cfg.status_path
        self.ledger: Ledger | None = None
        self.supervisor: RecordingSupervisor | None = None
        self.finalizer: Finalizer | None = None
        self.integrity: IntegrityWorker | None = None
        self.storage: StorageMonitor | None = None
        self._next_janitor = 0.0
        self._next_checkpoint = 0.0
        self._next_anchor = self.clock.mono() + cfg.service.chain_anchor_interval_s
        self._last_anchor: ChainHead | None = None
        self._next_ntp = 0.0
        self._ntp: bool | None = None
        self._clock_offset: float | None = None      # wall - mono at last tick
        self._clock_anomaly_until: float = 0.0
        self._critical_codes: set[str] = set()
        self.last_report: str | None = None
        self._terminal = False            # status says "cannot record" and the process is ending
        self._system_shutdown = False     # stop because the OS is shutting down: bounded drain
        self._next_snapshot = 0.0
        # E1 clock observations (docs/03 §4.4): host wall-vs-monotonic drift since service start
        # (steps excluded) and periodic OS time-sync records.
        self._host_clock: ClockStepDetector | None = None
        self._next_clock_obs = 0.0
        self._clock_obs_thread: threading.Thread | None = None
        self.last_clock_observation: dict | None = None
        self._closing = threading.Event()

    # ------------------------------------------------------------------
    def run(self) -> int:
        cfg = self.cfg
        self.platform.restrict_new_files()
        try:
            lock = self.platform.acquire_instance_lock(cfg.data_path / "recorder.lock")
        except InstanceLockError as exc:
            log.error("%s", exc)
            return EXIT_LOCKED
        try:
            return self._run_locked()
        finally:
            lock.close()

    def _run_locked(self) -> int:
        cfg = self.cfg
        cfg.spool_path.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(cfg.data_path / "sentinel.db")
        self.storage = StorageMonitor(cfg.spool_path, cfg.storage.warning_free_gb, cfg.storage.critical_free_gb,
                                      disk_usage=self._disk_usage)
        self._audit_start()

        problems = check_tools(cfg)
        version = ffmpeg_version(cfg.tools.ffmpeg) if not problems else None
        parsed = parse_version(version)
        if version and parsed is None:
            log.warning("could not parse FFmpeg version %r; minimum %s.%s not verified", version, *MIN_FFMPEG)
            self.ledger.add_event("warning", "config", "ffmpeg_version_unknown", f"Unrecognised FFmpeg: {version}")
        elif parsed is not None and parsed < MIN_FFMPEG:
            problems.append(f"FFmpeg {parsed[0]}.{parsed[1]} is older than the minimum {MIN_FFMPEG[0]}.{MIN_FFMPEG[1]}")
        creds = None
        if cfg.camera.credentials_file:
            try:
                creds = load_credentials(cfg.camera.credentials_file)
                register_secret(creds.password)
                register_secret(creds.username)
                perm = self.platform.secret_file_problems(Path(cfg.camera.credentials_file))
                for p in perm:
                    log.error("credentials file: %s", p)
                    self.ledger.add_event("warning", "config", "secret_permissions", p)
                if perm:  # POSIX mode bits / Windows DACL (both implemented and enforced)
                    problems.append("camera credentials file is accessible by other accounts")
                if self.platform.name == "windows" and creds.protection != "dpapi":
                    log.warning("camera credentials are stored as plain text; protect them with "
                                "'python -m sentinel.winsvc set-camera-credentials'")
                    self.ledger.add_event("warning", "config", "secret_plaintext",
                                          "Camera credentials file is not DPAPI-protected")
            except CredentialError as exc:
                problems.append(str(exc))
        if problems:
            for p in problems:
                log.error("cannot start recording: %s", p)
            self._fatal = ["C-001"]
            self._terminal = True
            self._write_status(force=True)
            self.ledger.add_event("error", "recorder", "startup_failed", "; ".join(problems))
            self._anchor_chain("startup_failed")
            self._maybe_report()
            return EXIT_CONFIG

        self.integrity = IntegrityWorker(cfg, self.ledger, self.platform, version,
                                         audit_interval_s=cfg.service.audit_interval_s,
                                         chain_verify_interval_s=cfg.service.chain_verify_interval_s)
        self.finalizer = Finalizer(cfg, self.ledger, self.platform, self.storage, version,
                                   on_sealed=self.integrity.submit)
        report = Recovery(cfg, self.ledger, self.finalizer).run()
        log.info("startup recovery: %s", report)
        self._anchor_chain("service_start")
        self.finalizer.start()
        self.integrity.start()
        w0, m0, _ = read_clock_pair(self.clock)
        self._host_clock = ClockStepDetector(w0, m0, cfg.recording.clock_step_tolerance_s)
        self._next_clock_obs = self.clock.mono() + cfg.service.clock_observation_interval_s
        self._sample_clock("startup")
        self.supervisor = RecordingSupervisor(
            cfg, self.ledger, self.platform, self.storage, self.finalizer,
            url_with_credentials(cfg.camera.url, creds), self.stop_event, clock=self.clock,
            ffmpeg_version=version, tick=self._tick)
        self._write_status(force=True)
        self.platform.notify("READY=1")
        exit_code = EXIT_OK
        if self.cfg.service.watchdog_timeout_s < 60:
            log.warning("watchdog_timeout_s=%.0f is short; a normal camera reconnect may pause the recording "
                        "loop for up to ~50 s (120 s recommended)", self.cfg.service.watchdog_timeout_s)
        self.watchdog.start()
        try:
            self.supervisor.run_forever()
        except Exception:
            log.exception("recording supervisor failed repeatedly; exiting so the service manager restarts it")
            self.ledger.add_event("error", "recorder", "supervisor_crash",
                                  "Recording supervisor failed repeatedly; service restarting")
            self._fatal = ["R-006"]
            exit_code = EXIT_RUNTIME
        finally:
            self.watchdog.stop()
            self.platform.notify("STOPPING=1")
            if self._system_shutdown:
                # The OS gives a bounded window (Windows pre-shutdown, SHUTDOWN_DRAIN_BUDGET_S).
                # Whatever is not sealed in time stays unsealed on disk and is sealed by startup
                # recovery at the next boot (the same path as a crash; nothing is lost or altered).
                drained = self.finalizer.stop(drain_timeout=SHUTDOWN_DRAIN_S, job_timeout=SHUTDOWN_JOB_S)
                self.integrity.stop(timeout=SHUTDOWN_INTEGRITY_S)
            else:
                drained = self.finalizer.stop()
                self.integrity.stop()
            if not drained:
                log.error("finaliser did not drain before shutdown; recovery will complete it on next start")
            self.ledger.add_event("audit", "recorder", "service_stop", "Recorder service stopped",
                                  {"drained": drained, "exit_code": exit_code})
            self._anchor_chain("service_stop")
            # A crash is reported as an error, never as a clean stop.
            self._write_status(force=True, stopping=exit_code == EXIT_OK)
            if exit_code != EXIT_OK:
                self._maybe_report()
            # An observation still querying the OS must not write after the ledger is closed: it
            # checks _closing before writing; the join covers its worst-case query time.
            self._closing.set()
            if self._clock_obs_thread is not None:
                self._clock_obs_thread.join(30)
            self.ledger.close()
        return exit_code

    def _audit_start(self) -> None:
        assert self.ledger is not None
        cfg_hash = None
        if self.cfg.source_path:
            cfg_hash = hashlib.sha256(Path(self.cfg.source_path).read_bytes()).hexdigest()
        prev = self.ledger.query("SELECT detail FROM events WHERE code='service_start' ORDER BY event_id DESC LIMIT 1")
        prev_hash = json.loads(prev[0]["detail"]).get("config_sha256") if prev and prev[0]["detail"] else None
        self.ledger.add_event("audit", "recorder", "service_start", f"Recorder service {__version__} started",
                              {"config_sha256": cfg_hash, "camera_id": self.cfg.camera.id,
                               "platform": self.platform.name})
        if prev and prev_hash != cfg_hash:
            self.ledger.add_event("audit", "config", "config_changed", "Configuration changed since last start",
                                  {"previous_sha256": prev_hash, "current_sha256": cfg_hash})

    # ------------------------------------------------------------------ periodic work (supervisor thread)
    def _tick(self) -> None:
        """Called from the recording loop. Watchdog ping proves the loop itself is alive."""
        self.watchdog.beat()
        self.platform.notify("WATCHDOG=1")
        now = self.clock.mono()
        self._check_clock(now)
        if now >= self._next_janitor:
            self._next_janitor = now + self.cfg.service.janitor_interval_s
            try:
                self._janitor(now)
            except Exception:
                log.exception("janitor failed")
        self._write_status()

    def _check_clock(self, now: float) -> None:
        offset = self.clock.wall() - now
        if self._clock_offset is not None and abs(offset - self._clock_offset) > self.cfg.recording.clock_step_tolerance_s:
            step = offset - self._clock_offset
            log.error("system clock stepped by %+.1f s", step)
            self._clock_anomaly_until = now + 24 * 3600
            assert self.ledger is not None
            try:
                self.ledger.add_event("warning", "clock", "clock_step_service",
                                      f"System clock changed by {step:+.1f} s", {"step_s": step})
                self.ledger.flag_open_gap_clock_anomaly(self.cfg.camera.id)
            except Exception:
                log.exception("could not record clock step")
            self._sample_clock("clock_step")
        self._clock_offset = offset
        if self._host_clock is not None:
            w_pair, m_pair, spread = read_clock_pair(self.clock)   # tight pair, not ``now``
            self._host_clock.update(w_pair, m_pair, spread)
        if now >= self._next_ntp:
            self._next_ntp = now + 300.0
            self._ntp = self.platform.clock_synchronized()
        if now >= self._next_clock_obs:
            self._next_clock_obs = now + self.cfg.service.clock_observation_interval_s
            self._sample_clock("periodic")

    # ------------------------------------------------------------------ clock observations (E1)
    def _sample_clock(self, reason: str) -> None:
        """Record a clock observation in the background (OS query commands can take seconds;
        the recording loop must not wait for them). Skipped if one is still running."""
        if self._clock_obs_thread is not None and self._clock_obs_thread.is_alive():
            return
        self._clock_obs_thread = threading.Thread(target=self._observe_clock, args=(reason,),
                                                  name="clock-observation", daemon=True)
        self._clock_obs_thread.start()

    def _observe_clock(self, reason: str) -> None:
        try:
            wall, mono, _ = read_clock_pair(self.clock)
            svc = self.cfg.service
            osync = query_os_time_sync(max_sync_age_s=svc.max_sync_age_s or None)
            ref = sntp_offset(svc.time_reference_server, clock=self.clock.wall) if svc.time_reference_server else None
            host = self._host_clock
            cols: dict = {
                "observed_utc": iso_utc(wall), "mono_s": mono, "reason": reason, "platform": self.platform.name,
                "sync_tool": osync.tool,
                "synchronized": None if osync.synchronized is None else int(osync.synchronized),
                "sync_source": osync.source, "last_sync_utc": osync.last_sync_utc, "os_offset_s": osync.offset_s,
                "os_detail": json.dumps(osync.detail, sort_keys=True, default=str),
                "wall_minus_mono_s": wall - mono,
                "host_clock_drift_s": round(host.drift_s, 6) if host else None,
                "host_clock_drift_ppm": (round(host.drift_ppm, 3) if host and host.drift_ppm is not None else None),
                "clock_steps_total_s": round(host.total_s, 6) if host else None,
            }
            if ref is not None:
                # Measured offset of this PC's clock from the configured reference (E1).
                cols.update(ref_server=svc.time_reference_server, ref_offset_s=ref.offset_s,
                            ref_delay_s=ref.delay_s, ref_detail=json.dumps(ref.detail, sort_keys=True))
            sup = self.supervisor
            if sup is not None:
                with sup.live.lock:
                    lv = sup.live
                    if lv.ffmpeg_running:
                        cols.update(run_id=lv.run_id, stream_time_s=lv.last_out_time_s,
                                    camera_drift_ppm=lv.camera_drift_ppm, time_correction_s=lv.time_correction_s)
            assert self.ledger is not None
            if self._closing.is_set():
                log.info("clock observation (%s) discarded: service is stopping", reason)
                return
            insert_clock_observation(self.ledger, cols)
            self.last_clock_observation = {k: cols.get(k) for k in (
                "observed_utc", "reason", "sync_tool", "synchronized", "sync_source", "last_sync_utc", "os_offset_s",
                "ref_server", "ref_offset_s", "host_clock_drift_ppm")}
            log.info("clock observation (%s): synchronized=%s source=%s os offset=%s reference offset=%s "
                     "host drift=%s ppm", reason, osync.synchronized, osync.source, osync.offset_s,
                     cols.get("ref_offset_s"), cols["host_clock_drift_ppm"])
        except Exception:
            log.exception("could not record clock observation")

    def _janitor(self, now: float) -> None:
        assert self.ledger is not None and self.finalizer is not None and self.integrity is not None
        if not self.finalizer.alive and not self.stop_event.is_set():
            log.critical("finaliser thread died; restarting it")
            self.ledger.add_event("error", "recorder", "finalizer_restarted", "Verification worker restarted")
            self.finalizer.start()
        if not self.integrity.alive and not self.stop_event.is_set():
            log.critical("integrity worker died; restarting it")
            self.ledger.add_event("error", "integrity", "integrity_worker_restarted", "Integrity worker restarted")
            self.integrity.start()
        current = self.supervisor.live.run_id if self.supervisor and self.supervisor.live.ffmpeg_running else None
        for seg in self.ledger.unsealed_segments(exclude_run_id=current):
            if seg["finalize_attempts"] >= MAX_FINALIZE_ATTEMPTS or self.finalizer.is_queued(seg["segment_id"]):
                continue
            path = self.cfg.spool_path / seg["rel_path"]
            if path.exists() and time.time() - path.stat().st_mtime < 10.0:
                continue  # still changing: never seal a file that may be written
            kind = "closed" if seg["state"] == str(SegmentState.CLOSED) else "interrupted"
            log.warning("janitor: retrying unsealed segment %s (%s)", seg["rel_path"], kind)
            self.finalizer.submit(SegmentJob(seg["segment_id"], kind))
        if now >= self._next_checkpoint:
            self._next_checkpoint = now + 3600.0
            self.ledger.checkpoint()
        snap = self.cfg.ledger_snapshot_path
        if snap is not None and now >= self._next_snapshot:
            self._next_snapshot = now + LEDGER_SNAPSHOT_INTERVAL_S
            try:
                self.ledger.write_snapshot(snap)
            except Exception:
                log.exception("could not write the operator ledger snapshot")
        if now >= self._next_anchor:
            self._next_anchor = now + self.cfg.service.chain_anchor_interval_s
            self._anchor_chain("periodic")

    def _anchor_chain(self, reason: str) -> None:
        """Copy the recording-log chain head to the append-only anchor log (docs/07). Periodic
        anchors are skipped when nothing was logged since the last one."""
        try:
            assert self.ledger is not None
            head = current_chain_head(self.ledger)
            if head is None or (reason == "periodic" and head == self._last_anchor):
                return
            append_anchor(self.cfg.data_path / ANCHOR_LOG_NAME, head, reason, fsync_dir=self.platform.fsync_dir)
            self._last_anchor = head
        except Exception:
            log.exception("could not save the recording-log chain head")

    # ------------------------------------------------------------------ status
    def _write_status(self, force: bool = False, stopping: bool = False) -> None:
        now = self.clock.mono()
        if not force and now - self._last_status_mono < self.cfg.service.status_interval_s:
            return
        self._last_status_mono = now
        try:
            doc = self._build_status(now, stopping)
            write_status(self.status_path, doc)
            if doc.alert:
                self.platform.notify(f"STATUS={doc.alert['message']}")
            crit = {a["code"] for a in doc.alerts if a["severity"] >= Severity.CRITICAL}
            if crit - self._critical_codes:
                self._maybe_report(reason="critical alert: " + ", ".join(sorted(crit - self._critical_codes)))
            self._critical_codes = crit
        except Exception:
            log.exception("could not write status file")

    def _maybe_report(self, reason: str = "startup failure") -> None:
        def work() -> None:
            try:
                self.last_report = str(build_report(self.cfg, reason))
            except Exception:
                log.exception("could not build diagnostic report")
        threading.Thread(target=work, name="diagnostics", daemon=True).start()

    def _build_status(self, now: float, stopping: bool) -> StatusDocument:
        cfg, rec = self.cfg, self.cfg.recording
        sample = self.storage.sample() if self.storage else None
        sup, fin, integ = self.supervisor, self.finalizer, self.integrity
        errors = list(self._fatal)
        live_d: dict = {}
        ffmpeg_running = media_seen = in_backoff = False
        since_adv = last_media = None
        attempts, unstable, last_end = 0, 0, None
        clock_anomaly = now < self._clock_anomaly_until
        if sup is not None:
            with sup.live.lock:
                lv = sup.live
                ffmpeg_running, media_seen, in_backoff = lv.ffmpeg_running, lv.media_seen, lv.in_backoff
                since_adv = (now - lv.last_advance_mono) if lv.last_advance_mono is not None else None
                last_media = lv.last_media_mono_any
                attempts, last_end = lv.attempts, lv.last_end_reason
                unstable = sum(1 for t, media, _ in lv.recent_run_ends if now - t < 600 and media > 0)
                if abs(lv.clock_step_s) > rec.clock_step_tolerance_s or lv.clock_slew_run:
                    clock_anomaly = True
                live_d = {"run_id": lv.run_id, "run_started_utc": lv.run_launched_utc,
                          "stream_time_s": lv.last_out_time_s, "fps": lv.fps, "attempts": lv.attempts,
                          "last_end_reason": lv.last_end_reason, "clock_step_s": round(lv.clock_step_s, 3),
                          "stream_lag_s": round(lv.lag_s, 3), "internal_errors": lv.internal_errors,
                          "camera_drift_ppm": lv.camera_drift_ppm, "host_clock_drift_ppm": lv.host_clock_drift_ppm,
                          "time_correction_s": (round(lv.time_correction_s, 3)
                                                if lv.time_correction_s is not None else None),
                          "retry_in_s": (round(lv.next_attempt_mono - now, 1) if lv.next_attempt_mono else None)}

        since_seal = None
        verify_problem = verify_critical = corrupt_recent = False
        integrity_d: dict = {}
        if fin is not None:
            if fin.has_recent_errors():
                verify_problem = True
            if fin.last_good_seal_mono is not None:
                since_seal = now - fin.last_good_seal_mono
            corrupt_recent = fin.last_corrupt_mono is not None and now - fin.last_corrupt_mono < 3600
            integrity_d["verification_queue"] = fin.pending()
        if integ is not None:
            integrity_d["content_hash_backlog"] = integ.backlog()
            integrity_d["content_hashed_since_start"] = integ.hashed
            if any(now - t < 600 for t, _ in integ.recent_errors):
                verify_problem = True
        integrity_d["last_diagnostic_report"] = self.last_report

        last_seg = gap = None
        ledger_altered = ledger_unprotected = False
        ever_recorded = last_media is not None
        outage = None
        outage_since = None
        if self.ledger is not None:
            try:
                row = self.ledger.latest_sealed_segment(cfg.camera.id)
                if row is not None:
                    ever_recorded = True
                    last_seg = {"segment_id": row["segment_id"], "start_utc": row["start_utc"],
                                "end_utc": row["end_utc"], "state": row["state"]}
                g = self.ledger.open_gap(cfg.camera.id)
                if g is not None:
                    gap = {"since_utc": g["start_utc"], "reason": g["reason"], "attempts": g["attempts"]}
                    if g["start_utc"]:
                        gap["seconds"] = round(time.time() - parse_iso_utc(g["start_utc"]), 1)
                current = sup.live.run_id if sup is not None and ffmpeg_running else None
                stuck = self.ledger.unsealed_segments(exclude_run_id=current)
                overdue = [s for s in stuck if s["finalize_attempts"] >= MAX_FINALIZE_ATTEMPTS]
                old = [s for s in stuck if time.time() - parse_iso_utc(s["updated_utc"]) > rec.segment_seconds * 2 + 120]
                integrity_d["unsealed_segments"] = len(stuck)
                if overdue:
                    verify_critical = True
                elif old:
                    verify_problem = True
                if integ is not None and integ.outstanding_audit_failures():
                    verify_critical = True
                    integrity_d["audit_failures"] = integ.outstanding_audit_failures()
                # Chain head anchored in every status file (docs/07); verification result for support.
                head = self.ledger.chain_head()
                chain_d: dict = head.to_dict() if head is not None else {}
                rep = integ.chain_report if integ is not None else None
                chain_d["last_check_utc"] = rep.checked_utc if rep else None
                chain_d["last_check_ok"] = rep.ok if rep else None
                if rep is not None and rep.first is not None:
                    chain_d["first_problem_entry"] = rep.first.position
                    chain_d["problems"] = len(rep.findings)
                if rep is not None and rep.faults:
                    chain_d["protection_gaps"] = len(rep.faults)
                if self.ledger.chain_discrepancies or self.ledger.chain_failures:
                    chain_d["discrepancies_found_on_write"] = self.ledger.chain_discrepancies
                    chain_d["entries_not_written"] = self.ledger.chain_failures
                integrity_d["ledger_chain"] = chain_d
                # V-003: signs of alteration. V-004: entries could not be written (system fault).
                ledger_altered = bool((integ is not None and integ.chain_altered) or self.ledger.chain_discrepancies)
                ledger_unprotected = bool((integ is not None and integ.chain_unprotected)
                                          or self.ledger.chain_failures)
            except Exception as exc:  # ledger trouble is itself a system error
                log.exception("ledger query failed while building status")
                errors.append("D-001")
                integrity_d["ledger_error"] = str(exc)
        # Outage length: measured from when media last advanced; after a service restart the
        # ledger's open gap (wall clock) is the only memory of when the outage began.
        candidates = []
        if last_media is not None:
            candidates.append(now - last_media)
        if gap and gap.get("seconds") is not None:
            candidates.append(gap["seconds"])
        if not candidates:
            candidates.append(now - self._started_mono)
        outage = max(candidates)
        outage_since = gap["since_utc"] if gap and gap.get("since_utc") else iso_utc(time.time() - outage)

        m = Measurements(
            now_mono=now, service_started_mono=self._started_mono, ffmpeg_running=ffmpeg_running,
            media_seen_this_run=media_seen, seconds_since_media_advance=since_adv,
            seconds_since_last_seal=since_seal,
            storage_level=sample.level if sample else StorageLevel.OK,
            storage_error=sample.error if sample else None, component_errors=errors,
            in_backoff=in_backoff, stopping=stopping, outage_seconds=outage, outage_since_utc=outage_since,
            last_end_reason=last_end, ever_recorded=ever_recorded, attempts=attempts,
            unstable_restarts=unstable, clock_anomaly=clock_anomaly, clock_synchronized=self._ntp,
            corrupt_recent=corrupt_recent, verify_problem=verify_problem, verify_critical=verify_critical,
            ledger_altered=ledger_altered, ledger_unprotected=ledger_unprotected, simulated=cfg.camera.development_source)
        d = evaluate(m, rec.stall_timeout_s, rec.segment_seconds, rec.connect_timeout_s, rec.escalate_after_s)

        storage_d = {}
        if sample is not None:
            storage_d = {"level": str(sample.level), "free_bytes": sample.free_bytes,
                         "total_bytes": sample.total_bytes, "rate_bytes_per_s": sample.rate_bytes_per_s,
                         "seconds_until_attention": sample.seconds_until_critical}
        doc = StatusDocument(
            overall=str(d.overall), camera_link=str(d.link), messages=d.messages, camera_id=cfg.camera.id,
            generated_utc=iso_utc(time.time()), heartbeat_epoch=time.time(),
            service={"version": __version__, "platform": self.platform.name,
                     "uptime_s": round(now - self._started_mono, 1), "simulated_camera": cfg.camera.development_source,
                     "clock_synchronized": self._ntp, "clock_observation": self.last_clock_observation},
            recording=live_d, storage=storage_d, integrity=integrity_d, last_segment=last_seg, open_gap=gap,
            terminal=self._terminal)
        apply_alerts(doc, d.alerts)
        return doc

    # ------------------------------------------------------------------ watchdog (watchdog thread)
    def _on_watchdog_expired(self, age_s: float) -> None:
        """The recording loop is hung. Record what we can — each step bounded, because the hang
        may be holding a lock any of them needs — then end the process for a clean restart."""
        limit = self.cfg.service.watchdog_timeout_s
        msg = (f"WATCHDOG: recording loop has not run for {age_s:.0f} s (limit {limit:.0f} s); "
               f"exiting with code {EXIT_WATCHDOG} so the service manager restarts the recorder")
        _bounded("log", lambda: log.critical(msg), WATCHDOG_STEP_TIMEOUT_S)
        _bounded("threads", self._dump_threads, WATCHDOG_STEP_TIMEOUT_S)
        _bounded("status", lambda: self._write_watchdog_status(age_s), WATCHDOG_STEP_TIMEOUT_S)
        if self.ledger is not None:
            ledger = self.ledger
            _bounded("ledger", lambda: ledger.add_event(
                "error", "recorder", "watchdog_restart",
                "Recording loop stopped responding; recorder restarting",
                {"stalled_s": round(age_s, 1), "limit_s": limit, "exit_code": EXIT_WATCHDOG}),
                WATCHDOG_STEP_TIMEOUT_S)
        _bounded("ffmpeg", self._watchdog_stop_ffmpeg, self.cfg.recording.stop_grace_s + 2.0)
        self._watchdog_exit(EXIT_WATCHDOG)

    def _dump_threads(self) -> None:
        """All thread stacks to a file in the log directory (for support: where was it hung?)."""
        self.cfg.log_path.mkdir(parents=True, exist_ok=True)
        path = self.cfg.log_path / f"watchdog-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.txt"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"Sentinel {__version__} watchdog thread dump\n")
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)

    def _write_watchdog_status(self, age_s: float) -> None:
        # Built without the supervisor's locks or the ledger: either may be what is hung. Not
        # terminal: a restart is expected, so an old R-006 must turn into X-001.
        doc = fatal_status_document(
            "R-006", self.cfg.camera.id, terminal=False, detail={"watchdog_stalled_s": round(age_s, 1)},
            service={"version": __version__, "platform": self.platform.name,
                     "uptime_s": round(self.clock.mono() - self._started_mono, 1),
                     "simulated_camera": self.cfg.camera.development_source,
                     "clock_synchronized": self._ntp, "watchdog_exit": EXIT_WATCHDOG})
        write_status(self.status_path, doc)

    def _watchdog_stop_ffmpeg(self) -> None:
        """Ask FFmpeg to finish its current segment ('q') so the file ends cleanly; startup
        recovery seals it. If it does not exit in time it dies with the process anyway."""
        proc = self.supervisor.current_proc if self.supervisor is not None else None
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except (OSError, ValueError):
            return
        try:
            proc.wait(self.cfg.recording.stop_grace_s)
        except Exception:
            pass

    def request_shutdown(self) -> None:
        """Stop because the operating system is shutting down: same graceful FFmpeg stop, but the
        verification backlog is drained only within SHUTDOWN_BUDGET_S (rest: recovery at boot)."""
        self._system_shutdown = True
        self.request_stop()

    def request_stop(self, *_: object) -> None:
        log.info("stop requested")
        self.watchdog.disarm()   # shutdown is supervised by the service manager's stop timeout
        self.stop_event.set()


# ---------------------------------------------------------------------- CLI
def run_recorder(config_path: str | Path, before_run: Callable[[RecorderService], None] | None = None) -> int:
    """Load the configuration and run the recorder in this process until stopped.

    ``before_run`` lets a host (the launcher, a Windows service wrapper) attach an extra stop
    channel to the service before recording starts; it must not block."""
    config_path = Path(config_path).resolve()   # absolute: the process changes directory below
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(cfg.log_path, cfg.service.log_level)
    from .install_check import location_problems
    from .paths import safe_cwd

    problems = location_problems(Path(config_path), cfg)
    if problems:
        for p in problems:
            log.error("refusing to record: %s", p)
            print(f"configuration error: {p}", file=sys.stderr)
        return EXIT_CONFIG
    # All paths are absolute by now (config.py); run, and let FFmpeg run, from a folder that
    # standard users cannot write, so nothing is ever loaded from a user-controlled directory.
    os.chdir(safe_cwd())
    svc = RecorderService(cfg)
    signal.signal(signal.SIGINT, svc.request_stop)
    signal.signal(signal.SIGTERM, svc.request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, svc.request_stop)  # Windows CTRL_BREAK_EVENT
    if before_run is not None:
        before_run(svc)
    log.info("Sentinel recorder %s starting (camera %s, ffmpeg %s)", __version__, cfg.camera.id, cfg.tools.ffmpeg)
    return svc.run()


def _cmd_run(args: argparse.Namespace) -> int:
    return run_recorder(args.config)


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG
    problems = check_tools(cfg)
    parsed = parse_version(ffmpeg_version(cfg.tools.ffmpeg)) if not problems else None
    if parsed is not None and parsed < MIN_FFMPEG:
        problems.append(f"FFmpeg {parsed[0]}.{parsed[1]} is older than the minimum")
    from .install_check import location_problems

    problems += location_problems(Path(args.config), cfg)
    if cfg.camera.credentials_file:
        try:
            load_credentials(cfg.camera.credentials_file)
        except CredentialError as exc:
            problems.append(str(exc))
    print(f"camera {cfg.camera.id}: segments {cfg.recording.segment_seconds:g}s, audio "
          f"{'INCLUDED' if cfg.camera.include_audio else 'excluded'}, spool {cfg.storage.spool_dir}")
    if cfg.camera.development_source:
        print("WARNING: development_source = true (simulated/test camera; recordings are not evidence)")
    for p in problems:
        print(f"PROBLEM: {p}")
    print("configuration OK" if not problems else "configuration has problems")
    return EXIT_OK if not problems else EXIT_CONFIG


def banner(doc: dict | None, problem: str | None) -> str:
    """Large, unmissable text block for terminals and the support console."""
    alert = (doc or {}).get("alert")
    if problem:
        title, body = "RECORDER NOT RESPONDING", problem
    elif alert and alert.get("severity", 0) >= Severity.WARNING:
        title = "RECORDING STOPPED" if alert.get("recording_stopped") else "ATTENTION REQUIRED"
        body = alert["message"]
    else:
        title = {"RECORDING": "RECORDING NORMALLY", "STARTING": "STARTING"}.get(
            (doc or {}).get("overall", ""), str((doc or {}).get("overall", "UNKNOWN")))
        body = alert["message"] if alert else "No problems detected."
    width = 72
    lines = ["#" * width, f"#  {title}".ljust(width - 1) + "#", "#" + " " * (width - 2) + "#"]
    for ln in textwrap.wrap(body, width - 6):
        lines.append(f"#  {ln}".ljust(width - 1) + "#")
    lines.append("#" * width)
    return "\n".join(lines)


def _cmd_status(args: argparse.Namespace) -> int:
    from .status import read_status

    cfg = load_config(args.config)
    doc, problem = read_status(cfg.status_path)
    print(banner(doc, problem))
    if args.json and doc:
        print(json.dumps(doc, indent=2))
    healthy = doc is not None and not problem and doc.get("overall") in ("RECORDING", "STORAGE_WARNING")
    return EXIT_OK if healthy else EXIT_RUNTIME


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    path = build_report(cfg, "requested by administrator", Path(args.out) if args.out else None)
    print(f"Diagnostic report: {path}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sentinel-recorder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "check-config", "status", "diagnostics"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name == "status":
            p.add_argument("--json", action="store_true", help="also print the full status document")
        if name == "diagnostics":
            p.add_argument("--out", help="directory for the report (default: data_dir/diagnostics)")
    args = ap.parse_args(argv)
    return {"run": _cmd_run, "check-config": _cmd_check, "status": _cmd_status,
            "diagnostics": _cmd_diagnostics}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
