"""Operator alert catalogue and escalation rules.

Every condition an operator may see has a fixed code, a fixed plain-language headline and a
fixed action. Codes never change meaning once released (they are quoted to support staff
over the phone). Messages contain no paths, commands, codec or network terms.

Escalation: an outage first shows a *notice* ("reconnecting automatically"). If automatic
recovery has not restored recording within ``escalate_after_s``, it becomes *critical*:

    Recording stopped. Error R-003: Camera connection lost. Automatic recovery unsuccessful.
    Contact technical support and provide diagnostic report.

The recorder keeps retrying after escalation; if recording resumes the alert clears and the
outage remains in the ledger as a gap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any


class Severity(IntEnum):
    NOTICE = 1      # informational; automatic recovery in progress
    WARNING = 2     # recording continues but something needs attention
    CRITICAL = 3    # recording stopped, or integrity at risk; needs a person


@dataclass(frozen=True)
class AlertDef:
    code: str
    headline: str           # short noun phrase, operator language
    action: str             # what the operator should do
    stops_recording: bool


SUPPORT = "Contact technical support and provide diagnostic report."

CATALOG: dict[str, AlertDef] = {d.code: d for d in [
    AlertDef("R-001", "Cannot connect to camera", SUPPORT, True),
    AlertDef("R-002", "Camera video stopped", SUPPORT, True),
    AlertDef("R-003", "Camera connection lost", SUPPORT, True),
    AlertDef("R-004", "Recording keeps restarting", SUPPORT, False),
    AlertDef("R-005", "Video is arriving but recordings are not being saved", SUPPORT, True),
    AlertDef("R-006", "The recording software stopped unexpectedly and is restarting", SUPPORT, True),
    AlertDef("S-001", "Recording storage is low", "Prepare a review drive. " + SUPPORT, False),
    AlertDef("S-002", "Recording storage is full", SUPPORT, True),
    AlertDef("S-003", "Recording storage cannot be read", SUPPORT, True),
    AlertDef("V-001", "Recordings could not be verified", SUPPORT, False),
    AlertDef("V-002", "A damaged recording segment was detected", SUPPORT, False),
    AlertDef("V-003", "The recording log has been altered", SUPPORT, False),
    AlertDef("V-004", "Part of the recording log could not be protected against changes", SUPPORT, False),
    AlertDef("T-001", "The system clock was changed during recording", SUPPORT, False),
    AlertDef("T-002", "The system clock is not synchronised", SUPPORT, False),
    AlertDef("C-001", "The recorder cannot start because of a configuration or software problem", SUPPORT, True),
    AlertDef("C-002", "This computer has not been set up for recording yet",
             "Setup must be done by the installing technician. Contact technical support.", True),
    AlertDef("C-003", "The recorder could not be started", SUPPORT, True),
    AlertDef("D-001", "Recording database problem", SUPPORT, True),
    AlertDef("X-001", "The recorder is not responding", SUPPORT, False),
    # Review-drive export (separate process; never stops recording). docs/04 §6.
    AlertDef("E-001", "The review drive is full",
             "The export did not finish. Use a larger drive, or contact technical support.", False),
    AlertDef("E-002", "The review drive was removed before the export finished",
             "Recordings on this system are safe. Insert the same drive again and press "
             "Prepare Review Drive to finish the export.", False),
    AlertDef("E-003", "Files on the review drive could not be verified",
             "Do not remove the drive and do not give it to a reviewer. " + SUPPORT, False),
    AlertDef("E-004", "The review drive already holds different files with the same names",
             "Nothing was overwritten. Do not give this drive to a reviewer. " + SUPPORT, False),
    AlertDef("E-005", "The review drive cannot be used",
             "Use a different drive, or contact technical support.", False),
    AlertDef("E-006", "A recording on this system could not be read for export", SUPPORT, False),
    AlertDef("E-007", "The export stopped because of a software problem",
             "Do not remove the drive. " + SUPPORT, False),
    # M-: removable Media (archive / review drives). "D-" is already the recording Database.
    # The destructive action is called "Erase and Set Up Drive" so it cannot be confused with the
    # export action "Prepare Review Drive" (U4).
    # None of these stop recording: recording never writes to removable drives (R3).
    AlertDef("M-001", "The inserted drive is empty and must be set up before use",
             "Press Erase and Set Up Drive.", False),
    AlertDef("M-002", "The inserted drive uses the wrong format and must be set up before use",
             "Press Erase and Set Up Drive.", False),
    AlertDef("M-003", "The inserted drive contains other files",
             "Erasing it will delete them. Do not erase it unless you are sure the files are not needed. "
             "Contact technical support if unsure.", False),
    AlertDef("M-004", "The inserted drive cannot be used", "Remove it and use a different drive.", False),
    AlertDef("M-005", "Drive setup failed",
             "Do not use this drive. " + SUPPORT, False),
    AlertDef("M-006", "The drive changed before it could be erased",
             "Nothing was erased. Check the drive and try again.", False),
    AlertDef("M-007", "The drive could not be safely removed",
             "Do not unplug the drive yet. Close any open files on it and try again. "
             "If this continues, contact technical support.", False),
    AlertDef("M-008", "Drive checks are unavailable", SUPPORT, False),
    AlertDef("M-009", "This drive holds Sentinel recordings and will not be erased",
             "Contact technical support.", False),
    AlertDef("M-010", "Setting up drives is not enabled on this system yet",
             "Contact technical support.", False),
    # Archive drive (separate low-priority process; never stops recording). docs/04 §2-3, S1.
    # Not "Recording continues": also shown while nothing is recorded (camera not added, paused).
    AlertDef("A-001", "No archive drive is connected",
             "Recordings are kept on this computer until one is inserted. Insert the archive drive.", False),
    AlertDef("A-002", "The archive drive is full",
             "Recording continues on this system. Do not unplug the archive drive yet: press Swap Archive "
             "Drive and wait until the screen says which drive to take out and how. Take out only that "
             "drive, then insert an empty archive drive.", False),
    AlertDef("A-003", "The archive drive was removed without preparing it",
             "No recordings were lost. Copying will continue when an archive drive is inserted.", False),
    AlertDef("A-004", "Recordings are waiting too long to be copied to the archive drive",
             "Recording continues on this system. Check that an archive drive with free space is inserted. "
             + SUPPORT, False),
    AlertDef("A-005", "The archive drive cannot be used",
             "Use a different archive drive, or contact technical support.", False),
    AlertDef("A-006", "Files on the archive drive could not be verified",
             "Do not use this archive drive as evidence. " + SUPPORT, False),
    AlertDef("A-007", "Archive copying is not running", SUPPORT, False),
    AlertDef("A-008", "A recording on this system could not be read for archiving", SUPPORT, False),
    AlertDef("A-009", "The archive drive will be full within the next few days",
             "Recording continues. Have an empty archive drive ready for the next swap.", False),
    # Local retention (admin-enabled removal of archived recordings from this computer).
    AlertDef("L-001", "Recording storage on this computer will be full before the next expected drive swap",
             "Recording continues for now. Swap the archive drive. " + SUPPORT, False),
    AlertDef("L-002", "Archived recordings could not be removed to free space on this computer",
             "Recording continues for now. Swap the archive drive. " + SUPPORT, False),
    # Planned pause ([schedule]); not a fault. The status message names the resume time.
    AlertDef("P-001", "Recording is paused by the recording schedule",
             "Recording resumes automatically at the scheduled time. No action is needed.", False),
    # Motion period ([schedule] action "motion"): waiting for motion. Planned, not a fault.
    AlertDef("P-003", "Waiting for motion",
             "Outside the recording hours, recording starts by itself when motion is detected. "
             "No action is needed.", False),
    AlertDef("W-001", "Motion detection is not working, so the recorder records all the time",
             "Recording continues. " + SUPPORT, False),
    # Motion was seen but the recording that should follow failed (review 2026-11-10: this was
    # shown as "Waiting for motion ... No action is needed").
    AlertDef("W-002", "Motion was detected but could not be recorded", SUPPORT, True),
    # Set up in advance ("setup --camera-later"); the technician adds the camera on site (set-camera).
    AlertDef("P-002", "Not recording: the camera has not been added yet",
             "Recording starts once the installing technician adds the camera. If the camera should "
             "already be set up, contact technical support.", False),
    # Host power settings: an unattended laptop that goes to sleep stops recording (sentinel.power).
    AlertDef("H-001", "This computer is set to go to sleep, which would stop recording",
             "Recording continues for now. Do not close the lid. " + SUPPORT, False),
    AlertDef("Z-001", "A test camera is configured. These recordings are not evidence",
             "Contact technical support before any operational use.", False),
]}


@dataclass
class Alert:
    code: str
    severity: int
    severity_name: str
    message: str            # complete operator sentence(s)
    recording_stopped: bool
    since_utc: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)   # for administrators / support only

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_alert(code: str, severity: Severity, *, recovering: bool = False, attempt: int | None = None,
               since_utc: str | None = None, detail: dict[str, Any] | None = None) -> Alert:
    d = CATALOG[code]
    stopped = d.stops_recording and (recovering or severity is Severity.CRITICAL)
    if recovering and severity is Severity.NOTICE:
        tries = f" (attempt {attempt})" if attempt and attempt > 1 else ""
        msg = f"Recording interrupted. Error {code}: {d.headline}. Reconnecting automatically{tries}."
    elif code.startswith("P-"):
        msg = f"{d.headline} ({code}). {d.action}"      # a planned state, not an error
    elif d.stops_recording and severity is Severity.CRITICAL:
        tail = " Automatic recovery unsuccessful." if recovering else ""
        msg = f"Recording stopped. Error {code}: {d.headline}.{tail} {d.action}"
    else:
        msg = f"Error {code}: {d.headline}. {d.action}"
    return Alert(code=code, severity=int(severity), severity_name=severity.name, message=msg,
                 recording_stopped=stopped, since_utc=since_utc, detail=detail or {})


def outage_code(last_end_reason: str | None, ever_recorded: bool) -> str:
    """Which R-code describes a camera outage."""
    if last_end_reason == "stall":
        return "R-002"
    if not ever_recorded:
        return "R-001"
    return "R-003"


def most_severe(alerts: list[Alert]) -> Alert | None:
    return max(alerts, key=lambda a: a.severity) if alerts else None
