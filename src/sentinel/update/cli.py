"""``Sentinel update ...`` (administrator):

* ``info FILE``      - check an update file and say what it contains (changes nothing)
* ``apply FILE``     - apply it (also: drop the file onto Sentinel.exe, or System > Updates)
* ``rollback``       - go back to the version kept in ``<program folder>.previous``
* ``status``         - installed and previous version, last update result
* ``finish`` / ``restart`` - used by the update itself (see ``apply``); not for people
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .. import __version__
from ..clock import iso_utc
from ..paths import app_dir, find_config
from .apply import (Folders, Pending, RecordingState, check_installed, finisher_script, folder_version,
                    launch_finisher, read_result, record_event, restart_after_refusal, stage, start_recording,
                    stop_recording, write_result)
from .package import UpdateRefused, read_package

EXIT_OK, EXIT_REFUSED, EXIT_FAILED = 0, 2, 1
PENDING_STALE_S = 3600.0


def _who() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def _is_admin() -> bool:
    if sys.platform != "win32":
        import os

        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _confirm(prompt: str, word: str) -> bool:
    try:
        return input(f"{prompt}\nType {word} and press Enter to continue: ").strip().upper() == word
    except (EOFError, KeyboardInterrupt):
        return False


def _still_stopping(waited_s: float) -> None:
    print(f"  still finishing the last recording file ({waited_s:.0f} s); please wait, do not close this window",
          flush=True)


def _pause() -> None:
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass


def _app() -> Path | None:
    return app_dir()


def _config(explicit: str | None) -> Path | None:
    p = find_config(explicit)
    return p if p is not None and p.is_file() else None


def _relaunch_elevated(argv: list[str]) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    import subprocess

    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, subprocess.list2cmdline(argv), None, 1)
    return int(rc) > 32


def _busy(folders: Folders) -> str | None:
    if not folders.state.is_file():
        return None
    age = time.time() - folders.state.stat().st_mtime
    if age > PENDING_STALE_S:
        return None
    return ("Another update or rollback started less than an hour ago and has not finished "
            f"({folders.state}). Wait for it, or delete that file if it was interrupted.")


# ---------------------------------------------------------------------- commands for people
def cmd_info(args: argparse.Namespace) -> int:
    try:
        pkg = read_package(Path(args.file), installed_version=None)
    except UpdateRefused as exc:
        print(f"Not usable: {exc}")
        return EXIT_REFUSED
    print(pkg.summary())
    print(f"\nInstalled version: {__version__}")
    print(f"Update file SHA-256: {pkg.sha256}")
    return EXIT_OK


def cmd_apply(args: argparse.Namespace) -> int:
    app = _app()
    if app is None:
        print("Updates apply to the installed program (Sentinel.exe), not to a source checkout.")
        return EXIT_REFUSED
    if sys.platform != "win32":
        print("Applying updates is available on Windows only so far; install the full program folder.")
        return EXIT_REFUSED
    if not _is_admin():
        print("Updating needs an administrator. Windows will ask for permission.")
        argv = ["update", "apply", str(Path(args.file).resolve())] + (["--config", args.config] if args.config else [])
        return EXIT_OK if _relaunch_elevated(argv) else EXIT_REFUSED
    folders = Folders(app)
    try:
        busy = _busy(folders)
        if busy:
            raise UpdateRefused("busy", busy)
        pkg = read_package(Path(args.file), installed_version=__version__)
        print(f"Installed: Sentinel {__version__}\nUpdate:    {pkg.summary()}\n")
        if not args.yes and not _confirm("Recording stops for about a minute while Sentinel updates, then "
                                         "restarts by itself. Sentinel windows close and reopen.", "UPDATE"):
            print("Not confirmed. Nothing was changed.")
            return EXIT_REFUSED
        staging = folders.staging(pkg.version)
        print("Preparing the new version (recording continues)...")
        last = [0.0]

        def progress(done: int, total: int, _rel: str) -> None:
            if time.monotonic() - last[0] > 1.0 or done == total:
                last[0] = time.monotonic()
                print(f"  {done}/{total} files")

        stage(app, pkg, staging, progress)
        cfg_path = _config(args.config)
        print("Stopping the recorder (the current recording file is closed and saved first)...")
        try:
            st = stop_recording(cfg_path, on_wait=_still_stopping)
        except UpdateRefused as exc:
            import shutil

            shutil.rmtree(staging, ignore_errors=True)
            again = restart_after_refusal(exc, cfg_path)      # review 2026-11-12: never leave it stopped
            if again:
                raise UpdateRefused(exc.code, f"{exc} {again}") from None
            raise
        pending = Pending("update", __version__, pkg.version, str(app), str(staging), str(folders.previous),
                          str(cfg_path) if cfg_path else None, asdict(st), str(pkg.path), pkg.sha256, pkg.key_id,
                          who=_who(), started_utc=iso_utc(time.time()))
        pending.write(folders.state)
        record_event(pending.config, "software_update_started",
                     f"Software update from Sentinel {__version__} to {pkg.version} started",
                     {"from": __version__, "to": pkg.version, "package_sha256": pkg.sha256, "key_id": pkg.key_id,
                      "who": pending.who})
        launch_finisher(finisher_script(pending, folders, args.window_pid), folders)
    except UpdateRefused as exc:
        print(f"\nThe update was not applied: {exc}")
        _pause()
        return EXIT_REFUSED
    print("\nThe update continues in a new window; this one closes now.")
    time.sleep(2)
    return EXIT_OK


def cmd_rollback(args: argparse.Namespace) -> int:
    app = _app()
    if app is None or sys.platform != "win32":
        print("Going back to a previous version is available for the installed Windows program only.")
        return EXIT_REFUSED
    if not _is_admin():
        print("Going back needs an administrator. Windows will ask for permission.")
        argv = ["update", "rollback"] + (["--config", args.config] if args.config else [])
        if args.reason:
            argv += ["--reason", args.reason]
        return EXIT_OK if _relaunch_elevated(argv) else EXIT_REFUSED
    folders = Folders(app)
    prev_version = folder_version(folders.previous)
    if prev_version is None:
        print("No previous version is kept on this computer. Nothing was changed.")
        return EXIT_REFUSED
    busy = _busy(folders)
    if busy:
        print(busy)
        return EXIT_REFUSED
    if not args.yes and not _confirm(f"Go back from Sentinel {__version__} to {prev_version}? Recording stops for "
                                     "about a minute.", "BACK"):
        print("Not confirmed. Nothing was changed.")
        return EXIT_REFUSED
    cfg_path = _config(args.config)
    print("Stopping the recorder (the current recording file is closed and saved first)...")
    try:
        st = stop_recording(cfg_path, on_wait=_still_stopping)
    except UpdateRefused as exc:
        again = restart_after_refusal(exc, cfg_path)          # review 2026-11-12: never leave it stopped
        print(f"Sentinel did not go back to the previous version: {exc} {again}".rstrip())
        return EXIT_REFUSED
    pending = Pending("rollback", __version__, prev_version, str(app), str(folders.previous), str(folders.previous),
                      str(cfg_path) if cfg_path else None, asdict(st), who=_who(), started_utc=iso_utc(time.time()),
                      reason=args.reason or "requested by an administrator")
    pending.write(folders.state)
    record_event(pending.config, "software_rollback_started",
                 f"Going back from Sentinel {__version__} to {prev_version}",
                 {"from": __version__, "to": prev_version, "who": pending.who, "reason": pending.reason})
    launch_finisher(finisher_script(pending, folders, args.window_pid), folders)
    print("Going back continues in a new window; this one closes now.")
    time.sleep(2)
    return EXIT_OK


def cmd_status(_args: argparse.Namespace) -> int:
    app = _app()
    print(f"Installed: Sentinel {__version__}")
    if app is None:
        print("(source checkout: no update folders)")
        return EXIT_OK
    folders = Folders(app)
    prev = folder_version(folders.previous)
    print(f"Previous version kept: {prev or 'none'}")
    res = read_result(folders)
    if res:
        print(f"Last update: {res.get('outcome')} at {res.get('finished_utc')}: {res.get('message')}")
    if folders.state.is_file():
        print(f"An update or rollback is in progress or was interrupted: {folders.state}")
    return EXIT_OK


# ---------------------------------------------------------------------- steps run by the update itself
def cmd_finish(args: argparse.Namespace) -> int:
    """Run by the version just swapped in. Exit 0 = keep it; anything else = the finisher swaps back."""
    pending = Pending.read(Path(args.state))
    folders = Folders(Path(pending.app))
    problems = check_installed(Path(pending.app), pending.config)
    if not problems:
        ok, detail = start_recording(RecordingState(**pending.recording), pending.config)
        if not ok:
            problems.append(f"the recorder did not start: {detail}")
    if problems:
        pending.reason = "; ".join(problems)
        pending.write(folders.state)
        print("The new version did not pass its check:\n  " + "\n  ".join(problems))
        return EXIT_FAILED
    verb = "updated" if pending.action == "update" else "went back"
    msg = (f"Sentinel {verb} from {pending.from_version} to {pending.to_version}."
           + (f" Reason: {pending.reason}." if pending.action == "rollback" and pending.reason else ""))
    record_event(pending.config, "software_updated" if pending.action == "update" else "software_rolled_back", msg,
                 {"from": pending.from_version, "to": pending.to_version, "package_sha256": pending.package_sha256,
                  "key_id": pending.key_id, "who": pending.who, "action": pending.action})
    write_result(folders, "updated" if pending.action == "update" else "rolled_back", pending, msg,
                 watch=pending.action == "update")
    print(msg)
    return EXIT_OK


def cmd_restart(args: argparse.Namespace) -> int:
    """Run by the version that stays (after a cancelled or failed update): restart recording, say why."""
    pending = Pending.read(Path(args.state))
    folders = Folders(Path(pending.app))
    ok, detail = start_recording(RecordingState(**pending.recording), pending.config)
    reason = pending.reason or args.reason or "unknown"
    target = "update to" if pending.action == "update" else "going back to"
    msg = (f"The {target} Sentinel {pending.to_version} was "
           f"{'cancelled' if args.outcome == 'aborted' else 'undone'}: {reason}. "
           f"Sentinel {__version__} is in use"
           + ("." if ok else f", but the recorder did not start again: {detail}."))
    record_event(pending.config, "software_update_failed", msg,
                 {"from": pending.from_version, "to": pending.to_version, "outcome": args.outcome, "reason": reason,
                  "package_sha256": pending.package_sha256, "recorder_restarted": ok})
    write_result(folders, args.outcome, pending, msg)
    print(msg)
    return EXIT_OK if ok else EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="Sentinel update", description="Version updates from a single signed file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info", help="check an update file and show what it contains")
    p.add_argument("file")
    p = sub.add_parser("apply", help="apply an update file (administrator)")
    p.add_argument("file")
    p.add_argument("--config")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--window-pid", type=int, default=None, help=argparse.SUPPRESS)
    p = sub.add_parser("rollback", help="go back to the previous version (administrator)")
    p.add_argument("--config")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--reason", default="")
    p.add_argument("--window-pid", type=int, default=None, help=argparse.SUPPRESS)
    sub.add_parser("status", help="installed and previous version, last update")
    p = sub.add_parser("finish")
    p.add_argument("--state", required=True)
    p = sub.add_parser("restart")
    p.add_argument("--state", required=True)
    p.add_argument("--outcome", choices=("aborted", "rolled_back"), required=True)
    p.add_argument("--reason", default="")
    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return {"info": cmd_info, "apply": cmd_apply, "rollback": cmd_rollback, "status": cmd_status,
            "finish": cmd_finish, "restart": cmd_restart}[args.cmd](args)
