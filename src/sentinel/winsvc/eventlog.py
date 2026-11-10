"""Write to the Windows Application event log (ctypes ``ReportEventW``) — the last-resort channel
when the service fails before its own log file exists (e.g. the configuration cannot be read).

The source "SentinelRecorder" is not registered with a message file, so Event Viewer prefixes
the text with "The description for Event ID 1000 … cannot be found"; the message itself is
shown in full below that. Registering a message DLL is not worth a binary for one message.
Best effort: never raises.
"""

from __future__ import annotations

import ctypes
import logging
import sys

from . import SERVICE_NAME

EVENTLOG_ERROR_TYPE = 0x0001
EVENTLOG_WARNING_TYPE = 0x0002
EVENTLOG_INFORMATION_TYPE = 0x0004
EVENT_ID = 1000

log = logging.getLogger(__name__)


def report_event(message: str, event_type: int = EVENTLOG_ERROR_TYPE, source: str = SERVICE_NAME) -> bool:
    if sys.platform != "win32":
        return False
    try:
        from ctypes import wintypes

        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.RegisterEventSourceW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        adv.RegisterEventSourceW.restype = wintypes.HANDLE
        adv.ReportEventW.argtypes = [wintypes.HANDLE, wintypes.WORD, wintypes.WORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.WORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPCWSTR), ctypes.c_void_p]
        adv.ReportEventW.restype = wintypes.BOOL
        adv.DeregisterEventSource.argtypes = [wintypes.HANDLE]
        h = adv.RegisterEventSourceW(None, source)
        if not h:
            return False
        try:
            strings = (wintypes.LPCWSTR * 1)(message[:31000])
            return bool(adv.ReportEventW(h, event_type, 0, EVENT_ID, None, 1, 0, strings, None))
        finally:
            adv.DeregisterEventSource(h)
    except Exception:  # noqa: BLE001 - a logging fallback must never take the service down
        log.debug("ReportEventW failed", exc_info=True)
        return False
