"""Archive status for operators: ``<data_dir>/archive_status.json`` and A-0xx alerts.

Written atomically by the archive process. The recorder service reads it (see
:func:`recorder_view`) and shows the archive alerts in its own status, so the operator sees
one status. Fields: ``state``, ``drive`` (present | absent | full | unusable), ``message``
(plain language), ``backlog`` (``seconds``, ``segments``, ``bytes``, ``oldest_start_utc``,
``text`` = "Not yet archived: N h"), ``alerts`` (A-0xx), ``heartbeat_epoch``,
``throughput`` (last file), ``priority`` (what lowering was applied).

States: STARTING, ABSENT, UNUSABLE, COPYING, UP_TO_DATE, FULL, SWAP_PENDING, SAFE_TO_REMOVE,
REMOVE_MANUALLY, ATTENTION, STOPPED.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..alerts import Alert, Severity, make_alert
from ..clock import iso_utc

STATUS_FILE = "archive_status.json"
SCHEMA = 1

ST_STARTING, ST_ABSENT, ST_UNUSABLE, ST_COPYING, ST_UP_TO_DATE = "STARTING", "ABSENT", "UNUSABLE", "COPYING", "UP_TO_DATE"
ST_FULL, ST_SWAP_PENDING, ST_SAFE, ST_REMOVE_MANUALLY = "FULL", "SWAP_PENDING", "SAFE_TO_REMOVE", "REMOVE_MANUALLY"
ST_ATTENTION, ST_STOPPED = "ATTENTION", "STOPPED"


def backlog_text(seconds: float) -> str:
    """'Not yet archived: 0 h' / '0.4 h' / '26 h' (recorded footage not yet on an archive drive)."""
    if seconds < 1:
        return "Not yet archived: 0 h"
    h = seconds / 3600.0
    return f"Not yet archived: {h:.1f} h" if h < 10 else f"Not yet archived: {h:.0f} h"


@dataclass
class Condition:
    """What the worker knows; turned into alerts by :func:`evaluate_alerts`."""
    state: str
    backlog_s: float
    threshold_s: float
    removed_unexpectedly: bool = False
    after_swap: bool = False                # drive taken out after a proper swap
    full: bool = False
    unusable: bool = False
    verify_failed: bool = False
    source_unreadable: bool = False
    software_errors: int = 0


def evaluate_alerts(c: Condition) -> list[Alert]:
    alerts: list[Alert] = []
    over = c.backlog_s >= c.threshold_s
    if over:
        alerts.append(make_alert("A-004", Severity.CRITICAL, detail={"backlog_s": round(c.backlog_s, 1)}))
    if c.state == ST_ABSENT:
        if c.removed_unexpectedly:
            alerts.append(make_alert("A-003", Severity.WARNING))
        else:
            alerts.append(make_alert("A-001", Severity.NOTICE if c.after_swap else Severity.WARNING))
    if c.full:
        alerts.append(make_alert("A-002", Severity.CRITICAL if over else Severity.WARNING))
    if c.unusable:
        alerts.append(make_alert("A-005", Severity.WARNING))
    if c.verify_failed:
        alerts.append(make_alert("A-006", Severity.CRITICAL))
    if c.source_unreadable:
        alerts.append(make_alert("A-008", Severity.CRITICAL))
    if c.software_errors >= 3:
        alerts.append(make_alert("A-007", Severity.WARNING, detail={"consecutive_errors": c.software_errors}))
    alerts.sort(key=lambda a: -a.severity)
    return alerts


class ArchiveStatusWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.doc: dict[str, Any] = {}

    def write(self, **fields: Any) -> dict[str, Any]:
        now = time.time()
        doc = dict(self.doc)
        doc.update(fields)
        doc.update(schema=SCHEMA, updated_utc=iso_utc(now), heartbeat_epoch=now, pid=os.getpid())
        self.doc = doc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, self.path)
                break
            except PermissionError:  # Windows: a reader has the file open
                if attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
        return doc


def read_archive_status(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def recorder_view(data_path: Path, poll_interval_s: float, now: float | None = None) -> tuple[dict[str, Any], list[Alert]]:
    """For the recorder's status.json: the archive section and its alerts. A missing or stale
    archive status (archive process not running / hung) is alert A-007."""
    now = time.time() if now is None else now
    doc = read_archive_status(data_path / STATUS_FILE)
    max_age = max(60.0, 6 * poll_interval_s)
    if doc is None or now - float(doc.get("heartbeat_epoch", 0)) > max_age:
        age = None if doc is None else round(now - float(doc.get("heartbeat_epoch", 0)), 1)
        view = {"running": False, "state": None if doc is None else doc.get("state"), "status_age_s": age,
                "backlog": (doc or {}).get("backlog"),
                "message": "Archive copying is not running."}
        return view, [make_alert("A-007", Severity.WARNING, detail={"status_age_s": age})]
    alerts = []
    for a in doc.get("alerts") or []:
        try:
            alerts.append(make_alert(a["code"], Severity(int(a["severity"])), detail=a.get("detail") or {}))
        except (KeyError, ValueError):
            continue
    view = {"running": True, "state": doc.get("state"), "drive": doc.get("drive"), "message": doc.get("message"),
            "backlog": doc.get("backlog"), "drive_label": doc.get("drive_label")}
    return view, alerts
