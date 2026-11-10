"""Install layout: bundled-tool resolution, per-machine defaults, configuration lookup order."""

from __future__ import annotations

import sys
from pathlib import Path, PureWindowsPath

import pytest

from conftest import base_config_dict
from sentinel import paths
from sentinel.config import load_config_dict


def _bundle(app: Path, *names: str, platform: str = sys.platform) -> None:
    (app / "ffmpeg").mkdir(parents=True, exist_ok=True)
    for n in names:
        (app / "ffmpeg" / paths.tool_filename(n, platform)).write_bytes(b"")


def _freeze(monkeypatch: pytest.MonkeyPatch, app: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "Sentinel.exe"))


def test_tool_filename_per_platform() -> None:
    assert paths.tool_filename("ffmpeg", "win32") == "ffmpeg.exe"
    assert paths.tool_filename("ffprobe", "linux") == "ffprobe"


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_bundled_tool_found_next_to_app(tmp_path: Path, platform: str) -> None:
    _bundle(tmp_path, "ffmpeg", platform=platform)
    got = paths.default_tool("ffmpeg", app=tmp_path, platform=platform)
    assert Path(got) == tmp_path / "ffmpeg" / paths.tool_filename("ffmpeg", platform)
    assert paths.default_tool("ffprobe", app=tmp_path, platform=platform) == "ffprobe"   # not bundled -> PATH


def test_directory_named_like_tool_is_not_a_tool(tmp_path: Path) -> None:
    (tmp_path / "ffmpeg" / paths.tool_filename("ffmpeg")).mkdir(parents=True)
    assert paths.default_tool("ffmpeg", app=tmp_path) == "ffmpeg"


def test_source_checkout_never_uses_bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths.app_dir() is None
    assert paths.default_tool("ffmpeg") == "ffmpeg"


def test_config_uses_bundled_tools_when_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    _bundle(app, "ffmpeg", "ffprobe")
    _freeze(monkeypatch, app)
    cfg = load_config_dict(base_config_dict(tmp_path), base_dir=tmp_path)
    assert Path(cfg.tools.ffmpeg) == (app / "ffmpeg" / paths.tool_filename("ffmpeg")).resolve()
    assert Path(cfg.tools.ffprobe) == (app / "ffmpeg" / paths.tool_filename("ffprobe")).resolve()


def test_configured_tools_win_over_bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    _bundle(app, "ffmpeg", "ffprobe")
    _freeze(monkeypatch, app)
    d = base_config_dict(tmp_path, tools={"ffmpeg": "C:/custom/ffmpeg.exe"})
    cfg = load_config_dict(d, base_dir=tmp_path)
    assert cfg.tools.ffmpeg == "C:/custom/ffmpeg.exe"
    assert Path(cfg.tools.ffprobe).parent == (app / "ffmpeg").resolve()   # the other one still bundled


def test_frozen_without_bundle_falls_back_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch, tmp_path / "app")
    cfg = load_config_dict(base_config_dict(tmp_path), base_dir=tmp_path)
    assert (cfg.tools.ffmpeg, cfg.tools.ffprobe) == ("ffmpeg", "ffprobe")


def test_unknown_tools_key_still_rejected(tmp_path: Path) -> None:
    from sentinel.config import ConfigError
    with pytest.raises(ConfigError, match="unknown key"):
        load_config_dict(base_config_dict(tmp_path, tools={"ffplay": "x"}), base_dir=tmp_path)


def test_windows_layout_uses_programdata() -> None:
    lay = paths.default_layout("win32", env={"ProgramData": r"D:\PD"})
    assert lay.scope == "machine"
    assert str(PureWindowsPath(lay.config_file)) == r"D:\PD\Sentinel\sentinel.toml"
    assert str(PureWindowsPath(lay.credentials_file)) == r"D:\PD\Sentinel\camera.secret"
    assert str(PureWindowsPath(lay.spool_dir)) == r"D:\PD\Sentinel\spool"
    assert str(PureWindowsPath(lay.log_dir)) == r"D:\PD\Sentinel\logs"
    assert str(PureWindowsPath(paths.default_layout("win32", env={}).data_dir)) == r"C:\ProgramData\Sentinel\data"


def test_linux_layouts() -> None:
    root = paths.default_layout("linux", env={}, is_root=True)
    assert root.config_file.as_posix() == "/etc/sentinel/sentinel.toml"
    assert root.spool_dir.as_posix() == "/var/lib/sentinel/spool"
    assert root.log_dir.as_posix() == "/var/log/sentinel"
    user = paths.default_layout("linux", env={}, is_root=False, home="/home/op")
    assert user.scope == "user"
    assert user.config_file.as_posix() == "/home/op/.config/sentinel/sentinel.toml"
    assert user.data_dir.as_posix() == "/home/op/.local/share/sentinel/data"
    xdg = paths.default_layout("linux", env={"XDG_CONFIG_HOME": "/c", "XDG_DATA_HOME": "/d"}, is_root=False,
                               home="/home/op")
    assert xdg.config_file.as_posix() == "/c/sentinel/sentinel.toml"
    assert xdg.spool_dir.as_posix() == "/d/sentinel/spool"


def test_storage_defaults_when_omitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ProgramData", str(tmp_path / "pd"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(paths, "_is_root", lambda: False)
    d = base_config_dict(tmp_path)
    del d["storage"]
    cfg = load_config_dict(d, base_dir=tmp_path)
    assert Path(cfg.storage.spool_dir).is_relative_to(tmp_path)
    assert Path(cfg.storage.spool_dir).name == "spool" and Path(cfg.storage.data_dir).name == "data"
    d2 = base_config_dict(tmp_path)
    d2["storage"] = {"spool_dir": str(tmp_path / "big" / "spool")}   # partial: the rest defaulted
    cfg2 = load_config_dict(d2, base_dir=tmp_path)
    assert Path(cfg2.storage.spool_dir) == tmp_path / "big" / "spool"
    assert Path(cfg2.storage.log_dir).is_relative_to(tmp_path)


def test_camera_section_still_required(tmp_path: Path) -> None:
    from sentinel.config import ConfigError
    d = base_config_dict(tmp_path)
    del d["camera"]
    with pytest.raises(ConfigError, match=r"\[camera\]"):
        load_config_dict(d, base_dir=tmp_path)


def test_config_lookup_order(tmp_path: Path) -> None:
    app, pd = tmp_path / "app", tmp_path / "pd"
    env = {"ProgramData": str(pd)}
    machine = pd / "Sentinel" / "sentinel.toml"
    assert paths.find_config(env=env, app=app, platform="win32") is None            # first run
    machine.parent.mkdir(parents=True)
    machine.write_text("")
    assert paths.find_config(env=env, app=app, platform="win32") == machine
    app.mkdir()
    (app / "sentinel.toml").write_text("")                                          # portable folder wins
    assert paths.find_config(env=env, app=app, platform="win32") == app / "sentinel.toml"
    named = tmp_path / "named.toml"
    assert paths.find_config(env={**env, paths.CONFIG_ENV: str(named)}, app=app, platform="win32") == named
    assert paths.find_config(explicit=tmp_path / "x.toml", env=env, app=app, platform="win32") == tmp_path / "x.toml"


def test_linux_lookup_prefers_system_then_user() -> None:
    cands = paths.config_candidates(env={"XDG_CONFIG_HOME": "/xdg"}, app=None, platform="linux",
                                    is_root=False, home="/home/op")
    assert [c.as_posix() for c in cands] == ["/etc/sentinel/sentinel.toml", "/xdg/sentinel/sentinel.toml"]
    assert len(paths.config_candidates(env={}, app=None, platform="linux", is_root=True)) == 1
