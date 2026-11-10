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

from .model import DiskInfo, norm_serial

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
        if any(v.volume_id.lower() in self.volume_ids for v in disk.volumes):
            why.append("known_volume")
        if any(v.fs_serial and v.fs_serial.lower() in self.fs_serials for v in disk.volumes):
            why.append("known_filesystem")
        if disk.unique_id and disk.unique_id.lower() in self.unique_ids:
            why.append("known_hardware")
        elif disk.serial and ((disk.serial.lower(), (disk.model or "").lower(), disk.size_bytes) in self.hardware
                              or disk.serial.lower() in self.serials):
            why.append("known_hardware")
        return why


def _add(acc: dict[str, set[Any]], kind: str, value: Any) -> None:
    if value is None or value == "":
        return
    acc[kind].add(str(value).strip().lower())


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
    except Exception as exc:  # noqa: BLE001 - any ledger problem must block erasing, not crash classification
        return KnownDrives(error=f"{type(exc).__name__}: {exc}")
    return KnownDrives(frozenset(acc["uuid"]), frozenset(acc["vol"]), frozenset(acc["fs"]), frozenset(acc["uid"]),
                       frozenset(hw), frozenset(acc["serial"]))
