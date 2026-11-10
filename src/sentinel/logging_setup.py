"""Logging configuration with mandatory credential redaction."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .credentials import redact


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # malformed format args: keep raw text
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class RedactingFormatter(logging.Formatter):
    """Also redacts formatted tracebacks, which are rendered after filters run."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)-7s %(name)s: %(message)s"


def setup_logging(log_dir: Path | None, level: str = "INFO", to_stderr: bool = True) -> None:
    import time as _time

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level.upper())
    formatter = RedactingFormatter(FORMAT, datefmt="%Y-%m-%dT%H:%M:%S")
    formatter.converter = _time.gmtime  # log timestamps in UTC
    handlers: list[logging.Handler] = []
    if to_stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_dir / "recorder.log", maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(formatter)
        h.addFilter(RedactingFilter())
        root.addHandler(h)
