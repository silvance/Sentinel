"""Measured system status and operator alerts.

The overall status is derived only from measurements: stream time advancing, segments being
sealed, free space, and component health. "FFmpeg process exists" is never sufficient for
RECORDING. Readers must also check the heartbeat age (:func:`read_status`); a stale heartbeat
is itself alert X-001.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .alerts import Alert, Severity, make_alert, most_severe, outage_code
from .models import CameraLink, OverallStatus, StorageLevel

STATUS_SCHEMA = 2


@dataclass
class Measurements:
    """Inputs to the status decision, all measured by the recorder."""
    now_mono: float
    service_started_mono: float
    ffmpeg_running: bool
    media_seen_this_run: bool
    seconds_since_media_advance: float | None   # None if no media this run
    seconds_since_last_seal: float | None       # last *good* seal; None if none since service start
    storage_level: StorageLevel
    storage_error: str | None
    component_errors: list[str]                 # alert codes, e.g. ["C-001"], ["D-001"]
    in_backoff: bool
    stopping: bool = False
    outage_seconds: float | None = None         # time since media last advanced (or since start)
    outage_since_utc: str | None = None
    last_end_reason: str | None = None
    ever_recorded: bool = False
    attempts: int = 0
    unstable_restarts: int = 0                  # runs that recorded media and then ended, last 10 min
    clock_anomaly: bool = False
    clock_synchronized: bool | None = None
    corrupt_recent: bool = False
    verify_problem: bool = False                # sealing/fingerprinting errors, or segments waiting too long
    verify_critical: bool = False               # sealing given up after retries, or audit found a change
    ledger_altered: bool = False                # recording-log hash chain check failed (ledgerchain)
    ledger_unprotected: bool = False            # chain entries could not be written (system fault)
    simulated: bool = False
    scheduled_pause: bool = False               # the recording schedule says pause (planned; P-001)
    resumed_s: float | None = None              # seconds since a scheduled pause ended (reconnect grace)


@dataclass
class Decision:
    overall: OverallStatus
    link: CameraLink
    messages: list[str]
    alerts: list[Alert]


def evaluate(m: Measurements, stall_timeout_s: float, segment_seconds: float, connect_timeout_s: float,
             escalate_after_s: float = 180.0) -> Decision:
    if m.stopping:
        return Decision(OverallStatus.STOPPED, CameraLink.DISCONNECTED, ["Recording service stopped."], [])
    alerts: list[Alert] = []

    flowing = (m.ffmpeg_running and m.media_seen_this_run and m.seconds_since_media_advance is not None
               and m.seconds_since_media_advance <= stall_timeout_s)
    # A good sealed segment is expected at least once per segment period (+ validation slack).
    seal_deadline = segment_seconds * 1.5 + 60.0
    sealing_ok = m.seconds_since_last_seal is None or m.seconds_since_last_seal <= seal_deadline
    if m.seconds_since_last_seal is None and (m.now_mono - m.service_started_mono) > seal_deadline:
        sealing_ok = False

    if flowing:
        link = CameraLink.CONNECTED
    elif (m.ffmpeg_running and not m.media_seen_this_run
          and (m.outage_seconds is None or m.outage_seconds < 2 * connect_timeout_s)):
        link = CameraLink.CONNECTING      # only early in an outage; days of retries read "disconnected"
    else:
        link = CameraLink.DISCONNECTED

    for code in m.component_errors:
        alerts.append(make_alert(code, Severity.CRITICAL))
    if m.storage_error:
        alerts.append(make_alert("S-003", Severity.CRITICAL, detail={"error": m.storage_error}))
    elif m.storage_level is StorageLevel.CRITICAL:
        alerts.append(make_alert("S-002", Severity.CRITICAL))
    elif m.storage_level is StorageLevel.WARNING:
        alerts.append(make_alert("S-001", Severity.WARNING))
    if m.verify_critical:
        alerts.append(make_alert("V-001", Severity.CRITICAL))
    elif m.verify_problem:
        alerts.append(make_alert("V-001", Severity.WARNING))
    if m.corrupt_recent:
        alerts.append(make_alert("V-002", Severity.WARNING))
    if m.ledger_altered:
        alerts.append(make_alert("V-003", Severity.CRITICAL))
    elif m.ledger_unprotected:
        alerts.append(make_alert("V-004", Severity.WARNING))
    if m.clock_anomaly:
        alerts.append(make_alert("T-001", Severity.WARNING))
    if m.clock_synchronized is False:
        alerts.append(make_alert("T-002", Severity.WARNING))
    if m.unstable_restarts >= 5:
        alerts.append(make_alert("R-004", Severity.WARNING, detail={"restarts_10min": m.unstable_restarts}))
    if m.simulated:
        # First among equal-severity alerts, so the single-alert banner shows "not evidence"
        # rather than another warning (e.g. T-002 on a host whose time service is stopped).
        alerts.insert(0, make_alert("Z-001", Severity.WARNING))

    starting = False
    paused = False
    fatal = bool(m.component_errors) or bool(m.storage_error)
    if flowing and not sealing_ok:
        alerts.append(make_alert("R-005", Severity.CRITICAL))
    elif m.scheduled_pause and not flowing and not fatal:
        # Planned: no outage alert and no escalation. The recorder fills in the resume time.
        paused = True
        alerts.append(make_alert("P-001", Severity.NOTICE))
    elif not flowing and not fatal and m.storage_level is not StorageLevel.CRITICAL:
        in_grace = ((m.now_mono - m.service_started_mono) < connect_timeout_s and not m.in_backoff
                    and m.attempts <= 1)
        # Reconnecting after a scheduled pause gets the same grace as a fresh start.
        in_grace = in_grace or (m.resumed_s is not None and m.resumed_s < connect_timeout_s and not m.in_backoff)
        if in_grace:
            starting = True
        else:
            escalated = (m.outage_seconds or 0.0) >= escalate_after_s
            alerts.append(make_alert(outage_code(m.last_end_reason, m.ever_recorded),
                                     Severity.CRITICAL if escalated else Severity.NOTICE,
                                     recovering=True, attempt=m.attempts, since_utc=m.outage_since_utc,
                                     detail={"outage_s": round(m.outage_seconds or 0.0, 1),
                                             "last_end_reason": m.last_end_reason}))

    if fatal:
        overall = OverallStatus.ERROR
    elif m.storage_level is StorageLevel.CRITICAL and not paused:
        overall = OverallStatus.STORAGE_FULL
    elif paused:
        overall = OverallStatus.PAUSED
    elif flowing and sealing_ok:
        overall = OverallStatus.STORAGE_WARNING if m.storage_level is StorageLevel.WARNING else OverallStatus.RECORDING
    elif flowing:
        overall = OverallStatus.ERROR
    elif starting:
        overall = OverallStatus.STARTING
    else:
        overall = OverallStatus.INTERRUPTED

    alerts.sort(key=lambda a: -a.severity)
    msgs = [a.message for a in alerts]
    if starting:
        msgs.append("Connecting to camera…")
    return Decision(overall, link, msgs, alerts)


def decide(m: Measurements, stall_timeout_s: float, segment_seconds: float,
           connect_timeout_s: float) -> tuple[OverallStatus, CameraLink, list[str]]:
    d = evaluate(m, stall_timeout_s, segment_seconds, connect_timeout_s)
    return d.overall, d.link, d.messages


@dataclass
class StatusDocument:
    overall: str
    camera_link: str
    messages: list[str]
    camera_id: str
    generated_utc: str
    heartbeat_epoch: float
    alert: dict[str, Any] | None = None          # the most severe alert (what the operator must see)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    banner: bool = False                         # True => show full-width, high-contrast banner
    service: dict[str, Any] = field(default_factory=dict)
    recording: dict[str, Any] = field(default_factory=dict)
    storage: dict[str, Any] = field(default_factory=dict)
    integrity: dict[str, Any] = field(default_factory=dict)
    last_segment: dict[str, Any] | None = None
    open_gap: dict[str, Any] | None = None
    schema: int = STATUS_SCHEMA
    # True only for the last document a process writes when it cannot record at all (e.g. C-001
    # at startup). Readers then show its alert even when the heartbeat is old, instead of the
    # generic X-001 "not responding": the recorder *did* respond, with a reason.
    terminal: bool = False
    archive: dict[str, Any] = field(default_factory=dict)  # archive-drive copy (sentinel.archive); empty if disabled
    retention: dict[str, Any] = field(default_factory=dict)  # local retention (sentinel.retention): oldest kept, last pass
    schedule: dict[str, Any] = field(default_factory=dict)   # recording schedule: enabled, paused, resume time
    power: dict[str, Any] = field(default_factory=dict)      # keep-awake and power-plan check (sentinel.power)


def fatal_status_document(code: str, camera_id: str, service: dict[str, Any] | None = None,
                          detail: dict[str, Any] | None = None, terminal: bool = True) -> StatusDocument:
    """A status built without the ledger or any recorder state (startup failure, watchdog)."""
    from .clock import iso_utc

    alert = make_alert(code, Severity.CRITICAL, detail=detail)
    now = time.time()
    doc = StatusDocument(overall=str(OverallStatus.ERROR), camera_link=str(CameraLink.DISCONNECTED),
                         messages=[alert.message], camera_id=camera_id, generated_utc=iso_utc(now),
                         heartbeat_epoch=now, service=dict(service or {}), terminal=terminal)
    apply_alerts(doc, [alert])
    return doc


def apply_alerts(doc: StatusDocument, alerts: list[Alert]) -> None:
    top = most_severe(alerts)
    doc.alerts = [a.to_dict() for a in alerts]
    doc.alert = top.to_dict() if top else None
    doc.banner = bool(top and top.severity >= Severity.WARNING)


def write_status(path: Path, doc: StatusDocument) -> None:
    """Atomic replace so readers never see a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(asdict(doc), indent=2, sort_keys=True).encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has the file open; retry briefly
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def read_status(path: Path, max_age_s: float = 15.0) -> tuple[dict[str, Any] | None, str | None]:
    """Return (document, problem). A missing, unreadable or stale status is alert X-001."""
    x001 = make_alert("X-001", Severity.CRITICAL).message
    try:
        doc = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None, f"{x001} (The recording service is not running.)"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{x001} (Status unreadable: {exc})"
    age = time.time() - float(doc.get("heartbeat_epoch", 0))
    if age > max_age_s and not (doc.get("terminal") and doc.get("alert")):
        return doc, f"{x001} (Last update {int(age)} s ago.)"
    return doc, None
