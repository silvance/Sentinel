"""Archive-drive layout. Pure functions, no I/O.

::

    Sentinel/
      README_ARCHIVE.txt                                   plain-language description (written once)
      <camera>/<YYYY-MM-DD>/<HHMMSS>Z_<HHMMSS>Z_<seg8>.ts   sealed originals, unchanged (FINALIZED / INTERRUPTED)
      <camera>/<YYYY-MM-DD>/<HHMMSS>Z_<HHMMSS>Z_<seg8>.framehash.txt   per-frame SHA-256 sidecar
      <camera>/Quarantine/<YYYY-MM-DD>/...                 CORRUPT originals (kept for examination)
      INDEX/archive_index.json | archive_index.csv | gaps.csv | SHA256SUMS.txt   rolling index
      INDEX/verified_journal.csv                           append-only, one line per verified file

Design choices (docs/branches/design-archive-drive.md):

* Archive names use **UTC** (``Z`` suffix), not the display time zone: the archive is the bulk
  evidence store, read by examiners and tools, and UTC names cannot repeat in the daylight-saving
  fall-back hour. Local times are given in the index. Reviewer-friendly local names are the
  review-drive export's job.
* ``<seg8>`` (first 8 hex digits of the segment UUID) makes every name unique even when two
  segments start in the same second (e.g. around a reconnect), and ties the file to its ledger row.
* A segment without a start time (possible for CORRUPT ones) is named after its run and
  sequence number under ``undated/``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

ROOT = "Sentinel"
INDEX_DIR = f"{ROOT}/INDEX"
README = f"{ROOT}/README_ARCHIVE.txt"
INDEX_JSON = f"{INDEX_DIR}/archive_index.json"
INDEX_CSV = f"{INDEX_DIR}/archive_index.csv"
GAPS_CSV = f"{INDEX_DIR}/gaps.csv"
SUMS = f"{INDEX_DIR}/SHA256SUMS.txt"
JOURNAL = f"{INDEX_DIR}/verified_journal.csv"
QUARANTINE = "Quarantine"
FRAMEHASH_SUFFIX = ".framehash.txt"

KIND_ORIGINAL, KIND_QUARANTINE, KIND_FRAMEHASH = "original", "quarantine", "framehash"
MEDIA_KINDS = (KIND_ORIGINAL, KIND_QUARANTINE)

_SAFE = re.compile(r"[^0-9A-Za-z_+\-.]")


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def safe(text: str) -> str:
    return _SAFE.sub("_", text)


def media_kind(state: str) -> str:
    return KIND_QUARANTINE if state == "CORRUPT" else KIND_ORIGINAL


def base_rel_path(seg: Any) -> str:
    """Path of the segment's media file on the drive, without extension (``seg`` = ledger row)."""
    cam = safe(seg["camera_id"])
    sid8 = str(seg["segment_id"]).replace("-", "")[:8]
    start, end = _parse(seg["start_utc"]), _parse(seg["end_utc"])
    top = f"{ROOT}/{cam}/{QUARANTINE}" if seg["state"] == "CORRUPT" else f"{ROOT}/{cam}"
    if start is None:
        return f"{top}/undated/{safe(str(seg['run_id']))}_seq{int(seg['seq']):06d}_{sid8}"
    e = end.strftime("%H%M%SZ") if end is not None else "open"
    return f"{top}/{start.strftime('%Y-%m-%d')}/{start.strftime('%H%M%SZ')}_{e}_{sid8}"


def media_rel_path(seg: Any) -> str:
    ext = "." + str(seg["rel_path"]).rsplit(".", 1)[-1] if "." in str(seg["rel_path"]).rsplit("/", 1)[-1] else ".ts"
    return base_rel_path(seg) + safe(ext)


def framehash_rel_path(seg: Any) -> str:
    return base_rel_path(seg) + FRAMEHASH_SUFFIX


def rel_path_for(seg: Any, kind: str) -> str:
    return framehash_rel_path(seg) if kind == KIND_FRAMEHASH else media_rel_path(seg)
