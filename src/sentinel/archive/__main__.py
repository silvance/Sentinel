"""Command line for the archive copy.

    python -m sentinel.archive run    --config <toml>     continuous worker (its own process)
    python -m sentinel.archive swap   --config <toml>     "Swap Archive Drive": wait for SAFE TO REMOVE
    python -m sentinel.archive resume --config <toml>     cancel a swap / keep using the same drive
    python -m sentinel.archive status --config <toml>
    python -m sentinel.archive bench  --dir <empty folder> [--segments N] [--segment-mb M] [--rate-mb-s R]

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sentinel.archive", description="Sentinel archive-drive copy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "swap", "resume", "status"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name == "swap":
            p.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for SAFE TO REMOVE")
        if name == "run":
            p.add_argument("--allow-same-volume", action="store_true",
                           help="development/test only: allow an archive folder on the recording storage volume")
    b = sub.add_parser("bench", help="measure copy+verify throughput and CPU on this machine")
    b.add_argument("--dir", required=True, help="empty folder (created) on the disk to measure")
    b.add_argument("--segments", type=int, default=8)
    b.add_argument("--segment-mb", type=float, default=256.0)
    b.add_argument("--rate-mb-s", type=float, default=0.0, help="throttle; 0 = unthrottled")
    args = ap.parse_args(argv)
    if args.cmd == "bench":
        from .bench import main_bench

        return main_bench(Path(args.dir), args.segments, args.segment_mb, args.rate_mb_s)
    return {"run": _cmd_run, "swap": _cmd_swap, "resume": _cmd_resume, "status": _cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
