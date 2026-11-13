"""Exact install/uninstall commands for the Windows service (generated, not executed: needs admin)."""

from __future__ import annotations

import subprocess
from pathlib import Path, PureWindowsPath

import pytest

from sentinel.platform import windows as winplat
from sentinel.winsvc import SERVICE_NAME
from sentinel.winsvc.__main__ import EXIT_MEANINGS, execute, main
from sentinel.winsvc.commands import (FAILURE_ACTIONS, InstallInputs, PlanError, Step, check_dedicated_dir,
                                      host_command, install_plan, render, uninstall_plan)

SVC_SID = "S-1-5-80-3978339998-2545952871-631246257-1716324596-627421672"
FORBIDDEN = [r"C:\Windows", r"C:\Program Files", r"C:\ProgramData", r"C:\Users", r"C:\Users\op"]


def _inputs(start: bool = True, creds: bool = True, readers: str = "group") -> InstallInputs:
    host = host_command(Path(r"C:\ProgramData\Sentinel\config\sentinel.toml"), frozen=True,
                        executable=r"C:\Program Files\Sentinel\sentinel-service.exe")
    return InstallInputs(Path(r"C:\ProgramData\Sentinel\config\sentinel.toml"),
                         (Path(r"D:\Sentinel\spool"), Path(r"C:\ProgramData\Sentinel\data"),
                          Path(r"C:\ProgramData\Sentinel\logs")),
                         Path(r"C:\ProgramData\Sentinel\config\camera.dpapi") if creds else None, host, start=start,
                         status_dir=Path(r"C:\ProgramData\Sentinel\status"), status_readers=readers)


def test_service_name_constant_matches_platform_layer() -> None:
    assert winplat.SENTINEL_SERVICE_NAME == SERVICE_NAME == "SentinelRecorder"


def test_frozen_host_command_quotes_executable_and_config() -> None:
    h = host_command(Path(r"C:\ProgramData\Sentinel\config\sentinel.toml"), frozen=True,
                     executable=r"C:\Program Files\Sentinel\sentinel-service.exe")
    assert h.command_line == ('"C:\\Program Files\\Sentinel\\sentinel-service.exe" host --config '
                              'C:\\ProgramData\\Sentinel\\config\\sentinel.toml')
    assert h.environment == ()


def test_venv_host_command_uses_base_interpreter_and_pythonpath() -> None:
    h = host_command(Path(r"C:\ProgramData\Sentinel config\sentinel.toml"), frozen=False,
                     executable=r"C:\Sentinel\venv\Scripts\python.exe",
                     base_executable=r"C:\Program Files\Python312\python.exe",
                     prefix=r"C:\Sentinel\venv", base_prefix=r"C:\Program Files\Python312",
                     package_root=Path(r"C:\Sentinel\src"))
    assert h.command_line == ('"C:\\Program Files\\Python312\\python.exe" -m sentinel.winsvc host --config '
                              '"C:\\ProgramData\\Sentinel config\\sentinel.toml"')
    assert h.interpreter == r"C:\Program Files\Python312\python.exe"
    assert h.environment == (r"PYTHONPATH=C:\Sentinel\src;C:\Sentinel\venv\Lib\site-packages",)


def test_plain_interpreter_host_command_has_no_venv_path() -> None:
    h = host_command(Path(r"C:\Cfg\Sentinel\sentinel.toml"), frozen=False, executable=r"C:\Py\python.exe",
                     base_executable=r"C:\Py\python.exe", prefix=r"C:\Py", base_prefix=r"C:\Py",
                     package_root=Path(r"C:\Program Files\Sentinel\src"))
    assert h.environment == (r"PYTHONPATH=C:\Program Files\Sentinel\src",)


def test_install_plan_exact_commands() -> None:
    steps = install_plan(_inputs(), forbidden=FORBIDDEN)
    argvs = [s.argv for s in steps if s.argv]
    assert argvs[0] == ("sc.exe", "create", "SentinelRecorder", "binPath=",
                        '"C:\\Program Files\\Sentinel\\sentinel-service.exe" host --config '
                        'C:\\ProgramData\\Sentinel\\config\\sentinel.toml',
                        "start=", "auto", "obj=", "NT SERVICE\\SentinelRecorder", "DisplayName=", "Sentinel Recorder")
    assert ("sc.exe", "failure", "SentinelRecorder", "reset=", "86400", "actions=",
            "restart/5000/restart/30000/restart/60000") in argvs
    assert ("sc.exe", "failureflag", "SentinelRecorder", "1") in argvs
    assert ("sc.exe", "sidtype", "SentinelRecorder", "unrestricted") in argvs
    assert ("sc.exe", "privs", "SentinelRecorder", "SeChangeNotifyPrivilege") in argvs
    assert ("icacls", r"D:\Sentinel\spool", "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F", f"*{SVC_SID}:(OI)(CI)M", "/Q") in argvs
    assert ("icacls", r"D:\Sentinel\spool", "/setowner", "*S-1-5-32-544", "/Q") in argvs
    assert ("icacls", r"D:\Sentinel\spool\*", "/reset", "/T", "/C", "/Q") in argvs
    assert ("icacls", r"C:\ProgramData\Sentinel\config", "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F", f"*{SVC_SID}:(OI)(CI)RX", "*S-1-5-32-545:(OI)(CI)RX", "/Q") in argvs
    assert ("icacls", r"C:\ProgramData\Sentinel\config\camera.dpapi", "/inheritance:r", "/grant:r",
            "*S-1-5-18:F", "*S-1-5-32-544:F", f"*{SVC_SID}:R", "/Q") in argvs
    assert argvs[-1] == ("sc.exe", "start", "SentinelRecorder")
    actions = [s.action for s in steps if s.action]
    assert [a for a in actions if a != "remove_account_aces"] == ["set_preshutdown_timeout", "make_dirs"]
    # Review 2026-11-12: what first-run setup left (its account's Full Control, ownership) is removed.
    drops = [s.params for s in steps if s.action == "remove_account_aces"]
    assert {"paths": [r"C:\ProgramData\Sentinel\config"], "accounts": "all_but",
            "keep": ["S-1-5-18", "S-1-5-32-544", SVC_SID, "S-1-5-32-545"]} in drops
    assert {"paths": [r"C:\ProgramData\Sentinel\config\camera.dpapi"], "accounts": "all_but",
            "keep": ["S-1-5-18", "S-1-5-32-544", SVC_SID]} in drops
    assert ("icacls", r"C:\ProgramData\Sentinel\config", "/setowner", "*S-1-5-32-544", "/Q") in argvs
    assert ("icacls", r"C:\ProgramData\Sentinel\config\camera.dpapi", "/setowner", "*S-1-5-32-544", "/Q") in argvs
    pre = [s for s in steps if s.action == "set_preshutdown_timeout"][0]
    assert pre.params == {"service": "SentinelRecorder", "timeout_ms": 180000}
    assert ("net", "localgroup", "Sentinel Operators", "/add",
            "/comment:May read Sentinel recorder status (not recordings, not settings)") in argvs
    assert ("net", "localgroup", "Sentinel Operators") in argvs
    assert ("icacls", r"C:\ProgramData\Sentinel\status", "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F", f"*{SVC_SID}:(OI)(CI)M", "Sentinel Operators:(OI)(CI)RX", "/Q") in argvs
    by_argv = {s.argv: s for s in steps if s.argv}
    assert not by_argv[("net", "localgroup", "Sentinel Operators")].may_fail       # group must exist
    cred = [s for s in steps if s.argv and "camera.dpapi" in " ".join(s.argv)]
    assert len(cred) == 2 and not any(c.may_fail for c in cred)       # review 2.6: fatal (ACL, then owner)
    assert not any(s.may_fail for s in steps if s.action == "remove_account_aces")
    mk = [s for s in steps if s.action == "make_dirs"][0]
    assert r"C:\ProgramData\Sentinel\status" in mk.params["paths"]
    # The service must exist before folder ACLs name its SID? Not required (SIDs are literal), but
    # the order is: register -> configure -> folders -> start.
    assert [s.argv[1] for s in steps if s.argv and s.argv[0] == "sc.exe" and s.argv[2] == "SentinelRecorder"] == [
        "create", "description", "failure", "failureflag", "sidtype", "privs", "start"]


def test_status_readers_users_option_and_status_dir_required() -> None:
    steps = install_plan(_inputs(readers="users"), forbidden=FORBIDDEN)
    argvs = [s.argv for s in steps if s.argv]
    assert not any(a[0] == "net" for a in argvs)
    assert any(a[:2] == ("icacls", r"C:\ProgramData\Sentinel\status") and "*S-1-5-32-545:(OI)(CI)RX" in a
               for a in argvs)
    inp = _inputs()
    for bad in (None, Path(r"C:\ProgramData\Sentinel\data"), Path(r"C:\ProgramData\Sentinel\config")):
        with pytest.raises(PlanError, match="status_dir"):
            install_plan(InstallInputs(inp.config_path, inp.data_dirs, None, inp.host, status_dir=bad),
                         forbidden=FORBIDDEN)


def test_preshutdown_window_covers_the_bounded_shutdown_work() -> None:
    from sentinel import service
    from sentinel.winsvc.commands import PRESHUTDOWN_TIMEOUT_MS

    assert service.SHUTDOWN_BUDGET_S < PRESHUTDOWN_TIMEOUT_MS / 1000


def test_install_plan_rendering_is_what_subprocess_runs() -> None:
    step = install_plan(_inputs(), forbidden=FORBIDDEN)[0]
    line = render(step)
    assert line == subprocess.list2cmdline(step.argv)
    assert 'binPath= "\\"C:\\Program Files\\Sentinel\\sentinel-service.exe\\" host --config' in line


def test_install_plan_options() -> None:
    steps = install_plan(_inputs(start=False, creds=False), forbidden=FORBIDDEN)
    assert not any(s.argv == ("sc.exe", "start", "SentinelRecorder") for s in steps)
    assert not any(s.argv and "camera.dpapi" in " ".join(s.argv) for s in steps)
    assert FAILURE_ACTIONS.endswith("restart/60000")


@pytest.mark.parametrize("path", [r"C:\\", r"C:\ProgramData", r"C:\Users", r"D:\Data", r"C:\Users\op",
                                  "relative\\dir"])
def test_refuses_to_re_acl_system_or_shallow_folders(path: str) -> None:
    with pytest.raises(PlanError):
        check_dedicated_dir(PureWindowsPath(path), "folder", FORBIDDEN)  # type: ignore[arg-type]


def test_refuses_ancestor_of_a_system_folder() -> None:
    with pytest.raises(PlanError):
        check_dedicated_dir(PureWindowsPath(r"C:\Program Files\Sub"), "folder",  # type: ignore[arg-type]
                            [r"C:\Program Files\Sub\Windows"])


def test_accepts_dedicated_folders() -> None:
    check_dedicated_dir(PureWindowsPath(r"C:\ProgramData\Sentinel\data"), "f", FORBIDDEN)  # type: ignore[arg-type]
    check_dedicated_dir(PureWindowsPath(r"C:\Users\op\Sentinel\data"), "f", FORBIDDEN)  # type: ignore[arg-type]


def test_config_folder_must_not_be_a_data_folder() -> None:
    inp = _inputs()
    bad = InstallInputs(inp.config_path, (inp.config_path.parent,), None, inp.host)
    with pytest.raises(PlanError):
        install_plan(bad, forbidden=FORBIDDEN)


def test_uninstall_plan_never_touches_data() -> None:
    steps = uninstall_plan()
    assert [s.argv for s in steps if s.argv] == [("sc.exe", "stop", "SentinelRecorder"),
                                                ("sc.exe", "delete", "SentinelRecorder")]
    assert steps[0].may_fail
    assert not any(s.argv and s.argv[0] in ("icacls", "rmdir", "del") for s in steps)


def test_execute_stops_at_first_required_failure() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if argv[0] in ("may", "bad") else 0, "", "boom")

    out: list[str] = []
    steps = [Step("ok", ("ok",)), Step("may", ("may",), may_fail=True), Step("bad", ("bad",)), Step("never", ("x",))]
    assert execute(steps, run=run, out=out.append) is False
    assert calls == [["ok"], ["may"], ["bad"]]
    assert any("FAILED" in o for o in out)


def test_cli_dry_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["uninstall", "--dry-run"]) == 0
    assert "sc.exe delete SentinelRecorder" in capsys.readouterr().out
    cfg = tmp_path / "missing.toml"
    assert main(["install", "--config", str(cfg), "--dry-run"]) == 1
    assert "configuration" in capsys.readouterr().out


def test_exit_meanings_cover_service_exit_codes() -> None:
    from sentinel import service

    for code in (service.EXIT_OK, service.EXIT_CONFIG, service.EXIT_LOCKED, service.EXIT_RUNTIME,
                 service.EXIT_WATCHDOG):
        assert code in EXIT_MEANINGS


# ------------------------------------------------------------------ owner decisions (8) and (9)
def test_status_readers_default_is_the_operators_group() -> None:
    import argparse

    from sentinel.winsvc import __main__ as cli

    assert InstallInputs(Path("c"), (), None, _inputs().host).status_readers == "group"
    captured: dict = {}

    def fake_install(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 0
    old = cli.cmd_install
    cli.cmd_install = fake_install
    try:
        cli.main(["install", "--config", "x.toml"])
    finally:
        cli.cmd_install = old
    assert captured["args"].status_readers == "group" and captured["args"].time_server is None


def test_time_service_steps_exact_commands() -> None:
    from sentinel.winsvc.commands import time_service_steps

    steps = time_service_steps("ntp1.sentinel.lan")
    assert [s.argv for s in steps] == [
        ("sc.exe", "config", "w32time", "start=", "auto"),
        ("sc.exe", "start", "w32time"),
        ("w32tm", "/config", "/manualpeerlist:ntp1.sentinel.lan,0x8", "/syncfromflags:manual", "/update"),
        ("w32tm", "/resync", "/nowait"),
    ]
    assert [s.may_fail for s in steps] == [False, True, False, True]
    assert [s.argv for s in time_service_steps(None)] == [("sc.exe", "config", "w32time", "start=", "auto"),
                                                          ("sc.exe", "start", "w32time")]


def test_install_plan_configures_time_before_starting_the_recorder() -> None:
    inp = _inputs()
    plan = install_plan(InstallInputs(inp.config_path, inp.data_dirs, inp.credentials_path, inp.host,
                                      status_dir=inp.status_dir, time_server="192.0.2.123"), forbidden=FORBIDDEN)
    argvs = [s.argv for s in plan if s.argv]
    i_time = argvs.index(("w32tm", "/config", "/manualpeerlist:192.0.2.123,0x8", "/syncfromflags:manual", "/update"))
    assert argvs.index(("sc.exe", "config", "w32time", "start=", "auto")) < i_time
    assert i_time < argvs.index(("sc.exe", "start", "SentinelRecorder"))


@pytest.mark.parametrize("host", ["ntp", "ntp1.sentinel.lan", "time-1.example.org", "192.0.2.1", "10.0.0.255",
                                  "ntp.example.org.", "a" * 63 + ".lan"])
def test_valid_time_servers(host: str) -> None:
    from sentinel.winsvc.commands import validate_time_server

    assert validate_time_server(host) == host.rstrip(".")


@pytest.mark.parametrize("host", [
    "", " ", "ntp lan", "ntp1 evil", "ntp,0x1", "ntp\"", "ntp'", "ntp&calc", "ntp|x", "ntp>x", "ntp^x", "%PATH%",
    "/update", "ntp/x", "ntp\\x", "-ntp", "ntp-", "ntp..lan", ".ntp", "256.1.1.1", "1.2.3", "01.2.3.4",
    "ntp\nlan", "ntp\tlan", "ntp\x00", "nt\u0440.lan", "[2001:db8::1]", "2001:db8::1", "ntp:123",
    "http://ntp.lan", "a" * 64 + ".lan", ("a" * 60 + ".") * 5])
def test_invalid_time_servers_are_refused(host: str) -> None:
    from sentinel.winsvc.commands import time_service_steps, validate_time_server

    with pytest.raises(PlanError):
        validate_time_server(host)
    with pytest.raises(PlanError):
        time_service_steps(host)


# ------------------------------------------------------------------ review 2026-11-12: install / repair
def _archive_inputs(existing: frozenset[str]) -> InstallInputs:
    inp = _inputs()
    ahost = host_command(inp.config_path, frozen=True, executable=r"C:\Program Files\Sentinel\SentinelService.exe",
                         program="archive")
    return InstallInputs(inp.config_path, inp.data_dirs, inp.credentials_path, inp.host, status_dir=inp.status_dir,
                         archive_host=ahost, existing=existing)


def test_repair_updates_the_registration_in_place_and_sets_every_permission_again() -> None:
    """'Install / repair service' on an installed computer stopped at 'already installed', so the 0.3.12
    permission fixes (camera secret, configuration folder, archive access to status\\requests) never arrived."""
    from sentinel.platform.winacl import service_sid

    steps = install_plan(_archive_inputs(frozenset({"SentinelRecorder", "SentinelArchive"})), forbidden=FORBIDDEN)
    argvs = [s.argv for s in steps if s.argv]
    assert not any(a[:2] == ("sc.exe", "create") for a in argvs)
    rec = [a for a in argvs if a[:3] == ("sc.exe", "config", "SentinelRecorder")]
    arc = [a for a in argvs if a[:3] == ("sc.exe", "config", "SentinelArchive")]
    assert rec and rec[0][3:] == ("binPath=", _inputs().host.command_line, "start=", "auto", "obj=",
                                  "NT SERVICE\\SentinelRecorder", "DisplayName=", "Sentinel Recorder")
    assert arc and arc[0][5:7] == ("start=", "delayed-auto")
    # the recorder is never stopped; the archive copy is paused first and started again at the end
    assert ("sc.exe", "stop", "SentinelRecorder") not in argvs
    assert argvs[0] == ("sc.exe", "stop", "SentinelArchive") and steps[0].may_fail
    ends = [s for s in steps if s.action == "ensure_running"]
    assert [s.params["service"] for s in ends] == ["SentinelRecorder", "SentinelArchive"] and steps[-1] is ends[-1]
    assert not any(a[:2] == ("sc.exe", "start") and a[2].startswith("Sentinel") for a in argvs)
    # every permission step of a fresh installation runs again
    fresh = install_plan(_archive_inputs(frozenset()), forbidden=FORBIDDEN)
    acl = [s.argv for s in fresh if s.argv and s.argv[0] == "icacls"]
    assert acl and all(a in argvs for a in acl)
    assert ("icacls", r"C:\ProgramData\Sentinel\status\requests", "/grant",
            f"*{service_sid('SentinelArchive')}:(OI)(CI)M", "/Q") in argvs
    assert [s.params for s in steps if s.action == "remove_account_aces"] == \
           [s.params for s in fresh if s.action == "remove_account_aces"]


def test_repair_registers_an_archive_service_that_was_not_installed() -> None:
    steps = install_plan(_archive_inputs(frozenset({"SentinelRecorder"})), forbidden=FORBIDDEN)
    argvs = [s.argv for s in steps if s.argv]
    assert any(a[:3] == ("sc.exe", "config", "SentinelRecorder") for a in argvs)
    assert any(a[:3] == ("sc.exe", "create", "SentinelArchive") for a in argvs)
    assert ("sc.exe", "stop", "SentinelArchive") not in argvs
    assert argvs[-1] == ("sc.exe", "start", "SentinelArchive")


class _Info:
    def __init__(self, state: int, win32: int = 0, svc: int = 0, binary: str = "") -> None:
        self.state, self.win32_exit_code, self.service_exit_code, self.binary_path = state, win32, svc, binary


def _install_world(monkeypatch: pytest.MonkeyPatch, installed: set[str], ok: bool = True):  # noqa: ANN202
    import sys

    from sentinel.winsvc import __main__ as cli
    from sentinel.winsvc import scm

    inp = _archive_inputs(frozenset())
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cli, "preflight", lambda _p: (inp, [], []))
    monkeypatch.setattr(scm, "query_service", lambda n: _Info(4, binary="old") if n in installed else None)
    monkeypatch.setattr(scm, "is_admin", lambda: True)
    ran: list[list] = []
    monkeypatch.setattr(cli, "execute", lambda steps: ran.append(steps) or ok)
    started: list[str] = []
    monkeypatch.setitem(cli._ACTIONS, "ensure_running", lambda service, timeout_s: started.append(service))
    return cli, ran, started


def _install_args(dry_run: bool):  # noqa: ANN202
    import argparse

    return argparse.Namespace(config="c.toml", dry_run=dry_run, no_start=False, status_readers="group",
                              time_server=None, keep_power_settings=False)


def test_install_on_an_installed_computer_repairs_instead_of_refusing(monkeypatch: pytest.MonkeyPatch,
                                                                       capsys: pytest.CaptureFixture[str]) -> None:
    cli, ran, _started = _install_world(monkeypatch, {"SentinelRecorder", "SentinelArchive"})
    assert cli.cmd_install(_install_args(dry_run=True)) == 0 and not ran
    out = capsys.readouterr().out
    assert "REPAIR:" in out and "sc.exe config SentinelRecorder" in out and "Run 'uninstall' first" not in out
    assert "runs with its previous command until it is restarted" in out          # binPath differs
    assert cli.cmd_install(_install_args(dry_run=False)) == 0
    assert "Repaired." in capsys.readouterr().out
    assert any(s.argv and s.argv[:3] == ("sc.exe", "config", "SentinelRecorder") for s in ran[0])


def test_a_failed_repair_starts_the_services_again(monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    cli, _ran, started = _install_world(monkeypatch, {"SentinelRecorder", "SentinelArchive"}, ok=False)
    assert cli.cmd_install(_install_args(dry_run=False)) == 1
    assert started == ["SentinelRecorder", "SentinelArchive"] and "Repair stopped" in capsys.readouterr().out


def test_ensure_running_starts_only_a_stopped_service_and_reports_a_failed_start() -> None:
    from types import SimpleNamespace

    from sentinel.winsvc import scm
    from sentinel.winsvc.__main__ import _act_ensure_running

    def world(states: list[_Info]) -> tuple[SimpleNamespace, list[str]]:
        calls: list[str] = []

        def query(_n: str) -> _Info:
            return states.pop(0) if len(states) > 1 else states[0]
        return SimpleNamespace(query_service=query, start_service=lambda n: calls.append(n) or 0,
                               SERVICE_RUNNING=scm.SERVICE_RUNNING, SERVICE_STOPPED=scm.SERVICE_STOPPED,
                               SERVICE_START_PENDING=scm.SERVICE_START_PENDING,
                               ERROR_SERVICE_ALREADY_RUNNING=scm.ERROR_SERVICE_ALREADY_RUNNING), calls

    fake, calls = world([_Info(scm.SERVICE_RUNNING)])
    _act_ensure_running("SentinelRecorder", scm=fake, sleep=lambda _s: None)
    assert calls == []                                                   # running: left alone
    fake, calls = world([_Info(scm.SERVICE_STOPPED), _Info(scm.SERVICE_START_PENDING), _Info(scm.SERVICE_RUNNING)])
    _act_ensure_running("SentinelRecorder", scm=fake, sleep=lambda _s: None)
    assert calls == ["SentinelRecorder"]
    fake, _ = world([_Info(scm.SERVICE_STOPPED), _Info(scm.SERVICE_STOPPED, 1066, 2)])
    with pytest.raises(RuntimeError, match="exit 2"):
        _act_ensure_running("SentinelRecorder", scm=fake, sleep=lambda _s: None)


def test_the_archive_service_logs_to_its_own_file(tmp_path: Path) -> None:
    """Two services rotating one recorder.log on Windows lose records; the archive service writes archive.log."""
    import logging

    from conftest import base_config_dict
    from gui_support import write_toml

    from sentinel.winsvc.__main__ import run_archive_in_host

    class Host:
        def on_stop(self, cb) -> None:  # noqa: ANN001
            self.cb = cb

        def report_running(self) -> None:
            pass

    class Worker:
        def __init__(self, _cfg) -> None:  # noqa: ANN001
            pass

        def stop(self, _code: int) -> None:
            pass

        def run(self, whole_process: bool) -> None:
            logging.getLogger("sentinel.archive").warning("copy note")
            host.cb()

    d = base_config_dict(tmp_path)
    d["archive"] = {"enabled": True, "drive_path": str(tmp_path / "arch")}
    host = Host()
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    try:
        assert run_archive_in_host(host, str(write_toml(tmp_path / "c.toml", d)), worker_factory=Worker,
                                   event_log=lambda _m: None) == 0
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])
    logs = Path(d["storage"]["log_dir"])
    assert "copy note" in (logs / "archive.log").read_text("utf-8")
    assert not (logs / "recorder.log").exists()
