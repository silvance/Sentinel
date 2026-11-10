"""First-run setup, for the installing technician.

Operators never see these questions: when Sentinel is not set up, the operator screen shows
error C-002 ("not set up yet ... contact technical support") and only a technician who types
SETUP continues. The questions are limited to what the technician knows at install time:
the camera's address, its stream path (from the camera's documentation — never guessed,
docs/02 risk A1), its recording account, a label, the display time zone and where recordings
go. Everything else keeps the tested defaults of ``sentinel.config``.

Credentials are written to a separate file, never into the configuration (docs/07). On Windows
the file is restricted to the setup account, SYSTEM and Administrators with ``icacls``; the
Windows-service branch replaces this file with DPAPI-protected storage. On Linux it is 0600.
"""

from __future__ import annotations

import getpass
import ipaddress
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import ConfigError, load_config_dict
from ..paths import SECRET_NAME, Layout

log = logging.getLogger(__name__)

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class SetupError(ValueError):
    """An answer cannot be used. Message is shown to the technician."""


@dataclass(frozen=True)
class SetupAnswers:
    camera_host: str
    stream_path: str
    port: int = 554
    username: str = ""
    password: str = ""
    camera_name: str = "Camera 1"
    timezone: str = "UTC"
    storage_root: Path | None = None        # None = the layout's default folders
    simulated_url: str | None = None        # developer use only: tcp:// simulator, marked SIMULATED


def check_host(host: str) -> str:
    host = host.strip().strip("[]")
    if not host:
        raise SetupError("The camera address is required.")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not _HOST_RE.fullmatch(host):
        raise SetupError("Enter only the camera's IP address or host name (no 'rtsp://', user name or path).")
    return host


def check_stream_path(path: str) -> str:
    path = path.strip().lstrip("/")
    if not path:
        raise SetupError("The stream path is required (see the camera's documentation).")
    if any(c.isspace() for c in path) or "@" in path or "://" in path:
        raise SetupError("The stream path must not contain spaces, '@' or '://'.")
    return path


def check_timezone(name: str) -> str:
    name = name.strip() or "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SetupError(f"Unknown time zone {name!r}; use a name like America/New_York or UTC.") from None
    return name


def camera_url(ans: SetupAnswers) -> str:
    if ans.simulated_url:
        return ans.simulated_url
    host = check_host(ans.camera_host)
    if ":" in host:
        host = f"[{host}]"
    return f"rtsp://{host}:{ans.port}/{check_stream_path(ans.stream_path)}"


def build_config(ans: SetupAnswers, layout: Layout, credentials_file: Path | None = None) -> dict[str, Any]:
    """Configuration dictionary for ``ans`` (validated by ``sentinel.config``)."""
    if not 1 <= ans.port <= 65535:
        raise SetupError("The camera port must be between 1 and 65535.")
    camera: dict[str, Any] = {"id": "CAM01", "name": ans.camera_name.strip() or "Camera 1",
                              "url": camera_url(ans), "transport": "tcp", "include_audio": False}
    if ans.simulated_url:
        camera.update({"input_format": "mpegts", "development_source": True})
    if ans.username:
        camera["credentials_file"] = str(credentials_file or layout.credentials_file)
    if ans.storage_root is not None:
        root = Path(ans.storage_root)
        storage = {"spool_dir": str(root / "spool"), "data_dir": str(root / "data"), "log_dir": str(root / "logs")}
    else:
        storage = layout.storage_defaults()
    return {"camera": camera, "recording": {"display_timezone": check_timezone(ans.timezone)}, "storage": storage}


# ------------------------------------------------------------------ TOML writing (stdlib has no writer)
def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        out = []
        for ch in v:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        return '"' + "".join(out) + '"'
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def to_toml(d: dict[str, dict[str, Any]], header: str = "") -> str:
    lines = [f"# {ln}" if ln else "#" for ln in header.splitlines()]
    for section, values in d.items():
        if lines:
            lines.append("")
        lines.append(f"[{section}]")
        for k, v in values.items():
            lines.append(f"{k} = {_toml_value(v)}")
    return "\n".join(lines) + "\n"


def credentials_toml(username: str, password: str) -> str:
    return f"username = {_toml_value(username)}\npassword = {_toml_value(password)}\n"


# ------------------------------------------------------------------ writing files
def current_user_sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         timeout=30, check=True, creationflags=0x08000000).stdout
    sid = out.strip().rsplit(",", 1)[-1].strip().strip('"')
    if not sid.startswith("S-1-"):
        raise OSError(f"could not determine the current account SID from {out!r}")
    return sid


def icacls_restrict_argv(path: Path, user_sid: str) -> list[str]:
    """Owner account, SYSTEM (S-1-5-18) and Administrators (S-1-5-32-544) only; no inheritance."""
    return ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{user_sid}:(F)", "*S-1-5-18:(F)",
            "*S-1-5-32-544:(F)"]


def write_secret(path: Path, username: str, password: str) -> list[str]:
    """Write the credentials file readable only by this account. Returns warnings (empty = OK)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = credentials_toml(username, password).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return []
    try:
        res = subprocess.run(icacls_restrict_argv(path, current_user_sid()), capture_output=True, text=True,
                             timeout=60, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"could not restrict access to the credentials file: {exc}"]
    if res.returncode != 0:
        return [f"could not restrict access to the credentials file (icacls exit {res.returncode})"]
    return []


def write_setup(ans: SetupAnswers, layout: Layout, config_path: Path | None = None,
                force: bool = False, clock: Callable[[], float] = time.time) -> tuple[Path, list[str]]:
    """Validate and write configuration (+ credentials). Returns (config path, warnings)."""
    config_path = Path(config_path or layout.config_file)
    d = build_config(ans, layout, config_path.parent / SECRET_NAME)
    try:
        load_config_dict(d, base_dir=config_path.parent)
    except ConfigError as exc:
        raise SetupError(f"The answers do not form a valid configuration: {exc}") from None
    if config_path.exists():
        if not force:
            raise SetupError("Sentinel is already set up on this computer (use setup --force to replace it).")
        backup = config_path.with_name(f"{config_path.name}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(clock()))}.bak")
        config_path.replace(backup)
        log.info("previous configuration kept as %s", backup)
    warnings: list[str] = []
    if ans.username:
        warnings += write_secret(Path(d["camera"]["credentials_file"]), ans.username, ans.password)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    header = ("Sentinel configuration written by first-run setup.\n"
              "Settings not listed here use the tested defaults (docs/08_PACKAGING.md).\n"
              "Camera credentials are kept in the separate credentials file, never here.")
    tmp = config_path.with_name(config_path.name + ".partial")
    tmp.write_text(to_toml(d, header), encoding="utf-8")
    tmp.replace(config_path)
    return config_path, warnings


# ------------------------------------------------------------------ interactive questions
TECHNICIAN_INTRO = """\
Sentinel setup - for the installing technician

You will need: the camera's IP address, the stream path from the camera's documentation,
and the camera account the recorder should use (a viewer/least-privilege account).
Press Enter to accept the value shown in [brackets].
"""


def interactive_setup(layout: Layout, ask: Callable[[str], str] = input,
                      ask_secret: Callable[[str], str] | None = None,
                      out: Callable[[str], None] = print, simulated_url: str | None = None) -> SetupAnswers:
    if ask_secret is None:
        ask_secret = getpass.getpass if sys.stdin is not None and sys.stdin.isatty() else ask
    out(TECHNICIAN_INTRO)

    def question(prompt: str, default: str = "", check: Callable[[str], Any] | None = None) -> str:
        while True:
            raw = ask(f"{prompt}{f' [{default}]' if default else ''}: ").strip() or default
            try:
                if check is not None:
                    check(raw)
                return raw
            except SetupError as exc:
                out(f"  {exc}")

    if simulated_url:
        out(f"DEVELOPMENT: using the simulated camera {simulated_url}; recordings are not evidence.")
        host, path, port, user, pw = "", "", 554, "", ""
    else:
        host = question("Camera IP address or host name", check=check_host)
        path = question("Camera stream path (from the camera documentation)", check=check_stream_path)
        port = int(question("Camera stream port", "554", check=_check_port))
        user = question("Camera account user name (empty if none)")
        pw = ask_secret("Camera account password (not shown): ") if user else ""
    name = question("Camera label for reports", "Camera 1")
    tz = question("Local time zone (IANA name)", "UTC", check=check_timezone)
    default_root = layout.spool_dir.parent
    root = question("Folder for recordings and the recording database", str(default_root))
    return SetupAnswers(camera_host=host, stream_path=path, port=port, username=user, password=pw,
                        camera_name=name, timezone=tz,
                        storage_root=None if Path(root) == default_root else Path(root),
                        simulated_url=simulated_url)


def _check_port(raw: str) -> None:
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise SetupError("The port must be a number between 1 and 65535.")
