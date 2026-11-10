"""Cross-branch integration items (docs/INTEGRATION.md): single-executable x windows-service.

No real service is installed, started or stopped: the SCM is a fake.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from sentinel import paths
from sentinel.launcher import cli, control
from sentinel.launcher import setup as lsetup
from sentinel.winsvc import SERVICE_NAME
from sentinel.winsvc import control as wctl
from sentinel.winsvc.commands import host_command
from sentinel.winsvc.scm import ERROR_ACCESS_DENIED, SERVICE_RUNNING, SERVICE_STOPPED


@dataclass
class Info:
    state: int
    win32_exit_code: int = 0
    service_exit_code: int = 0


class FakeScm:
    def __init__(self, states: list[Info | None], start_err: int = 0, stop_err: int = 0,
                 query_raises: bool = False) -> None:
        self.states = list(states)
        self.start_err, self.stop_err = start_err, stop_err
        self.query_raises = query_raises
        self.started = self.stopped = 0

    def calls(self) -> wctl.ScmCalls:
        return wctl.ScmCalls(self.query, self.start, self.stop)

    def query(self, name: str) -> Info | None:
        assert name == SERVICE_NAME
        if self.query_raises:
            raise OSError(5, "access denied")
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def start(self, name: str) -> int:
        self.started += 1
        return self.start_err

    def stop(self, name: str) -> int:
        self.stopped += 1
        return self.stop_err


def _ctl(scm: FakeScm) -> wctl.ServiceControl:
    t = [0.0]

    def sleep(s: float) -> None:
        t[0] += s
    return wctl.ServiceControl(scm.calls(), clock=lambda: t[0], sleep=sleep)


# ---------------------------------------------------------------------- (a) service executable
def test_frozen_install_from_the_launcher_points_the_service_at_sentinelservice_exe() -> None:
    hc = host_command(Path(r"C:\ProgramData\Sentinel\config\sentinel.toml"), frozen=True,
                      executable=r"C:\Program Files\Sentinel\Sentinel.exe")
    assert hc.command_line.startswith('"C:\\Program Files\\Sentinel\\SentinelService.exe" host --config ')
    assert hc.interpreter.endswith("SentinelService.exe")
    hc2 = host_command(Path(r"C:\cfg\sentinel.toml"), frozen=True,
                       executable=r"C:\Program Files\Sentinel\SentinelService.exe")
    assert hc2.interpreter.endswith("SentinelService.exe")


def test_packaging_declares_the_service_executable() -> None:
    root = Path(__file__).resolve().parents[2] / "packaging"
    spec = (root / "windows" / "sentinel.spec").read_text("utf-8")
    assert "common.SERVICE_ENTRY" in spec and "name=common.SERVICE_EXE_NAME" in spec and "exe_svc" in spec
    entry = (root / "entry_service.py").read_text("utf-8")
    assert "from sentinel.winsvc.__main__ import main" in entry
    sys.path.insert(0, str(root))
    try:
        import spec_common
        assert spec_common.SERVICE_EXE_NAME + ".exe" == "SentinelService.exe"
        assert "sentinel.winsvc.__main__" in spec_common.SERVICE_HIDDEN_IMPORTS
    finally:
        sys.path.remove(str(root))


# ---------------------------------------------------------------------- (b) one layout
def test_windows_layout_matches_the_service_layout() -> None:
    lay = paths.default_layout("win32", env={"ProgramData": r"C:\ProgramData"})
    assert lay.config_file.parent == lay.credentials_file.parent           # own config folder
    assert lay.config_file.parent.name == "config" and lay.credentials_file.name == "camera.secret"
    assert lay.status_dir is not None and lay.status_dir.name == "status"
    # status_dir is never defaulted into a configuration that names its own data folder
    assert "status_dir" not in paths.default_storage_dirs()


def test_setup_writes_status_dir_when_the_layout_has_one(tmp_path: Path) -> None:
    lay = paths.Layout("machine", tmp_path / "config" / "sentinel.toml", tmp_path / "config" / "camera.secret",
                       tmp_path / "spool", tmp_path / "data", tmp_path / "logs", tmp_path / "status")
    d = lsetup.build_config(lsetup.SetupAnswers("192.0.2.10", "live"), lay)
    assert d["storage"]["status_dir"] == str(tmp_path / "status")
    d2 = lsetup.build_config(lsetup.SetupAnswers("192.0.2.10", "live", storage_root=tmp_path / "x"), lay)
    assert d2["storage"]["status_dir"] == str(tmp_path / "x" / "status")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_setup_secret_on_windows_is_dpapi_and_readable_by_the_service_loader(tmp_path: Path) -> None:
    from sentinel.credentials import DPAPI_MAGIC, load_credentials

    secret = tmp_path / "camera.secret"
    lsetup.write_secret(secret, "viewer", "Pa\"ss\\w0rd")
    raw = secret.read_bytes()
    assert raw.startswith(DPAPI_MAGIC) and b"Pa" not in raw            # no plaintext on disk
    creds = load_credentials(secret)
    assert (creds.username, creds.password, creds.protection) == ("viewer", "Pa\"ss\\w0rd", "dpapi")
    assert lsetup.secret_problems(secret) == []                        # still private from creation


def test_drives_installed_path_is_the_shared_layout() -> None:
    from sentinel.drives.installed import installed_config_path

    if sys.platform == "win32":
        assert installed_config_path() == paths.default_layout("win32").config_file
    else:
        assert installed_config_path() == paths.default_layout(sys.platform, is_root=True).config_file


# ---------------------------------------------------------------------- (b) launcher defers to the service
def test_service_provider_is_first_and_only_answers_on_windows() -> None:
    assert control.CONTROL_PROVIDERS[0] is control._windows_service_provider
    assert wctl.service_control_provider(Path("x"), None, api=FakeScm([Info(1)]).calls(), platform="linux") is None


def test_provider_returns_a_service_control_whenever_installed_whatever_its_state() -> None:
    for state in (SERVICE_STOPPED, SERVICE_RUNNING):
        c = wctl.service_control_provider(Path("x"), None, api=FakeScm([Info(state)]).calls(), platform="win32")
        assert isinstance(c, wctl.ServiceControl) and c.service_managed
    assert wctl.service_control_provider(Path("x"), None, api=FakeScm([None]).calls(), platform="win32") is None


def test_provider_fails_closed_when_the_scm_cannot_be_asked() -> None:
    scm = FakeScm([None], query_raises=True)
    c = wctl.service_control_provider(Path("x"), None, api=scm.calls(), platform="win32")
    assert isinstance(c, wctl.ServiceControl)
    r = c.start()
    assert not r.ok and r.code == "C-003" and scm.started == 0          # never a detached fallback


def test_start_access_denied_is_c003_without_fallback() -> None:
    scm = FakeScm([Info(SERVICE_STOPPED)], start_err=ERROR_ACCESS_DENIED)
    r = _ctl(scm).start()
    assert not r.ok and r.code == "C-003" and "access denied" in r.detail


def test_start_confirms_running_and_maps_a_configuration_exit_to_c001() -> None:
    # States seen by start() after StartService returned (START_PENDING = 2).
    assert _ctl(FakeScm([Info(2), Info(2), Info(SERVICE_RUNNING)])).start().ok
    r = _ctl(FakeScm([Info(2), Info(SERVICE_STOPPED, 1066, 2)])).start()
    assert not r.ok and r.code == "C-001"
    r = _ctl(FakeScm([Info(SERVICE_STOPPED, 1066, 5)])).start()
    assert not r.ok and r.code == "C-003"
    assert _ctl(FakeScm([Info(2)])).start(confirm_s=3).ok      # still starting: not an error


def test_stop_waits_for_stopped() -> None:
    scm = FakeScm([Info(SERVICE_RUNNING), Info(3), Info(SERVICE_STOPPED)])
    assert _ctl(scm).stop(timeout_s=10) and scm.stopped == 1


def test_launcher_with_installed_service_only_views_the_status_folder(tmp_path: Path, monkeypatch) -> None:
    cfgf = tmp_path / "sentinel.toml"
    cfgf.write_text(
        '[camera]\nid = "CAM01"\nname = "Cam"\nurl = "rtsp://192.0.2.10:554/live"\n'
        f"[storage]\nspool_dir = '{tmp_path / 'spool'}'\ndata_dir = '{tmp_path / 'data'}'\n"
        f"log_dir = '{tmp_path / 'logs'}'\nstatus_dir = '{tmp_path / 'status'}'\n", encoding="utf-8")
    scm = FakeScm([Info(SERVICE_STOPPED), Info(SERVICE_RUNNING)])
    svc = _ctl(scm)
    seen: dict = {}

    class View:
        def run(self, status_path: Path, camera_name: str, **kw) -> int:
            seen.update(status_path=status_path, **kw)
            return 0

    def no_checks(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("service-managed launcher must not check or create the service's folders")

    monkeypatch.setattr(cli, "select_control", lambda p, c: svc)
    monkeypatch.setattr(cli, "select_view", lambda: View())
    monkeypatch.setattr(cli, "location_problems", no_checks)
    rc = cli.cmd_launch(argparse.Namespace(config=str(cfgf), once=True, no_start=False))
    assert rc == 0 and scm.started == 1
    assert seen["status_path"] == tmp_path / "status" / "status.json"
    assert seen["start_problem"] is None
    assert not (tmp_path / "data").exists()                              # no launcher.lock written


# ---------------------------------------------------------------------- merge 7: ledger chain x others
def _chain_db(tmp_path: Path) -> Path:
    from sentinel.db import Ledger

    led = Ledger(tmp_path / "data" / "sentinel.db")
    led.add_event("audit", "recorder", "service_start", "started")
    led.close()
    return tmp_path / "data" / "sentinel.db"


def test_chain_reader_retries_a_transient_error_like_ledger_readers(tmp_path: Path, monkeypatch) -> None:
    import sqlite3

    from sentinel import ledgerchain as lc

    db = _chain_db(tmp_path)
    real = sqlite3.connect
    calls = {"n": 0}

    def flaky(*a, **k):  # noqa: ANN002, ANN003
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(*a, **k)

    monkeypatch.setattr(lc.sqlite3, "connect", flaky)
    assert lc.current_chain_head(db) is not None and calls["n"] == 2
    calls["n"] = -100   # a non-transient error is not retried
    monkeypatch.setattr(lc.sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(
        sqlite3.OperationalError("no such table: x")))
    assert lc.current_chain_head(db) is None


def test_chain_verification_reads_the_status_anchor_from_status_dir(tmp_path: Path) -> None:
    import json

    from sentinel import ledgerchain as lc

    db = _chain_db(tmp_path)
    head = lc.current_chain_head(db)
    status = tmp_path / "status" / "status.json"
    status.parent.mkdir()
    status.write_text(json.dumps({"generated_utc": "2026-11-10T00:00:00Z",
                                  "integrity": {"ledger_chain": {"seq": head.seq, "hash": "f" * 64}}}), "utf-8")
    # A wrong head in the separate status folder must be noticed when that folder is passed ...
    assert not lc.verify_ledger(db, status_path=status).ok
    # ... and is not looked for beside the ledger when it is not (previous behaviour).
    assert lc.verify_ledger(db).ok
