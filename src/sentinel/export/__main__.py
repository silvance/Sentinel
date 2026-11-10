"""Command line: ``python -m sentinel.export run --config <toml> --target <drive folder>``.

Runs as its own process, separate from the recorder (both write the SQLite ledger; WAL mode and
``busy_timeout`` let them share it). Progress for a future GUI goes to
``<data_dir>/export_status.json``; the last line printed is the plain-language result.

Exit codes: 0 complete (or nothing new to export), 2 failed (needs attention),
3 interrupted (drive removed; run again with the same drive to finish), 1 usage/config error.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from pathlib import Path

from ..config import ConfigError, load_config
from ..logging_setup import FORMAT, RedactingFilter, RedactingFormatter
from .engine import ReviewExporter
from .status import STATUS_FILE, read_export_status
from .target import TargetError, identify_target


def _setup_logging(log_dir: Path, level: str) -> None:
    import time as _time

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level.upper())
    fmt = RedactingFormatter(FORMAT, datefmt="%Y-%m-%dT%H:%M:%S")
    fmt.converter = _time.gmtime
    log_dir.mkdir(parents=True, exist_ok=True)
    # Own file: the recorder rotates recorder.log; two processes must not rotate the same file.
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stderr),
        logging.handlers.RotatingFileHandler(log_dir / "export.log", maxBytes=10 * 1024 * 1024, backupCount=5,
                                             encoding="utf-8")]
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())
        root.addHandler(h)


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    _setup_logging(cfg.log_path, cfg.service.log_level)
    try:
        target = identify_target(Path(args.target))
    except TargetError as exc:
        print(f"Error E-005: The review drive cannot be used. ({exc})", file=sys.stderr)
        return 2
    res = ReviewExporter(cfg, target, operator_id=args.operator_id, allow_same_volume=args.allow_same_volume).run()
    print(res.message)
    return {"COMPLETE": 0, "NOTHING_TO_EXPORT": 0, "INTERRUPTED": 3}.get(res.state, 2)


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    doc = read_export_status(cfg.data_path / STATUS_FILE)
    print(json.dumps(doc, indent=2) if doc else "No export has run yet.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sentinel.export", description="Sentinel review-drive export")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="export new recordings to a mounted review drive")
    r.add_argument("--config", required=True)
    r.add_argument("--target", required=True, help="mounted, writable folder on the review drive (usually its root)")
    r.add_argument("--operator-id", default=None, help="recorded in the manifest and the ledger")
    r.add_argument("--allow-same-volume", action="store_true",
                   help="development/test only: allow a target on the recording storage volume")
    s = sub.add_parser("status", help="print the last export status")
    s.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    return _cmd_run(args) if args.cmd == "run" else _cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
