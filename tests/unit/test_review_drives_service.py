"""Regression tests for the drives / Windows service / security findings of review 2026-11-10.

No drive is erased, no service is installed and no system folder is re-permissioned: ACL tests
work on pytest temporary folders, drive tests use the fakes in ``tests/drive_fakes.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from drive_fakes import FakeInspector, FakeRunner, Sequence, blank_disk, protected_for, system_disk

from sentinel import paths
from sentinel.launcher import setup

win = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
READ = "0x1200a9"      # read & execute
MODIFY = "0x1301bf"    # modify


def _me() -> str:
    from sentinel.platform import winacl
    return winacl.current_user_sid()


def _svc() -> str:
    from sentinel.platform import winacl
    return winacl.service_sid("SentinelRecorder")


def _sddl(p: Path) -> str:
    from sentinel.platform import winacl
    return winacl.get_sddl(p)


def _service_dir(path: Path, svc_rights: str, extra: str = "") -> None:
    """A folder as 'winsvc install' leaves it (plus this account, so the test can run unelevated)."""
    from sentinel.platform import winacl
    path.mkdir(parents=True, exist_ok=True)
    winacl.set_dacl(path, f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{_me()})"
                          f"(A;OICI;{svc_rights};;;{_svc()}){extra}")


def _restore_logging():  # noqa: ANN202
    import logging

    root = logging.getLogger()
    saved = list(root.handlers), root.level

    def restore() -> None:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])
    return restore


# ============================================================================ H1: setup --force + service
@win
def test_setup_force_keeps_the_installed_service_access(tmp_path: Path) -> None:
    root = tmp_path / "Sentinel"
    lay = paths.Layout("machine", root / "config" / "sentinel.toml", root / "config" / "camera.secret",
                       root / "spool", root / "data", root / "logs", root / "status")
    _service_dir(root / "config", READ, f"(A;OICI;{READ};;;BU)")
    for d in ("spool", "data", "logs"):
        _service_dir(root / d, MODIFY)
    _service_dir(root / "status", MODIFY, f"(A;OICI;{READ};;;BU)")
    (root / "spool" / "CAM01").mkdir()                          # recordings: not "Sentinel's own" names
    (root / "data" / "sentinel.db").write_bytes(b"x")
    lay.config_file.write_text("[camera]\n", encoding="utf-8")
    before = {d: _sddl(root / d) for d in ("config", "spool", "data", "logs", "status")}

    path, warnings = setup.write_setup(setup.SetupAnswers("192.0.2.10", "live", username="viewer",
                                                          password="pw12345"), lay, force=True, clock=lambda: 0.0)
    assert warnings == []
    assert {d: _sddl(root / d) for d in before} == before        # no folder DACL was replaced
    assert _svc() in _sddl(path)                                 # the service can still read the config
    secret = _sddl(lay.credentials_file)
    assert secret.startswith("D:P") and _svc() in secret and "BU" not in secret
    assert setup.secret_problems(lay.credentials_file, (_svc(),)) == []
    assert list(lay.config_file.parent.glob("sentinel.toml.*.bak"))


@win
def test_setup_force_warns_about_new_folders_the_service_cannot_use(tmp_path: Path) -> None:
    root = tmp_path / "Sentinel"
    lay = paths.Layout("machine", root / "config" / "sentinel.toml", root / "config" / "camera.secret",
                       root / "spool", root / "data", root / "logs", root / "status")
    _service_dir(root / "config", READ, f"(A;OICI;{READ};;;BU)")
    lay.config_file.write_text("[camera]\n", encoding="utf-8")
    _path, warnings = setup.write_setup(setup.SetupAnswers("192.0.2.10", "live"), lay, force=True)
    assert len(warnings) == 4 and all("service is installed" in w for w in warnings)


def _broadly_writable(path: Path) -> None:
    if sys.platform == "win32":
        from sentinel.platform import winacl
        winacl.set_dacl(path, f"D:P(A;OICI;FA;;;{_me()})(A;OICI;{MODIFY};;;BU)")
    else:
        os.chmod(path, 0o777)


def test_setup_checks_every_folder_before_changing_any(tmp_path: Path) -> None:
    root = tmp_path / "m"
    lay = paths.Layout("machine", root / "config" / "sentinel.toml", root / "config" / "camera.secret",
                       root / "spool", root / "data", root / "logs")
    (root / "config").mkdir(parents=True)
    lay.config_file.write_text("[camera]\n", encoding="utf-8")
    _broadly_writable(root / "config")                       # would be re-protected (own files only)
    (root / "spool").mkdir()
    (root / "spool" / "someone-elses.docx").write_text("x")
    _broadly_writable(root / "spool")                        # must stop setup
    before = _sddl(root / "config") if sys.platform == "win32" else (root / "config").stat().st_mode
    with pytest.raises(setup.SetupError, match="already contains other files"):
        setup.write_setup(setup.SetupAnswers("192.0.2.10", "live"), lay, force=True)
    after = _sddl(root / "config") if sys.platform == "win32" else (root / "config").stat().st_mode
    assert after == before, "the configuration folder was changed before setup refused"
    assert not (root / "data").exists()


@win
def test_service_managed_sddl_rules() -> None:
    me, svc = _me(), _svc()
    base = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;{READ};;;{svc})"
    assert setup.service_managed_sddl(base + f"(A;OICI;{READ};;;BU)", me)
    assert not setup.service_managed_sddl(base + f"(A;OICI;{MODIFY};;;BU)", me)       # others may write
    assert not setup.service_managed_sddl(base.replace("D:P", "D:"), me)               # inherits
    assert not setup.service_managed_sddl("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", me)  # no service


# ============================================================================ M: unreadable config in the service
def test_recorder_service_reports_an_unreadable_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.winsvc.__main__ import run_recorder_in_host

    cfg = tmp_path / "config" / "sentinel.toml"
    cfg.mkdir(parents=True)                       # opening it as a file fails with an OSError
    (tmp_path / "status").mkdir()
    lay = SimpleNamespace(config_file=cfg, status_dir=tmp_path / "status", log_dir=tmp_path / "logs")
    monkeypatch.setattr(paths, "default_layout", lambda *a, **k: lay)
    events: list[str] = []
    restore = _restore_logging()
    try:
        rc = run_recorder_in_host(SimpleNamespace(), str(cfg), event_log=events.append)
    finally:
        restore()
    assert rc == 2                                 # configuration/startup problem, not exit 4
    assert len(events) == 1 and "cannot be read" in events[0]
    st = json.loads((tmp_path / "status" / "status.json").read_text("utf-8"))
    assert st["terminal"] and st["alert"]["code"] == "C-001"


def test_archive_service_reports_an_unreadable_config(tmp_path: Path) -> None:
    from sentinel.winsvc.__main__ import run_archive_in_host

    cfg = tmp_path / "sentinel.toml"
    cfg.mkdir()
    events: list[str] = []
    assert run_archive_in_host(SimpleNamespace(), str(cfg), worker_factory=lambda c: 1 / 0,
                               event_log=events.append) == 2
    assert events and "cannot be read" in events[0]


def test_exit_4_meaning_points_at_the_logs() -> None:
    from sentinel.winsvc.__main__ import EXIT_MEANINGS

    assert "event log" in EXIT_MEANINGS[4]


# ============================================================================ L: archive lock held by another worker
def test_archive_service_exits_nonzero_when_the_worker_ends_by_itself(tmp_path: Path) -> None:
    from conftest import base_config_dict
    from gui_support import write_toml

    from sentinel.winsvc.__main__ import run_archive_in_host

    class Host:
        def on_stop(self, cb) -> None:  # noqa: ANN001
            self.cb = cb

        def report_running(self) -> None:
            pass

    class LockedWorker:          # ArchiveWorker.run returns at once when archive.lock is held
        def __init__(self, cfg) -> None:  # noqa: ANN001
            pass

        def run(self, whole_process: bool) -> None:
            return None

        def stop(self, timeout: float) -> None:
            pass

    d = base_config_dict(tmp_path)
    d["archive"] = {"enabled": True, "drive_path": str(tmp_path / "arch")}
    events: list[str] = []
    restore = _restore_logging()
    try:
        rc = run_archive_in_host(Host(), str(write_toml(tmp_path / "c.toml", d)), worker_factory=LockedWorker,
                                 event_log=events.append)
    finally:
        restore()
    assert rc == 3 and events and "without being asked to stop" in events[0]


# ============================================================================ L: credentials temp file race
@win
def test_credentials_temp_file_is_private_before_icacls(tmp_path: Path) -> None:
    from sentinel.platform import winacl
    from sentinel.winsvc.credstore import write_protected_credentials

    folder = tmp_path / "config"
    _service_dir(folder, READ, f"(A;OICI;{READ};;;BU)")        # Users may read, as after install
    seen: list[str] = []

    def run(argv):  # noqa: ANN001, ANN202
        tmp = Path(argv[1])
        assert tmp.stat().st_size == 0
        seen.append(winacl.get_sddl(tmp))
        return subprocess.CompletedProcess(argv, 0, "", "")

    write_protected_credentials(folder / "camera.secret", "viewer", "pw",
                                acl_argv=lambda p: ["icacls", str(p), "/inheritance:r"], run=run)
    assert len(seen) == 1
    assert seen[0].startswith("D:P") and not winacl.foreign_trustees(seen[0], _me()), seen[0]


@win
def test_write_private_file_extra_readers_are_read_only(tmp_path: Path) -> None:
    from sentinel.platform import winacl

    f = tmp_path / "s"
    winacl.write_private_file(f, b"x", extra_read_sids=(_svc(),))
    aces = {t: m for typ, _fl, m, t in winacl.dacl_aces(winacl.get_sddl(f)) if typ == "A"}
    assert aces[_svc()] & winacl.FILE_READ_DATA and not aces[_svc()] & winacl.WRITE_BITS
    with pytest.raises(winacl.AclError):
        winacl.write_private_file(tmp_path / "t", b"x", extra_read_sids=("BU)(A;;FA;;;WD",))


# ============================================================================ L: unreadable ledger is not "no ledger"
def _deny(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    real = os.stat

    def fake(p, *a, **k):  # noqa: ANN001, ANN202
        if Path(p).name in names:
            raise PermissionError(13, "Access is denied", str(p))
        return real(p, *a, **k)
    monkeypatch.setattr(os, "stat", fake)


def _cfg(tmp_path: Path):  # noqa: ANN202
    from sentinel.config import load_config_dict

    (tmp_path / "spool").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sentinel.db").write_bytes(b"")
    (tmp_path / "data" / "recorder.lock").write_bytes(b"")
    return load_config_dict({"camera": {"id": "CAM01", "url": "tcp://127.0.0.1:9", "development_source": True},
                             "storage": {"spool_dir": "spool", "data_dir": "data", "log_dir": "logs"}},
                            base_dir=tmp_path)


def test_installed_check_says_access_denied_not_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.drives.installed import installed_config_problems

    cfg = _cfg(tmp_path)
    cfgfile = tmp_path / "sentinel.toml"
    cfgfile.write_text("x")
    _deny(monkeypatch, "sentinel.db", "recorder.lock")
    problems = installed_config_problems(cfgfile, cfg, expected=cfgfile)
    text = " ".join(problems)
    assert "no recorder ledger" not in text and "never run" not in text
    assert len(problems) == 2 and all("administrator" in p for p in problems)


def test_drive_listing_fails_closed_when_the_ledger_is_unreadable(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.drives import cli
    from sentinel.drives.classify import classify_all
    from sentinel.drives.policy import DrivePolicy

    env = cli._Env.__new__(cli._Env)
    env.cfg = _cfg(tmp_path)
    _deny(monkeypatch, "sentinel.db")
    known = env.known()
    assert known.error and "administrator" in known.error
    classes, problems = classify_all([blank_disk(), system_disk()], DrivePolicy(erase_enabled=True), protected_for(),
                                     inspector=FakeInspector(), known=known)
    assert any(p.startswith("ledger:") for p in problems)
    assert not any(c.can_prepare for c in classes)


# ============================================================================ L: Linux helper and mounted drives
def _linux_disk():  # noqa: ANN202
    from dataclasses import replace
    return replace(blank_disk(), platform="linux", disk_id="sdb", device_path="/dev/sdb", os_devices=("8:16",))


def test_linux_script_unmounts_after_all_checks_and_reports_in_use() -> None:
    from sentinel.drives.commands import build_linux_script

    s = build_linux_script(_linux_disk(), filesystem="ntfs", label="SENTINEL_2", protected_devices=("252:0",))
    assert s.index("PROTECTED_device") < s.index("IDENTITY_MISMATCH_fsuuid") < s.index("umount --") \
        < s.index("wipefs --all")
    generic = [ln for ln in s.splitlines() if 'lsblk -no MOUNTPOINTS "$DEV"' in ln]
    assert generic and all("fail 5 IN_USE_mounted" in ln for ln in generic)
    assert "fail 4 PROTECTED_mounted" in s                  # system mount points still refuse as protected
    assert s.count("\nnotmounted\n") == 2                    # before wiping and again before partitioning


def test_helper_in_use_exit_is_reported_as_in_use(tmp_path: Path) -> None:
    from sentinel.db import Ledger
    from sentinel.drives.classify import classify
    from sentinel.drives.commands import EXIT_IN_USE
    from sentinel.drives.errors import PrepareRefused
    from sentinel.drives.policy import DrivePolicy
    from sentinel.drives.prepare import Authorization, Confirmation, HelperResult, PrepareContext, prepare_drive

    d = blank_disk()
    led = Ledger(tmp_path / "data" / "sentinel.db")
    try:
        runner = FakeRunner(HelperResult(EXIT_IN_USE, {"ok": False, "step": "verify", "error": "IN_USE_mounted"}))
        ctx = PrepareContext(policy=DrivePolicy(erase_enabled=True), protected=protected_for(),
                             enumerate=Sequence([d, system_disk()]), runner=runner, audit=led, platform="windows",
                             inspector=FakeInspector(), root_for_volume=lambda v: str(tmp_path / "vol"),
                             settle_s=0.0, work_dir=tmp_path / "work")
        conf = Confirmation.for_classification(classify(d, DrivePolicy(erase_enabled=True), protected_for(),
                                                        inspector=FakeInspector()))
        with pytest.raises(PrepareRefused) as ei:
            prepare_drive(ctx, d.disk_id, conf, Authorization("op", administrator=False, method="test"))
        assert ei.value.reason == "in_use" and "Nothing was erased" in str(ei.value)
        assert "used by the computer itself" not in str(ei.value)
    finally:
        led.close()


# ============================================================================ L: design doc installed path
def test_design_doc_gives_the_installed_config_path() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "branches" / "design-drive-prepare.md").read_text("utf-8")
    row = next(ln for ln in doc.splitlines() if ln.startswith("| 1.3 "))
    assert "%ProgramData%\\Sentinel\\config\\sentinel.toml" in row
    assert paths.default_layout("win32").config_file.parts[-2:] == ("config", "sentinel.toml")


# ============================================================================ window: marker role in list --json
@pytest.mark.parametrize("role,expected", [("archive", "archive"), (None, None), ("", None), (7, None)])
def test_classification_dict_has_the_marker_role(role, expected) -> None:  # noqa: ANN001
    from sentinel.drives.classify import classify
    from sentinel.drives.marker import MARKER_FORMAT, RootContents
    from sentinel.drives.model import DriveClass
    from sentinel.drives.policy import DrivePolicy
    from drive_fakes import data_part, msr_part, usb_disk, vol

    marker = {"format": MARKER_FORMAT, "version": 1, "drive_uuid": "0b0e5f0a-1c2d-4e3f-8a9b-0c1d2e3f4a5b",
              "label": "SENTINEL_3", "prepared_utc": "2026-11-09T00:00:00.000000Z", "prepared_by": "admin1"}
    if role is not None:
        marker["role"] = role
    d = usb_disk(partitions=(msr_part(), data_part(2, vol("00000000-0000-0000-0000-0000000000e5", label="SENTINEL_3"))))
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("SENTINEL_DRIVE.json",),
                                                                marker=marker)})
    c = classify(d, DrivePolicy(), protected_for(), inspector=insp)
    assert c.drive_class is DriveClass.READY
    out = c.to_dict()
    assert out["role"] == expected
    json.dumps(out)
    assert classify(blank_disk(), DrivePolicy(), protected_for(), inspector=FakeInspector()).to_dict()["role"] is None


# ============================================================================ install: operator requests folder
@pytest.mark.parametrize("readers,who", [("group", "Sentinel Operators"), ("users", "*S-1-5-32-545")])
def test_install_plan_creates_the_requests_folder_for_operators(readers: str, who: str) -> None:
    from sentinel.winsvc import OPERATORS_GROUP
    from sentinel.winsvc.commands import InstallInputs, host_command, install_plan

    cfg = Path(r"C:\ProgramData\Sentinel\config\sentinel.toml")
    inp = InstallInputs(cfg, (Path(r"D:\Sentinel\spool"), Path(r"C:\ProgramData\Sentinel\data"),
                              Path(r"C:\ProgramData\Sentinel\logs")), None,
                        host_command(cfg, frozen=True, executable=r"C:\Program Files\Sentinel\SentinelService.exe"),
                        status_dir=Path(r"C:\ProgramData\Sentinel\status"), status_readers=readers)
    assert readers == "users" or who == OPERATORS_GROUP
    steps = install_plan(inp, forbidden=[r"C:\Windows", r"C:\Program Files", r"C:\ProgramData", r"C:\Users"])
    req = r"C:\ProgramData\Sentinel\status\requests"
    mk = next(i for i, s in enumerate(steps) if s.action == "make_dirs")
    assert req in steps[mk].params["paths"]
    status_reset = next(i for i, s in enumerate(steps)
                        if s.argv and s.argv[:2] == ("icacls", r"C:\ProgramData\Sentinel\status\*"))
    grant = [i for i, s in enumerate(steps) if s.argv and s.argv[:2] == ("icacls", req)]
    assert len(grant) == 1 and grant[0] > status_reset > mk       # after the status folder's /reset
    argv = steps[grant[0]].argv
    assert argv == ("icacls", req, "/grant", f"{who}:(RD,X,RA,REA,RC,S,WD)", f"{who}:(OI)(IO)M", "/Q")
    assert not steps[grant[0]].may_fail
    # The status folder itself stays read-only for them.
    status = next(s for s in steps if s.argv and s.argv[:2] == ("icacls", r"C:\ProgramData\Sentinel\status"))
    assert f"{who}:(OI)(CI)RX" in status.argv and not any(a.startswith(f"{who}:") and a.endswith("M")
                                                           for a in status.argv)
