"""Save a diagnostic report to a folder the operator chose (default: the Desktop).

The report is built by :func:`sentinel.diagnostics.build_report` in a private temporary folder
and then copied to the destination. Building directly into the destination would let
``build_report``'s housekeeping prune older ``Sentinel_Diagnostics_*.zip`` files in a folder
the user owns (e.g. their Desktop); the GUI must never delete user files. The copy is written as
``.partial``, read back and hash-compared, then renamed; an existing file is never overwritten.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from ..config import AppConfig
from ..diagnostics import build_report


class ReportError(RuntimeError):
    """Plain-language failure; the original exception is chained for administrators."""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _free_name(dest_dir: Path, name: str) -> Path:
    target = dest_dir / name
    stem, suffix = target.stem, target.suffix
    n = 2
    while target.exists():
        target = dest_dir / f"{stem}_{n}{suffix}"
        n += 1
    return target


def save_diagnostic_report(cfg: AppConfig, dest_dir: Path, reason: str = "requested from operator screen") -> Path:
    dest_dir = Path(dest_dir)
    if not dest_dir.is_dir():
        raise ReportError("The chosen folder does not exist.")
    try:
        with tempfile.TemporaryDirectory(prefix="sentinel_report_") as td:
            built = build_report(cfg, reason, Path(td))
            target = _free_name(dest_dir, built.name)
            partial = target.with_name(target.name + ".partial")
            shutil.copyfile(built, partial)
            if _sha256(partial) != _sha256(built):
                partial.unlink(missing_ok=True)
                raise ReportError("The report could not be saved correctly. Try another location.")
            if target.exists():   # appeared meanwhile: never overwrite
                target = _free_name(dest_dir, built.name)
            os.replace(partial, target)
            return target
    except ReportError:
        raise
    except Exception as exc:  # any failure (disk, ledger, permissions) becomes one plain message
        raise ReportError("The report could not be saved in that location. Try another location.") from exc
