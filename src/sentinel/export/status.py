"""``export_status.json``: what a future operator GUI shows while an export runs.

Written atomically (temp file + replace) by the export process only. Fields:
``state`` (CHECKING | PLANNING | COPYING | VERIFYING | MANIFEST | EJECTING | SAFE_TO_REMOVE |
COMPLETE_REMOVE_MANUALLY | NOTHING_TO_EXPORT | FAILED | INTERRUPTED), ``percent`` (0-100),
``message`` (plain language, complete sentence(s)), ``alert`` (E-0xx alert or null), and
``heartbeat_epoch`` so a reader can tell a dead export from a slow one.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..clock import iso_utc

STATUS_FILE = "export_status.json"
SCHEMA = 1
FINAL_STATES = ("SAFE_TO_REMOVE", "COMPLETE_REMOVE_MANUALLY", "NOTHING_TO_EXPORT", "FAILED", "INTERRUPTED")


class ExportStatusWriter:
    def __init__(self, path: Path, min_interval_s: float = 0.5) -> None:
        self.path = path
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self.doc: dict[str, Any] = {}

    def write(self, state: str, message: str, percent: int | None = None, force: bool = True,
              **extra: Any) -> None:
        now = time.time()
        if not force and now - self._last < self.min_interval_s:
            return
        self._last = now
        doc = dict(self.doc)
        doc.update(extra)
        doc.update(schema=SCHEMA, state=state, message=message, updated_utc=iso_utc(now), heartbeat_epoch=now,
                   pid=os.getpid(), final=state in FINAL_STATES)
        if percent is not None:
            doc["percent"] = max(0, min(100, int(percent)))
        doc.setdefault("percent", 0)
        doc.setdefault("alert", None)
        self.doc = doc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:  # Windows: a reader has the file open
                time.sleep(0.05 * (attempt + 1))
        os.replace(tmp, self.path)


def read_export_status(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
