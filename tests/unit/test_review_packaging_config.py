"""Regression tests for the packaging/config/docs findings of review 2026-11-10.

Linux Python licence lookup, FFmpeg static-library notices and source offer, release/GPL FFmpeg
offers, Qt modules and replaceable PySide6, the build's source-state check, the
``Sentinel ledger`` command, status_dir overlap, config section types and the camera port.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

from sentinel.config import ConfigError, load_config_dict

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "ffmpeg"
BTBN_VERSION = "n9.0.2-48-gd4070a14ce-20261110"


def _load(name: str):  # noqa: ANN202
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        spec = importlib.util.spec_from_file_location(f"review_pkg_{name}", ROOT / "packaging" / f"{name}.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(ROOT / "packaging"))


lic = _load("licences_bundle")
fetch = _load("fetch_sources")
build = _load("build")
spec_common = _load("spec_common")


def _btbn_conf() -> str:
    text = (FIX / "btbn-9.0.2-lgpl.version.txt").read_text("utf-8")
    return next(ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("configuration:"))


def _tar(path: Path, files: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


# ---------------------------------------------------------------------- 1. Linux Python licence
def test_python_licence_found_with_the_standard_library_on_linux(tmp_path: Path) -> None:
    prefix, stdlib = tmp_path / "usr", tmp_path / "usr" / "lib64" / "python3.14"
    stdlib.mkdir(parents=True)
    (stdlib / "LICENSE.txt").write_text("PSF")
    cands = lic.python_licence_candidates(str(prefix), str(stdlib))
    assert lic.find_python_licence(cands) == stdlib / "LICENSE.txt"


def test_python_licence_missing_names_every_place_looked(tmp_path: Path) -> None:
    cands = lic.python_licence_candidates(str(tmp_path / "p"), str(tmp_path / "p" / "lib" / "python3.12"))
    with pytest.raises(lic.LicenceError, match="python3.12"):
        lic.find_python_licence(cands)


def test_this_interpreters_licence_is_found() -> None:
    assert lic.find_python_licence().is_file()


# ---------------------------------------------------------------------- 2. static FFmpeg libraries
def test_btbn_configuration_names_the_linked_libraries() -> None:
    libs = fetch.external_libraries(_btbn_conf())
    for name in ("mp3lame", "ssh", "srt", "zmq", "twolame", "bluray", "gmp", "opencore-amrnb", "soxr", "ass",
                 "fribidi", "xml2", "dav1d", "aom", "opus", "zlib"):
        assert name in libs, name
    assert "x264" not in libs                  # --disable-libx264 is not a linked library


def test_btbn_scripts_pins_are_parsed_for_the_lgpl_variant(tmp_path: Path) -> None:
    root = "FFmpeg-Builds-autobuild-X/scripts.d/"
    tar = _tar(tmp_path / "b.tar.gz", {
        root + "50-libssh.sh": b'SCRIPT_REPO="https://gitlab.com/libssh/libssh-mirror.git"\n'
                               b'SCRIPT_COMMIT="9de02f3d"\nffbuild_enabled() {\n    return 0\n}\n',
        root + "50-x264.sh": b'SCRIPT_REPO="https://code.videolan.org/videolan/x264.git"\nSCRIPT_COMMIT="0480cb"\n'
                             b'ffbuild_enabled() {\n    [[ $VARIANT == lgpl* ]] && return -1\n    return 0\n}\n',
        root + "50-fdk.sh": b'SCRIPT_REPO="https://github.com/mstorsjo/fdk-aac.git"\nSCRIPT_COMMIT="abc"\n'
                            b'ffbuild_enabled() {\n    [[ $VARIANT == nonfree* ]] || return -1\n    return 0\n}\n',
        root + "50-libmp3lame.sh": b'SCRIPT_REPO="https://svn.code.sf.net/p/lame/svn/trunk/lame"\nSCRIPT_REV="7113"\n',
        root + "50-soxr.sh": b'SCRIPT_REPO="https://git.code.sf.net/p/soxr/code"\nSCRIPT_COMMIT="945b59"\n',
        root + "50-vpx.sh": b'SCRIPT_REPO="https://chromium.googlesource.com/webm/libvpx"\nSCRIPT_COMMIT="c0ffee"\n'
                            b'SCRIPT_REPO2="https://github.com/a/b.git"\nSCRIPT_COMMIT2="v1.0"\n',
        root + "zz-final.sh": b'SCRIPT_SKIP="1"\n',
        "FFmpeg-Builds-autobuild-X/README.md": b"x",
    })
    deps = {d.name: d for d in fetch.btbn_dependencies(tar)}
    assert set(deps) == {"libssh-mirror", "lame", "soxr", "libvpx", "b"}      # x264 (GPL), fdk-aac (nonfree) out
    assert deps["lame"].kind == "svn" and deps["lame"].ref == "7113"
    assert fetch.dependency_archive_url(deps["libssh-mirror"]) == \
        "https://gitlab.com/libssh/libssh-mirror/-/archive/9de02f3d/libssh-mirror-9de02f3d.tar.gz"
    assert fetch.dependency_archive_url(deps["libvpx"]) == \
        "https://chromium.googlesource.com/webm/libvpx/+archive/c0ffee.tar.gz"
    assert fetch.dependency_archive_url(deps["b"]) == "https://github.com/a/b/archive/v1.0.tar.gz"
    assert fetch.dependency_archive_url(deps["soxr"]) is None              # needs git
    assert "git clone https://git.code.sf.net/p/soxr/code soxr" in fetch.checkout_command(deps["soxr"])
    assert fetch.checkout_command(deps["lame"]).startswith("svn export -r 7113")


def test_real_btbn_scripts_when_archived() -> None:
    tar = ROOT / "third_party" / "sources" / f"FFmpeg-Builds-{fetch.BTBN_RELEASE}.tar.gz"
    if not tar.is_file():
        pytest.skip("BtbN scripts archive not present (third_party/ is git-ignored)")
    names = {d.name for d in fetch.btbn_dependencies(tar)}
    assert {"libssh-mirror", "lame", "srt", "libzmq", "twolame", "gmplib", "soxr", "libass"} <= names
    assert "x264" not in names and "x265" not in names


def test_notices_extracted_without_code_or_unsafe_paths(tmp_path: Path) -> None:
    arc = _tar(tmp_path / "q.tar.gz", {
        "qtbase-src/LICENSES/LGPL-3.0-only.txt": b"L", "qtbase-src/src/3rdparty/zlib/LICENSE": b"Z",
        "qtbase-src/src/3rdparty/zlib/qt_attribution.json": b"{}", "qtbase-src/COPYING": b"C",
        "qtbase-src/src/corelib/qstring.cpp": b"code", "qtbase-src/sub/LICENSE": b"not top level",
        "../evil/LICENSE": b"x",
    })
    out = tmp_path / "n"
    got = fetch.extract_notices(arc, out)
    assert got == ["COPYING", "LICENSES/LGPL-3.0-only.txt", "src/3rdparty/zlib/LICENSE",
                   "src/3rdparty/zlib/qt_attribution.json"]
    assert not (tmp_path / "evil").exists() and not list(out.rglob("*.cpp"))


def _app(tmp_path: Path, files: list[str]) -> Path:
    app = tmp_path / "Sentinel"
    for f in files:
        p = app / "_internal" / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return app


def _notices(tmp_path: Path, qt: str, ref: str = "d4070a14ce", complete: bool = True) -> Path:
    n = tmp_path / "notices"
    (n / "btbn-dep-libssh").mkdir(parents=True)
    (n / "btbn-dep-libssh" / "COPYING").write_text("libssh LGPL")
    (n / fetch.NOTICES_INDEX).write_text(json.dumps({"ffmpeg_ref": ref, "qt": qt, "complete": complete}))
    return n


GOOD_APP = ["PySide6/__init__.py", "PySide6/Qt6Core.dll", "PySide6/Qt6Svg.dll", "shiboken6/__init__.py",
            "PySide6/plugins/imageformats/qjpeg.dll"]


def test_licences_without_library_notices_are_marked_incomplete(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    app = _app(tmp_path, GOOD_APP)
    block = lic.write_licences(app, BTBN_VERSION, "d4070a14ce", "autobuild-X",
                               ffmpeg_configuration=_btbn_conf(), notices_dir=tmp_path / "none")
    assert block["notices_complete"] is False and "fetch_sources" in block["notices_problems"][0]
    notices = (app / "licences" / "THIRD-PARTY-NOTICES.txt").read_text("utf-8")
    assert "mp3lame" in notices and "NOT INCLUDED" in notices and "INCOMPLETE" in notices
    offer = (app / "licences" / "SOURCE-OFFER.txt").read_text("utf-8")
    assert "Libraries linked into ffmpeg/ffprobe" in offer and "srt" in offer and "scripts.d" in offer


def test_licences_with_matching_notices_are_complete(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    app = _app(tmp_path, GOOD_APP)
    qt = lic._version("pyside6-essentials")
    block = lic.write_licences(app, BTBN_VERSION, "d4070a14ce", "autobuild-X", ffmpeg_configuration=_btbn_conf(),
                               notices_dir=_notices(tmp_path, qt), platform="win32")
    assert block["notices_complete"] is True, block["notices_problems"]
    assert (app / "licences" / "third-party" / "btbn-dep-libssh" / "COPYING").read_text() == "libssh LGPL"
    assert "third-party/btbn-dep-libssh/COPYING" in block["files"]
    assert block["pyside6_python"] == "replaceable .py files"
    offer = (app / "licences" / "SOURCE-OFFER.txt").read_text("utf-8")
    assert "PySide6/shiboken6 Python files" in offer and "_internal\\PySide6" in offer
    assert "qtimageformats" not in offer                         # its plugins are not in this folder


@pytest.mark.parametrize("ref, complete", [("other", True), ("d4070a14ce", False)])
def test_notices_for_another_build_or_incomplete_fetch_are_not_trusted(tmp_path: Path, ref: str,
                                                                       complete: bool) -> None:
    pytest.importorskip("PySide6")
    app = _app(tmp_path, GOOD_APP)
    qt = lic._version("pyside6-essentials")
    block = lic.write_licences(app, BTBN_VERSION, "d4070a14ce", "autobuild-X", ffmpeg_configuration=_btbn_conf(),
                               notices_dir=_notices(tmp_path, qt, ref, complete))
    assert block["notices_complete"] is False


# ---------------------------------------------------------------------- 6. release / GPL FFmpeg
def test_release_and_gyan_versions_point_at_release_tags() -> None:
    assert build.ffmpeg_source_ref("7.1.2") == "n7.1.2"
    assert build.ffmpeg_source_ref("n7.1.2") == "n7.1.2"
    assert build.ffmpeg_source_ref("9.0.1-full_build-www.gyan.dev") == "n9.0.1"
    assert build.ffmpeg_source_ref(BTBN_VERSION) == "d4070a14ce"
    assert fetch.ffmpeg_source_urls("n7.1.2")[0] == "https://ffmpeg.org/releases/ffmpeg-7.1.2.tar.xz"


def test_self_built_release_offer_names_no_btbn(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    conf = "--disable-shared --enable-static --enable-version3 --disable-doc"
    assert not fetch.is_btbn_build(conf) and fetch.is_btbn_build(_btbn_conf())
    lic.write_licences(tmp_path, "7.1.2", "n7.1.2", None, ffmpeg_configuration=conf)
    offer = (tmp_path / "licences" / "SOURCE-OFFER.txt").read_text("utf-8")
    assert "BtbN" not in offer and "ffmpeg.org/releases/ffmpeg-7.1.2.tar.xz" in offer
    assert "archive/7.1.2.tar.gz" not in offer and conf in offer
    items = fetch.source_list({"ffmpeg": {"version": "7.1.2"}, "build_environment": ["PySide6_Essentials==6.11.3"]},
                              None)
    assert items[0][1] == "https://ffmpeg.org/releases/ffmpeg-7.1.2.tar.xz"
    assert not any("BtbN" in u for _, u, _ in items)


def test_gpl_internal_build_is_labelled_gpl(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    lic.write_licences(tmp_path, "9.0.1-full_build-www.gyan.dev", "n9.0.1", None,
                       ffmpeg_licence="GPL-3.0-or-later", ffmpeg_configuration="--enable-gpl --enable-libx264")
    notices = (tmp_path / "licences" / "THIRD-PARTY-NOTICES.txt").read_text("utf-8")
    offer = (tmp_path / "licences" / "SOURCE-OFFER.txt").read_text("utf-8")
    assert "GPL-3.0-or-later" in notices and "LGPL-3.0-or-later" not in notices
    assert "INTERNAL USE ONLY" in offer and "BtbN" not in offer


# ---------------------------------------------------------------------- 7. Qt modules / PySide6 / Mesa
def test_qt_modules_follow_the_files_in_the_folder() -> None:
    assert fetch.qt_modules_in(["PySide6/Qt6Core.dll"]) == ["qtbase", "qttranslations"]
    mods = fetch.qt_modules_in(["PySide6/Qt6Svg.dll", "PySide6\\plugins\\imageformats\\qtiff.dll"])
    assert "qtsvg" in mods and "qtimageformats" in mods
    urls = [u for _, u, _ in fetch.qt_archive_urls("6.11.3", mods)]
    assert any(u.endswith("qtimageformats-everywhere-src-6.11.3.tar.xz") for u in urls)


def test_sealed_pyside_or_mesa_dll_makes_the_folder_incomplete(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    app = _app(tmp_path, ["PySide6/Qt6Core.dll", "PySide6/opengl32sw.dll", "PySide6/plugins/imageformats/qwebp.dll"])
    block = lic.write_licences(app, BTBN_VERSION, "d4070a14ce", "r")
    problems = " ".join(block["notices_problems"])
    assert "inside Sentinel.exe" in problems and "opengl32sw.dll" in problems
    assert "qtimageformats" in block["qt_modules"]
    assert "qtimageformats-everywhere-src" in (app / "licences" / "SOURCE-OFFER.txt").read_text("utf-8")


def test_spec_leaves_out_unused_qt_binaries_and_keeps_pyside_replaceable() -> None:
    toc = [("PySide6\\opengl32sw.dll", "s", "BINARY"), ("PySide6\\plugins\\imageformats\\qtiff.dll", "s", "BINARY"),
           ("PySide6/plugins/imageformats/libqwebp.so", "s", "BINARY"),
           ("PySide6\\plugins\\imageformats\\qjpeg.dll", "s", "BINARY"),
           ("PySide6\\plugins\\imageformats\\qsvg.dll", "s", "BINARY"), ("PySide6\\Qt6Core.dll", "s", "BINARY")]
    kept = [e[0] for e in spec_common.filter_binaries(toc)]
    assert kept == ["PySide6\\plugins\\imageformats\\qjpeg.dll", "PySide6\\plugins\\imageformats\\qsvg.dll",
                    "PySide6\\Qt6Core.dll"]
    assert spec_common.MODULE_COLLECTION_MODE == {"PySide6": "py", "shiboken6": "py"}
    for spec in ("windows", "linux"):
        text = (ROOT / "packaging" / spec / "sentinel.spec").read_text("utf-8")
        assert "module_collection_mode=common.MODULE_COLLECTION_MODE" in text
        assert "common.filter_binaries(a.binaries)" in text


# ---------------------------------------------------------------------- 8. source state
def test_untracked_files_count_and_missing_git_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        calls.append(args)
        return {"rev-parse": "abc123", "status": "?? src/sentinel/gui/app/new.py"}[args[0]]

    monkeypatch.setattr(build, "git", fake_git)
    with pytest.raises(build.BuildError, match="untracked"):
        build.source_state(allow_dirty=False)
    assert ("status", "--porcelain") in calls                       # no --untracked-files=no
    assert build.source_state(allow_dirty=True) == ("abc123", True)
    monkeypatch.setattr(build, "git", lambda *a: "")
    with pytest.raises(build.BuildError, match="git commit"):
        build.source_state(allow_dirty=False)
    assert build.source_state(allow_dirty=True) == ("unknown", True)


def test_build_help_runs() -> None:
    with pytest.raises(SystemExit) as exc:
        build.main(["--help"])
    assert exc.value.code == 0


# ---------------------------------------------------------------------- 3. Sentinel ledger
def test_ledger_command_forwards_and_finds_the_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel import ledgerchain
    from sentinel.launcher import cli

    seen: list[tuple[list[str], str]] = []
    monkeypatch.setattr(ledgerchain, "main", lambda argv, prog="": seen.append((list(argv), prog)) or 0)
    cfg = tmp_path / "sentinel.toml"
    cfg.write_text("")
    monkeypatch.setattr(cli, "find_config", lambda explicit=None: cfg)
    assert cli.main(["ledger", "head"]) == 0
    assert seen[-1] == (["head", "--config", str(cfg)], "Sentinel ledger")
    assert cli.main(["ledger", "verify", "--config", "X", "--check-files"]) == 0
    assert seen[-1][0] == ["verify", "--config", "X", "--check-files"]
    assert cli.main(["ledger", "--help"]) == 0 and seen[-1][0] == ["--help"]
    monkeypatch.setattr(cli, "find_config", lambda explicit=None: None)
    assert cli.main(["ledger", "head"]) == 2


def test_ledger_command_is_in_the_parser_and_the_build() -> None:
    from sentinel.launcher import cli

    assert "ledger" in cli.build_parser().format_help()
    assert "sentinel.ledgerchain" in spec_common.HIDDEN_IMPORTS
    for doc in ("docs/07_SECURITY.md", "docs/09_BENCH_TEST.md"):
        text = (ROOT / doc).read_text("utf-8")
        assert "Sentinel.exe ledger head" in text
    assert "python -m sentinel.ledgerchain head`)" not in (ROOT / "docs/07_SECURITY.md").read_text("utf-8")


def test_about_warns_when_notices_are_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    from sentinel.launcher import cli

    (tmp_path / "BUILD-MANIFEST.json").write_text(json.dumps({
        "ffmpeg_license_status": "OK: LGPL-3.0-or-later", "ffmpeg": {"version": "x"},
        "third_party_licences": {"notices_complete": False, "notices_problems": ["no licence files"]}}))
    monkeypatch.setattr(cli, "app_dir", lambda: tmp_path)
    assert cli.main(["about"]) == 0
    out = capsys.readouterr().out
    assert "Licence notices incomplete" in out and "no licence files" in out


# ---------------------------------------------------------------------- 4/10. configuration
def _cfg(tmp_path: Path, **over: object) -> dict:
    d: dict = {"camera": {"id": "CAM01", "url": "rtsp://192.0.2.10:554/x"},
               "storage": {"spool_dir": str(tmp_path / "spool"), "data_dir": str(tmp_path / "data"),
                           "log_dir": str(tmp_path / "logs")}}
    d.update(over)
    return d


@pytest.mark.parametrize("status", ["", "data", "data/status", "spool", "logs/x"])
def test_status_dir_overlapping_service_only_folders_is_refused(tmp_path: Path, status: str) -> None:
    d = _cfg(tmp_path)
    d["storage"]["status_dir"] = str(tmp_path / status) if status else str(tmp_path)
    with pytest.raises(ConfigError, match="status_dir must be its own folder"):
        load_config_dict(d, base_dir=tmp_path)


def test_separate_status_dir_is_accepted(tmp_path: Path) -> None:
    d = _cfg(tmp_path)
    d["storage"]["status_dir"] = str(tmp_path / "status")
    assert load_config_dict(d, base_dir=tmp_path).status_path == tmp_path / "status" / "status.json"
    d["storage"]["status_dir"] = str(tmp_path / "database")      # shares a prefix with "data", not inside it
    load_config_dict(d, base_dir=tmp_path)


@pytest.mark.parametrize("raw", [{"camera": "x"}, {"camera": [{"id": "A"}]}])
def test_camera_section_that_is_not_a_table_is_a_config_error(raw: dict) -> None:
    with pytest.raises(ConfigError, match=r"\[camera\] must be a single table"):
        load_config_dict(raw)


@pytest.mark.parametrize("section", ["recording", "storage", "tools", "service", "export", "archive",
                                     "retention", "motion"])
def test_other_sections_that_are_not_tables_are_config_errors(tmp_path: Path, section: str) -> None:
    with pytest.raises(ConfigError, match=rf"\[{section}\] must be a single table"):
        load_config_dict(_cfg(tmp_path, **{section: [1]}), base_dir=tmp_path)


@pytest.mark.parametrize("url", ["rtsp://192.0.2.10:99999/x", "rtsp://192.0.2.10:0/x", "rtsp://192.0.2.10:abc/x"])
def test_camera_port_out_of_range_is_refused(tmp_path: Path, url: str) -> None:
    with pytest.raises(ConfigError, match="port"):
        load_config_dict(_cfg(tmp_path, camera={"id": "CAM01", "url": url}), base_dir=tmp_path)


def test_valid_ports_still_accepted(tmp_path: Path) -> None:
    for url in ("rtsp://192.0.2.10:554/x", "rtsp://192.0.2.10/x", "rtsps://[2001:db8::1]:322/x"):
        load_config_dict(_cfg(tmp_path, camera={"id": "CAM01", "url": url}), base_dir=tmp_path)


# ---------------------------------------------------------------------- 5. Windows example
def test_windows_example_points_at_the_built_ffmpeg_folder() -> None:
    raw = tomllib.loads((ROOT / "config" / "sentinel.windows.example.toml").read_text("utf-8"))
    for tool in ("ffmpeg", "ffprobe"):
        p = raw["tools"][tool].replace("\\", "/")
        assert p == f"C:/Program Files/Sentinel/ffmpeg/{tool}.exe"        # build.py: <app>/ffmpeg/<tool>.exe
    text = (ROOT / "config" / "sentinel.windows.example.toml").read_text("utf-8")
    assert "SentinelService.exe install" in text
    load_config_dict(raw, base_dir=ROOT / "config")                   # still a valid configuration


# ---------------------------------------------------------------------- 9. bench test
def test_bench_test_explains_the_erase_gate() -> None:
    text = (ROOT / "docs" / "09_BENCH_TEST.md").read_text("utf-8")
    sec5 = text.split("## 5.")[1].split("## 6.")[0]
    assert "erase_enabled" in sec5 and "design-drive-prepare.md §9" in sec5 and "M-010" in sec5
