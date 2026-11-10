"""Where is the archive drive? (Detection of an inserted drive.)

Drive enumeration, classification ("is this a prepared Sentinel drive?") and preparation belong
to ``sentinel.drives`` (another branch). The archive worker only needs a :class:`DriveLocator`
that returns the currently attached archive drive as an
:class:`~sentinel.export.target.ExportTarget` (mounted root + stable identity), or ``None``.

:class:`ConfiguredPathLocator` (shipped default) watches one configured mounted folder
(``[archive] drive_path``: a drive letter on Windows, a fixed mount point on Linux). When a
different disk appears at the same place (operator swapped drives), its identity differs
(``SENTINEL_DRIVE.json`` ``drive_uuid`` / volume GUID), so it is treated as a new drive.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from ..export.target import DRIVE_MARKER, ExportTarget, TargetError, identify_target

log = logging.getLogger(__name__)


class DriveLocator(Protocol):
    def locate(self) -> ExportTarget | None:
        """The archive drive currently attached, or None. Must not raise for 'absent'."""


class DriveUnusable(Exception):
    """A drive is attached at the archive location but must not be written (operator alert A-005)."""


class ConfiguredPathLocator:
    def __init__(self, path: str | Path, require_marker: bool = True) -> None:
        self.path = Path(path)
        self.require_marker = require_marker

    def locate(self) -> ExportTarget | None:
        try:
            if not self.path.is_dir():
                return None
            target = identify_target(self.path)
        except (OSError, TargetError) as exc:
            log.debug("archive drive not available at %s: %s", self.path, exc)
            return None
        if self.require_marker and not target.identity.get("drive_uuid"):
            # Present but not prepared: reported as unusable by the worker (never written).
            raise DriveUnusable(f"no valid {DRIVE_MARKER} (drive_uuid) at the archive drive root; prepare the drive "
                                f"first")
        return target
