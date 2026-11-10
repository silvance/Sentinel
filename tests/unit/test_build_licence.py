"""FFmpeg licence policy of packaging/build.py (owner decision: LGPL only), from captured outputs.

Fixtures in tests/fixtures/ffmpeg/ were captured from the development host's FFmpeg
(gyan.dev 9.0.1 "full" build, GPL-3.0). LGPL and nonfree cases use synthetic lines built from
the same wording FFmpeg prints (``ffmpeg -L``), since no such build is installed here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "ffmpeg"


def _load_build():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("sentinel_packaging_build", ROOT / "packaging" / "build.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


build = _load_build()


def _fixture(kind: str) -> str:
    return (FIX / f"gyan-9.0.1-full.{kind}.txt").read_text("utf-8")


def _configuration(version_text: str) -> str:
    return next(ln.split(":", 1)[1].strip() for ln in version_text.splitlines() if ln.startswith("configuration:"))


GYAN_CONF = _configuration(_fixture("version"))
GYAN_L = _fixture("L")
LGPL3_L = ("ffmpeg is free software; you can redistribute it and/or modify\nit under the terms of the GNU Lesser "
           "General Public License as published by\nthe Free Software Foundation; either\nversion 3 of the License, "
           "or (at your option) any later version.\n")
LGPL21_L = LGPL3_L.replace("version 3 of the License", "version 2.1 of the License")
NONFREE_L = "This version of ffmpeg has nonfree parts compiled in.\nTherefore it is not legally redistributable.\n"
LGPL_CONF = ("--prefix=/ffbuild/prefix --pkg-config-flags=--static --enable-version3 --disable-debug "
             "--enable-libdav1d --enable-libvpx --enable-libopus --enable-zlib --enable-gnutls")


def test_captured_gyan_build_is_gpl3_by_both_sources() -> None:
    assert build.licence_class(GYAN_CONF) == "GPL-3.0-or-later"
    assert build.licence_from_text(GYAN_L) == "GPL-3.0-or-later"
    assert "--enable-libx264" in build.gpl_flags(GYAN_CONF)


def test_gpl_refused_by_default_with_clear_message() -> None:
    with pytest.raises(build.BuildError, match="LGPL FFmpeg only.*--allow-gpl-internal-use"):
        build.licence_decision(GYAN_CONF, {"ffmpeg": GYAN_L, "ffprobe": GYAN_L}, allow_gpl_internal_use=False)


def test_gpl_internal_use_override_is_recorded_prominently() -> None:
    block = build.licence_decision(GYAN_CONF, {"ffmpeg": GYAN_L, "ffprobe": GYAN_L}, allow_gpl_internal_use=True)
    assert block["gpl_internal_use_override"] is True
    assert "INTERNAL USE ONLY" in block["license_status"] and "NOT be distributed" in block["license_status"]


@pytest.mark.parametrize("conf,text,expected", [
    (LGPL_CONF, LGPL3_L, "LGPL-3.0-or-later"),
    (LGPL_CONF.replace("--enable-version3 ", ""), LGPL21_L, "LGPL-2.1-or-later"),
])
def test_lgpl_accepted(conf: str, text: str, expected: str) -> None:
    block = build.licence_decision(conf, {"ffmpeg": text, "ffprobe": text}, allow_gpl_internal_use=False)
    assert block["license"] == expected and block["license_status"].startswith("OK")
    assert block["gpl_internal_use_override"] is False and block["gpl_flags"] == []


@pytest.mark.parametrize("allow", [False, True])
def test_nonfree_always_refused(allow: bool) -> None:
    conf = LGPL_CONF + " --enable-nonfree --enable-libfdk-aac"
    with pytest.raises(build.BuildError, match="nonfree"):
        build.licence_decision(conf, {"ffmpeg": NONFREE_L, "ffprobe": NONFREE_L}, allow_gpl_internal_use=allow)


def test_gpl_library_without_gpl_flag_counts_as_gpl() -> None:
    assert build.licence_class(LGPL_CONF + " --enable-libx264") == "GPL-3.0-or-later"
    assert build.licence_class("--enable-postproc") == "GPL-2.0-or-later"


@pytest.mark.parametrize("ffprobe_text", [GYAN_L, "something unexpected"])
def test_disagreeing_or_unknown_evidence_refused(ffprobe_text: str) -> None:
    # Configure flags say LGPL, but one binary reports otherwise (e.g. mixed builds in one folder).
    with pytest.raises(build.BuildError, match="disagrees|cannot determine"):
        build.licence_decision(LGPL_CONF, {"ffmpeg": LGPL3_L, "ffprobe": ffprobe_text}, allow_gpl_internal_use=True)


def test_required_components_parsed_from_captured_lists() -> None:
    for kind, wanted in build.REQUIRED_COMPONENTS.items():
        have = build.parse_components(kind, _fixture(kind))
        assert set(wanted) <= have, (kind, set(wanted) - have)
    assert "mov" in build.parse_components("demuxers", _fixture("demuxers"))     # "mov,mp4,m4a,..." split
    assert "h264" not in build.parse_components("muxers", "  E h264x   fake\n")    # no header: nothing parsed


def test_manifest_status_shown_by_about(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    from sentinel.launcher import cli
    block = build.licence_decision(GYAN_CONF, {"ffmpeg": GYAN_L, "ffprobe": GYAN_L}, allow_gpl_internal_use=True)
    (tmp_path / "BUILD-MANIFEST.json").write_text(json.dumps({
        "ffmpeg_license_status": block["license_status"], "ffmpeg": {"version": "x", **block}}))
    monkeypatch.setattr(cli, "app_dir", lambda: tmp_path)
    assert cli.main(["about"]) == 0
    out = capsys.readouterr().out
    assert "INTERNAL USE ONLY" in out and "must not be given to anyone outside" in out
