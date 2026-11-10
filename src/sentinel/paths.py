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
  a *frozen* build always uses ``<app>/ffmpeg/<tool>[.exe]`` — if that file is missing the
  recorder refuses to start (C-001); it never falls back to PATH. A source checkout searches the
  absolute directories on PATH itself (never the current directory). Every tool path handed to
  the recorder is absolute.
* **Configuration file**, first that exists: ``--config``; the per-machine file;
  ``<app>/sentinel.toml`` (frozen builds only, a technician-preconfigured "portable" folder);
  ``$SENTINEL_CONFIG``; on Linux without root, the per-user file. Whatever is found must also pass
  :mod:`sentinel.install_check` (not changeable by other accounts) before the recorder starts.
* **Data defaults** (used when ``[storage]`` keys are omitted, and offered by first-run setup):

  ============  ==========================================  =====================================
  Platform      Configuration / credentials                 spool, data, logs
  ============  ==========================================  =====================================
  Windows       ``%ProgramData%\\Sentinel\\config\\*``       ``%ProgramData%\\Sentinel\\spool|data|logs|status``
  Linux, root   ``/etc/sentinel/sentinel.toml``             ``/var/lib/sentinel/spool|data``, ``/var/log/sentinel``
  Linux, user   ``$XDG_CONFIG_HOME/sentinel/sentinel.toml``  ``$XDG_DATA_HOME/sentinel/spool|data|logs``
  ============  ==========================================  =====================================

Integration (docs/INTEGRATION.md): the Windows layout is the one the Windows service installs and
re-permissions: ``sentinel.toml`` and ``camera.secret`` (DPAPI content on Windows, detected by
content) in their own ``config`` folder, separate from spool/data/logs, plus an operator-readable
``status`` folder (``[storage] status_dir``). No release used the earlier
``%ProgramData%\\Sentinel\\sentinel.toml`` location, so it is not searched.

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
CONFIG_SUBDIR = "config"          # Windows: configuration + credentials in their own folder
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


def resolve_on_path(name: str, path_env: str | None = None, platform: str | None = None) -> str | None:
    """Absolute path of ``name`` found in the absolute directories of PATH, or ``None``.

    Unlike a shell (and ``shutil.which`` on Windows), the current directory and relative PATH
    entries are never searched, so a planted ``ffmpeg.exe`` in the working folder is not run."""
    platform = platform or sys.platform
    filename = tool_filename(name, platform) if not Path(name).suffix else name
    raw = os.environ.get("PATH", "") if path_env is None else path_env
    sep = ";" if platform == "win32" else ":"
    for entry in raw.split(sep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        candidate = Path(entry) / filename
        if candidate.is_file() and (platform == "win32" or os.access(candidate, os.X_OK)):
            return str(candidate)
    return None


def default_tool(name: str, app: Path | None | object = _DETECT, platform: str | None = None) -> str:
    """Value used for ``[tools] <name>`` when the configuration does not set it.

    Frozen build: always the bundled location (absolute; existence is checked at start, there
    is no PATH fallback). Source checkout: the absolute PATH match, else the bare name (which
    the start-up tool check then reports as not found)."""
    folder = app_dir() if app is _DETECT else app
    if isinstance(folder, Path):
        return str(folder / TOOLS_SUBDIR / tool_filename(name, platform))
    return resolve_on_path(name, platform=platform) or name


def absolute_tool(value: str, base_dir: Path, platform: str | None = None) -> str:
    """Make a configured tool value absolute: relative paths against the configuration folder,
    bare names through :func:`resolve_on_path`. Unresolvable bare names are returned unchanged."""
    p = Path(value)
    if p.is_absolute():
        return value
    if len(p.parts) > 1:
        return str((base_dir / p).resolve())
    return resolve_on_path(value, platform=platform) or value


def safe_cwd(platform: str | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Working directory for the recorder and its children: a folder standard users cannot write
    (Windows ``%SystemRoot%\\System32``, as for services; ``/`` elsewhere)."""
    env = os.environ if env is None else env
    if (platform or sys.platform) == "win32":
        return Path(env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows") / "System32"
    return Path("/")


@dataclass(frozen=True)
class Layout:
    """Per-machine (or, on Linux without root, per-user) default locations."""
    scope: str                      # "machine" | "user"
    config_file: Path
    credentials_file: Path
    spool_dir: Path
    data_dir: Path
    log_dir: Path
    status_dir: Path | None = None  # operator-readable status folder (Windows service layout)

    def storage_defaults(self) -> dict[str, str]:
        out = {"spool_dir": str(self.spool_dir), "data_dir": str(self.data_dir), "log_dir": str(self.log_dir)}
        if self.status_dir is not None:
            out["status_dir"] = str(self.status_dir)
        return out


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def default_layout(platform: str | None = None, env: Mapping[str, str] | None = None,
                   is_root: bool | None = None, home: str | None = None) -> Layout:
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform == "win32":
        root = PureWindowsPath(env.get("ProgramData") or env.get("PROGRAMDATA") or r"C:\ProgramData") / "Sentinel"
        return Layout("machine", Path(root / CONFIG_SUBDIR / CONFIG_NAME), Path(root / CONFIG_SUBDIR / SECRET_NAME),
                      Path(root / "spool"), Path(root / "data"), Path(root / "logs"), Path(root / "status"))
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
    """``[storage]`` directory defaults for this machine (used when the keys are omitted).

    ``status_dir`` is deliberately not defaulted here: a configuration that names its own
    data_dir but no status_dir must keep its status beside its data (status_dir = data_dir),
    never in the machine's status folder. First-run setup writes status_dir explicitly."""
    return {k: v for k, v in default_layout().storage_defaults().items() if k != "status_dir"}


def config_candidates(explicit: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None,
                      app: Path | None | object = _DETECT, platform: str | None = None,
                      is_root: bool | None = None, home: str | None = None) -> list[Path]:
    """Configuration files to consider, highest priority first."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if explicit:
        return [Path(explicit)]
    # The machine configuration comes first: neither an environment variable nor a file a user
    # can create may take precedence over what the administrator installed.
    if platform == "win32":
        out = [default_layout(platform, env).config_file]
    else:
        out = [default_layout(platform, env, is_root=True).config_file]
    folder = app_dir() if app is _DETECT else app
    if isinstance(folder, Path):
        out.append(folder / CONFIG_NAME)
    if env.get(CONFIG_ENV):
        out.append(Path(env[CONFIG_ENV]))
    if platform != "win32" and not (_is_root() if is_root is None else is_root):
        out.append(default_layout(platform, env, is_root=False, home=home).config_file)
    return out


def find_config(explicit: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None,
                app: Path | None | object = _DETECT, platform: str | None = None,
                is_root: bool | None = None, home: str | None = None) -> Path | None:
    """First existing configuration file, or ``None`` (first run).

    An explicit ``--config`` is returned even if missing, so the caller reports "not found" for
    the file the user named instead of silently using another one."""
    cands = config_candidates(explicit, env, app, platform, is_root, home)
    if explicit:
        return cands[0]
    for c in cands:
        if c.is_file():
            return c
    return None
