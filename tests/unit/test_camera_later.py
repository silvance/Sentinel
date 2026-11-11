"""Set up in advance without the camera ("setup --camera-later"), add it on site ("set-camera").

Owner request 2026-11-10: complete the setup before the camera's address is known. Until the
camera is added the recorder records nothing, contacts nothing, and says so plainly (P-002,
never an outage alarm). set-camera replaces only the [camera] section and restarts the recorder.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from gui_support import base_measurements
from sentinel.alerts import CATALOG, Severity
from sentinel.config import ConfigError, load_config, load_config_dict
from sentinel.launcher import cli, setcamera, setup
from sentinel.models import OverallStatus
from sentinel.paths import Layout
from sentinel.status import evaluate


def _layout(tmp: Path) -> Layout:
    root = tmp / "machine"
    return Layout("machine", root / "config" / "sentinel.toml", root / "config" / "camera.secret", root / "spool",
                  root / "data", root / "logs")


def _pending_config(tmp: Path) -> Path:
    lay = _layout(tmp)
    path, _ = setup.write_setup(setup.SetupAnswers("", "", camera_later=True, camera_name="Front door",
                                                   timezone="America/Chicago"), lay)
    return path


# ---------------------------------------------------------------------- configuration
def test_camera_later_configuration_is_valid_and_has_no_address(tmp_path: Path) -> None:
    path = _pending_config(tmp_path)
    cfg = load_config(path)
    assert cfg.camera.pending and cfg.camera.url == "" and cfg.camera.credentials_file is None
    assert cfg.camera.name == "Front door" and cfg.recording.display_timezone == "America/Chicago"
    assert cfg.archive.enabled and cfg.retention.enabled            # everything else is ready
    assert "set-camera" in path.read_text(encoding="utf-8")          # header tells the technician


def test_pending_and_address_rules(tmp_path: Path) -> None:
    base = {"storage": {"spool_dir": "s", "data_dir": "d", "log_dir": "l"}}
    with pytest.raises(ConfigError, match="pending"):
        load_config_dict({"camera": {"id": "C", "url": "rtsp://192.0.2.1/x", "pending": True}, **base},
                         base_dir=tmp_path)
    with pytest.raises(ConfigError, match="url is required"):
        load_config_dict({"camera": {"id": "C", "url": ""}, **base}, base_dir=tmp_path)


def test_camera_later_cannot_carry_an_account() -> None:
    with pytest.raises(setup.SetupError):
        setup.build_config(setup.SetupAnswers("", "", username="u", camera_later=True), _layout(Path("x")))


def test_interactive_camera_later_asks_no_camera_questions(tmp_path: Path) -> None:
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return ""

    ans = setup.interactive_setup(_layout(tmp_path), ask=ask, ask_secret=ask, out=lambda _s: None,
                                  camera_later=True)
    assert ans.camera_later and not any("IP address" in q or "stream" in q or "password" in q for q in asked)


def test_double_click_setup_accepts_later_for_the_camera(tmp_path: Path) -> None:
    """Owner report 2026-11-10: double-click SETUP could not be finished without a camera."""
    answers = iter(["later", "Front door", "America/Chicago", ""])
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return next(answers)

    said: list[str] = []
    ans = setup.interactive_setup(_layout(tmp_path), ask=ask, ask_secret=ask, out=said.append)
    assert ans.camera_later and ans.camera_host == "" and ans.username == ""
    assert "LATER" in said[0] and "(or LATER)" in asked[0]
    assert not any("stream path" in q or "password" in q for q in asked)
    path, _ = setup.write_setup(ans, _layout(tmp_path))
    cfg = load_config(path)
    assert cfg.camera.pending and cfg.camera.name == "Front door"


# ---------------------------------------------------------------------- status
def test_status_is_p002_not_an_outage() -> None:
    m = base_measurements(ffmpeg_running=False, media_seen_this_run=False, seconds_since_media_advance=None,
                          camera_pending=True, outage_seconds=86_400.0, attempts=0)
    d = evaluate(m, stall_timeout_s=6, segment_seconds=300, connect_timeout_s=10, escalate_after_s=180)
    assert d.overall is OverallStatus.CAMERA_NOT_SET_UP
    assert [a.code for a in d.alerts] == ["P-002"]
    a = d.alerts[0]
    assert a.severity == Severity.NOTICE and "Error" not in a.message
    assert a.message.startswith("Not recording: the camera has not been added yet (P-002).")
    assert CATALOG["P-002"].stops_recording is False


def test_launch_once_is_not_healthy_without_a_camera() -> None:
    from sentinel.launcher.view import HEALTHY

    assert "CAMERA_NOT_SET_UP" not in HEALTHY


# ---------------------------------------------------------------------- set-camera: text edit
def test_replace_camera_section_keeps_everything_else() -> None:
    text = ("# header comment\n\n[camera]\nid = \"CAM01\"\nname = \"Front\"\nurl = \"\"\npending = true\n\n"
            "# schedule comment\n[schedule]\nenabled = true\n\n[[schedule.weekly]]\naction = \"pause\"\n"
            "days = [\"weekend\"]\nstart = \"00:00\"\nend = \"24:00\"\n")
    out = setcamera.replace_camera_section(text, {"id": "CAM01", "name": "Front", "url": "rtsp://192.0.2.5:554/s"})
    d = tomllib.loads(out)
    assert d["camera"] == {"id": "CAM01", "name": "Front", "url": "rtsp://192.0.2.5:554/s"}
    assert d["schedule"]["weekly"][0]["days"] == ["weekend"]
    assert "# header comment" in out and "# schedule comment" in out


def test_new_camera_table_drops_pending_and_keeps_settings() -> None:
    t = setcamera.new_camera_table({"id": "CAM07", "name": "x", "url": "", "pending": True, "transport": "udp",
                                    "include_audio": True}, "rtsp://h:554/p", Path("C:/c/camera.secret"), "Gate")
    assert t == {"id": "CAM07", "name": "Gate", "url": "rtsp://h:554/p", "transport": "udp", "include_audio": True,
                 "credentials_file": str(Path("C:/c/camera.secret"))}


def test_ffprobe_errors_never_show_credentials() -> None:
    class CP:
        returncode, stdout = 1, ""
        stderr = "rtsp://admin:hunter2@192.0.2.5:554/s: Server returned 401 Unauthorized (authorization failed)"

    ok, msg = setcamera.test_camera("ffprobe", "rtsp://192.0.2.5:554/s",
                                    setcamera.Credentials("admin", "hunter2"), run=lambda *_a, **_k: CP())
    assert not ok and "hunter2" not in msg and "admin" not in msg and "password" in msg


# ---------------------------------------------------------------------- set-camera: end to end
class FakeControl:
    service_managed = False

    def __init__(self, running: bool) -> None:
        self.running, self.calls = running, []

    def is_running(self) -> bool:
        return self.running

    def stop(self, _t: float) -> bool:
        self.calls.append("stop")
        self.running = False
        return True

    def start(self) -> Any:
        self.calls.append("start")
        self.running = True
        return type("R", (), {"ok": True, "code": None})()


def _answers(*values: str):  # noqa: ANN202
    it = iter(values)
    return lambda _prompt: next(it)


def test_set_camera_adds_the_camera_and_restarts_the_recorder(tmp_path: Path) -> None:
    path = _pending_config(tmp_path)
    ctl = FakeControl(running=True)
    seen: list[tuple] = []

    def tester(ffprobe: str, url: str, creds: Any, transport: str) -> tuple[bool, str]:
        seen.append((url, creds.username if creds else None, transport))
        return True, "The camera answered."

    out: list[str] = []
    rc = setcamera.set_camera(path, ask=_answers("192.0.2.44", "Streaming/Channels/101", "", "viewer", ""),
                              ask_secret=lambda _p: "s3cret!", out=out.append, tester=tester,
                              control_for=lambda *_a: ctl, clock=lambda: 1_800_000_000.0)
    assert rc == 0, out
    assert seen == [("rtsp://192.0.2.44:554/Streaming/Channels/101", "viewer", "tcp")]
    cfg = load_config(path)
    assert not cfg.camera.pending and cfg.camera.url == "rtsp://192.0.2.44:554/Streaming/Channels/101"
    assert cfg.camera.name == "Front door" and cfg.retention.enabled
    assert cfg.camera.credentials_file and Path(cfg.camera.credentials_file).exists()
    assert "s3cret!" not in path.read_text(encoding="utf-8") and not any("s3cret!" in o for o in out)
    assert list(path.parent.glob("sentinel.toml.*.bak"))            # previous settings kept
    assert ctl.calls == ["stop", "start"]


def test_set_camera_changes_nothing_when_the_camera_does_not_answer(tmp_path: Path) -> None:
    path = _pending_config(tmp_path)
    before = path.read_text(encoding="utf-8")
    ctl = FakeControl(running=True)
    rc = setcamera.set_camera(path, ask=_answers("192.0.2.44", "s", "", "", "Front door", ""),
                              out=lambda _s: None, tester=lambda *_a: (False, "no answer"),
                              control_for=lambda *_a: ctl)
    assert rc != 0 and path.read_text(encoding="utf-8") == before and ctl.calls == []


def test_set_camera_saves_anyway_only_on_yes(tmp_path: Path) -> None:
    path = _pending_config(tmp_path)
    ctl = FakeControl(running=False)
    rc = setcamera.set_camera(path, ask=_answers("192.0.2.44", "s", "", "", "Front door", "yes"),
                              out=lambda _s: None, tester=lambda *_a: (False, "no answer"),
                              control_for=lambda *_a: ctl)
    assert rc == 0 and not load_config(path).camera.pending and ctl.calls == ["start"]


def test_cli_offers_both_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ap = cli.build_parser()
    assert ap.parse_args(["setup", "--camera-later"]).camera_later is True
    a = ap.parse_args(["set-camera", "--no-test", "--no-restart"])
    assert a.no_test and a.no_restart
    seen: list[tuple] = []
    monkeypatch.setattr(cli.setcamera, "set_camera", lambda p, **kw: seen.append((p, kw)) or 0)
    monkeypatch.setattr(cli, "_pause", lambda: None)
    cfg = make_config(tmp_path)
    cfg_file = tmp_path / "sentinel.toml"
    cfg_file.write_text("", encoding="utf-8")
    assert cfg is not None
    assert cli.main(["set-camera", "--config", str(cfg_file), "--no-test"]) == 0
    assert seen == [(cfg_file, {"test": False, "restart": True})]
