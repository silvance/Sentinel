"""Self-describing archive drive: ``Sentinel/INDEX/`` documents (pure builders, no I/O).

Built ONLY from ``archive_copies`` rows of the drive (= files read back and verified), so an
index can never list a file that is not verified on that drive. Copies are only ever added,
never removed, so an older index generation is always a correct (smaller) description: a drive
pulled while the index is being rewritten is still described correctly by whichever generation
each file holds. Between index rewrites, ``verified_journal.csv`` (one line appended and flushed
per verified file) documents the newest copies.

Formats:

* ``archive_index.json`` — ``format: "sentinel-archive-index"``, ``format_version: 1``,
  ``generation``, ``generated_utc``, ``camera_id``, ``display_timezone``, ``drive``,
  ``software``, ``policy``, ``verification``, ``coverage``, ``copies[]``, ``gaps[]``,
  ``not_on_this_drive[]``, ``notes[]``.
* ``archive_index.csv`` — one row per verified file.
* ``gaps.csv`` — recorded gaps overlapping the span on the drive, and segments in that span that
  are NOT on this drive (held on another archive drive, or not yet archived).
* ``SHA256SUMS.txt`` — ``<sha256>  <path>`` (paths relative to the drive root, ``/``), LF. Check
  with ``sha256sum -c Sentinel/INDEX/SHA256SUMS.txt`` from the drive root.
* ``verified_journal.csv`` — ``verified_utc,segment_id,kind,sha256,size_bytes,path``. A reader
  must ignore a final line that does not end with a newline (torn by a power cut).
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any
from zoneinfo import ZoneInfo

from ..clock import parse_iso_utc
from ..export.layout import local_text
from . import layout

FORMAT = "sentinel-archive-index"
FORMAT_VERSION = 1

JOURNAL_HEADER = "verified_utc,segment_id,kind,sha256,size_bytes,path\n"

README_TEXT = """SENTINEL ARCHIVE DRIVE
======================

This drive holds archive copies of recordings made by a Sentinel recording system.

* Sentinel/<camera>/<date>/ contains the original recording files, unchanged, named by their
  UTC start and end time. Files ending in .framehash.txt list a fingerprint for every picture.
* Sentinel/<camera>/Quarantine/ holds recordings that were damaged when they were made.
* Sentinel/INDEX/ describes everything on this drive: archive_index.csv (one line per file),
  gaps.csv (times with no recording, and recordings kept on a different archive drive), and
  SHA256SUMS.txt (a fingerprint of every file, to check the copies).
* Every file listed was read back from this drive and checked against the fingerprint taken
  when it was recorded.

Do not change, rename or delete anything on this drive. For review, ask for a review drive.
"""


def _local(iso: str | None, tz: ZoneInfo) -> str | None:
    return local_text(parse_iso_utc(iso), tz) if iso else None


def _flags(r: Any) -> list[str]:
    f = []
    if r["state"] == "INTERRUPTED" or r["interrupted"]:
        f.append("interrupted")
    if r["state"] == "CORRUPT":
        f.append("corrupt")
    if r["clock_anomaly"]:
        f.append("clock_anomaly")
    if r["warnings"]:
        try:
            f.extend(f"warning:{w}" for w in json.loads(r["warnings"]))
        except ValueError:
            f.append("warning:unparsed")
    return f


def build_index(*, generation: int, generated_utc: str, camera_id: str, tz: ZoneInfo, drive: dict[str, Any],
                software: dict[str, Any], policy: str, copies: list[Any], gaps: list[Any],
                not_on_drive: list[dict[str, Any]]) -> dict[str, bytes]:
    """Return {relative path: bytes} for the index files (the journal and README excluded)."""
    entries = []
    for r in copies:
        entries.append({
            "path": r["path"], "kind": r["kind"], "sha256": r["sha256"], "size_bytes": r["size_bytes"],
            "segment_id": r["segment_id"], "run_id": r["run_id"], "seq": r["seq"], "segment_state": r["state"],
            "start_utc": r["start_utc"], "end_utc": r["end_utc"], "start_local": _local(r["start_utc"], tz),
            "end_local": _local(r["end_utc"], tz), "duration_s": r["duration_s"],
            "sealed_utc": r["sealed_utc"], "verified_utc": r["verified_utc"], "verify_method": r["verify_method"],
            "already_present": bool(r["already_present"]), "content_sha256": r["content_sha256"],
            "content_hash_method": r["content_hash_method"], "video_codec": r["video_codec"],
            "width": r["width"], "height": r["height"], "flags": _flags(r),
        })
    media = [e for e in entries if e["kind"] in layout.MEDIA_KINDS]
    starts = [e["start_utc"] for e in media if e["start_utc"]]
    ends = [e["end_utc"] for e in media if e["end_utc"]]
    gap_list = [{
        "gap_id": g["gap_id"], "kind": g["kind"], "start_utc": g["start_utc"], "end_utc": g["end_utc"],
        "start_local": _local(g["start_utc"], tz), "end_local": _local(g["end_utc"], tz),
        "duration_s": g["duration_s"], "reason": g["reason"], "open": g["end_utc"] is None,
        "prev_segment_id": g["prev_segment_id"], "next_segment_id": g["next_segment_id"],
    } for g in gaps]
    doc = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "generation": generation,
        "generated_utc": generated_utc, "camera_id": camera_id, "display_timezone": str(tz.key),
        "drive": drive, "software": software, "policy": policy,
        "verification": {
            "rule": "Every file listed was read back from this drive and its SHA-256 equals the SHA-256 recorded "
                    "when the recording was sealed on the recording system.",
            "method_notes": "Read-back bypasses the operating-system cache (Windows FILE_FLAG_NO_BUFFERING; Linux "
                            "O_DIRECT). It cannot bypass a cache inside the drive or its USB adapter."},
        "coverage": {"first_start_utc": min(starts) if starts else None, "last_end_utc": max(ends) if ends else None,
                     "media_files": len(media), "sidecar_files": len(entries) - len(media),
                     "recorded_seconds": round(sum(float(e["duration_s"] or 0) for e in media), 3),
                     "bytes": sum(int(e["size_bytes"]) for e in entries)},
        "copies": entries, "gaps": gap_list, "not_on_this_drive": not_on_drive,
        "notes": ["Copies verified after this index was written are listed in verified_journal.csv.",
                  "Times are UTC unless marked local; local times use the display time zone."],
    }
    files: dict[str, bytes] = {}
    files[layout.INDEX_JSON] = json.dumps(doc, indent=1, sort_keys=True).encode("utf-8")

    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    cols = ["path", "kind", "sha256", "size_bytes", "segment_id", "run_id", "seq", "segment_state", "start_utc",
            "end_utc", "start_local", "end_local", "duration_s", "verified_utc", "verify_method", "flags"]
    w.writerow(cols)
    for e in entries:
        w.writerow([";".join(e[c]) if c == "flags" else ("" if e[c] is None else e[c]) for c in cols])
    files[layout.INDEX_CSV] = buf.getvalue().encode("utf-8")

    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["what", "start_utc", "end_utc", "start_local", "end_local", "duration_s", "reason", "detail"])
    for g in gap_list:
        w.writerow(["no_recording" if g["kind"] == "between_runs" else "gap_inside_recording", g["start_utc"] or "",
                    g["end_utc"] or "", g["start_local"] or "", g["end_local"] or "",
                    "" if g["duration_s"] is None else round(float(g["duration_s"]), 3), g["reason"],
                    "still open" if g["open"] else ""])
    for n in not_on_drive:
        w.writerow([n["what"], n["start_utc"] or "", n["end_utc"] or "", _local(n["start_utc"], tz) or "",
                    _local(n["end_utc"], tz) or "", "" if n["duration_s"] is None else n["duration_s"],
                    n["segment_state"], ";".join(n["other_drives"])])
    files[layout.GAPS_CSV] = buf.getvalue().encode("utf-8")

    files[layout.SUMS] = "".join(f"{e['sha256']}  {e['path']}\n" for e in entries).encode("utf-8")
    return files


def journal_line(verified_utc: str, segment_id: str, kind: str, sha256: str, size: int, path: str) -> str:
    return f"{verified_utc},{segment_id},{kind},{sha256},{size},{path}\n"


def parse_journal(data: bytes) -> list[dict[str, str]]:
    """Complete lines only (a torn last line without a newline is ignored)."""
    text = data.decode("utf-8", "replace")
    lines = text.split("\n")
    out = []
    for ln in lines[1:-1]:   # skip header; the element after the last "\n" is incomplete or empty
        parts = ln.split(",")
        if len(parts) != 6 or len(parts[3]) != 64:
            continue
        out.append(dict(zip(("verified_utc", "segment_id", "kind", "sha256", "size_bytes", "path"), parts)))
    return out
