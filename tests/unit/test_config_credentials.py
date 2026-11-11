from __future__ import annotations

import logging
from pathlib import Path

import pytest

from conftest import base_config_dict, make_config
from sentinel.config import ConfigError, load_config, load_config_dict
from sentinel.credentials import (CredentialError, Credentials, load_credentials, redact, register_secret,
                                  url_with_credentials)
from sentinel.logging_setup import RedactingFilter, RedactingFormatter


def test_minimal_config_defaults(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert cfg.camera.include_audio is False          # audio excluded unless explicitly enabled
    assert cfg.recording.content_hash == "deferred"
    assert Path(cfg.storage.spool_dir).is_absolute()


@pytest.mark.parametrize("section,key,value,msg", [
    ("camera", "colour", "red", "unknown key"),
    ("camera", "url", "rtsp://admin:secret@192.0.2.1/x", "must not contain credentials"),
    ("camera", "url", "ftp://192.0.2.1/x", "scheme"),
    ("camera", "id", "bad id!", "id must be"),
    ("recording", "segment_seconds", 1, "between 2 and 3600"),
    ("recording", "display_timezone", "Mars/Olympus", "display_timezone"),
    ("recording", "validation", "vibes", "validation"),
    ("recording", "stall_timeout_s", 0, "positive"),
    ("recording", "align_to_clock", "yes", "true or false"),
    ("storage", "critical_free_gb", 100.0, "critical_free_gb"),
])
def test_invalid_config_rejected(tmp_path: Path, section: str, key: str, value: object, msg: str) -> None:
    d = base_config_dict(tmp_path)
    d[section][key] = value
    with pytest.raises(ConfigError, match=msg):
        load_config_dict(d, base_dir=tmp_path)


def test_frame_hashes_require_content_hash(tmp_path: Path) -> None:
    d = base_config_dict(tmp_path, recording={"content_hash": "none", "frame_hashes": True})
    with pytest.raises(ConfigError, match="frame_hashes"):
        load_config_dict(d, base_dir=tmp_path)


def test_unknown_section_rejected(tmp_path: Path) -> None:
    d = base_config_dict(tmp_path)
    d["cloud"] = {"upload": True}
    with pytest.raises(ConfigError, match="unknown section"):
        load_config_dict(d, base_dir=tmp_path)


def test_example_config_parses() -> None:
    example = Path(__file__).resolve().parents[2] / "config" / "sentinel.example.toml"
    cfg = load_config(example)
    assert cfg.recording.segment_seconds == 3600          # one file per clock hour (0.3.5)
    assert "@" not in cfg.camera.url


def test_url_with_credentials_escapes_reserved_characters() -> None:
    url = url_with_credentials("rtsp://192.0.2.10:554/stream", Credentials("view er", "p@ss:w/rd"))
    assert url == "rtsp://view%20er:p%40ss%3Aw%2Frd@192.0.2.10:554/stream"
    assert url_with_credentials("rtsp://192.0.2.10/s", None) == "rtsp://192.0.2.10/s"


def test_redact_removes_userinfo_and_registered_secrets() -> None:
    register_secret("Sup3rS3cret!")
    text = "Error opening rtsp://viewer:Sup3rS3cret%21@192.0.2.10/s; password Sup3rS3cret! rejected"
    out = redact(text)
    assert "Sup3rS3cret" not in out and "viewer" not in out
    assert "rtsp://***:***@192.0.2.10/s" in out


def test_credentials_repr_hides_values(tmp_path: Path) -> None:
    f = tmp_path / "cam.secret"
    f.write_text('username = "viewer"\npassword = "hunter22"\n')
    c = load_credentials(f)
    assert "hunter22" not in repr(c) and "viewer" not in repr(c)


@pytest.mark.parametrize("content", ['username = "x"\n', 'username = "x"\npassword = "y"\nextra = 1\n', "not toml ["])
def test_bad_credentials_file(tmp_path: Path, content: str) -> None:
    f = tmp_path / "cam.secret"
    f.write_text(content)
    with pytest.raises(CredentialError):
        load_credentials(f)


def test_logging_filter_and_formatter_redact(capsys: pytest.CaptureFixture[str]) -> None:
    logger = logging.getLogger("test.redact")
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error("connect %s failed", "rtsp://u:pw123@192.0.2.1/x")
        try:
            raise RuntimeError("rtsp://u:pw123@192.0.2.1/x unreachable")
        except RuntimeError:
            logger.exception("traceback test")
    finally:
        logger.removeHandler(handler)
    err = capsys.readouterr().err
    assert "pw123" not in err
    assert err.count("***:***@") >= 2
