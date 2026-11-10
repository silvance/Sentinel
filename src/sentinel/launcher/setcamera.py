"""``Sentinel set-camera``: add (or change) the camera on site, for the installing technician.

Setup can be done in advance with ``setup --camera-later``: the configuration then says
``[camera] pending = true``, the recorder records nothing and the status shows P-002. On site
this command asks only the camera questions, checks that the camera answers (unless
``--no-test``), saves the camera credentials (private file; with the Windows service installed,
readable by the service account too), replaces only the ``[camera]`` section of the configuration
(everything else, including comments and the recording schedule, is kept; the previous file is
kept as a dated ``.bak``), and restarts the recorder so the camera is used at once.

Credentials are never printed or logged; FFprobe's error text is shown with them removed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import AppConfig, ConfigError, load_config, load_config_dict
from ..credentials import Credentials, url_with_credentials
from ..paths import SECRET_NAME
from .setup import (SetupError, _toml_value, ask_camera, check_host, check_stream_path, default_secret_prompt,
                    make_question, write_secret)

EXIT_OK, EXIT_CONFIG, EXIT_RUNTIME = 0, 2, 4

_SECTION_RE = re.compile(r"^\s*\[{1,2}\s*([^\]]+?)\s*\]{1,2}\s*(?:#.*)?$")


class SetCameraError(Exception):
    """Shown to the technician; nothing was changed."""


# ---------------------------------------------------------------------- configuration text
def replace_camera_section(text: str, camera: dict[str, Any]) -> str:
    """``text`` with its ``[camera]`` table replaced by ``camera``; every other line is kept."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if (m := _SECTION_RE.match(ln)) and m.group(1) == "camera" and not ln.lstrip().startswith("[[")),
                 None)
    if start is None:
        raise SetCameraError("The configuration has no [camera] section.")
    end = next((i for i in range(start + 1, len(lines)) if _SECTION_RE.match(lines[i])), len(lines))
    # Keep comment/blank lines that sit directly above the next section with that section.
    while end - 1 > start and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    block = ["[camera]"] + [f"{k} = {_toml_value(v)}" for k, v in camera.items()]
    return "\n".join(lines[:start] + block + lines[end:]) + "\n"


def new_camera_table(old: dict[str, Any], url: str, credentials_file: Path | None, name: str) -> dict[str, Any]:
    keep = {k: v for k, v in old.items()
            if k not in ("url", "pending", "credentials_file", "development_source", "input_format", "name")}
    table: dict[str, Any] = {"id": keep.pop("id", "CAM01"), "name": name, "url": url}
    table.update(keep)
    table.setdefault("transport", "tcp")
    table.setdefault("include_audio", False)
    if credentials_file is not None:
        table["credentials_file"] = str(credentials_file)
    return table


def current_address(cfg: AppConfig) -> tuple[str, str, int]:
    if cfg.camera.pending or not cfg.camera.url.startswith("rtsp"):
        return "", "", 554
    parts = urlsplit(cfg.camera.url)
    return parts.hostname or "", parts.path.lstrip("/") + (f"?{parts.query}" if parts.query else ""), parts.port or 554


# ---------------------------------------------------------------------- camera test
def redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return re.sub(r"(rtsps?://)[^@/\s]*@", r"\1***@", text)


def test_camera(ffprobe: str, url: str, creds: Credentials | None, transport: str = "tcp",
                timeout_s: float = 30.0, run: Callable[..., Any] = subprocess.run) -> tuple[bool, str]:
    """Ask the camera for its video stream with FFprobe. (ok, what to tell the technician)."""
    argv = [ffprobe, "-v", "error", "-rtsp_transport", transport, "-timeout", str(int(15e6)),
            "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of",
            "default=noprint_wrappers=1", url_with_credentials(url, creds)]
    secrets = (creds.username, creds.password) if creds else ()
    kw: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout_s}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
    try:
        cp = run(argv, **kw)
    except subprocess.TimeoutExpired:
        return False, "The camera did not answer within 30 seconds."
    except OSError as exc:
        return False, f"FFprobe could not be run: {exc}"
    out = dict(ln.split("=", 1) for ln in (cp.stdout or "").splitlines() if "=" in ln)
    if cp.returncode == 0 and out.get("codec_name"):
        return True, f"The camera answered: {out.get('codec_name')} video, {out.get('width')}x{out.get('height')}."
    err = redact((cp.stderr or "").strip().splitlines()[-1] if (cp.stderr or "").strip() else "", *secrets)
    hint = ""
    if "401" in err or "Unauthorized" in err:
        hint = " Check the camera user name and password."
    elif "404" in err or "Not Found" in err:
        hint = " Check the stream path in the camera documentation."
    elif "timed out" in err.lower() or "Connection refused" in err or "No route" in err:
        hint = " Check the camera address, port, power and network cable."
    return False, f"The camera did not send video ({err or 'no answer'}).{hint}"


# ---------------------------------------------------------------------- credentials
def write_credentials(path: Path, username: str, password: str, service_installed: bool) -> None:
    if sys.platform == "win32" and service_installed:
        from ..platform import winacl
        from ..winsvc.commands import SERVICE_NAME, secret_file_acl_argv
        from ..winsvc.credstore import write_protected_credentials

        sid = winacl.service_sid(SERVICE_NAME)
        try:
            write_protected_credentials(path, username, password, scope="machine",
                                        acl_argv=lambda p: secret_file_acl_argv(p, sid))
        except OSError as exc:
            raise SetCameraError(f"Could not save the camera credentials: {exc}") from None
        except Exception as exc:     # CredentialError
            raise SetCameraError(f"Could not save the camera credentials: {exc}") from None
    else:
        try:
            write_secret(path, username, password)
        except SetupError as exc:
            raise SetCameraError(str(exc)) from None


# ---------------------------------------------------------------------- command
def set_camera(cfg_path: Path, ask: Callable[[str], str] = input, ask_secret: Callable[[str], str] | None = None,
               out: Callable[[str], None] = print, test: bool = True, restart: bool = True,
               tester: Callable[..., tuple[bool, str]] = test_camera,
               control_for: Callable[[Path, AppConfig], Any] | None = None,
               clock: Callable[[], float] = time.time) -> int:
    ask_secret = ask_secret or default_secret_prompt(ask)
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        out(f"The configuration cannot be read: {exc}")
        return EXIT_CONFIG
    text = cfg_path.read_text(encoding="utf-8")
    old = tomllib.loads(text).get("camera", {})
    if control_for is None:
        from .control import select_control as control_for
    control = control_for(cfg_path, cfg)
    service_installed = bool(getattr(control, "service_managed", False))

    out("Sentinel - add the camera (installing technician)\n")
    out("No camera has been added yet." if cfg.camera.pending else
        "A camera is already set up; the answers below replace it.")
    question = make_question(ask, out)
    host, path, port = current_address(cfg)
    try:
        host, path, port, user, pw = ask_camera(question, ask_secret, host, path, port)
        name = question("Camera label for reports", cfg.camera.name or "Camera 1")
    except (EOFError, KeyboardInterrupt):
        out("\nCancelled; nothing was changed.")
        return EXIT_CONFIG
    host, path = check_host(host), check_stream_path(path)
    url = f"rtsp://{f'[{host}]' if ':' in host else host}:{port}/{path}"
    creds = Credentials(user, pw) if user else None

    if test:
        out("\nChecking that the camera answers (up to 30 seconds)...")
        ok, msg = tester(cfg.tools.ffprobe, url, creds, cfg.camera.transport)
        out(msg)
        if not ok:
            try:
                go = ask("Save these camera settings anyway? Type YES to save, or press Enter to stop: ")
            except (EOFError, KeyboardInterrupt):
                go = ""
            if go.strip().upper() != "YES":
                out("Nothing was changed.")
                return EXIT_CONFIG

    cred_path = cfg_path.parent / SECRET_NAME if user else None
    table = new_camera_table(old, url, cred_path, name.strip() or "Camera 1")
    new_text = replace_camera_section(text, table)
    try:
        new_cfg = load_config_dict(tomllib.loads(new_text), base_dir=cfg_path.parent)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        out(f"The new camera settings do not form a valid configuration: {exc}\nNothing was changed.")
        return EXIT_CONFIG

    try:
        if cred_path is not None:
            write_credentials(cred_path, user, pw, service_installed)
        backup = cfg_path.with_name(f"{cfg_path.name}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(clock()))}.bak")
        tmp = cfg_path.with_name(cfg_path.name + ".partial")
        tmp.write_text(new_text, encoding="utf-8")
        cfg_path.replace(backup)
        tmp.replace(cfg_path)
    except SetCameraError as exc:
        out(f"{exc}\nThe configuration was not changed.")
        return EXIT_CONFIG
    except OSError as exc:
        out(f"Could not write the configuration: {exc}")
        if sys.platform == "win32":
            out("Changing the configuration of an installed service needs 'Run as administrator'.")
        return EXIT_CONFIG
    finally:
        pw = ""
    out(f"\nCamera saved in {cfg_path} (previous settings kept as {backup.name}).")

    if not restart:
        out("Restart the recorder so it uses the camera ('Sentinel stop', then start Sentinel).")
        return EXIT_OK
    control = control_for(cfg_path, new_cfg)
    if control.is_running():
        out("Restarting the recorder so it uses the camera...")
        if not control.stop(90.0):
            out("The recorder did not stop. Restart this computer, or run 'Sentinel stop' and start Sentinel.")
            return EXIT_RUNTIME
    result = control.start()
    if not getattr(result, "ok", False):
        out(f"The recorder could not be started ({getattr(result, 'code', '?')}). "
            "Start Sentinel, and run 'Sentinel check-config' if it still does not record.")
        return EXIT_RUNTIME
    if hasattr(control, "start_archive"):
        control.start_archive()
    out("The recorder is running with the camera. Open Sentinel and check that it shows 'Recording normally'.")
    return EXIT_OK


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-test", action="store_true", help="save without checking that the camera answers")
    p.add_argument("--no-restart", action="store_true", help="do not restart the recorder")
