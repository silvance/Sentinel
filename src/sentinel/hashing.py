"""Streaming SHA-256."""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 4 * 1024 * 1024


def sha256_file(path: str | Path) -> tuple[str, int]:
    """Return (hex digest, bytes read). Reads the file; never trusts metadata."""
    h = hashlib.sha256()
    total = 0
    with open(path, "rb", buffering=0) as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
            total += len(block)
    return h.hexdigest(), total
