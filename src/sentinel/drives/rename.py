"""Give a Sentinel drive a new permanent name (owner request 2026-11-12).

The name is the one written on the sticker: it is the drive's volume label (what Windows shows) and the
``label`` in its ``SENTINEL_DRIVE.json``. Renaming changes nothing else: the drive keeps its identity
(``drive_uuid``, volume and filesystem IDs), its recordings and its records. The rename is recorded in
the tamper-evident log (event ``drive_renamed``), so earlier records under the old name stay traceable.

A name is refused while another drive has it: by Sentinel's records (the latest name each drive was set
up or renamed with) or on a connected volume. Administrator only (the CLI checks).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .marker import MARKER_NAME, validate_marker
from .policy import _LABEL_RE, LABEL_MAX, DrivePolicy
from .prepare import AUDIT_CATEGORY, AuditSink

RENAMED = "drive_renamed"


class RenameRefused(Exception):
    """Nothing was changed."""


def current_names(audit: AuditSink) -> dict[str, str]:
    """drive_uuid -> its current name, from the set-up and rename records (latest wins)."""
    out: dict[str, str] = {}
    rows = audit.query("SELECT code, detail FROM events WHERE category=? AND code IN ('drive_prepared', ?) "
                       "ORDER BY event_id", (AUDIT_CATEGORY, RENAMED))
    for row in rows:
        try:
            d = json.loads(row[1] or "{}")
        except ValueError:
            continue
        uid = str(d.get("drive_uuid") or "")
        name = d.get("new_label") if row[0] == RENAMED else d.get("label")
        if uid and name:
            out[uid] = str(name).upper()
    return out


def check_name(name: str, policy: DrivePolicy) -> str:
    """The name as it will be written, or RenameRefused."""
    n = (name or "").strip().upper()
    limit = LABEL_MAX.get(policy.approved_filesystem, 11)
    if not _LABEL_RE.fullmatch(n) or len(n) > limit or not n[0].isalpha():
        raise RenameRefused(f"“{name}” cannot be a drive name: use letters A-Z, digits and _ only, starting "
                            f"with a letter, at most {limit} characters (for example SSD_1).")
    return n


def windows_set_label(root: str, label: str) -> None:
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mount = str(root) if str(root).endswith("\\") else str(root) + "\\"
    if not k32.SetVolumeLabelW(mount, label):
        err = ctypes.get_last_error()
        raise OSError(err, f"Windows did not change the volume name (error {err})")


def _write_marker(root: Path, marker: dict[str, Any]) -> None:
    tmp = root / (MARKER_NAME + ".partial")
    blob = (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, root / MARKER_NAME)
    if json.loads((root / MARKER_NAME).read_bytes().decode("utf-8")) != marker:
        raise OSError("the drive's SENTINEL_DRIVE.json read back differently")


def rename_drive(root: str | Path, new_name: str, who: str, audit: AuditSink, policy: DrivePolicy, *,
                 other_labels: Iterable[str] = (), set_label: Callable[[str, str], None] | None = None,
                 now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Rename the Sentinel drive at ``root``. ``other_labels``: volume labels of the other connected
    drives. Returns the audit detail. Raises RenameRefused (nothing changed) or OSError."""
    from ..clock import iso_utc

    root = Path(root)
    if not (who or "").strip():
        raise RenameRefused("Say who is renaming the drive. Nothing was changed.")
    try:
        marker = json.loads((root / MARKER_NAME).read_bytes().decode("utf-8"))
    except FileNotFoundError:
        raise RenameRefused("Sentinel has not set up this drive, so it has no Sentinel name to change. "
                            "Nothing was changed.") from None
    except (OSError, ValueError) as exc:
        raise RenameRefused(f"The drive's SENTINEL_DRIVE.json cannot be read ({exc}). Nothing was changed.") from None
    if validate_marker(marker) is not None:
        raise RenameRefused("The drive's SENTINEL_DRIVE.json is not valid. Nothing was changed.")
    uid, old = str(marker["drive_uuid"]), str(marker["label"])
    name = check_name(new_name, policy)
    if name == old.upper():
        raise RenameRefused(f"The drive is already called {old}. Nothing was changed.")
    holder = next((u for u, n in current_names(audit).items() if n == name and u != uid), None)
    if holder is not None:
        raise RenameRefused(f"Another drive in Sentinel's records is already called {name}. Choose another name. "
                            "Nothing was changed.")
    if name in {(x or "").upper() for x in other_labels}:
        raise RenameRefused(f"Another connected drive is already called {name}. Nothing was changed.")
    if set_label is None:
        if sys.platform != "win32":
            raise RenameRefused("Renaming a drive is only available on Windows in this version. Nothing was changed.")
        set_label = windows_set_label
    set_label(str(root), name)
    stamp = iso_utc(now())
    new = dict(marker, label=name, renamed_utc=stamp, renamed_by=who.strip(),
               previous_labels=[*marker.get("previous_labels", []), old])
    try:
        _write_marker(root, new)
    except OSError:
        try:
            set_label(str(root), old)          # leave the drive as it was
        except OSError:
            pass
        raise
    from .prepare import _os_user

    detail = {"drive_uuid": uid, "old_label": old, "new_label": name, "who": who.strip(), "os_user": _os_user(),
              "renamed_utc": stamp}
    audit.add_event("audit", AUDIT_CATEGORY, RENAMED, f"Drive {old} renamed {name} by {who.strip()}", detail)
    return detail
