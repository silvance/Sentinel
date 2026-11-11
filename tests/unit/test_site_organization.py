"""Optional organisation name ([site] organization; owner request 2026-11-10): chosen per
installation, empty by default, and then the organisation is named nowhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import base_config_dict
from sentinel.config import ConfigError, load_config, load_config_dict
from sentinel.launcher import setup
from sentinel.paths import Layout


def _layout(tmp: Path) -> Layout:
    r = tmp / "machine"
    return Layout("machine", r / "config" / "sentinel.toml", r / "config" / "camera.secret", r / "spool",
                  r / "data", r / "logs")


def test_default_is_unnamed(tmp_path: Path) -> None:
    assert load_config_dict(base_config_dict(tmp_path, "tcp://127.0.0.1:9"), base_dir=tmp_path).site.organization == ""


@pytest.mark.parametrize("bad", [" padded", "x" * 101, "two\nlines", 7])
def test_bad_names_are_refused(tmp_path: Path, bad: object) -> None:
    d = base_config_dict(tmp_path, "tcp://127.0.0.1:9")
    d["site"] = {"organization": bad}
    with pytest.raises(ConfigError, match=r"\[site\] organization"):
        load_config_dict(d, base_dir=tmp_path)


def test_setup_writes_it_only_when_given(tmp_path: Path) -> None:
    p, _ = setup.write_setup(setup.SetupAnswers("", "", camera_later=True), _layout(tmp_path / "a"))
    assert "[site]" not in p.read_text(encoding="utf-8") and load_config(p).site.organization == ""
    p, _ = setup.write_setup(setup.SetupAnswers("", "", camera_later=True, organization="Bragg RA"),
                             _layout(tmp_path / "b"))
    assert load_config(p).site.organization == "Bragg RA"


def test_export_names_it_only_when_set() -> None:
    from zoneinfo import ZoneInfo

    from sentinel.export.manifest import ManifestBuilder

    batch = {"batch_id": "B1", "purpose": "review", "camera_id": "CAM01", "manifest_generated_utc": None,
             "planned_utc": None, "interval_start_utc": None, "interval_end_utc": None, "display_timezone": "UTC",
             "drive_id": "D1", "app_version": "t", "ffmpeg_version": "t", "operator_id": "x", "plan": None}
    for org in ("", "Bragg RA"):
        mb = ManifestBuilder(batch, [], {}, {}, ZoneInfo("UTC"), org)
        doc = json.loads(mb.manifest([]))
        assert ("organization" in doc) == bool(org) and doc.get("organization", "") == org
        html = mb.html()
        html = html.decode("utf-8") if isinstance(html, bytes) else html
        assert ("Organisation: Bragg RA" in html) == bool(org)


def test_diagnostic_report_names_it_only_when_set(tmp_path: Path) -> None:
    from dataclasses import replace

    from conftest import make_config
    from sentinel.config import SiteConfig
    from sentinel.diagnostics import build_report

    cfg = make_config(tmp_path)
    for org in ("", "Bragg RA"):
        c = replace(cfg, site=SiteConfig(org))
        out = tmp_path / f"r{len(org)}"
        out.mkdir()
        import zipfile

        with zipfile.ZipFile(build_report(c, "test", out)) as zf:
            text = zf.read("summary.txt").decode("utf-8")
        assert ("Organisation:       Bragg RA" in text) == bool(org)
