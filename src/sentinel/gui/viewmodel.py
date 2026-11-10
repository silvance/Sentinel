"""Operator view model: turns measured state into the words and tones the GUI shows.

Qt-free so every mapping is unit-testable. Inputs are only:
* the status document returned by :func:`sentinel.status.read_status` (plus its problem string),
* a :class:`~sentinel.gui.ledger_view.LedgerSnapshot` read through a read-only connection.

Rules (requirements U1, U7):
* the alert text shown is exactly ``alert.message`` from the recorder; it is never rewritten;
* a missing, unreadable or stale status is alert X-001, shown as a critical banner;
* nothing derived here contains paths, commands, network addresses or codec terms. Text that
  could contain them (``problem`` strings, ``detail`` dictionaries, end reasons) is never shown
  to operators; it is available on the administrator page only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..alerts import Severity, make_alert
from ..clock import parse_iso_utc
from ..models import CameraLink, OverallStatus
from ..status import read_status
from . import textfmt as tf


class Tone(StrEnum):
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    NEUTRAL = "neutral"


class ReadKind(StrEnum):
    OK = "ok"
    MISSING = "missing"          # no status file at all
    UNREADABLE = "unreadable"    # file exists but could not be parsed (after retries)
    STALE = "stale"              # heartbeat too old


@dataclass(frozen=True)
class StatusRead:
    doc: dict[str, Any] | None
    problem: str | None          # raw text from read_status; ADMINISTRATOR ONLY (may contain paths)
    kind: ReadKind
    age_s: float | None          # heartbeat age when a document was read


def read_status_robust(path: Path, max_age_s: float = 15.0, attempts: int = 5,
                       retry_sleep_s: float = 0.02) -> StatusRead:
    """Read status.json via :func:`read_status`, tolerating the file being replaced mid-read.

    The recorder replaces the file atomically, but on Windows a read can still fail transiently
    (sharing violation) while ``os.replace`` runs. A failed read is retried a few times before it
    is reported as X-001, so a single unlucky refresh does not flash a false alarm.
    """
    doc: dict[str, Any] | None = None
    problem: str | None = None
    for i in range(max(1, attempts)):
        doc, problem = read_status(path, max_age_s=max_age_s)
        if doc is not None or not path.exists():
            break
        if i + 1 < attempts:
            time.sleep(retry_sleep_s * (i + 1))
    if doc is None:
        return StatusRead(None, problem, ReadKind.MISSING if not path.exists() else ReadKind.UNREADABLE, None)
    try:
        age = time.time() - float(doc.get("heartbeat_epoch", 0))
    except (TypeError, ValueError):
        age = None
    if problem:
        return StatusRead(doc, problem, ReadKind.STALE, age)
    return StatusRead(doc, None, ReadKind.OK, age)


# --------------------------------------------------------------------------- view dataclasses
@dataclass(frozen=True)
class AlertView:
    code: str
    message: str                 # exactly alert.message
    severity: int
    tone: Tone
    label: str                   # words that accompany the icon, e.g. "Recording stopped"
    recording_stopped: bool
    offer_report: bool           # show "Save diagnostic report"

    @property
    def code_text(self) -> str:
        return f"Error {self.code}"


@dataclass(frozen=True)
class TileView:
    title: str
    value: str
    tone: Tone
    detail: str = ""
    detail2: str = ""


@dataclass(frozen=True)
class DashboardView:
    headline: str
    supporting: str
    tone: Tone
    banner: AlertView | None             # full-width high-contrast banner
    banner_takes_over: bool              # critical: the banner replaces the status area
    notice: AlertView | None             # below-banner alert (severity NOTICE), shown in the status area
    other_alerts: tuple[AlertView, ...]
    test_camera: AlertView | None        # Z-001 anywhere in the alert list
    stale: bool
    camera: TileView
    storage: TileView
    drive: TileView
    updated: str
    live: bool                           # fresh status that says footage is being recorded now
    raw_overall: str | None = None
    detail: str = ""                     # measured context, e.g. "No recording since 22:56 (03:56 UTC) — 7 min."


# --------------------------------------------------------------------------- mappings
HEADLINES: dict[str, tuple[str, Tone]] = {
    str(OverallStatus.RECORDING): ("Recording normally", Tone.OK),
    str(OverallStatus.STARTING): ("Starting recording", Tone.INFO),
    str(OverallStatus.INTERRUPTED): ("Recording interrupted", Tone.WARNING),
    str(OverallStatus.STORAGE_WARNING): ("Storage warning", Tone.WARNING),
    str(OverallStatus.STORAGE_FULL): ("Recording stopped", Tone.CRITICAL),
    str(OverallStatus.ERROR): ("System error", Tone.CRITICAL),
    str(OverallStatus.STOPPED): ("Not recording", Tone.CRITICAL),
}
UNKNOWN_HEADLINE = ("System error", Tone.CRITICAL)
RECORDING_STATES = {str(OverallStatus.RECORDING), str(OverallStatus.STORAGE_WARNING)}


def severity_tone(severity: int) -> Tone:
    if severity >= Severity.CRITICAL:
        return Tone.CRITICAL
    if severity >= Severity.WARNING:
        return Tone.WARNING
    return Tone.INFO


def alert_label(code: str, severity: int, recording_stopped: bool) -> str:
    if code == "Z-001":
        return "Test system — not evidence"
    if severity >= Severity.CRITICAL:
        return "Recording stopped" if recording_stopped else "Action required"
    if severity >= Severity.WARNING:
        return "Attention needed"
    return "Recovering automatically" if recording_stopped else "Notice"


def alert_view(a: dict[str, Any]) -> AlertView | None:
    code, message = a.get("code"), a.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        return None
    try:
        sev = int(a.get("severity", 0))
    except (TypeError, ValueError):
        sev = 0
    stopped = bool(a.get("recording_stopped"))
    return AlertView(code=code, message=message, severity=sev, tone=severity_tone(sev),
                     label=alert_label(code, sev, stopped), recording_stopped=stopped,
                     offer_report=sev >= Severity.WARNING and code != "Z-001")


def x001_view() -> AlertView:
    a = make_alert("X-001", Severity.CRITICAL)
    return AlertView(code=a.code, message=a.message, severity=a.severity, tone=Tone.CRITICAL,
                     label="Recorder not responding", recording_stopped=False, offer_report=True)


def _epoch(iso: Any) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        return parse_iso_utc(iso)
    except ValueError:
        return None


def _stale_detail(read: StatusRead, tz: ZoneInfo, now: float) -> str:
    if read.kind is ReadKind.MISSING:
        return "No status information has been received from the recording service."
    if read.kind is ReadKind.UNREADABLE:
        return "The recorder's status information could not be read."
    doc = read.doc or {}
    hb = doc.get("heartbeat_epoch")
    try:
        hb_f = float(hb)
    except (TypeError, ValueError):
        return "The recorder has stopped reporting its status."
    if doc.get("overall") == str(OverallStatus.STOPPED):
        return f"The recording service was shut down at {tf.when(hb_f, tz, now)}."
    return f"Last status update {tf.duration(now - hb_f)} ago, at {tf.when(hb_f, tz, now)}."


def _camera_tile(doc: dict[str, Any] | None, stale: bool, top: AlertView | None, last_saved: float | None,
                 tz: ZoneInfo, now: float) -> TileView:
    if last_saved is None and doc:
        last_saved = _epoch((doc.get("last_segment") or {}).get("end_utc"))
    saved = (f"Last recording saved {tf.when(last_saved, tz, now, seconds=True)} "
             f"({tf.hm_utc(last_saved, seconds=True)})") if last_saved else "No recording saved yet"
    if stale or not doc:
        return TileView("Camera", "Unknown", Tone.NEUTRAL,
                        "Not known while the recorder is not responding.", saved)
    link = doc.get("camera_link")
    if link == str(CameraLink.CONNECTED):
        return TileView("Camera", "Connected", Tone.OK, saved)
    if link == str(CameraLink.CONNECTING):
        return TileView("Camera", "Connecting", Tone.INFO, saved)
    overall = doc.get("overall")
    if overall == str(OverallStatus.STOPPED):
        return TileView("Camera", "Not in use", Tone.NEUTRAL, "The recording service is shut down.", saved)
    # When recording is stopped for a reason that is not the camera, "disconnected" would blame the
    # camera without evidence: the recorder simply is not reading from it.
    if overall == str(OverallStatus.STORAGE_FULL):
        return TileView("Camera", "Not in use", Tone.NEUTRAL, "Recording is stopped because storage is full.", saved)
    if overall == str(OverallStatus.ERROR):
        return TileView("Camera", "Not in use", Tone.NEUTRAL, "Recording is stopped by a system error.", saved)
    tone = Tone.CRITICAL if top and top.severity >= Severity.CRITICAL else Tone.WARNING
    return TileView("Camera", "Disconnected", tone, saved)


def _storage_tile(doc: dict[str, Any] | None, stale: bool) -> TileView:
    st = (doc or {}).get("storage") or {}
    free = st.get("free_bytes")
    level = st.get("level")
    tone = {"ok": Tone.OK, "warning": Tone.WARNING, "critical": Tone.CRITICAL}.get(level, Tone.NEUTRAL)
    if not isinstance(free, (int, float)):
        return TileView("Recording storage", "Not measured yet", Tone.NEUTRAL,
                        "Free space has not been reported." if not stale else
                        "Not known while the recorder is not responding.")
    value = f"{tf.size(free)} free"
    secs = st.get("seconds_until_attention")
    if level == "critical":
        detail = "Full — no space left for new recordings."
    elif isinstance(secs, (int, float)):
        detail = f"Space for {tf.approx_remaining(secs)} of recording."
    else:
        detail = "Recording time left is still being measured."
    if stale:
        return TileView("Recording storage", value, Tone.NEUTRAL, "Last known value. " + detail)
    return TileView("Recording storage", value, tone, detail)


def drive_tile(export_available: bool) -> TileView:
    if not export_available:
        return TileView("Review drive", "Not available", Tone.NEUTRAL,
                        "The review-drive feature is not installed in this version.")
    return TileView("Review drive", "Ready to use", Tone.NEUTRAL,
                    "Press Prepare Review Drive to check and prepare a drive.")


def build_dashboard(read: StatusRead, *, tz: ZoneInfo, now: float | None = None,
                    last_saved_epoch: float | None = None, recording_since_epoch: float | None = None,
                    export_available: bool = False) -> DashboardView:
    now = time.time() if now is None else now
    doc = read.doc
    stale = read.kind is not ReadKind.OK
    alerts = [v for v in (alert_view(a) for a in ((doc or {}).get("alerts") or [])) if v is not None]
    top = alert_view((doc or {}).get("alert") or {}) if doc else None
    test_cam = next((a for a in alerts if a.code == "Z-001"), None)
    if top is not None and top.code == "Z-001":
        test_cam = top

    if stale:
        banner = x001_view()
        cam = _camera_tile(doc, True, banner, last_saved_epoch, tz, now)
        sto = _storage_tile(doc, True)
        # Alerts inside a stale document are no longer current and are not shown, except the
        # test-camera warning, which comes from the configuration and is still true.
        return DashboardView(headline="System error", supporting="", tone=Tone.CRITICAL, banner=banner,
                             banner_takes_over=True, notice=None, other_alerts=(), test_camera=test_cam,
                             stale=True, camera=cam, storage=sto, drive=drive_tile(export_available),
                             updated="Status not updating", live=False, raw_overall=(doc or {}).get("overall"),
                             detail=_stale_detail(read, tz, now))

    assert doc is not None
    overall = doc.get("overall")
    headline, tone = HEADLINES.get(overall if isinstance(overall, str) else "", UNKNOWN_HEADLINE)
    if top is not None and top.recording_stopped and top.severity >= Severity.CRITICAL:
        headline, tone = "Recording stopped", Tone.CRITICAL

    banner = top if (doc.get("banner") and top is not None) else None
    notice = top if (top is not None and banner is None) else None
    takes_over = banner is not None and banner.severity >= Severity.CRITICAL
    others = tuple(a for a in alerts if a.code != "Z-001"
                   and (top is None or a.code != top.code or a.message != top.message))

    supporting = ""
    if overall in RECORDING_STATES:
        if recording_since_epoch:
            supporting = f"Since {tf.when(recording_since_epoch, tz, now)}"
            supporting += " today" if not tf.day_prefix(recording_since_epoch, tz, now) else ""
        supporting = (supporting + " · " if supporting else "") + (
            "No action needed." if overall == str(OverallStatus.RECORDING) else
            "Recording continues, but storage is running low.")
    elif overall == str(OverallStatus.STARTING):
        supporting = "Connecting to the camera. This usually takes less than a minute."
    elif overall == str(OverallStatus.STOPPED):
        supporting = "The recording service has been shut down. Contact technical support if this was not planned."
    elif overall not in HEADLINES:
        supporting = "The recorder reported a state this screen does not recognise. Contact technical support."
    if notice is not None:
        supporting = notice.message      # exact text; the notice IS the explanation

    detail = ""
    gap_since = _epoch((doc.get("open_gap") or {}).get("since_utc"))
    if gap_since and overall not in RECORDING_STATES:
        detail = (f"No recording since {tf.when(gap_since, tz, now)} ({tf.hm_utc(gap_since)}) "
                  f"— {tf.duration(now - gap_since)} so far.")

    gen = _epoch(doc.get("generated_utc"))
    updated = f"Updated {tf.hm(gen, tz, seconds=True)}" if gen else "Updated"
    live = overall in RECORDING_STATES
    return DashboardView(headline=headline, supporting=supporting, tone=tone, banner=banner,
                         banner_takes_over=takes_over, notice=notice, other_alerts=others,
                         test_camera=test_cam, stale=False,
                         camera=_camera_tile(doc, False, top, last_saved_epoch, tz, now),
                         storage=_storage_tile(doc, False), drive=drive_tile(export_available),
                         updated=updated, live=live, raw_overall=overall if isinstance(overall, str) else None,
                         detail=detail)
