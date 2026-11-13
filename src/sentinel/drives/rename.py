"""Give a Sentinel drive a new permanent name (owner request 2026-11-12).

The name is the one written on the sticker: it is the drive's volume label (what Windows shows) and the
``label`` in its ``SENTINEL_DRIVE.json``. Renaming changes nothing else: the drive keeps its identity
(``drive_uuid``, volume and filesystem IDs), its recordings and its records. The rename is recorded in
the tamper-evident log (event ``drive_renamed``), so earlier records under the old name stay traceable.

A name is refused while another drive has it: by Sentinel's records (the latest name each drive was set
up or renamed with) or on a connected volume. Administrator only (the CLI checks).

Review 2026-11-12: the rename is recorded as authorised (``drive_rename_authorized``) before the drive is
changed, and ledger writes are retried while the ledger is locked. If the final record still cannot be
written the drive is put back to its old name; if even that fails, running the same rename again records
it. The command line can be bound to the drive the person selected (``expect_uuid``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .marker import MARKER_NAME, validate_marker
from .policy import _LABEL_RE, LABEL_MAX, DrivePolicy
from .prepare import AUDIT_CATEGORY, AuditSink

RENAMED = "drive_renamed"
RENAME_AUTHORIZED = "drive_rename_authorized"


class RenameRefused(Exception):
    """Nothing was changed."""


class RenameNotRecorded(Exception):
    """The drive carries the new name, but the record of the rename could not be written."""


def _name_records(audit: AuditSink) -> tuple[dict[str, str], dict[str, str]]:
    """(drive_uuid -> current name, drive_uuid -> name of a rename authorised after that but never recorded
    as done). Review 2026-11-12: the rename is recorded as authorised before the drive is changed, so a
    rename whose final record failed still holds its name."""
    current: dict[str, str] = {}
    pending: dict[str, str] = {}
    rows = audit.query("SELECT code, detail FROM events WHERE category=? AND code IN ('drive_prepared', ?, ?) "
                       "ORDER BY event_id", (AUDIT_CATEGORY, RENAMED, RENAME_AUTHORIZED))
    for row in rows:
        try:
            d = json.loads(row[1] or "{}")
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        uid = str(d.get("drive_uuid") or "")
        name = d.get("label") if row[0] == "drive_prepared" else d.get("new_label")
        if not (uid and name):
            continue
        if row[0] == RENAME_AUTHORIZED:
            pending[uid] = str(name).upper()
        else:
            current[uid] = str(name).upper()
            pending.pop(uid, None)
    return current, pending


def current_names(audit: AuditSink) -> dict[str, str]:
    """drive_uuid -> its current name, from the set-up and rename records (latest wins)."""
    return _name_records(audit)[0]


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
                 now: Callable[[], float] = time.time, expect_uuid: str | None = None) -> dict[str, Any]:
    """Rename the Sentinel drive at ``root``. ``other_labels``: volume labels of the other connected
    drives. ``expect_uuid``: the drive_uuid of the drive the person selected (refused if the drive at
    ``root`` is another one). Returns the audit detail. Raises RenameRefused (nothing changed),
    RenameNotRecorded (renamed, record missing) or OSError (the drive was put back as it was)."""
    from ..clock import iso_utc
    from .prepare import _os_user, record_event

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
    if expect_uuid is not None and expect_uuid.strip().lower() != uid.lower():
        # Review 2026-11-12: the window's list can be minutes old, and drive letters move between drives.
        raise RenameRefused(f"The drive at {root} is {old}, not the drive that was selected: it was replaced or "
                            "the drive letters changed. Nothing was changed. Select the drive in the list again and try again.")
    name = check_name(new_name, policy)
    current, pending = _name_records(audit)
    if name == old.upper():
        if pending.get(uid) == name and current.get(uid) != name:
            # A rename whose record could not be written (review 2026-11-12): write it now.
            detail = {"drive_uuid": uid, "old_label": (marker.get("previous_labels") or [current.get(uid) or ""])[-1],
                      "new_label": name, "who": who.strip(), "os_user": _os_user(),
                      "renamed_utc": str(marker.get("renamed_utc") or iso_utc(now())), "recorded_late": True}
            try:
                record_event(audit, RENAMED, f"Drive {detail['old_label']} renamed {name} by {who.strip()}", detail)
            except sqlite3.Error as exc:
                raise RenameNotRecorded(f"The drive is called {name}, but Sentinel still cannot record the rename "
                                        f"({exc}). Run the same rename again in a minute.") from None
            return detail
        raise RenameRefused(f"The drive is already called {old}. Nothing was changed.")
    held = {u: {n for n in (current.get(u), pending.get(u)) if n} for u in {*current, *pending}}
    if any(name in names for u, names in held.items() if u != uid):
        raise RenameRefused(f"Another drive in Sentinel's records is already called {name}. Choose another name. "
                            "Nothing was changed.")
    if name in {(x or "").upper() for x in other_labels}:
        raise RenameRefused(f"Another connected drive is already called {name}. Nothing was changed.")
    if set_label is None:
        if sys.platform != "win32":
            raise RenameRefused("Renaming a drive is only available on Windows in this version. Nothing was changed.")
        set_label = windows_set_label
    stamp = iso_utc(now())
    detail = {"drive_uuid": uid, "old_label": old, "new_label": name, "who": who.strip(), "os_user": _os_user(),
              "renamed_utc": stamp}
    # Review 2026-11-12: the record comes first. A drive is never renamed without a record that it may carry
    # the new name (so that name, and its number, are not given to another drive).
    try:
        record_event(audit, RENAME_AUTHORIZED, f"Drive {old} to be renamed {name} by {who.strip()}", detail)
    except sqlite3.Error as exc:
        raise RenameRefused(f"Sentinel's records could not be written ({exc}). Nothing was changed. "
                            "Try again in a minute.") from None
    set_label(str(root), name)
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
    try:
        record_event(audit, RENAMED, f"Drive {old} renamed {name} by {who.strip()}", detail)
    except sqlite3.Error as exc:
        # Put the drive back: a rename is either done and recorded, or not done.
        not_recorded = RenameNotRecorded(
            f"The drive is now called {name}, but Sentinel could not record the rename ({exc}). Run the same rename "
            "again in a minute to record it (on the command line; the window cannot), or contact technical support.")
        try:
            set_label(str(root), old)
        except OSError:
            raise not_recorded from None
        try:
            _write_marker(root, marker)
        except OSError:
            try:
                set_label(str(root), name)     # keep the volume name and SENTINEL_DRIVE.json agreeing
            except OSError:
                pass
            raise not_recorded from None
        raise RenameRefused(f"Sentinel could not record the rename ({exc}), so the drive keeps its name {old}. "
                            "Nothing was changed. Try again in a minute.") from None
    return detail
