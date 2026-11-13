"""Launcher: first-run setup, background-process control, stop channel, status view, CLI."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from sentinel import paths
from sentinel.alerts import CATALOG
from sentinel.config import load_config
from sentinel.launcher import cli, control, setup, stopsignal, view
from sentinel.platform import get_platform


def _layout(tmp: Path) -> paths.Layout:
    root = tmp / "machine"
    return paths.Layout("machine", root / "sentinel.toml", root / "camera.secret", root / "spool",
                        root / "data", root / "logs")


# ---------------------------------------------------------------------- setup
def test_toml_writer_round_trips_awkward_strings() -> None:
    d = {"a": {"win": "C:\\ProgramData\\Sentinel\\spool", "q": 'say "hi"', "uni": "Câmera ü",
               "ctl": "tab\there\nline", "n": 5, "f": 2.5, "b": True}}
    assert tomllib.loads(setup.to_toml(d, "header\nline two")) == d


def test_build_config_is_valid_and_has_no_credentials(tmp_path: Path) -> None:
    lay = _layout(tmp_path)
    ans = setup.SetupAnswers("192.0.2.10", "/Streaming/Channels/101", username="viewer", password="pw!",
                             camera_name="Front", timezone="America/New_York")
    d = setup.build_config(ans, lay)
    assert d["camera"]["url"] == "rtsp://192.0.2.10:554/Streaming/Channels/101"
    assert d["camera"]["include_audio"] is False
    assert "pw!" not in json.dumps(d) and "viewer" not in json.dumps(d)
    assert d["storage"] == lay.storage_defaults()


def test_ipv6_host_bracketed() -> None:
    assert setup.camera_url(setup.SetupAnswers("2001:db8::5", "s")) == "rtsp://[2001:db8::5]:554/s"


@pytest.mark.parametrize("host", ["", "rtsp://192.0.2.1", "admin:pw@192.0.2.1", "cam era", "192.0.2.1/path"])
def test_bad_host_rejected(host: str) -> None:
    with pytest.raises(setup.SetupError):
        setup.check_host(host)


@pytest.mark.parametrize("p", ["", "/", "a b", "x@y", "rtsp://h/x"])
def test_bad_stream_path_rejected(p: str) -> None:
    with pytest.raises(setup.SetupError):
        setup.check_stream_path(p)


def test_bad_timezone_and_port_rejected(tmp_path: Path) -> None:
    with pytest.raises(setup.SetupError, match="time zone"):
        setup.check_timezone("Mars/Base")
    with pytest.raises(setup.SetupError, match="port"):
        setup.build_config(setup.SetupAnswers("192.0.2.1", "s", port=70000), _layout(tmp_path))


def _private(path: Path) -> bool:
    """Only this account, SYSTEM, Administrators (Windows, protected DACL) / no group-other write."""
    if sys.platform == "win32":
        from sentinel.platform import winacl
        sddl = winacl.get_sddl(path)
        return sddl.startswith("D:P") and not winacl.foreign_trustees(sddl, winacl.current_user_sid())
    return path.stat().st_mode & 0o077 == 0 if path.is_file() else path.stat().st_mode & 0o027 == 0


def test_write_setup_creates_config_and_private_secret(tmp_path: Path) -> None:
    lay = _layout(tmp_path)
    ans = setup.SetupAnswers("192.0.2.10", "live", username="viewer", password="Pa\"ss\\w0rd")
    path, warnings = setup.write_setup(ans, lay)
    assert warnings == []
    cfg = load_config(path)
    assert cfg.camera.url == "rtsp://192.0.2.10:554/live"
    secret = Path(cfg.camera.credentials_file or "")
    assert secret == lay.config_file.parent / "camera.secret"
    from sentinel.credentials import load_credentials
    creds = load_credentials(secret)
    assert (creds.username, creds.password) == ("viewer", "Pa\"ss\\w0rd")
    text = path.read_text("utf-8")
    assert "w0rd" not in text and "viewer" not in text
    assert _private(secret), "credentials file must be private"
    for folder in (lay.config_file.parent, lay.spool_dir, lay.data_dir, lay.log_dir):
        assert folder.is_dir() and _private(folder), folder
    assert not list(secret.parent.glob("camera.secret.*.partial"))
    from sentinel.install_check import location_problems
    assert location_problems(path, cfg) == []


def test_secret_never_left_behind_when_it_cannot_be_made_private(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    lay = _layout(tmp_path)
    monkeypatch.setattr(setup, "secret_problems", lambda _p: ["also accessible to BU"])
    with pytest.raises(setup.SetupError, match="could not be made private"):
        setup.write_setup(setup.SetupAnswers("192.0.2.10", "live", username="viewer", password="pw12345"), lay)
    assert not list(lay.config_file.parent.glob("camera.secret*")), "plaintext left behind"
    assert not lay.config_file.exists(), "no configuration may point at a missing secret"


def test_secret_creation_is_private_from_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file must already be private when the first byte is written (not fixed up afterwards)."""
    seen: list[bool] = []
    if sys.platform == "win32":
        from sentinel.platform import winacl
        real = winacl.write_private_file

        def spy(path: Path, data: bytes, user_sid: str | None = None) -> None:
            real(path, data, user_sid)
            seen.append(_private(path))
        monkeypatch.setattr(winacl, "write_private_file", spy)
    else:
        real_open = os.open

        def spy_open(p: Any, flags: int, mode: int = 0o777, *a: Any, **k: Any) -> int:
            if str(p).endswith(".partial"):
                seen.append(mode == 0o600 and bool(flags & os.O_EXCL))
            return real_open(p, flags, mode, *a, **k)
        monkeypatch.setattr(os, "open", spy_open)
    lay = _layout(tmp_path)
    lay.config_file.parent.mkdir(parents=True)
    setup.write_secret(lay.credentials_file, "viewer", "pw12345")
    assert seen == [True]


def test_setup_refuses_to_reprotect_a_foreign_shared_folder(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "someone-elses.docx").write_text("x")
    _make_broadly_writable(shared)
    with pytest.raises(setup.SetupError, match="already contains other files"):
        setup.prepare_private_dir(shared)
    own = tmp_path / "old-install"
    own.mkdir()
    (own / "sentinel.toml").write_text("")
    _make_broadly_writable(own)
    setup.prepare_private_dir(own)                    # a previous Sentinel folder is re-protected
    assert _private(own)


def _make_broadly_writable(path: Path) -> None:
    if sys.platform == "win32":
        from sentinel.platform import winacl
        winacl.set_dacl(path, f"D:P(A;OICI;FA;;;{winacl.current_user_sid()})(A;OICI;0x1301bf;;;BU)")
    else:
        os.chmod(path, 0o777)


def test_install_check_flags_broadly_writable_locations(tmp_path: Path) -> None:
    from sentinel.install_check import location_problems, nearest_existing
    cfg_path = tmp_path / "conf" / "sentinel.toml"
    cfg_path.parent.mkdir()
    cfg_path.write_text("")
    cfg = make_config(tmp_path / "store")
    assert nearest_existing(tmp_path / "store" / "spool" / "x") == tmp_path
    assert location_problems(cfg_path, cfg) == []
    _make_broadly_writable(cfg_path.parent)
    problems = location_problems(cfg_path, cfg)
    assert any("configuration folder" in p for p in problems), problems
    assert any(("Users" in p) or ("all users" in p) for p in problems), problems


def test_launcher_refuses_insecure_configuration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from conftest import base_config_dict
    conf = tmp_path / "conf"
    conf.mkdir()
    cfg_file = conf / "sentinel.toml"
    cfg_file.write_text(setup.to_toml(base_config_dict(tmp_path / "store")), encoding="utf-8")
    _make_broadly_writable(conf)
    rc = cli.main(["launch", "--once", "--config", str(cfg_file)])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_CONFIG and "Error C-001" in out
    assert not (tmp_path / "store" / "data" / "launcher.lock").exists(), "nothing may be started"

def test_write_setup_never_silently_overwrites(tmp_path: Path) -> None:
    lay = _layout(tmp_path)
    setup.write_setup(setup.SetupAnswers("192.0.2.10", "a"), lay)
    with pytest.raises(setup.SetupError, match="already set up"):
        setup.write_setup(setup.SetupAnswers("192.0.2.11", "b"), lay)
    path, _ = setup.write_setup(setup.SetupAnswers("192.0.2.11", "b"), lay, force=True, clock=lambda: 0.0)
    assert load_config(path).camera.url.startswith("rtsp://192.0.2.11")
    backup = lay.config_file.with_name("sentinel.toml.19700101T000000Z.bak")
    assert "192.0.2.10" in backup.read_text("utf-8")


def test_write_setup_rejects_invalid_answers_before_touching_files(tmp_path: Path) -> None:
    lay = _layout(tmp_path)
    with pytest.raises(setup.SetupError):
        setup.write_setup(setup.SetupAnswers("192.0.2.10", "a", timezone="Nowhere/Land"), lay)
    assert not lay.config_file.parent.exists()


def test_interactive_setup_reprompts_and_uses_defaults(tmp_path: Path) -> None:
    lay = _layout(tmp_path)
    answers = iter(["rtsp://bad", "192.0.2.10", "/live/main", "", "viewer", "", "Mars/X", "", ""])
    said: list[str] = []
    ans = setup.interactive_setup(lay, ask=lambda _p: next(answers), ask_secret=lambda _p: "secret",
                                  out=said.append)
    assert (ans.camera_host, ans.stream_path, ans.port, ans.username, ans.password) == \
        ("192.0.2.10", "/live/main", 554, "viewer", "secret")
    assert ans.camera_name == "Camera 1" and ans.timezone == "UTC" and ans.storage_root is None
    assert any("IP address or host name" in s for s in said)       # bad host explained, asked again
    assert any("time zone" in s for s in said)


def test_simulated_camera_setup_is_marked_development(tmp_path: Path) -> None:
    d = setup.build_config(setup.SetupAnswers("", "", simulated_url="tcp://127.0.0.1:8554"), _layout(tmp_path))
    assert d["camera"]["development_source"] is True and d["camera"]["input_format"] == "mpegts"


def test_new_alert_codes_are_plain_language() -> None:
    for code in ("C-002", "C-003"):
        d = CATALOG[code]
        assert not any(w in (d.headline + d.action).lower() for w in ("rtsp", "ffmpeg", "config", "\\", "/"))
    banner = view.alert_banner("C-002")
    assert "Error C-002" in banner and "Recording stopped" not in banner


# ---------------------------------------------------------------------- control
def test_recorder_command_frozen_and_source() -> None:
    cfgp = Path("C:/x/sentinel.toml")
    assert control.recorder_command(cfgp, frozen=True, executable="S.exe") == \
        ["S.exe", "recorder", "run", "--config", str(cfgp)]
    assert control.recorder_command(cfgp, frozen=False, executable="py")[:3] == ["py", "-m", "sentinel.launcher"]


def test_detached_kwargs() -> None:
    win = control.detached_popen_kwargs(platform="win32")
    assert win["cwd"].lower().endswith("system32")                # never a user-writable folder
    assert win["creationflags"] == control.CREATE_NO_WINDOW | control.CREATE_NEW_PROCESS_GROUP | \
        control.CREATE_BREAKAWAY_FROM_JOB
    assert win["stdin"] is subprocess.DEVNULL and "start_new_session" not in win
    assert control.detached_popen_kwargs(platform="win32", breakaway=False)["creationflags"] & \
        control.CREATE_BREAKAWAY_FROM_JOB == 0
    posix = control.detached_popen_kwargs(platform="linux")
    assert posix["start_new_session"] is True and "creationflags" not in posix and Path(posix["cwd"]).as_posix() == "/"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class FakeProc:
    def __init__(self, rc_after: tuple[float, int] | None, clock: FakeClock) -> None:
        self.rc_after, self.clock, self.pid = rc_after, clock, 4242
        self.born = clock.t

    def poll(self) -> int | None:
        if self.rc_after and self.clock.t - self.born >= self.rc_after[0]:
            return self.rc_after[1]
        return None


def _ctl(tmp: Path, popen: Any, clock: FakeClock) -> control.DetachedProcessControl:
    cfg = make_config(tmp)
    return control.DetachedProcessControl(tmp / "sentinel.toml", cfg, popen=popen, clock=clock,
                                          sleep=clock.sleep, command=["recorder"])


@pytest.mark.parametrize("rc,ok,code", [(3, True, None), (2, False, "C-001"), (1, False, "C-003")])
def test_start_reports_early_exit(tmp_path: Path, rc: int, ok: bool, code: str | None) -> None:
    clock = FakeClock()
    ctl = _ctl(tmp_path, lambda *a, **k: FakeProc((1.0, rc), clock), clock)
    res = ctl.start()
    assert (res.ok, res.code) == (ok, code)


def test_start_confirmed_by_fresh_heartbeat_only(tmp_path: Path) -> None:
    clock = FakeClock()
    ctl = _ctl(tmp_path, None, clock)
    status = ctl.status_path
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({"heartbeat_epoch": 10.0}))           # stale file from an earlier run

    ctl._popen = lambda *_a, **_k: FakeProc(None, clock)
    res = ctl.start(confirm_s=3.0)
    assert res.ok and "not yet confirmed" in res.detail                   # stale heartbeat ignored
    status.write_text(json.dumps({"heartbeat_epoch": clock.t + 0.5}))
    res2 = ctl.start(confirm_s=3.0)
    assert res2.ok and "confirmed by status heartbeat" in res2.detail


def test_spawn_failure_and_breakaway_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("no such program")

    assert _ctl(tmp_path, boom, clock).start() == control.StartResult(False, "C-003", "spawn failed: no such program")
    if sys.platform != "win32":
        return
    calls: list[int] = []

    def picky(*_a: Any, **k: Any) -> FakeProc:
        calls.append(k["creationflags"])
        if k["creationflags"] & control.CREATE_BREAKAWAY_FROM_JOB:
            err = OSError("denied")
            err.winerror = 5  # type: ignore[attr-defined]
            raise err
        return FakeProc((0.0, 3), clock)

    assert _ctl(tmp_path, picky, clock).start().ok
    assert len(calls) == 2 and not calls[1] & control.CREATE_BREAKAWAY_FROM_JOB


def test_is_running_never_touches_the_instance_lock(tmp_path: Path) -> None:
    clock = FakeClock()
    ctl = _ctl(tmp_path, None, clock)
    assert ctl.is_running() is False
    held = get_platform().acquire_instance_lock(ctl.lock_path)   # a recorder that is just starting
    try:
        ctl.is_running()                                         # must not fail or steal the lock
        with pytest.raises(Exception):
            get_platform().acquire_instance_lock(ctl.lock_path).close()
    finally:
        held.close()
    if sys.platform == "win32":
        listener = stopsignal.StopListener(ctl.cfg.data_path, lambda: None).start()
        try:
            assert ctl.is_running() is True
        finally:
            listener.close()
    else:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "recorder", "run"])
        try:
            ctl.lock_path.write_text(str(proc.pid))
            assert ctl.is_running() is True
        finally:
            proc.kill()
            proc.wait()
        assert ctl.is_running() is False                          # stale PID is not "running"
    assert ctl.is_running() is False

def test_providers_take_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    sentinel_ctl = object()
    monkeypatch.setattr(control, "CONTROL_PROVIDERS", [lambda _p, _c: None, lambda _p, _c: sentinel_ctl])
    assert control.select_control(tmp_path / "x.toml", cfg) is sentinel_ctl
    monkeypatch.setattr(control, "CONTROL_PROVIDERS", [])
    assert isinstance(control.select_control(tmp_path / "x.toml", cfg), control.DetachedProcessControl)


# ---------------------------------------------------------------------- stop channel
def test_event_name_is_per_data_folder(tmp_path: Path) -> None:
    a, b = stopsignal.event_name(tmp_path / "a"), stopsignal.event_name(tmp_path / "b")
    assert a != b and a.startswith("Local\\SentinelRecorderStop-")
    assert stopsignal.event_name(tmp_path / "a") == a


@pytest.mark.skipif(sys.platform != "win32", reason="named events are the Windows stop channel")
def test_stop_channel_round_trip(tmp_path: Path) -> None:
    assert stopsignal.request_stop(tmp_path) is False                     # nobody listening
    fired = threading.Event()
    listener = stopsignal.StopListener(tmp_path, fired.set).start()
    try:
        assert stopsignal.request_stop(tmp_path / "other") is False      # different installation
        assert stopsignal.request_stop(tmp_path) is True
        assert fired.wait(5.0)
    finally:
        listener.close()
    assert stopsignal.request_stop(tmp_path) is False                     # never left armed


# ---------------------------------------------------------------------- view
def _status(path: Path, overall: str, epoch: float, alert: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"overall": overall, "heartbeat_epoch": epoch, "alert": alert}))


def test_view_missing_status_is_not_responding(tmp_path: Path) -> None:
    text, healthy = view.ConsoleStatusView().render(tmp_path / "status.json", "Front")
    assert "RECORDER NOT RESPONDING" in text and "X-001" in text and not healthy
    assert view.CLOSE_NOTE not in text


def test_view_recording_and_startup_grace(tmp_path: Path) -> None:
    st = tmp_path / "status.json"
    now = time.time()
    v = view.ConsoleStatusView(clock=lambda: now)
    _status(st, "RECORDING", now - 100)                                  # stale, but we just started it
    text, healthy = v.render(st, "Front", started_after=now - 2)
    assert "STARTING" in text and not healthy
    _status(st, "RECORDING", now)
    text, healthy = v.render(st, "Front", started_after=now - 2)
    assert "RECORDING NORMALLY" in text and healthy and view.CLOSE_NOTE in text and "Camera: Front" in text
    _status(st, "RECORDING", now - 100)
    late = view.ConsoleStatusView(clock=lambda: now + 60)
    text, healthy = late.render(st, "Front", started_after=now - 2)     # grace over: say so honestly
    assert "RECORDER NOT RESPONDING" in text and not healthy


def test_a_start_problem_gives_way_to_the_status_once_the_recorder_runs_again(tmp_path: Path) -> None:
    """Review 2026-11-12: 'Error C-003' stayed on screen for good, also after Windows restarted the recorder."""
    st = tmp_path / "status.json"
    t0 = time.time()
    now = [t0 - 10]
    v = view.ConsoleStatusView(clock=lambda: now[0])
    _status(st, "RECORDING", t0 - 20)                                     # from before the problem
    text, healthy = v.render(st, "Front", start_problem="C-003")
    assert "Error C-003" in text and not healthy and "Checked at" in text
    now[0] = t0
    _status(st, "RECORDING", t0 - 1)                                      # the service runs again
    text, healthy = v.render(st, "Front", start_problem="C-003")
    assert "RECORDING NORMALLY" in text and healthy and "C-003" not in text


def test_service_managed_launch_opens_the_window_when_the_start_is_refused(tmp_path: Path, monkeypatch) -> None:
    """Review 2026-11-12: a standard user opening Sentinel while the service is stopped (Windows restarts it after a
    failure) got a frozen console 'Error C-003' instead of the window showing what the service wrote."""
    from sentinel.launcher.control import StartResult

    cfg_path = tmp_path / "sentinel.toml"
    cfg_path.write_text('[camera]\nid = "CAM01"\nname = "Cam"\nurl = "rtsp://192.0.2.10:554/live"\n'
                        f"[storage]\nspool_dir = '{tmp_path / 'spool'}'\ndata_dir = '{tmp_path / 'data'}'\n"
                        f"log_dir = '{tmp_path / 'logs'}'\nstatus_dir = '{tmp_path / 'status'}'\n", encoding="utf-8")
    archive: list[bool] = []

    class Svc:
        name, service_managed = "Windows service", True

        def is_running(self) -> bool:
            return False

        def start(self) -> StartResult:
            return StartResult(False, "C-003", "access denied")

        def start_archive(self) -> bool:
            archive.append(True)
            return False

    seen: dict = {}

    class View:
        def run(self, status_path: Path, camera_name: str, **kw) -> int:
            seen.update(kw)
            return 0

    monkeypatch.setattr(cli, "select_control", lambda p, c: Svc())
    monkeypatch.setattr(cli, "select_view", lambda *_a: View())
    assert cli.cmd_launch(__import__("argparse").Namespace(config=str(cfg_path), once=False, no_start=False)) == 0
    assert seen["start_problem"] is None and seen["started_after"] is None and archive == [True]


def test_view_start_problem_and_once_exit_code(tmp_path: Path) -> None:
    buf = io.StringIO()
    rc = view.ConsoleStatusView(out=buf).run(tmp_path / "status.json", "Front", once=True, start_problem="C-003")
    assert rc == 4 and "Error C-003" in buf.getvalue() and "RECORDING STOPPED" in buf.getvalue()
    _status(tmp_path / "status.json", "RECORDING", time.time())
    assert view.ConsoleStatusView(out=io.StringIO()).run(tmp_path / "status.json", "Front", once=True) == 0


# ---------------------------------------------------------------------- CLI
def test_cli_not_set_up_shows_c002(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["launch", "--once", "--config", str(tmp_path / "missing.toml")])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_CONFIG and "Error C-002" in out
    assert str(tmp_path) not in out                                       # no paths on operator screens
    assert cli.main(["status", "--config", str(tmp_path / "missing.toml")]) == cli.EXIT_CONFIG


def test_cli_invalid_config_shows_c001_without_detail(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ProgramData", str(tmp_path / "pd"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    bad = tmp_path / "sentinel.toml"
    bad.write_text('[camera]\nid = "CAM01"\nurl = "ftp://192.0.2.1/x"\n', encoding="utf-8")
    rc = cli.main(["launch", "--once", "--config", str(bad)])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_CONFIG and "Error C-001" in out and "ftp" not in out


def test_cli_about_runs(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["about"]) == 0
    assert "Sentinel" in capsys.readouterr().out


def test_manifest_verification(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"abc")
    from sentinel.hashing import sha256_file
    m = {"files": {"a.bin": sha256_file(tmp_path / "a.bin")[0], "gone.bin": "0" * 64}}
    assert cli.verify_manifest(tmp_path, m) == ["gone.bin: missing"]
    (tmp_path / "a.bin").write_bytes(b"abd")
    assert "a.bin: changed" in cli.verify_manifest(tmp_path, m)
