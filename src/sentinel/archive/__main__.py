"""Command line for the archive copy.

    python -m sentinel.archive run    --config <toml>     continuous worker (its own process)
    python -m sentinel.archive swap   --config <toml>     "Swap Archive Drive": wait until it says what to take out
    python -m sentinel.archive resume --config <toml>     cancel a swap / keep using the same drive
    python -m sentinel.archive status --config <toml>
    python -m sentinel.archive designate   --config <toml> --drive <drive root> --admin-id <id>   (administrator)
    python -m sentinel.archive acknowledge --config <toml> --admin-id <id> [--problem N ...]       (administrator)
    python -m sentinel.archive return --config <toml> --drive <root> --admin-id <id> --destination <text>
        --copied-by <name> --copied-on YYYY-MM-DD --checked      (administrator; after the office copy)
    python -m sentinel.archive service-plan --config <toml> --host-command "<service host command line>"
        prints the Windows service installation commands (NOT EXECUTED; see sentinel.archive.hosting)

The throughput benchmark is a development tool: ``tests/tools/archive_bench.py``.

Why a separate process (not a thread inside the recorder) — design choice:

1. **Isolation.** A failing USB drive can block a read/write system call for minutes. In a
   separate process that only stalls archiving; the recorder's process, its supervisor loop,
   the watchdog ping and service shutdown are unaffected. A thread blocked in the kernel would
   also keep the recorder process from exiting cleanly.
2. **Priority.** Windows "background mode" (CPU + I/O + memory priority) and Linux ``nice`` /
   ``ioprio`` apply cleanly to a whole process; the recorder keeps normal priority.
3. **No interpreter contention.** Hashing and copying run in another interpreter, so they never
   compete with the recorder's Python threads for the GIL.
4. **Same model as the review-drive export**, which is also its own process; both share the
   SQLite ledger (WAL + busy timeout).

The worker class can also run as a thread (``ArchiveWorker.start()``), which the tests use to
run it next to an in-process recorder.

Exit codes: 0 ok, 1 usage/config error, 2 swap did not complete in time, 3 archive not enabled (run) or
archive copying not running (swap, return), 4 needs an administrator (return).
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import sys
import time
from pathlib import Path

from ..config import ConfigError, load_config
from ..logging_setup import FORMAT, RedactingFilter, RedactingFormatter
from .status import (COPIER_NOT_RUNNING, STATUS_FILE, ST_ABSENT, ST_REMOVE_MANUALLY, ST_SAFE, ST_UNUSABLE,
                     copier_running, read_archive_status)
from .worker import ArchiveWorker, request_resume_file, request_swap_file


def _setup_logging(log_dir: Path, level: str) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level.upper())
    fmt = RedactingFormatter(FORMAT, datefmt="%Y-%m-%dT%H:%M:%S")
    fmt.converter = time.gmtime
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stderr),
        logging.handlers.RotatingFileHandler(log_dir / "archive.log", maxBytes=10 * 1024 * 1024, backupCount=5,
                                             encoding="utf-8")]
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())
        root.addHandler(h)


def _load(args: argparse.Namespace):  # noqa: ANN202
    try:
        return load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return None


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if cfg is None:
        return 1
    if not cfg.archive.enabled:
        print("Archive copying is not enabled in the configuration ([archive] enabled = true).", file=sys.stderr)
        return 3
    _setup_logging(cfg.log_path, cfg.service.log_level)
    w = ArchiveWorker(cfg, allow_same_volume=args.allow_same_volume)
    signal.signal(signal.SIGINT, lambda *_: w.stop(0))
    signal.signal(signal.SIGTERM, lambda *_: w.stop(0))
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: w.stop(0))
    w.run(whole_process=True)
    return 0


def _cmd_swap(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if cfg is None:
        return 1
    path = cfg.data_path / STATUS_FILE
    doc = read_archive_status(path) or {}
    # Field report 2026-11-12: with the copier stopped, swap waited its full 10 minutes in silence and
    # blocked every other button on the Drives page. Nothing is being copied then, so say so at once,
    # and leave no swap request behind for a copier that starts later.
    if not copier_running(doc):
        print(COPIER_NOT_RUNNING, flush=True)
        return 3
    before = doc.get("heartbeat_epoch", 0)
    waiting = doc.get("removal") if isinstance(doc.get("removal"), dict) else None
    if waiting:
        # Review 2026-11-12: a second Swap while one drive waits to be taken out prepared the other drive.
        print(f"{waiting.get('text') or 'An archive drive is ready to be taken out.'} Take that drive out first; "
              "nothing else was prepared.", flush=True)
        return 0
    if doc.get("state") == ST_UNUSABLE:
        # Review 2026-11-12: nothing is attached; a swap would only prepare the next good drive inserted.
        print(f"{doc.get('message') or 'The inserted drive cannot be used as an archive drive.'} Nothing was "
              "prepared for removal: Sentinel is not copying to that drive.", flush=True)
        return 2
    request_swap_file(cfg)
    print("Preparing the archive drive for removal. Do not remove it yet.", flush=True)
    end = time.monotonic() + args.timeout
    while time.monotonic() < end:
        doc = read_archive_status(path) or {}
        fresh = doc.get("heartbeat_epoch", 0) > before
        removal = doc.get("removal") if isinstance(doc.get("removal"), dict) else None
        if fresh and doc.get("state") in (ST_SAFE, ST_REMOVE_MANUALLY, ST_ABSENT):
            print(doc.get("message", ""), flush=True)
            return 0
        if fresh and removal:
            # Review 2026-11-12: with two drives the FULL drive is prepared while copying goes on to the
            # other one, so the state stays COPYING / UP_TO_DATE; the removal entry says it is done.
            print(removal.get("text") or doc.get("message", ""), flush=True)
            return 0
        if fresh and doc.get("state") == ST_UNUSABLE:
            # Review 2026-11-12: the copier drops a swap pressed while the inserted drive is refused (it would
            # otherwise prepare the next good drive inserted); say so instead of waiting the full timeout.
            print(f"{doc.get('message') or 'The inserted drive cannot be used as an archive drive.'} Nothing was "
                  "prepared for removal: Sentinel is not copying to that drive.", flush=True)
            return 2
        if not copier_running(doc):
            print("Archive copying stopped while the drive was being prepared. " + COPIER_NOT_RUNNING, flush=True)
            return 3
        time.sleep(0.5)
    print("The archive drive is not ready to be removed yet. Do not remove it. If this keeps happening, "
          "contact technical support.", flush=True)
    return 2


def _cmd_resume(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if cfg is None:
        return 1
    request_resume_file(cfg)
    print("Archive copying will continue on the inserted drive.")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if cfg is None:
        return 1
    doc = read_archive_status(cfg.data_path / STATUS_FILE)
    print(json.dumps(doc, indent=2) if doc else "Archive copying has not run yet.")
    return 0


def _cmd_designate(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if cfg is None:
        return 1
    from ..db import Ledger
    from .ledger import ArchiveLedger
    from .locator import DriveUnusable, designate_archive

    led = Ledger(cfg.data_path / "sentinel.db")
    try:
        al = ArchiveLedger(led, cfg.camera.id)
        try:
            m = designate_archive(Path(args.drive), args.admin_id, lambda key: al.review_batches_on(key) > 0)
        except (DriveUnusable, OSError, ValueError) as exc:
            print(f"Error A-005: The archive drive cannot be used. ({exc})", file=sys.stderr)
            return 2
        al.event("audit", "archive_drive_designated", "Drive designated as an archive drive",
                 {"drive_uuid": m.get("drive_uuid"), "admin_id": args.admin_id})
    finally:
        led.close()
    print("The drive is now an archive drive.")
    return 0


def _cmd_acknowledge(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if cfg is None:
        return 1
    from ..db import Ledger
    from .ledger import ArchiveLedger

    led = Ledger(cfg.data_path / "sentinel.db")
    try:
        al = ArchiveLedger(led, cfg.camera.id)
        if not args.problem:
            for p in al.open_problems():
                print(f"{p['problem_id']}: {p['kind']} drive={p['drive_id']} path={p['path']} {p['detail']}")
            if not args.all:
                print("Nothing acknowledged. Give --problem N (repeatable) or --all.")
                return 0
        n = al.acknowledge(args.admin_id, None if args.all else [int(x) for x in args.problem])
    finally:
        led.close()
    print(f"{n} archive problem(s) acknowledged.")
    return 0


def _cmd_return(args: argparse.Namespace) -> int:
    """Return an archive drive to service after the office has copied it (0.3.9; see worker.py). The
    running archive copier does the work between two files; this command asks it and waits."""
    import datetime as dt
    import os
    import uuid

    from ..clock import iso_utc
    from ..drives.prepare import os_administrator_check
    from .locator import read_marker
    from .worker import CONTROL_DIR, RETURN_REQUEST, RETURN_RESULT

    cfg = _load(args)
    if cfg is None:
        return 1
    admin, _how = os_administrator_check()
    if not admin:
        print("Returning a drive to service needs an administrator. Nothing was deleted.", file=sys.stderr)
        return 4
    texts = {"destination": args.destination, "copied_by": args.copied_by, "admin_id": args.admin_id}
    missing = [k for k, v in texts.items() if not (v or "").strip()]
    if missing or not args.checked:
        print("Say where the recordings were copied (--destination), who copied them (--copied-by) and confirm "
              "that every file was checked at the office (--checked). Nothing was deleted.", file=sys.stderr)
        return 1
    try:
        copied_on = dt.date.fromisoformat(args.copied_on)
    except (TypeError, ValueError):
        print("--copied-on must be a date like 2026-11-26. Nothing was deleted.", file=sys.stderr)
        return 1
    if copied_on > dt.date.today():
        print("The copy date is in the future. Nothing was deleted.", file=sys.stderr)
        return 1
    marker = read_marker(Path(args.drive))
    if not marker or marker.get("role") != "archive" or not marker.get("drive_uuid"):
        print(f"{args.drive} is not a Sentinel archive drive. Nothing was deleted.", file=sys.stderr)
        return 2
    expect = (getattr(args, "expect_uuid", None) or "").strip().lower()
    if expect and str(marker["drive_uuid"]).strip().lower() != expect:
        # Review 2026-11-12: the window names the drive the person confirmed; another drive now at that letter
        # (replugged, letters changed) is never cleared.
        print(f"The drive at {args.drive} ({marker.get('label') or 'unnamed'}) is not the drive you selected. "
              "Nothing was deleted. Press Refresh list and select the drive again.", file=sys.stderr)
        return 2
    status_path = cfg.data_path / STATUS_FILE
    if not copier_running(read_archive_status(status_path) or {}):
        print("Archive copying is not running, so the drive cannot be returned to service now. Nothing was "
              "deleted. Contact technical support.", file=sys.stderr)
        return 3
    control = cfg.data_path / CONTROL_DIR
    result = control / RETURN_RESULT
    rid = str(uuid.uuid4())
    req = {"written_utc": iso_utc(time.time()), "request_id": rid, "drive_uuid": str(marker["drive_uuid"]),
           "admin_id": args.admin_id.strip(), "destination": args.destination.strip(),
           "copied_by": args.copied_by.strip(), "copied_on": copied_on.isoformat(), "checked": True}
    try:
        control.mkdir(parents=True, exist_ok=True)
        result.unlink(missing_ok=True)
        tmp = control / (RETURN_REQUEST + ".partial")
        tmp.write_text(json.dumps(req), "utf-8")
        os.replace(tmp, control / RETURN_REQUEST)
    except OSError as exc:
        print(f"The request could not be made ({type(exc).__name__}). Open Sentinel as administrator and try "
              "again. Nothing was deleted.", file=sys.stderr)
        return 4
    label = marker.get("label") or args.drive
    print(f"Returning {label} to service. Copying finishes the file in progress first; do not unplug the "
          "drive.", flush=True)
    end = time.monotonic() + args.timeout
    while time.monotonic() < end:
        try:
            out = json.loads(result.read_text("utf-8"))
        except (OSError, ValueError):
            time.sleep(0.5)
            continue
        if out.get("request_id") != rid:
            time.sleep(0.5)
            continue
        result.unlink(missing_ok=True)
        print(out.get("message", ""))
        return 0 if out.get("ok") else 2
    print("The archive copier has not answered yet. Do not unplug the drive; look at the Drives page in a few "
          "minutes. If nothing changes, contact technical support.")
    return 2


def _cmd_service_plan(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if cfg is None:
        return 1
    import subprocess

    from .hosting import windows_service_commands

    from .hosting import default_host_command

    host = args.host_command or default_host_command(Path(args.config))
    cfg_dir = Path(cfg.source_path).resolve().parent if cfg.source_path else None
    print("NOT EXECUTED. Run as administrator only after review (see sentinel.archive.hosting):")
    for argv in windows_service_commands(host, cfg.spool_path, cfg.data_path, cfg.log_path, cfg_dir):
        print(subprocess.list2cmdline(argv))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sentinel.archive", description="Sentinel archive-drive copy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "swap", "resume", "status", "designate", "acknowledge", "return", "service-plan"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name == "swap":
            p.add_argument("--timeout", type=float, default=600.0,
                           help="seconds to wait until the drive is ready to be taken out")
        if name == "run":
            p.add_argument("--allow-same-volume", action="store_true",
                           help="development/test only: allow an archive folder on the recording storage volume")
        if name == "designate":
            p.add_argument("--drive", required=True, help="root folder of a prepared drive")
            p.add_argument("--admin-id", required=True)
        if name == "acknowledge":
            p.add_argument("--admin-id", required=True)
            p.add_argument("--problem", action="append", help="problem id (repeatable); omit to list")
            p.add_argument("--all", action="store_true")
        if name == "return":
            p.add_argument("--drive", required=True, help="root folder (letter) of the archive drive")
            p.add_argument("--admin-id", required=True)
            p.add_argument("--destination", required=True, help="where the office copied the recordings")
            p.add_argument("--copied-by", required=True, help="who made the office copy")
            p.add_argument("--copied-on", required=True, help="date of the office copy, YYYY-MM-DD")
            p.add_argument("--checked", action="store_true",
                           help="every file was checked against SHA256SUMS.txt at the office and matched")
            p.add_argument("--timeout", type=float, default=1800.0)
            p.add_argument("--expect-uuid", default=None,
                           help="refuse unless the drive at --drive has this drive_uuid (the drive confirmed)")
        if name == "service-plan":
            p.add_argument("--host-command", default=None,
                           help="override; default: the Windows service host with --program archive")
    args = ap.parse_args(argv)
    return {"run": _cmd_run, "swap": _cmd_swap, "resume": _cmd_resume, "status": _cmd_status,
            "designate": _cmd_designate, "acknowledge": _cmd_acknowledge, "return": _cmd_return,
            "service-plan": _cmd_service_plan}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())