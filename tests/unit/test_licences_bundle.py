"""Third-party licences and the LGPL source offer shipped in the build (packaging/licences_bundle.py,
packaging/fetch_sources.py). Qt/PySide6 wheels carry only the commercial licence file, so the build
must add the LGPL/GPL texts itself, and say exactly where the matching source is."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERSION = "n9.0.2-48-gd4070a14ce-20261110"


def _load(name: str):  # noqa: ANN202
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        spec = importlib.util.spec_from_file_location(f"sentinel_packaging_{name}", ROOT / "packaging" / f"{name}.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(ROOT / "packaging"))


lic = _load("licences_bundle")
fetch = _load("fetch_sources")


def test_committed_licence_texts_are_the_real_ones() -> None:
    gpl = (ROOT / "packaging" / "licences" / "GPL-3.0.txt").read_text(encoding="utf-8")
    lgpl = (ROOT / "packaging" / "licences" / "LGPL-3.0.txt").read_text(encoding="utf-8")
    assert gpl.lstrip().startswith("GNU GENERAL PUBLIC LICENSE") and "Version 3, 29 June 2007" in gpl
    assert "END OF TERMS AND CONDITIONS" in gpl and len(gpl.splitlines()) > 600
    assert lgpl.lstrip().startswith("GNU LESSER GENERAL PUBLIC LICENSE") and "Version 3, 29 June 2007" in lgpl


def test_licences_folder_contents(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    block = lic.write_licences(tmp_path, VERSION, "d4070a14ce", "autobuild-2026-11-10-12-39")
    d = tmp_path / "licences"
    names = {p.name for p in d.iterdir()}
    assert {"LGPL-3.0.txt", "GPL-3.0.txt", "Python-LICENSE.txt", "SOURCE-OFFER.txt",
            "THIRD-PARTY-NOTICES.txt"} <= names
    assert not any("Commercial" in n for n in names)          # Qt is used under the LGPL, not commercially
    offer = (d / "SOURCE-OFFER.txt").read_text(encoding="utf-8")
    qt = block["qt_for_python"]
    assert "https://github.com/FFmpeg/FFmpeg/archive/d4070a14ce.tar.gz" in offer
    assert "FFmpeg-Builds/tree/autobuild-2026-11-10-12-39" in offer
    assert f"qtbase-everywhere-src-{qt}.tar.xz" in offer and f"pyside-setup-everywhere-src-{qt}.tar.xz" in offer
    assert "three years" in offer and "replace the Qt/PySide6 DLLs" in offer
    notices = (d / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
    assert "LGPL-3.0-only" in notices and "LGPL-3.0-or-later" in notices
    assert block["files"] == sorted(names)


def test_contact_is_named_when_given(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    lic.write_licences(tmp_path, VERSION, "d4070a14ce", "r", contact="the Technical Unit, ext. 1234")
    assert "the Technical Unit, ext. 1234 will give you" in (tmp_path / "licences" / "SOURCE-OFFER.txt").read_text()


def test_missing_licence_text_fails_the_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lic, "TEXTS", tmp_path / "nowhere")
    with pytest.raises(lic.LicenceError, match="GPL"):
        lic.write_licences(tmp_path, VERSION, "d4070a14ce", "r")


def test_ffmpeg_source_reference() -> None:
    assert fetch.ffmpeg_commit(VERSION) == "d4070a14ce"
    build = _load("build")
    assert build.ffmpeg_source_ref(VERSION) == "d4070a14ce"
    assert build.ffmpeg_source_ref("n9.0.2") == "n9.0.2"          # own build from a release tag


def test_source_list_names_exact_versions() -> None:
    items = fetch.source_list({"ffmpeg": {"version": VERSION},
                               "build_environment": ["PySide6_Essentials==6.11.3", "x==1"]}, "autobuild-X")
    urls = [u for _, u, _ in items]
    assert "https://github.com/FFmpeg/FFmpeg/archive/d4070a14ce.tar.gz" in urls
    assert any(u.endswith("/6.11/6.11.3/submodules/qtbase-everywhere-src-6.11.3.tar.xz") for u in urls)
    assert any("autobuild-X" in u for u in urls)
