"""Per-user display preference (Light / Dark / Follow Windows).

Stored in the operator's own profile, never in the recorder's data folder (operators cannot write
there when the recorder runs as a service):

* Windows: ``%APPDATA%\\Sentinel\\gui-preferences.json``
* Linux/other: ``$XDG_CONFIG_HOME/sentinel/gui-preferences.json`` (default ``~/.config``)

Anything missing, unreadable or unknown falls back to "auto" (Follow Windows). Saving is
best-effort: a failure to save never affects the screen, it only means the choice is not remembered.

This is an ordinary appearance preference. It does not and must not change whether the window, its
taskbar entry or its notifications are visible.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .theme import APPEARANCES

FILE_NAME = "gui-preferences.json"
DEFAULT_APPEARANCE = "auto"


def default_prefs_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "Sentinel" / FILE_NAME
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "sentinel" / FILE_NAME


def load_appearance(path: Path | None = None) -> str:
    path = path or default_prefs_path()
    try:
        doc = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return DEFAULT_APPEARANCE
    value = doc.get("appearance") if isinstance(doc, dict) else None
    return value if value in APPEARANCES else DEFAULT_APPEARANCE


def save_appearance(value: str, path: Path | None = None) -> bool:
    """Remember the choice; returns False (and changes nothing else) if it cannot be saved."""
    if value not in APPEARANCES:
        raise ValueError(f"unknown appearance {value!r}")
    path = path or default_prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            doc = json.loads(path.read_text("utf-8"))
            if not isinstance(doc, dict):
                doc = {}
        except (OSError, ValueError):
            doc = {}
        doc["appearance"] = value
        tmp = path.with_name(path.name + ".partial")
        tmp.write_text(json.dumps(doc, indent=2), "utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False
