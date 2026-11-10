"""Refuse to record with files or folders that other local accounts could change.

The recorder trusts its configuration (camera URL, storage folders), the camera credentials, the
FFmpeg/FFprobe programs it runs and the folders it writes evidence into. If a standard user
could replace any of them, that user could redirect recording, swap FFmpeg for another program,
or plant files among the recordings. Before recording starts (``service.run_recorder`` and the
launcher), every such location is checked with ``Platform.write_access_problems``:

* Windows: no allow-ACE granting write/create/delete/permission-change to Everyone,
  Authenticated Users, Users, Interactive, Guests, Anonymous or Power Users (inherit-only ACEs
  ignored). ``%ProgramData%`` itself fails this check (Users may create folders and files in
  it), which is why first-run setup creates ``%ProgramData%\\Sentinel`` with a private DACL.
* Linux: not world-writable, not group-writable (unless the group is root), owned by root or
  the recorder's own account.

A data folder that does not exist yet is judged by its nearest existing parent, because the
recorder will create it there and it inherits that parent's permissions.
"""

from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .platform import Platform, get_platform


def nearest_existing(path: Path) -> Path:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def location_problems(config_path: Path, cfg: AppConfig, platform: Platform | None = None) -> list[str]:
    """Human-readable problems for administrators (empty = all locations acceptable)."""
    platform = platform or get_platform()
    config_path = Path(config_path).resolve()
    targets: list[tuple[str, Path]] = [("configuration file", config_path),
                                       ("configuration folder", config_path.parent)]
    if cfg.camera.credentials_file and Path(cfg.camera.credentials_file).exists():
        cred = Path(cfg.camera.credentials_file)
        targets += [("credentials file", cred), ("credentials folder", cred.parent)]
    for label, folder in (("recordings folder", cfg.spool_path), ("data folder", cfg.data_path),
                          ("log folder", cfg.log_path)):
        existing = nearest_existing(folder)
        targets.append((label if existing == folder else f"{label} (parent)", existing))
    for tool in (cfg.tools.ffmpeg, cfg.tools.ffprobe):
        p = Path(tool)
        if p.is_absolute() and p.exists():
            targets += [("FFmpeg program", p), ("FFmpeg folder", p.parent)]
    problems: list[str] = []
    seen: set[Path] = set()
    for label, p in targets:
        if p in seen:
            continue
        seen.add(p)
        problems += [f"{label} {p} {issue}" for issue in platform.write_access_problems(p)]
    return problems
