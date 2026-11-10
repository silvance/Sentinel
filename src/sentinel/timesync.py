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


def parse_w32tm_status(text: str, now_epoch: float | None = None,
                       max_sync_age_s: float | None = None) -> dict[str, Any]:
    """Parse ``w32tm /query /status [/verbose]`` (English labels).

    Returns parsed fields plus ``synchronized`` and ``sync_reason``:
    * False for Leap Indicator 3 ("not synchronized"), a "Local CMOS Clock"/"Free-running System
      Clock" source, a non-zero ``Last Sync Error``, or a last good sync older than
      ``max_sync_age_s`` (default: twice the reported poll interval);
    * True only for leap indicator 0-2, stratum 1-15, last sync error 0 and a fresh last good sync;
    * None when it cannot be decided (e.g. non-verbose output without the sync age, or
      unrecognised/localised labels). ``Phase Offset`` (verbose only) is
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
    if "Poll Interval" in kv:
        m = re.search(r"\((\d+(?:\.\d+)?)s\)", kv["Poll Interval"])
        out["poll_interval_s"] = float(m.group(1)) if m else None
    since = out.get("since_last_good_sync_s")
    if since is not None and now_epoch is not None:
        out["last_good_sync_utc"] = iso_utc(now_epoch - since)
    src = (out.get("source") or "").lower()
    leap_i, strat = out.get("leap_indicator"), out.get("stratum")
    err = out.get("last_sync_error")
    limit = max_sync_age_s if max_sync_age_s else (2 * out["poll_interval_s"] if out.get("poll_interval_s") else None)
    out["max_sync_age_s"] = limit
    sync: bool | None
    if leap_i == 3 or "local cmos clock" in src or "free-running" in src:
        sync, reason = False, "not_synchronized"
    elif leap_i is None or leap_i not in (0, 1, 2) or strat is None or not 1 <= strat <= 15:
        sync, reason = None, "status_not_recognised"
    elif err is not None and not re.match(r"^0\b", err):
        sync, reason = False, "last_sync_error"
    elif since is None:
        sync, reason = None, "sync_age_not_reported"
    elif limit is None:
        sync, reason = None, "sync_age_limit_unknown"
    elif since > limit:
        sync, reason = False, "last_sync_too_old"
    else:
        sync, reason = True, "ok"
    out["synchronized"], out["sync_reason"] = sync, reason
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


def query_windows(run: Runner = run_command, now: Callable[[], float] = time.time,
                  max_sync_age_s: float | None = None) -> OsTimeSync:
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
        parsed = parse_w32tm_status(cr.stdout, now(), max_sync_age_s)
        detail["w32tm"] = parsed
        detail["reason"] = parsed.get("sync_reason")
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


def query_os_time_sync(platform: str | None = None, run: Runner = run_command,
                       max_sync_age_s: float | None = None) -> OsTimeSync:
    """Best-effort OS time-sync status for this host; never raises."""
    plat = platform or sys.platform
    try:
        return query_windows(run, max_sync_age_s=max_sync_age_s) if plat == "win32" else query_linux(run)
    except Exception as exc:  # defensive: an observation must never break recording
        return OsTimeSync(detail={"error": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------- SNTP offset measurement
NTP_UNIX_OFFSET = 2208988800          # seconds from 1900-01-01 (NTP era 0) to 1970-01-01


@dataclass
class SntpResult:
    offset_s: float | None            # this PC's clock minus the reference (s)
    delay_s: float | None             # round-trip delay; the offset's uncertainty is at most delay/2
    detail: dict[str, Any] = field(default_factory=dict)


def _ntp_to_unix(b: bytes) -> float:
    sec = int.from_bytes(b[:4], "big")
    frac = int.from_bytes(b[4:8], "big")
    return sec - NTP_UNIX_OFFSET + frac / 2**32


def _unix_to_ntp(t: float) -> bytes:
    sec = int(t) + NTP_UNIX_OFFSET
    frac = int((t - int(t)) * 2**32) & 0xFFFFFFFF
    return (sec & 0xFFFFFFFF).to_bytes(4, "big") + frac.to_bytes(4, "big")


def _split_server(server: str) -> tuple[str, int]:
    if server.startswith("["):                       # [IPv6]:port
        host, _, rest = server[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else 123
    if server.count(":") == 1:
        host, _, port = server.partition(":")
        return host, int(port)
    return server, 123


def sntp_offset(server: str, timeout_s: float = 2.0, clock: Callable[[], float] = time.time) -> SntpResult:
    """One SNTP (RFC 4330) client query to ``server`` ("host" or "host:port"); never raises.

    Read-only: it sends one 48-byte client request and changes nothing on this PC. The caller
    chooses the server (the approved local time source); there is no default. The reply is
    accepted only if it is a server-mode reply echoing our transmit timestamp, with stratum
    1-15 and leap indicator != 3 (unsynchronised server).
    """
    import socket

    detail: dict[str, Any] = {"server": server}
    try:
        host, port = _split_server(server)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        family, _, _, _, addr = infos[0]
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_s)
            req = bytearray(48)
            req[0] = (0 << 6) | (4 << 3) | 3          # LI 0, version 4, mode 3 (client)
            t1 = clock()
            tx = _unix_to_ntp(t1)
            req[40:48] = tx
            sock.sendto(bytes(req), addr)
            data, _ = sock.recvfrom(512)
            t4 = clock()
    except (OSError, ValueError) as exc:          # unreachable, timeout, bad name
        detail["error"] = f"{type(exc).__name__}: {exc}"
        return SntpResult(None, None, detail)
    if len(data) < 48:
        detail["error"] = "short_reply"
        return SntpResult(None, None, detail)
    li, mode, stratum = data[0] >> 6, data[0] & 7, data[1]
    detail.update(leap=li, version=(data[0] >> 3) & 7, stratum=stratum, reference_id=data[12:16].hex())
    if mode != 4 or data[24:32] != tx:
        detail["error"] = "not_a_reply_to_this_request"
        return SntpResult(None, None, detail)
    if li == 3 or not 1 <= stratum <= 15:
        detail["error"] = "server_not_synchronised"
        return SntpResult(None, None, detail)
    t2, t3 = _ntp_to_unix(data[32:40]), _ntp_to_unix(data[40:48])
    ref_minus_local = ((t2 - t1) + (t3 - t4)) / 2
    delay = (t4 - t1) - (t3 - t2)
    return SntpResult(round(-ref_minus_local, 6), round(delay, 6), detail)
