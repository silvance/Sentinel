"""Configuration schema and validation.

Configuration is TOML (stdlib ``tomllib``). Unknown keys are rejected so typos cannot
silently fall back to defaults. Credentials are never stored here; see ``credentials.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .drives.policy import DrivePolicy, DrivePolicyError, load_policy
from .paths import TOOL_NAMES, absolute_tool, default_storage_dirs, default_tool
from .motion import MotionConfig, MotionConfigError
from .motion import validate as validate_motion
from .schedule import ScheduleConfig, ScheduleError, parse_schedule


LEDGER_SNAPSHOT_NAME = "ledger-snapshot.db"   # operator-readable ledger copy in [storage] status_dir


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
    # Set up in advance without the camera ("setup --camera-later"): url is empty and nothing is
    # recorded (status P-002) until "set-camera" fills in the camera and clears this.
    pending: bool = False


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
    # Segment-time drift correction (docs/03 §4.2): the correction toward measured reception time
    # may change at most this fast. Real camera/PC clocks drift < ~100 ppm; faster changes are
    # not drift and are not followed (the segment is flagged instead).
    max_clock_drift_ppm: float = 1000.0
    display_timezone: str = "UTC"


@dataclass(frozen=True)
class StorageConfig:
    spool_dir: str
    data_dir: str
    log_dir: str
    warning_free_gb: float = 50.0
    critical_free_gb: float = 5.0
    # Where operator-readable files go: status.json and a periodic read-only ledger snapshot.
    # Default: data_dir (unchanged behaviour). On Windows the installer gives this folder a
    # read-only entry for the "Sentinel Operators" group; data_dir itself stays service-only.
    status_dir: str | None = None


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
    chain_anchor_interval_s: float = 900.0    # copy the recording-log chain head to chain_heads.log
    chain_verify_interval_s: float = 3600.0   # full recording-log chain verification (integrity worker)
    diagnostics_keep: int = 20
    clock_observation_interval_s: float = 900.0  # E1: OS time-sync + clock offset record (docs/03 §4.4)
    # E1 measured offset: SNTP query (read-only) to the APPROVED time source, "host" or "host:port".
    # Empty = not measured (no Internet default). Must be the local/approved NTP server (docs/07).
    time_reference_server: str = ""
    # A Windows Time sync older than this counts as not synchronised (0 = 2 x the reported poll interval).
    max_sync_age_s: float = 0.0
    # Internal watchdog (all platforms): if the recording loop has not run for this long, the
    # process exits with EXIT_WATCHDOG so the service manager restarts it. A normal run end
    # (FFmpeg stop + wait + bookkeeping) can legitimately pause the loop for ~50 s.
    watchdog_timeout_s: float = 120.0


@dataclass(frozen=True)
class ExportConfig:
    """Review-drive export (sentinel.export). Byte verification of every file is always done;
    ``content_check`` only controls the additional decoded-picture comparison of MP4 videos:
    full | sampled | deferred (see docs/branches/req-review-drive-export.md). Default
    "sampled": owner decision, confirmed at integration (docs/INTEGRATION.md)."""
    content_check: str = "sampled"
    sample_edge_frames: int = 30              # sampled: frames checked at the start and at the end
    sample_spans: int = 3                     # sampled: additional spans at pseudo-random positions
    sample_span_frames: int = 30              # sampled: frames per additional span


@dataclass(frozen=True)
class ArchiveConfig:
    """Continuous copy of sealed recordings to an attached archive drive (S1, docs/04 §2-3).
    Runs as its own process (``python -m sentinel.archive run``); see sentinel/archive/."""
    enabled: bool = False
    drive_path: str = ""                      # optional extra folder to scan; drives are found by marker on any volume
    require_drive_marker: bool = True         # SENTINEL_DRIVE.json must carry drive_uuid AND "role": "archive"
    scan_volumes: bool = True                 # look for archive drives on every mounted volume (false: drive_path only)
    resume_policy: str = "any_drive"          # owner decision 2026-11-10 (integration); this_drive | any_drive
    chunk_mib: int = 8                        # copy chunk size
    fsync_every_mib: int = 64                 # flush to the drive at least this often while copying
    max_rate_mb_s: float = 40.0               # throttle (MB/s, 10^6 bytes) for reading local recordings
    reserve_free_mb: float = 1024.0           # never fill the drive beyond this (room for the index)
    index_every_s: float = 3600.0             # rewrite the drive index after this much footage (and on swap)
    backlog_alert_hours: float = 24.0         # "not yet archived" beyond this => critical alert A-004
    poll_interval_s: float = 5.0              # how often to look for a drive / new recordings when idle
    swap_max_wait_s: float = 120.0            # on swap, finish the file in flight if it takes no longer than this
    verify_attempts: int = 3                  # read-back failures per file before alerting and skipping it
    not_running_critical_after_s: float = 1800.0  # archive process down this long => A-007 becomes critical
    capacity_warning_days: float = 5.0        # A-009 when the attached drive holds less than this at the measured rate


@dataclass(frozen=True)
class RetentionConfig:
    """Admin-enabled deletion of ARCHIVED local originals when internal storage runs low (sentinel.retention).
    OFF by default: the project owner must decide to enable it (docs/branches/design-local-retention.md)."""
    enabled: bool = False
    floor_free_gb: float = -1.0               # act when free space drops below this; < 0 = storage.warning_free_gb
    margin_gb: float = 10.0                   # ...and free only up to floor + margin, never more
    min_verified_copies: int = 1              # VERIFIED archive copies (not LOST) required before deleting
    require_removed_drive: bool = True        # count only copies on drives that have left the machine
    keep_recent_hours: float = 24.0           # never delete footage sealed less than this long ago
    max_deletions_per_pass: int = 500
    expected_swap_days: float = 5.0           # horizon for the "storage will run out before the next swap" alert


@dataclass(frozen=True)
class AppConfig:
    camera: CameraConfig
    recording: RecordingConfig
    storage: StorageConfig
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    source_path: str | None = None
    drives: DrivePolicy = field(default_factory=DrivePolicy)   # [drives]; validated by sentinel.drives.policy
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)   # [schedule] (sentinel.schedule); off by default
    motion: MotionConfig = field(default_factory=MotionConfig)         # [motion]: used by schedule action "motion"

    @property
    def spool_path(self) -> Path:
        return Path(self.storage.spool_dir)

    @property
    def data_path(self) -> Path:
        return Path(self.storage.data_dir)

    @property
    def log_path(self) -> Path:
        return Path(self.storage.log_dir)

    @property
    def status_dir_path(self) -> Path:
        return Path(self.storage.status_dir) if self.storage.status_dir else self.data_path

    @property
    def status_path(self) -> Path:
        return self.status_dir_path / "status.json"

    @property
    def ledger_snapshot_path(self) -> Path | None:
        """Read-only ledger copy for operators; only when a separate status_dir is configured."""
        return self.status_dir_path / LEDGER_SNAPSHOT_NAME if self.storage.status_dir else None


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
        for key in ("spool_dir", "data_dir", "log_dir", "status_dir"):
            if key not in kwargs:
                if key == "status_dir":
                    continue
                raise ConfigError(f"[storage] {key} is required")
            p = Path(kwargs[key])
            kwargs[key] = str(p if p.is_absolute() else (base_dir / p).resolve())
    if cls is ArchiveConfig and kwargs.get("drive_path"):
        p = Path(kwargs["drive_path"])
        kwargs["drive_path"] = str(p if p.is_absolute() else (base_dir / p).resolve())
    try:
        return cls(**kwargs)
    except TypeError as exc:  # missing required field
        raise ConfigError(f"[{section}] {exc}") from None


def _validate(cfg: AppConfig) -> None:
    cam = cfg.camera
    if not _CAMERA_ID_RE.fullmatch(cam.id):
        raise ConfigError("[camera] id must be 1-32 characters: letters, digits, '-' or '_'")
    if cam.pending:
        if cam.url or cam.credentials_file or cam.development_source or cam.input_format:
            raise ConfigError("[camera] pending = true (camera not added yet) requires url = \"\" and no "
                              "credentials_file; add the camera with 'Sentinel set-camera'")
    else:
        _validate_camera_url(cam)
    _validate_rest(cfg)


def _validate_camera_url(cam: CameraConfig) -> None:
    if not cam.url:
        raise ConfigError("[camera] url is required (or pending = true until the camera is added)")
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
    try:
        port = parts.port   # review 2026-11-10: 'rtsp://host:99999/x' was accepted (endless R- alerts)
    except ValueError:
        port = -1
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("[camera] url port must be a number from 1 to 65535")
    if cam.input_format not in ALLOWED_INPUT_FORMATS:
        raise ConfigError("[camera] input_format must be omitted (or \"mpegts\" for the simulator)")
    if cam.input_format and not cam.development_source:
        raise ConfigError("[camera] input_format is only allowed with development_source = true")


def _validate_rest(cfg: AppConfig) -> None:
    cam, rec, sto = cfg.camera, cfg.recording, cfg.storage
    if cam.transport not in ("tcp", "udp"):
        raise ConfigError("[camera] transport must be 'tcp' or 'udp'")
    if not 2.0 <= rec.segment_seconds <= 3600.0:
        raise ConfigError("[recording] segment_seconds must be between 2 and 3600")
    for name in ("connect_timeout_s", "stall_timeout_s", "io_timeout_s", "stop_grace_s", "backoff_initial_s",
                 "backoff_max_s", "backoff_reset_after_s", "max_internal_gap_s", "clock_step_tolerance_s",
                 "timeline_drift_tolerance_s", "timeline_drift_persist_s", "escalate_after_s",
                 "max_clock_drift_ppm"):
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
    if sto.status_dir:
        _validate_status_dir(sto)
    if any(v <= 0 for v in (cfg.service.poll_interval_s, cfg.service.status_interval_s,
                            cfg.service.audit_interval_s, cfg.service.janitor_interval_s,
                            cfg.service.chain_anchor_interval_s, cfg.service.chain_verify_interval_s,
                            cfg.service.clock_observation_interval_s)):
        raise ConfigError("[service] intervals must be positive")
    if cfg.service.watchdog_timeout_s < 5.0:
        raise ConfigError("[service] watchdog_timeout_s must be at least 5 (120 is recommended)")
    if cfg.service.max_sync_age_s < 0:
        raise ConfigError("[service] max_sync_age_s must be 0 (automatic) or positive")
    ref = cfg.service.time_reference_server
    if ref and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}(:[0-9]{1,5})?|\[[0-9A-Fa-f:]+\](:[0-9]{1,5})?", ref):
        raise ConfigError("[service] time_reference_server must be a host name or address, optionally :port")
    if cfg.service.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("[service] log_level must be DEBUG, INFO, WARNING or ERROR")
    ex = cfg.export
    if ex.content_check not in ("full", "sampled", "deferred"):
        raise ConfigError("[export] content_check must be 'full', 'sampled' or 'deferred'")
    if ex.sample_edge_frames < 1 or ex.sample_spans < 0 or ex.sample_span_frames < 1:
        raise ConfigError("[export] sample_edge_frames and sample_span_frames must be >= 1, sample_spans >= 0")
    arc = cfg.archive
    if arc.resume_policy not in ("this_drive", "any_drive"):
        raise ConfigError("[archive] resume_policy must be 'this_drive' or 'any_drive'")
    if not 1 <= arc.chunk_mib <= 64 or arc.fsync_every_mib < arc.chunk_mib:
        raise ConfigError("[archive] require 1 <= chunk_mib <= 64 and fsync_every_mib >= chunk_mib")
    if arc.verify_attempts < 1:
        raise ConfigError("[archive] verify_attempts must be at least 1")
    for name in ("max_rate_mb_s", "index_every_s", "backlog_alert_hours", "poll_interval_s", "swap_max_wait_s",
                 "not_running_critical_after_s"):
        if getattr(arc, name) <= 0:
            raise ConfigError(f"[archive] {name} must be positive")
    if arc.reserve_free_mb < 0:
        raise ConfigError("[archive] reserve_free_mb must not be negative")
    ret = cfg.retention
    if ret.min_verified_copies < 1:
        raise ConfigError("[retention] min_verified_copies must be at least 1")
    if ret.margin_gb < 0 or ret.keep_recent_hours < 0 or ret.expected_swap_days <= 0 or ret.max_deletions_per_pass < 1:
        raise ConfigError("[retention] margin_gb and keep_recent_hours must be >= 0, expected_swap_days > 0, "
                          "max_deletions_per_pass >= 1")
    if ret.enabled and not arc.enabled:
        raise ConfigError("[retention] requires [archive] enabled = true (only archived recordings may be removed)")


def _norm_dir(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def _same_or_inside(child: str, parent: str) -> bool:
    c, par = _norm_dir(child), _norm_dir(parent)
    return c == par or c.startswith(par.rstrip(os.sep) + os.sep)


def _validate_status_dir(sto: StorageConfig) -> None:
    """Review 2026-11-10: the operator-readable status folder must not be, contain or lie inside the
    service-only spool/data/log folders. The Windows installer grants operators read access to it
    and resets the permissions of everything below it, so status_dir = C:\\ProgramData\\Sentinel
    would have exposed every recording and the live ledger."""
    assert sto.status_dir is not None
    for key in ("spool_dir", "data_dir", "log_dir"):
        other = getattr(sto, key)
        if _same_or_inside(sto.status_dir, other) or _same_or_inside(other, sto.status_dir):
            raise ConfigError(f"[storage] status_dir must be its own folder: it may not be, contain or lie "
                              f"inside {key}, because operators may read it (e.g. use a 'status' folder next to "
                              f"the data folder)")


def _table(raw: dict[str, Any], section: str) -> dict[str, Any] | None:
    """``raw[section]`` if it is a TOML table (or absent). Review 2026-11-10: '[[camera]]' or
    'recording = [..]' raised a bare ValueError/TypeError instead of ConfigError (no C-001)."""
    value = raw.get(section)
    if value is None or isinstance(value, dict):
        return value
    kind = "[[" + section + "]] (a list of tables)" if isinstance(value, list) else f"{section} = <value>"
    raise ConfigError(f"[{section}] must be a single table ('[{section}]'), not {kind}")


def _build_drives(raw: Any) -> DrivePolicy:
    if raw is not None and not isinstance(raw, dict):
        raise ConfigError("[drives] must be a table")
    try:
        return load_policy(raw)
    except DrivePolicyError as exc:
        raise ConfigError(str(exc)) from None


def load_config_dict(raw: dict[str, Any], base_dir: Path | None = None, source: str | None = None) -> AppConfig:
    base_dir = base_dir or Path.cwd()
    unknown = sorted(set(raw) - {"camera", "recording", "storage", "tools", "service", "export", "drives",
                                 "archive", "retention", "schedule", "motion"})
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")
    if "camera" not in raw:
        raise ConfigError("[camera] section is required")
    sections = {name: _table(raw, name) for name in ("camera", "recording", "storage", "tools", "service",
                                                      "export", "archive", "retention", "motion")}
    # Omitted keys fall back to the install layout (sentinel/paths.py): bundled FFmpeg next to a
    # frozen executable before PATH, and the per-machine data folders.
    storage_raw = {**default_storage_dirs(), **dict(sections["storage"] or {})}
    tools_raw = {**{t: default_tool(t) for t in TOOL_NAMES}, **dict(sections["tools"] or {})}
    for t in TOOL_NAMES:   # always hand absolute tool paths to the recorder (no cwd/PATH surprises)
        if isinstance(tools_raw.get(t), str):
            tools_raw[t] = absolute_tool(tools_raw[t], base_dir)
    camera_raw = dict(sections["camera"] or {})
    cred = camera_raw.get("credentials_file")
    if isinstance(cred, str) and cred and not Path(cred).is_absolute():
        camera_raw["credentials_file"] = str((base_dir / cred).resolve())
    cfg = AppConfig(
        camera=_build(CameraConfig, "camera", camera_raw, base_dir),
        recording=_build(RecordingConfig, "recording", sections["recording"], base_dir),
        storage=_build(StorageConfig, "storage", storage_raw, base_dir),
        tools=_build(ToolsConfig, "tools", tools_raw, base_dir),
        service=_build(ServiceConfig, "service", sections["service"], base_dir),
        export=_build(ExportConfig, "export", sections["export"], base_dir),
        source_path=source,
        drives=_build_drives(raw.get("drives")),
        archive=_build(ArchiveConfig, "archive", sections["archive"], base_dir),
        retention=_build(RetentionConfig, "retention", sections["retention"], base_dir),
        motion=_build(MotionConfig, "motion", sections["motion"], base_dir),
    )
    _validate(cfg)
    try:
        validate_motion(cfg.motion)
        sched = parse_schedule(raw.get("schedule"), cfg.recording.display_timezone)
    except (ScheduleError, MotionConfigError) as exc:
        raise ConfigError(str(exc)) from None
    return replace(cfg, schedule=sched)


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
