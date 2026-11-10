"""Owner decision 2026-11-10: "Erase and Set Up Drive" stays OFF (``[drives] erase_enabled = false``)
until a supervised dock test; detection, classification and safe removal keep working."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from drive_fakes import FakeInspector, FakeRunner, Sequence, blank_disk, exfat_disk, foreign_disk, protected_for, system_disk

from sentinel.alerts import CATALOG, Severity, make_alert
from sentinel.config import ConfigError, load_config_dict
from sentinel.db import Ledger
from sentinel.drives import cli
from sentinel.drives import eject as ej
from sentinel.drives.classify import ERASE_DISABLED_MESSAGE, classify, classify_all
from sentinel.drives.errors import PrepareRefused
from sentinel.drives.marker import RootContents
from sentinel.drives.model import DriveClass
from sentinel.drives.policy import DrivePolicy, DrivePolicyError, load_policy
from sentinel.drives.prepare import Authorization, Confirmation, HelperResult, PrepareContext, check_guards, prepare_drive

OFF = DrivePolicy()
ADMIN = Authorization("admin1", administrator=True, method="test")
MSG = "Setting up drives is not enabled on this system yet. Contact technical support."


def test_default_is_off_and_owner_defaults_kept() -> None:
    p = load_policy(None)
    assert p.erase_enabled is False and p.approved_filesystem == "ntfs" and p.foreign_data_authorization == "admin_only"
    assert load_policy({"erase_enabled": True}).erase_enabled is True
    with pytest.raises(DrivePolicyError):
        load_policy({"erase_enabled": "yes"})


def test_config_section_controls_the_gate(tmp_path: Path) -> None:
    base = {"camera": {"id": "CAM01", "url": "tcp://127.0.0.1:9", "development_source": True},
            "storage": {"spool_dir": "s", "data_dir": "d", "log_dir": "l"}}
    assert load_config_dict(base, base_dir=tmp_path).drives.erase_enabled is False
    assert load_config_dict({**base, "drives": {"erase_enabled": True}}, base_dir=tmp_path).drives.erase_enabled
    with pytest.raises(ConfigError):
        load_config_dict({**base, "drives": {"erase_enabled": 1}}, base_dir=tmp_path)


@pytest.mark.parametrize("disk,cls,first", [
    (blank_disk(), DriveClass.BLANK, "This drive is empty and must be set up before use."),
    (exfat_disk(), DriveClass.WRONG_FORMAT, "This drive uses the wrong format (exFAT)."),
    (foreign_disk(), DriveClass.FOREIGN_DATA, "This drive contains other files (1 TB)."),
])
def test_classification_still_works_but_offers_no_erase(disk, cls, first: str) -> None:
    insp = FakeInspector({v.volume_id: RootContents(readable=True, entries=("a",)) for v in disk.volumes}
                         if cls is DriveClass.FOREIGN_DATA else {})
    c = classify(disk, OFF, protected_for(), inspector=insp)
    assert c.drive_class is cls                                   # detection unchanged
    assert not c.can_prepare and c.refusal == "erase_disabled" and c.alert_code == "M-010"
    assert c.message == f"{first} {MSG}" and "Press" not in c.message


def test_unsuitable_and_evidence_unaffected_by_gate() -> None:
    c = classify(system_disk(), OFF, protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.UNSUITABLE and c.alert_code == "M-004"


def test_alert_m010_wording() -> None:
    assert make_alert("M-010", Severity.WARNING).message == f"Error M-010: {MSG}"
    assert not CATALOG["M-010"].stops_recording


def test_guards_refuse_even_with_a_perfect_confirmation(tmp_path: Path) -> None:
    d = blank_disk()
    c = classify(d, DrivePolicy(erase_enabled=True), protected_for(), inspector=FakeInspector())  # as shown when ON
    with pytest.raises(PrepareRefused) as ei:
        check_guards(c, Confirmation.for_classification(c), ADMIN, OFF)
    assert ei.value.reason == "erase_disabled" and ei.value.code == "M-010" and str(ei.value) == MSG


def test_prepare_drive_never_starts_helper_when_off(tmp_path: Path) -> None:
    d = blank_disk()
    led = Ledger(tmp_path / "s.db")
    try:
        runner = FakeRunner(HelperResult(0, {"ok": True, "step": "done"}))
        ctx = PrepareContext(policy=OFF, protected=protected_for(), enumerate=Sequence([d, system_disk()]),
                             runner=runner, audit=led, platform="windows", inspector=FakeInspector(), settle_s=0)
        with pytest.raises(PrepareRefused) as ei:
            prepare_drive(ctx, d.disk_id, Confirmation(d.disk_id, d.identity.fingerprint(), DriveClass.BLANK), ADMIN)
        assert ei.value.reason == "erase_disabled" and runner.plans == []
        assert led.query("SELECT COUNT(*) FROM events")[0][0] == 0
    finally:
        led.close()


class _Env:
    def __init__(self, tmp: Path, policy: DrivePolicy) -> None:
        self.policy, self.protected, self.windows = policy, protected_for(), True
        self.cfg = SimpleNamespace(data_path=tmp / "data")
        self.config_path = tmp / "sentinel.toml"

    def enumerate(self):
        return [blank_disk(), system_disk()]

    def classify(self):
        return classify_all(self.enumerate(), self.policy, self.protected, inspector=FakeInspector())


def test_cli_prepare_refuses_when_off(tmp_path: Path, monkeypatch, capsys) -> None:
    called = []
    monkeypatch.setattr("sentinel.drives.prepare.prepare_drive", lambda *a: called.append(a))
    monkeypatch.setattr(cli._installed, "installed_config_problems", lambda p, c: [])
    args = cli.build_parser().parse_args(["prepare", "--disk", "9", "--yes", "--serial-tail", "ABCD", "--admin"])
    assert cli.cmd_prepare(_Env(tmp_path, OFF), args, interactive=False) == cli.EXIT_REFUSED and called == []
    out = capsys.readouterr().out
    assert "Error M-010" in out and MSG in out and "Nothing was erased" in out


def test_cli_list_and_classify_still_work_when_off(tmp_path: Path, capsys) -> None:
    env = _Env(tmp_path, OFF)
    assert cli.cmd_list(env, cli.build_parser().parse_args(["list"])) == cli.EXIT_OK
    assert cli.cmd_classify(env, cli.build_parser().parse_args(["classify", "--disk", "9"])) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "BLANK" in out and MSG in out


def test_safe_remove_works_when_off(monkeypatch) -> None:
    """The gate only concerns erasing; safe removal does not consult it."""
    d = blank_disk()

    class CM:
        def locate(self, i): return 1
        def ancestors(self, n, levels=16): return []
        def status(self, n): return ej.DN_REMOVABLE
        def device_id(self, n): return "n"
        def eject(self, n): return 0, 0, ""

    monkeypatch.setattr(ej, "_flush_windows", lambda disk, steps: None)
    monkeypatch.setattr(ej, "_CfgMgr", CM)
    res = ej.safe_remove_windows(d, [d], protected_for(), enumerate_fn=lambda: [], wait_s=1)
    assert res.ok and res.message == "SAFE TO REMOVE DRIVE"
