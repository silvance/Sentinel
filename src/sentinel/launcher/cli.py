"""``Sentinel.exe`` — the one program an operator starts.

    Sentinel.exe                    start: set up if needed, start the recorder if it is not
                                    running, then show its status (same as ``launch``)
    Sentinel.exe launch [--once] [--no-start] [--config FILE]

Technician / support commands (same executable):

    Sentinel.exe setup [--force] [--config FILE]       first-run setup questions
    Sentinel.exe recorder run [--config FILE]          run the recorder in this window
    Sentinel.exe archive run|swap|resume|status|designate ... --config FILE   archive-drive copy
    Sentinel.exe stop [--timeout S] [--config FILE]    graceful stop of the background recorder
    Sentinel.exe status [--json] | check-config | diagnostics [--out DIR] | schedule [--days N]
    Sentinel.exe about [--verify]                      version, paths, bundled tools, build manifest

The launcher never stops recording on its own and closing its window has no effect on the
recorder (U6). Configuration lookup and tool resolution: ``sentinel/paths.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from .. import __version__
from ..config import ConfigError, check_tools, load_config
from ..install_check import location_problems
from ..hashing import sha256_file
from ..logging_setup import FORMAT, RedactingFilter, RedactingFormatter
from ..paths import (CONFIG_ENV, MANIFEST_NAME, TOOL_NAMES, app_dir, bundled_tool, config_candidates,
                     default_layout, default_tool, find_config, is_frozen)
from ..platform import InstanceLockError, get_platform
from . import stopsignal
from .control import select_control
from .setup import SetupError, interactive_setup, write_setup
from .view import alert_banner, select_view, set_console_title

log = logging.getLogger("sentinel.launcher")

EXIT_OK, EXIT_CONFIG, EXIT_LOCKED, EXIT_RUNTIME = 0, 2, 3, 4
FORWARDED = ("status", "check-config", "diagnostics", "schedule")


def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _pause() -> None:
    """Keep a double-clicked console window open long enough to read an error."""
    if _interactive():
        try:
            input("\nPress Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass


def setup_launcher_logging(log_dir: Path) -> None:
    """Launcher has its own small log so it never rotates the recorder's log file under it."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_dir / "launcher.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    except OSError:
        return
    formatter = RedactingFormatter(FORMAT, datefmt="%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _resolve(explicit: str | None) -> Path | None:
    return find_config(explicit)


# ---------------------------------------------------------------------- commands
def cmd_launch(args: argparse.Namespace) -> int:
    set_console_title("Sentinel")
    layout = default_layout()
    cfg_path = _resolve(args.config)
    if cfg_path is None or not cfg_path.exists():
        print(alert_banner("C-002"))
        if args.once or not _interactive():
            return EXIT_CONFIG
        print("\nTechnician: type SETUP and press Enter to set up this computer.\n"
              "Everyone else: please close this window.")
        try:
            if input("> ").strip().upper() != "SETUP":
                return EXIT_CONFIG
        except (EOFError, KeyboardInterrupt):
            return EXIT_CONFIG
        rc = cmd_setup(argparse.Namespace(config=args.config, force=False, simulated_camera=None))
        if rc != EXIT_OK:
            _pause()
            return rc
        cfg_path = _resolve(args.config) or layout.config_file

    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        setup_launcher_logging(layout.log_dir)
        log.error("configuration %s is invalid: %s", cfg_path, exc)
        print(alert_banner("C-001"))
        _pause()
        return EXIT_CONFIG
    control = select_control(cfg_path, cfg)
    if getattr(control, "service_managed", False):
        # Integration: an installed Windows service runs the recorder. Its data, spool and log
        # folders are service-only, so this (operator) account neither checks nor writes them;
        # the service checks its own configuration at every start and reports C-001 in the
        # status folder, which is what this window shows.
        log.info("Sentinel %s launcher: recorder is managed by the %s", __version__, control.name)
        started_after = None
        start_problem = None
        if not args.no_start and not control.is_running():
            started_after = time.time()
            result = control.start()
            log.info("start via %s: ok=%s %s", control.name, result.ok, result.detail)
            if not result.ok:
                start_problem = result.code
        return select_view().run(cfg.status_path, cfg.camera.name, started_after=started_after,
                                 once=args.once, start_problem=start_problem)
    # Refuse before creating anything: configuration, credentials, tools and data folders must
    # not be changeable by other accounts; bundled tools must be present (no PATH fallback).
    problems = location_problems(cfg_path, cfg) + check_tools(cfg)
    if problems:
        if cfg.log_path.is_dir():
            setup_launcher_logging(cfg.log_path)
        for p in problems:
            log.error("refusing to start the recorder: %s", p)
        print(alert_banner("C-001"))
        if _interactive():
            print("\nTechnician: run 'Sentinel check-config' for details.")
        _pause()
        return EXIT_CONFIG
    setup_launcher_logging(cfg.log_path)
    log.info("Sentinel %s launcher (frozen=%s) using %s", __version__, is_frozen(), cfg_path)

    started_after: float | None = None
    start_problem: str | None = None
    try:
        launcher_lock = get_platform().acquire_instance_lock(cfg.data_path / "launcher.lock")
    except InstanceLockError:
        launcher_lock = None   # another launcher is starting the recorder right now: just show status
    except OSError as exc:
        log.error("cannot use the data folder %s: %s", cfg.data_path, exc)
        print(alert_banner("C-003"))
        _pause()
        return EXIT_RUNTIME
    if launcher_lock is not None:
        try:
            if not args.no_start and not control.is_running():
                started_after = time.time()
                result = control.start()
                log.info("start via %s: ok=%s %s", control.name, result.ok, result.detail)
                if not result.ok:
                    start_problem = result.code
            if not args.no_start and hasattr(control, "start_archive"):
                control.start_archive()   # [archive] enabled (deployed default); no-op when it already runs
        finally:
            launcher_lock.close()
    view = select_view()
    return view.run(cfg.status_path, cfg.camera.name, started_after=started_after,
                    once=args.once, start_problem=start_problem)


def _setup_target(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    if os.environ.get(CONFIG_ENV):
        return Path(os.environ[CONFIG_ENV])
    return default_layout().config_file


def cmd_setup(args: argparse.Namespace) -> int:
    layout = default_layout()
    target = _setup_target(args.config)
    if target.exists() and not args.force:
        print(f"Sentinel is already set up ({target}). Use 'setup --force' to replace the configuration;\n"
              "the previous file is kept as a dated .bak copy.")
        return EXIT_CONFIG
    try:
        answers = interactive_setup(layout, simulated_url=args.simulated_camera)
        path, warnings = write_setup(answers, layout, target, force=args.force)
    except SetupError as exc:
        print(f"\nSetup not completed: {exc}")
        return EXIT_CONFIG
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled; nothing was written.")
        return EXIT_CONFIG
    print(f"\nConfiguration written: {path}")
    for w in warnings:
        print(f"WARNING: {w}")
    print("If the recorder was already running, restart it ('Sentinel stop', then start Sentinel) "
          "so the new settings take effect.")
    return EXIT_OK


def cmd_recorder_run(args: argparse.Namespace) -> int:
    from ..service import run_recorder

    cfg_path = _resolve(args.config)
    if cfg_path is None:
        print("configuration error: no configuration file found (run 'setup')", file=sys.stderr)
        return EXIT_CONFIG
    listeners: list[stopsignal.StopListener] = []

    def attach(svc) -> None:  # noqa: ANN001 - RecorderService
        listeners.append(stopsignal.StopListener(svc.cfg.data_path, svc.request_stop).start())

    try:
        return run_recorder(cfg_path, before_run=attach)
    finally:
        for listener in listeners:
            listener.close()


def cmd_stop(args: argparse.Namespace) -> int:
    cfg_path = _resolve(args.config)
    if cfg_path is None:
        print("No configuration found; nothing to stop.")
        return EXIT_CONFIG
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG
    control = select_control(cfg_path, cfg)
    if not control.is_running():
        print("The recorder is not running.")
        return EXIT_OK
    print("Stopping the recorder (the current recording segment is closed and saved first)...")
    if control.stop(args.timeout):
        print("The recorder has stopped.")
        return EXIT_OK
    print(f"The recorder did not stop within {args.timeout:g} seconds.")
    return EXIT_RUNTIME


def cmd_forward(args: argparse.Namespace, extra: list[str]) -> int:
    from ..service import main as service_main

    cfg_path = _resolve(args.config)
    if cfg_path is None or not cfg_path.exists():
        if args.cmd == "status":
            print(alert_banner("C-002"))
        else:
            print("No configuration file found. Looked in:")
            for c in config_candidates():
                print(f"  {c}")
        return EXIT_CONFIG
    return service_main([args.cmd, "--config", str(cfg_path), *extra])


def read_manifest(folder: Path | None) -> dict | None:
    if folder is None:
        return None
    try:
        return json.loads((folder / MANIFEST_NAME).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_manifest(folder: Path, manifest: dict) -> list[str]:
    """Files whose SHA-256 differs from the build manifest (empty = all match)."""
    bad = []
    for rel, expected in sorted(manifest.get("files", {}).items()):
        p = folder / rel
        try:
            actual = sha256_file(p)[0]
        except OSError:
            bad.append(f"{rel}: missing")
            continue
        if actual != expected:
            bad.append(f"{rel}: changed")
    return bad


def cmd_about(args: argparse.Namespace) -> int:
    folder = app_dir()
    print(f"Sentinel {__version__}  ({'built program' if is_frozen() else 'source checkout'})")
    print(f"program folder:  {folder or '(not a built program)'}")
    print(f"python:          {sys.version.split()[0]}  {sys.executable}")
    for t in TOOL_NAMES:
        b = bundled_tool(t, folder)
        print(f"{t + ':':<16} {default_tool(t)}  ({'bundled' if b else 'from PATH unless configured'})")
    cfg = find_config(args.config)
    print(f"configuration:   {cfg if cfg else 'none found (not set up)'}")
    if cfg is None:
        for c in config_candidates(args.config):
            print(f"  searched:      {c}")
    lay = default_layout()
    print(f"default data:    {lay.spool_dir.parent} ({lay.scope})")
    manifest = read_manifest(folder)
    if manifest:
        ff = manifest.get("ffmpeg", {})
        print(f"build:           {manifest.get('built_utc')}  commit {manifest.get('git_commit')}"
              f"{' (uncommitted changes)' if manifest.get('git_dirty') else ''}")
        print(f"bundled FFmpeg:  {ff.get('version')}  licence {ff.get('license')}")
        status = manifest.get("ffmpeg_license_status") or ff.get("license_status") or "unknown (older build)"
        if ff.get("gpl_internal_use_override") or not str(status).startswith("OK"):
            print("!" * 72)
            print(f"!! FFmpeg licence: {status}")
            print("!! This folder must not be given to anyone outside the organisation.")
            print("!" * 72)
        else:
            print(f"FFmpeg licence:  {status}")
        if args.verify and folder is not None:
            bad = verify_manifest(folder, manifest)
            print("installed files match the build manifest" if not bad else "FILES DIFFER FROM THE BUILD MANIFEST:")
            for b in bad:
                print(f"  {b}")
            return EXIT_OK if not bad else EXIT_RUNTIME
    elif args.verify:
        print("no build manifest found; nothing to verify")
        return EXIT_RUNTIME
    return EXIT_OK


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="Sentinel", description="Sentinel camera recorder")
    sub = ap.add_subparsers(dest="cmd")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--config", help="configuration file (default: see 'about')")
        return p

    p = add("launch", "set up if needed, start the recorder, show status (default)")
    p.add_argument("--once", action="store_true", help="show status once and exit (exit code 0 = healthy)")
    p.add_argument("--no-start", action="store_true", help="only show status; never start the recorder")
    p = add("setup", "first-run setup (technician)")
    p.add_argument("--force", action="store_true", help="replace an existing configuration (kept as .bak)")
    p.add_argument("--simulated-camera", metavar="URL", help=argparse.SUPPRESS)  # developer use only
    arc = sub.add_parser("archive", help="archive-drive copy commands (run, swap, resume, status, designate, ...)")
    arc.add_argument("archive_args", nargs=argparse.REMAINDER)
    rec = sub.add_parser("recorder", help="recorder process commands")
    rec_sub = rec.add_subparsers(dest="recorder_cmd", required=True)
    run = rec_sub.add_parser("run", help="run the recorder in the foreground")
    run.add_argument("--config")
    p = add("stop", "gracefully stop the background recorder (technician)")
    p.add_argument("--timeout", type=float, default=90.0)
    p = add("status", "print the recorder status banner")
    p.add_argument("--json", action="store_true")
    add("check-config", "validate the configuration and tools")
    p = add("schedule", "show when the recording schedule records and pauses (technician)")
    p.add_argument("--days", type=int, default=14)
    p = add("diagnostics", "write a diagnostic report for support")
    p.add_argument("--out")
    p = add("about", "version, folders, bundled tools and build manifest")
    p.add_argument("--verify", action="store_true", help="check installed files against the build manifest")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv or ["launch"])
    if args.cmd == "launch":
        return cmd_launch(args)
    if args.cmd == "setup":
        return cmd_setup(args)
    if args.cmd == "recorder":
        return cmd_recorder_run(args)
    if args.cmd == "archive":
        from ..archive.__main__ import main as archive_main

        return archive_main(list(args.archive_args))
    if args.cmd == "stop":
        return cmd_stop(args)
    if args.cmd == "about":
        return cmd_about(args)
    extra: list[str] = []
    if args.cmd == "status" and args.json:
        extra.append("--json")
    if args.cmd == "diagnostics" and args.out:
        extra += ["--out", args.out]
    if args.cmd == "schedule":
        extra += ["--days", str(args.days)]
    return cmd_forward(args, extra)


if __name__ == "__main__":
    sys.exit(main())
