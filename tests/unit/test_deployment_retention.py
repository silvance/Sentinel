"""Owner decision 2026-11-10: deployed recorders copy to archive drives and remove archived
recordings when local storage runs low (retention ON at deployment). The code default stays OFF;
deployment turns it on (first-run setup, example configuration), and every deployment path
actually runs the archive copy that retention depends on (Windows service install, detached
launcher)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from sentinel import paths
from sentinel.archive import hosting
from sentinel.config import RetentionConfig, load_config, load_config_dict
from sentinel.launcher import cli, control, setup
from sentinel.winsvc.commands import (ARCHIVE_DESCRIPTION, ARCHIVE_DISPLAY_NAME, ARCHIVE_SERVICE_NAME,
                                      InstallInputs, host_command, install_plan, uninstall_plan)

ROOT = Path(__file__).resolve().parents[2]


def _layout(tmp: Path) -> paths.Layout:
    root = tmp / "machine"
    return paths.Layout("machine", root / "sentinel.toml", root / "camera.secret", root / "spool",
                        root / "data", root / "logs")


# ---------------------------------------------------------------------- configuration
def test_code_default_stays_off() -> None:
    assert RetentionConfig().enabled is False


def test_first_run_setup_turns_archive_and_retention_on(tmp_path: Path) -> None:
    lay = _layout(tmp_path)
    d = setup.build_config(setup.SetupAnswers("192.0.2.10", "stream1"), lay)
    assert d["archive"] == {"enabled": True}
    assert d["retention"] == {"enabled": True, "expected_swap_days": 5.0}
    cfg = load_config_dict(d, base_dir=tmp_path)
    assert cfg.archive.enabled and cfg.retention.enabled
    # Every safeguard keeps its tested default: only verified copies on REMOVED drives count.
    assert cfg.retention.require_removed_drive is True
    assert cfg.retention.min_verified_copies == 1
    assert cfg.retention.keep_recent_hours == 24.0


def test_written_configuration_says_retention_is_on(tmp_path: Path) -> None:
    lay = _layout(tmp_path)
    path, _ = setup.write_setup(setup.SetupAnswers("192.0.2.10", "stream1"), lay)
    text = path.read_text(encoding="utf-8")
    assert tomllib.loads(text)["retention"]["enabled"] is True
    assert "[retention] enabled = false" in text          # header tells the technician how to turn it off
    cfg = load_config(path)
    assert cfg.retention.enabled and cfg.archive.enabled


def test_example_configuration_matches_the_deployment_decision() -> None:
    cfg = load_config(ROOT / "config" / "sentinel.example.toml")
    assert cfg.archive.enabled and cfg.retention.enabled
    assert cfg.retention.expected_swap_days == 5


# ---------------------------------------------------------------------- detached launcher
def test_archive_command_frozen_and_source() -> None:
    cfg = Path("C:/S/sentinel.toml")
    assert control.archive_command(cfg, frozen=True, executable="Sentinel.exe") == [
        "Sentinel.exe", "archive", "run", "--config", str(cfg)]
    assert control.archive_command(cfg, frozen=False, executable="python") == [
        "python", "-m", "sentinel.archive", "run", "--config", str(cfg)]


def _write_archive_status(cfg: Any, **doc: Any) -> None:
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    (cfg.data_path / "archive_status.json").write_text(json.dumps(doc), encoding="utf-8")


def test_archive_alive_uses_the_heartbeat_only(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, archive={"enabled": True})
    assert control.archive_alive(cfg, now=1000.0) is False                       # no status yet
    _write_archive_status(cfg, state="COPYING", heartbeat_epoch=990.0)
    assert control.archive_alive(cfg, now=1000.0) is True
    assert control.archive_alive(cfg, now=990.0 + 61.0) is False                 # stale (A-007 threshold)
    _write_archive_status(cfg, state="STOPPED", heartbeat_epoch=999.0)
    assert control.archive_alive(cfg, now=1000.0) is False                       # stopped cleanly


class _Popen:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv: list[str], **_kw: Any) -> Any:
        self.calls.append(argv)
        if self.fail:
            raise OSError("no such program")
        return type("P", (), {"pid": 77})()


def _ctl(tmp: Path, popen: _Popen, enabled: bool, now: float = 1000.0) -> control.DetachedProcessControl:
    cfg = make_config(tmp, archive={"enabled": enabled}, retention={"enabled": enabled})
    return control.DetachedProcessControl(tmp / "sentinel.toml", cfg, popen=popen, clock=lambda: now)


def test_start_archive_only_when_enabled_and_not_running(tmp_path: Path) -> None:
    off = _Popen()
    assert _ctl(tmp_path / "a", off, enabled=False).start_archive() is False and off.calls == []

    p = _Popen()
    ctl = _ctl(tmp_path / "b", p, enabled=True)
    assert ctl.start_archive() is True
    assert p.calls and p.calls[0][-3:] == ["run", "--config", str(ctl.config_path)]
    assert p.calls[0] == control.archive_command(ctl.config_path)

    _write_archive_status(ctl.cfg, state="UP_TO_DATE", heartbeat_epoch=995.0)  # already running
    assert ctl.start_archive() is False and len(p.calls) == 1


def test_start_archive_failure_is_reported_not_raised(tmp_path: Path) -> None:
    assert _ctl(tmp_path, _Popen(fail=True), enabled=True).start_archive() is False


def test_launcher_forwards_archive_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentinel.archive.__main__ as archive_main

    seen: list[list[str]] = []
    monkeypatch.setattr(archive_main, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["archive", "swap", "--config", "x.toml", "--timeout", "5"]) == 0
    assert seen == [["swap", "--config", "x.toml", "--timeout", "5"]]


def test_launcher_forwards_export_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentinel.export.__main__ as export_main

    seen: list[list[str]] = []
    monkeypatch.setattr(export_main, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["export", "run", "--config", "x.toml", "--target", "E:\\"]) == 0
    assert seen == [["run", "--config", "x.toml", "--target", "E:\\"]]


def test_launcher_forwards_drive_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentinel.drives.cli as drives_cli

    seen: list[list[str]] = []
    monkeypatch.setattr(drives_cli, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["drives", "prepare", "--disk", "3", "--dry-run"]) == 0
    assert seen == [["prepare", "--disk", "3", "--dry-run"]]


# ---------------------------------------------------------------------- Windows service install
CFG = Path(r"C:\ProgramData\Sentinel\config\sentinel.toml")
DIRS = (Path(r"D:\Sentinel\spool"), Path(r"C:\ProgramData\Sentinel\data"), Path(r"C:\ProgramData\Sentinel\logs"))
EXE = r"C:\Program Files\Sentinel\SentinelService.exe"


def _inputs(archive: bool) -> InstallInputs:
    return InstallInputs(CFG, DIRS, None, host_command(CFG, frozen=True, executable=EXE),
                         status_dir=Path(r"C:\ProgramData\Sentinel\status"),
                         archive_host=host_command(CFG, frozen=True, executable=EXE, program="archive")
                         if archive else None)


FORBIDDEN = [r"C:\Windows", r"C:\Program Files", r"C:\ProgramData", r"C:\Users"]


def test_names_match_the_archive_hosting_module() -> None:
    assert (ARCHIVE_SERVICE_NAME, ARCHIVE_DISPLAY_NAME) == (hosting.SERVICE_NAME, hosting.DISPLAY_NAME)
    assert "Never deletes recordings" in ARCHIVE_DESCRIPTION


def test_install_without_archive_is_unchanged() -> None:
    argvs = [s.argv for s in install_plan(_inputs(False), FORBIDDEN) if s.argv]
    assert not any(ARCHIVE_SERVICE_NAME in a for a in argvs)


def test_install_registers_archive_service_after_folder_restriction() -> None:
    steps = install_plan(_inputs(True), FORBIDDEN)
    argvs = [s.argv for s in steps if s.argv]
    create = next(i for i, a in enumerate(argvs) if a[:3] == ("sc.exe", "create", ARCHIVE_SERVICE_NAME))
    assert argvs[create][argvs[create].index("binPath=") + 1] == (
        f'"{EXE}" host --config {CFG} --program archive')
    assert argvs[create][argvs[create].index("start=") + 1] == "delayed-auto"
    assert argvs[create][argvs[create].index("obj=") + 1] == r"NT SERVICE\SentinelArchive"
    # Archive grants come after the LAST /grant:r (which would otherwise reset them).
    last_restrict = max(i for i, a in enumerate(argvs) if a[0] == "icacls" and "/grant:r" in a)
    grants = [i for i, a in enumerate(argvs) if a[0] == "icacls" and "/grant" in a
              and any(str(x).startswith("*S-1-5-80-") and x.endswith(("RX", "M")) for x in a)
              and i > last_restrict]
    assert len(grants) == 6
    granted = {argvs[i][1]: argvs[i][3].split(":")[1] for i in grants}
    # Review 2026-11-12: plus the operators' request folder (Swap / Cancel swap from the operator window).
    assert granted == {str(DIRS[0]): "(OI)(CI)RX", str(DIRS[1]): "(OI)(CI)M", str(DIRS[2]): "(OI)(CI)M",
                       str(CFG.parent): "(OI)(CI)RX", r"C:\ProgramData\Sentinel\status": "RX",
                       r"C:\ProgramData\Sentinel\status\requests": "(OI)(CI)M"}
    # Recorder starts first, then the archive copy.
    starts = [a[2] for a in argvs if a[:2] == ("sc.exe", "start") and a[2] != "w32time"]
    assert starts == ["SentinelRecorder", ARCHIVE_SERVICE_NAME]


def test_uninstall_removes_archive_service_only_when_asked() -> None:
    assert not any(s.argv and ARCHIVE_SERVICE_NAME in s.argv for s in uninstall_plan())
    argvs = [s.argv for s in uninstall_plan(archive=True) if s.argv]
    assert ("sc.exe", "delete", ARCHIVE_SERVICE_NAME) in argvs and ("sc.exe", "delete", "SentinelRecorder") in argvs
    assert not any(a[0] in ("icacls", "rmdir", "del") for a in argvs)


# ---------------------------------------------------------------------- review 2026-11-12: SentinelArchive control
class _ScmWorld:
    """Fake SCM for both services: start -> RUNNING, stop -> STOPPED."""

    def __init__(self, states: dict[str, int | None], start_err: int = 0) -> None:
        self.states, self.start_err = dict(states), start_err
        self.calls: list[tuple[str, str]] = []

    def calls_api(self):  # noqa: ANN201
        from sentinel.winsvc import control as wctl

        return wctl.ScmCalls(self.query, self.start, self.stop)

    def query(self, name: str) -> Any:
        s = self.states.get(name)
        return None if s is None else type("I", (), {"state": s, "win32_exit_code": 0, "service_exit_code": 0})()

    def start(self, name: str) -> int:
        self.calls.append(("start", name))
        if self.start_err == 0:
            self.states[name] = 4
        return self.start_err

    def stop(self, name: str) -> int:
        self.calls.append(("stop", name))
        self.states[name] = 1
        return 0


def _svc_ctl(world: _ScmWorld, archive_enabled: bool | None = True):  # noqa: ANN202
    from sentinel.winsvc import control as wctl

    return wctl.ServiceControl(world.calls_api(), clock=lambda: 0.0, sleep=lambda _s: None,
                               archive_enabled=archive_enabled)


def test_service_control_starts_a_stopped_archive_service() -> None:
    """Nothing started SentinelArchive again after a clean stop: ServiceControl had no start_archive, so 'Save and
    restart recorder' restarted only the recorder."""
    from sentinel.winsvc import control as wctl

    assert wctl.ARCHIVE_SERVICE_NAME == ARCHIVE_SERVICE_NAME
    w = _ScmWorld({"SentinelRecorder": 4, ARCHIVE_SERVICE_NAME: 1})
    assert _svc_ctl(w).start_archive() is True and w.calls == [("start", ARCHIVE_SERVICE_NAME)]
    assert _svc_ctl(w).start_archive() is False and len(w.calls) == 1            # already running
    off = _ScmWorld({"SentinelRecorder": 4, ARCHIVE_SERVICE_NAME: 1})
    assert _svc_ctl(off, archive_enabled=False).start_archive() is False and off.calls == []
    absent = _ScmWorld({"SentinelRecorder": 4})
    assert _svc_ctl(absent).start_archive() is False and absent.calls == []
    denied = _ScmWorld({"SentinelRecorder": 4, ARCHIVE_SERVICE_NAME: 1}, start_err=5)
    assert _svc_ctl(denied).start_archive() is False                              # logged, not raised


def test_service_control_restarts_the_archive_service_so_it_reads_saved_settings() -> None:
    w = _ScmWorld({"SentinelRecorder": 4, ARCHIVE_SERVICE_NAME: 4})
    assert _svc_ctl(w).restart_archive() is True
    assert w.calls == [("stop", ARCHIVE_SERVICE_NAME), ("start", ARCHIVE_SERVICE_NAME)]
    # copying switched off in the settings: stopped and left stopped
    w2 = _ScmWorld({"SentinelRecorder": 4, ARCHIVE_SERVICE_NAME: 4})
    assert _svc_ctl(w2, archive_enabled=False).restart_archive() is False
    assert w2.calls == [("stop", ARCHIVE_SERVICE_NAME)] and w2.states[ARCHIVE_SERVICE_NAME] == 1


def test_service_provider_passes_the_archive_setting(tmp_path: Path) -> None:
    from sentinel.winsvc import control as wctl

    w = _ScmWorld({"SentinelRecorder": 4})
    on = wctl.service_control_provider(tmp_path, make_config(tmp_path, archive={"enabled": True}),
                                       api=w.calls_api(), platform="win32")
    off = wctl.service_control_provider(tmp_path, make_config(tmp_path, archive={"enabled": False}),
                                        api=w.calls_api(), platform="win32")
    assert on.archive_enabled is True and off.archive_enabled is False
