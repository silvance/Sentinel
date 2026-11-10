"""CLI confirmation rules and safe-removal logic (with fakes; no real disk is touched)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from drive_fakes import FakeInspector, blank_disk, foreign_disk, protected_for, system_disk

from sentinel.drives import cli
from sentinel.drives import eject as ej
from sentinel.drives import prepare as prep
from sentinel.drives.classify import classify_all
from sentinel.drives.errors import SafeRemoveError
from sentinel.drives.marker import RootContents
from sentinel.drives.policy import DrivePolicy


class FakeEnv:
    def __init__(self, tmp: Path, disks, inspector=None, cfg=True) -> None:
        self.policy = DrivePolicy()
        self.protected = protected_for()
        self.windows = True
        self.cfg = SimpleNamespace(data_path=tmp / "data") if cfg else None
        self._disks = disks
        self._insp = inspector or FakeInspector()

    def enumerate(self):
        return list(self._disks)

    def classify(self):
        return classify_all(self.enumerate(), self.policy, self.protected, inspector=self._insp)


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    def fake_prepare(ctx, disk_id, conf, auth):
        seen.append((disk_id, conf, auth))
        return prep.PrepareResult(disk_id, "SENTINEL_1", "u", "ntfs", {}, {"uncached": True}, "BLANK")

    monkeypatch.setattr(prep, "prepare_drive", fake_prepare)
    return seen


def _args(*extra: str):
    return cli.build_parser().parse_args(["prepare", "--disk", "9", *extra])


def _reader(*answers: str):
    it = iter(answers)
    return lambda prompt: next(it)


def test_env_reads_drives_policy_from_sentinel_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "sentinel.toml"
    cfg.write_text('[camera]\nid = "CAM01"\nurl = "tcp://127.0.0.1:9"\ninput_format = "mpegts"\n'
                   'development_source = true\n[storage]\nspool_dir = "spool"\ndata_dir = "data"\nlog_dir = "logs"\n'
                   '[drives]\napproved_filesystem = "exfat"\nforeign_data_authorization = "never"\n', encoding="utf-8")
    env = cli._Env(cli.build_parser().parse_args(["list", "--config", str(cfg)]))
    assert env.policy.approved_filesystem == "exfat" and env.policy.foreign_data_authorization == "never"
    roles = {i.role for i in env.protected.items if not i.error}
    assert {"operating_system", "recordings", "ledger", "logs"} <= roles


def test_prepare_needs_config(tmp_path: Path, calls) -> None:
    env = FakeEnv(tmp_path, [blank_disk(), system_disk()], cfg=False)
    assert cli.cmd_prepare(env, _args(), interactive=True) == cli.EXIT_USAGE and calls == []


@pytest.mark.parametrize("extra", [(), ("--yes",), ("--serial-tail", "ABCD"), ("--yes", "--serial-tail", "ABCE")])
def test_non_interactive_refuses_without_yes_and_correct_serial(tmp_path: Path, calls, extra) -> None:
    env = FakeEnv(tmp_path, [blank_disk(), system_disk()])
    assert cli.cmd_prepare(env, _args(*extra), interactive=False) == cli.EXIT_REFUSED
    assert calls == []


def test_non_interactive_with_yes_and_serial(tmp_path: Path, calls) -> None:
    env = FakeEnv(tmp_path, [blank_disk(), system_disk()])
    assert cli.cmd_prepare(env, _args("--yes", "--serial-tail", "abcd", "--who", "op1"), interactive=False) == 0
    disk_id, conf, auth = calls[0]
    assert conf.typed_serial_tail == "ABCD" and auth.who == "op1" and not auth.administrator
    assert conf.fingerprint == blank_disk().identity.fingerprint()


def test_interactive_blank_requires_typing_erase(tmp_path: Path, calls) -> None:
    env = FakeEnv(tmp_path, [blank_disk(), system_disk()])
    assert cli.cmd_prepare(env, _args(), reader=_reader("yes"), interactive=True) == cli.EXIT_REFUSED
    assert calls == []
    assert cli.cmd_prepare(env, _args(), reader=_reader("ERASE"), interactive=True) == 0
    assert len(calls) == 1


def test_interactive_eof_is_not_confirmation(tmp_path: Path, calls) -> None:
    def eof(prompt):
        raise EOFError

    env = FakeEnv(tmp_path, [blank_disk(), system_disk()])
    assert cli.cmd_prepare(env, _args(), reader=eof, interactive=True) == cli.EXIT_REFUSED and calls == []


def test_foreign_requires_admin_flag_and_os_admin(tmp_path: Path, calls, monkeypatch) -> None:
    d = foreign_disk()
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("x",))})
    env = FakeEnv(tmp_path, [d, system_disk()], inspector=insp)
    monkeypatch.setattr(prep, "os_administrator_check", lambda: (False, "test"))
    assert cli.cmd_prepare(env, _args(), reader=_reader("ABCD"), interactive=True) == cli.EXIT_REFUSED
    assert cli.cmd_prepare(env, _args("--admin"), reader=_reader("ABCD"), interactive=True) == cli.EXIT_REFUSED
    assert calls == []
    monkeypatch.setattr(prep, "os_administrator_check", lambda: (True, "test"))
    assert cli.cmd_prepare(env, _args("--admin"), reader=_reader("abcd"), interactive=True) == 0
    assert calls[0][2].administrator and calls[0][1].typed_serial_tail == "ABCD"


def test_system_disk_never_reaches_confirmation(tmp_path: Path, calls) -> None:
    s = system_disk()
    env = FakeEnv(tmp_path, [s])
    args = cli.build_parser().parse_args(["prepare", "--disk", s.disk_id, "--yes", "--serial-tail", "S123"])
    assert cli.cmd_prepare(env, args, reader=_reader("ERASE"), interactive=True) == cli.EXIT_REFUSED
    assert calls == []


def test_dry_run_prints_script_and_changes_nothing(tmp_path: Path, calls, capsys) -> None:
    env = FakeEnv(tmp_path, [blank_disk(), system_disk()])
    assert cli.cmd_prepare(env, _args("--dry-run"), interactive=False) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Clear-Disk -InputObject $d -RemoveData -RemoveOEM" in out and calls == []


# ---------------------------------------------------------------------------- safe removal

def test_safe_remove_refuses_system_disk_before_touching_anything(monkeypatch) -> None:
    monkeypatch.setattr(ej, "_flush_windows", lambda *a: pytest.fail("must not flush"))
    with pytest.raises(SafeRemoveError) as ei:
        ej.safe_remove_windows(system_disk(), [system_disk()], protected_for(), enumerate_fn=list)
    assert ei.value.reason == "protected" and ei.value.code == "M-007"


def test_safe_remove_refuses_disk_holding_spool(monkeypatch) -> None:
    d = foreign_disk()
    monkeypatch.setattr(ej, "_flush_windows", lambda *a: pytest.fail("must not flush"))
    with pytest.raises(SafeRemoveError):
        ej.safe_remove_windows(d, [d], protected_for(spool_vol=d.volumes[0].volume_id), enumerate_fn=list)


class FakeCM:
    """Device tree: 1=disk(USBSTOR) -> 2=USB device(removable) -> 3=hub; 4=other disk under 2 or 3."""

    def __init__(self, other_parent: int, veto: int = 0) -> None:
        self.parents = {1: 2, 2: 3, 3: None, 4: other_parent}
        self.ids = {"USBSTOR\\DISK&VEN_WDC\\FAKE": 1, "OTHER": 4}
        self.veto = veto
        self.ejected: list[int] = []

    def locate(self, i):
        return self.ids[i]

    def ancestors(self, n, levels=16):
        out = []
        while self.parents.get(n):
            n = self.parents[n]
            out.append(n)
        return out[:levels]

    def status(self, n):
        return ej.DN_REMOVABLE if n == 2 else 0

    def device_id(self, n):
        return f"node{n}"

    def eject(self, n):
        self.ejected.append(n)
        return 0, self.veto, "proc" if self.veto else ""


def _patch(monkeypatch, cm: FakeCM) -> list:
    flushed = []
    monkeypatch.setattr(ej, "_flush_windows", lambda d, steps: flushed.append(d.disk_id))
    monkeypatch.setattr(ej, "_CfgMgr", lambda: cm)
    return flushed


def test_safe_remove_success_flow(monkeypatch) -> None:
    d = blank_disk()
    cm = FakeCM(other_parent=3)  # another disk on the same hub, different USB device: fine
    flushed = _patch(monkeypatch, cm)
    other = replace(blank_disk(disk_id="5", serial="OTHER1"), pnp_device_id="OTHER")
    res = ej.safe_remove_windows(d, [d, other], protected_for(), enumerate_fn=lambda: [other], wait_s=1)
    assert res.ok and res.message == "SAFE TO REMOVE DRIVE" and cm.ejected == [2] and flushed == ["9"]


def test_safe_remove_refuses_shared_dock(monkeypatch) -> None:
    d = blank_disk()
    cm = FakeCM(other_parent=2)  # two-bay dock: other disk under the same removable device
    _patch(monkeypatch, cm)
    other = replace(blank_disk(disk_id="5", serial="OTHER1"), pnp_device_id="OTHER")
    with pytest.raises(SafeRemoveError) as ei:
        ej.safe_remove_windows(d, [d, other], protected_for(), enumerate_fn=list, wait_s=1)
    assert ei.value.reason == "shared_device" and cm.ejected == []


def test_safe_remove_veto_reported(monkeypatch) -> None:
    d = blank_disk()
    cm = FakeCM(other_parent=3, veto=6)
    _patch(monkeypatch, cm)
    res = ej.safe_remove_windows(d, [d], protected_for(), enumerate_fn=lambda: [d], wait_s=1)
    assert not res.ok and "still in use" in res.message and res.veto == "outstanding open (files in use)"


def test_safe_remove_not_reported_safe_while_disk_still_present(monkeypatch) -> None:
    d = blank_disk()
    _patch(monkeypatch, FakeCM(other_parent=3))
    res = ej.safe_remove_windows(d, [d], protected_for(), enumerate_fn=lambda: [d], wait_s=0.1)
    assert not res.ok and "Do not unplug" in res.message
