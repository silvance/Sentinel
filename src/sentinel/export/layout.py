"""Reviewer-facing drive layout (requirement S5). Pure functions, no I/O.

::

    README_FIRST.txt
    Recordings/<YYYY-MM-DD>/<HHMMSS>-<HHMMSS>_<TZ>.mp4      local date/time in the display timezone
    Originals/<camera>/<run>/seg_NNNNNN.ts (+ .framehash.txt) evidential originals, unchanged
    Quarantine/<camera>/<run>/seg_NNNNNN.ts                   damaged originals, kept for examination
    Manifests/<stamp>_<id8>/Export_Manifest.json | .csv | SHA256SUMS.txt
    Reports/Export_Summary_<stamp>_<id8>.html

Naming decisions (design choices, see docs/branches/req-review-drive-export.md):

* Times are **seconds** precision (``HHMMSS``): after a gap two files can start in the same
  minute, and minute-only names would then collide or mislead.
* ``<TZ>`` is the zone abbreviation **and** the UTC offset, e.g. ``EST_UTC-0500``; plain
  ``UTC`` for UTC. The offset makes names unambiguous across the daylight-saving fall-back hour
  (01:00–02:00 happens twice) and for abbreviations shared by several zones (``CST``). Zones
  without a letter abbreviation get only the offset (``UTC+0530``).
* Characters are restricted to ``[0-9A-Za-z_+-.]`` so names are valid on NTFS, exFAT and ext4.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

RECORDINGS = "Recordings"
ORIGINALS = "Originals"
QUARANTINE = "Quarantine"
MANIFESTS = "Manifests"
REPORTS = "Reports"
README = "README_FIRST.txt"

_SAFE = re.compile(r"[^0-9A-Za-z_+\-.]")


def to_local(epoch_s: float, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(epoch_s, tz=UTC).astimezone(tz)


def offset_text(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}{(total % 3600) // 60:02d}"


def tz_label(dt: datetime) -> str:
    """``UTC`` | ``EST_UTC-0500`` | ``UTC+0530`` (abbreviation omitted when it is only digits)."""
    off = dt.utcoffset() or timedelta(0)
    abbr = dt.tzname() or ""
    if off == timedelta(0) and abbr in ("UTC", "GMT", "Z", ""):
        return "UTC"
    abbr = _SAFE.sub("", abbr)
    if not abbr or not re.search(r"[A-Za-z]", abbr) or abbr.upper().startswith(("UTC", "GMT")):
        return f"UTC{offset_text(dt)}"
    return f"{abbr}_UTC{offset_text(dt)}"


def hour_key(epoch_s: float, tz: ZoneInfo) -> tuple[str, int, int]:
    """Local (date, hour, utc-offset minutes): segments sharing it go into one hourly file. The
    offset is part of the key so the repeated DST hour gives two files, never one."""
    dt = to_local(epoch_s, tz)
    off = int((dt.utcoffset() or timedelta(0)).total_seconds() // 60)
    return dt.strftime("%Y-%m-%d"), dt.hour, off


def _hms(dt: datetime) -> str:
    return dt.strftime("%H%M%S")


def mp4_rel_path(start_epoch: float, end_epoch: float, tz: ZoneInfo, suffix: str = "") -> str:
    s = to_local(round(start_epoch), tz)
    e = to_local(round(end_epoch), tz)
    name = f"{_hms(s)}-{_hms(e)}_{tz_label(s)}{suffix}.mp4"
    return f"{RECORDINGS}/{s.strftime('%Y-%m-%d')}/{name}"


def original_rel_path(spool_rel: str) -> str:
    return f"{ORIGINALS}/{spool_rel}"


def quarantine_rel_path(spool_rel: str) -> str:
    return f"{QUARANTINE}/{spool_rel}"


def export_stamp(epoch_s: float, tz: ZoneInfo) -> str:
    dt = to_local(epoch_s, tz)
    return f"{dt.strftime('%Y-%m-%d_%H%M%S')}_{tz_label(dt)}"


def manifest_dir(stamp: str, batch_id: str) -> str:
    return f"{MANIFESTS}/Export_{stamp}_{batch_id[:8]}"


def summary_rel_path(stamp: str, batch_id: str) -> str:
    return f"{REPORTS}/Export_Summary_{stamp}_{batch_id[:8]}.html"


def local_text(epoch_s: float, tz: ZoneInfo) -> str:
    """Human text for reports, e.g. '2026-11-09 14:00:05 EST (UTC-05:00)'."""
    dt = to_local(epoch_s, tz)
    o = offset_text(dt)
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {dt.tzname()} (UTC{o[:3]}:{o[3:]})"
