"""Drives the ledger already knows as Sentinel drives (review finding 1.1).

A drive whose ``drive_uuid``, volume GUID, filesystem serial or hardware identity appears in the
ledger was prepared by (or exported to by) this appliance, and may hold the only copy of
recordings. Such a drive is SENTINEL_EVIDENCE and is never erased by this tool, whatever is (or
is not) visible on it now — its marker may be deleted, corrupted, or from a newer version.

Sources:
* ``events`` rows with ``category='drives'`` and ``code='drive_prepared'`` (written by this
  package; ``detail`` holds ``drive_uuid``, ``volume_id``, ``disk`` and ``identity_after``).
* a ``drives`` table, if the export branch has created one (docs/04 §5). Its exact schema is not
  known to this branch, so only columns with recognised names are read.

Consequence (design choice, open for the owner): a drive once prepared by Sentinel cannot be
re-prepared by this tool, even when it looks blank. Recycling old archive/review drives needs a
documented procedure outside the operator tool (e.g. technical support after retention review).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .model import DiskInfo, norm_serial, serial_is_placeholder, unique_id_is_placeholder

_DRIVES_TABLE_COLUMNS = {
    "drive_uuid": "uuid", "volume_id": "vol", "volume_guid": "vol", "fs_uuid": "fs", "filesystem_uuid": "fs",
    "fs_serial": "fs", "unique_id": "uid", "wwn": "uid", "serial": "serial",
}


@dataclass(frozen=True)
class KnownDrives:
    drive_uuids: frozenset[str] = frozenset()
    volume_ids: frozenset[str] = frozenset()
    fs_serials: frozenset[str] = frozenset()
    unique_ids: frozenset[str] = frozenset()
    hardware: frozenset[tuple[str, str, int]] = frozenset()   # (serial, model, size)
    serials: frozenset[str] = frozenset()                     # from a drives table without model/size
    error: str | None = None                                  # the ledger could not be read

    def matches(self, disk: DiskInfo, marker_uuids: tuple[str, ...] = ()) -> list[str]:
        why = []
        if any(u in self.drive_uuids for u in marker_uuids):
            why.append("known_drive_uuid")
        if any(norm_volume_id(v.volume_id) in self.volume_ids for v in disk.volumes):
            why.append("known_volume")
        if any(v.fs_serial and v.fs_serial.lower() in self.fs_serials for v in disk.volumes):
            why.append("known_filesystem")
        if serial_is_placeholder(disk.serial):
            # Field report 2026-11-10: a dock that reports one placeholder serial (eighteen zeros) for
            # every disk. Its serial and UniqueId identify the dock or bay, not the disk, so they would
            # mark every new disk as a known Sentinel drive. Only the disk's own contents (marker,
            # volume GUIDs, filesystem serials above) identify it. Consequence: a Sentinel drive that
            # was reformatted outside Sentinel is not recognised in such a dock (docs/04 §4).
            return why
        # Field report 2026-11-12: a USB bridge's filler UniqueId (5000000000000001) is shared by unrelated
        # drives (a new Samsung T7 and the Sabrent dock's disks), so it identifies nothing.
        if (disk.unique_id and not unique_id_is_placeholder(disk.unique_id)
                and disk.unique_id.lower() in self.unique_ids):
            why.append("known_hardware")
        elif disk.serial and ((disk.serial.lower(), (disk.model or "").lower(), disk.size_bytes) in self.hardware
                              or disk.serial.lower() in self.serials):
            why.append("known_hardware")
        return why


_VOLUME_PREFIX = "\\\\?\\volume"


def norm_volume_id(value: str) -> str:
    """Compare Windows volume GUIDs in one form (integration fix): this package records
    ``\\\\?\\Volume{GUID}\\`` while the export branch's ``drives.volume_guid`` stores the bare
    ``{GUID}``. Both become ``{guid}``. Linux volume ids (filesystem UUID / ``dev:``) are unchanged
    apart from case."""
    v = str(value).strip().lower()
    if v.startswith(_VOLUME_PREFIX):
        v = v[len(_VOLUME_PREFIX):]
    return v.rstrip("\\")


def _add(acc: dict[str, set[Any]], kind: str, value: Any) -> None:
    if value is None or value == "":
        return
    if kind == "uid" and unique_id_is_placeholder(str(value)):
        return  # a bridge's filler value names no drive (field report 2026-11-12)
    acc[kind].add(norm_volume_id(value) if kind == "vol" else str(value).strip().lower())


def load_known(audit: Any) -> KnownDrives:
    """Read known drives from the ledger. A read failure is reported in ``error`` (callers fail closed)."""
    acc: dict[str, set[Any]] = {k: set() for k in ("uuid", "vol", "fs", "uid", "serial")}
    hw: set[tuple[str, str, int]] = set()
    try:
        rows = audit.query("SELECT detail FROM events WHERE category='drives' AND code='drive_prepared'")
        for row in rows:
            try:
                d = json.loads(row[0] or "{}")
            except ValueError:
                continue
            _add(acc, "uuid", d.get("drive_uuid"))
            _add(acc, "vol", d.get("volume_id"))
            for key in ("disk", "identity_after"):
                ident = d.get(key) or {}
                if not serial_is_placeholder(ident.get("serial")):
                    # Behind a dock that hides serials the UniqueId is the dock's or bay's, not the disk's.
                    _add(acc, "uid", ident.get("unique_id"))
                for v in ident.get("volume_ids") or []:
                    _add(acc, "vol", v)
                for s in ident.get("fs_serials") or []:
                    _add(acc, "fs", s)
                serial = norm_serial(ident.get("serial"))
                if serial and ident.get("size_bytes"):
                    hw.add((serial.lower(), str(ident.get("model") or "").lower(), int(ident["size_bytes"])))
        cols = [r[1] for r in audit.query("PRAGMA table_info(drives)")]
        use = [c for c in cols if c in _DRIVES_TABLE_COLUMNS]
        if use:
            for row in audit.query(f"SELECT {', '.join(use)} FROM drives"):
                for c, v in zip(use, row):
                    _add(acc, _DRIVES_TABLE_COLUMNS[c], norm_serial(v) if c == "serial" else v)
        if "identity" in cols:
            # Integration: the export branch keeps the Windows filesystem serial only inside the
            # identity JSON, as 8 hex digits (GetVolumeInformationW); this package compares the
            # decimal form (Win32_Volume.SerialNumber).
            for (raw_ident,) in audit.query("SELECT identity FROM drives"):
                try:
                    vs = (json.loads(raw_ident or "{}") or {}).get("volume_serial")
                    n = int(str(vs), 16) if vs else 0
                except (ValueError, TypeError, AttributeError):
                    continue
                if n:
                    _add(acc, "fs", str(n))
    except Exception as exc:  # noqa: BLE001 - any ledger problem must block erasing, not crash classification
        return KnownDrives(error=f"{type(exc).__name__}: {exc}")
    return KnownDrives(frozenset(acc["uuid"]), frozenset(acc["vol"]), frozenset(acc["fs"]), frozenset(acc["uid"]),
                       frozenset(hw), frozenset(acc["serial"]))
