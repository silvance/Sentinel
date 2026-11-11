"""Self-check, last known good settings, history and restore (sentinel.selfcheck)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gui_support import base_measurements, status_doc
from sentinel import selfcheck
from sentinel.clock import iso_utc
from sentinel.config import ConfigError, load_config
from sentinel.launcher import setup
from sentinel.paths import Layout


def _cfg(tmp: Path) -> Path:
    r = tmp / "m"
    lay = Layout("machine", r / "config" / "sentinel.toml", r / "config" / "camera.secret", r / "spool", r / "data",
                 r / "logs")
    p, _ = setup.write_setup(setup.SetupAnswers("192.0.2.5", "s", username="viewer", password="pw1"), lay)
    return p


def _reader(doc):  # noqa: ANN001, ANN202
    return lambda: (doc, None if doc else "not running")


def test_healthy_recorder_passes(tmp_path: Path) -> None:
    now = time.time()
    rep = selfcheck.run_checks(_cfg(tmp_path), _reader(status_doc(base_measurements(), now, last_segment_end=now - 30)),
                               now)
    assert rep.ok, rep.summary()
    assert [c.name for c in rep.checks][:3] == ["Settings file", "Recorder running", "Recorder state"]


@pytest.mark.parametrize("problem", ["not_running", "error", "no_files", "critical"])
def test_each_failure_is_named(tmp_path: Path, problem: str) -> None:
    now = time.time()
    doc = {"not_running": None,
           "error": status_doc(base_measurements(component_errors=["C-001"]), now),
           "no_files": status_doc(base_measurements(), now, last_segment_end=now - 3 * 3600),   # 1 h files
           "critical": status_doc(base_measurements(storage_level=__import__("sentinel.models", fromlist=["x"])
                                                    .StorageLevel.CRITICAL, ffmpeg_running=False,
                                                    media_seen_this_run=False, seconds_since_media_advance=None),
                                  now)}[problem]
    rep = selfcheck.run_checks(_cfg(tmp_path), _reader(doc), now)
    assert not rep.ok and rep.failures


def test_first_file_of_a_new_run_is_not_due_until_its_period_ends(tmp_path: Path) -> None:
    """One-hour files (owner request 2026-11-11): after a reconnect the last saved file can be hours old
    while the current one is still open; that is measured from the start of the current run."""
    now = time.time()
    cfg = _cfg(tmp_path)
    doc = status_doc(base_measurements(), now, last_segment_end=now - 5 * 3600)
    doc["recording"]["run_started_utc"] = iso_utc(now - 40 * 60)
    rep = selfcheck.run_checks(cfg, _reader(doc), now)
    assert rep.ok, rep.summary()
    doc["recording"]["run_started_utc"] = iso_utc(now - 3 * 3600)
    assert not selfcheck.run_checks(cfg, _reader(doc), now).ok
    doc = status_doc(base_measurements(), now)
    doc["last_segment"] = None
    doc["recording"]["run_started_utc"] = iso_utc(now - 10 * 60)
    rep = selfcheck.run_checks(cfg, _reader(doc), now)
    assert rep.ok and any("still open" in c.detail for c in rep.checks)


def test_files_are_not_required_right_after_a_restart(tmp_path: Path) -> None:
    now = time.time()
    doc = status_doc(base_measurements(), now, last_segment_end=now - 3600)
    assert selfcheck.run_checks(_cfg(tmp_path), _reader(doc), now, require_saved_file=False).ok


def test_known_good_includes_the_camera_account_and_restore_is_undoable(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    now = time.time()
    rep = selfcheck.run_checks(p, _reader(status_doc(base_measurements(), now, last_segment_end=now)), now)
    selfcheck.mark_known_good(p, rep)
    secret = Path(load_config(p).camera.credentials_file)
    good_text, good_secret = p.read_bytes(), secret.read_bytes()
    p.write_text(p.read_text(encoding="utf-8").replace('192.0.2.5:', '192.0.2.99:'), encoding="utf-8")
    secret.write_bytes(b"changed")
    backup = selfcheck.restore_known_good(p, clock=lambda: 1_800_000_000.0)
    assert p.read_bytes() == good_text and secret.read_bytes() == good_secret
    assert b"192.0.2.99" in backup.read_bytes()                  # the bad settings are kept: undoable
    assert selfcheck.history(p)[0] == backup


def test_an_invalid_file_is_never_restored(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    bad = tmp_path / "bad.toml"
    bad.write_text("[camera]\nid = 5\n", encoding="utf-8")
    before = p.read_bytes()
    with pytest.raises(ConfigError):
        selfcheck.restore(p, bad)
    assert p.read_bytes() == before
