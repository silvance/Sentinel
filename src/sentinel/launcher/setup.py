"""First-run setup, for the installing technician.

Operators never see these questions: when Sentinel is not set up, the operator screen shows
error C-002 ("not set up yet ... contact technical support") and only a technician who types
SETUP continues. The questions are limited to what the technician knows at install time:
the camera's address, its stream path (from the camera's documentation — never guessed,
docs/02 risk A1), its recording account, a label, the display time zone and where recordings
go. Everything else keeps the tested defaults of ``sentinel.config``.

Credentials are written to a separate file, never into the configuration (docs/07). On Windows
the file is created with a protected DACL (setup account, SYSTEM, Administrators) — it is never
readable by other accounts, not even briefly; any failure aborts setup and deletes the file.
The configuration and storage folders are created private the same way, so evidence never
inherits ``%ProgramData%``'s "Users may read and create" permissions. On Linux: 0600 / 0750.
On Windows the credentials file's content is DPAPI-protected (machine scope), the format of the
Windows-service branch, so a later service install reads the same file (integration).
"""

from __future__ import annotations

import getpass
import ipaddress
import logging
import os
import re
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

# Owner: drives are swapped every 3-5 days; the longer end is the horizon for alert L-001.
DEPLOYED_SWAP_DAYS = 5.0

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
    camera_later: bool = False              # prepare everything now; "set-camera" adds the camera on site


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
    if ans.camera_later and (ans.simulated_url or ans.username):
        raise SetupError("A camera that is added later has no address or account yet.")
    camera: dict[str, Any] = {"id": "CAM01", "name": ans.camera_name.strip() or "Camera 1",
                              "url": "" if ans.camera_later else camera_url(ans), "transport": "tcp",
                              "include_audio": False}
    if ans.camera_later:
        camera["pending"] = True
    if ans.simulated_url:
        camera.update({"input_format": "mpegts", "development_source": True})
    if ans.username:
        camera["credentials_file"] = str(credentials_file or layout.credentials_file)
    if ans.storage_root is not None:
        root = Path(ans.storage_root)
        storage = {"spool_dir": str(root / "spool"), "data_dir": str(root / "data"), "log_dir": str(root / "logs")}
        if layout.status_dir is not None:   # Windows service layout: operator-readable status folder
            storage["status_dir"] = str(root / "status")
    else:
        storage = layout.storage_defaults()
    return {"camera": camera, "recording": {"display_timezone": check_timezone(ans.timezone)}, "storage": storage,
            # Owner decision 2026-11-10: a deployed recorder copies to the archive drive and, when its
            # own storage runs low, removes recordings already verified on an archive drive that has
            # been taken away (drives swapped every 3-5 days). The code default stays OFF, so only a
            # configuration written here (or by an administrator) enables removal.
            "archive": {"enabled": True},
            "retention": {"enabled": True, "expected_swap_days": DEPLOYED_SWAP_DAYS}}


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
# Names setup may find in a folder it is asked to protect (a previous Sentinel installation).
_OWN_NAMES = ("spool", "data", "logs", "status", "config", "sentinel.toml", "camera.secret", "recorder.lock",
              "launcher.lock")


def _is_own(name: str) -> bool:
    return name in _OWN_NAMES or name.startswith(("sentinel.toml.", "camera.secret."))


def prepare_private_dir(path: Path) -> None:
    """Create ``path`` private to this account (+ SYSTEM/Administrators on Windows, 0750 on Linux),
    or make an existing one private. An existing folder is only re-protected when it is empty or
    holds nothing but Sentinel's own files; otherwise setup stops (we never re-permission a
    folder full of someone else's data). Any failure raises SetupError."""
    path = Path(path)
    try:
        if sys.platform == "win32":
            from ..platform import winacl

            sid = winacl.current_user_sid()
            if not path.exists():
                winacl.create_private_dir(path, sid)
            elif winacl.foreign_trustees(winacl.get_sddl(path), sid) or not winacl.get_sddl(path).startswith("D:P"):
                _check_reprotectable(path)
                winacl.protect_existing(path, sid)
            sddl = winacl.get_sddl(path)
            if winacl.foreign_trustees(sddl, sid) or not sddl.startswith("D:P"):
                raise SetupError(f"Could not make {path} private (permissions now {sddl}).")
        else:
            if not path.exists():
                path.mkdir(parents=True, mode=0o750)
            st = path.stat()
            if st.st_uid != os.geteuid():
                raise SetupError(f"{path} belongs to another account; choose another folder.")
            if st.st_mode & 0o027:
                _check_reprotectable(path)
            os.chmod(path, 0o750)
    except OSError as exc:
        raise SetupError(f"Could not create or protect the folder {path}: {exc}") from None


def _check_reprotectable(path: Path) -> None:
    foreign = [p.name for p in path.iterdir() if not _is_own(p.name)]
    if foreign:
        raise SetupError(f"The folder {path} already contains other files ({', '.join(sorted(foreign)[:5])}) "
                         "and other accounts can access it. Choose an empty or new folder.")


def secret_problems(path: Path) -> list[str]:
    """Why the credentials file is not private (empty = only this account/SYSTEM/Administrators)."""
    if sys.platform == "win32":
        from ..platform import winacl

        sddl = winacl.get_sddl(path)
        problems = [f"also accessible to {t}" for t in winacl.foreign_trustees(sddl, winacl.current_user_sid())]
        if not sddl.startswith("D:P"):
            problems.append("inherits permissions from its folder")
        return problems
    mode = path.stat().st_mode & 0o777
    return [] if mode == 0o600 else [f"mode {oct(mode)} instead of 0600"]


def write_secret(path: Path, username: str, password: str) -> None:
    """Write the credentials file so that it is private from the moment it exists.

    Windows: created with CreateFileW and an explicit protected DACL (this account, SYSTEM,
    Administrators) under a temporary name, verified, then renamed over the target (a rename
    keeps the file's own DACL). Linux: O_CREAT|O_EXCL with mode 0600, verified, renamed.
    Any failure is fatal and leaves no plaintext behind.

    Integration (one secret format for setup and the Windows service, docs/INTEGRATION.md): on
    Windows the *content* is the windows-service branch's DPAPI format (machine scope, the same
    bytes ``python -m sentinel.winsvc set-camera-credentials`` writes), so the service can read
    the file setup wrote and the plaintext warning does not apply. The file is read back and
    decrypted before it replaces the target. Linux keeps the plain TOML (mode 0600)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.partial")
    replaced = False
    try:
        if sys.platform == "win32":
            from ..credentials import CredentialError, load_credentials
            from ..platform import winacl
            from ..winsvc.credstore import protected_file_bytes

            try:
                data = protected_file_bytes(username, password, scope="machine")
            except (CredentialError, OSError) as exc:
                raise SetupError(f"The camera password could not be protected with Windows DPAPI: {exc}") from None
            winacl.write_private_file(tmp, data)
            try:
                back = load_credentials(tmp)
            except CredentialError as exc:
                raise SetupError(f"The protected credentials file could not be read back: {exc}") from None
            if back.username != username or back.password != password or back.protection != "dpapi":
                raise SetupError("The protected credentials file did not decrypt to the values entered.")
        else:
            data = credentials_toml(username, password).encode("utf-8")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        problems = secret_problems(tmp)
        if problems:
            raise SetupError("The credentials file could not be made private: " + "; ".join(problems))
        os.replace(tmp, path)
        replaced = True
        problems = secret_problems(path)
        if problems:
            raise SetupError("The credentials file could not be made private: " + "; ".join(problems))
    except BaseException as exc:
        for p in (tmp, path) if replaced else (tmp,):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.error("could not delete %s after a failed credentials write; delete it by hand", p)
        if isinstance(exc, SetupError):
            raise
        if isinstance(exc, OSError):
            raise SetupError(f"Could not write the credentials file: {exc}") from None
        raise


def write_setup(ans: SetupAnswers, layout: Layout, config_path: Path | None = None,
                force: bool = False, clock: Callable[[], float] = time.time) -> tuple[Path, list[str]]:
    """Validate and write configuration (+ credentials). Returns (config path, warnings).

    Order: validate everything; make the configuration folder and the storage folders private;
    write the credentials file (private from creation); only then back up any previous
    configuration and write the new one (inheriting the private folder's permissions)."""
    config_path = Path(config_path or layout.config_file)
    d = build_config(ans, layout, config_path.parent / SECRET_NAME)
    try:
        load_config_dict(d, base_dir=config_path.parent)
    except ConfigError as exc:
        raise SetupError(f"The answers do not form a valid configuration: {exc}") from None
    if config_path.exists() and not force:
        raise SetupError("Sentinel is already set up on this computer (use setup --force to replace it).")
    for folder in dict.fromkeys([config_path.parent, *(Path(d["storage"][k]) for k in
                                                       ("spool_dir", "data_dir", "log_dir", "status_dir")
                                                       if k in d["storage"])]):
        prepare_private_dir(folder)
    if ans.username:
        write_secret(Path(d["camera"]["credentials_file"]), ans.username, ans.password)
    if config_path.exists():
        backup = config_path.with_name(f"{config_path.name}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(clock()))}.bak")
        config_path.replace(backup)
        log.info("previous configuration kept as %s", backup)
    header = ("Sentinel configuration written by first-run setup.\n"
              "Settings not listed here use the tested defaults (docs/08_PACKAGING.md).\n"
              "[archive] and [retention] are ON by owner decision (2026-11-10): recordings already\n"
              "verified on a REMOVED archive drive are deleted from this computer when it runs low\n"
              "on space (docs/branches/design-local-retention.md). Set [retention] enabled = false\n"
              "to keep every recording on this computer.\n"
              "Camera credentials are kept in the separate credentials file, never here.")
    if ans.camera_later:
        header += ("\nNO CAMERA ADDED YET ([camera] pending = true): nothing is recorded until the\n"
                   "technician runs 'Sentinel set-camera' on site.")
    tmp = config_path.with_name(config_path.name + ".partial")
    tmp.write_text(to_toml(d, header), encoding="utf-8")
    tmp.replace(config_path)
    return config_path, []

# ------------------------------------------------------------------ interactive questions
TECHNICIAN_INTRO = """\
Sentinel setup - for the installing technician

You will need: the camera's IP address, the stream path from the camera's documentation,
and the camera account the recorder should use (a viewer/least-privilege account).
Press Enter to accept the value shown in [brackets].
"""


CAMERA_LATER_INTRO = """\
Sentinel setup - for the installing technician (camera added later)

Everything except the camera is set up now. Nothing is recorded, and the screen shows
"Not recording: camera not added yet" (P-002), until you run  Sentinel.exe set-camera  on site.
Press Enter to accept the value shown in [brackets].
"""


def make_question(ask: Callable[[str], str], out: Callable[[str], None]) -> Callable[..., str]:
    def question(prompt: str, default: str = "", check: Callable[[str], Any] | None = None) -> str:
        while True:
            raw = ask(f"{prompt}{f' [{default}]' if default else ''}: ").strip() or default
            try:
                if check is not None:
                    check(raw)
                return raw
            except SetupError as exc:
                out(f"  {exc}")
    return question


def ask_camera(question: Callable[..., str], ask_secret: Callable[[str], str], host: str = "",
               path: str = "", port: int = 554) -> tuple[str, str, int, str, str]:
    """The camera questions (setup and set-camera): host, stream path, port, user, password."""
    host = question("Camera IP address or host name", host, check=check_host)
    path = question("Camera stream path (from the camera documentation)", path, check=check_stream_path)
    port = int(question("Camera stream port", str(port), check=_check_port))
    user = question("Camera account user name (empty if none)")
    pw = ask_secret("Camera account password (not shown): ") if user else ""
    return host, path, port, user, pw


def default_secret_prompt(ask: Callable[[str], str]) -> Callable[[str], str]:
    return getpass.getpass if sys.stdin is not None and sys.stdin.isatty() else ask


def interactive_setup(layout: Layout, ask: Callable[[str], str] = input,
                      ask_secret: Callable[[str], str] | None = None,
                      out: Callable[[str], None] = print, simulated_url: str | None = None,
                      camera_later: bool = False) -> SetupAnswers:
    if ask_secret is None:
        ask_secret = default_secret_prompt(ask)
    out(CAMERA_LATER_INTRO if camera_later else TECHNICIAN_INTRO)
    question = make_question(ask, out)

    if simulated_url:
        out(f"DEVELOPMENT: using the simulated camera {simulated_url}; recordings are not evidence.")
        host, path, port, user, pw = "", "", 554, "", ""
    elif camera_later:
        host, path, port, user, pw = "", "", 554, "", ""
    else:
        host, path, port, user, pw = ask_camera(question, ask_secret)
    name = question("Camera label for reports", "Camera 1")
    tz = question("Local time zone (IANA name)", "UTC", check=check_timezone)
    default_root = layout.spool_dir.parent
    root = question("Folder for recordings and the recording database", str(default_root))
    return SetupAnswers(camera_host=host, stream_path=path, port=port, username=user, password=pw,
                        camera_name=name, timezone=tz,
                        storage_root=None if Path(root) == default_root else Path(root),
                        simulated_url=simulated_url, camera_later=camera_later and not simulated_url)


def _check_port(raw: str) -> None:
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise SetupError("The port must be a number between 1 and 65535.")
