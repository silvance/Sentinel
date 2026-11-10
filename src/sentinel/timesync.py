"""Operating-system time-synchronisation status, for the clock-observation record (E1).

What the OS says about its own clock: whether it is synchronised, to which source, when it
last synchronised and (where the tool reports it with a documented sign) the measured offset
from the reference. Collected by running read-only, non-administrative query commands:

* Windows: ``w32tm /query /status /verbose`` (falls back to ``/status``); the Windows Time
  service state from ``sc query w32time``; the configured source from the registry
  (``HKLM\\SYSTEM\\CurrentControlSet\\Services\\W32Time\\Parameters``, readable without admin).
* Linux: ``chronyc tracking`` if chrony is installed, else ``timedatectl show`` (+
  ``timedatectl show-timesync`` for systemd-timesyncd's server).

Nothing here changes any setting. Parsing is for English-language tool output (the field
labels are localised on non-English Windows); anything unparseable is kept as a raw excerpt
with ``synchronized = None`` rather than guessed.

Verification status (see docs/branches/design-time-accuracy.md): the Windows paths were run on
the development PC, where the Windows Time service is stopped (demand-start) and ``w32tm``
returns "The service has not been started. (0x80070426)" — that real output is a unit-test
fixture. The *successful* ``w32tm`` output format and all Linux tool outputs are parsed from
their documented formats and are NOT verified against a live system.

This module deliberately does not touch ``sentinel.platform`` (another branch owns
``Platform.clock_synchronized``); it can later back that method.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .clock import iso_utc

Runner = Callable[[list[str]], "CommandResult"]
_RAW_LIMIT = 1500


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None          # None: could not run (missing, timeout)
    stdout: str
    stderr: str = ""
    error: str | None = None


def run_command(argv: list[str], timeout_s: float = 5.0) -> CommandResult:
    """Run a read-only query command; never raises."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000      # CREATE_NO_WINDOW: no console flash for a GUI-less service
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False,
                            encoding="utf-8", errors="replace", **kwargs)
    except FileNotFoundError:
        return CommandResult(None, "", error="not_found")
    except subprocess.TimeoutExpired:
        return CommandResult(None, "", error="timeout")
    except OSError as exc:
        return CommandResult(None, "", error=f"os_error: {exc}")
    return CommandResult(cp.returncode, cp.stdout or "", cp.stderr or "")


@dataclass
class OsTimeSync:
    tool: str | None = None                 # w32tm | chronyc | timedatectl | None
    synchronized: bool | None = None
    source: str | None = None
    last_sync_utc: str | None = None
    offset_s: float | None = None           # local clock minus reference; only with a documented sign
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------- parsers (pure)
def _kv_colon(text: str) -> dict[str, str]:
    """'Label: value' lines -> {label: value} (first colon splits; labels stripped)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k and k not in out:
            out[k] = v
    return out


def _seconds(value: str | None) -> float | None:
    """'0.0312500s' / '1234.5678s' / '12.3 seconds' -> float seconds."""
    if not value:
        return None
    m = re.match(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(?:s|sec|seconds)?\b", value)
    return float(m.group(1)) if m else None


def parse_w32tm_status(text: str, now_epoch: float | None = None) -> dict[str, Any]:
    """Parse ``w32tm /query /status [/verbose]`` (English labels).

    Returns parsed fields plus ``synchronized``: False for Leap Indicator 3 ("not
    synchronized") or a "Local CMOS Clock"/"Free-running System Clock" source, True for leap
    indicator 0-2 with a stratum 1-15 source, otherwise None. ``Phase Offset`` (verbose only) is
    reported as ``phase_offset_s_raw`` with w32tm's own sign: its convention is not documented
    clearly enough to store as local-minus-reference. ``Last Successful Sync Time`` is local
    time in the user's locale format and is kept as text; ``Time since Last Good Sync Time``
    (verbose) is converted to a UTC time.
    """
    kv = _kv_colon(text)
    out: dict[str, Any] = {}
    leap = kv.get("Leap Indicator")
    if leap is not None:
        m = re.match(r"(\d)", leap)
        out["leap_indicator"] = int(m.group(1)) if m else None
    stratum = kv.get("Stratum")
    if stratum is not None:
        m = re.match(r"(\d+)", stratum)
        out["stratum"] = int(m.group(1)) if m else None
    for label, key in (("Source", "source"), ("ReferenceId", "reference_id"),
                       ("Last Successful Sync Time", "last_successful_sync_time_local_text"),
                       ("State Machine", "state_machine"), ("Last Sync Error", "last_sync_error"),
                       ("Server Role", "server_role")):
        if label in kv:
            out[key] = kv[label]
    for label, key in (("Root Delay", "root_delay_s"), ("Root Dispersion", "root_dispersion_s"),
                       ("Phase Offset", "phase_offset_s_raw"), ("Time since Last Good Sync Time", "since_last_good_sync_s")):
        if label in kv:
            out[key] = _seconds(kv[label])
    if out.get("since_last_good_sync_s") is not None and now_epoch is not None:
        out["last_good_sync_utc"] = iso_utc(now_epoch - out["since_last_good_sync_s"])
    src = (out.get("source") or "").lower()
    leap_i, strat = out.get("leap_indicator"), out.get("stratum")
    if leap_i == 3 or "local cmos clock" in src or "free-running" in src:
        out["synchronized"] = False
    elif leap_i is not None and leap_i in (0, 1, 2) and strat is not None and 1 <= strat <= 15:
        out["synchronized"] = True
    else:
        out["synchronized"] = None
    return out


def parse_sc_state(text: str) -> str | None:
    """``sc query <svc>`` -> 'RUNNING' / 'STOPPED' / ... (the numeric code is locale-independent)."""
    codes = {1: "STOPPED", 2: "START_PENDING", 3: "STOP_PENDING", 4: "RUNNING", 5: "CONTINUE_PENDING",
             6: "PAUSE_PENDING", 7: "PAUSED"}
    for line in text.splitlines():
        if "STATE" in line.upper() and ":" in line:
            m = re.search(r":\s*(\d+)", line)
            if m:
                return codes.get(int(m.group(1)), f"STATE_{m.group(1)}")
    return None


def parse_timedatectl_show(text: str) -> dict[str, Any]:
    """``timedatectl show`` / ``show-timesync`` key=value output."""
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    sync = out.get("NTPSynchronized", "").lower()
    out["synchronized"] = True if sync == "yes" else False if sync == "no" else None
    return out


def parse_chronyc_tracking(text: str) -> dict[str, Any]:
    """``chronyc tracking`` (human-readable form: explicit 'fast'/'slow' wording, so the sign of
    the offset is unambiguous; ``offset_s`` is local clock minus NTP time)."""
    kv = _kv_colon(text)
    out: dict[str, Any] = {}
    if "Reference ID" in kv:
        out["reference_id"] = kv["Reference ID"]
        m = re.search(r"\(([^)]*)\)", kv["Reference ID"])
        out["reference_name"] = m.group(1) if m else None
    if "Stratum" in kv:
        m = re.match(r"(\d+)", kv["Stratum"])
        out["stratum"] = int(m.group(1)) if m else None
    if "Ref time (UTC)" in kv:
        out["ref_time_utc_text"] = kv["Ref time (UTC)"]
    st = kv.get("System time")
    if st:
        m = re.match(r"([-+]?\d+(?:\.\d+)?)\s+seconds\s+(fast|slow)", st)
        if m:
            mag = float(m.group(1))
            out["offset_s"] = mag if m.group(2) == "fast" else -mag
    for label, key in (("Last offset", "last_offset_s"), ("RMS offset", "rms_offset_s"),
                       ("Root delay", "root_delay_s"), ("Root dispersion", "root_dispersion_s"),
                       ("Update interval", "update_interval_s")):
        if label in kv:
            out[key] = _seconds(kv[label])
    fr = kv.get("Frequency")
    if fr:
        m = re.match(r"([-+]?\d+(?:\.\d+)?)\s+ppm\s+(fast|slow)", fr)
        if m:
            out["frequency_ppm"] = float(m.group(1)) * (1 if m.group(2) == "fast" else -1)
    if "Leap status" in kv:
        out["leap_status"] = kv["Leap status"]
    ref = (out.get("reference_id") or "")
    leap = (out.get("leap_status") or "").lower()
    strat = out.get("stratum")
    if leap == "not synchronised" or ref.startswith("00000000") or strat == 0:
        out["synchronized"] = False
    elif leap in ("normal", "insert second", "delete second") and strat is not None and strat >= 1:
        out["synchronized"] = True
    else:
        out["synchronized"] = None
    return out


# ---------------------------------------------------------------------- collection
def _excerpt(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _RAW_LIMIT else text[:_RAW_LIMIT] + " ..."


def _windows_time_config() -> dict[str, Any]:
    """Configured time source from the registry (read-only; no admin needed)."""
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return {}
    out: dict[str, Any] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\W32Time\Parameters") as key:
            for name in ("NtpServer", "Type"):
                try:
                    out[f"configured_{name.lower()}"] = str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    pass
    except OSError as exc:
        out["registry_error"] = str(exc)
    return out


def query_windows(run: Runner = run_command, now: Callable[[], float] = time.time) -> OsTimeSync:
    res = OsTimeSync(tool="w32tm")
    detail: dict[str, Any] = {}
    sc = run(["sc", "query", "w32time"])
    detail["service_state"] = parse_sc_state(sc.stdout) if sc.returncode is not None else None
    detail.update(_windows_time_config())
    cr = run(["w32tm", "/query", "/status", "/verbose"])
    if cr.returncode not in (0, None) or (cr.returncode == 0 and "Leap Indicator" not in cr.stdout):
        cr2 = run(["w32tm", "/query", "/status"])
        if cr2.returncode == 0:
            cr = cr2
    detail["returncode"] = cr.returncode
    if cr.returncode == 0:
        parsed = parse_w32tm_status(cr.stdout, now())
        detail["w32tm"] = parsed
        if "leap_indicator" not in parsed and "source" not in parsed:
            detail["raw"] = _excerpt(cr.stdout)          # e.g. localised labels: keep, do not guess
        res.synchronized = parsed.get("synchronized")
        res.source = parsed.get("source")
        res.last_sync_utc = parsed.get("last_good_sync_utc")
    else:
        detail["error"] = cr.error or _excerpt(cr.stdout + "\n" + cr.stderr)
        if detail.get("service_state") == "STOPPED":
            # The Windows Time service is not running (demand-start on Windows Home): nothing is
            # disciplining the clock right now. Reported as not synchronised, with the reason.
            res.synchronized = False
            detail["reason"] = "windows_time_service_stopped"
        res.source = detail.get("configured_ntpserver")
        if res.source:
            detail["source_is_configured_not_confirmed"] = True
    res.detail = detail
    return res


def query_linux(run: Runner = run_command, which: Callable[[str], str | None] = shutil.which) -> OsTimeSync:
    detail: dict[str, Any] = {}
    if which("chronyc"):
        cr = run(["chronyc", "tracking"])
        detail["chronyc_returncode"] = cr.returncode
        if cr.returncode == 0:
            p = parse_chronyc_tracking(cr.stdout)
            detail["chronyc"] = p
            return OsTimeSync(tool="chronyc", synchronized=p.get("synchronized"),
                              source=p.get("reference_name") or p.get("reference_id"),
                              offset_s=p.get("offset_s"), detail=detail)
        detail["chronyc_error"] = cr.error or _excerpt(cr.stdout + "\n" + cr.stderr)
    if which("timedatectl"):
        cr = run(["timedatectl", "show"])
        detail["timedatectl_returncode"] = cr.returncode
        if cr.returncode == 0:
            p = parse_timedatectl_show(cr.stdout)
            detail["timedatectl"] = {k: p.get(k) for k in ("NTP", "NTPSynchronized", "CanNTP", "Timezone",
                                                            "LocalRTC")}
            source = None
            ts = run(["timedatectl", "show-timesync"])
            if ts.returncode == 0:
                tp = parse_timedatectl_show(ts.stdout)
                source = tp.get("ServerName") or tp.get("ServerAddress") or None
                detail["timesync"] = {k: tp.get(k) for k in ("ServerName", "ServerAddress", "PollIntervalUSec")}
            return OsTimeSync(tool="timedatectl", synchronized=p.get("synchronized"), source=source, detail=detail)
        detail["timedatectl_error"] = cr.error or _excerpt(cr.stdout + "\n" + cr.stderr)
    detail.setdefault("error", "no time synchronisation query tool available")
    return OsTimeSync(tool=None, detail=detail)


def query_os_time_sync(platform: str | None = None, run: Runner = run_command) -> OsTimeSync:
    """Best-effort OS time-sync status for this host; never raises."""
    plat = platform or sys.platform
    try:
        return query_windows(run) if plat == "win32" else query_linux(run)
    except Exception as exc:  # defensive: an observation must never break recording
        return OsTimeSync(detail={"error": f"{type(exc).__name__}: {exc}"})
