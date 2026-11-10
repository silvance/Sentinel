"""Recording schedule and power check as the operator sees them: configuration, status decision,
P-001 / H-001 wording, gap wording."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import base_config_dict, make_config
from gui_support import base_measurements
from sentinel.alerts import CATALOG, Severity, make_alert
from sentinel.config import ConfigError, load_config_dict
from sentinel.export.manifest import GAP_REASONS
from sentinel.gui.ledger_view import GAP_REASON_WORDS
from sentinel.models import OverallStatus, RunEndReason, StorageLevel
from sentinel.power import PowerSettings
from sentinel.status import evaluate

WEEKEND = {"enabled": True, "weekly": [{"action": "pause", "days": ["weekend"], "start": "00:00", "end": "24:00"}]}


def _eval(**kw):  # noqa: ANN202
    return evaluate(base_measurements(**kw), stall_timeout_s=6, segment_seconds=300, connect_timeout_s=10,
                    escalate_after_s=180)


PAUSED = dict(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
              outage_seconds=36_000.0, attempts=4, last_end_reason="scheduled_pause", scheduled_pause=True)


# ---------------------------------------------------------------------- configuration
def test_schedule_section_is_parsed_with_the_display_timezone(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, recording={"display_timezone": "America/Chicago"}, schedule=WEEKEND)
    assert cfg.schedule.enabled and cfg.schedule.timezone == "America/Chicago"
    assert make_config(tmp_path / "x").schedule.enabled is False            # off unless configured


def test_bad_schedule_is_a_configuration_error(tmp_path: Path) -> None:
    raw = base_config_dict(tmp_path, schedule={"enabled": True, "default": "pause"})
    with pytest.raises(ConfigError, match="never records"):
        load_config_dict(raw, base_dir=tmp_path)


def test_example_configuration_documents_a_disabled_schedule() -> None:
    from sentinel.config import load_config

    cfg = load_config(Path(__file__).resolve().parents[2] / "config" / "sentinel.example.toml")
    assert cfg.schedule.enabled is False and len(cfg.schedule.weekly) >= 1


# ---------------------------------------------------------------------- status decision
def test_scheduled_pause_is_planned_not_an_outage() -> None:
    d = _eval(**PAUSED)
    assert d.overall is OverallStatus.PAUSED
    assert [a.code for a in d.alerts] == ["P-001"]               # no R-00x, even 10 h "outage"
    a = d.alerts[0]
    assert a.severity == Severity.NOTICE and not a.recording_stopped
    assert a.message.startswith("Recording is paused by the recording schedule (P-001).")
    assert "Error" not in a.message


def test_other_problems_still_show_while_paused() -> None:
    d = _eval(**PAUSED, storage_level=StorageLevel.CRITICAL)
    assert d.overall is OverallStatus.PAUSED and {"S-002", "P-001"} <= {a.code for a in d.alerts}
    d2 = _eval(**PAUSED, component_errors=["D-001"])
    assert d2.overall is OverallStatus.ERROR and "P-001" not in {a.code for a in d2.alerts}


def test_reconnecting_after_a_pause_gets_the_start_grace_then_escalates_normally() -> None:
    after = dict(ffmpeg_running=True, media_seen_this_run=False, seconds_since_media_advance=None,
                 outage_seconds=4.0, attempts=5, last_end_reason="scheduled_pause", now_mono=10_000.0)
    assert _eval(**after, resumed_s=4.0).overall is OverallStatus.STARTING
    late = _eval(**dict(after, outage_seconds=200.0, ffmpeg_running=False), resumed_s=200.0, in_backoff=True)
    assert late.overall is OverallStatus.INTERRUPTED
    assert late.alerts[0].code == "R-003" and late.alerts[0].severity == Severity.CRITICAL


def test_gap_reason_is_worded_for_operators_and_reviewers() -> None:
    assert str(RunEndReason.SCHEDULED_PAUSE) == "scheduled_pause"
    assert "schedule" in GAP_REASONS["scheduled_pause"] and "planned" in GAP_REASONS["scheduled_pause"]
    assert "schedule" in GAP_REASON_WORDS["scheduled_pause"]


def test_new_alert_codes() -> None:
    assert CATALOG["P-001"].stops_recording is False
    h = make_alert("H-001", Severity.WARNING)
    assert h.message == ("Error H-001: This computer is set to go to sleep, which would stop recording. "
                         "Recording continues for now. Do not close the lid. Contact technical support and "
                         "provide diagnostic report.")


# ---------------------------------------------------------------------- service: power check -> H-001
def test_power_risk_raises_h001_in_the_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel import service as service_mod
    from sentinel.db import Ledger
    from sentinel.service import RecorderService

    cfg = make_config(tmp_path)
    svc = RecorderService(cfg)
    svc.ledger = Ledger(cfg.data_path / "sentinel.db")
    try:
        monkeypatch.setattr(service_mod, "read_power_settings",
                            lambda: PowerSettings(lid_present=True, lid_ac=1, lid_dc=0))
        svc._check_power()
        doc = svc._build_status(svc.clock.mono(), stopping=False)
        codes = [a["code"] for a in doc.alerts]
        assert "H-001" in codes
        assert doc.power["risks"] == ["closing the lid makes the computer sleep (on mains power)"]
        assert [e["code"] for e in svc.ledger.query("SELECT code FROM events WHERE code='sleep_risk'")] == ["sleep_risk"]
        svc._check_power()                                          # unchanged risk: not logged again
        assert len(svc.ledger.query("SELECT 1 FROM events WHERE code='sleep_risk'")) == 1
        monkeypatch.setattr(service_mod, "read_power_settings",
                            lambda: PowerSettings(lid_present=True, lid_ac=0, lid_dc=0))
        svc._check_power()
        doc = svc._build_status(svc.clock.mono(), stopping=False)
        assert "H-001" not in [a["code"] for a in doc.alerts]
    finally:
        svc.ledger.close()
