"""Command line for the Windows service (administrators only). See the package docstring."""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import SERVICE_NAME
from .commands import (InstallInputs, PlanError, Step, host_command, install_plan, render, secret_file_acl_argv,
                       uninstall_plan)

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NOT_ADMIN = 0, 1, 2, 5

# What the recorder's exit codes mean (sentinel.service), for `status`.
EXIT_MEANINGS = {0: "stopped normally", 1: "unexpected error", 2: "configuration or startup problem "
                 "(run: python -m sentinel.service check-config)", 3: "another recorder instance was running",
                 4: "recording supervisor failed repeatedly", 5: "watchdog: the recording loop stopped responding",
                 6: "did not stop within the stop deadline"}


# ---------------------------------------------------------------------- built-in actions
def _act_set_preshutdown_timeout(service: str, timeout_ms: int) -> None:
    from .scm import set_preshutdown_timeout

    set_preshutdown_timeout(service, timeout_ms)


def _act_set_service_environment(service: str, entries: list[str]) -> None:
    import winreg

    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{service}", 0,
                         winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, entries)
    finally:
        winreg.CloseKey(key)


def _act_make_dirs(paths: list[str]) -> None:
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)


def _act_wait_stopped(service: str, timeout_s: float) -> None:
    from .scm import SERVICE_STOPPED, wait_for_state

    info = wait_for_state(service, SERVICE_STOPPED, timeout_s)
    if info is not None and info.state != SERVICE_STOPPED:
        raise RuntimeError(f"service still {info.state_name} after {timeout_s:.0f} s")


_ACTIONS: dict[str, Callable[..., None]] = {
    "set_preshutdown_timeout": _act_set_preshutdown_timeout,
    "set_service_environment": _act_set_service_environment,
    "make_dirs": _act_make_dirs,
    "wait_stopped": _act_wait_stopped,
}


def execute(steps: list[Step], run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
            out: Callable[[str], None] = print) -> bool:
    for i, step in enumerate(steps, 1):
        out(f"[{i}/{len(steps)}] {step.description}")
        try:
            if step.argv is not None:
                cp = run(list(step.argv), capture_output=True, text=True, timeout=120, check=False)
                ok = cp.returncode == 0
                detail = ((cp.stdout or "") + (cp.stderr or "")).strip()
            else:
                _ACTIONS[str(step.action)](**step.params)
                ok, detail = True, ""
        except Exception as exc:  # report and stop; never continue past a failed required step
            ok, detail = False, f"{exc.__class__.__name__}: {exc}"
        if ok:
            continue
        if step.may_fail:
            out(f"      (ignored: {detail.splitlines()[-1] if detail else 'failed'})")
            continue
        out(f"      FAILED: {detail}")
        out(f"      command: {render(step)}")
        return False
    return True


def _print_plan(steps: list[Step]) -> None:
    print("Commands (run in an elevated Command Prompt, in this order):")
    for i, s in enumerate(steps, 1):
        print(f"  {i:2}. {s.description}")
        print(f"      {render(s)}")


# ---------------------------------------------------------------------- preflight
def preflight(cfg_path: Path) -> tuple[InstallInputs | None, list[str], list[str]]:
    """Return (inputs, errors, warnings). Errors mean the installed service could not work."""
    from ..config import ConfigError, load_config
    from ..platform import winacl

    errors: list[str] = []
    warnings: list[str] = []
    cfg_path = cfg_path.resolve()
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        return None, [f"configuration: {exc}"], []
    host = host_command(cfg_path)
    if cfg.camera.development_source:
        warnings.append("the configuration uses a TEST camera (development_source = true)")
    for label, exe in (("ffmpeg", cfg.tools.ffmpeg), ("ffprobe", cfg.tools.ffprobe)):
        if not Path(exe).is_absolute():
            errors.append(f"[tools] {label} must be a full path: the service does not see your personal PATH")
        elif not Path(exe).is_file():
            errors.append(f"[tools] {label} not found: {exe}")
    creds = Path(cfg.camera.credentials_file) if cfg.camera.credentials_file else None
    if creds is not None and not creds.exists():
        warnings.append(f"credentials file {creds} does not exist yet; run set-camera-credentials")
    svc_sid = winacl.service_sid(SERVICE_NAME)
    readers = {svc_sid, winacl.SID_SERVICE, winacl.SID_AUTHENTICATED_USERS, winacl.SID_USERS, winacl.SID_EVERYONE}
    must_read = [host.interpreter, *host.read_paths, str(cfg_path)]
    must_read += [e for e in (cfg.tools.ffmpeg, cfg.tools.ffprobe) if Path(e).is_absolute() and Path(e).exists()]
    for p in must_read:
        try:
            dacl = winacl.read_dacl(Path(p))
        except (winacl.AclError, OSError) as exc:
            warnings.append(f"could not check whether the service account can read {p}: {exc}")
            continue
        if not any(winacl.effective_allow(dacl, s, winacl.FILE_READ_DATA | winacl.GENERIC_READ
                                          | winacl.GENERIC_ALL) for s in readers):
            errors.append(f"the service account cannot read {p} (install Python, Sentinel and FFmpeg "
                          "under C:\\Program Files, not in a personal user folder)")
    if not cfg.storage.status_dir:
        errors.append("[storage] status_dir is not set: operators could not read the recorder status "
                      "(use a separate folder, e.g. C:\\ProgramData\\Sentinel\\status)")
    errors += [f"code could be changed by a non-administrator: {p}" for p in code_write_problems(cfg, cfg_path, host)]
    inp = InstallInputs(cfg_path, (cfg.spool_path, cfg.data_path, cfg.log_path),
                        creds if creds is not None and creds.exists() else None, host,
                        status_dir=cfg.status_dir_path if cfg.storage.status_dir else None)
    return inp, errors, warnings


def code_write_problems(cfg: object, cfg_path: Path, host: object, recursive: bool = True,
                        scan: Callable[..., list[str]] | None = None) -> list[str]:
    """Interpreter / PYTHONPATH / package / FFmpeg folders (recursively) and the configuration file
    and folder must not be writable by non-administrators (see codeacl). ``recursive=False`` is the
    cheap variant run at every service start."""
    from .codeacl import scan as default_scan

    tools = [Path(t) for t in (cfg.tools.ffmpeg, cfg.tools.ffprobe) if Path(t).is_absolute()]  # type: ignore[attr-defined]
    roots = [Path(p) for p in host.read_paths] + sorted({t.parent for t in tools})  # type: ignore[attr-defined]
    singles = [Path(host.interpreter), cfg_path, cfg_path.parent, *tools]  # type: ignore[attr-defined]
    singles = [p for p in singles if p.exists()]
    roots = [r for r in roots if r.exists()]
    run = scan or default_scan
    return run(singles, roots) if recursive else run(singles + roots, [])


# ---------------------------------------------------------------------- commands
def cmd_install(args: argparse.Namespace) -> int:
    if sys.platform != "win32" and not args.dry_run:
        print("install is for Windows; on Linux use packaging/systemd/sentinel-recorder.service")
        return EXIT_USAGE
    inp, errors, warnings = preflight(Path(args.config))
    for w in warnings:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"PROBLEM: {e}")
    if inp is None:
        return EXIT_FAILED
    try:
        steps = install_plan(InstallInputs(inp.config_path, inp.data_dirs, inp.credentials_path, inp.host,
                                           start=not args.no_start, status_dir=inp.status_dir,
                                           status_readers=args.status_readers))
    except PlanError as exc:
        print(f"PROBLEM: {exc}")
        return EXIT_FAILED
    if args.dry_run:
        _print_plan(steps)
        print("DRY RUN: nothing was changed.")
        return EXIT_OK if not errors else EXIT_FAILED
    if errors:
        print("Installation refused: fix the problems above first (nothing was changed).")
        return EXIT_FAILED
    from .scm import is_admin, query_service

    if not is_admin():
        print("Installation needs administrator rights. Open 'Command Prompt' with 'Run as administrator' "
              "and run this command again (nothing was changed).")
        return EXIT_NOT_ADMIN
    if query_service(SERVICE_NAME) is not None:
        print(f"The service {SERVICE_NAME} is already installed. Run 'uninstall' first to reinstall.")
        return EXIT_FAILED
    ok = execute(steps)
    print("Installed." if ok else "Installation stopped at the failed step above.")
    return EXIT_OK if ok else EXIT_FAILED


def cmd_uninstall(args: argparse.Namespace) -> int:
    steps = uninstall_plan()
    if args.dry_run:
        _print_plan(steps)
        print("DRY RUN: nothing was changed.")
        return EXIT_OK
    from .scm import is_admin, query_service

    if query_service(SERVICE_NAME) is None:
        print(f"The service {SERVICE_NAME} is not installed.")
        return EXIT_OK
    if not is_admin():
        print("Removing the service needs administrator rights (nothing was changed).")
        return EXIT_NOT_ADMIN
    ok = execute(steps)
    print("Service removed. Recordings, logs, configuration and credentials were NOT deleted." if ok
          else "Removal stopped at the failed step above.")
    return EXIT_OK if ok else EXIT_FAILED


def _dir_report(path: Path, svc_sid: str) -> str:
    from ..platform import winacl

    try:
        dacl = winacl.read_dacl(path)
    except (winacl.AclError, OSError) as exc:
        return f"cannot read permissions ({exc})"
    allowed = {winacl.SID_SYSTEM, winacl.SID_ADMINISTRATORS, svc_sid}
    probs = winacl.disallowed_grants(dacl, allowed)
    can_write = winacl.effective_allow(dacl, svc_sid, winacl.FILE_WRITE_DATA)
    parts = []
    if probs:
        parts.append("WIDER THAN EXPECTED: " + "; ".join(probs))
    if not can_write:
        parts.append("the service account has no write access")
    return "; ".join(parts) or "restricted to SYSTEM, Administrators and the service (OK)"


def _status_dir_report(path: Path, svc_sid: str) -> str:
    """Operators may read the status folder; only SYSTEM, Administrators and the service may write."""
    from ..platform import winacl
    from .codeacl import WRITE_RIGHTS

    try:
        dacl = winacl.read_dacl(path)
    except (winacl.AclError, OSError) as exc:
        return f"cannot read permissions ({exc})"
    writers = winacl.disallowed_grants(dacl, {winacl.SID_SYSTEM, winacl.SID_ADMINISTRATORS, svc_sid}, WRITE_RIGHTS)
    readers = {a.sid for a in dacl.aces if a.allows and not a.inherit_only and a.mask & winacl.FILE_READ_DATA}
    readers -= {winacl.SID_SYSTEM, winacl.SID_ADMINISTRATORS, svc_sid}
    if writers:
        return "WRITABLE BY OTHERS: " + "; ".join(writers)
    return f"OK (read access for {len(readers)} other principal(s): {', '.join(sorted(readers)) or 'none'})"


def cmd_status(args: argparse.Namespace) -> int:
    from ..platform import winacl
    from ..platform.windows import WindowsPlatform, query_w32time
    from .scm import SERVICE_RUNNING, query_service

    healthy = True
    info = query_service(SERVICE_NAME)
    if info is None:
        print(f"Service {SERVICE_NAME}: NOT INSTALLED")
        healthy = False
    else:
        print(f"Service {SERVICE_NAME}: {info.state_name} (start: {info.start_type_name}, account: {info.account}, "
              f"pid: {info.pid or '-'})")
        print(f"  command: {info.binary_path}")
        if info.state != SERVICE_RUNNING:
            healthy = False
            code = info.service_exit_code if info.win32_exit_code == 1066 else info.win32_exit_code
            print(f"  last exit: {code} ({EXIT_MEANINGS.get(code, 'see the recorder log')})")
    ts = query_w32time()
    print(f"Windows time synchronisation: {'yes' if ts.synchronized else 'NO' if ts.synchronized is False else 'unknown'}"
          f" - {ts.reason}")
    if args.config:
        from ..config import ConfigError, load_config
        from ..credentials import CredentialError, load_credentials
        from ..service import banner
        from ..status import read_status

        try:
            cfg = load_config(args.config)
        except ConfigError as exc:
            print(f"Configuration: PROBLEM: {exc}")
            return EXIT_FAILED
        svc_sid = winacl.service_sid(SERVICE_NAME)
        for label, p in (("spool", cfg.spool_path), ("data", cfg.data_path), ("logs", cfg.log_path)):
            print(f"Folder {label} {p}: {_dir_report(p, svc_sid) if p.exists() else 'MISSING'}")
        sd = cfg.status_dir_path
        if not cfg.storage.status_dir:
            print("Folder status: NOT SET ([storage] status_dir) - operators cannot read the status")
        else:
            print(f"Folder status {sd}: {_status_dir_report(sd, svc_sid) if sd.exists() else 'MISSING'}")
        if cfg.camera.credentials_file:
            cp = Path(cfg.camera.credentials_file)
            try:
                c = load_credentials(cp)
                kind = "DPAPI-protected" if c.protection == "dpapi" else "PLAIN TEXT (run set-camera-credentials)"
                probs = WindowsPlatform().secret_file_problems(cp)
                print(f"Camera credentials: {kind}; permissions: {'; '.join(probs) if probs else 'OK'}")
            except CredentialError as exc:
                print(f"Camera credentials: PROBLEM: {exc}")
                healthy = False
        doc, problem = read_status(cfg.status_path)
        print(banner(doc, problem))
        healthy = healthy and doc is not None and not problem
    return EXIT_OK if healthy else EXIT_FAILED


def cmd_set_credentials(args: argparse.Namespace, prompt: Callable[[str], str] = input,
                        secret_prompt: Callable[[str], str] = getpass.getpass) -> int:
    from ..config import ConfigError, load_config
    from ..credentials import CredentialError
    from ..platform import winacl
    from .credstore import write_protected_credentials

    if sys.platform != "win32":
        print("DPAPI protection is Windows-only; on Linux use a 0600 credentials file (docs/07).")
        return EXIT_USAGE
    target = Path(args.file) if args.file else None
    if target is None:
        try:
            cfg = load_config(args.config)
        except ConfigError as exc:
            print(f"configuration error: {exc}")
            return EXIT_FAILED
        if not cfg.camera.credentials_file:
            print("The configuration has no [camera] credentials_file. Add for example\n"
                  '  credentials_file = "C:\\\\ProgramData\\\\Sentinel\\\\config\\\\camera.dpapi"\n'
                  "and run this command again.")
            return EXIT_FAILED
        target = Path(cfg.camera.credentials_file)
    username = prompt("Camera user name (the recorder's view-only camera account): ").strip()
    password = secret_prompt("Camera password (not shown): ")
    again = secret_prompt("Camera password again: ")
    if password != again:
        print("The two passwords are different. Nothing was saved.")
        return EXIT_FAILED
    if not username or not password:
        print("User name and password must not be empty. Nothing was saved.")
        return EXIT_FAILED
    svc_sid = winacl.service_sid(SERVICE_NAME)
    acl = None if args.no_acl else (lambda p: secret_file_acl_argv(p, svc_sid))
    try:
        write_protected_credentials(target, username, password, scope=args.scope, acl_argv=acl)
    except (CredentialError, OSError) as exc:
        print(f"Could not save the camera credentials: {exc}")
        if not args.no_acl and not _is_admin_safe():
            print("Saving into the protected configuration folder needs 'Run as administrator'.")
        return EXIT_FAILED
    finally:
        password = again = ""   # drop references early (Python cannot guarantee wiping)
    print(f"Camera credentials saved, protected with Windows DPAPI ({args.scope} scope), and verified.")
    if args.no_acl:
        print("WARNING: --no-acl: the file keeps the permissions of its folder (development only).")
    print("Restart the service for the change to take effect: sc.exe stop SentinelRecorder, then sc.exe start "
          "SentinelRecorder")
    return EXIT_OK


def _is_admin_safe() -> bool:
    try:
        from .scm import is_admin

        return is_admin()
    except OSError:
        return False


def cmd_host(args: argparse.Namespace) -> int:
    """Entry point the SCM starts (binPath). Not for interactive use."""
    from .scm import CtypesScmApi, NotStartedByScm, ServiceHost

    config = args.config

    def run_recorder(host: ServiceHost) -> int:
        return run_recorder_in_host(host, config)

    host = ServiceHost(SERVICE_NAME, run_recorder, CtypesScmApi())
    try:
        host.dispatch()
    except NotStartedByScm:
        if sys.stderr is not None:
            print("This command is started by Windows as the 'Sentinel Recorder' service. To run the recorder "
                  "in a console use: python -m sentinel.service run --config ...", file=sys.stderr)
        return EXIT_USAGE
    return host.exit_code if host.exit_code is not None else EXIT_FAILED


def _raw_storage_paths(config: str) -> tuple[str, Path | None, Path | None]:
    """Best effort for a configuration that failed validation: (camera id, status file, log dir)
    taken from the raw TOML, so the failure can still be shown to operators and logged."""
    import tomllib

    base = Path(config).resolve().parent
    try:
        with open(config, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return "unknown", None, None

    def path(v: object) -> Path | None:
        if not isinstance(v, str) or not v:
            return None
        p = Path(v)
        return p if p.is_absolute() else (base / p)
    sto = raw.get("storage") if isinstance(raw.get("storage"), dict) else {}
    cam = raw.get("camera") if isinstance(raw.get("camera"), dict) else {}
    status_dir = path(sto.get("status_dir")) or path(sto.get("data_dir"))
    camera_id = cam.get("id") if isinstance(cam.get("id"), str) else "unknown"
    return camera_id, (status_dir / "status.json") if status_dir else None, path(sto.get("log_dir"))


def _startup_failure(message: str, camera_id: str, status_file: Path | None, log_dir: Path | None,
                     event_log: Callable[[str], object]) -> None:
    """Make a failure before the recorder exists visible: Windows event log, the recorder log
    (if its folder is known) and a *terminal* C-001 status, so operators see "Recording stopped.
    Error C-001 …" instead of a stale X-001. Each step is best effort."""
    import logging

    from ..logging_setup import setup_logging
    from ..status import fatal_status_document, write_status

    event_log(f"Sentinel Recorder cannot start: {message}")
    if log_dir is not None:
        try:
            setup_logging(log_dir, "INFO", to_stderr=False)
            logging.getLogger("sentinel.winsvc").critical("cannot start recording: %s", message)
        except Exception:  # noqa: BLE001
            pass
    if status_file is not None:
        try:
            from .. import __version__

            write_status(status_file, fatal_status_document(
                "C-001", camera_id, detail={"reason": message},
                service={"version": __version__, "platform": "windows", "windows_service": SERVICE_NAME}))
        except Exception:  # noqa: BLE001
            pass


def _default_event_log(message: str) -> object:
    from .eventlog import report_event

    return report_event(message)


def _running_code_problems(cfg: object, config: str) -> list[str]:
    from .commands import host_command

    return code_write_problems(cfg, Path(config).resolve(), host_command(Path(config).resolve()), recursive=False)


def run_recorder_in_host(host: object, config: str, recorder_factory: Callable | None = None,
                         event_log: Callable[[str], object] = _default_event_log,
                         code_check: Callable[[object, str], list[str]] | None = None) -> int:
    """Body of ServiceMain: load configuration, log to file only (a service has no console),
    refuse to run from code non-administrators can change, run the recorder until stopped.
    Separated so it can be tested with a fake SCM."""
    import logging

    from ..config import ConfigError, load_config
    from ..logging_setup import setup_logging
    from ..service import EXIT_CONFIG, RecorderService

    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _startup_failure(f"configuration error: {exc}", *_raw_storage_paths(config), event_log=event_log)
        return EXIT_CONFIG          # SCM restarts it every 60 s; each attempt is visible as above
    setup_logging(cfg.log_path, cfg.service.log_level, to_stderr=False)
    wlog = logging.getLogger("sentinel.winsvc")
    try:
        unsafe = (code_check or _running_code_problems)(cfg, config)
    except Exception as exc:  # noqa: BLE001 - a broken check must not silently pass
        unsafe = [f"the program-file permission check failed: {exc}"]
    if unsafe:
        for p in unsafe:
            wlog.critical("refusing to start: code could be changed by a non-administrator: %s", p)
        _startup_failure("program files can be changed by non-administrators (see the recorder log)",
                         cfg.camera.id, cfg.status_path, None, event_log)
        return EXIT_CONFIG
    svc = (recorder_factory or RecorderService)(cfg)

    def stop() -> None:
        if getattr(host, "system_shutdown", False) and hasattr(svc, "request_shutdown"):
            svc.request_shutdown()       # bounded drain inside the pre-shutdown window
        else:
            svc.request_stop()
    host.on_stop(stop)                    # type: ignore[attr-defined]
    host.report_running()                 # type: ignore[attr-defined]
    wlog.info("Sentinel recorder starting as Windows service %s (pid %d)", SERVICE_NAME, os.getpid())
    return svc.run()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sentinel.winsvc",
                                 description="Install and manage the Sentinel recorder Windows service.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="register the service (administrator)")
    p.add_argument("--config", required=True)
    p.add_argument("--dry-run", action="store_true", help="only print the commands")
    p.add_argument("--no-start", action="store_true", help="do not start the service after installing")
    p.add_argument("--status-readers", choices=("group", "users"), default="group",
                   help="who may read the status folder: the local group 'Sentinel Operators' (default) "
                        "or every local user")
    p = sub.add_parser("uninstall", help="remove the service registration (recordings are kept)")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("status", help="service, time synchronisation, permissions and recorder status")
    p.add_argument("--config")
    p = sub.add_parser("set-camera-credentials", help="store the camera login protected with DPAPI")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--config")
    g.add_argument("--file", help="write this file instead of the configured credentials_file")
    p.add_argument("--scope", choices=("machine", "user"), default="machine",
                   help="machine (default, required for the service) or user (development only)")
    p.add_argument("--no-acl", action="store_true", help="do not restrict file permissions (development only)")
    p = sub.add_parser("host", help=argparse.SUPPRESS)
    p.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status,
        "set-camera-credentials": cmd_set_credentials, "host": cmd_host}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
