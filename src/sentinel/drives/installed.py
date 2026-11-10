"""Is this the configuration of the recorder installed on this computer? (review finding 1.3)

The disks that must never be erased (spool, ledger, logs) are taken from the configuration. An
administrator passing the wrong ``--config`` (a copy, a test file, another appliance's) would
therefore protect the wrong disks. Erasing is refused unless ALL of these hold:

1. the file is the installed configuration path (agreed at integration with packaging and the
   Windows service, ``sentinel.paths``): Windows ``%ProgramData%\\Sentinel\\config\\sentinel.toml``;
   Linux ``/etc/sentinel/sentinel.toml``;
2. its ``spool_dir`` and ``data_dir`` exist as directories (no "nearest existing parent" guess);
3. ``<data_dir>/sentinel.db`` exists and is a Sentinel ledger (``schema_meta.schema_version``);
4. ``<data_dir>/recorder.lock`` exists — the recorder service has run with this configuration
   (it creates and locks this file on every start, see ``service.py``).

Limits: this establishes that the configuration is the installed one and has been used by a
recorder; it cannot prove that the recorder is *currently* running with it. Listing/classifying
drives does not require it; erasing does.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


def installed_config_path() -> Path:
    """Integration: the one installed location shared with packaging (``sentinel.paths``) and the
    Windows service: ``%ProgramData%\\Sentinel\\config\\sentinel.toml`` on Windows,
    ``/etc/sentinel/sentinel.toml`` on Linux."""
    from ..paths import default_layout

    if sys.platform == "win32":
        return default_layout("win32").config_file
    return default_layout(sys.platform, is_root=True).config_file


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def installed_config_problems(config_path: str | Path | None, cfg: Any,
                              expected: Path | None = None) -> list[str]:
    problems: list[str] = []
    expected = expected or installed_config_path()
    if config_path is None or not _same(Path(config_path), expected):
        problems.append(f"the configuration given is not the installed recorder configuration ({expected})")
    spool, data = Path(cfg.storage.spool_dir), Path(cfg.storage.data_dir)
    if not spool.is_dir():
        problems.append("the recording storage folder named in the configuration does not exist")
    if not data.is_dir():
        problems.append("the data folder named in the configuration does not exist")
    db = data / "sentinel.db"
    if not db.is_file():
        problems.append("no recorder ledger found in the data folder")
    else:
        try:
            conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            finally:
                conn.close()
            if row is None:
                problems.append("the ledger in the data folder is not a Sentinel ledger")
        except sqlite3.Error as exc:
            problems.append(f"the ledger in the data folder cannot be read ({exc})")
    if not (data / "recorder.lock").is_file():
        problems.append("the recorder has never run with this configuration")
    return problems
