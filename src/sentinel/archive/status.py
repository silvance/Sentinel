"""Archive status for operators: ``<data_dir>/archive_status.json`` and A-0xx alerts.

Written atomically by the archive process. The recorder service reads it (see
:func:`recorder_view`) and shows the archive alerts in its own status, so the operator sees
one status. Fields: ``state``, ``drive`` (present | absent | full | unusable), ``message``
(plain language), ``backlog`` (``seconds``, ``segments``, ``bytes``, ``oldest_start_utc``,
``text`` = "Not yet archived: N h"), ``alerts`` (A-0xx), ``heartbeat_epoch``,
``throughput`` (last file), ``priority`` (what lowering was applied), ``drives`` (every archive drive
inserted: label, use = in_use | full | standby | take_out, free_bytes), ``removal`` (the drive prepared
for removal, or null).

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

# What the window shows for each state (status bar), instead of the machine token.
STATE_WORDS = {ST_STARTING: "starting", ST_ABSENT: "no drive inserted", ST_UNUSABLE: "drive cannot be used",
               ST_COPYING: "copying", ST_UP_TO_DATE: "up to date", ST_FULL: "drive full, swap it",
               ST_SWAP_PENDING: "preparing for removal", ST_SAFE: "safe to remove",
               ST_REMOVE_MANUALLY: "remove with Windows", ST_ATTENTION: "needs attention", ST_STOPPED: "stopped"}


def state_words(state: str | None) -> str:
    return STATE_WORDS.get(state or "", (state or "").replace("_", " ").lower())


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
    capacity_low: bool = False              # attached drive holds less than N days at the measured rate


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
    if c.capacity_low and not c.full:
        alerts.append(make_alert("A-009", Severity.WARNING))
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


HEARTBEAT_STALE_S = 120.0
COPIER_NOT_RUNNING = (
    "Archive copying is not running, so nothing is being copied to the archive drive and there is nothing to "
    "finish. To take the drive out now: select it in the list and press Safely remove… (or use Windows' Safely "
    "Remove Hardware). Recordings stay on this computer and are copied when archive copying runs again (alert "
    "A-007). If A-007 stays, contact technical support.")


def copier_running(doc: dict, now: float | None = None) -> bool:
    """The archive copier publishes its status at least every poll interval; a fresh heartbeat and a
    state other than STOPPED means it runs (and will answer a request)."""
    if not doc or doc.get("state") == ST_STOPPED:
        return False
    try:
        beat = float(doc.get("heartbeat_epoch") or 0)
    except (TypeError, ValueError):
        return False
    return (time.time() if now is None else now) - beat <= HEARTBEAT_STALE_S


def read_archive_status(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def _merge(alerts: list[Alert]) -> list[Alert]:
    """One alert per code (the most severe), most severe first."""
    best: dict[str, Alert] = {}
    for a in alerts:
        if a.code not in best or a.severity > best[a.code].severity:
            best[a.code] = a
    return sorted(best.values(), key=lambda a: -a.severity)


class RecorderArchiveView:
    """For the recorder's ``status.json`` (runs inside the recorder process): the archive section and
    its alerts. It does NOT depend on the archive process being alive:

    * the "not yet archived" backlog is computed from the ledger itself (cached ``backlog_every_s``),
      so A-004 still fires when the archive process is dead;
    * open archive problems (A-006) are read from the ledger, so they persist until acknowledged;
    * a missing or stale ``archive_status.json`` is A-007, WARNING at first and CRITICAL once it has
      lasted ``critical_after_s`` (default 30 min)."""

    def __init__(self, data_path: Path, camera_id: str, poll_interval_s: float, threshold_s: float,
                 critical_after_s: float, backlog_every_s: float = 60.0) -> None:
        self.data_path = data_path
        self.camera_id = camera_id
        self.max_age = max(60.0, 6 * poll_interval_s)
        self.threshold_s = threshold_s
        self.critical_after_s = critical_after_s
        self.backlog_every_s = backlog_every_s
        self._down_since: float | None = None
        self._cache: tuple[float, float, int, str | None, int] | None = None   # (t, seconds, segments, oldest, problems)

    def _from_ledger(self, ledger: Any, now: float) -> tuple[float, int, str | None, int]:
        if self._cache is None or now - self._cache[0] >= self.backlog_every_s:
            from .ledger import ArchiveLedger

            al = ArchiveLedger(ledger, self.camera_id)
            b = al.backlog(None)
            self._cache = (now, b.seconds, b.segments, b.oldest_start_utc, len(al.open_problems()))
        return self._cache[1], self._cache[2], self._cache[3], self._cache[4]

    def evaluate(self, ledger: Any, now: float | None = None) -> tuple[dict[str, Any], list[Alert]]:
        now = time.time() if now is None else now
        alerts: list[Alert] = []
        seconds, segments, oldest, problems = self._from_ledger(ledger, now) if ledger is not None else (0.0, 0, None, 0)
        if seconds >= self.threshold_s:
            alerts.append(make_alert("A-004", Severity.CRITICAL, detail={"backlog_s": round(seconds, 1)}))
        if problems:
            alerts.append(make_alert("A-006", Severity.CRITICAL, detail={"open_problems": problems}))
        backlog = {"text": backlog_text(seconds), "seconds": round(seconds, 1), "segments": segments,
                   "oldest_start_utc": oldest, "source": "ledger"}
        doc = read_archive_status(self.data_path / STATUS_FILE)
        age = None if doc is None else now - float(doc.get("heartbeat_epoch", 0))
        if doc is None or age is None or age > self.max_age:
            if self._down_since is None:
                self._down_since = now - (age if age is not None and age > self.max_age else 0.0)
            down = now - self._down_since
            sev = Severity.CRITICAL if down >= self.critical_after_s else Severity.WARNING
            alerts.append(make_alert("A-007", sev, detail={"status_age_s": None if age is None else round(age, 1),
                                                           "down_s": round(down, 1)}))
            view = {"running": False, "state": None if doc is None else doc.get("state"),
                    "status_age_s": None if age is None else round(age, 1), "backlog": backlog,
                    "message": f"Archive copying is not running. {backlog['text']}."}
            return view, _merge(alerts)
        self._down_since = None
        for a in doc.get("alerts") or []:
            try:
                alerts.append(make_alert(a["code"], Severity(int(a["severity"])), detail=a.get("detail") or {}))
            except (KeyError, ValueError):
                continue
        view = {"running": True, "state": doc.get("state"), "drive": doc.get("drive"), "message": doc.get("message"),
                "backlog": doc.get("backlog") or backlog, "drive_label": doc.get("drive_label"),
                "drives": doc.get("drives") or [], "removal": doc.get("removal")}
        return view, _merge(alerts)