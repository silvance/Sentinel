"""Unattended laptop: keep-awake, power plan at install, H-001 sleep-risk alert (sentinel.power)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from sentinel import power
from sentinel.power import (ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake, PowerSettings, power_setting_commands,
                            read_power_settings, sleep_risks)
from sentinel.winsvc.commands import InstallInputs, host_command, install_plan

CFG = Path(r"C:\ProgramData\Sentinel\config\sentinel.toml")
FORBIDDEN = [r"C:\Windows", r"C:\Program Files", r"C:\ProgramData", r"C:\Users"]


class FakeApi:
    def __init__(self, result: int = 1) -> None:
        self.calls: list[tuple[int, str]] = []
        self.result = result

    def __call__(self, flags: int) -> int:
        self.calls.append((flags, threading.current_thread().name))
        return self.result


def test_keep_awake_holds_and_releases_on_its_own_thread() -> None:
    api = FakeApi()
    k = KeepAwake(api=api, platform="win32").start()
    assert k.active is True
    k.stop()
    assert api.calls == [(ES_CONTINUOUS | ES_SYSTEM_REQUIRED, "keep-awake"), (ES_CONTINUOUS, "keep-awake")]
    assert k.active is False


def test_keep_awake_failure_is_reported_not_raised() -> None:
    k = KeepAwake(api=FakeApi(result=0), platform="win32").start()
    assert k.active is False and k.error
    k.stop()

    def boom(_flags: int) -> int:
        raise OSError("no kernel32")

    k2 = KeepAwake(api=boom, platform="win32").start()
    assert k2.active is False and "no kernel32" in (k2.error or "")
    k2.stop()


def test_keep_awake_is_a_no_op_elsewhere() -> None:
    api = FakeApi()
    k = KeepAwake(api=api, platform="linux").start()
    k.stop()
    assert k.active is None and api.calls == []


@pytest.mark.parametrize("ps,awake,expected", [
    (PowerSettings(lid_present=True, lid_ac=0, lid_dc=0, standby_ac_s=600), True, []),
    (PowerSettings(lid_present=True, lid_ac=1, lid_dc=0), True, ["closing the lid makes the computer sleep (on mains power)"]),
    (PowerSettings(lid_present=True, lid_ac=0, lid_dc=2), True, ["closing the lid makes the computer hibernate (on battery)"]),
    (PowerSettings(lid_present=False, lid_ac=1, lid_dc=1), True, []),          # desktop: no lid
    (PowerSettings(lid_present=False, standby_ac_s=900, hibernate_ac_s=0), False,
     ["the recorder could not keep the computer awake", "the computer sleeps after 15 min without use (on mains power)"]),
])
def test_sleep_risks(ps: PowerSettings, awake: bool, expected: list[str]) -> None:
    assert sleep_risks(ps, awake) == expected


def test_install_sets_the_power_plan_before_starting_unless_kept() -> None:
    host = host_command(CFG, frozen=True, executable=r"C:\Program Files\Sentinel\SentinelService.exe")
    dirs = (Path(r"D:\Sentinel\spool"), Path(r"C:\ProgramData\Sentinel\data"), Path(r"C:\ProgramData\Sentinel\logs"))
    base = dict(status_dir=Path(r"C:\ProgramData\Sentinel\status"))
    steps = install_plan(InstallInputs(CFG, dirs, None, host, **base), FORBIDDEN)
    argvs = [s.argv for s in steps if s.argv]
    expected = [argv for _, argv, _ in power_setting_commands()]
    i = argvs.index(expected[0])
    assert argvs[i:i + len(expected)] == expected
    assert i + len(expected) <= argvs.index(("sc.exe", "start", "SentinelRecorder"))
    assert ("powercfg", "/change", "standby-timeout-dc", "0") in argvs          # battery: a power cut keeps recording
    assert ("powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", "0") in argvs
    lid = [s for s in steps if s.argv and "LIDACTION" in s.argv]
    assert len(lid) == 2 and all(s.may_fail for s in lid)                       # desktops have no lid
    kept = install_plan(InstallInputs(CFG, dirs, None, host, power_settings=False, **base), FORBIDDEN)
    assert not any(s.argv and s.argv[0] == "powercfg" for s in kept)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows power API")
def test_reads_the_real_power_plan_of_this_computer() -> None:
    ps = read_power_settings()
    assert ps.error is None, ps.error
    assert ps.lid_present in (True, False)
    for v in (ps.lid_ac, ps.lid_dc, ps.standby_ac_s, ps.standby_dc_s, ps.hibernate_ac_s, ps.hibernate_dc_s):
        assert v is None or v >= 0


def test_lock_workstation_does_nothing_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "linux")
    assert power.lock_workstation() is False
