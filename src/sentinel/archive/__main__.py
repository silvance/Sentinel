"""Command line for the archive copy.

    python -m sentinel.archive run    --config <toml>     continuous worker (its own process)
    python -m sentinel.archive swap   --config <toml>     "Swap Archive Drive": wait for SAFE TO REMOVE
    python -m sentinel.archive resume --config <toml>     cancel a swap / keep using the same drive
    python -m sentinel.archive status --config <toml>
    python -m sentinel.archive designate   --config <toml> --drive <drive root> --admin-id <id>   (administrator)
    python -m sentinel.archive acknowledge --config <toml> --admin-id <id> [--problem N ...]       (administrator)
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

Exit codes: 0 ok, 1 usage/config error, 2 swap did not complete in time, 3 archive not enabled.
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
from .status import STATUS_FILE, ST_ABSENT, ST_REMOVE_MANUALLY, ST_SAFE, read_archive_status
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
    before = (read_archive_status(path) or {}).get("heartbeat_epoch", 0)
    request_swap_file(cfg)
    print("Preparing the archive drive for removal. Do not remove it yet.")
    end = time.monotonic() + args.timeout
    while time.monotonic() < end:
        doc = read_archive_status(path) or {}
        if doc.get("heartbeat_epoch", 0) > before and doc.get("state") in (ST_SAFE, ST_REMOVE_MANUALLY, ST_ABSENT):
            print(doc.get("message", ""))
            return 0
        time.sleep(0.5)
    print("The archive drive is not ready to be removed yet. Do not remove it. If this keeps happening, "
          "contact technical support.")
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


def _cmd_service_plan(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if cfg is None:
        return 1
    import subprocess

    from .hosting import windows_service_commands

    print("NOT EXECUTED. Run as administrator only after review (see sentinel.archive.hosting):")
    for argv in windows_service_commands(args.host_command, cfg.spool_path, cfg.data_path, cfg.log_path):
        print(subprocess.list2cmdline(argv))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sentinel.archive", description="Sentinel archive-drive copy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "swap", "resume", "status", "designate", "acknowledge", "service-plan"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name == "swap":
            p.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for SAFE TO REMOVE")
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
        if name == "service-plan":
            p.add_argument("--host-command", required=True)
    args = ap.parse_args(argv)
    return {"run": _cmd_run, "swap": _cmd_swap, "resume": _cmd_resume, "status": _cmd_status,
            "designate": _cmd_designate, "acknowledge": _cmd_acknowledge,
            "service-plan": _cmd_service_plan}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())