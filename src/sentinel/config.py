"""Configuration schema and validation.

Configuration is TOML (stdlib ``tomllib``). Unknown keys are rejected so typos cannot
silently fall back to defaults. Credentials are never stored here; see ``credentials.py``.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised for any invalid configuration. Message is safe to show to an administrator."""


_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
PRODUCTION_SCHEMES = ("rtsp", "rtsps")
# Development/test only (simulated camera, recorded files). Never accepted unless the config
# explicitly says development_source = true, and then every status report says SIMULATED.
DEVELOPMENT_SCHEMES = ("tcp", "file")
ALLOWED_INPUT_FORMATS = (None, "mpegts")
# Credentials must come from credentials_file; reject OEM-style credentials in path/query.
_CRED_KEYS = re.compile(r"(?i)(?:^|[/?&;])(user|username|usr|password|passwd|pass|pwd|token|auth|key)=")


@dataclass(frozen=True)
class CameraConfig:
    id: str
    url: str                                  # no credentials; e.g. rtsp://192.0.2.10:554/<path>
    name: str = ""
    transport: str = "tcp"                    # rtsp transport: tcp | udp
    credentials_file: str | None = None
    include_audio: bool = False               # collection-scope decision; default excluded
    input_format: str | None = None           # "mpegts" for the simulator only; None = auto
    development_source: bool = False          # allow tcp:// / file:// test sources (never in production)


@dataclass(frozen=True)
class RecordingConfig:
    segment_seconds: float = 300.0
    align_to_clock: bool = True
    connect_timeout_s: float = 20.0
    stall_timeout_s: float = 15.0
    io_timeout_s: float = 10.0
    stop_grace_s: float = 8.0
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 30.0
    backoff_reset_after_s: float = 60.0
    validation: str = "packets"               # packets | decode
    content_hash: str = "deferred"            # deferred | none  (decoded-frame hash, low-priority worker)
    frame_hashes: bool = True                 # write per-frame SHA-256 sidecar alongside each segment
    max_internal_gap_s: float = 2.0
    timeline_drift_tolerance_s: float = 3.0   # stream time behind real time by more than this => gap
    timeline_drift_persist_s: float = 5.0     # ...for at least this long (filters CPU hiccups)
    escalate_after_s: float = 180.0           # outage longer than this => "automatic recovery unsuccessful"
    clock_step_tolerance_s: float = 2.0
    display_timezone: str = "UTC"


@dataclass(frozen=True)
class StorageConfig:
    spool_dir: str
    data_dir: str
    log_dir: str
    warning_free_gb: float = 50.0
    critical_free_gb: float = 5.0


@dataclass(frozen=True)
class ToolsConfig:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


@dataclass(frozen=True)
class ServiceConfig:
    status_interval_s: float = 2.0
    poll_interval_s: float = 0.5
    log_level: str = "INFO"
    audit_interval_s: float = 600.0           # rolling re-verification of sealed segments
    janitor_interval_s: float = 60.0          # retry stuck segments, checkpoint ledger, tidy run dirs
    diagnostics_keep: int = 20


@dataclass(frozen=True)
class AppConfig:
    camera: CameraConfig
    recording: RecordingConfig
    storage: StorageConfig
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    source_path: str | None = None

    @property
    def spool_path(self) -> Path:
        return Path(self.storage.spool_dir)

    @property
    def data_path(self) -> Path:
        return Path(self.storage.data_dir)

    @property
    def log_path(self) -> Path:
        return Path(self.storage.log_dir)


def _build(cls: type, section: str, raw: dict[str, Any] | None, base_dir: Path) -> Any:
    raw = dict(raw or {})
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(f"[{section}] unknown key(s): {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, f in known.items():
        if name not in raw:
            continue
        value = raw[name]
        expected = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", str(f.type))
        if "float" in expected and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if expected.startswith("float") and not isinstance(value, float):
            raise ConfigError(f"[{section}] {name} must be a number")
        if expected.startswith("bool") and not isinstance(value, bool):
            raise ConfigError(f"[{section}] {name} must be true or false")
        if expected.startswith("int") and (not isinstance(value, int) or isinstance(value, bool)):
            raise ConfigError(f"[{section}] {name} must be a whole number")
        if expected.startswith("str") and not isinstance(value, str):
            raise ConfigError(f"[{section}] {name} must be text")
        kwargs[name] = value
    if cls is StorageConfig:
        for key in ("spool_dir", "data_dir", "log_dir"):
            if key not in kwargs:
                raise ConfigError(f"[storage] {key} is required")
            p = Path(kwargs[key])
            kwargs[key] = str(p if p.is_absolute() else (base_dir / p).resolve())
    try:
        return cls(**kwargs)
    except TypeError as exc:  # missing required field
        raise ConfigError(f"[{section}] {exc}") from None


def _validate(cfg: AppConfig) -> None:
    cam, rec, sto = cfg.camera, cfg.recording, cfg.storage
    if not _CAMERA_ID_RE.fullmatch(cam.id):
        raise ConfigError("[camera] id must be 1-32 characters: letters, digits, '-' or '_'")
    parts = urlsplit(cam.url)
    allowed = PRODUCTION_SCHEMES + (DEVELOPMENT_SCHEMES if cam.development_source else ())
    if parts.scheme not in allowed:
        hint = "" if cam.development_source else " (test sources need development_source = true)"
        raise ConfigError(f"[camera] url scheme must be one of {', '.join(allowed)}{hint}")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ConfigError("[camera] url must not contain credentials; use credentials_file")
    if _CRED_KEYS.search(parts.path) or _CRED_KEYS.search(parts.query):
        raise ConfigError("[camera] url must not contain credentials in its path or query; use credentials_file")
    if "listen" in parts.query.lower():
        raise ConfigError("[camera] url must not put the recorder in listening (server) mode")
    if parts.scheme != "file" and not parts.hostname:
        raise ConfigError("[camera] url must include the camera host")
    if cam.input_format not in ALLOWED_INPUT_FORMATS:
        raise ConfigError("[camera] input_format must be omitted (or \"mpegts\" for the simulator)")
    if cam.input_format and not cam.development_source:
        raise ConfigError("[camera] input_format is only allowed with development_source = true")
    if cam.transport not in ("tcp", "udp"):
        raise ConfigError("[camera] transport must be 'tcp' or 'udp'")
    if not 2.0 <= rec.segment_seconds <= 3600.0:
        raise ConfigError("[recording] segment_seconds must be between 2 and 3600")
    for name in ("connect_timeout_s", "stall_timeout_s", "io_timeout_s", "stop_grace_s", "backoff_initial_s",
                 "backoff_max_s", "backoff_reset_after_s", "max_internal_gap_s", "clock_step_tolerance_s",
                 "timeline_drift_tolerance_s", "timeline_drift_persist_s", "escalate_after_s"):
        if getattr(rec, name) <= 0:
            raise ConfigError(f"[recording] {name} must be positive")
    if rec.backoff_max_s < rec.backoff_initial_s:
        raise ConfigError("[recording] backoff_max_s must be >= backoff_initial_s")
    if rec.validation not in ("packets", "decode"):
        raise ConfigError("[recording] validation must be 'packets' or 'decode'")
    if rec.content_hash not in ("deferred", "none"):
        raise ConfigError("[recording] content_hash must be 'deferred' or 'none'")
    if rec.frame_hashes and rec.content_hash == "none":
        raise ConfigError("[recording] frame_hashes requires content_hash = 'deferred'")
    try:
        ZoneInfo(rec.display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"[recording] unknown display_timezone {rec.display_timezone!r}") from None
    if sto.critical_free_gb < 0 or sto.warning_free_gb < sto.critical_free_gb:
        raise ConfigError("[storage] require 0 <= critical_free_gb <= warning_free_gb")
    if any(v <= 0 for v in (cfg.service.poll_interval_s, cfg.service.status_interval_s,
                            cfg.service.audit_interval_s, cfg.service.janitor_interval_s)):
        raise ConfigError("[service] intervals must be positive")
    if cfg.service.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("[service] log_level must be DEBUG, INFO, WARNING or ERROR")


def load_config_dict(raw: dict[str, Any], base_dir: Path | None = None, source: str | None = None) -> AppConfig:
    base_dir = base_dir or Path.cwd()
    unknown = sorted(set(raw) - {"camera", "recording", "storage", "tools", "service"})
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")
    if "camera" not in raw or "storage" not in raw:
        raise ConfigError("[camera] and [storage] sections are required")
    cfg = AppConfig(
        camera=_build(CameraConfig, "camera", raw.get("camera"), base_dir),
        recording=_build(RecordingConfig, "recording", raw.get("recording"), base_dir),
        storage=_build(StorageConfig, "storage", raw.get("storage"), base_dir),
        tools=_build(ToolsConfig, "tools", raw.get("tools"), base_dir),
        service=_build(ServiceConfig, "service", raw.get("service"), base_dir),
        source_path=source,
    )
    _validate(cfg)
    return cfg


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration file is not valid TOML: {exc}") from None
    return load_config_dict(raw, base_dir=path.parent.resolve(), source=str(path))


def check_tools(cfg: AppConfig) -> list[str]:
    """Return a list of human-readable problems with external tools (empty = OK)."""
    problems = []
    for label, exe in (("FFmpeg", cfg.tools.ffmpeg), ("FFprobe", cfg.tools.ffprobe)):
        if shutil.which(exe) is None:
            problems.append(f"{label} not found ({exe})")
    return problems
