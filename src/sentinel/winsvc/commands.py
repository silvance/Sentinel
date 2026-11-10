"""Generate the exact install / uninstall steps for the Windows service (pure, unit-tested).

Every external step is an argv list for ``sc.exe`` or ``icacls``; :func:`render` shows the
command line exactly as :mod:`subprocess` passes it (paste into an *elevated Command Prompt* to
run by hand). Two settings have no ``sc.exe`` sub-command and are in-process actions:
the pre-shutdown timeout (ChangeServiceConfig2) and, for source installs only, the service's
``Environment`` registry value.

ACLs use language-neutral ``*S-1-...`` SIDs so the same commands work on any Windows language:
* SYSTEM ``S-1-5-18`` and Administrators ``S-1-5-32-544``: full control;
* the service's own virtual account ``NT SERVICE\\SentinelRecorder`` (SID computed by
  :func:`sentinel.platform.winacl.service_sid`): modify on data/spool/log, read on config,
  read on the credentials file;
* Users ``S-1-5-32-545``: read on the configuration directory (the camera address is not a
  secret; the operator GUI needs to find the status folder);
* the local group "Sentinel Operators" (default; or Users if the owner chooses
  ``--status-readers users``): read on the status folder only (status.json + read-only ledger
  snapshot). Nothing else.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from ..platform.winacl import SID_ADMINISTRATORS, SID_SYSTEM, SID_USERS, service_sid
from . import DESCRIPTION, DISPLAY_NAME, OPERATORS_GROUP, OPERATORS_GROUP_COMMENT, SERVICE_NAME

# Restart after 5 s, then 30 s, then every 60 s. The SCM repeats the LAST action for every
# further failure ("Subsequent failures" in services.msc), so it never gives up. The failure
# counter resets after a day without failures.
FAILURE_RESET_S = 86400
FAILURE_ACTIONS = "restart/5000/restart/30000/restart/60000"
# Time Windows waits at shutdown for the recorder to stop FFmpeg gracefully and seal the last
# segment. Normally a few seconds; the recorder bounds its shutdown work to
# sentinel.service.SHUTDOWN_BUDGET_S (165 s) so it always fits (unit test checks this).
PRESHUTDOWN_TIMEOUT_MS = 180_000
# Only privilege the recorder needs (directory traversal). Everything else the virtual account
# could hold is removed from the service token. NOT EXECUTED: if the service fails to start with
# a privilege error on the target machine, remove this step and report it.
REQUIRED_PRIVILEGES = "SeChangeNotifyPrivilege"


class PlanError(ValueError):
    """The requested installation is unsafe or impossible; nothing has been changed."""


@dataclass(frozen=True)
class Step:
    description: str
    argv: tuple[str, ...] | None = None          # external command
    action: str | None = None                    # in-process action name (see __main__._ACTIONS)
    params: dict[str, Any] = field(default_factory=dict)
    may_fail: bool = False                       # e.g. stopping a service that is not running


def render(step: Step) -> str:
    if step.argv is not None:
        return subprocess.list2cmdline(step.argv)
    args = ", ".join(f"{k}={v!r}" for k, v in step.params.items())
    return f"[built-in] {step.action}({args})"


# ---------------------------------------------------------------------- service command line
@dataclass(frozen=True)
class HostCommand:
    command_line: str
    environment: tuple[str, ...]                 # "NAME=value" entries for the service's environment
    interpreter: str                             # file the service account must be able to execute
    read_paths: tuple[str, ...]                  # directories the service account must be able to read


def host_command(config_path: Path, *, frozen: bool | None = None, executable: str | None = None,
                 base_executable: str | None = None, prefix: str | None = None, base_prefix: str | None = None,
                 package_root: Path | None = None) -> HostCommand:
    """The ``binPath`` for the service.

    * PyInstaller one-folder build (``sys.frozen``): ``"<exe>" host --config "<cfg>"``. The
      service exe's entry point must be ``sentinel.winsvc.__main__:main`` (packaging branch).
      One-*file* builds must NOT be used for the service: their bootloader re-launches itself as
      a child process, and the SCM only accepts the process it started.
    * Source install: the *base* interpreter, because a venv's ``Scripts\\python.exe`` is a
      redirector that starts the real interpreter as a child process (same SCM problem). The
      venv's packages and the source tree are supplied through ``PYTHONPATH`` in the service's
      own environment (registry ``Environment`` value).
    """
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable = executable or sys.executable
    cfg = str(config_path)
    if frozen:
        return HostCommand(_command_line(executable, ["host", "--config", cfg]), (), executable,
                           (str(Path(executable).parent),))
    base_executable = base_executable or getattr(sys, "_base_executable", executable)
    prefix = prefix or sys.prefix
    base_prefix = base_prefix or sys.base_prefix
    package_root = package_root or Path(__file__).resolve().parents[2]   # .../src (contains sentinel/)
    in_venv = os.path.normcase(prefix) != os.path.normcase(base_prefix)
    interpreter = base_executable if in_venv else executable
    paths = [str(package_root)]
    if in_venv:
        paths.append(str(Path(prefix) / "Lib" / "site-packages"))
    line = _command_line(interpreter, ["-m", "sentinel.winsvc", "host", "--config", cfg])
    return HostCommand(line, ("PYTHONPATH=" + ";".join(paths),), interpreter,
                       (str(Path(interpreter).parent), *paths))


def _command_line(exe: str, args: list[str]) -> str:
    """The executable is always quoted (avoids the "unquoted service path" problem, where the SCM
    tries C:\\Program.exe first); arguments use the standard Windows quoting rules."""
    if '"' in exe:
        raise PlanError(f"executable path must not contain quotes: {exe}")
    return f'"{exe}" ' + subprocess.list2cmdline(args)


# ---------------------------------------------------------------------- safety
def _forbidden_dirs() -> set[str]:
    names = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData",
             "ALLUSERSPROFILE", "PUBLIC", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SystemDrive", "HOMEDRIVE")
    out = set()
    for n in names:
        v = os.environ.get(n)
        if v:
            out.add(_norm(v))
    for fixed in (r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)", r"C:\ProgramData", r"C:\Users"):
        out.add(_norm(fixed))
    return out


def _norm(p: str | Path) -> str:
    return os.path.normcase(str(PureWindowsPath(p))).rstrip("\\/")


def check_dedicated_dir(path: Path, what: str, forbidden: Iterable[str] | None = None) -> None:
    """Refuse to replace the ACL of a drive root, a system/profile directory, or any ancestor of
    one: ``icacls /inheritance:r`` there would lock users and Windows out of their own files."""
    p = PureWindowsPath(path)
    if not p.is_absolute():
        raise PlanError(f"{what} must be an absolute path: {path}")
    if len(p.parts) < 3:
        raise PlanError(f"{what} must be a dedicated folder at least two levels below the drive "
                        f"(e.g. C:\\ProgramData\\Sentinel\\data), not {path}")
    me = _norm(p)
    for f in (forbidden if forbidden is not None else _forbidden_dirs()):
        f = _norm(f)
        if me == f or f.startswith(me + "\\"):
            raise PlanError(f"{what} {path} is (or contains) a Windows system or profile folder; "
                            "use a dedicated folder such as C:\\ProgramData\\Sentinel\\data")


# ---------------------------------------------------------------------- ACL commands
def _sid(s: str) -> str:
    return f"*{s}"


def restrict_dir_steps(directory: Path, svc_sid: str, service_rights: str = "M",
                       extra_grants: tuple[str, ...] = ()) -> list[Step]:
    """Replace the directory's ACL (no inheritance from its parent) with SYSTEM/Administrators
    full control + the service account; children inherit it. Existing children are reset to
    inherit only (explicit extra grants on them are removed)."""
    d = str(directory)
    grants = (f"{_sid(SID_SYSTEM)}:(OI)(CI)F", f"{_sid(SID_ADMINISTRATORS)}:(OI)(CI)F",
              f"{_sid(svc_sid)}:(OI)(CI){service_rights}", *extra_grants)
    return [
        Step(f"Restrict {d} to SYSTEM, Administrators and the service account",
             ("icacls", d, "/inheritance:r", "/grant:r", *grants, "/Q")),
        Step(f"Make Administrators the owner of {d}", ("icacls", d, "/setowner", _sid(SID_ADMINISTRATORS), "/Q")),
        # Fails harmlessly ("file not found") when the directory is empty.
        Step(f"Make existing contents of {d} inherit that ACL",
             ("icacls", str(PureWindowsPath(d) / "*"), "/reset", "/T", "/C", "/Q"), may_fail=True),
    ]


def secret_file_acl_argv(path: Path, svc_sid: str, extra_read_sids: tuple[str, ...] = ()) -> tuple[str, ...]:
    """SYSTEM + Administrators full control, the service account read-only, nothing inherited."""
    grants = [f"{_sid(SID_SYSTEM)}:F", f"{_sid(SID_ADMINISTRATORS)}:F", f"{_sid(svc_sid)}:R"]
    grants += [f"{_sid(s)}:R" for s in extra_read_sids]
    return ("icacls", str(path), "/inheritance:r", "/grant:r", *grants, "/Q")


# ---------------------------------------------------------------------- plans
@dataclass(frozen=True)
class InstallInputs:
    config_path: Path
    data_dirs: tuple[Path, ...]          # spool, data, log
    credentials_path: Path | None
    host: HostCommand
    service_name: str = SERVICE_NAME
    start: bool = True
    status_dir: Path | None = None       # operator-readable folder ([storage] status_dir); required
    status_readers: str = "group"        # "group" (local group Sentinel Operators) | "users" (BUILTIN\Users)
    time_server: str | None = None       # local NTP server for W32Time (validated); None = keep Windows' source


def install_plan(inp: InstallInputs, forbidden: Iterable[str] | None = None) -> list[Step]:
    name = inp.service_name
    sid = service_sid(name)
    account = f"NT SERVICE\\{name}"
    cfg_dir = inp.config_path.parent
    forbidden = list(forbidden) if forbidden is not None else None
    check_dedicated_dir(cfg_dir, "the configuration folder", forbidden)
    for d in inp.data_dirs:
        check_dedicated_dir(d, "the recording/data/log folder", forbidden)
    if any(_norm(d) == _norm(cfg_dir) for d in inp.data_dirs):
        raise PlanError("keep the configuration file in its own folder, separate from data, spool and logs")
    if inp.status_dir is None or any(_norm(inp.status_dir) == _norm(d) for d in (*inp.data_dirs, cfg_dir)):
        raise PlanError("set [storage] status_dir to its own folder (e.g. C:\\ProgramData\\Sentinel\\status): "
                        "operators may read it, while the data folder stays restricted to the service")
    check_dedicated_dir(inp.status_dir, "the status folder", forbidden)
    if inp.status_readers not in ("group", "users"):
        raise PlanError("status readers must be 'group' or 'users'")

    steps = [
        Step(f"Register the service {name} (automatic start at boot, virtual account {account})",
             ("sc.exe", "create", name, "binPath=", inp.host.command_line, "start=", "auto",
              "obj=", account, "DisplayName=", DISPLAY_NAME)),
        Step("Describe the service", ("sc.exe", "description", name, DESCRIPTION)),
        Step("Restart after every failure, forever (5 s, 30 s, then every 60 s)",
             ("sc.exe", "failure", name, "reset=", str(FAILURE_RESET_S), "actions=", FAILURE_ACTIONS)),
        Step("Treat a non-zero exit (e.g. watchdog) as a failure, so it is restarted too",
             ("sc.exe", "failureflag", name, "1")),
        Step("Give the service its own security identifier (used in the folder permissions)",
             ("sc.exe", "sidtype", name, "unrestricted")),
        Step("Remove privileges the recorder does not need", ("sc.exe", "privs", name, REQUIRED_PRIVILEGES)),
        Step(f"Allow {PRESHUTDOWN_TIMEOUT_MS // 1000} s at Windows shutdown to finish the last recording segment",
             action="set_preshutdown_timeout", params={"service": name, "timeout_ms": PRESHUTDOWN_TIMEOUT_MS}),
    ]
    if inp.host.environment:
        steps.append(Step("Give the service the Python search path of this source installation",
                          action="set_service_environment",
                          params={"service": name, "entries": list(inp.host.environment)}))
    if inp.status_readers == "group":
        steps += [
            # Fails harmlessly if the group already exists (re-install); the next step verifies it.
            Step(f'Create the local group "{OPERATORS_GROUP}" (add operator accounts to it)',
                 ("net", "localgroup", OPERATORS_GROUP, "/add", f"/comment:{OPERATORS_GROUP_COMMENT}"),
                 may_fail=True),
            Step(f'Check that the group "{OPERATORS_GROUP}" exists', ("net", "localgroup", OPERATORS_GROUP)),
        ]
        reader = OPERATORS_GROUP          # custom group names are not localised
    else:
        reader = _sid(SID_USERS)
    steps.append(Step("Create the data, spool, log and status folders", action="make_dirs",
                      params={"paths": [str(d) for d in (*inp.data_dirs, inp.status_dir)]}))
    for d in inp.data_dirs:
        steps += restrict_dir_steps(d, sid, "M")
    steps += restrict_dir_steps(inp.status_dir, sid, "M", extra_grants=(f"{reader}:(OI)(CI)RX",))
    steps.append(Step(f"Configuration folder {cfg_dir}: Administrators change, service and Users read",
                      ("icacls", str(cfg_dir), "/inheritance:r", "/grant:r",
                       f"{_sid(SID_SYSTEM)}:(OI)(CI)F", f"{_sid(SID_ADMINISTRATORS)}:(OI)(CI)F",
                       f"{_sid(sid)}:(OI)(CI)RX", f"{_sid(SID_USERS)}:(OI)(CI)RX", "/Q")))
    if inp.credentials_path is not None:
        steps.append(Step("Camera credentials file: Administrators and the service account only",
                          secret_file_acl_argv(inp.credentials_path, sid)))
    steps += time_service_steps(inp.time_server)
    if inp.start:
        steps.append(Step("Start the service now", ("sc.exe", "start", name)))
    return steps


# ---------------------------------------------------------------------- Windows Time (owner decision)
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def validate_time_server(host: str) -> str:
    """Accept exactly one DNS name or dotted IPv4 address; reject everything else.

    The value ends up inside a ``w32tm`` argument, so anything that could change the command —
    spaces (w32tm's peer-list separator), commas (peer flags), quotes, slashes, ``&|<>^%``,
    non-ASCII look-alikes, IPv6 or URLs — is refused rather than escaped.
    """
    if not isinstance(host, str) or not host or len(host) > 253 or not host.isascii():
        raise PlanError("time server must be a host name or IPv4 address (at most 253 characters)")
    h = host[:-1] if host.endswith(".") else host
    if _IPV4.fullmatch(h):
        if any(int(o) > 255 or (len(o) > 1 and o.startswith("0")) for o in h.split(".")):
            raise PlanError(f"time server {host!r} is not a valid IPv4 address")
        return h
    labels = h.split(".")
    if not all(_HOST_LABEL.fullmatch(lbl) for lbl in labels) or labels[-1].isdigit():
        raise PlanError(f"time server {host!r} is not a valid host name (letters, digits, '-' and '.')")
    return h


# Peer flag 0x8 = "client mode" requests, the mode ordinary (non-Windows) NTP servers answer.
NTP_PEER_FLAGS = "0x8"


def time_service_steps(time_server: str | None) -> list[Step]:
    """Owner decision: a small local time server; Windows Time starts automatically.
    Syntax checked against ``sc.exe config`` and ``w32tm /?`` help text on the dev host.
    NOT EXECUTED (administrator rights)."""
    steps = [
        Step("Windows Time service: start automatically at boot", ("sc.exe", "config", "w32time", "start=", "auto")),
        # Fails harmlessly when it is already running (error 1056); w32tm /config below needs it running.
        Step("Start the Windows Time service now", ("sc.exe", "start", "w32time"), may_fail=True),
    ]
    if time_server is not None:
        host = validate_time_server(time_server)
        steps += [
            Step(f"Synchronise the clock from the local time server {host}",
                 ("w32tm", "/config", f"/manualpeerlist:{host},{NTP_PEER_FLAGS}", "/syncfromflags:manual", "/update")),
            # A server that is not reachable yet must not abort the installation; status shows T-002.
            Step("Ask Windows to synchronise now", ("w32tm", "/resync", "/nowait"), may_fail=True),
        ]
    return steps


def uninstall_plan(service_name: str = SERVICE_NAME) -> list[Step]:
    """Removes the service registration only. Recordings, the ledger, logs, configuration,
    credentials and folder permissions are deliberately left untouched."""
    return [
        Step("Stop the service (finalises the current segment)", ("sc.exe", "stop", service_name), may_fail=True),
        Step("Wait until it has stopped (up to 3 minutes)", action="wait_stopped",
             params={"service": service_name, "timeout_s": 180}),
        Step("Remove the service registration (recordings and data are NOT deleted)",
             ("sc.exe", "delete", service_name)),
    ]
