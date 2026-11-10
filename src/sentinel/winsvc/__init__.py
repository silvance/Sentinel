"""Windows service integration for the Sentinel recorder.

    python -m sentinel.winsvc install --config C:\\ProgramData\\Sentinel\\config\\sentinel.toml [--dry-run]
    python -m sentinel.winsvc uninstall [--dry-run]
    python -m sentinel.winsvc status [--config ...]
    python -m sentinel.winsvc set-camera-credentials --config ...
    python -m sentinel.winsvc host --config ...        (started by the Service Control Manager only)

Design (see docs/branches/design-windows-service.md for the comparison of alternatives):
* A real Windows service, registered with the Service Control Manager (SCM) by ``sc.exe``.
  The service host is implemented here with ctypes (``scm.py``) — no pywin32, no external
  wrapper executable. The recorder runs *in the service process*, so a stop/shutdown request
  reaches ``RecorderService.request_stop`` directly and FFmpeg gets its graceful ``q``.
* Runs as the virtual account ``NT SERVICE\\SentinelRecorder``: no password to manage, its own
  SID for ACLs, no administrator rights, normal outbound network access to the camera.
* Starts automatically at boot without anyone logged in; SCM failure actions restart it after
  every failure, forever (the last failure action repeats); ``failureflag 1`` makes a non-zero
  exit (e.g. the internal watchdog) count as a failure.
* Pre-shutdown notification gives the recorder time to finalise the last segment.

Nothing in this package runs at import time and nothing here imports Windows-only modules at
module level, so the package imports cleanly on Linux.
"""

from __future__ import annotations

SERVICE_NAME = "SentinelRecorder"
DISPLAY_NAME = "Sentinel Recorder"
DESCRIPTION = ("Records the configured camera continuously and verifies the recordings. "
               "Stopping this service stops recording.")
# Local group whose members (operator accounts) may READ the status folder. Created by install.
OPERATORS_GROUP = "Sentinel Operators"
OPERATORS_GROUP_COMMENT = "May read Sentinel recorder status (not recordings, not settings)"
