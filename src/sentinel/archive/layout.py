"""Archive-drive layout. Pure functions, no I/O.

::

    Sentinel/
      README_ARCHIVE.txt                                   plain-language description (written once)
      <camera>/<YYYY-MM-DD>/<HHMMSS>Z_<HHMMSS>Z_<seg8>.ts   sealed originals, unchanged (FINALIZED / INTERRUPTED)
      <camera>/Fingerprints/<YYYY-MM-DD>/<same name>.framehash.txt     per-frame SHA-256 sidecar
      <camera>/Playlists/<local YYYY-MM-DD>.m3u            plays a local day's originals in time order
      <camera>/Quarantine/<YYYY-MM-DD>/...                 CORRUPT originals (kept for examination;
                                                           their sidecars in Quarantine/Fingerprints/)
      <camera>/INDEX/archive_index.json | archive_index.csv | gaps.csv | SHA256SUMS.txt   rolling index
      <camera>/INDEX/verified_journal.csv                  append-only, one line per verified file
      <camera>/INDEX/chain_heads.log                       append-only recording-log fingerprints (tamper evidence)

The index is **per camera** so drives that receive footage from several appliances / cameras keep
one index per camera; a reinstalled ledger carries entries it does not know forward (index.py).

Design choices (docs/branches/design-archive-drive.md):

* Archive names use **UTC** (``Z`` suffix), not the display time zone: the archive is the bulk
  evidence store, read by examiners and tools, and UTC names cannot repeat in the daylight-saving
  fall-back hour. Local times are given in the index. Reviewer-friendly local names are the
  review-drive export's job.
* ``<seg8>`` (first 8 hex digits of the segment UUID) makes every name unique even when two
  segments start in the same second (e.g. around a reconnect), and ties the file to its ledger row.
* A segment without a start time (possible for CORRUPT ones) is named after its run and
  sequence number under ``undated/``.
* (owner request 2026-11-11) Sidecars live in ``Fingerprints/`` so a day folder shows only video,
  and ``Playlists/`` holds one small ``.m3u`` per *local* day that plays the verified originals on
  the drive back to back (VLC, Windows Media Player), without any extra copy of the footage. Copies
  made before this change keep the path recorded for them (the index lists each file's path).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

ROOT = "Sentinel"
README = f"{ROOT}/README_ARCHIVE.txt"
SET_ASIDE_MARK = ".unverified-"          # suffix of a file that failed read-back and was moved aside


def index_dir(camera_id: str) -> str:
    return f"{ROOT}/{safe(camera_id)}/INDEX"


def chain_heads_path(camera_id: str) -> str:
    """Append-only copies of the recording log's fingerprint (chain head), one line per index write;
    same line format as the recorder's own chain_heads.log (sentinel.ledgerchain)."""
    return f"{index_dir(camera_id)}/chain_heads.log"


def index_files(camera_id: str) -> dict[str, str]:
    d = index_dir(camera_id)
    return {"json": f"{d}/archive_index.json", "csv": f"{d}/archive_index.csv", "gaps": f"{d}/gaps.csv",
            "sums": f"{d}/SHA256SUMS.txt", "journal": f"{d}/verified_journal.csv"}
QUARANTINE = "Quarantine"
FINGERPRINTS = "Fingerprints"
PLAYLISTS = "Playlists"
FRAMEHASH_SUFFIX = ".framehash.txt"
PLAYLIST_SUFFIX = ".m3u"

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


def _top_and_name(seg: Any) -> tuple[str, str, str]:
    """(camera or quarantine folder, day folder, file name without extension)."""
    cam = safe(seg["camera_id"])
    sid8 = str(seg["segment_id"]).replace("-", "")[:8]
    start, end = _parse(seg["start_utc"]), _parse(seg["end_utc"])
    top = f"{ROOT}/{cam}/{QUARANTINE}" if seg["state"] == "CORRUPT" else f"{ROOT}/{cam}"
    if start is None:
        return top, "undated", f"{safe(str(seg['run_id']))}_seq{int(seg['seq']):06d}_{sid8}"
    e = end.strftime("%H%M%SZ") if end is not None else "open"
    return top, start.strftime("%Y-%m-%d"), f"{start.strftime('%H%M%SZ')}_{e}_{sid8}"


def base_rel_path(seg: Any) -> str:
    """Path of the segment's media file on the drive, without extension (``seg`` = ledger row)."""
    top, day, name = _top_and_name(seg)
    return f"{top}/{day}/{name}"


def media_rel_path(seg: Any) -> str:
    ext = "." + str(seg["rel_path"]).rsplit(".", 1)[-1] if "." in str(seg["rel_path"]).rsplit("/", 1)[-1] else ".ts"
    return base_rel_path(seg) + safe(ext)


def framehash_rel_path(seg: Any) -> str:
    """Sidecars sit in ``Fingerprints/<day>/`` beside the day folders, under the same name as the video."""
    top, day, name = _top_and_name(seg)
    return f"{top}/{FINGERPRINTS}/{day}/{name}{FRAMEHASH_SUFFIX}"


def playlist_dir(camera_id: str) -> str:
    return f"{ROOT}/{safe(camera_id)}/{PLAYLISTS}"


def playlist_rel_path(camera_id: str, local_day: str) -> str:
    return f"{playlist_dir(camera_id)}/{safe(local_day)}{PLAYLIST_SUFFIX}"


def rel_path_for(seg: Any, kind: str) -> str:
    return framehash_rel_path(seg) if kind == KIND_FRAMEHASH else media_rel_path(seg)
