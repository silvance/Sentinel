"""Shared enumerations. Values are stored in the ledger; never rename a value."""

from __future__ import annotations

from enum import StrEnum


class SegmentState(StrEnum):
    ACTIVE = "ACTIVE"            # FFmpeg may still be writing
    CLOSED = "CLOSED"            # listed in segments.csv; awaiting validation
    FINALIZED = "FINALIZED"      # closed normally, validated, hashed, sealed
    INTERRUPTED = "INTERRUPTED"  # writer ended abnormally; playable part sealed
    CORRUPT = "CORRUPT"          # not decodable / no video; sealed as-is


TERMINAL_STATES = frozenset({SegmentState.FINALIZED, SegmentState.INTERRUPTED, SegmentState.CORRUPT})

ALLOWED_TRANSITIONS: dict[SegmentState, frozenset[SegmentState]] = {
    SegmentState.ACTIVE: frozenset({SegmentState.CLOSED, SegmentState.INTERRUPTED, SegmentState.CORRUPT}),
    SegmentState.CLOSED: frozenset({SegmentState.FINALIZED, SegmentState.CORRUPT}),
    SegmentState.FINALIZED: frozenset(),
    SegmentState.INTERRUPTED: frozenset(),
    SegmentState.CORRUPT: frozenset(),
}


class RunEndReason(StrEnum):
    GRACEFUL_STOP = "graceful_stop"          # service shutdown requested
    SOURCE_ENDED = "source_ended"            # FFmpeg exited 0 on its own (stream EOF)
    FFMPEG_ERROR = "ffmpeg_error"            # FFmpeg exited non-zero (disconnect, I/O timeout, ...)
    FFMPEG_KILLED = "ffmpeg_killed"          # FFmpeg terminated by a signal / external kill
    STALL = "stall"                          # stream time stopped advancing
    CONNECT_TIMEOUT = "connect_timeout"      # no media within connect timeout
    STORAGE_CRITICAL = "storage_critical"    # stopped to protect storage
    SERVICE_INTERRUPTED = "service_interrupted"  # discovered at startup: service died mid-run
    LAUNCH_FAILED = "launch_failed"          # could not start FFmpeg at all
    TIMELINE_DISCONTINUITY = "timeline_discontinuity"  # stream time fell behind real time (lost footage)
    INTERNAL_ERROR = "internal_error"        # unexpected recorder error; run closed and retried
    SCHEDULED_PAUSE = "scheduled_pause"      # [schedule] says pause: run closed normally, resumes on schedule


class OverallStatus(StrEnum):
    STARTING = "STARTING"
    RECORDING = "RECORDING"                  # "Recording normally"
    INTERRUPTED = "INTERRUPTED"              # "Recording interrupted"
    STORAGE_WARNING = "STORAGE_WARNING"      # recording, but space is low
    STORAGE_FULL = "STORAGE_FULL"            # recording stopped to protect storage
    ERROR = "ERROR"                          # "System error"
    STOPPED = "STOPPED"                      # service shut down cleanly
    PAUSED = "PAUSED"                        # not recording because the recording schedule says so (P-001)
    CAMERA_NOT_SET_UP = "CAMERA_NOT_SET_UP"  # set up with "camera later"; camera not added yet (P-002)


class CameraLink(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class StorageLevel(StrEnum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


class GapKind(StrEnum):
    BETWEEN_RUNS = "between_runs"
    WITHIN_SEGMENT = "within_segment"
