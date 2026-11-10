"""Keeping the recording computer awake, and finding power settings that would stop recording.

A laptop left unattended (for example in a communications closet) stops recording when it goes to
sleep or hibernates; locking the screen or signing out does not stop the recorder service. Three
measures (owner request 2026-11-10):

1. :class:`KeepAwake` — while the recorder service runs, it tells Windows the system is in use
   (``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``), so *idle* sleep and idle
   hibernation do not happen. The display may still turn off. It does NOT stop sleep caused by
   closing the lid, pressing the power/sleep button or choosing Sleep; those are settings (2).
2. :func:`power_setting_commands` — the administrator's service install sets the active power plan:
   lid closed = do nothing, sleep and hibernate after = never (mains and battery: on a power cut
   the battery keeps it recording), screen off after 5 min (mains) / 2 min (battery).
3. :func:`read_power_settings` + :func:`sleep_risks` — the recorder checks the active plan
   periodically and raises H-001 "This computer is set to go to sleep, which would stop recording"
   when a setting would still put it to sleep (for example someone changed the plan later).

Linux: no-op (servers do not idle-sleep by default; ``systemd-inhibit`` in the unit is the
equivalent and is NOT EXECUTED here).
"""

from __future__ import annotations

import logging
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

# Power setting GUIDs (winnt.h / powercfg aliases)
SUB_SLEEP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
STANDBYIDLE = "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"
HIBERNATEIDLE = "9d7815a6-7ee4-497e-8888-515a05f02364"
SUB_BUTTONS = "4f971e89-eebd-4455-a8de-9e59040e7347"
LIDACTION = "5ca83367-6e45-459f-a27b-476b1d01c936"
LID_ACTIONS = {0: "do nothing", 1: "sleep", 2: "hibernate", 3: "shut down"}

# Screen-off minutes set at install (a lit screen in a closet draws attention; recording is unaffected).
MONITOR_OFF_AC_MIN = 5
MONITOR_OFF_DC_MIN = 2


class KeepAwake:
    """Holds ES_SYSTEM_REQUIRED for the lifetime of the recorder service. The execution state
    belongs to the calling thread and ends when it exits, so a dedicated thread owns it."""

    def __init__(self, api: Any = None, platform: str | None = None) -> None:
        self._platform = platform or sys.platform
        self._api = api
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.active: bool | None = None          # None: not applicable on this platform
        self.error: str | None = None

    def _set(self, flags: int) -> bool:
        api = self._api
        if api is None:
            import ctypes

            api = ctypes.windll.kernel32.SetThreadExecutionState    # type: ignore[attr-defined]
            api.restype = ctypes.c_uint32
            api.argtypes = [ctypes.c_uint32]
        return bool(api(flags))     # previous state; 0 = failed

    def _run(self) -> None:
        try:
            self.active = self._set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            if not self.active:
                self.error = "SetThreadExecutionState failed"
        except Exception as exc:  # never stop the recorder over this; H-001 reports it
            self.active, self.error = False, f"{type(exc).__name__}: {exc}"
        self._ready.set()
        if self.active:
            log.info("keeping the computer awake while recording (idle sleep disabled)")
        else:
            log.error("could not keep the computer awake: %s", self.error)
        self._stop.wait()
        if self.active:
            try:
                self._set(ES_CONTINUOUS)
            except Exception:
                log.exception("could not release keep-awake")
            self.active = False

    def start(self) -> KeepAwake:
        if self._platform != "win32":
            return self
        self._thread = threading.Thread(target=self._run, name="keep-awake", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5.0)


@dataclass
class PowerSettings:
    lid_present: bool | None = None
    lid_ac: int | None = None               # LID_ACTIONS
    lid_dc: int | None = None
    standby_ac_s: int | None = None         # 0 = never
    standby_dc_s: int | None = None
    hibernate_ac_s: int | None = None
    hibernate_dc_s: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_power_settings() -> PowerSettings:
    """Active power plan values (Windows, any account; powrprof.dll). Elsewhere: all None."""
    if sys.platform != "win32":
        return PowerSettings()
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16),
                    ("d4", ctypes.c_ubyte * 8)]

    def guid(s: str) -> GUID:
        return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)

    ps = PowerSettings()
    try:
        powrprof = ctypes.WinDLL("powrprof")
        caps = (ctypes.c_ubyte * 512)()                  # SYSTEM_POWER_CAPABILITIES (76 bytes)
        if powrprof.GetPwrCapabilities(ctypes.byref(caps)):
            ps.lid_present = bool(caps[2])               # PowerButtonPresent, SleepButtonPresent, LidPresent
        scheme = ctypes.POINTER(GUID)()
        rc = powrprof.PowerGetActiveScheme(None, ctypes.byref(scheme))
        if rc != 0:
            ps.error = f"PowerGetActiveScheme failed ({rc})"
            return ps
        try:
            def read(fn: Any, sub: str, setting: str) -> int | None:
                val = wintypes.DWORD()
                s, t = guid(sub), guid(setting)
                r = fn(None, scheme, ctypes.byref(s), ctypes.byref(t), ctypes.byref(val))
                return int(val.value) if r == 0 else None

            ac, dc = powrprof.PowerReadACValueIndex, powrprof.PowerReadDCValueIndex
            ps.lid_ac, ps.lid_dc = read(ac, SUB_BUTTONS, LIDACTION), read(dc, SUB_BUTTONS, LIDACTION)
            ps.standby_ac_s, ps.standby_dc_s = read(ac, SUB_SLEEP, STANDBYIDLE), read(dc, SUB_SLEEP, STANDBYIDLE)
            ps.hibernate_ac_s = read(ac, SUB_SLEEP, HIBERNATEIDLE)
            ps.hibernate_dc_s = read(dc, SUB_SLEEP, HIBERNATEIDLE)
        finally:
            ctypes.windll.kernel32.LocalFree(scheme)     # type: ignore[attr-defined]
    except Exception as exc:
        ps.error = f"{type(exc).__name__}: {exc}"
    return ps


def sleep_risks(ps: PowerSettings, keep_awake_active: bool | None) -> list[str]:
    """Plain descriptions (for support, in the alert detail) of settings that would stop recording."""
    risks = []
    if ps.lid_present:
        for label, v in (("on mains power", ps.lid_ac), ("on battery", ps.lid_dc)):
            if v not in (None, 0):
                risks.append(f"closing the lid makes the computer {LID_ACTIONS.get(v, f'action {v}')} ({label})")
    if keep_awake_active is False:
        risks.append("the recorder could not keep the computer awake")
        for label, v in (("sleeps", ps.standby_ac_s), ("hibernates", ps.hibernate_ac_s)):
            if v:
                risks.append(f"the computer {label} after {v // 60} min without use (on mains power)")
    return risks


def power_setting_commands() -> list[tuple[str, tuple[str, ...], bool]]:
    """(description, argv, may_fail) for the administrator install. Applies to the active power plan."""
    pc = "powercfg"
    lid = "Closing the lid does nothing"
    return [
        ("Never sleep on mains power", (pc, "/change", "standby-timeout-ac", "0"), False),
        ("Never sleep on battery (a power cut does not stop recording)", (pc, "/change", "standby-timeout-dc", "0"), False),
        ("Never hibernate on mains power", (pc, "/change", "hibernate-timeout-ac", "0"), False),
        ("Never hibernate on battery", (pc, "/change", "hibernate-timeout-dc", "0"), False),
        (f"Turn the screen off after {MONITOR_OFF_AC_MIN} min on mains power (recording continues)",
         (pc, "/change", "monitor-timeout-ac", str(MONITOR_OFF_AC_MIN)), False),
        (f"Turn the screen off after {MONITOR_OFF_DC_MIN} min on battery",
         (pc, "/change", "monitor-timeout-dc", str(MONITOR_OFF_DC_MIN)), False),
        # Desktops have no lid; the setting may then be refused, which is harmless.
        (f"{lid} (mains power)", (pc, "/setacvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", "0"), True),
        (f"{lid} (battery)", (pc, "/setdcvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", "0"), True),
        ("Apply the power plan", (pc, "/setactive", "SCHEME_CURRENT"), False),
    ]


def lock_workstation() -> bool:
    """Lock Windows (same as Windows key + L). Recording continues: the recorder is a service."""
    if sys.platform != "win32":
        return False
    import ctypes

    return bool(ctypes.windll.user32.LockWorkStation())     # type: ignore[attr-defined]
