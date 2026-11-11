"""Self-check and "last known good" settings (owner request 2026-11-10).

``run_checks`` looks at the whole chain as the recorder reports it: the settings file loads, the
recorder is running and answering, it is in a healthy state, the camera sends video, recording files
are being saved, and there is no critical alert. It only reads (status.json, the settings file); it
never contacts the camera or changes anything.

When the check passes, the settings are copied to ``<config>.good`` (the last known good settings).
After a settings change the window waits for a pass; if none comes, it restores ``<config>.good``.
Every restore first keeps the current file as a dated ``.bak``, so a rollback can be undone too.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .clock import iso_utc, parse_iso_utc

GOOD_SUFFIX = ".good"
GOOD_INFO_SUFFIX = ".good.json"
# Healthy overall states: recording, or not recording on purpose (schedule, motion, no camera yet).
HEALTHY = ("RECORDING", "STORAGE_WARNING", "PAUSED", "WATCHING", "CAMERA_NOT_SET_UP")
RECORDING_STATES = ("RECORDING", "STORAGE_WARNING")


@dataclass
class Check:
    name: str
    ok: bool | None           # None: not applicable now (e.g. no files are due while paused)
    detail: str = ""
    warning: bool = False     # ok, but worth knowing (e.g. clock not synchronised)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    checked_utc: str = ""

    @property
    def ok(self) -> bool:
        return all(c.ok is not False for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False]

    def summary(self) -> str:
        if self.ok:
            return "Everything works." + (" Notes: " + "; ".join(f"{c.name}: {c.detail}" for c in self.checks
                                                                  if c.warning) if any(c.warning for c in self.checks)
                                          else "")
        return "Not working: " + "; ".join(f"{c.name}: {c.detail}" for c in self.failures)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checked_utc": self.checked_utc, "checks": [asdict(c) for c in self.checks]}


def run_checks(cfg_path: Path, read_status: Callable[[], tuple[dict[str, Any] | None, str | None]],
               now: float | None = None, require_saved_file: bool = True) -> Report:
    """``read_status`` returns (status document, problem) like ``sentinel.status.read_status``.
    ``require_saved_file``: also require a recording file saved recently (off right after a
    restart, when the first file of the new run is not due yet)."""
    from .config import ConfigError, load_config

    now = time.time() if now is None else now
    rep = Report(checked_utc=iso_utc(now))
    try:
        cfg = load_config(cfg_path)
        rep.checks.append(Check("Settings file", True, "valid"))
    except (ConfigError, OSError) as exc:
        rep.checks.append(Check("Settings file", False, str(exc)))
        return rep
    doc, problem = read_status()
    if doc is None:
        rep.checks.append(Check("Recorder running", False, problem or "no status from the recorder"))
        return rep
    rep.checks.append(Check("Recorder running", True, "answering"))
    overall = str(doc.get("overall"))
    top = (doc.get("alert") or {}).get("message") or overall
    rep.checks.append(Check("Recorder state", overall in HEALTHY, overall.replace("_", " ").lower()
                            if overall in HEALTHY else top))
    recording = overall in RECORDING_STATES
    if cfg.camera.pending:
        rep.checks.append(Check("Camera", None, "not added yet"))
    elif recording:
        link = str(doc.get("camera_link") or "")
        rep.checks.append(Check("Camera sends video", link == "connected", link.replace("_", " ") or "unknown"))
    else:
        rep.checks.append(Check("Camera sends video", None, "not recording now (planned)" if overall in HEALTHY
                                else "not recording"))
    if recording and require_saved_file:
        last = (doc.get("last_segment") or {}).get("end_utc")
        due = cfg.recording.segment_seconds * 1.5 + 120
        # The first file of a recording run is finished only at the end of its period (up to an hour):
        # measure from the later of the last saved file and the start of the current run.
        run_start = (doc.get("recording") or {}).get("run_started_utc")
        try:
            started = parse_iso_utc(run_start) if run_start else None
        except ValueError:
            started = None
        if last:
            age = now - max(parse_iso_utc(last), started or 0.0)
            rep.checks.append(Check("Recordings being saved", age <= due,
                                    f"last file saved {int(now - parse_iso_utc(last))} s ago" if age <= due else
                                    f"no file saved for {int(age // 60)} min"))
        elif started is not None and now - started <= due:
            rep.checks.append(Check("Recordings being saved", True, "the first file of this recording is still open"))
        else:
            rep.checks.append(Check("Recordings being saved", False, "no recording file saved yet"))
    critical = [a for a in doc.get("alerts") or [] if isinstance(a, dict) and a.get("severity") == 3]
    rep.checks.append(Check("No critical alerts", not critical,
                            "none" if not critical else "; ".join(str(a.get("code")) for a in critical)))
    warns = [a for a in doc.get("alerts") or [] if isinstance(a, dict) and a.get("severity") == 2]
    if warns:
        rep.checks.append(Check("Warnings", True, ", ".join(str(a.get("code")) for a in warns), warning=True))
    return rep


# ---------------------------------------------------------------------- known good and history
def good_path(cfg_path: Path) -> Path:
    return cfg_path.with_name(cfg_path.name + GOOD_SUFFIX)


def _secret_of(cfg_path: Path) -> Path | None:
    from .config import load_config

    try:
        cred = load_config(cfg_path).camera.credentials_file
    except Exception:  # noqa: BLE001 - no usable settings: no secret to keep
        return None
    return Path(cred) if cred else None


def _copy_atomic(src: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".partial")
    shutil.copy2(src, tmp)                    # the copy takes the private folder's permissions
    os.replace(tmp, dest)


def mark_known_good(cfg_path: Path, report: Report) -> Path:
    """Remember the current settings, and the camera account file, as working (passing report only)."""
    assert report.ok
    dest = good_path(cfg_path)
    _copy_atomic(cfg_path, dest)
    secret = _secret_of(cfg_path)
    if secret is not None and secret.is_file():
        _copy_atomic(secret, secret.with_name(secret.name + GOOD_SUFFIX))
    cfg_path.with_name(cfg_path.name + GOOD_INFO_SUFFIX).write_text(json.dumps(report.to_dict(), indent=1),
                                                                    encoding="utf-8")
    return dest


def known_good_info(cfg_path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(cfg_path.with_name(cfg_path.name + GOOD_INFO_SUFFIX).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def same_settings(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def history(cfg_path: Path) -> list[Path]:
    """Earlier versions (``<config>.<UTC time>.bak``), newest first."""
    return sorted(cfg_path.parent.glob(cfg_path.name + ".*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)


def restore_known_good(cfg_path: Path, clock: Callable[[], float] = time.time) -> Path:
    """Put the last known good settings back, with the camera account file that went with them."""
    backup = restore(cfg_path, good_path(cfg_path), clock)
    secret = _secret_of(cfg_path)
    if secret is not None:
        good_secret = secret.with_name(secret.name + GOOD_SUFFIX)
        if good_secret.is_file() and not same_settings(good_secret, secret):
            _copy_atomic(good_secret, secret)
    return backup


def restore(cfg_path: Path, source: Path, clock: Callable[[], float] = time.time) -> Path:
    """Put ``source`` back as the settings. It must load as a valid configuration; the current file
    is kept as a dated .bak first (so this can be undone). Returns that backup."""
    from .config import ConfigError, load_config_dict

    import tomllib

    text = source.read_text(encoding="utf-8")
    try:
        load_config_dict(tomllib.loads(text), base_dir=cfg_path.parent, source=str(cfg_path))
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{source.name} is not a valid configuration: {exc}") from None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    backup = cfg_path.with_name(f"{cfg_path.name}.{stamp}.bak")
    n = 1
    while backup.exists():
        backup = cfg_path.with_name(f"{cfg_path.name}.{stamp}-{n}.bak")
        n += 1
    shutil.copy2(cfg_path, backup)
    tmp = cfg_path.with_name(cfg_path.name + ".partial")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, cfg_path)
    return backup
