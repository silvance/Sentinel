"""Install layout: where the program, its bundled tools, the configuration and the data live.

Sentinel ships as a folder (see ``packaging/``)::

    Sentinel\\
        Sentinel.exe            <- the one program an operator starts
        _internal\\              <- Python runtime and libraries (PyInstaller one-folder build)
        ffmpeg\\ffmpeg.exe       <- bundled FFmpeg / FFprobe, used before anything on PATH
        ffmpeg\\ffprobe.exe
        BUILD-MANIFEST.json     <- what was built, from what, with SHA-256 of every file

Resolution rules (documented in docs/08_PACKAGING.md):

* **Tools.** ``[tools] ffmpeg/ffprobe`` in the configuration always win. When they are not set,
  a *frozen* build uses ``<app>/ffmpeg/<tool>[.exe]`` if that file exists; otherwise the bare
  name is used and the OS searches PATH (the behaviour of a source checkout).
* **Configuration file**, first that exists: ``--config``; ``$SENTINEL_CONFIG``;
  ``<app>/sentinel.toml`` (frozen builds only, a technician-preconfigured "portable" folder);
  the per-machine file; on Linux without root, the per-user file.
* **Data defaults** (used when ``[storage]`` keys are omitted, and offered by first-run setup):

  ============  ==========================================  =====================================
  Platform      Configuration / credentials                 spool, data, logs
  ============  ==========================================  =====================================
  Windows       ``%ProgramData%\\Sentinel\\sentinel.toml``     ``%ProgramData%\\Sentinel\\spool|data|logs``
  Linux, root   ``/etc/sentinel/sentinel.toml``             ``/var/lib/sentinel/spool|data``, ``/var/log/sentinel``
  Linux, user   ``$XDG_CONFIG_HOME/sentinel/sentinel.toml``  ``$XDG_DATA_HOME/sentinel/spool|data|logs``
  ============  ==========================================  =====================================

Every function takes its environment as optional arguments so the rules are unit-tested for
both platforms on either platform.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

CONFIG_ENV = "SENTINEL_CONFIG"
CONFIG_NAME = "sentinel.toml"
SECRET_NAME = "camera.secret"
TOOLS_SUBDIR = "ffmpeg"
TOOL_NAMES = ("ffmpeg", "ffprobe")
MANIFEST_NAME = "BUILD-MANIFEST.json"


def is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) build rather than a source checkout."""
    return bool(getattr(sys, "frozen", False))


def app_dir(frozen: bool | None = None, executable: str | None = None) -> Path | None:
    """Folder that holds ``Sentinel.exe`` in a frozen build; ``None`` for a source checkout."""
    if not (is_frozen() if frozen is None else frozen):
        return None
    return Path(executable or sys.executable).resolve().parent


def tool_filename(name: str, platform: str | None = None) -> str:
    return f"{name}.exe" if (platform or sys.platform) == "win32" else name


def bundled_tool(name: str, app: Path | None, platform: str | None = None) -> Path | None:
    """``<app>/ffmpeg/<name>[.exe]`` if it exists as a file, else ``None``."""
    if app is None:
        return None
    candidate = app / TOOLS_SUBDIR / tool_filename(name, platform)
    return candidate if candidate.is_file() else None


_DETECT = object()


def default_tool(name: str, app: Path | None | object = _DETECT, platform: str | None = None) -> str:
    """Value used for ``[tools] <name>`` when the configuration does not set it.

    Bundled copy next to the executable first; otherwise the bare name (PATH lookup)."""
    folder = app_dir() if app is _DETECT else app
    found = bundled_tool(name, folder if isinstance(folder, Path) else None, platform)
    return str(found) if found is not None else name


@dataclass(frozen=True)
class Layout:
    """Per-machine (or, on Linux without root, per-user) default locations."""
    scope: str                      # "machine" | "user"
    config_file: Path
    credentials_file: Path
    spool_dir: Path
    data_dir: Path
    log_dir: Path

    def storage_defaults(self) -> dict[str, str]:
        return {"spool_dir": str(self.spool_dir), "data_dir": str(self.data_dir), "log_dir": str(self.log_dir)}


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def default_layout(platform: str | None = None, env: Mapping[str, str] | None = None,
                   is_root: bool | None = None, home: str | None = None) -> Layout:
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform == "win32":
        root = PureWindowsPath(env.get("ProgramData") or env.get("PROGRAMDATA") or r"C:\ProgramData") / "Sentinel"
        return Layout("machine", Path(root / CONFIG_NAME), Path(root / SECRET_NAME), Path(root / "spool"),
                      Path(root / "data"), Path(root / "logs"))
    if _is_root() if is_root is None else is_root:
        etc, var = PurePosixPath("/etc/sentinel"), PurePosixPath("/var/lib/sentinel")
        return Layout("machine", Path(etc / CONFIG_NAME), Path(etc / SECRET_NAME), Path(var / "spool"),
                      Path(var / "data"), Path(PurePosixPath("/var/log/sentinel")))
    user_home = PurePosixPath(home or env.get("HOME") or os.path.expanduser("~"))
    cfg = PurePosixPath(env.get("XDG_CONFIG_HOME") or user_home / ".config") / "sentinel"
    data = PurePosixPath(env.get("XDG_DATA_HOME") or user_home / ".local" / "share") / "sentinel"
    return Layout("user", Path(cfg / CONFIG_NAME), Path(cfg / SECRET_NAME), Path(data / "spool"),
                  Path(data / "data"), Path(data / "logs"))


def default_storage_dirs() -> dict[str, str]:
    """``[storage]`` directory defaults for this machine (used when the keys are omitted)."""
    return default_layout().storage_defaults()


def config_candidates(explicit: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None,
                      app: Path | None | object = _DETECT, platform: str | None = None,
                      is_root: bool | None = None, home: str | None = None) -> list[Path]:
    """Configuration files to consider, highest priority first."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    out: list[Path] = []
    if explicit:
        return [Path(explicit)]
    if env.get(CONFIG_ENV):
        return [Path(env[CONFIG_ENV])]
    folder = app_dir() if app is _DETECT else app
    if isinstance(folder, Path):
        out.append(folder / CONFIG_NAME)
    if platform == "win32":
        out.append(default_layout(platform, env).config_file)
    else:
        out.append(default_layout(platform, env, is_root=True).config_file)
        if not (_is_root() if is_root is None else is_root):
            out.append(default_layout(platform, env, is_root=False, home=home).config_file)
    return out


def find_config(explicit: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None,
                app: Path | None | object = _DETECT, platform: str | None = None,
                is_root: bool | None = None, home: str | None = None) -> Path | None:
    """First existing configuration file, or ``None`` (first run).

    An explicit ``--config`` or ``$SENTINEL_CONFIG`` is returned even if missing, so the caller
    reports "not found" for the file the user named instead of silently using another one."""
    cands = config_candidates(explicit, env, app, platform, is_root, home)
    if explicit or (env if env is not None else os.environ).get(CONFIG_ENV):
        return cands[0]
    for c in cands:
        if c.is_file():
            return c
    return None
